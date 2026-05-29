#!/usr/bin/env python3
"""Baidu Netdisk OAuth 2.0 Client — upload, list, delete, move, directory management."""
import json
import os
import time
import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

# ============================================================================
# Module-level constants (Issue 9: extract hard-coded values)
# ============================================================================
CHUNK_SIZE: int = 4 * 1024 * 1024       # 4 MB upload chunk
DEFAULT_TIMEOUT: int = 30               # seconds for most API calls
UPLOAD_TIMEOUT: int = 60                # seconds for precreate / create / batch ops
LONG_TIMEOUT: int = 300                 # seconds for single-chunk upload
MAX_RETRIES: int = 3                    # retries for chunk upload & token refresh
BATCH_SIZE: int = 100                   # max files per batch filemanager call
LIST_LIMIT: int = 1000                  # max items per list page
RETRY_DELAY: float = 3.0                # base delay between retries (seconds)
TIME_FORMAT: str = "%Y-%m-%d %H:%M:%S"  # cached log timestamp format (Issue 16)

# AList's Client Credentials  (Public, widely used)
CLIENT_ID: str = "hq9yQ9w9kR4YHj1kyYafLygVocobh7Sf"
CLIENT_SECRET: str = "YH2VpZcFJHYNnV6vLfHQXDBhcE7ZChyE"
REDIRECT_URI: str = "https://alistgo.com/tool/baidu/callback"

TOKEN_FILE: str = "/data/baidu_token.json"
UPLOAD_CACHE_FILE: str = "/data/upload_cache.json"


# ============================================================================
# Logger
# ============================================================================
def log(msg: str) -> None:
    """Print a timestamped log message and flush immediately."""
    print(f"[{datetime.now().strftime(TIME_FORMAT)}] {msg}", flush=True)


# ============================================================================
# Baidu OAuth 2.0 Client
# ============================================================================
class BaiduClient:
    """Uses OAuth 2.0 access_token — official API, same method as AList."""

    def __init__(self, refresh_token: str) -> None:
        self.initial_refresh_token: str = refresh_token
        self.refresh_token: str = refresh_token
        self.access_token: Optional[str] = None
        self.token_expires: float = 0.0

        # Upload dedup cache  (Issue 12: skip already-uploaded files)
        self._upload_cache: Dict[str, bool] = {}
        self._load_upload_cache()

        # Restore cached token
        self._load_cached_token()

        # Refresh if needed
        if not self.access_token or time.time() >= self.token_expires - 600:
            self._refresh_access_token()

        if not self.access_token:
            raise Exception(
                "Failed to obtain access_token. Please check your refresh_token."
            )

        log(f"BaiduClient initialized. Token: {self.access_token[:10]}...")

    # ------------------------------------------------------------------
    # Token persistence
    # ------------------------------------------------------------------
    def _load_cached_token(self) -> None:
        """Load token from cache file; ignore cache when config has changed."""
        try:
            if os.path.exists(TOKEN_FILE):
                with open(TOKEN_FILE, "r") as f:
                    data: Dict[str, Any] = json.load(f)

                # Issue 11: guard against empty-string cached_source
                cached_source = data.get("source_refresh_token", "")
                if (
                    self.initial_refresh_token
                    and cached_source is not None
                    and cached_source != ""
                    and self.initial_refresh_token != cached_source
                ):
                    log(
                        "Configuration changed! Ignoring cache and using new refresh_token."
                    )
                    return

                self.access_token = data.get("access_token")
                self.token_expires = data.get("expires_at", 0)
                if data.get("refresh_token"):
                    self.refresh_token = data["refresh_token"]
                log("Loaded cached token.")
        except Exception as e:
            log(f"Failed to load cached token: {e}")

    def _save_cached_token(self) -> None:
        """Persist token to cache file."""
        try:
            os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
            with open(TOKEN_FILE, "w") as f:
                json.dump(
                    {
                        "access_token": self.access_token,
                        "refresh_token": self.refresh_token,
                        "expires_at": self.token_expires,
                        "source_refresh_token": self.initial_refresh_token,
                    },
                    f,
                )
        except Exception as e:
            log(f"Failed to save token: {e}")

    # ------------------------------------------------------------------
    # Upload dedup cache  (Issue 12)
    # ------------------------------------------------------------------
    def _load_upload_cache(self) -> None:
        try:
            if os.path.exists(UPLOAD_CACHE_FILE):
                with open(UPLOAD_CACHE_FILE, "r") as f:
                    self._upload_cache = json.load(f)
        except Exception:
            self._upload_cache = {}

    def _save_upload_cache(self) -> None:
        try:
            os.makedirs(os.path.dirname(UPLOAD_CACHE_FILE), exist_ok=True)
            with open(UPLOAD_CACHE_FILE, "w") as f:
                json.dump(self._upload_cache, f)
        except Exception:
            pass

    def _is_already_uploaded(self, local_path: str) -> bool:
        """Check whether *local_path* (by name + size + mtime) was already uploaded."""
        try:
            stat = os.stat(local_path)
            key = f"{os.path.basename(local_path)}:{stat.st_size}:{int(stat.st_mtime)}"
            return self._upload_cache.get(key, False)
        except Exception:
            return False

    def _mark_uploaded(self, local_path: str) -> None:
        """Record a successful upload in the dedup cache."""
        try:
            stat = os.stat(local_path)
            key = f"{os.path.basename(local_path)}:{stat.st_size}:{int(stat.st_mtime)}"
            self._upload_cache[key] = True
            self._save_upload_cache()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Token management  (Issue 8: 3-retry for token refresh)
    # ------------------------------------------------------------------
    def _refresh_access_token(self) -> None:
        """Use refresh_token to obtain a new access_token (with retries)."""
        log("Refreshing access_token...")
        url = "https://openapi.baidu.com/oauth/2.0/token"
        params = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }
        headers: Dict[str, str] = {"User-Agent": "pan.baidu.com"}

        last_error: Optional[str] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.get(
                    url, params=params, headers=headers, timeout=DEFAULT_TIMEOUT
                )
                data = resp.json()

                if "access_token" in data:
                    self.access_token = data["access_token"]
                    expires_in = data.get("expires_in", 2592000)
                    self.token_expires = time.time() + expires_in
                    if "refresh_token" in data:
                        self.refresh_token = data["refresh_token"]
                    self._save_cached_token()
                    log(
                        f"Token refreshed. Expires in {expires_in / 86400:.1f} days."
                    )
                    return

                last_error = data.get(
                    "error_description", data.get("error", "unknown error")
                )
                if any(
                    kw in str(last_error)
                    for kw in ("expired", "used", "invalid")
                ):
                    log("=" * 50)
                    log(
                        "CRITICAL ERROR: Your Refresh Token has expired or is invalid!"
                    )
                    log(
                        "Reason: The token may have been used already (tokens are one-time use)."
                    )
                    log(
                        "ACTION REQUIRED: Please get a NEW refresh_token and update your config."
                    )
                    log("=" * 50)
                    raise Exception(f"Token refresh failed: {last_error}")

                # Non-permanent error → retry
                if attempt < MAX_RETRIES:
                    log(
                        f"Token refresh attempt {attempt} failed: {last_error}. Retrying..."
                    )
                    time.sleep(RETRY_DELAY * attempt)

            except requests.RequestException as e:
                last_error = str(e)
                if attempt < MAX_RETRIES:
                    log(
                        f"Token refresh attempt {attempt} network error: {e}. Retrying..."
                    )
                    time.sleep(RETRY_DELAY * attempt)
                else:
                    log(
                        f"Network error refreshing token after {MAX_RETRIES} attempts: {e}"
                    )
                    raise

        raise Exception(
            f"Token refresh failed after {MAX_RETRIES} attempts: {last_error}"
        )

    def _ensure_token(self) -> None:
        """Ensure access_token is valid (refresh if near expiry)."""
        if time.time() >= self.token_expires - 600:
            self._refresh_access_token()

    # ------------------------------------------------------------------
    # Quota
    # ------------------------------------------------------------------
    def get_quota(self) -> Optional[Dict[str, Any]]:
        """获取网盘容量信息。返回 {total, used, free, expire} (bytes)；失败返回 None。"""
        self._ensure_token()
        url = "https://pan.baidu.com/api/quota"
        params = {
            "access_token": self.access_token,
            "checkfree": 1,
            "checkexpire": 1,
        }
        headers = {"User-Agent": "pan.baidu.com"}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=DEFAULT_TIMEOUT)
            data = r.json()
            if data.get("errno", 0) != 0:
                log(f"获取容量失败：{data}")
                return None
            return {
                "total": int(data.get("total", 0)),
                "used": int(data.get("used", 0)),
                "free": int(data.get("free", 0)),
                "expire": bool(data.get("expire", False)),
            }
        except Exception as e:
            log(f"获取容量异常：{e}")
            return None

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------
    def upload_file(self, local_path: str, remote_dir: str) -> bool:
        """Upload a single file to *remote_dir* using the official xpan API."""
        self._ensure_token()

        filename = os.path.basename(local_path)
        if remote_dir.endswith("/"):
            remote_dir = remote_dir[:-1]
        if not remote_dir.startswith("/"):
            remote_dir = "/" + remote_dir

        full_remote_path = f"{remote_dir}/{filename}"
        log(f"Uploading: {filename} -> {full_remote_path}")

        try:
            if self._do_upload_sliced(local_path, full_remote_path):
                self._mark_uploaded(local_path)  # Issue 12: cache success
                log(f"Upload SUCCESS: {filename}")
                return True
            else:
                log(f"Upload FAILED: {filename}")
                return False
        except Exception as e:
            log(f"Upload error: {e}")
            return False

    def _do_upload_sliced(self, local_path: str, full_remote_path: str) -> bool:
        """Sliced upload: precreate → upload chunks → merge."""
        headers: Dict[str, str] = {"User-Agent": "pan.baidu.com"}
        file_size: int = os.path.getsize(local_path)

        # Step 1 — block MD5s
        log("Calculating block MD5s...")
        block_list: List[str] = []
        with open(local_path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                block_list.append(hashlib.md5(chunk).hexdigest())

        log(f"File size: {file_size / 1024 / 1024:.1f} MB, Blocks: {len(block_list)}")

        # Step 2 — precreate
        log("Step 1/3: Precreate...")
        precreate_url = (
            f"https://pan.baidu.com/rest/2.0/xpan/file"
            f"?method=precreate&access_token={self.access_token}"
        )
        precreate_data = {
            "path": full_remote_path,
            "size": str(file_size),
            "isdir": "0",
            "autoinit": "1",
            "block_list": json.dumps(block_list),
            "rtype": "3",
        }
        resp = requests.post(
            precreate_url, data=precreate_data, headers=headers, timeout=UPLOAD_TIMEOUT
        )
        pre_json = resp.json()

        if pre_json.get("errno") != 0:
            log(f"Precreate failed: {pre_json}")
            return False

        uploadid = pre_json.get("uploadid")
        return_type = pre_json.get("return_type")

        if return_type == 2:
            log("Rapid upload (秒传) successful! File already exists on server.")
            return True

        log(f"Precreate OK. return_type={return_type}, UploadID: {uploadid}")

        # Step 3 — upload chunks
        log("Step 2/3: Uploading chunks...")
        with open(local_path, "rb") as f:
            for i in range(len(block_list)):
                chunk = f.read(CHUNK_SIZE)

                for attempt in range(MAX_RETRIES):
                    try:
                        progress = ((i + 1) / len(block_list)) * 100
                        if i % 5 == 0 or attempt > 0:
                            log(
                                f"  Chunk {i + 1}/{len(block_list)} ({progress:.0f}%)"
                                + (
                                    f" retry {attempt + 1}"
                                    if attempt > 0
                                    else ""
                                )
                            )

                        upload_url = (
                            f"https://d.pcs.baidu.com/rest/2.0/pcs/superfile2"
                            f"?method=upload&access_token={self.access_token}"
                            f"&type=tmpfile&path={requests.utils.quote(full_remote_path)}"
                            f"&uploadid={uploadid}&partseq={i}"
                        )
                        files = {
                            "file": ("blob", chunk, "application/octet-stream")
                        }
                        r = requests.post(
                            upload_url,
                            files=files,
                            headers=headers,
                            timeout=LONG_TIMEOUT,
                        )

                        # Issue 10: save json result before checking
                        data = r.json()
                        if r.status_code == 200 and "md5" in data:
                            break
                        else:
                            log(f"  Chunk {i} response: {r.text[:100]}")
                    except Exception as e:
                        log(f"  Chunk {i} error: {e}")

                    time.sleep(RETRY_DELAY)
                else:
                    log(f"Failed to upload chunk {i} after {MAX_RETRIES} retries")
                    return False

        # Step 4 — merge
        log("Step 3/3: Merging...")
        create_url = (
            f"https://pan.baidu.com/rest/2.0/xpan/file"
            f"?method=create&access_token={self.access_token}"
        )
        create_data = {
            "path": full_remote_path,
            "size": str(file_size),
            "isdir": "0",
            "block_list": json.dumps(block_list),
            "uploadid": uploadid,
            "rtype": "3",
        }
        resp = requests.post(
            create_url, data=create_data, headers=headers, timeout=UPLOAD_TIMEOUT
        )
        result = resp.json()

        if result.get("errno") == 0:
            log(f"Merge OK. File ID: {result.get('fs_id')}")
            return True
        else:
            log(f"Merge failed: {result}")
            return False

    # ------------------------------------------------------------------
    # Remote file listing
    # ------------------------------------------------------------------
    def list_remote_files(self, remote_dir: str) -> List[Dict[str, Any]]:
        """List files in *remote_dir* (paginated)."""
        self._ensure_token()
        log(f"Listing files in remote dir: {remote_dir}")
        url = "https://pan.baidu.com/rest/2.0/xpan/file"

        all_items: List[Dict[str, Any]] = []
        start: int = 0
        while True:
            params = {
                "method": "list",
                "access_token": self.access_token,
                "dir": remote_dir,
                "limit": LIST_LIMIT,
                "start": start,
            }
            try:
                resp = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
                data = resp.json()
            except Exception as e:
                log(f"Error listing remote files: {e}")
                break

            if data.get("errno") != 0:
                log(f"Failed to list remote files: {data}")
                break

            items: List[Dict[str, Any]] = data.get("list", [])
            all_items.extend(items)

            # Issue 13: guard against missing has_more (None → don't loop forever)
            has_more = data.get("has_more")
            if has_more is not True:
                break

            if len(items) < LIST_LIMIT:
                break

            start += len(items)

        log(f"--- Remote Directory Content ({len(all_items)} files) ---")
        for f in all_items:
            tag = "DIR" if f.get("isdir") == 1 else "FILE"
            log(
                f"[{tag}] {f.get('server_filename')} ({f.get('size')} bytes)"
            )
        log("------------------------------------------------")
        return all_items

    # ------------------------------------------------------------------
    # Batch filemanager  (Issue 6: deduplicate delete / move)
    # ------------------------------------------------------------------
    def _batch_filemanager(
        self,
        remote_paths: List[Any],
        opera: str,
        action_name: str,
    ) -> bool:
        """Generic batched filemanager call (delete / move / copy etc.)."""
        self._ensure_token()
        if not remote_paths:
            return True

        url = "https://pan.baidu.com/rest/2.0/xpan/file"
        params = {
            "method": "filemanager",
            "access_token": self.access_token,
            "opera": opera,
        }
        headers: Dict[str, str] = {"User-Agent": "pan.baidu.com"}

        ok = True
        for i in range(0, len(remote_paths), BATCH_SIZE):
            batch = remote_paths[i : i + BATCH_SIZE]
            form_data = {"async": "0", "filelist": json.dumps(batch)}
            try:
                resp = requests.post(
                    url,
                    params=params,
                    data=form_data,
                    headers=headers,
                    timeout=UPLOAD_TIMEOUT,
                )
                data = resp.json()
                if data.get("errno") == 0:
                    log(f"{action_name} {len(batch)} remote files")
                else:
                    ok = False
                    log(
                        f"Failed to {action_name.lower()} remote files: {data}"
                    )
            except Exception as e:
                ok = False
                log(f"Error {action_name.lower()} remote files: {e}")
        return ok

    def delete_remote_files(self, remote_paths: List[str]) -> bool:
        """Delete *remote_paths* on Baidu Netdisk (batched)."""
        return self._batch_filemanager(remote_paths, "delete", "Deleted")

    def move_remote_files(self, moves: List[Dict[str, str]]) -> bool:
        """Move files on Baidu Netdisk (batched).  *moves*: list of
        ``{"path": src, "dest": dst_dir, "ondup": "overwrite"}``."""
        return self._batch_filemanager(moves, "move", "Moved")

    # ------------------------------------------------------------------
    # Directory management
    # ------------------------------------------------------------------
    def create_remote_dir(self, remote_dir: str) -> bool:
        """Explicitly create *remote_dir* (AList-compatible)."""
        self._ensure_token()
        log(f"Ensuring remote directory exists: {remote_dir}")

        url = "https://pan.baidu.com/rest/2.0/xpan/file"
        params = {"method": "create", "access_token": self.access_token}
        headers: Dict[str, str] = {"User-Agent": "pan.baidu.com"}
        form_data = {
            "path": remote_dir,
            "isdir": "1",
            "size": "0",
            "rtype": "3",
        }

        try:
            resp = requests.post(
                url,
                params=params,
                data=form_data,
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
            )
            res = resp.json()
            log(f"Create dir response: {res}")

            if res.get("errno") == 0:
                log(f"Directory created: {remote_dir}")
                return True
            elif res.get("errno") == -8:
                log(f"Directory already exists: {remote_dir}")
                return True
            elif "path" in res or "fs_id" in res or "category" in res:
                log(f"Directory exists (non-standard response): {remote_dir}")
                return True
            else:
                log(f"Failed to create directory: {res}")
                return False
        except Exception as e:
            log(f"Error creating directory: {e}")
            return False