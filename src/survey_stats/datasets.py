import blaze as bz
from odo import odo
import feather
import pandas as pd
import numpy as np
import rpy2
from rpy2.robjects import Formula
import types
import attr
from cattr import typed
from pathlib import Path
from typing import Union, Optional, Sequence
from cytoolz.curried import map, curry
from cytoolz.functoolz import identity, pipe
from cytoolz.dicttoolz import keymap, valmap, keyfilter
from cached_property import threaded_cached_property
from survey_stats import log
from survey_stats import pdutil as u
from survey_stats.types import DatasetConfig, ColumnFilter
from survey_stats.dbi import DatabaseConfig, DatasetPart, get_datafile_path
from survey_stats.const import DBTBL_FMT
from survey_stats.const import DECIMALS, ID_COLUMN, ANNO_COLUMNS, STATS_COLUMNS


logger = log.getLogger()


def get_if(x, d):
    return d[x] if x in d else x

def get_unique_aggvals(xf):
    return xf.drop_duplicates().tolist()

def map_with_dict(d, val):
    repl_f = curry(get_if)(d=d)
    typ = type(val)
    if typ == str:
        return repl_f(val)
    if typ == list or typ == set:
        return typ(map(repl_f, val))
    if typ == dict:
        return keymap(repl_f, val)


def hydrate_dataset_part(part, dbc, cdir, dsid, as_blaze=True):
    if dbc is not None:
        logger.info('hydrating with database table')
        res = pipe(part.value,
                   lambda x: DBTBL_FMT.format(dsid=dsid, part=x),
                   dbc.resolve_table)
        res = res if as_blaze else odo(res, pd.DataFrame)
        return res
    else:
        logger.info('hydrating with feather file')
        bzfn = bz.data if as_blaze else identity
        res = pipe(part.value,
                   curry(get_datafile_path)(dsid=dsid, cdir=cdir),
                   feather.read_dataframe,
                   bzfn)
        return res


@attr.s(frozen=True)
class SurveyMeta(object):

    dsid = typed(str)
    strata = typed(Sequence[str])
    facets = typed(Sequence[str])
    national = typed(ColumnFilter)
    qns = typed(Union[pd.DataFrame, pd.core.frame.DataFrame])
    flevels = typed(Union[pd.DataFrame, pd.core.frame.DataFrame])
    parts = typed(Sequence[DatasetPart])

    @classmethod
    def load_metadata(cls, cfg, cdir):
        dsid = cfg.id
        qns = hydrate_dataset_part(DatasetPart.SCHEMA, None, cdir, dsid, as_blaze=False)
        flevels = hydrate_dataset_part(DatasetPart.FACETS, None, cdir, dsid, as_blaze=False)
        logger.info('initializing survey meta', t_qns=qns.head(), t_lvls=flevels.head())
        parts = []
        if cfg.surveys:
            parts.append(DatasetPart.SURVEYS)
        if cfg.socrata:
            parts.append(DatasetPart.SOCRATA)
        logger.info('found parts', p=parts, svy=cfg.surveys, soc=cfg.socrata)
        return cls(dsid=dsid, strata=cfg.strata,
                   facets=cfg.facets, national=cfg.national,
                   qns=qns, flevels=flevels, parts=parts)

    @threaded_cached_property
    def has_socrata(self):
        return DatasetPart.SOCRATA in self.parts
    
    @threaded_cached_property
    def has_surveys(self):
        return DatasetPart.SURVEYS in self.parts

    @threaded_cached_property
    def facet_map(self):
        facs = (self.flevels
                .groupby(['facet'])
                .agg({'facet_level':
                      lambda x: x.drop_duplicates().tolist()})
                .pipe(lambda xf: u.fill_none(xf))
                .to_dict(orient='index'))
        return pipe(facs,
                    curry(valmap)(lambda x: x['facet_level']),
                    curry(keyfilter)(lambda x: x != 'Overall'))

    @threaded_cached_property
    def questions(self, year=True, sitecode=True):
        def get_first_aggval(xf):
            return xf.get_values()[0]
        # optional metadata to add to questions
        opts = (['year'] if year else []) + \
            (['sitecode'] if sitecode else [])
        # columns to exclude before aggregating question metadata
        # consists of the groupby column, qid, plus unused optional cols
        excl = set([ID_COLUMN] + self.strata).difference(opts)
        # columns that need to be uniqued -- otherwise assume constant for each qid
        uniq = ['response'] + opts
        # columns to aggregate over
        dkeys = set(self.qns.columns).difference(excl)
        # aggregation function for each col
        # -- first value for const cols
        # -- deduplication function for uniq cols
        aggd = {k: get_unique_aggvals if
                k in uniq else get_first_aggval
                for k in dkeys}
        res = (self.qns
               .groupby([ID_COLUMN])
               .agg(aggd)
               .reset_index()
               .pipe(lambda xf: u.fill_none(xf))
               .set_index([ID_COLUMN])
               .to_dict(orient='index'))
        return res

    @threaded_cached_property
    def vars(self):
        return self.strata + self.facets

    @threaded_cached_property
    def facet_levels(self):
        return {k: list(self.meta_db[k].cat.categories)
                for k in self.strata + self.facets}


@attr.s(slots=True, frozen=True)
class SurveyDataset(object):

    dsid = typed(str)
    dbc = typed(Optional[DatabaseConfig])
    cdir = typed(Path)
    meta = typed(SurveyMeta)
    soc = typed(Optional[bz.interactive._Data])
    svy = typed(Optional[bz.interactive._Data])
    des = typed(Optional[rpy2.robjects.vectors.ListVector])
    mapper = typed(Optional[Union[types.FunctionType,
                                  types.LambdaType]])

    @classmethod
    def load_dataset(cls, cfg_f, dbc, cdir, init_des=False, 
                     use_feather=False, init_soc=True, init_svy=True):
        # given a config file and blaze data handle,
        # work some magic
        cfg = DatasetConfig.from_yaml(cfg_f)
        dsid = cfg.id
        meta = SurveyMeta.load_metadata(cfg, cdir)
        use_db = dbc is not None and use_feather is False
        svytbl = hydrate_dataset_part(DatasetPart.SURVEYS, None, cdir, dsid) \
            if cfg.surveys and init_svy else None
        soctbl = hydrate_dataset_part(DatasetPart.SOCRATA, None, cdir, dsid) \
            if cfg.socrata and init_soc else None
        # year is a reserved keyword in monetdb so work around
        mapper = curry(map_with_dict)({'year': 'yr'}) if use_db else identity
        des = None
        if cfg.surveys and init_des:
            from survey_stats.survey import des_from_survey_db, des_from_feather
            des = des_from_survey_db(
                DBTBL_FMT.format(dsid=dsid, part=DatasetPart.SURVEYS.value),
                dbc.name, dbc.host, dbc.port, denovo=cfg.surveys.denovo_strata
            ) if use_db else des_from_feather(get_datafile_path(
                DatasetPart.SURVEYS.value, dsid, cdir),
                                              denovo=cfg.surveys.denovo_strata)
        return cls(dsid=dsid, dbc=dbc, cdir=cdir, meta=meta, svy=svytbl, soc=soctbl, des=des, mapper=mapper)

    def hydrate_part(self, part):
        return hydrate_dataset_part(part, self.dbc, self.cdir, self.dsid)

    def values_for_col(self, col, exclude=[]):
        r = odo(self.svy[col].distinct(), pd.DataFrame)
        vals = list(r.dropna()[col])
        return list(set(vals)
                    .difference(exclude))

    def fetch_socrata(self, qn, vars, filt={}):
        vars = self.mapper(vars)
        filt = self.mapper(filt)
        sel = None
        df = self.soc
        fcts = list(set(self.meta.facets)
                    .intersection(df.fields))
        sel = df[ID_COLUMN] == qn
        site_col = self.mapper('sitecode')
        year_col = self.mapper('year')
        if site_col in filt.keys():
            sel = sel & df[site_col].isin(filt[site_col])
        elif site_col not in vars:
            sel = sel & (df[site_col] == 'XX')
        if year_col in filt.keys():
            sel = sel & df[year_col].isin(filt[year_col])
        elif year_col not in vars:
            sel = sel & (df[year_col] == 'Total')
        for v in fcts:
            if v in filt.keys():
                sel = sel & df[v].isin(filt[v])
            elif v not in vars:
                sel = sel & (df[v] == 'Total')
        cols = (set(ANNO_COLUMNS)
                .union(vars)
                .union(STATS_COLUMNS)
                .intersection(df.fields))
        cols = list(cols)
        dfz = odo(df[sel][cols], pd.DataFrame)
        stats_sub = list(set(STATS_COLUMNS).intersection(dfz.columns))
        dfz[stats_sub] = dfz[stats_sub].apply(
            lambda xf: xf.astype(float).replace(-1.0, np.nan)
        )
        # logger.info('done filtering, replacing NaNs', dfz=dfz)
        return u.fill_none(dfz.round(DECIMALS).reset_index(drop=True))

    def fetch_stats(self, qn, vars=[], filt={}):
        vars = self.mapper(vars)
        filt = self.mapper(filt)
        lvls = self.responses_for_qn(qn)
        res = map(lambda r: fetch_stats(self.des, qn, r, vars, filt), lvls)
        dfz = pd.concat(res, ignore_index=True)
        return dfz

    def fetch_stats_for_slice(self, qn, r, vars=[], filt={}):
        from survey_stats.survey import (fetch_stats_totals, 
                                         fetch_stats_by, subset_survey,
                                         dim_design)
        vars = self.mapper(vars)
        filt = self.mapper(filt)
        qn_f = '~I(%s=="%s")' % (qn, r)
        logger.info('subsetting des with filter', filt=filt, v=vars, q=qn, r=r)
        des = subset_survey(self.des, filt, qn)
        dsubs = dim_design(des) 
        if dsubs[0] == 0:
            logger.info('subsetting yields empty df, returning empty result', dim=dsubs)
            cols = ['level'+'response'] + vars + \
                ['mean', 'se', 'ci_l', 'ci_u', 'count', 'sample_size']
            return pd.DataFrame(columns=cols)
        ret = None
        if len(vars) > 0:
            logger.info('fetching stats with var levels', vs=vars, qn=qn, r=r)
            ret = fetch_stats_by(des, qn_f, r, vars)
        else:
            logger.info('fetching top level stats', qn=qn, r=r)
            ret = fetch_stats_totals(des, qn_f, r)
        return ret

    def generate_slices(self, qn, vars=[], filt={}):
        vars = self.mapper(vars)
        filt = self.mapper(filt)
        site_col = self.mapper('sitecode')
        if not site_col in filt:
            # no sitecode in filter 
            # -- national
            nat = self.meta.national
            filt[site_col] = nat.vals if nat.incl else self.values_for_col(site_col, exclude=nat.vals)
        resps = self.values_for_col(qn)
        vlvls = [vars[:k+1] for k in range(len(vars))]
        res = []
        d = self.dsid
        logger.info('mapping slices over resps', r=resps)
        for r in resps:
            top = [{'d': d, 'q': qn, 'r': r, 'f': filt, 'vs': []}]
            rs = [{'d': d, 'q': qn, 'r': r, 'f': filt, 'vs': vs} for vs in vlvls]
            res = res + rs + top
        logger.info(res)
        return res

    def generate_sample_sizes(self, qn, vars=[], filt={}):
        vars = self.mapper(vars)
        filt = self.mapper(filt)
        ret = None
        return ret
