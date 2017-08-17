import pandas as pd

from toolz.dicttoolz import merge

from sanic import Sanic
from sanic.config import Config
from sanic.response import text, json
from sanic.exceptions import InvalidUsage, ServerError, NotFound

from survey_stats import log
from survey_stats import settings
from survey_stats import fetch
from survey_stats import state as st

Config.REQUEST_TIMEOUT = 50000000

app = Sanic(__name__)

logger = log.getLogger()


@app.route("/questions/v2")
def fetch_questions(req):
    dset=req.args.get('d')
    national = True
    combined = True
    svy = st.dset[dset].fetch_survey(combined, national)
    res = []
    if svy:
        res = {k: get_meta(k, v, dset) for k, v in svy.vars.items()}
    else:
        qnkey = st.meta[dset].config['qnkey']
        res = st.meta[dset].qnmeta.reset_index(level=0)
        sl_res =[]
        sl_res = res[['facet', 'facet_description',
            'facet_level', 'facet_level_value']].drop_duplicates()
        sl_res = {f[0]: {
            'facet':f[0],
            'facet_description': f[1]['facet_description'].get_values()[0],
            'levels': dict(list(f[1][['facet_level','facet_level_value']].to_records(index=False)))
            } for f in sl_res.groupby('facet')}

        qn_res = res[['questionid', 'topic', 'subtopic', 'question', 'response']].groupby('questionid').agg({
            'topic': lambda x: x.head(1).get_values()[0],
            'subtopic': lambda x: x.get_values()[0],
            'question': lambda x: x.get_values()[0],
            'response': lambda x: list(x.drop_duplicates())
        })
        qn_res = qn_res.to_dict(orient='index')
        res = {'facets':sl_res, 'questions':qn_res}
        #logger.info(res)
    return json(res)

@app.route("/questions")
def fetch_questions(req):
    def get_meta(k, v, dset):
        key = k.lower()
        res = (dict(st.meta[dset].qnmeta.ix[key].ix[0].to_dict(), **v, id=k) if key in
               st.meta[dset].qnmeta.index else dict(v, id=k))
        return res
    dset=req.args.get('d')
    national = True
    combined = True
    svy = st.dset[dset].fetch_survey(combined, national)
    res = []
    sl_res = {}
    if svy:
        res = {k: get_meta(k, v, dset) for k, v in svy.vars.items()}
        # removed  responses
        for key,value in res.items():
            del value['responses']
            del value['is_integer']
            if 'response' in value:
               del value['response']
    else:
        qnkey = st.meta[dset].config['qnkey']
        res = st.meta[dset].qnmeta.reset_index()
        sl_res =[]
        sl_res = res[['facet', 'facet_description',
            'facet_level', 'facet_level_value']].drop_duplicates()
        sl_res = {f[0]: {
            'facet':f[0],
            'facet_description': f[1]['facet_description'].get_values()[0],
            'levels': dict(list(f[1][['facet_level','facet_level_value']].to_records(index=False)))
            } for f in sl_res.groupby('facet')}

        qn_res = res[['questionid', 'topic', 'subtopic', 'question', 'response']].groupby('questionid').agg({
            'topic': lambda x: x.head(1).get_values()[0],
            'subtopic': lambda x: x.get_values()[0],
            'question': lambda x: x.get_values()[0],
            'response': lambda x: list(x.drop_duplicates())
        })
        qn_res['class'] = qn_res['topic']
        qn_res['topic'] = qn_res['subtopic']
        del qn_res['subtopic']
        qn_res = qn_res.to_dict(orient='index')
        res = qn_res
        #logger.info(res)
    # looping through dictionary and replacing all 'nan' values with empty string
    for value in res.values():
        for i in value.keys():
            item = value[i]
            if isinstance(item, float) and math.isnan(item):
               value[i] = ''
    res = {'questions': res, 'facets': sl_res}
    return json(res)



def gen_slices(k, svy, qn, resp, m_vars, m_filt):
    loc = {'svy_id': k, 'dset_id': 'yrbss'}
    slices = [merge(loc, s)
        for s in svy.generate_slices(qn, True, m_vars, m_filt) ]
    slices += [merge(loc, s)
        for s in svy.generate_slices(qn, False, m_vars, m_filt) ]
    return slices

async def fetch_computed(k, svy, qn, resp, m_vars, m_filt, cfg):
    slices = gen_slices(k, svy, qn, resp, m_vars, m_filt)
    results = await fetch.fetch_all(slices)
    results = [remap_vars(cfg, x, into=False) for x in results]
    return results

def fetch_socrata(qn, resp, vars, filt, meta):
    precomp = meta.fetch_dash(qn, resp, vars, filt)
    precomp = pd.DataFrame(precomp).fillna(-1)
    precomp['method']='socrata'
    return precomp.to_dict(orient='records')


def parse_filter(f):
    return dict(map(lambda fv: (fv.split(':')[0],
                                fv.split(':')[1].split(',')), f.split('|')))

def parse_response(r):
    if r.lower() == 'yes' or r.lower() == 'true' or r.lower() == '1':
        return True
    elif r.lower() == 'no' or r.lower() == 'false' or r.lower() == '0':
        return False
    else:
        raise Exception('Invalid response value specified!')

@app.route('/stats')
async def fetch_survey_stats(req):
    dset = req.args.get('d')
    qn = req.args.get('q')
    vars = [] if not 'v' in req.args else req.args.get('v').split(',')
    resp = None if not 'r' in req.args else parse_response(req.args.get('r'))
    filt = {} if not 'f' in req.args else parse_filter(req.args.get('f'))
    use_socrata = False if not 's' in req.args else not 0 ** int(req.args.get('s'), 2)
    meta = st.meta[dset]
    question = qn #meta.qnmeta[qn]
    results = None #fetch_socrata(qn, resp, vars, filt, national, meta)
    error = None
    #try:
    if not use_socrata:
        (k, cfg) = st.dset[dset].fetch_config(national=True, year=None)
        svy = st.dset[dset].surveys[k]
        m_filt = remap_vars(cfg, filt, into=True)
        m_vars = remap_vars(cfg, vars, into=True)
        if not svy.subset(m_filt).sample_size > 1:
            raise SSEmptyFilterError('EmptyFilterError: %s' % (str(m_filt)))
        question = svy.vars[qn]['question']
        var_levels = remap_vars(cfg, {v: svy.vars[v] for v in m_vars}, into=False)
        results = await fetch_computed(k, svy, qn, resp, m_vars, m_filt, cfg)
    else:
        results = fetch_socrata(qn, resp, vars, filt, meta)
    #except Exception as e:
    #    error = str(e)
    return json({
        'error': error,
        'q': qn,
        'filter': filt,
        'question': question,
        'response': resp,
        'vars': vars,
        'var_levels': None, #var_levels,
        'results': results,
        'is_socrata':use_socrata
    })



def setup_app(db_conf, stats_svc):
    app.config.db_conf = db_conf
    app.config.stats_svc = stats_svc
    return app


def serve_app(host, port, workers, stats_svc, debug):
    app.run(host=host, port=port, workers=workers, debug=debug)

if __name__ == '__main__':
    serve_app(host='0.0.0.0', port=7778, workers=1, debug=True)
