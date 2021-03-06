from pathlib import Path

import fire
import logging
import numpy as np
import pandas as pd
import re

DB_PATH = Path('databases')
MIMIC4_PATH = DB_PATH / 'mimic4'

logging.basicConfig(
    filename='extract-dataset.logs',
    filemode='a',
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
    level=logging.DEBUG,
)
logger = logging.getLogger('default')

LABEVENTS_FEATURES = {
    'bicarbonate': [50882],  # mEq/L
    'chloride': [50902],
    'creatinine': [50912],
    'glucose': [50931],
    'magnesium': [50960],
    'potassium': [50822, 50971],  # mEq/L
    'sodium': [50824, 50983],  # mEq/L == mmol/L
    'bun': [51006],
    'hemoglobin': [51222],
    'platelets': [51265],
    'wbcs': [51300, 51301],
}
CHARTEVENTS_FEATURES = {
    'height': [
        226707,  # inches
        226730,  # cm
        1394,  # inches
    ],
    'weight': [
        763,  # kg
        224639,  # kg
    ]
}


def partition_rows(input_path, output_path):
    '''
    Reads in the combined chartevents and labevents csv file (filtered_events.csv)
    and aggregates the features values with respect to each ICU day of the different 
    patients (feature values as the columns, ICU day as the rows).

    Parameters:
    input_path: the path of the input csv to be processed (e.g., filtered_events.csv)
    output_path: the path as to where the output of this step should be dumped
    '''
    logger.info('`partition_rows` has started')
    df = pd.read_csv(input_path)
    df.columns = map(str.lower, df.columns)

    # extract the day of the event
    df['chartday'] = df['charttime'].astype(
        'str').str.split(' ').apply(lambda x: x[0])

    # group day into a specific ICU stay
    df['stay_day'] = df['stay_id'].astype('str') + '_' + df['chartday']

    # add feature label column
    features = {**LABEVENTS_FEATURES, **CHARTEVENTS_FEATURES}
    features_reversed = {v2: k for k, v1 in features.items() for v2 in v1}
    df['feature'] = df['itemid'].apply(lambda x: features_reversed[x])

    # save mapping of icu stay ID to patient ID
    icu_subject_mapping = dict(zip(df['stay_day'], df['subject_id']))

    # convert height (inches to cm)
    mask = (df['itemid'] == 226707) | (df['itemid'] == 1394)
    df.loc[mask, 'valuenum'] *= 2.54

    # average all feature values each day
    df = pd.pivot_table(
        df,
        index='stay_day',
        columns='feature',
        values='valuenum',
        fill_value=np.nan,
        aggfunc=np.nanmean,
        dropna=False,
    )

    # insert back information related to the patient (for persistence)
    df['stay_day'] = df.index
    df['stay_id'] = df['stay_day'].str.split(
        '_').apply(lambda x: x[0]).astype('int')
    df['subject_id'] = df['stay_day'].apply(lambda x: icu_subject_mapping[x])

    # save result
    df.to_csv(output_path, index=False)
    logger.info('`partition_rows` has ended')


def impute_holes(input_path, output_path):
    '''
    Fills in NaN values using forward/backward imputation.
    Entries that doesn't meet some imposed criteria will be dropped.

    Parameters:
    input_path: the path of the input csv to be processed (e.g., events_partitioned.csv)
    output_path: the path as to where the output of this step should be dumped
    '''
    logger.info('`impute_holes` has started')
    df = pd.read_csv(input_path)
    df.columns = map(str.lower, df.columns)

    # collect all feature keys
    features = {**LABEVENTS_FEATURES, **CHARTEVENTS_FEATURES}.keys()

    # fill NaN values with the average feature value (only for the current ICU stay)
    # ICU stays with NaN average values are dropped
    stay_ids = pd.unique(df['stay_id'])
    logger.info(f'Total ICU stays: {len(stay_ids)}')

    for stay_id in stay_ids:
        # get mask for the current icu stay
        stay_id_mask = df['stay_id'] == stay_id

        # there are ICU stays that even though its los >= 3
        # the actual measurements done in labevents or chartevents are fewer than that
        # so we drop them here
        if df[stay_id_mask].shape[0] < 3:
            logger.warning(f'ICU stay id={stay_id} has los<3 (dropped)')
            df = df[~stay_id_mask]
            continue

        # drop ICU stays with no creatinine levels
        # after the first 48 hours
        if not np.isfinite(df[stay_id_mask]['creatinine'].values[2:]).any():
            logger.warning(f'ICU stay id={stay_id} creatinine levels'
                           + ' are all NaN after 48 hours (dropped)')
            df = df[~stay_id_mask]
            continue

        # drop ICU stays with no creatinine levels
        # at the third day
        nan_index = get_nan_index(df[stay_id_mask]['creatinine'])
        if nan_index == 2:
            logger.warning(f'ICU stay id={stay_id} creatinine level'
                           + ' at 3rd day is not available (dropped)')
            df = df[~stay_id_mask]
            continue

        # drop ICU stay days (and onwards) with no creatinine levels defined
        if nan_index != -1:
            logger.warning(f'ICU stay id={stay_id} creatinine level'
                           + f' at {nan_index}th day is not available (dropped)')
            nan_indices = df[stay_id_mask].index[nan_index:]
            df = df.drop(nan_indices)

        # fill feature missing values with the mean value
        # of the ICU stay, dropping ICU stays with missing values
        df = fill_nas_or_drop(df, stay_id, features)

    # save result
    df.to_csv(output_path, index=False)
    logger.info('`impute_holes` has ended')


def fill_nas_or_drop(df, stay_id, features):
    '''
    A helper function to the impute_holes function. This does the actual
    forward/backward imputation which fills the NaN values. Also, entries
    without valid values for the whole ICU stay span will be dropped.

    Parameters:
    df: The input dataframe to be processed.
    stay_id: The ID of the ICU stay of a certain patient to be processed.
    features: The features used in this work (defined at the top as a constant).
    '''
    # get mask for the current icu stay
    stay_id_mask = df['stay_id'] == stay_id

    for feature in features:
        # drop ICU stays with features that doesn't contain any
        # finite values (e.g., all values are NaN)
        entity_features = df.loc[stay_id_mask, feature]
        if not np.isfinite(entity_features).any():
            logger.warning(f'ICU stay id={stay_id} feature={feature}'
                           + ' does not contain valid values (dropped)')
            return df[~stay_id_mask]

    # we impute feature values using forward/backward fills
    df.loc[stay_id_mask] = df[stay_id_mask].ffill().bfill()

    return df


def add_patient_info(input_path, output_path):
    '''
    Adds the patient information (static) to each of the entries.

    Parameters:
    input_path: the path of the input csv to be processed (e.g., events_imputed.csv)
    output_path: the path as to where the output of this step should be dumped
    '''
    logger.info('`add_patient_info` has started')

    admissions_path = MIMIC4_PATH / 'filtered_admissions.csv'
    admissions = pd.read_csv(admissions_path)
    admissions.columns = map(str.lower, admissions.columns)

    icustays_path = MIMIC4_PATH / 'filtered_icustays.csv'
    icustays = pd.read_csv(icustays_path)
    icustays.columns = map(str.lower, icustays.columns)

    patients_path = MIMIC4_PATH / 'filtered_patients.csv'
    patients = pd.read_csv(patients_path)
    patients.columns = map(str.lower, patients.columns)

    df = pd.read_csv(input_path)
    df.columns = map(str.lower, df.columns)

    stay_ids = pd.unique(df['stay_id'])
    logger.info(f'Total ICU stays: {len(stay_ids)}')

    # get auxiliary features
    hadm_id_mapping = dict(zip(icustays['stay_id'], icustays['hadm_id']))
    ethnicity_mapping = dict(
        zip(admissions['hadm_id'], admissions['ethnicity']))
    gender_mapping = dict(zip(patients['subject_id'], patients['gender']))
    age_mapping = dict(zip(patients['subject_id'], patients['anchor_age']))

    # retrieve admission ID from stay_day
    df['stay_id'] = df['stay_day'].str.split('_').apply(lambda x: x[0])
    df['stay_id'] = df['stay_id'].astype('int')
    df['hadm_id'] = df['stay_id'].apply(lambda x: hadm_id_mapping[x])

    # compute patient's age
    df['age'] = df['subject_id'].apply(lambda x: age_mapping[x])

    # add patient's gender
    df['gender'] = df['subject_id'].apply(lambda x: gender_mapping[x])
    df['gender'] = (df['gender'] == 'M').astype('int')

    # add patient's ethnicity (black or not)
    df['ethnicity'] = df['hadm_id'].apply(lambda x: ethnicity_mapping[x])
    df['black'] = df['ethnicity'].str.contains(
        r'.*black.*', flags=re.IGNORECASE).astype('int')

    # drop unneeded columns
    del df['ethnicity']

    # save result
    df.to_csv(output_path, index=False)
    logger.info('`add_patient_info` has ended')


def add_aki_labels(input_path, output_path):
    '''
    Adds the AKI label to each of the entries (using the values of 
    age, gender, race, and creatinine).

    Parameters:
    input_path: the path of the input csv to be processed (e.g., events_with_demographics.csv)
    output_path: the path as to where the output of this step should be dumped
    '''
    logger.info('`add_aki_labels` has started')

    df = pd.read_csv(input_path)
    df.columns = map(str.lower, df.columns)

    stay_ids = pd.unique(df['stay_id'])
    logger.info(f'Total ICU stays: {len(stay_ids)}')

    for stay_id in stay_ids:
        # get auxiliary variables
        stay_id_mask = df['stay_id'] == stay_id
        black = df[stay_id_mask]['black'].values[0]
        age = df[stay_id_mask]['age'].values[0]
        gender = df[stay_id_mask]['gender'].values[0]

        # get difference of creatinine levels
        scr = df[stay_id_mask]['creatinine'].values
        diffs = scr[1:] - scr[:-1]

        # drop patients with age < 20
        # since KDIGO criteria doesn't have an Scr baseline for them
        if age < 20:
            logger.warning(f'ICU stay id={stay_id} age < 20 (dropped)')
            df = df[~stay_id_mask]
            continue

        # drop ICU stays with AKIs for the first 48 hours
        if (
            has_aki(diff=diffs[0])
            or has_aki(scr=scr[0], black=black, age=age, gender=gender)
            or has_aki(scr=scr[1], black=black, age=age, gender=gender)
        ):
            logger.warning(
                f'ICU stay id={stay_id} has AKI pre-48 (dropped)')
            df = df[~stay_id_mask]
            continue

        # we do next-day AKI prediction
        # use the 3rd day's creatinine level to get the AKI label of day 2 data
        aki1 = pd.Series(diffs[1:]).apply(lambda x: has_aki(diff=x))
        aki2 = pd.Series(scr[2:]).apply(lambda x: has_aki(
            scr=x, black=black, age=age, gender=gender))
        aki = (aki1 | aki2).astype('int').values.tolist()

        # drop last day values
        last_day_index = df[stay_id_mask].index[-1]
        df = df.drop(last_day_index)

        # assign aki labels
        stay_id_mask = df['stay_id'] == stay_id
        aki_labels = [0] + aki
        df.loc[stay_id_mask, 'aki'] = aki_labels

        # truncate icu stays (retain first 8 days)
        to_truncate_indices = df[stay_id_mask].index[8:]
        if len(to_truncate_indices) > 0:
            logger.warning(
                f'ICU stay id={stay_id} will be truncated to 8 days.')
            df = df.drop(to_truncate_indices)

    # save results
    df.to_csv(output_path, index=False)
    logger.info('`add_aki_labels` has ended')


def has_aki(diff=None, scr=None, black=None, age=None, gender=None):
    '''
    Given scr or age,race,gender, determine if it has AKI or not.

    Parameters:
    diff: The difference of the creatinine values of a patient between two days
    scr: The creatinine value of a certain day
    black: Whether the patient is black or not (1=True, 0=false)
    age: The age of the patient
    gender: The gender of the patient (1=Male, 0=Female)
    '''
    # KDIGO criteria no. 1
    # Increase in SCr by >= 0.3 mg/dl (>= 26.5 lmol/l) within 48 hours
    if diff is not None:
        return diff >= 0.3

    # KDIGO criteria no. 2
    # increase in SCr to ≥1.5 times baseline, which is known or
    # presumed to have occurred within the prior 7 days
    if scr is not None:
        assert black is not None
        assert age is not None
        assert gender is not None

        baseline = get_baseline(black=black, age=age, gender=gender)
        return scr >= 1.5 * baseline

    # KDIGO criteria no. 3
    # Urine volume <0.5 ml/kg/h for 6 hours
    # not included since urine output data is scarce in MIMIC-III dataset

    raise AssertionError('ERROR - Should pass diff OR scr')


def get_baseline(*, black, age, gender):
    '''
    Returns the AKI baseline based on the given parameters.
    '''
    if 20 <= age <= 24:
        if black == 1:
            # black males: 1.5, black females: 1.2
            return 1.5 if gender == 1 else 1.2
        else:
            # other males: 1.3, other females: 1.0
            return 1.3 if gender == 1 else 1.0

    if 25 <= age <= 29:
        if black == 1:
            # black males: 1.5, black females: 1.2
            return 1.5 if gender == 1 else 1.1
        else:
            # other males: 1.3, other females: 1.0
            return 1.2 if gender == 1 else 1.0

    if 30 <= age <= 39:
        if black == 1:
            # black males: 1.5, black females: 1.2
            return 1.4 if gender == 1 else 1.1
        else:
            # other males: 1.3, other females: 1.0
            return 1.2 if gender == 1 else 0.9

    if 40 <= age <= 54:
        if black == 1:
            # black males: 1.5, black females: 1.2
            return 1.3 if gender == 1 else 1.0
        else:
            # other males: 1.3, other females: 1.0
            return 1.1 if gender == 1 else 0.9

    # for ages > 65
    if black == 1:
        # black males: 1.5, black females: 1.2
        return 1.2 if gender == 1 else 0.9
    else:
        # other males: 1.3, other females: 1.0
        return 1.0 if gender == 1 else 0.8


def get_nan_index(series):
    '''
    Returns the index of the first nan value within the series (-1 if there's
    no nan values in the series). This only considers the nan values from the 3rd element
    onwards since the first and second element are already checked and assumed to be non-nan.
    '''
    result = ~np.isfinite(series)
    for i, x in enumerate(result[2:]):
        if x:
            return i + 2

    return -1


def transform_outliers(input_path, output_path):
    '''
    Detects the presence of outliers and replaces their values 
    with the lower/upper bound.

    Parameters:
    input_path: the path of the input csv to be processed (e.g., events_with_labels.csv)
    output_path: the path as to where the output of this step should be dumped
    '''
    logger.info('`transform_outliers` has started')
    df = pd.read_csv(input_path)
    df.columns = map(str.lower, df.columns)

    features = {**LABEVENTS_FEATURES, **CHARTEVENTS_FEATURES}
    for feature in features.keys():
        # there are some bizarre values (e.g., negative person weights)
        # most likely due to typos, so we correct them here
        upper_bound = df[feature].quantile(.99)
        lower_bound = df[feature].quantile(.01)
        logger.info(f'Feature={feature} upper bound={upper_bound}')
        logger.info(f'Feature={feature} lower bound={lower_bound}')

        upper_mask = df[feature] > upper_bound
        lower_mask = df[feature] < lower_bound
        upper_ids = pd.unique(df.loc[upper_mask, 'stay_id'])
        lower_ids = pd.unique(df.loc[lower_mask, 'stay_id'])

        if len(upper_ids) > 0:
            # rescale values to the upper bound
            logger.info(f'Feature={feature}, {upper_ids} contains +outliers')
            df.loc[upper_mask, feature] = upper_bound

        if len(lower_ids) > 0:
            # rescale values to the lower bound
            logger.info(f'Feature={feature}, {lower_ids} contains -outliers')
            df.loc[lower_mask, feature] = lower_bound

    # save result
    df.to_csv(output_path, index=False)
    logger.info('`transform_outliers` has ended')


def extract_dataset(output_dir='dataset', redo=False):
    # create output dir if it does not exist
    # all of the artifacts of this script will be put inside this directory
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=False, exist_ok=True)

    # partition features into days
    # transform the huge events table into a feature table
    ipath = MIMIC4_PATH / 'filtered_events.csv'
    opath = output_dir / 'events_partitioned.csv'
    if redo or not opath.exists():
        partition_rows(ipath, opath)

    # not all feature values has a valid value (some of them have NaN)
    # we use a combination of forward and backward imputation to fill these holes
    ipath = opath
    opath = output_dir / 'events_imputed.csv'
    if redo or not opath.exists():
        impute_holes(ipath, opath)

    # in addition to the feature values (dynamic), add the patient's
    # demographic information (static)
    ipath = opath
    opath = output_dir / 'events_with_demographics.csv'
    if redo or not opath.exists():
        add_patient_info(ipath, opath)

    # based on the gathered feature values, determine if patient has
    # AKI or not (as defined by the KDIGO criteria)
    ipath = opath
    opath = output_dir / 'events_with_labels.csv'
    if redo or not opath.exists():
        add_aki_labels(ipath, opath)

    # MIMIC-IV contains typographical errors and this will come out as outliers
    # in this step, we remove these outliers
    ipath = opath
    opath = output_dir / 'events_complete.csv'
    if redo or not opath.exists():
        transform_outliers(ipath, opath)


if __name__ == '__main__':
    fire.Fire(extract_dataset)
