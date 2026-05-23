#!/usr/bin/env python3
"""Sync local Home Assistant backup files (.tar) to Baidu Netdisk."""
import os
import glob
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from client import BaiduClient  # noqa: F401
else:
    BaiduClient = object

from client import log

BACKUP_DIR: str = "/backup"


def sync_all_backups(
    client: "BaiduClient",  # type: ignore[valid-type]
    upload_path: str,
) -> None:
    """Upload every ``.tar`` file in ``BACKUP_DIR`` to *upload_path*.

    Files already cached (by name + size + mtime) are skipped (Issue 12).
    """
    if not os.path.exists(BACKUP_DIR):
        log(f"Backup directory {BACKUP_DIR} does not exist.")
        return

    files = glob.glob(f"{BACKUP_DIR}/*.tar")
    if not files:
        log("No backups found in /backup directory.")
        return

    # Ensure remote target directory exists
    client.create_remote_dir(upload_path)

    # Oldest first (catch-up order)
    files.sort(key=os.path.getctime)

    log(f"Found {len(files)} backup files. Starting sync...")

    success_count = 0
    for local_path in files:
        try:
            # Issue 12: skip files already known to be uploaded
            if client._is_already_uploaded(local_path):
                log(f"Already uploaded (cached): {os.path.basename(local_path)}")
                success_count += 1
                continue

            if client.upload_file(local_path, upload_path):
                success_count += 1
        except Exception as e:
            log(f"Error syncing {os.path.basename(local_path)}: {e}")

    log(f"Sync completed. {success_count}/{len(files)} files synced.")