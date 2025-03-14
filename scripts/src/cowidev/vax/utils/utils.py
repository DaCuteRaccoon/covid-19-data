import os
from glob import glob

import pandas as pd

from cowidev.utils.utils import make_monotonic as _make_monotonic
from cowidev.vax.incremental.africacdc import AfricaCDC
from cowidev.vax.utils.orgs import ACDC_COUNTRIES_ALL
from cowidev.vax.utils.checks import METRICS


def get_latest_file(path, extension):
    files = glob(os.path.join(path, f"*.{extension}"))
    return max(files, key=os.path.getctime)


def make_monotonic(df: pd.DataFrame, max_removed_rows=10, new_version=False) -> pd.DataFrame:
    # Forces vaccination time series to become monotonic.
    # The algorithm assumes that the most recent values are the correct ones,
    # and therefore removes previous higher values.
    if new_version:
        return _make_monotonic(
            df=df,
            column_date="date",
            column_metrics=["total_vaccinations", "people_vaccinated", "people_fully_vaccinated"],
            max_removed_rows=max_removed_rows,
            strict=False,
        )

    n_rows_before = len(df)
    dates_before = set(df.date)
    df_before = df.copy()

    df = df.sort_values("date")
    metrics = ("total_vaccinations", "people_vaccinated", "people_fully_vaccinated")
    for metric in metrics:
        while not df[metric].ffill().fillna(0).is_monotonic:
            diff = df[metric].ffill().shift(-1) - df[metric].ffill()
            df = df[(diff >= 0) | (diff.isna())]
    dates_now = set(df.date)

    if max_removed_rows is not None:
        num_removed_rows = n_rows_before - len(df)
        if num_removed_rows > max_removed_rows:
            dates_wrong = dates_before.difference(dates_now)
            df_wrong = df_before[df_before.date.isin(dates_wrong)]
            raise Exception(
                f"{num_removed_rows} rows have been removed. That is more than maximum allowed ({max_removed_rows}) by"
                f" make_monotonic() - check the data. Check \n{df_wrong}"  # {', '.join(sorted(dates_wrong))}"
            )

    return df


def build_vaccine_timeline(df: pd.DataFrame, vaccine_timeline: dict) -> pd.DataFrame:
    """
    vaccine_timeline: dictionary of "vaccine" -> "start_date"
    Example: {
        "Pfizer/BioNTech": "2021-02-24",
        "Sinovac": "2021-03-03",
        "Oxford/AstraZeneca": "2021-05-03",
        "CanSino": "2021-05-09",
        "Sinopharm": "2021-09-18",
    }
    """

    def _build_vaccine_row(date, vaccine_timeline: dict):
        vaccines = [k for k, v in vaccine_timeline.items() if v <= date]
        return ", ".join(sorted(list(set(vaccines))))

    df["vaccine"] = df.date.apply(_build_vaccine_row, vaccine_timeline=vaccine_timeline)
    return df


def add_latest_who_values(df: pd.DataFrame, who_location_name: str, metrics: list):
    """
    Inserts the latest data available from the WHO vaccination dataset into the existing dataframe.
    metrics: list of metrics to be used from the WHO dataset. Other metrics that aren't listed
    will be automatically set to pd.NA for this specific row.
    """
    assert isinstance(metrics, list), "The `metrics` argument in add_latest_who_values should be a list!"

    df["date"] = df.date.astype(str)
    df = df.sort_values("date")

    who = pd.read_csv(
        "https://covid19.who.int/who-data/vaccination-data.csv",
        usecols=[
            "COUNTRY",
            "DATA_SOURCE",
            "DATE_UPDATED",
            "TOTAL_VACCINATIONS",
            "PERSONS_VACCINATED_1PLUS_DOSE",
            "PERSONS_FULLY_VACCINATED",
        ],
    )

    who = who[(who.COUNTRY == who_location_name) & (who.DATA_SOURCE == "REPORTING")]
    if len(who) == 0:
        raise Exception(f"No row of type REPORTING was found in the WHO dataset for location '{who_location_name}'")

    last_who_report_date = who.DATE_UPDATED.values[0]

    who_row = df[df.date >= last_who_report_date].head(1).copy()
    original_rows = df[df.date != last_who_report_date].copy()

    who_row["date"] = last_who_report_date
    who_row["total_vaccinations"] = who.TOTAL_VACCINATIONS.values[0] if "total_vaccinations" in metrics else pd.NA
    who_row["people_vaccinated"] = (
        who.PERSONS_VACCINATED_1PLUS_DOSE.values[0] if "people_vaccinated" in metrics else pd.NA
    )
    who_row["people_fully_vaccinated"] = (
        who.PERSONS_FULLY_VACCINATED.values[0] if "people_fully_vaccinated" in metrics else pd.NA
    )
    if "total_boosters" in who_row.columns:
        who_row["total_boosters"] = pd.NA
    who_row["source_url"] = "https://covid19.who.int/"

    df = pd.concat([original_rows, who_row], ignore_index=True).sort_values("date")

    return df


def add_latest_from_acdc(df: pd.DataFrame, metrics):
    # Get mapping countries
    locations = set(df.location)
    countries = {org: owid for org, owid in ACDC_COUNTRIES_ALL.items() if org in locations}
    # Get ACDC data
    api = AfricaCDC(True)
    dfa = api.read()
    dfa = dfa.pipe(api.pipeline, countries, exclude=False)
    # Set ignored metrics to NA
    metrics_ignore = {m: pd.NA for m in METRICS if m not in metrics}
    dfa = dfa.assign(**metrics_ignore)
    # Concatenate
    df = pd.concat([df, dfa], ignore_index=True).sort_values(["date", "location"])
    # Propagate vaccines
    x = (
        df.dropna(subset=["vaccine"])
        .sort_values("date")
        .drop_duplicates()
        .rename(columns={"vaccine": "vaccine_latest"})
    )
    df = df.merge(x[["location", "vaccine_latest"]], on="location", how="outer")
    df = df.assign(vaccine=df.vaccine.fillna(df.vaccine_latest)).drop(columns=["vaccine_latest"])
    return df
