from __future__ import annotations

import json
import os
import re
import time
from getpass import getpass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd


class DailyCellLimitExceeded(RuntimeError):
    """Raised when Morningstar says the daily 500,000-cell limit is exceeded."""


class MorningstarAuthError(RuntimeError):
    """Raised when the Analytics Lab token is missing, malformed, or expired."""


def log(message: str) -> None:
    """Print a timestamped message so long runs are easy to follow."""
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def slug(value: str) -> str:
    """Make a safe folder/file name from country names and datapoint IDs."""
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value).strip()).strip("_").lower()


def make_output_dir(domicile: str) -> Path:
    """All output for one country goes under output/<country>."""
    out = Path("output") / slug(domicile)
    out.mkdir(parents=True, exist_ok=True)
    return out


def save_df(df: pd.DataFrame, path: Path) -> None:
    """Save a DataFrame and log the row/column count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    try:
        from .cloud_storage import sync_file_to_cloud

        sync_file_to_cloud(path)
    except Exception:
        pass
    log(f"Saved {path} | rows={len(df)} cols={len(df.columns)}")


def read_df_if_exists(path: Path) -> Optional[pd.DataFrame]:
    """Read a CSV if it exists; otherwise return None."""
    if path.exists():
        return pd.read_csv(path)
    return None


def save_json(obj: Dict[str, Any], path: Path) -> None:
    """Save JSON state for audit/resume."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)
    try:
        from .cloud_storage import sync_file_to_cloud

        sync_file_to_cloud(path)
    except Exception:
        pass
    log(f"Saved {path}")


def read_json_if_exists(path: Path) -> Dict[str, Any]:
    """Read JSON state if it exists."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def update_state(output_dir: Path, **updates: Any) -> None:
    """Update output/<country>/run_state.json without losing older fields."""
    path = output_dir / "run_state.json"
    state = read_json_if_exists(path)
    state.update(updates)
    state["updated_at_local"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json(state, path)


def is_jwt_shape(token: str) -> bool:
    """Analytics Lab tokens should look like header.payload.signature."""
    from .token_manager import is_jwt_shape as _is_jwt_shape

    return _is_jwt_shape(token)


def ensure_token(prompt_for_token: bool = True, force_refresh: bool = False, use_browser: bool = True) -> None:
    """Validate or refresh MD_AUTH_TOKEN using Analytics Lab token manager."""
    from .token_manager import ensure_md_auth_token

    try:
        ensure_md_auth_token(
            allow_prompt=prompt_for_token,
            prefer_clipboard=True,
            force_refresh=force_refresh,
            use_browser=use_browser,
        )
    except RuntimeError as exc:
        raise MorningstarAuthError(str(exc)) from exc

def is_daily_limit_error(exc: BaseException) -> bool:
    """Detect Morningstar's daily-cell-limit error from exception text."""
    text = str(exc).lower()
    markers = [
        "daily query limit",
        "500000",
        "cells today",
        "exceeding your daily",
        "daily limit",
    ]
    return any(marker in text for marker in markers)


def is_auth_error(exc: BaseException) -> bool:
    """Detect token/auth problems early so we don't misreport them as no data."""
    text = str(exc).lower()
    markers = [
        "malformedjwt",
        "invalid jwt",
        "accessdenied",
        "forbidden",
        "unauthorized",
        "authentication",
        "auth token",
        "token",
    ]
    return any(marker in text for marker in markers)


def classify_and_raise(exc: BaseException) -> None:
    """Convert common Morningstar failures into clear pipeline exceptions."""
    if is_daily_limit_error(exc):
        try:
            from .token_manager import mark_daily_limit_exceeded

            mark_daily_limit_exceeded()
        except Exception:
            pass
        raise DailyCellLimitExceeded(str(exc)) from exc
    if is_auth_error(exc):
        raise MorningstarAuthError(str(exc)) from exc
    raise exc


def chunk_list(values: List[str], batch_size: int) -> Iterable[List[str]]:
    """Yield batches from a list."""
    for start in range(0, len(values), batch_size):
        yield values[start : start + batch_size]


def find_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    """Find a column by exact or case-insensitive match."""
    lower = {c.lower(): c for c in df.columns}
    for candidate in candidates:
        if candidate in df.columns:
            return candidate
        if candidate.lower() in lower:
            return lower[candidate.lower()]
    return None


def find_id_col(df: pd.DataFrame) -> str:
    """Find the investment/SecId column returned by Morningstar."""
    col = find_col(
        df,
        [
            "Id",
            "ID",
            "id",
            "SecId",
            "secid",
            "SECID",
            "Security Id",
            "SecurityID",
            "Investment Id",
            "investment_id",
        ],
    )
    if col:
        return col
    raise RuntimeError(f"Could not find investment ID column. Columns: {list(df.columns)}")


def normalize_blanks(series: pd.Series) -> pd.Series:
    """Turn Morningstar blank-ish values into pandas NA."""
    cleaned = series.astype(str).str.strip()
    return cleaned.replace(
        {
            "": pd.NA,
            "nan": pd.NA,
            "NaN": pd.NA,
            "None": pd.NA,
            "none": pd.NA,
            "NULL": pd.NA,
            "null": pd.NA,
            "--": pd.NA,
            "-": pd.NA,
            "N/A": pd.NA,
            "n/a": pd.NA,
            "-N/A": pd.NA,
            "-n/a": pd.NA,
        }
    )


def unique_strings(values: Iterable[Any]) -> List[str]:
    """Convert values to clean strings and dedupe while preserving order."""
    seen = set()
    out: List[str] = []
    for value in values:
        if pd.isna(value):
            continue
        cleaned = str(value).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out
