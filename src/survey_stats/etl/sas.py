import io
import re
import zipfile
import pandas as pd
import asteval
from cytoolz.curried import map, filter, curry, mapcat
from cytoolz.functoolz import pipe, thread_last, identity
from dask import delayed
from survey_stats import log
from survey_stats.etl import survey_df as sdf
from survey_stats.etl import download as dl

logger = log.getLogger(__name__)


def parse_questions(txt):
    rqt = re.compile(r'[\"\']')  # match quote chars
    assignments = thread_last(
        txt.split(';'),
        filter(lambda x: x.strip().lower().startswith('label')),
        mapcat(lambda x: x.lower().split('\n')),
        map(lambda x: x.split('=')),  # break out vars and format
        (map, lambda y: (y[0].strip().lower(), rqt.sub('', y[1].strip()))),  # tuple of var, fmt
        dict
    )
    return assignments

def parse_format_assignments(txt):
    assignments = thread_last(
        txt.split(';'),
        filter(lambda x: x.strip().lower().startswith('format')),
        mapcat(lambda x: x.lower().split('.')),
        map(lambda x: x.split()),  # break out vars and format
        (mapcat, lambda y: [(k, y[-1]) for k in y]),  # tuple of var, fmt
        dict
    )
    return assignments


def replace_if_keyed(k, repl):
    return repl[k] if k in repl else k


def block2dict(lines, repl, to_lower=False):
    f_lwr = str.lower if to_lower else identity
    f_repl = curry(lambda k, r: r[k] if k in r else k)(r=repl)
    rqt = re.compile(r'[\"\']')  # match quote chars
    rws = re.compile(r'\s')        # match whitespace
    # keep only alnum and a few unreserved symbols
    ruri = re.compile(r'(?![\w\s\-\_\.\'\$\-\+\(\)\/]|\.).')
    d = thread_last(
        lines,
        map(lambda x: x.replace('\x92', "'")),
        map(lambda x: rqt.sub('', x.strip()).split('=')),
        map(lambda x: (rws.sub('', x[0].strip()), ruri.sub('', x[1].strip()))),
        filter(lambda x: x[0].find('-') == -1),  # no support for ranges
        (mapcat, lambda x: map(lambda y: (y, x[1]), x[0].split(','))),
        filter(lambda x: x[0].isnumeric()),  # remove non-numeric codes
        map(lambda x: (int(x[0]),  # cat codes are ints
                       pipe(x[1], f_lwr, f_repl))),
        dict
    )
    # d[-1] = np.nan #use NA as a marker for unmapped vals
    return d


def parse_variable_labels(txt, repl, lbls_to_lower=True):
    b2d = curry(block2dict)(repl=repl, to_lower=lbls_to_lower)
    labels = thread_last(
        txt.split(';'),
        filter(lambda x: x.strip().lower().startswith('value')),
        map(lambda x: x.strip().split('\n')),
        map(lambda x: (x[0].split()[1].lower(), b2d(x[1:]))),
        dict
    )
    logger.info('parsed varlabels from format txt',
                nlabeled=len(labels.keys()), nrepl=len(repl.keys()))
    return labels


def load_variable_labels(format_f, formas_f, repl, year=None):
    logger.info("loading format labels", file=format_f)
    labels = thread_last(
        format_f,
        dl.fetch_data_from_url,
        lambda x: x.read(),
        lambda t: (t.decode('utf-8', errors='ignore')
                   if type(t) is bytes else t),
        curry(parse_variable_labels)(repl=repl)
    )
    logger.info("loaded format labels", lbls=labels)
    logger.info("loading format assignments", file=formas_f)
    assignments = thread_last(
        formas_f,
        dl.fetch_data_from_url,
        lambda x: x.read(),
        lambda t: (t.decode('utf-8', errors='ignore')
                   if type(t) is bytes else t),
        parse_format_assignments
    )
    logger.info("loaded format assns", ass=assignments)
    return {k: labels[v] for k, v in assignments.items() if v in labels}


def varlabels2df(vlbls, yr=None):
    return thread_last(
        vlbls.items(),
        map(lambda k, v: pd.DataFrame({'code': list(v.keys()),
                                       'label': list(v.values()),
                                       'var': k})),
        map(lambda df: df.assign(year=yr) if yr else df),
        pd.concat,
        lambda df: (df.set_index(['var', 'year', 'code'])
                    if yr else df.set_index(['var', 'code']))
    )


def load_sas_from_zip(fh, format):
    with zipfile.ZipFile(io.BytesIO(fh.read())) as zipf:
        with zipf.open(zipf.namelist()[0]) as fh:
            return pd.read_sas(fh, format=format)


def load_sas_from_url(url, format, lgr=logger):
    fh = dl.fetch_data_from_url(url)
    df = (load_sas_from_zip(fh, format)
          if url[-3:].lower() == 'zip'
          else (pd.read_csv(fh, sep='\t') if 
          url[-3:].lower() == 'tsv' else 
          pd.read_sas(fh, format=format)))
    logger.info("loaded SAS XPORT file", shape=df.shape)
    return df


def load_sas_xport_df(url, lgr=logger):
    df = load_sas_from_url(url, 'sas7bdat' if url.find('sas7bdat') > -1 else 'xport', lgr)
    df.columns = [x.lower() for x in df.columns]
    return df


def process_sas_survey(svy_cfg, facets, client=None, lgr=logger):
    g = svy_cfg
    prefix = g.s3_url_prefix
    lgr.bind(p=prefix)
    evalr = asteval.Interpreter()
    evalr.symtable['pd.util'] = pd.util
    fn = g.rename_cols
    map_fn = evalr(fn)
    df_munger = curry(sdf.munge_df)(facets=facets, qids=g.qids,
                                    na_syns=g.na_synonyms, col_fn=map_fn,
                                    fmts=g.patch_format, fpc=g.fpc, lgr=lgr)
    lbl_loader = curry(load_variable_labels)(repl=g.replace_labels)
    xpt_loader = curry(load_sas_xport_df)(lgr=lgr)
    dfs = map(
        lambda r: pipe(prefix+r.xpt,
                       delayed(xpt_loader),
                       delayed(df_munger(r=r,
                                         lbls=lbl_loader(prefix+r.format,
                                                         prefix+r.formas)))),
        [r for idx, r in g.meta.iterrows()])
    lgr.info('merging SAS dfs')
    dfs = delayed(pd.concat)(dfs, ignore_index=True)
    scols = delayed(
        lambda xf: list(xf.columns
                          .intersection(set(g.qids)
                                        .union(facets))))(dfs)
    lgr.info('re-filtering question and facet columns to cast to category dtype', cols=scols)
    dfz = (dfs
           .apply(lambda x: x.astype('category'))
           .reset_index(drop=True)
           .assign(year=dfs['year'].astype(int),
                   sitecode=dfs['sitecode'].astype('category'),
                   weight=dfs['weight'].astype(float),
                   strata=dfs['strata'].astype(int, errors='ignore'),
                   psu=dfs['psu'].astype(int, errors='ignore'))
           .reset_index(drop=True))
    if g.fpc:
        dfz = (dfz.assign(fpc=dfs['fpc'].astype(int, errors='ignore'),
                         sample_ct=dfs['sample_ct'].astype(int, errors='ignore'))
                  .reset_index(drop=True))
    dfz.visualize()
    lgr.info('merged SAS dfs')
    lgr.unbind('p')
    return dfz


'''
TODO: fetch questions from SAS files
and join with those from socrata_key4id
def parse_qn_meta()
    logger.info("loading format assignments", file=formas_f)
def process_sas_metadata(meta, facets, prefix, qids, na_syns,
                       repl, fmts, client=None, lgr=logger):
    lgr.bind(p=prefix)
    md = pd.DataFrame(meta['rows'], columns=meta['cols'])
    lbl_loader = curry(load_variable_labels)(repl=repl)
    dfs = thread_last([r for idx, r in md.iterrows()],
                      map(lambda r: lbl_loader(prefix+r.format, prefix+r.parse_format_assignments)),

    lgr.info('merging SAS dfs')
    dfs = delayed(pd.concat)(dfs, ignore_index=True)
'''
