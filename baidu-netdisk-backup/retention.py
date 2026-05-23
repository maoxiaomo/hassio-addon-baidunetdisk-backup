#!/usr/bin/env python3
"""Backup retention policy — keep / prune logic for remote backup files."""
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from client import BaiduClient  # noqa: F401  (used only as type hint)
else:
    BaiduClient = object

from client import log

# ============================================================================
# Regex for Home Assistant backup naming convention
# ============================================================================
_BACKUP_TS_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})_(\d{2})\.(\d{2})_")


# ============================================================================
# Helper functions
# ============================================================================
def _infer_backup_datetime(item: Dict[str, Any]) -> Optional[datetime]:
    """Extract a best-effort datetime from a remote backup item."""
    name = (item.get("server_filename") or "").strip()
    m = _BACKUP_TS_RE.search(name)
    if m:
        try:
            date_part = m.group(1)
            hour = int(m.group(2))
            minute = int(m.group(3))
            return datetime.strptime(date_part, "%Y-%m-%d").replace(
                hour=hour, minute=minute
            )
        except (ValueError, TypeError):  # Issue 2: specific exception types
            pass

    mtime = item.get("server_mtime") or item.get("server_ctime")
    if mtime:
        try:
            return datetime.fromtimestamp(int(mtime))
        except (ValueError, TypeError, OSError):
            return None
    return None


def _infer_backup_timestamp(item: Dict[str, Any]) -> Optional[int]:
    """Return Unix timestamp for *item* or None."""
    dt = _infer_backup_datetime(item)
    if not dt:
        return None
    return int(dt.timestamp())


def _join_remote_dir(base_dir: str, name: str) -> str:
    """Join two path segments for a remote directory."""
    if base_dir.endswith("/"):
        base_dir = base_dir[:-1]
    if not name:
        return base_dir
    if name.startswith("/"):
        name = name[1:]
    return f"{base_dir}/{name}"


def _paths_to_delete(
    items: List[Dict[str, Any]], keep_set: Set[str]
) -> List[str]:
    """Return paths in *items* that are NOT in *keep_set*."""
    out: List[str] = []
    for item in items:
        if item.get("isdir") == 1:
            continue
        name = item.get("server_filename") or ""
        if not name.endswith(".tar"):
            continue
        path = item.get("path")
        if path and path not in keep_set:
            out.append(path)
    return out


# ============================================================================
# Time-bucket selection  (Issue 5: unified via existing_keep parameter)
# ============================================================================
def _select_bucket_keep_paths(
    remote_items: List[Dict[str, Any]],
    bucket_key_fn: Callable[[datetime], Any],
    keep_count: int,
    existing_keep: Optional[Set[str]] = None,
) -> Set[str]:
    """Select newest *keep_count* files per time bucket.

    When *existing_keep* is provided, paths already kept by a higher-priority
    bucket are counted toward the limit but not re-added.
    """
    files: List[tuple] = []
    for item in remote_items:
        if item.get("isdir") == 1:
            continue
        name = item.get("server_filename") or ""
        if not name.endswith(".tar"):
            continue
        path = item.get("path")
        dt = _infer_backup_datetime(item)
        if not path or not dt:
            continue
        files.append((path, dt))

    files.sort(key=lambda x: x[1], reverse=True)  # newest first

    keep: Set[str] = set()
    seen: Set[Any] = set()
    for path, dt in files:
        key = bucket_key_fn(dt)
        if key in seen:
            continue
        if existing_keep is not None and path in existing_keep:
            seen.add(key)
            if keep_count and keep_count > 0 and len(seen) >= keep_count:
                break
            continue
        keep.add(path)
        seen.add(key)
        if keep_count and keep_count > 0 and len(seen) >= keep_count:
            break
    return keep


# ============================================================================
# Flat retention (single directory)
# ============================================================================
def _compute_retention_keep_paths(
    remote_items: List[Dict[str, Any]],
    daily: int,
    weekly: int,
    monthly: int,
) -> Set[str]:
    """Compute set of remote paths to retain under daily/weekly/monthly policy.

    Issue 5 fix: delegates all three tiers to ``_select_bucket_keep_paths``
    with cross-tier dedup via ``existing_keep``.
    """
    keep: Set[str] = set()

    if daily and daily > 0:
        keep |= _select_bucket_keep_paths(
            remote_items, lambda dt: dt.date(), daily
        )

    if weekly and weekly > 0:
        keep |= _select_bucket_keep_paths(
            remote_items,
            lambda dt: (dt.isocalendar().year, dt.isocalendar().week),
            weekly,
            existing_keep=keep,
        )

    if monthly and monthly > 0:
        keep |= _select_bucket_keep_paths(
            remote_items,
            lambda dt: (dt.year, dt.month),
            monthly,
            existing_keep=keep,
        )

    return keep


def cleanup_remote_backups(
    client: "BaiduClient",  # type: ignore[valid-type]
    upload_path: str,
    retention: Dict[str, Any],
) -> None:
    """Apply flat retention policy — keep newest N per day/week/month."""
    if not retention:
        return

    daily = int(retention.get("daily", 0) or 0)
    weekly = int(retention.get("weekly", 0) or 0)
    monthly = int(retention.get("monthly", 0) or 0)

    if daily <= 0 and weekly <= 0 and monthly <= 0:
        return

    remote_items = client.list_remote_files(upload_path) or []
    keep = _compute_retention_keep_paths(
        remote_items, daily=daily, weekly=weekly, monthly=monthly
    )

    candidates: List[str] = []
    for item in remote_items:
        if item.get("isdir") == 1:
            continue
        name = item.get("server_filename") or ""
        if not name.endswith(".tar"):
            continue
        path = item.get("path")
        if path and path not in keep:
            candidates.append(path)

    if not candidates:
        log("Remote retention: nothing to delete")
        return

    log(
        f"Remote retention: keeping {len(keep)} backups, "
        f"deleting {len(candidates)} backups"
    )
    client.delete_remote_files(candidates)


# ============================================================================
# Folder-mode retention (daily / weekly / monthly sub-directories)
# ============================================================================
def retention_folder_mode(
    client: "BaiduClient",  # type: ignore[valid-type]
    base_upload_path: str,
    retention: Dict[str, Any],
) -> None:
    """Apply folder-mode retention with promotion between tiers.

    Issue 4 fix: initial listing results are cached in dicts keyed by path.
    After move operations the caches are updated manually, eliminating 4 of
    the original 7 ``list_remote_files`` calls.
    """
    daily_n = int(retention.get("daily", 0) or 0)
    weekly_n = int(retention.get("weekly", 0) or 0)
    monthly_n = int(retention.get("monthly", 0) or 0)

    daily_dir = _join_remote_dir(base_upload_path, "每日")
    weekly_dir = _join_remote_dir(base_upload_path, "每周")
    monthly_dir = _join_remote_dir(base_upload_path, "每月")

    client.create_remote_dir(base_upload_path)
    client.create_remote_dir(daily_dir)
    client.create_remote_dir(weekly_dir)
    client.create_remote_dir(monthly_dir)

    # ── Phase 1: list once per directory (3 API calls) ──────────────────
    daily_items = client.list_remote_files(daily_dir) or []
    weekly_items = client.list_remote_files(weekly_dir) or []
    monthly_items = client.list_remote_files(monthly_dir) or []

    # Build path → item caches for manual update after moves
    daily_cache: Dict[str, Dict[str, Any]] = {}
    for item in daily_items:
        p = item.get("path")
        if p:
            daily_cache[p] = item

    weekly_cache: Dict[str, Dict[str, Any]] = {}
    for item in weekly_items:
        p = item.get("path")
        if p:
            weekly_cache[p] = item

    monthly_cache: Dict[str, Dict[str, Any]] = {}
    for item in monthly_items:
        p = item.get("path")
        if p:
            monthly_cache[p] = item

    # ── Phase 2: promote daily → monthly ────────────────────────────────
    monthly_keep_from_daily = _select_bucket_keep_paths(
        daily_items,
        bucket_key_fn=lambda dt: (dt.year, dt.month),
        keep_count=monthly_n,
    )

    existing_month_keys: Set[tuple] = set()
    for item in monthly_items:
        if item.get("isdir") == 1:
            continue
        dt = _infer_backup_datetime(item)
        if not dt:
            continue
        existing_month_keys.add((dt.year, dt.month))

    moves_to_monthly: List[Dict[str, str]] = []
    for path in monthly_keep_from_daily:
        item = daily_cache.get(path, {})
        dt = _infer_backup_datetime(item)
        if not dt:
            continue
        key = (dt.year, dt.month)
        if key in existing_month_keys:
            continue
        moves_to_monthly.append(
            {"path": path, "dest": monthly_dir, "ondup": "overwrite"}
        )

    if moves_to_monthly:
        log(
            f"Retention folders: promoting {len(moves_to_monthly)} "
            f"backups to monthly"
        )
        client.move_remote_files(moves_to_monthly)
        # Update caches instead of re-listing
        for m in moves_to_monthly:
            item = daily_cache.pop(m["path"], None)
            if item is not None:
                monthly_cache[m["path"]] = item

    # ── Phase 3: promote daily → weekly (from remaining daily cache) ────
    weekly_keep_from_daily = _select_bucket_keep_paths(
        list(daily_cache.values()),
        bucket_key_fn=lambda dt: (dt.isocalendar().year, dt.isocalendar().week),
        keep_count=weekly_n,
    )

    existing_week_keys: Set[tuple] = set()
    for item in weekly_cache.values():
        if item.get("isdir") == 1:
            continue
        dt = _infer_backup_datetime(item)
        if not dt:
            continue
        existing_week_keys.add((dt.isocalendar().year, dt.isocalendar().week))

    moves_to_weekly: List[Dict[str, str]] = []
    for path in weekly_keep_from_daily:
        item = daily_cache.get(path, {})
        dt = _infer_backup_datetime(item)
        if not dt:
            continue
        key = (dt.isocalendar().year, dt.isocalendar().week)
        if key in existing_week_keys:
            continue
        moves_to_weekly.append(
            {"path": path, "dest": weekly_dir, "ondup": "overwrite"}
        )

    if moves_to_weekly:
        log(
            f"Retention folders: promoting {len(moves_to_weekly)} "
            f"backups to weekly"
        )
        client.move_remote_files(moves_to_weekly)
        for m in moves_to_weekly:
            item = daily_cache.pop(m["path"], None)
            if item is not None:
                weekly_cache[m["path"]] = item

    # ── Phase 4: cleanup — enforce per-folder counts from caches ────────
    daily_keep = _select_bucket_keep_paths(
        list(daily_cache.values()),
        bucket_key_fn=lambda dt: dt.date(),
        keep_count=daily_n,
    )
    weekly_keep = _select_bucket_keep_paths(
        list(weekly_cache.values()),
        bucket_key_fn=lambda dt: (dt.isocalendar().year, dt.isocalendar().week),
        keep_count=weekly_n,
    )
    monthly_keep = _select_bucket_keep_paths(
        list(monthly_cache.values()),
        bucket_key_fn=lambda dt: (dt.year, dt.month),
        keep_count=monthly_n,
    )

    del_daily = _paths_to_delete(list(daily_cache.values()), daily_keep)
    del_weekly = _paths_to_delete(list(weekly_cache.values()), weekly_keep)
    del_monthly = _paths_to_delete(list(monthly_cache.values()), monthly_keep)

    if del_daily:
        log(f"Retention folders: deleting {len(del_daily)} backups from daily")
        client.delete_remote_files(del_daily)
    if del_weekly:
        log(
            f"Retention folders: deleting {len(del_weekly)} backups from weekly"
        )
        client.delete_remote_files(del_weekly)
    if del_monthly:
        log(
            f"Retention folders: deleting {len(del_monthly)} backups from monthly"
        )
        client.delete_remote_files(del_monthly)