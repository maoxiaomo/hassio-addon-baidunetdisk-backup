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

# v1.3.1：只删插件/HA 自动生成的备份；手工备份一律不删
_AUTO_BACKUP_RE = re.compile(r"^(?:automatic_backup|Automatic_backup)_")
DEFAULT_MAX_DELETE_PER_RUN = 10
MIN_MAX_DELETE_PER_RUN = 1
# 名字里的日期与 server_mtime 相差超过这个值 → 视为"用户改名/复制"的手工文件
_NAME_MTIME_TOLERANCE_SECONDS = 6 * 3600


def _retention_flags(retention: Dict[str, Any]) -> Dict[str, Any]:
    """从 retention 配置里取出护栏开关（缺项一律取安全侧）。

    ``max_delete_per_run <= 0`` **不再表示"不限"**：Web UI 的数字框清空即 0，
    那是把唯一的批量刹车一键关掉（P1-9）。0 / 负数 / 非数字一律回落默认 10。
    """
    raw_limit = retention.get("max_delete_per_run", DEFAULT_MAX_DELETE_PER_RUN)
    try:
        max_delete = int(raw_limit)
    except (TypeError, ValueError):
        max_delete = DEFAULT_MAX_DELETE_PER_RUN
    if max_delete < MIN_MAX_DELETE_PER_RUN:
        max_delete = DEFAULT_MAX_DELETE_PER_RUN
    return {
        "protect_manual": bool(retention.get("protect_manual", True)),
        "max_delete": max_delete,
        "dry_run": bool(retention.get("dry_run", False)),
    }


def _name_datetime(name: str) -> Optional[datetime]:
    """只从文件名解析日期（不回退 server_mtime）。"""
    m = _BACKUP_TS_RE.search(name or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").replace(
            hour=int(m.group(2)), minute=int(m.group(3))
        )
    except (ValueError, TypeError):
        return None


def _name_mtime_conflict(item: Dict[str, Any]) -> bool:
    """名字里的日期与 server_mtime 相差 > 6h → 不是插件自己写的那一份。

    ``protect_manual`` 只是命名启发式（P1-7）：用户把一份想长期保留的备份改名成
    ``automatic_backup_2026-11-03_16.01_keep.tar`` 时前缀照样命中。插件自己生成的
    文件，名字里的时间和文件 mtime 必然在同一分钟内；改名/复制过来的文件对不上。
    """
    dt = _name_datetime(item.get("server_filename") or "")
    if dt is None:
        return False
    mtime = item.get("server_mtime")
    if mtime is None:
        mtime = item.get("server_ctime")
    if mtime is None:
        return False
    try:
        return abs(int(dt.timestamp()) - int(mtime)) > _NAME_MTIME_TOLERANCE_SECONDS
    except (ValueError, TypeError, OSError):
        return False


def is_deletable(item: Dict[str, Any], protect_manual: bool = True) -> bool:
    """判断一个远端条目是否允许被保留策略删除。

    必须同时满足：
      1. 不是目录、以 .tar 结尾；
      2. protect_manual 时文件名匹配 HA 自动备份前缀；
      3. protect_manual 时名字里的日期与 server_mtime 相差不超过 6h
         （对不上 = 改名/复制来的手工文件）；
      4. 能推断出备份时间（无法定级 → 不删）。
    """
    if item.get("isdir") == 1:
        return False
    name = item.get("server_filename") or ""
    if not name.endswith(".tar"):
        return False
    if protect_manual:
        if not _AUTO_BACKUP_RE.match(name):
            return False
        if _name_mtime_conflict(item):
            return False
    if _infer_backup_datetime(item) is None:
        return False
    return True


def _count_protected(items: List[Dict[str, Any]], protect_manual: bool) -> int:
    """统计因护栏而不允许删除的 .tar 数量（用于通知里说明影响面）。"""
    n = 0
    for item in items:
        if item.get("isdir") == 1:
            continue
        name = item.get("server_filename") or ""
        if not name.endswith(".tar"):
            continue
        if not is_deletable(item, protect_manual):
            n += 1
    return n


def _apply_deletions(
    client: "BaiduClient",  # type: ignore[valid-type]
    label: str,
    paths: List[str],
    flags: Dict[str, Any],
) -> Dict[str, Any]:
    """统一删除入口：dry-run / 有界进度 / 结果如实计数。

    v1.3.0 的语义是"超过上限 → 一个都不删"，那是永久卡死（云端只增不减 → 配额
    满 → 上传失败 → 当日备份丢失）。v1.3.1 改成**有界进度**：每轮按最旧优先删
    ``min(候选, 上限)`` 个，并在通知里写"本轮删 N，剩 M，明天继续"。

    Returns:
        ``{"requested","deleted","failed","remaining","dry_run","guard"}``
    """
    res: Dict[str, Any] = {
        "requested": len(paths),
        "deleted": 0,
        "failed": 0,
        "remaining": 0,
        "dry_run": False,
        "guard": None,
    }
    if not paths:
        return res

    limit = int(flags["max_delete"])

    if flags["dry_run"]:
        log(f"DRY-RUN[{label}] 本应删除 {len(paths)} 个（单次上限 {limit}）：{paths[:20]}")
        res.update(
            dry_run=True,
            remaining=len(paths),
            guard=(
                f"dry-run：本应删除 {len(paths)} 个文件（单次上限 {limit}），实际未删除"
            ),
        )
        return res

    todo = paths[:limit]
    detailed = getattr(client, "delete_remote_files_detailed", None)
    if callable(detailed):
        stats = detailed(todo) or {}
        ok_n = int(stats.get("ok", 0) or 0)
        fail_n = int(stats.get("failed", 0) or 0)
        if not ok_n and not fail_n and "ok" not in stats:
            # 实现了接口但没回计数：退化成布尔语义
            ok_n, fail_n = (len(todo), 0) if stats.get("requested", 0) else (0, 0)
    else:
        ok = bool(client.delete_remote_files(todo))
        ok_n, fail_n = (len(todo), 0) if ok else (0, len(todo))

    if ok_n:
        marker = getattr(client, "mark_policy_deleted", None)
        if callable(marker) and fail_n == 0:
            # 只有整批都成功才标记"策略删除"，否则宁可下轮重传（重传安全）
            try:
                marker(todo[:ok_n])
            except Exception as e:                   # noqa: BLE001
                log(f"WARNING: 标记策略删除失败（不影响本轮结果）：{e}")

    res["deleted"] = ok_n
    res["failed"] = fail_n
    res["remaining"] = max(len(paths) - ok_n, 0)

    if fail_n and ok_n:
        res["guard"] = (
            f"{label}: 本轮已删除 {ok_n} 个，另有 {fail_n} 个删除失败（详见日志）；"
            f"剩余 {res['remaining']} 个待下次"
        )
    elif fail_n:
        res["guard"] = f"{label}: 删除失败 {fail_n} 个（云端未变化，详见日志）"
    elif res["remaining"]:
        res["guard"] = (
            f"{label}: 本轮删除 {ok_n} 个（单次上限 {limit}），"
            f"剩余 {res['remaining']} 个留到下次继续"
        )
    if res["guard"]:
        level = "ERROR" if fail_n else "WARNING"
        log(f"{level}: {res['guard']}")
    return res


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
    items: List[Dict[str, Any]],
    keep_set: Set[str],
    protect_manual: bool = True,
) -> List[str]:
    """Return paths in *items* that are NOT in *keep_set* and pass the guards.

    按时间**最旧优先**排序：有界进度（每轮删 N 个）必须可预测，否则"明天继续"
    可能每轮都从同一批里随机挑。
    """
    out: List[tuple] = []
    for item in items:
        if not is_deletable(item, protect_manual):
            continue
        path = item.get("path")
        if path and path not in keep_set:
            dt = _infer_backup_datetime(item) or datetime.max
            out.append((dt, path))
    out.sort(key=lambda pair: pair[0])
    return [path for _dt, path in out]


# ============================================================================
# Time-bucket selection  (Issue 5: unified via existing_keep parameter)
# ============================================================================
def _select_bucket_keep_paths(
    remote_items: List[Dict[str, Any]],
    bucket_key_fn: Callable[[datetime], Any],
    keep_count: int,
    existing_keep: Optional[Set[str]] = None,
    protect_manual: bool = True,
) -> Set[str]:
    """Select newest *keep_count* files per time bucket.

    When *existing_keep* is provided, paths already kept by a higher-priority
    bucket are counted toward the limit but not re-added.

    若 *keep_count* <= 0，直接返回空集（语义：未启用 = 不保留）。

    *protect_manual* 为真时，受保护的手工备份**不参与桶计数**：否则用户多做一份
    手工备份就会偷走一个每日槽位，表现为"我做了手工备份，然后少了一份每日备份"
    （E8）。
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
        if protect_manual and not is_deletable(item, True):
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
    protect_manual: bool = True,
) -> Set[str]:
    """Compute set of remote paths to retain under daily/weekly/monthly policy.

    Issue 5 fix: delegates all three tiers to ``_select_bucket_keep_paths``
    with cross-tier dedup via ``existing_keep``.
    """
    keep: Set[str] = set()

    if daily and daily > 0:
        keep |= _select_bucket_keep_paths(
            remote_items, lambda dt: dt.date(), daily,
            protect_manual=protect_manual,
        )

    if weekly and weekly > 0:
        keep |= _select_bucket_keep_paths(
            remote_items,
            lambda dt: (dt.isocalendar().year, dt.isocalendar().week),
            weekly,
            existing_keep=keep,
            protect_manual=protect_manual,
        )

    if monthly and monthly > 0:
        keep |= _select_bucket_keep_paths(
            remote_items,
            lambda dt: (dt.year, dt.month),
            monthly,
            existing_keep=keep,
            protect_manual=protect_manual,
        )

    return keep


def cleanup_remote_backups(
    client: "BaiduClient",  # type: ignore[valid-type]
    upload_path: str,
    retention: Dict[str, Any],
) -> Dict[str, Any]:
    """应用扁平保留策略，返回执行摘要。

    v1.3.0 护栏：只删自动命名备份、无法定级不删、dry-run、单次上限。
    """
    summary: Dict[str, Any] = {
        "upload_path": upload_path,
        "deleted": 0,
        "failed": 0,
        "remaining": 0,
        "candidates": 0,
        "protected": 0,
        "dry_run": False,
        "guard": None,
    }
    if not retention:
        return summary

    daily = int(retention.get("daily", 0) or 0)
    weekly = int(retention.get("weekly", 0) or 0)
    monthly = int(retention.get("monthly", 0) or 0)

    if daily <= 0 and weekly <= 0 and monthly <= 0:
        return summary

    flags = _retention_flags(retention)
    summary["dry_run"] = flags["dry_run"]

    # 列举失败 → 抛 RemoteListError，绝不当成空目录去算 keep
    remote_items = client.list_remote_files(upload_path)

    keep = _compute_retention_keep_paths(
        remote_items,
        daily=daily,
        weekly=weekly,
        monthly=monthly,
        protect_manual=flags["protect_manual"],
    )

    candidates = _paths_to_delete(remote_items, keep, flags["protect_manual"])
    summary["candidates"] = len(candidates)
    summary["protected"] = _count_protected(remote_items, flags["protect_manual"])

    if not candidates:
        log("Remote retention: nothing to delete")
        return summary

    log(
        f"Remote retention: keeping {len(keep)} backups, "
        f"candidates {len(candidates)} (protected {summary['protected']})"
    )
    applied = _apply_deletions(client, "Remote retention", candidates, flags)
    summary["deleted"] = applied["deleted"]
    summary["failed"] = applied["failed"]
    summary["remaining"] = applied["remaining"]
    summary["guard"] = applied["guard"]
    return summary


# ============================================================================
# Folder-mode retention (daily / weekly / monthly sub-directories)
# ============================================================================
def retention_folder_mode(
    client: "BaiduClient",  # type: ignore[valid-type]
    base_upload_path: str,
    retention: Dict[str, Any],
) -> Dict[str, Any]:
    """Apply folder-mode retention with promotion between tiers.

    Issue 4 fix: initial listing results are cached in dicts keyed by path.
    After move operations the caches are updated manually, eliminating 4 of
    the original 7 ``list_remote_files`` calls.

    v1.3.1 修复：
      * dry-run 覆盖**所有**写操作（顶层下沉 / 晋升 / 删除），不再"演练期改布局"；
      * 晋升移动失败时不清缓存，文件不会从两个视图里同时消失（E4）；
      * 受保护的手工备份不参与桶计数，也不被晋升搬走（E8）。
    """
    daily_n = int(retention.get("daily", 0) or 0)
    weekly_n = int(retention.get("weekly", 0) or 0)
    monthly_n = int(retention.get("monthly", 0) or 0)
    flags = _retention_flags(retention)
    protect = flags["protect_manual"]
    dry_run = flags["dry_run"]

    daily_dir = _join_remote_dir(base_upload_path, "每日")
    weekly_dir = _join_remote_dir(base_upload_path, "每周")
    monthly_dir = _join_remote_dir(base_upload_path, "每月")

    if dry_run:
        # 演练模式连"建目录"都不做：它同样是对云端布局的写操作
        log("DRY-RUN[Retention folders] 跳过创建/确认远端目录")
    else:
        client.create_remote_dir(base_upload_path)
        client.create_remote_dir(daily_dir)
        client.create_remote_dir(weekly_dir)
        client.create_remote_dir(monthly_dir)

    summary: Dict[str, Any] = {
        "upload_path": base_upload_path,
        "deleted": 0,
        "failed": 0,
        "remaining": 0,
        "candidates": 0,
        "protected": 0,
        "dry_run": dry_run,
        "moves_skipped": 0,
        "move_errors": 0,
        "guard": None,
    }

    def _move(moves: List[Dict[str, str]], what: str) -> bool:
        """统一移动入口：dry-run 只记录；失败不解缓存（让下轮重试）。"""
        if not moves:
            return False
        if dry_run:
            log(
                f"DRY-RUN[Retention folders] 将移动 {len(moves)} 个（{what}），实际未移动"
            )
            summary["moves_skipped"] += len(moves)
            return False
        if not client.move_remote_files(moves):
            summary["move_errors"] += len(moves)
            log(f"ERROR: Retention folders: 移动失败（{what}），保留原缓存待下轮重试")
            return False
        return True

    # ── Phase 0: 将 base_upload_path 顶层遗留的 .tar 下沉到 每日/ ─────────
    # （flat → folder 模式切换后会有这种残留；清单文件.txt 留在顶层不动）
    top_items = client.list_remote_files(base_upload_path)
    moves_top_to_daily: List[Dict[str, str]] = []
    for item in top_items:
        if item.get("isdir") == 1:
            continue
        name = item.get("server_filename") or ""
        if not name.endswith(".tar"):
            continue
        if protect and not is_deletable(item, True):
            # 用户手工放在顶层的备份不搬动
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
        _move(moves_top_to_daily, "顶层 → 每日")

    # ── Phase 1: list once per directory (3 API calls) ──────────────────
    daily_items = client.list_remote_files(daily_dir)
    weekly_items = client.list_remote_files(weekly_dir)
    monthly_items = client.list_remote_files(monthly_dir)

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
        protect_manual=protect,
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
        if _move(moves_to_monthly, "每日 → 每月"):
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
        protect_manual=protect,
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
        if _move(moves_to_weekly, "每日 → 每周"):
            for m in moves_to_weekly:
                item = daily_cache.pop(m["path"], None)
                if item is not None:
                    weekly_cache[m["path"]] = item

    # ── Phase 4: cleanup — enforce per-folder counts from caches ────────
    # 守卫：keep_count <= 0 表示"未启用"，跳过对应目录的清理（保留全部）
    del_all: List[str] = []

    if daily_n > 0:
        daily_keep = _select_bucket_keep_paths(
            list(daily_cache.values()),
            bucket_key_fn=lambda dt: dt.date(),
            keep_count=daily_n,
            protect_manual=protect,
        )
        daily_del = _paths_to_delete(
            list(daily_cache.values()), daily_keep, protect
        )
        log(f"Retention folders: daily 待删除 {len(daily_del)} 个")
        del_all.extend(daily_del)

    if weekly_n > 0:
        weekly_keep = _select_bucket_keep_paths(
            list(weekly_cache.values()),
            bucket_key_fn=lambda dt: (dt.isocalendar().year, dt.isocalendar().week),
            keep_count=weekly_n,
            protect_manual=protect,
        )
        weekly_del = _paths_to_delete(
            list(weekly_cache.values()), weekly_keep, protect
        )
        log(f"Retention folders: weekly 待删除 {len(weekly_del)} 个")
        del_all.extend(weekly_del)

    if monthly_n > 0:
        monthly_keep = _select_bucket_keep_paths(
            list(monthly_cache.values()),
            bucket_key_fn=lambda dt: (dt.year, dt.month),
            keep_count=monthly_n,
            protect_manual=protect,
        )
        monthly_del = _paths_to_delete(
            list(monthly_cache.values()), monthly_keep, protect
        )
        log(f"Retention folders: monthly 待删除 {len(monthly_del)} 个")
        del_all.extend(monthly_del)

    summary["candidates"] = len(del_all)
    summary["protected"] = (
        _count_protected(daily_items, protect)
        + _count_protected(weekly_items, protect)
        + _count_protected(monthly_items, protect)
    )
    applied = _apply_deletions(client, "Retention folders", del_all, flags)
    summary["deleted"] = applied["deleted"]
    summary["failed"] = applied["failed"]
    summary["remaining"] = applied["remaining"]
    summary["guard"] = applied["guard"]
    extras: List[str] = []
    if dry_run and summary["moves_skipped"]:
        extras.append(f"dry-run：另有 {summary['moves_skipped']} 个文件本应被移动/晋升")
    if summary["move_errors"]:
        extras.append(
            f"移动/晋升失败 {summary['move_errors']} 个（文件仍在原目录，下轮重试）"
        )
    if extras:
        joined = "；".join(extras)
        summary["guard"] = f"{summary['guard']}；{joined}" if summary["guard"] else joined
    return summary


# ============================================================================
# 旧目录迁移（仅执行一次）
# ============================================================================
_MIGRATION_FLAG_FILE: str = "/data/migration_done.flag"


def migrate_old_dirs(
    client: "BaiduClient",  # type: ignore[valid-type]
    base_upload_path: str,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """将旧的英文目录（daily/weekly/monthly）中的文件迁移到新的中文目录。

    通过 `/data/migration_done.flag` 标记，仅执行一次；写标记失败时降级为每次扫描。

    v1.3.1：
      * *dry_run* 为真时只记录"将移动 N 个"，**不移动、不删旧目录、不写 flag**
        （v1.3.0 的 dry_run 只拦删除，迁移路径照样改写云端布局 —— P1-5）；
      * 任一批次移动失败就**不写** flag（v1.3.0 在 for 循环外无条件写，`continue`
        管不到，迁移被永久跳过 —— E3）。

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

    all_ok = True
    pending = 0

    for old_name, new_name in dir_mapping.items():
        old_dir = _join_remote_dir(base_upload_path, old_name)
        new_dir = _join_remote_dir(base_upload_path, new_name)

        # 列出旧目录内容（失败会抛 RemoteListError → 上层记 post_error，不写 flag）
        old_items = client.list_remote_files(old_dir)

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

        moves: List[Dict[str, str]] = []
        for item in files:
            path = item.get("path")
            if path:
                moves.append({"path": path, "dest": new_dir, "ondup": "overwrite"})

        if not moves:
            continue

        if dry_run:
            pending += len(moves)
            log(
                f"DRY-RUN[旧目录迁移] 将移动 {len(moves)} 个文件：{old_dir} → {new_dir}"
                f"（未移动，也未删除旧目录）"
            )
            continue

        # 确保新目录存在
        client.create_remote_dir(new_dir)

        log(f"迁移旧目录：{old_dir} → {new_dir}，共 {len(moves)} 个文件")
        if not client.move_remote_files(moves):
            log(f"ERROR: 迁移 {old_dir} 存在失败批次，保留旧目录待下次重试")
            all_ok = False
            continue

        # 只有确认移动成功后才删除空的旧目录
        if not client.delete_remote_files([old_dir]):
            log(f"WARNING: 旧目录 {old_dir} 删除失败（文件已迁移）")
        else:
            log(f"迁移完成并已删除旧目录：{old_dir}")
        migrated.append(
            {"from_dir": old_dir, "to_dir": new_dir, "count": len(moves)}
        )

    if dry_run:
        if pending:
            log(f"DRY-RUN[旧目录迁移] 合计将移动 {pending} 个文件；不写迁移完成标记")
        return migrated

    if not all_ok:
        log("ERROR: 迁移未全部成功，本次**不写** migration_done.flag，下次继续重试")
        return migrated

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
        try:
            items = client.list_remote_files(remote_dir)
        except Exception as e:
            # 关键：列举失败 ≠ 0 个文件。宁可本次不生成清单，也不覆盖线上清单。
            log(f"ERROR: 列举 {remote_dir} 失败，放弃本次清单生成（不会写入 0 个文件）：{e}")
            raise RuntimeError(f"清单生成中止：列举 {remote_dir} 失败：{e}") from e

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
        try:
            client.upload_file(temp_path, base_upload_path)   # 失败会抛 UploadError
        except Exception as e:
            log(f"ERROR: 清单文件上传失败：{e}")
            raise
        log("清单文件上传完成")
        return {
            "manifest_path": f"{base_upload_path.rstrip('/')}/清单文件.txt",
            "file_count": total_count,
            "total_size": total_size,
        }
    finally:
        _shutil.rmtree(tmp_dir, ignore_errors=True)