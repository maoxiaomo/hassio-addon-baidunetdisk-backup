#!/usr/bin/env python3
"""Sync local Home Assistant backup files (.tar) to Baidu Netdisk."""
import os
import glob
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from client import BaiduClient  # noqa: F401
else:
    BaiduClient = object

from client import log

BACKUP_DIR: str = "/backup"


def sync_all_backups(
    client: "BaiduClient",  # type: ignore[valid-type]
    upload_path: str,
) -> Dict[str, Any]:
    """Upload every ``.tar`` file in ``BACKUP_DIR`` to *upload_path*.

    Files already cached (by name + size + mtime) are skipped (Issue 12).

    Returns:
        {
            "success": bool,          # 整体是否成功（有文件上传成功即为 True）
            "success_count": int,     # 成功上传数（含缓存命中）
            "total_count": int,       # 文件总数
            "skipped_count": int,     # 跳过的文件数（已缓存）
            "error": str | None,      # 如有致命错误，返回错误信息
        }
    """
    result: Dict[str, Any] = {
        "success": False,
        "success_count": 0,
        "total_count": 0,
        "skipped_count": 0,
        "error": None,
    }

    if not os.path.exists(BACKUP_DIR):
        msg = f"Backup directory {BACKUP_DIR} does not exist."
        log(msg)
        result["error"] = msg
        return result

    files = glob.glob(f"{BACKUP_DIR}/*.tar")
    result["total_count"] = len(files)
    if not files:
        log("No backups found in /backup directory.")
        result["success"] = True
        return result

    # Ensure remote target directory exists
    client.create_remote_dir(upload_path)

    # Oldest first (catch-up order)
    files.sort(key=os.path.getctime)

    log(f"Found {len(files)} backup files. Starting sync...")

    success_count = 0
    skipped_count = 0
    for local_path in files:
        try:
            # Issue 12: skip files already known to be uploaded
            if client._is_already_uploaded(local_path):
                log(f"Already uploaded (cached): {os.path.basename(local_path)}")
                success_count += 1
                skipped_count += 1
                continue

            if client.upload_file(local_path, upload_path):
                success_count += 1
        except Exception as e:
            log(f"Error syncing {os.path.basename(local_path)}: {e}")

    result["success_count"] = success_count
    result["skipped_count"] = skipped_count
    result["success"] = success_count > 0
    log(f"Sync completed. {success_count}/{len(files)} files synced.")
    return result