import us
import pandas as pd
import numpy as np
from cytoolz.itertoolz import mapcat
from cytoolz.functoolz import thread_last
from cytoolz.curried import map, filter

import dask
from dask import dataframe as dd
from dask.distributed import Executor
from dask.delayed import delayed

from survey_stats import log
from survey_stats import pdutil

logger = log.getLogger(__name__)


US_STATES_FIPS_INTS = thread_last(
    us.STATES_AND_TERRITORIES,
    map(lambda x: x.fips),
    filter(lambda x: x is not None),
    map(lambda x: int(x)),
    list
)

SITECODE_TRANSLATORS = {
    'fips': lambda x: (us.states.lookup('%.2d' % x).abbr if int(x) in US_STATES_FIPS_INTS else 'NA')
}

SVYDESIGN_COLS = ['sitecode', 'strata', 'psu', 'weight']

@dask.delayed
def force_convert_categorical(s, lbls):
    c = (pd.to_numeric(s.fillna(-1), downcast='integer')
         .replace(to_replace=lbls[s.name])
         .astype('category'))
    #logger.info('forced cat conversion', c=str(c.value_counts(dropna=False)),
    #            c_desc=str(c.describe()))
    return c

@dask.delayed
def eager_convert_categorical(s, lbls):
    if not s.name in lbls.keys():
        return s
    try:
        c = (pd.to_numeric(s.fillna(-1), downcast='integer')
             .astype('category'))
        #logger.info('eager conv - 1', summ=c.value_counts(dropna=False).to_dict(),
        #            labels=lbls[s.name])
        c = c.cat.rename_categories(
            [lbls[s.name][k] for k in sorted(c.unique())])
        #logger.info('eager conv - 2', summ=c.value_counts(dropna=False).to_dict(),
        #            keys=sorted(lbls[s.name].keys()))
        c = (c.cat.set_categories(
            [lbls[s.name][k] for k in sorted(lbls[s.name].keys())])
            .astype('category'))
        #logger.info('eager conv - 3', summ=c.value_counts(dropna=False).to_dict(),
        #            desc = str(c.describe()))
        return c
    except KeyError:
        return s
    except ValueError as e:
        logger.info('found value err', err=e, c=str(c.value_counts(dropna=False)),
                    c_desc=c.describe())
        return force_convert_categorical(s, lbls)

@dask.delayed
def filter_columns(df, facets, qids):
    set_union = lambda x,y: y.union(x)
    cols = thread_last(set(qids),
                       (set_union, facets.keys()),
                       lambda x: x.intersection(df.columns),
                       list,
                       sorted)
    ndf = df[cols]
    logger.info("filtered df columns",
                filtered=','.join(cols),
                missing=set(cols).difference(df.columns),
                ncols=len(cols), old_shape=df.shape, new_shape=ndf.shape)
    return ndf

@dask.delayed
def munge_df(df, lbls, facets, year, sitecode, weight, strata, psu, qids):
    logger.info('filtering, applying varlabels, munging')
    ndf = (df.pipe(lambda xdf: filter_columns(xdf, facets, qids))
           .apply(lambda x: eager_convert_categorical(x, lbls))
           .rename(index=str, columns=facets)
		   .reset_index(drop=True)
           .assign(year = int(year) if type(year) == int else df[year].astype(int),
                   sitecode = df[sitecode].apply(
                        SITECODE_TRANSLATORS['fips']).astype('category'),
                   weight = df[weight].astype(float),
                   strata = df[strata].astype(int),
                   psu = df[psu].astype(int))
           )
    logger.info('completed SAS df munging',
                summary=ndf.dtypes.value_counts(dropna=False).to_dict(),
                shape=ndf.shape, dups=list(ndf.columns[ndf.columns.duplicated()]),
                weights=ndf['weight'].describe().round(2).to_dict())
    return ndf.reset_index(drop=True)

@dask.delayed
def find_na_synonyms(df, na_syns):
    df = df.applymap(
        lambda x: np.nan if
        (x.lower() in na_syns if
            type(x) == str else
            False)
        else x)
    return df

def merge_multiyear_surveys(dfs, na_syns):
    logger.info('merging SAS dfs')
    undash_fn = lambda x: 'x' + x if x[0] == '_' else x
    dfs = (dd.concat(dfs, ignore_index=True, axis=0, join='outer')
           .pipe(lambda xf: find_na_synonyms(xf, na_syns))
           .apply(lambda x: x.fillna('NA').astype('category') if
                 x.dtype.name in ['O','object'] else (x.fillna('NA').astype('category') if x.dtype.name in ['category']
										else x.fillna(-1)))
           .pipe(lambda xf: xf.rename(index=str, columns={x:undash_fn(x) for x
                                                          in xf.columns})))
    logger.info('merged SAS dfs', shape=dfs.shape,
                 summary=dfs.dtypes.value_counts(dropna=False).to_dict(),
				 n_levels=dfs.select_dtypes(include=['O','category']).apply(lambda x: x.value_counts()),
				 levels=dfs.select_dtypes(include=['category']).apply(lambda x: x.categories),
				 weights=dfs['weight'].describe(), strata=dfs['strata'].describe(),
				 psu=dfs['psu'].describe(), desc=dfs.describe())
    return dfs
