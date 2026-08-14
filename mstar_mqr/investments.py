from __future__ import annotations

from pathlib import Path
from typing import Optional
import re

import pandas as pd
import morningstar_data as md

from .common import log, save_df, find_id_col, classify_and_raise


# Keep the investment-universe step cheap.
# We only need enough fields to identify the funds/ETFs.
BASE_DATA_POINTS = {
    # LS017 is enough to identify and audit a domicile search.
    # We intentionally do not request OS01W/Name here because Morningstar can
    # return many historical columns such as "Name 2024-01-31", which wastes
    # quota and makes the FO/FE universe files unnecessarily wide.
    "domicile": "LS017",
}


# ISO-3 first only for known countries.
# This avoids wasting quota on weak variants like "Korea", "Republic of Korea", etc.
COUNTRY_ALIASES = {
    "korea": ["KOR"],
    "south korea": ["KOR"],
    "republic of korea": ["KOR"],

    "france": ["FRA"],
    "usa": ["USA"],
    "united states": ["USA"],
    "uk": ["GBR"],
    "united kingdom": ["GBR"],
    "japan": ["JPN"],
    "aus": ["AUS"],
    "australia": ["AUS"],
    "sweden": ["SWE"],
    "netherlands": ["NLD"],
    "germany": ["DEU"],
    "luxembourg": ["LUX"],
    "ireland": ["IRL"],
    "canada": ["CAN"],
    "india": ["IND"],
}


def country_variants(domicile: str) -> list[str]:
    """
    Return the Morningstar domicile values to try.

    For known countries, use only ISO-3 to avoid burning quota.
    For unknown countries, fall back to the uppercased input.
    """
    key = domicile.strip().lower()

    variants = COUNTRY_ALIASES.get(
        key,
        [domicile.strip().upper()],
    )

    final: list[str] = []
    seen: set[str] = set()

    for value in variants:
        value = str(value).strip()

        if not value:
            continue

        if value in seen:
            continue

        seen.add(value)
        final.append(value)

    return final


def base_datapoints() -> list[dict]:
    """
    Minimal audit datapoints for identifying the investment universe.

    Do not add rating datapoints here. Ratings are pulled later in the
    optimized month-scan step.
    """
    return [
        {"datapointId": BASE_DATA_POINTS["domicile"]},
    ]


def build_search_criteria(
    universe: str,
    domicile_value: str,
    operator: str,
    security_status: str,
) -> dict:
    """
    Build the Morningstar custom search criteria object.

    universe:
        FO = open-end funds / mutual funds
        FE = ETFs, depending on entitlement/environment
    """
    return {
        "universeId": universe,
        "subUniverseId": "",
        "subUniverseName": "",
        "securityStatus": security_status,
        "useDefinedPrimary": False,
        "criteria": [
            {
                "relation": "",
                "field": BASE_DATA_POINTS["domicile"],
                "operator": operator,
                "value": domicile_value,
            }
        ],
    }



DATED_NAME_COLUMN = re.compile(r"^Name\s+\d{4}-\d{2}-\d{2}$", re.IGNORECASE)


def clean_investment_output(df: pd.DataFrame) -> pd.DataFrame:
    """
    Keep country-universe files compact and readable.

    Older runs may contain dozens of columns such as ``Name 2023-07-31``.
    Those are historical snapshots of the investment name, not separate funds
    or rating dates. They are not needed for the MQR launch-date analysis.

    If they exist, this function keeps one latest nonblank name in ``Name`` and
    removes the dated Name columns. New runs no longer request the Name datapoint
    at all, so they normally contain only ID, domicile, and query audit fields.
    """
    if df is None or df.empty:
        return df

    cleaned = df.copy()
    cleaned = cleaned.loc[:, ~cleaned.columns.astype(str).str.startswith("Unnamed:")]
    cleaned = cleaned.loc[:, ~cleaned.columns.duplicated()]

    dated_name_columns = [
        column for column in cleaned.columns if DATED_NAME_COLUMN.match(str(column).strip())
    ]
    plain_name_columns = [
        column for column in cleaned.columns if str(column).strip().lower() == "name"
    ]

    if dated_name_columns:
        def sort_key(column: object) -> str:
            match = re.search(r"(\d{4}-\d{2}-\d{2})$", str(column))
            return match.group(1) if match else ""

        candidates = sorted(dated_name_columns, key=sort_key, reverse=True)
        candidates.extend([column for column in plain_name_columns if column not in candidates])
        values = cleaned[candidates].copy()
        values = values.replace(r"^\s*$", pd.NA, regex=True)
        values = values.replace(["nan", "NaN", "None", "none"], pd.NA)
        cleaned["Name"] = values.bfill(axis=1).iloc[:, 0]
        cleaned = cleaned.drop(columns=dated_name_columns, errors="ignore")
    elif len(plain_name_columns) > 1:
        values = cleaned[plain_name_columns].replace(r"^\s*$", pd.NA, regex=True)
        cleaned["Name"] = values.bfill(axis=1).iloc[:, 0]
        cleaned = cleaned.drop(columns=[c for c in plain_name_columns if c != "Name"], errors="ignore")

    preferred = []
    for candidate in [
        "Id", "ID", "id", "SecId", "Investment ID",
        "Name", "Domicile",
        "query_universe", "query_status", "query_operator", "query_value",
    ]:
        if candidate in cleaned.columns and candidate not in preferred:
            preferred.append(candidate)
    remaining = [column for column in cleaned.columns if column not in preferred]
    return cleaned[preferred + remaining].copy()


def try_search(criteria: dict) -> Optional[pd.DataFrame]:
    """
    Run one Morningstar investment search.

    ResourceNotFoundError means this exact domicile query had no results.
    That is not fatal.

    Daily limit, token, malformed JWT, and auth errors should still stop.
    """
    try:
        result = md.direct.get_investment_data(
            investments=criteria,
            data_points=base_datapoints(),
            display_name=True,
        )
        return clean_investment_output(result)

    except Exception as exc:
        error_text = str(exc)
        error_name = type(exc).__name__

        combined_error = (error_name + " " + error_text).lower()

        # Daily-limit and token/auth errors should always stop. Do this before
        # treating optional universe failures as nonfatal.
        if any(marker in combined_error for marker in [
            "daily query limit", "cells today", "exceeding your daily", "malformedjwt",
            "invalid jwt", "accessdenied", "forbidden", "unauthorized", "authentication",
            "auth token", "token expired",
        ]):
            classify_and_raise(exc)

        nonfatal_markers = [
            "resourcenotfounderror",
            "resource not found",
            "invalid universe",
            "universe not found",
            "does not exist",
            "not entitled",
            "not available",
        ]
        if any(marker in combined_error for marker in nonfatal_markers):
            # Some optional fund universes such as VA/VL/LP may not exist or may not be
            # entitled in every Morningstar environment. Skip them instead of killing the run.
            log(f"Search returned no usable investments for this criteria. Continuing. ({error_name})")
            return None

        classify_and_raise(exc)
        return None


def load_existing_csv(path: Path) -> Optional[pd.DataFrame]:
    """
    Load a CSV if it exists and is not empty.
    """
    if not path.exists():
        return None

    try:
        df = pd.read_csv(path)
    except Exception:
        return None

    if df.empty:
        return None

    cleaned = clean_investment_output(df)
    if list(cleaned.columns) != list(df.columns) or cleaned.shape != df.shape:
        save_df(cleaned, path)
        log(f"Cleaned legacy dated Name columns in: {path}")
    log(f"Reusing existing file: {path}")
    return cleaned


def save_attempts(attempts: list[dict], attempts_file: Path) -> None:
    """
    Save the search-attempt audit log after every completed search.
    """
    if attempts:
        save_df(pd.DataFrame(attempts), attempts_file)


def combine_and_save(frames: list[pd.DataFrame], output_file: Path) -> pd.DataFrame:
    """
    Combine FO/FE partial files and deduplicate by Morningstar investment ID.
    """
    combined = clean_investment_output(pd.concat(frames, ignore_index=True))

    id_col = find_id_col(combined)
    combined = combined.drop_duplicates(subset=[id_col]).reset_index(drop=True)

    save_df(combined, output_file)
    return combined


def pull_investments_by_domicile(
    domicile: str,
    output_dir: Path,
    universes: list[str],
    reuse_existing: bool = True,
) -> pd.DataFrame:
    """
    Pull investments for one domicile and checkpoint aggressively.

    Resume behavior:
    - If output/<country>/02_investments.csv exists, reuse it.
    - If output/<country>/02_investments_FO.csv exists, skip FO search.
    - If output/<country>/02_investments_FE.csv exists, skip FE search.
    - As soon as a universe succeeds, save it immediately.

    This prevents the earlier problem where KOR succeeded or consumed quota,
    but nothing was saved before the daily limit was hit.
    """
    final_file = output_dir / "02_investments.csv"
    attempts_file = output_dir / "01_search_attempts.csv"

    if reuse_existing:
        existing_final = load_existing_csv(final_file)
        if existing_final is not None:
            return existing_final

    log(f"Pulling investments for domicile: {domicile}")

    variants = country_variants(domicile)

    # Keep the search plan tight to save cells.
    # Exact ISO-3 with active+inactive should usually be the correct path.
    search_plan = [
        ("activeinactive", "=", variants),
        ("activeonly", "=", variants),
    ]

    all_frames: list[pd.DataFrame] = []
    attempts: list[dict] = []

    # Keep old attempts for audit continuity.
    if attempts_file.exists():
        try:
            attempts.extend(pd.read_csv(attempts_file).to_dict("records"))
        except Exception:
            pass

    for universe in universes:
        partial_file = output_dir / f"02_investments_{universe}.csv"

        if reuse_existing:
            existing_partial = load_existing_csv(partial_file)
            if existing_partial is not None:
                all_frames.append(existing_partial)
                continue

        universe_found = False

        for status, operator, values in search_plan:
            if universe_found:
                break

            for value in values:
                log(
                    f"Trying universe={universe}, "
                    f"status={status}, operator={operator}, value={value}"
                )

                criteria = build_search_criteria(
                    universe=universe,
                    domicile_value=value,
                    operator=operator,
                    security_status=status,
                )

                df = try_search(criteria)
                rows = 0 if df is None else len(df)

                attempts.append(
                    {
                        "universe": universe,
                        "status": status,
                        "operator": operator,
                        "value": value,
                        "rows": rows,
                    }
                )

                save_attempts(attempts, attempts_file)

                if df is not None and not df.empty:
                    log(f"SUCCESS: universe={universe}, value={value}, rows={rows}")

                    df["query_universe"] = universe
                    df["query_status"] = status
                    df["query_operator"] = operator
                    df["query_value"] = value

                    # Critical checkpoint: save immediately.
                    save_df(df, partial_file)

                    all_frames.append(df)
                    universe_found = True
                    break

    if not all_frames:
        raise RuntimeError(
            f"No investments found for domicile={domicile}. "
            "Check 01_search_attempts.csv. If all attempts failed, "
            "add the country ISO-3 code to COUNTRY_ALIASES."
        )

    combined = combine_and_save(all_frames, final_file)

    log(f"Investment pull complete. Unique investments: {len(combined)}")
    return combined