from __future__ import annotations

import os
from pathlib import Path


def _load_toml(path: Path) -> dict:
    """Load TOML on both Python 3.9/3.10 and Python 3.11+."""
    text = path.read_text(encoding="utf-8")

    try:
        import tomllib  # Python 3.11+
        return tomllib.loads(text)
    except ModuleNotFoundError:
        import toml  # Python 3.9/3.10 fallback
        return toml.loads(text)


def load_local_secrets() -> None:
    path = Path(".streamlit/secrets.toml")
    if not path.exists():
        print(f"Secrets file not found: {path.resolve()}")
        return

    try:
        data = _load_toml(path)
    except Exception as exc:
        print(f"Could not read {path}: {exc}")
        return

    supabase = data.get("supabase", {}) if isinstance(data, dict) else {}

    mappings = {
        "SUPABASE_URL": supabase.get("url", ""),
        "SUPABASE_SECRET_KEY": (
            supabase.get("secret_key", "")
            or supabase.get("service_role_key", "")
        ),
        "SUPABASE_BUCKET": supabase.get("bucket", "mqr-data"),
    }

    for key, value in mappings.items():
        if value and not os.environ.get(key):
            os.environ[key] = str(value)


def main() -> int:
    load_local_secrets()

    from mstar_mqr.cloud_storage import (
        cloud_status,
        ensure_bucket_exists,
        sync_local_roots_to_cloud,
    )

    status = cloud_status()

    print(
        "Supabase config:"
        f" URL={'yes' if status.get('url_configured') else 'no'},"
        f" key={'yes' if status.get('key_configured') else 'no'},"
        f" bucket={status.get('bucket')}"
    )

    if not status.get("enabled"):
        print(
            "Supabase is not configured. Check .streamlit/secrets.toml "
            "and make sure [supabase] contains url, secret_key, and bucket."
        )
        return 1

    ensure_bucket_exists()

    try:
        count = sync_local_roots_to_cloud(raise_errors=True)
    except Exception as exc:
        print(f"Supabase upload failed: {exc}")
        return 1

    print(
        f"Uploaded {count} existing input/output files "
        f"to Supabase bucket: {status.get('bucket')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())