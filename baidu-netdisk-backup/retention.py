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

    mtime = item.get("server_mtime") if item.get("server_mtime") is not None else item.get("server_ctime")
    if mtime is not None:
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

    若 *keep_count* <= 0，直接返回空集（语义：未启用 = 不保留）。
    """
    if keep_count is None or keep_count <= 0:
        return set()

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
            if len(seen) >= keep_count:
                break
            continue
        keep.add(path)
        seen.add(key)
        if len(seen) >= keep_count:
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

    # ── Phase 0: 将 base_upload_path 顶层遗留的 .tar 下沉到 每日/ ─────────
    # （flat → folder 模式切换后会有这种残留；清单文件.txt 留在顶层不动）
    top_items = client.list_remote_files(base_upload_path) or []
    moves_top_to_daily: List[Dict[str, str]] = []
    for item in top_items:
        if item.get("isdir") == 1:
            continue
        name = item.get("server_filename") or ""
        if not name.endswith(".tar"):
            continue
        p = item.get("path")
        if p:
            moves_top_to_daily.append(
                {"path": p, "dest": daily_dir, "ondup": "overwrite"}
            )
    if moves_top_to_daily:
        log(
            f"Retention folders: 将 {len(moves_top_to_daily)} 个顶层备份下沉到 每日/"
        )
        client.move_remote_files(moves_top_to_daily)

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
    # 守卫：keep_count <= 0 表示"未启用"，跳过对应目录的清理（保留全部）
    del_daily: List[str] = []
    del_weekly: List[str] = []
    del_monthly: List[str] = []

    if daily_n > 0:
        daily_keep = _select_bucket_keep_paths(
            list(daily_cache.values()),
            bucket_key_fn=lambda dt: dt.date(),
            keep_count=daily_n,
        )
        del_daily = _paths_to_delete(list(daily_cache.values()), daily_keep)

    if weekly_n > 0:
        weekly_keep = _select_bucket_keep_paths(
            list(weekly_cache.values()),
            bucket_key_fn=lambda dt: (dt.isocalendar().year, dt.isocalendar().week),
            keep_count=weekly_n,
        )
        del_weekly = _paths_to_delete(list(weekly_cache.values()), weekly_keep)

    if monthly_n > 0:
        monthly_keep = _select_bucket_keep_paths(
            list(monthly_cache.values()),
            bucket_key_fn=lambda dt: (dt.year, dt.month),
            keep_count=monthly_n,
        )
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


# ============================================================================
# 旧目录迁移（仅执行一次）
# ============================================================================
_MIGRATION_FLAG_FILE: str = "/data/migration_done.flag"


def migrate_old_dirs(
    client: "BaiduClient",  # type: ignore[valid-type]
    base_upload_path: str,
) -> List[Dict[str, Any]]:
    """将旧的英文目录（daily/weekly/monthly）中的文件迁移到新的中文目录。

    通过 `/data/migration_done.flag` 标记，仅执行一次；写标记失败时降级为每次扫描。

    Returns:
        list of {"from_dir", "to_dir", "count"} — 本次实际发生迁移的目录；
        无迁移时返回空列表。
    """
    import os as _os

    migrated: List[Dict[str, Any]] = []

    if _os.path.exists(_MIGRATION_FLAG_FILE):
        return migrated

    dir_mapping: Dict[str, str] = {
        "daily": "每日",
        "weekly": "每周",
        "monthly": "每月",
    }

    for old_name, new_name in dir_mapping.items():
        old_dir = _join_remote_dir(base_upload_path, old_name)
        new_dir = _join_remote_dir(base_upload_path, new_name)

        # 列出旧目录内容
        old_items = client.list_remote_files(old_dir) or []

        # 过滤出备份文件（排除目录项和非 tar 文件）
        files = [
            item
            for item in old_items
            if item.get("isdir") != 1
            and item.get("server_filename", "").endswith(".tar")
        ]

        if not files:
            log(f"旧目录 {old_dir} 不存在或无备份文件，跳过迁移")
            continue

        # 确保新目录存在
        client.create_remote_dir(new_dir)

        # 构造移动参数列表
        moves: List[Dict[str, str]] = []
        for item in files:
            path = item.get("path")
            if path:
                moves.append(
                    {"path": path, "dest": new_dir, "ondup": "overwrite"}
                )

        if moves:
            log(f"迁移旧目录：{old_dir} → {new_dir}，共 {len(moves)} 个文件")
            client.move_remote_files(moves)

            # 迁移完成后删除空的旧目录
            client.delete_remote_files([old_dir])
            log(f"迁移完成并已删除旧目录：{old_dir}")
            migrated.append(
                {"from_dir": old_dir, "to_dir": new_dir, "count": len(moves)}
            )

    # 写入完成标记，下次启动跳过迁移；写失败则下次仍然扫描（容错）
    try:
        _os.makedirs(_os.path.dirname(_MIGRATION_FLAG_FILE), exist_ok=True)
        with open(_MIGRATION_FLAG_FILE, "w") as f:
            f.write("done\n")
    except Exception as e:
        log(f"迁移完成标记写入失败（不影响功能，下次会重新扫描）：{e}")

    return migrated


# ============================================================================
# 生成备份清单文件
# ============================================================================
def generate_manifest(
    client: "BaiduClient",  # type: ignore[valid-type]
    base_upload_path: str,
) -> Optional[Dict[str, Any]]:
    """生成备份清单文件并上传到网盘。

    汇总每日/每周/每月三个子目录的文件数量、总大小和日期范围，
    写入清单文件.txt 后上传到 upload_path 目录下。
    """
    import os as _os
    import shutil as _shutil
    import tempfile as _tempfile

    dir_names: List[str] = ["每日", "每周", "每月"]
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines: List[str] = []
    lines.append("百度网盘备份清单")
    lines.append(f"生成时间：{now_str}")
    lines.append("=" * 50)
    lines.append("")

    total_count: int = 0
    total_size: int = 0

    for dir_name in dir_names:
        remote_dir = _join_remote_dir(base_upload_path, dir_name)
        items = client.list_remote_files(remote_dir) or []

        # 过滤出备份文件（排除目录项和非 tar 文件）
        files = [
            item
            for item in items
            if item.get("isdir") != 1
            and item.get("server_filename", "").endswith(".tar")
        ]

        count = len(files)
        dir_size = sum(int(f.get("size", 0)) for f in files)
        total_count += count
        total_size += dir_size

        # 提取备份日期，找出最早和最晚
        dates: List[datetime] = []
        for item in files:
            dt = _infer_backup_datetime(item)
            if dt:
                dates.append(dt)

        earliest = (
            min(dates).strftime("%Y-%m-%d %H:%M:%S") if dates else "无"
        )
        latest = (
            max(dates).strftime("%Y-%m-%d %H:%M:%S") if dates else "无"
        )

        # 格式化大小
        if dir_size >= 1024 * 1024 * 1024:
            size_str = f"{dir_size / (1024**3):.2f} GB"
        elif dir_size >= 1024 * 1024:
            size_str = f"{dir_size / (1024**2):.2f} MB"
        else:
            size_str = f"{dir_size / 1024:.2f} KB"

        lines.append(f"目录：{remote_dir}")
        lines.append(f"  文件数量：{count}")
        lines.append(f"  总大小：  {size_str}")
        lines.append(f"  最早备份：{earliest}")
        lines.append(f"  最晚备份：{latest}")
        lines.append("")

    # 合计汇总
    if total_size >= 1024 * 1024 * 1024:
        total_size_str = f"{total_size / (1024**3):.2f} GB"
    elif total_size >= 1024 * 1024:
        total_size_str = f"{total_size / (1024**2):.2f} MB"
    else:
        total_size_str = f"{total_size / 1024:.2f} KB"

    lines.append("=" * 50)
    lines.append(f"合计：{total_count} 个文件，总大小 {total_size_str}")

    content = "\n".join(lines) + "\n"

    # 在临时目录中创建固定名文件，确保云端文件名为 “清单文件.txt”
    # （client.upload_file 使用 os.path.basename(local_path) 作为远端文件名，
    # 因此必须让本地文件名就是目标名，而不能用 mkstemp 的随机名）
    tmp_dir = _tempfile.mkdtemp(prefix="manifest_")
    try:
        temp_path = _os.path.join(tmp_dir, "清单文件.txt")
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(content)

        log(f"清单文件已生成，正在上传到 {base_upload_path}/清单文件.txt")
        client.upload_file(temp_path, base_upload_path)
        log("清单文件上传完成")
        return {
            "manifest_path": f"{base_upload_path.rstrip('/')}/清单文件.txt",
            "file_count": total_count,
            "total_size": total_size,
        }
    finally:
        _shutil.rmtree(tmp_dir, ignore_errors=True)