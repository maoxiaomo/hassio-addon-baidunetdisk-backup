#!/usr/bin/env python3
"""Entry point & scheduling loop for the Baidu Netdisk Backup add-on."""
import json
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Tuple

from client import BaiduClient, log
from retention import (
    cleanup_remote_backups,
    retention_folder_mode,
    _join_remote_dir,
)
from sync import sync_all_backups

CONFIG_PATH: str = "/data/options.json"


# ============================================================================
# Config & helpers  (Issue 14: extracted from main())
# ============================================================================
def parse_schedule_hour(schedule_str: str) -> int:
    """Extract the hour from a cron-like schedule string.

    Issue 3 fix: narrow ``except`` to expected types only.
    """
    try:
        parts = schedule_str.split()
        if len(parts) >= 2:
            return int(parts[1])
    except (ValueError, IndexError, AttributeError):
        pass
    return 3  # default: 3 AM


def load_config() -> Tuple[str, str, Dict[str, Any], bool, int]:
    """Read ``options.json`` and return parsed configuration.

    Returns:
        (refresh_token, upload_path, retention_dict, use_folders, target_hour)
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

    # Support both nested and flat retention config (Issue 7 / 20)
    retention = options.get("retention")
    if retention is None:
        retention = {
            "daily": options.get("retention_daily", 0),
            "weekly": options.get("retention_weekly", 0),
            "monthly": options.get("retention_monthly", 0),
        }
    else:
        retention = dict(retention)

    retention_use_folders: bool = bool(
        (isinstance(retention, dict) and retention.get("use_folders"))
        or options.get("retention_use_folders")
    )

    schedule_str: str = options.get("schedule", "0 5 * * *")
    target_hour: int = parse_schedule_hour(schedule_str)

    return refresh_token, upload_path, retention, retention_use_folders, target_hour


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
) -> None:
    """Execute one full synchronisation cycle."""
    if retention_use_folders:
        daily_dir = _join_remote_dir(upload_path, "daily")
        sync_all_backups(client, daily_dir)
        try:
            retention_folder_mode(client, upload_path, retention)
        except Exception as e:
            log(f"Remote retention error: {e}")
    else:
        sync_all_backups(client, upload_path)
        try:
            cleanup_remote_backups(client, upload_path, retention)
        except Exception as e:
            log(f"Remote retention error: {e}")


def schedule_loop(
    client: BaiduClient,
    upload_path: str,
    retention: Dict[str, Any],
    retention_use_folders: bool,
    target_hour: int,
) -> None:
    """Main scheduling loop — wakes every day at *target_hour*."""
    log(f"Entering scheduled mode. Target hour: {target_hour:02d}:00")

    while True:
        now = datetime.now()
        target = now.replace(
            hour=target_hour, minute=0, second=0, microsecond=0
        )
        if target <= now:
            target += timedelta(days=1)

        seconds_to_wait = (target - now).total_seconds()
        log(
            f"Next run: {target.strftime('%Y-%m-%d %H:%M')} "
            f"(in {seconds_to_wait / 3600:.1f}h)"
        )
        time.sleep(seconds_to_wait)

        log("Scheduled execution started")
        try:
            client._ensure_token()
            run_sync_cycle(client, upload_path, retention, retention_use_folders)
        except Exception as e:
            log(f"Scheduled upload error: {e}")

        time.sleep(60)


# ============================================================================
# Main
# ============================================================================
def main() -> None:
    """Application entry point."""
    log("=" * 50)
    log("Baidu Netdisk Backup Add-on v1.0.2 (OAuth 2.0)")
    log("Using AList-compatible authentication method")
    log("Mode: Sync ALL backups")
    log("=" * 50)

    refresh_token, upload_path, retention, retention_use_folders, target_hour = (
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

    log("Running initial sync...")
    run_sync_cycle(client, upload_path, retention, retention_use_folders)

    schedule_loop(client, upload_path, retention, retention_use_folders, target_hour)


if __name__ == "__main__":
    main()