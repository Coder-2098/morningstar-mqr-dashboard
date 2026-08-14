from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALLOWED_ROOTS = {"output", "inputs"}
DEFAULT_BUCKET = "mqr-data"


def _config() -> tuple[str, str, str]:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = (os.environ.get("SUPABASE_SECRET_KEY", "").strip() or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip())
    bucket = os.environ.get("SUPABASE_BUCKET", DEFAULT_BUCKET).strip() or DEFAULT_BUCKET
    return url, key, bucket


def cloud_enabled() -> bool:
    url, key, _ = _config()
    return bool(url and key)


def _client():
    if not cloud_enabled():
        return None
    try:
        from supabase import create_client  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "Supabase is configured but the supabase Python package is not installed."
        ) from exc
    url, key, _ = _config()
    return create_client(url, key)


def cloud_status() -> Dict[str, Any]:
    url, key, bucket = _config()
    return {
        "enabled": bool(url and key),
        "bucket": bucket,
        "url_configured": bool(url),
        "key_configured": bool(key),
    }


def ensure_bucket_exists() -> None:
    """Create the private bucket if it does not exist.

    This is best-effort because the user may already have created the bucket in
    the Supabase dashboard. A server-side Supabase secret key is expected for the hosted app.
    """
    client = _client()
    if client is None:
        return
    _, _, bucket = _config()
    try:
        buckets = client.storage.list_buckets()
        names = set()
        for item in buckets or []:
            if isinstance(item, dict):
                names.add(str(item.get("name") or item.get("id") or ""))
            else:
                names.add(str(getattr(item, "name", "") or getattr(item, "id", "")))
        if bucket not in names:
            client.storage.create_bucket(bucket, options={"public": False})
    except Exception:
        # The deployment instructions create the bucket explicitly. Failing to
        # inspect/create it here should not prevent the rest of the app loading.
        pass


def _relative_remote_path(local_path: Path) -> Optional[str]:
    try:
        relative = local_path.resolve().relative_to(PROJECT_ROOT.resolve())
    except Exception:
        return None
    if not relative.parts or relative.parts[0] not in ALLOWED_ROOTS:
        return None
    return relative.as_posix()


def sync_file_to_cloud(local_path: Path | str, *, raise_errors: bool = False) -> bool:
    """Upload one output/input file to the shared private Supabase bucket."""
    if not cloud_enabled():
        return False
    path = Path(local_path)
    if not path.exists() or not path.is_file():
        return False
    remote_path = _relative_remote_path(path)
    if not remote_path:
        return False

    try:
        client = _client()
        if client is None:
            return False
        _, _, bucket = _config()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        storage = client.storage.from_(bucket)
        with path.open("rb") as handle:
            try:
                storage.upload(
                    path=remote_path,
                    file=handle,
                    file_options={"content-type": mime, "upsert": "true"},
                )
            except Exception:
                handle.seek(0)
                storage.update(
                    path=remote_path,
                    file=handle,
                    file_options={"content-type": mime},
                )
        return True
    except Exception:
        if raise_errors:
            raise
        return False


def sync_tree_to_cloud(local_root: Path | str, *, raise_errors: bool = False) -> int:
    root = Path(local_root)
    if not root.exists():
        return 0
    count = 0
    for path in root.rglob("*"):
        if path.is_file() and sync_file_to_cloud(path, raise_errors=raise_errors):
            count += 1
    return count


def sync_local_roots_to_cloud(*, raise_errors: bool = False) -> int:
    count = 0
    for name in sorted(ALLOWED_ROOTS):
        count += sync_tree_to_cloud(PROJECT_ROOT / name, raise_errors=raise_errors)
    return count


def _entry_value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _list_remote_files(prefix: str) -> List[str]:
    """Recursively list file objects under one Supabase Storage prefix."""
    client = _client()
    if client is None:
        return []
    _, _, bucket = _config()
    storage = client.storage.from_(bucket)
    found: List[str] = []

    def walk(folder: str) -> None:
        offset = 0
        while True:
            items = storage.list(
                folder,
                {
                    "limit": 1000,
                    "offset": offset,
                    "sortBy": {"column": "name", "order": "asc"},
                },
            ) or []
            if not items:
                break
            for item in items:
                name = str(_entry_value(item, "name", "") or "")
                if not name:
                    continue
                child = f"{folder}/{name}" if folder else name
                item_id = _entry_value(item, "id")
                metadata = _entry_value(item, "metadata")
                # Supabase returns virtual folders with null id/metadata.
                if item_id in (None, "") and metadata in (None, {}):
                    walk(child)
                else:
                    found.append(child)
            if len(items) < 1000:
                break
            offset += len(items)

    walk(prefix.strip("/"))
    return found


def restore_prefix_from_cloud(prefix: str, *, raise_errors: bool = False) -> int:
    """Restore all files under a cloud prefix into the app's local working disk."""
    if not cloud_enabled():
        return 0
    try:
        client = _client()
        if client is None:
            return 0
        _, _, bucket = _config()
        storage = client.storage.from_(bucket)
        count = 0
        for remote_path in _list_remote_files(prefix):
            if not remote_path.split("/", 1)[0] in ALLOWED_ROOTS:
                continue
            payload = storage.download(remote_path)
            local_path = PROJECT_ROOT / Path(remote_path)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(payload)
            count += 1
        return count
    except Exception:
        if raise_errors:
            raise
        return 0


def restore_all_from_cloud(*, raise_errors: bool = False) -> int:
    count = 0
    for prefix in sorted(ALLOWED_ROOTS):
        count += restore_prefix_from_cloud(prefix, raise_errors=raise_errors)
    return count
