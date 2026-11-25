#!/usr/bin/env python3
"""Sync local Home Assistant backup files (.tar) to Baidu Netdisk."""
import os
import glob
import time
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from client import BaiduClient  # noqa: F401
else:
    BaiduClient = object

from client import log

BACKUP_DIR: str = "/backup"


def sync_all_backups(
    client: "BaiduClient",  # type: ignore[valid-type]
    upload_path: str,
    deadline: Optional[float] = None,
    recheck_days: float = 7.0,
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
        "outcome": "failed",      # ok | partial | failed | empty
        "success_count": 0,       # = uploaded_count + skipped_count（兼容旧字段）
        "uploaded_count": 0,      # 本次真正上传成功
        "skipped_count": 0,       # 缓存命中 / 远端已确认
        "failed_count": 0,        # 失败 + 预算耗尽未尝试
        "total_count": 0,         # 本地扫描到的 .tar 数量
        "failed_files": [],       # [{"name": str, "error": str}]
        "warnings": [],
        "budget_exhausted": False,
        "error": None,
    }

    if not os.path.isdir(BACKUP_DIR):
        msg = f"备份目录 {BACKUP_DIR} 不存在或不可读"
        log(f"ERROR: {msg}")
        result["error"] = msg
        return result

    try:
        files = glob.glob(os.path.join(BACKUP_DIR, "*.tar"))
        files.sort(key=os.path.getctime)
    except OSError as e:
        msg = f"扫描 {BACKUP_DIR} 失败：{e}"
        log(f"ERROR: {msg}")
        result["error"] = msg
        return result

    result["total_count"] = len(files)
    if not files:
        log(f"WARNING: {BACKUP_DIR} 中没有任何 .tar 备份文件")
        result["outcome"] = "empty"
        result["success"] = False        # 空目录既不是成功也不是失败
        return result

    log(f"Found {len(files)} backup files. Starting sync...")

    dir_ready = False
    for idx, local_path in enumerate(files):
        name = os.path.basename(local_path)

        if deadline is not None and time.monotonic() >= deadline:
            pending = [os.path.basename(p) for p in files[idx:]]
            result["budget_exhausted"] = True
            for pending_name in pending:
                result["failed_files"].append(
                    {"name": pending_name, "error": "同步时间预算耗尽，本次未尝试"}
                )
            result["failed_count"] += len(pending)
            log(f"ERROR: 时间预算耗尽，{len(pending)} 个文件未尝试：{', '.join(pending)}")
            break

        try:
            need, reason = client.needs_upload(local_path, upload_path, recheck_days)
            if not need:
                result["skipped_count"] += 1
                log(f"跳过（{reason}）：{name}")
                continue

            if not dir_ready:
                if not client.create_remote_dir(upload_path):
                    msg = f"无法确保远端目录存在：{upload_path}（仍会尝试上传）"
                    log(f"ERROR: {msg}")
                    result["warnings"].append(msg)
                dir_ready = True

            client.upload_file(local_path, upload_path, deadline=deadline)
            result["uploaded_count"] += 1
            log(f"本次上传成功（{reason}）：{name}")
        except Exception as e:      # noqa: BLE001 — 单文件失败不影响其余文件
            result["failed_count"] += 1
            result["failed_files"].append({"name": name, "error": str(e)})
            log(f"ERROR: 同步失败 {name}：{e}")

    result["success_count"] = result["uploaded_count"] + result["skipped_count"]
    if result["failed_count"] == 0:
        result["outcome"] = "ok"
        result["success"] = True
    else:
        result["outcome"] = "failed" if result["success_count"] == 0 else "partial"
        result["success"] = False
        detail = "；".join(
            f"{f['name']}: {f['error']}" for f in result["failed_files"][:5]
        )
        more = len(result["failed_files"]) - 5
        if more > 0:
            detail += f"；……另有 {more} 个"
        result["error"] = (
            f"{result['failed_count']}/{result['total_count']} 个文件同步失败：{detail}"
        )

    log(
        f"Sync finished. outcome={result['outcome']} "
        f"扫描 {result['total_count']} / 上传 {result['uploaded_count']} / "
        f"跳过 {result['skipped_count']} / 失败 {result['failed_count']}"
    )
    return result