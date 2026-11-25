#!/usr/bin/env python3
"""Entry point & scheduling loop for the Baidu Netdisk Backup add-on."""
import inspect
import json
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import notifier as notifier_mod

from client import BaiduClient, log
from notifier import notify_event
from retention import (
    cleanup_remote_backups,
    generate_manifest,
    migrate_old_dirs,
    retention_folder_mode,
    _join_remote_dir,
)
from sync import sync_all_backups
from web import (
    publish_sync_status,
    register_config_reload_callback,
    start_web_server,
    take_pending_options,
    wait_for_manual_sync,
)

CONFIG_PATH: str = "/data/options.json"


# ============================================================================
# 跨模块能力的兼容探测（v1.3.1 / F1：S2、S3 可以不含 S1）
# ============================================================================
# 背景：S2 的发布清单只有 sync.py + main.py（+ client 的 S2 部分），S3 只有
# web.py（+ client 的 S3 部分），两者都**不带** notifier.py / retention.py 的
# 补丁。任何"main 直接引用新符号/新形参"的写法都会让 S2/S3 单独发布时启动失败
# （ImportError）或每轮产生假失败（TypeError 被收进 post_errors → 成功的备份被
# 标成 backup_failure）。所以这里一律做**能力探测**，有就用、没有就退化成等价
# 行为，并把降级事实写进日志。

# 与 notifier.DEFAULT_EVENT_ENABLED（v1.3.1）保持一致的兜底默认值
_EVENT_DEFAULT_ENABLED: Dict[str, bool] = {
    "backup_success": True,
    "backup_failure": True,
    "storage_warning": True,
    "backup_empty": True,
    "retention_guard": True,
    # 这两个是"信息性"事件，默认关，避免把结果通知淹没
    "migration_done": False,
    "manifest_generated": False,
}


def _fallback_event_enabled(notifications: Any, event_type: str) -> bool:
    """v1.3.0 的 notifier 没有 ``event_enabled`` 时的等价判定。"""
    if not isinstance(notifications, dict):
        return False
    if not notifications.get("enabled", True):
        return False
    default = _EVENT_DEFAULT_ENABLED.get(event_type, True)
    events_cfg = notifications.get("events", {})
    if isinstance(events_cfg, dict):
        return bool(events_cfg.get(event_type, default))
    return default


def event_enabled(notifications: Dict[str, Any], event_type: str) -> bool:
    """某事件当前是否真的会发出去（调用方据此决定"兜底通知"）。

    优先用 ``notifier.event_enabled``（v1.3.1 的 notifier 补丁新增）；该函数
    不存在时（S2/S3 单独发布、notifier 仍是 v1.3.0）用本地等价实现，
    **不 ImportError**。
    """
    func = getattr(notifier_mod, "event_enabled", None)
    if callable(func):
        return bool(func(notifications, event_type))
    return _fallback_event_enabled(notifications, event_type)


def _accepts_keyword(func: Any, name: str) -> bool:
    """能力探测：*func* 是否接受名为 *name* 的关键字参数。"""
    if not callable(func):
        return False
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):          # 内建/装饰器包装：签名不可得 → 当作不支持
        return False
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _retention_supports_dry_run() -> bool:
    """当前 retention 模块是否真的实现了 ``retention.dry_run``。

    不能用 ``_retention_flags`` 判定：v1.3.0 的 flags 里已经有 ``dry_run`` 键，
    但 ``retention_folder_mode`` 根本不读它、``migrate_old_dirs`` 也没有这个
    形参 —— 默认的 folder 模式下 dry_run 会被**静默忽略**（照删照移）。这里用
    "同一份 S1 补丁才会加上的形参"作探针。
    """
    return _accepts_keyword(migrate_old_dirs, "dry_run")


def _call_migrate_old_dirs(
    client: BaiduClient, upload_path: str, dry_run: bool
) -> List[Dict[str, Any]]:
    """调用 ``retention.migrate_old_dirs``，按被调函数的签名决定是否传 dry_run。

    v1.3.0 的签名是 ``migrate_old_dirs(client, base_upload_path)``：直接传
    ``dry_run=`` 会 TypeError，被 ``run_sync_cycle`` 收进 post_errors →
    每轮把成功的备份标成 backup_failure。这里探测签名后降级为不传。
    """
    func = migrate_old_dirs           # 模块级名字，保留 monkeypatch 能力
    if _accepts_keyword(func, "dry_run"):
        return list(func(client, upload_path, dry_run=dry_run) or [])
    return list(func(client, upload_path) or [])


# ============================================================================
# Cron 解析（5 字段：min hour dom mon dow）
# ============================================================================
class CronSchedule:
    """支持 `*`、`N`、`N-M`、`N-M/S`、`*/S`、`N,M,...` 的最小 cron 解析器。

    dow / dom 使用经典 cron 的"OR"语义：仅当两者都不是 `*` 时取并集；都是 `*`
    时为 AND（即不限制）。dow 0=Sunday，与 crontab(5) 一致。
    """

    _FIELD_RANGES = [
        (0, 59),   # minute
        (0, 23),   # hour
        (1, 31),   # day-of-month
        (1, 12),   # month
        (0, 6),    # day-of-week (0=Sunday)
    ]

    def __init__(self, expr: str) -> None:
        self.expr: str = expr
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError(f"cron expression must have 5 fields, got: {expr!r}")
        self.fields = [
            self._parse_field(parts[i], *self._FIELD_RANGES[i])
            for i in range(5)
        ]
        self.minute_set, self.hour_set, self.dom_set, self.mon_set, self.dow_set = (
            self.fields
        )
        self.dom_restricted: bool = parts[2] != "*"
        self.dow_restricted: bool = parts[4] != "*"

    @staticmethod
    def _parse_field(spec: str, lo: int, hi: int) -> set:
        out: set = set()
        for token in spec.split(","):
            step = 1
            if "/" in token:
                token, step_s = token.split("/", 1)
                step = int(step_s)
                if step <= 0:
                    raise ValueError(f"invalid step in cron field: {spec!r}")
            if token == "*":
                start, end = lo, hi
            elif "-" in token:
                a, b = token.split("-", 1)
                start, end = int(a), int(b)
            else:
                start = end = int(token)
            if start < lo or end > hi or start > end:
                raise ValueError(
                    f"cron field {spec!r} out of range [{lo},{hi}]"
                )
            out.update(range(start, end + 1, step))
        return out

    def matches(self, dt: datetime) -> bool:
        # cron 中 weekday 0 / 7 都表示周日；Python weekday(): Mon=0..Sun=6
        cron_dow = (dt.weekday() + 1) % 7  # → Sun=0..Sat=6
        if dt.minute not in self.minute_set:
            return False
        if dt.hour not in self.hour_set:
            return False
        if dt.month not in self.mon_set:
            return False
        dom_ok = dt.day in self.dom_set
        dow_ok = cron_dow in self.dow_set
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def next_fire(self, now: datetime) -> datetime:
        # 从 now+1min 起按分钟扫描；上限 4 年防止表达式无解时死循环
        t = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
        limit = t + timedelta(days=366 * 4)
        while t < limit:
            if self.matches(t):
                return t
            t += timedelta(minutes=1)
        raise ValueError(f"cron expression has no firing within 4 years: {self.expr!r}")


def parse_cron(schedule_str: str) -> CronSchedule:
    """解析 cron；解析失败时回退到默认 `0 5 * * *` 并打日志。"""
    try:
        return CronSchedule(schedule_str)
    except (ValueError, IndexError, AttributeError, TypeError) as e:
        log(f"Invalid cron expression {schedule_str!r}: {e}. Falling back to '0 5 * * *'.")
        return CronSchedule("0 5 * * *")


DEFAULTS: Dict[str, Any] = {
    "upload_path": "/HomeAssistant/Backup",
    "schedule": "0 5 * * *",
}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_options(options: Dict[str, Any]) -> Dict[str, Any]:
    """纯函数：options.json 的 dict → 运行时配置（缺项一律兜底，只增不改）。"""
    if not isinstance(options, dict):
        options = {}

    retention_raw = options.get("retention") or {}
    if not isinstance(retention_raw, dict):
        retention_raw = {}
    retention: Dict[str, Any] = {
        "daily": _as_int(retention_raw.get("daily"), 7),
        "weekly": _as_int(retention_raw.get("weekly"), 4),
        "monthly": _as_int(retention_raw.get("monthly"), 12),
        "use_folders": bool(retention_raw.get("use_folders", True)),
        # v1.3.0 护栏（老配置没有 → 取安全侧默认）
        "protect_manual": bool(retention_raw.get("protect_manual", True)),
        "max_delete_per_run": _as_int(retention_raw.get("max_delete_per_run"), 10),
        "dry_run": bool(retention_raw.get("dry_run", False)),
    }

    retry_raw = options.get("retry") or {}
    if not isinstance(retry_raw, dict):
        retry_raw = {}
    sync_raw = options.get("sync") or {}
    if not isinstance(sync_raw, dict):
        sync_raw = {}

    notifications = options.get("notifications") or {}
    if not isinstance(notifications, dict):
        notifications = {}

    schedule = str(options.get("schedule") or DEFAULTS["schedule"])

    return {
        "refresh_token": str(
            options.get("refresh_token") or os.environ.get("REFRESH_TOKEN", "")
        ),
        "upload_path": str(options.get("upload_path") or DEFAULTS["upload_path"]),
        "schedule": schedule,
        "cron": parse_cron(schedule),
        "retention": retention,
        "notifications": notifications,
        "retry_deadline": max(_as_float(retry_raw.get("deadline_seconds"), 90.0), 10.0),
        "cycle_budget": max(
            _as_float(retry_raw.get("cycle_budget_seconds"), 1800.0), 60.0
        ),
        "recheck_days": max(_as_float(sync_raw.get("recheck_days"), 7.0), 0.0),
    }


def load_config(options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读取配置。*options* 非空时直接用它（Web UI 刚提交的那份），否则读文件。"""
    if options is None:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                options = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            log(f"Config file not available ({e}), using defaults")
            options = {}
    return parse_options(options)


def init_client(refresh_token: str) -> BaiduClient:
    """Create a BaiduClient with error handling."""
    try:
        return BaiduClient(refresh_token)
    except Exception as e:
        log(f"Failed to initialize client: {e}")
        log("Please check your refresh_token and try again.")
        raise


def _collect_guard(summary: Any) -> List[str]:
    """收集保留策略的护栏说明（不在这里发通知）。

    v1.3.0 的 ``_report_retention_guard`` 直接 notify_event，然后
    ``_notify_sync_result`` 再发一条 → 一轮两条通知，和"本轮只有一条通知"矛盾
    （E5）。现在护栏文字并入本轮结果通知；只有主事件被用户关掉时才用
    ``retention_guard`` 兜底，保证"不静默"。
    """
    if not isinstance(summary, dict):
        return []
    guard = summary.get("guard")
    return [str(guard)] if guard else []


def _notify_sync_result(
    sync_result: Dict[str, Any],
    notifications: Dict[str, Any],
    upload_path: str,
    post_errors: Optional[List[str]] = None,
    trigger: str = "schedule",
    guard_msgs: Optional[List[str]] = None,
) -> str:
    """本轮唯一的通知出口：按 outcome 穷举，绝无"两个字段都假就沉默"。

    Returns: 实际发出的事件名（空字符串 = 被配置关闭）。
    """
    payload: Dict[str, Any] = dict(sync_result)
    payload["upload_path"] = upload_path
    payload["trigger"] = trigger
    post_errors = post_errors or []
    guard_msgs = [g for g in (guard_msgs or []) if g]
    if guard_msgs:
        payload["guard"] = "；".join(guard_msgs)

    outcome = payload.get("outcome") or ("ok" if payload.get("success") else "failed")

    if post_errors:
        payload["stage"] = "、".join(e.split("：", 1)[0] for e in post_errors)
        payload["error"] = "；".join(
            ([payload["error"]] if payload.get("error") else []) + post_errors
        )
        if outcome == "ok":
            payload["outcome"] = "partial"
        elif outcome == "empty":
            payload["outcome"] = "failed"
        notify_event(notifications, "backup_failure", payload)
        return "backup_failure"

    if outcome == "ok":
        event = "backup_success"
    elif outcome == "empty":
        log("WARNING: /backup 无 .tar 文件，跳过成功通知")
        event = "backup_empty"
    else:
        event = "backup_failure"

    # 一轮只有一条通知。但若主事件被用户关掉、而本轮又发生了护栏事件，
    # 用 retention_guard 兜底，避免"护栏静默"（P0-1c 的老毛病）。
    if payload.get("guard") and not event_enabled(notifications, event):
        event = "retention_guard"

    payload["outcome"] = outcome
    notify_event(notifications, event, payload)
    return event


def run_sync_cycle(
    client: BaiduClient,
    cfg: Dict[str, Any],
    trigger: str = "schedule",
    budget_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """执行一轮完整同步；无论成败都只发一条结果通知。"""
    upload_path = cfg["upload_path"]
    retention = cfg["retention"]
    notifications = cfg["notifications"]
    recheck_days = float(cfg.get("recheck_days", 7.0))
    budget = float(
        budget_seconds if budget_seconds is not None else cfg.get("cycle_budget", 1800.0)
    )
    deadline = time.monotonic() + max(budget, 60.0)
    dry_run = bool(retention.get("dry_run", False))
    if dry_run and not _retention_supports_dry_run():
        # 静默忽略比报错更危险（v1.3.0 的 folder 模式照删照移）。必须显式告警：
        # 让运维看到"演练没生效"，而不是以为自己在演练。
        log(
            "WARNING: retention 模块不支持 dry_run，本次未启用演练"
            "（当前 retention.py 是旧版：folder 模式与旧目录迁移不看 dry_run，"
            "仍会真实删除/移动；请应用 patches/retention.py.patch（S1）后再演练）"
        )

    post_errors: List[str] = []
    guard_msgs: List[str] = []
    list_before = int(getattr(client, "list_calls", 0) or 0)

    if retention.get("use_folders", True):
        daily_dir = _join_remote_dir(upload_path, "每日")
        sync_result = sync_all_backups(
            client, daily_dir, deadline=deadline, recheck_days=recheck_days
        )

        if sync_result.get("outcome") != "empty":
            try:
                # 迁移旧英文目录到新中文目录（仅首次执行；dry-run 只记录）
                # 信息性事件也走事件开关，保证"一轮只有一条结果通知"
                if event_enabled(notifications, "migration_done"):
                    for info in _call_migrate_old_dirs(client, upload_path, dry_run):
                        notify_event(notifications, "migration_done", info)
                else:
                    _call_migrate_old_dirs(client, upload_path, dry_run)
            except Exception as e:
                post_errors.append(f"旧目录迁移：{e}")
                log(f"ERROR: 旧目录迁移失败：{e}")

            try:
                summary = retention_folder_mode(client, upload_path, retention)
                guard_msgs.extend(_collect_guard(summary))
            except Exception as e:
                post_errors.append(f"远端保留策略：{e}")
                log(f"ERROR: 远端保留策略失败：{e}")

            # 每次 retention 完成后生成备份清单文件
            try:
                manifest_info = generate_manifest(client, upload_path)
                if manifest_info and event_enabled(notifications, "manifest_generated"):
                    notify_event(notifications, "manifest_generated", manifest_info)
            except Exception as e:
                post_errors.append(f"清单生成：{e}")
                log(f"ERROR: 清单生成失败：{e}")
        else:
            # 源端空了：远端一份都不剪（P1-8）。否则挂载掉了会把历史删到只剩 7 份。
            log(
                "WARNING: /backup 为空，本轮跳过旧目录迁移、保留策略与清单生成"
                "（不做任何云端删除）"
            )
    else:
        sync_result = sync_all_backups(
            client, upload_path, deadline=deadline, recheck_days=recheck_days
        )
        if sync_result.get("outcome") != "empty":
            try:
                summary = cleanup_remote_backups(client, upload_path, retention)
                guard_msgs.extend(_collect_guard(summary))
            except Exception as e:
                post_errors.append(f"远端保留策略：{e}")
                log(f"ERROR: 远端保留策略失败：{e}")
        else:
            log("WARNING: /backup 为空，本轮跳过保留策略（不做任何云端删除）")

    event = _notify_sync_result(
        sync_result, notifications, upload_path, post_errors, trigger, guard_msgs
    )
    list_after = int(getattr(client, "list_calls", 0) or 0)
    log(
        f"本轮通知：{event or '（被配置关闭）'}；list 请求 {list_after - list_before} 次"
        "（folder 模式基线 7 次 = 顶层 1 + 三档 3 + 清单 3；旧目录迁移仅在首次"
        "（无 migration_done.flag）另加 3 次；缓存核对按 recheck_days 惰性追加）"
    )

    # 存储空间检查（独立告警，不属于"同步结果通知"）
    _check_storage_warning(client, notifications)
    return sync_result


def _check_storage_warning(
    client: BaiduClient,
    notifications: Dict[str, Any],
) -> None:
    """检查网盘容量，达到阈值时触发 storage_warning 通知。

    阈值取 notifications.storage_warning_threshold（0-1 之间小数；默认 0.9）。
    """
    try:
        threshold = float(notifications.get("storage_warning_threshold", 0.9))
    except (TypeError, ValueError):
        threshold = 0.9
    if threshold <= 0 or threshold >= 1:
        threshold = 0.9

    try:
        quota = client.get_quota()
    except Exception as e:
        log(f"Storage warning check error: {e}")
        return
    if not quota or quota.get("total", 0) <= 0:
        return

    used = quota["used"]
    total = quota["total"]
    ratio = used / total
    log(
        f"网盘容量：已用 {used / 1024**3:.2f} GB / 总 {total / 1024**3:.2f} GB "
        f"({ratio * 100:.1f}%)，阈值 {threshold * 100:.0f}%"
    )
    if ratio >= threshold:
        notify_event(
            notifications,
            "storage_warning",
            {"used": used, "total": total, "free": quota.get("free", 0)},
        )


_sync_lock = threading.Lock()
# 锁竞争时"排队一次"（P1-3）：拿不到锁不再静默丢弃
_deferred_lock = threading.Lock()
_deferred_trigger: Optional[str] = None


def _defer_trigger(trigger: str) -> bool:
    """登记一次待补跑；已存在排队时返回 False（合并，不叠加）。"""
    global _deferred_trigger
    with _deferred_lock:
        if _deferred_trigger is None:
            _deferred_trigger = trigger
            return True
    return False


def take_deferred_trigger() -> Optional[str]:
    """取走排队中的触发（最多补跑一次）。"""
    global _deferred_trigger
    with _deferred_lock:
        trigger = _deferred_trigger
        _deferred_trigger = None
    return trigger


def _wait_until(target: datetime) -> bool:
    """睡到 *target*；期间每 20s 检查一次手动同步请求。返回 True 表示被手动唤醒。"""
    while True:
        remaining = (target - datetime.now()).total_seconds()
        if remaining <= 0:
            return False
        if wait_for_manual_sync(min(remaining, 20.0)):
            return True


# 整轮预算的下界：再小也做不完一个文件，只会制造"必然失败"的通知
MIN_CYCLE_BUDGET = 120.0
# 距下次触发超过这个时间就不必收缩（日/周级 cron 的常态）
NO_SHRINK_ROOM = 86400.0


def _cycle_budget(
    cfg: Dict[str, Any],
    trigger: str = "schedule",
    now: Optional[datetime] = None,
) -> float:
    """整轮预算；靠近下一个 cron 点时收缩，避免影响下一次触发。

    v1.3.1：
      * 只有 ``trigger == "schedule"`` 才收缩。手动/启动触发在 04:50 撞上 05:00
        的 cron 时收缩到 480s，只会先给一条"预算耗尽"失败、再给一条成功（A5）。
      * ``room > 24h`` 直接返回原预算，不靠数值巧合。
      * 收缩有下界 120s，不会把预算砍成"注定失败"。
    """
    budget = max(float(cfg.get("cycle_budget") or 1800.0), MIN_CYCLE_BUDGET)
    if trigger != "schedule":
        return budget
    try:
        base = now or datetime.now()
        room = (cfg["cron"].next_fire(base) - base).total_seconds() - 120.0
    except Exception:
        return budget
    if room > NO_SHRINK_ROOM:
        return budget
    if room >= budget:
        return budget
    shrunk = max(room, MIN_CYCLE_BUDGET)
    log(f"同步时间预算收缩到 {shrunk:.0f}s（保护下一次 cron 触发）")
    return shrunk


def _apply_token_if_changed(client: BaiduClient, cfg: Dict[str, Any]) -> None:
    """热加载改过 refresh_token 时，在下一次同步开始时换新（P1-1）。"""
    new_token = cfg.get("refresh_token") or ""
    if not new_token or new_token == client.initial_refresh_token:
        return
    log("检测到 refresh_token 变更，正在重新获取 access_token…")
    updater = getattr(client, "update_refresh_token", None)
    if not callable(updater):
        # 半套补丁（main 已升、client 未升）时给一句能看懂的话，而不是每轮 AttributeError
        raise RuntimeError(
            "client.update_refresh_token 不存在：client.py 与 main.py 版本不一致，"
            "请应用 patches/client.py.patch"
        )
    try:
        ok = updater(new_token)
    except Exception as e:                       # noqa: BLE001
        log(f"ERROR: 使用新的 refresh_token 获取 access_token 失败：{e}")
        raise
    if not ok:
        raise RuntimeError("refresh_token 已更新，但未能获取新的 access_token")
    log("refresh_token 已生效（后续上传使用新的 access_token）")


def _run_once(
    client: BaiduClient, cfg: Dict[str, Any], trigger: str, wait_seconds: float = 0.0
) -> bool:
    """加锁执行一轮；返回是否真的执行了。"""
    acquired = (
        _sync_lock.acquire(timeout=wait_seconds)
        if wait_seconds > 0
        else _sync_lock.acquire(blocking=False)
    )
    if not acquired:
        # 不能静默丢弃：当日备份会整天不上传（P1-3）。排队一次 + 发通知。
        queued = _defer_trigger(trigger)
        log(
            "WARNING: 上一轮同步仍在运行，本次触发已"
            + ("排队一次（结束后立即补跑）" if queued else "排队（已有排队，合并为一次）")
        )
        if queued:
            notify_event(
                cfg.get("notifications") or {},
                "backup_failure",
                {
                    "upload_path": cfg.get("upload_path"),
                    "trigger": trigger,
                    "stage": "调度",
                    "error": (
                        "上一轮同步仍在运行，本次触发已排队一次，将在其结束后立即补跑；"
                        "若补跑仍失败会另外通知"
                    ),
                },
            )
        return False

    publish_sync_status(
        {
            "running": True,
            "trigger": trigger,
            "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    try:
        _apply_token_if_changed(client, cfg)
        result = run_sync_cycle(
            client,
            cfg,
            trigger=trigger,
            budget_seconds=_cycle_budget(cfg, trigger),
        )
        publish_sync_status(
            {
                "outcome": result.get("outcome"),
                "failed_count": result.get("failed_count", 0),
            }
        )
        return True
    except Exception as e:
        log(f"ERROR: 同步周期异常终止：{e}")
        notify_event(
            cfg.get("notifications") or {},
            "backup_failure",
            {
                "upload_path": cfg.get("upload_path"),
                "trigger": trigger,
                "stage": "同步周期",
                "error": f"{type(e).__name__}: {e}",
            },
        )
        return False
    finally:
        publish_sync_status(
            {"running": False, "finished": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        )
        _sync_lock.release()


def _drain_deferred(client: BaiduClient, cfg: Dict[str, Any], max_runs: int = 2) -> None:
    """补跑因锁竞争而排队的触发（最多 *max_runs* 次，防止活锁）。"""
    for _ in range(max_runs):
        deferred = take_deferred_trigger()
        if not deferred:
            return
        log(f"补跑排队中的触发：{deferred}")
        _run_once(client, cfg, trigger=deferred)


def schedule_loop(
    client: BaiduClient,
    cfg: Dict[str, Any],
) -> None:
    """Main scheduling loop — wakes at every cron-matched minute, or on manual request."""
    log(f"Entering scheduled mode. Cron: {cfg['cron'].expr!r}")

    while True:
        now = datetime.now()
        target = cfg["cron"].next_fire(now)

        seconds_to_wait = (target - now).total_seconds()
        log(
            f"Next run: {target.strftime('%Y-%m-%d %H:%M')} "
            f"(in {seconds_to_wait / 3600:.2f}h)"
        )

        if _wait_until(target):
            log("收到 Web UI 手动同步请求，立即执行")
            _run_once(client, cfg, trigger="manual")
            _drain_deferred(client, cfg)
            continue

        log("Scheduled execution started")
        _run_once(client, cfg, trigger="schedule", wait_seconds=600.0)
        _drain_deferred(client, cfg)

        # 防止同一分钟内重入（next_fire 从 now+1min 起算，仍兜底 sleep 60s）
        time.sleep(60)


# ============================================================================
# Main
# ============================================================================
def main() -> None:
    """Application entry point."""
    log("=" * 50)
    log("Baidu Netdisk Backup Add-on v1.3.1 (OAuth 2.0)")
    log("Using AList-compatible authentication method")
    log("Mode: Sync ALL backups with notifications")
    log("=" * 50)

    cfg: Dict[str, Any] = load_config()

    if not cfg["refresh_token"]:
        log("=" * 50)
        log("ERROR: refresh_token not configured!")
        log("")
        log("How to get refresh_token:")
        log(
            "1. Visit: https://openapi.baidu.com/oauth/2.0/authorize"
            "?response_type=code"
            "&client_id=hq9yQ9w9kR4YHj1kyYafLygVocobh7Sf"
            "&redirect_uri=https://alistgo.com/tool/baidu/callback"
            "&scope=basic,netdisk"
            "&qrcode=1"
        )
        log("2. Login with your Baidu account")
        log("3. Copy the 'refresh_token' from the result page")
        log("4. Paste it into the add-on configuration")
        log("=" * 50)
        # Keep container alive to show the message
        while True:
            time.sleep(3600)

    client = init_client(cfg["refresh_token"])
    client.retry_deadline = float(cfg["retry_deadline"])

    def _reload_config() -> None:
        """Web UI 保存配置后被调用：优先使用 Web UI 刚提交的那份 dict。"""
        try:
            pending = take_pending_options()
            new_cfg = load_config(pending)
            for key in (
                "upload_path",
                "retention",
                "notifications",
                "cron",
                "recheck_days",
                "retry_deadline",
                "cycle_budget",
            ):
                cfg[key] = new_cfg[key]
            client.retry_deadline = float(new_cfg["retry_deadline"])
            if new_cfg["refresh_token"] and new_cfg["refresh_token"] != cfg["refresh_token"]:
                cfg["refresh_token"] = new_cfg["refresh_token"]
                log("refresh_token 已变更，将在下一次同步开始时重建客户端")
            log(
                f"配置已热加载（cron: {new_cfg['cron'].expr!r}，"
                f"来源：{'Web UI 提交' if pending is not None else 'options.json'}）"
            )
        except Exception as e:
            log(f"ERROR: 配置热加载失败: {e}")

    # Web UI（Ingress 通道）— 后台线程，失败不影响主流程
    start_web_server(port=8099)
    register_config_reload_callback(_reload_config)

    log("Running initial sync...")
    _run_once(client, cfg, trigger="startup")

    schedule_loop(client, cfg)


if __name__ == "__main__":
    main()
