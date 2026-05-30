#!/usr/bin/env python3
"""Entry point & scheduling loop for the Baidu Netdisk Backup add-on."""
import json
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Tuple

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
from web import start_web_server

CONFIG_PATH: str = "/data/options.json"


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


def load_config() -> Tuple[str, str, Dict[str, Any], bool, CronSchedule, Dict[str, Any]]:
    """Read ``options.json`` and return parsed configuration.

    Returns:
        (refresh_token, upload_path, retention_dict, use_folders, cron, notifications)
    """
    try:
        with open(CONFIG_PATH, "r") as f:
            options: Dict[str, Any] = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        log("Config file not found, using defaults / env")
        options = {}

    refresh_token: str = options.get(
        "refresh_token", os.environ.get("REFRESH_TOKEN", "")
    )
    upload_path: str = options.get("upload_path", "/HomeAssistant/Backup")

    # 嵌套 retention 配置（v1.1.0 起仅支持嵌套写法）
    retention_raw = options.get("retention") or {}
    if not isinstance(retention_raw, dict):
        retention_raw = {}
    retention: Dict[str, Any] = {
        "daily": retention_raw.get("daily", 7),
        "weekly": retention_raw.get("weekly", 4),
        "monthly": retention_raw.get("monthly", 12),
    }
    retention_use_folders: bool = bool(retention_raw.get("use_folders", True))

    schedule_str: str = options.get("schedule", "0 5 * * *")
    cron = parse_cron(schedule_str)

    # 通知配置（v1.0.3）
    notifications: Dict[str, Any] = options.get("notifications", {})

    return refresh_token, upload_path, retention, retention_use_folders, cron, notifications


def init_client(refresh_token: str) -> BaiduClient:
    """Create a BaiduClient with error handling."""
    try:
        return BaiduClient(refresh_token)
    except Exception as e:
        log(f"Failed to initialize client: {e}")
        log("Please check your refresh_token and try again.")
        raise


def run_sync_cycle(
    client: BaiduClient,
    upload_path: str,
    retention: Dict[str, Any],
    retention_use_folders: bool,
    notifications: Dict[str, Any],
) -> None:
    """Execute one full synchronisation cycle with notification integration."""
    if retention_use_folders:
        daily_dir = _join_remote_dir(upload_path, "每日")
        sync_result = sync_all_backups(client, daily_dir)

        # 通知：备份成功 / 失败
        if sync_result["success"] and sync_result.get("success_count", 0) > 0:
            sync_result["upload_path"] = daily_dir
            notify_event(notifications, "backup_success", sync_result)
        elif sync_result.get("error"):
            sync_result["upload_path"] = daily_dir
            notify_event(notifications, "backup_failure", sync_result)

        try:
            # 迁移旧英文目录到新中文目录（仅首次执行）
            migrated = migrate_old_dirs(client, upload_path)
            for info in migrated:
                notify_event(notifications, "migration_done", info)
            retention_folder_mode(client, upload_path, retention)
        except Exception as e:
            log(f"Remote retention error: {e}")

        # 每次 retention 完成后生成备份清单文件
        try:
            manifest_info = generate_manifest(client, upload_path)
            if manifest_info:
                notify_event(notifications, "manifest_generated", manifest_info)
        except Exception as e:
            log(f"Generate manifest error: {e}")

        # 存储空间检查
        _check_storage_warning(client, notifications)
    else:
        sync_result = sync_all_backups(client, upload_path)

        # 通知：备份成功 / 失败
        if sync_result["success"] and sync_result.get("success_count", 0) > 0:
            sync_result["upload_path"] = upload_path
            notify_event(notifications, "backup_success", sync_result)
        elif sync_result.get("error"):
            sync_result["upload_path"] = upload_path
            notify_event(notifications, "backup_failure", sync_result)

        try:
            cleanup_remote_backups(client, upload_path, retention)
        except Exception as e:
            log(f"Remote retention error: {e}")

        # 存储空间检查
        _check_storage_warning(client, notifications)


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


def schedule_loop(
    client: BaiduClient,
    upload_path: str,
    retention: Dict[str, Any],
    retention_use_folders: bool,
    cron: CronSchedule,
    notifications: Dict[str, Any],
) -> None:
    """Main scheduling loop — wakes at every cron-matched minute."""
    log(f"Entering scheduled mode. Cron: {cron.expr!r}")

    while True:
        now = datetime.now()
        target = cron.next_fire(now)

        seconds_to_wait = (target - now).total_seconds()
        log(
            f"Next run: {target.strftime('%Y-%m-%d %H:%M')} "
            f"(in {seconds_to_wait / 3600:.2f}h)"
        )
        time.sleep(max(seconds_to_wait, 1))

        log("Scheduled execution started")
        try:
            run_sync_cycle(client, upload_path, retention, retention_use_folders, notifications)
        except Exception as e:
            log(f"Scheduled upload error: {e}")

        # 防止同一分钟内重入（next_fire 从 now+1min 起算，仍兜底 sleep 60s）
        time.sleep(60)


# ============================================================================
# Main
# ============================================================================
def main() -> None:
    """Application entry point."""
    log("=" * 50)
    log("Baidu Netdisk Backup Add-on v1.2.7 (OAuth 2.0)")
    log("Using AList-compatible authentication method")
    log("Mode: Sync ALL backups with notifications")
    log("=" * 50)

    refresh_token, upload_path, retention, retention_use_folders, cron, notifications = (
        load_config()
    )

    if not refresh_token:
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

    client = init_client(refresh_token)

    # Web UI（Ingress 通道）— 后台线程，失败不影响主流程
    start_web_server(port=8099)

    log("Running initial sync...")
    run_sync_cycle(client, upload_path, retention, retention_use_folders, notifications)

    schedule_loop(client, upload_path, retention, retention_use_folders, cron, notifications)


if __name__ == "__main__":
    main()
