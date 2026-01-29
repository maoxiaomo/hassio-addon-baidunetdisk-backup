#!/usr/bin/env python3
"""
Baidu Netdisk Backup Add-on for Home Assistant
Uses OAuth 2.0 (Same as AList) - The ONLY reliable method
"""
import os
import json
import time
import sys
import glob
import requests
import hashlib
import re
from datetime import datetime, timedelta

# === Configuration ===
CONFIG_PATH = "/data/options.json"
BACKUP_DIR = "/backup"
TOKEN_FILE = "/data/baidu_token.json"

# AList's Client Credentials (Public, widely used)
CLIENT_ID = "hq9yQ9w9kR4YHj1kyYafLygVocobh7Sf"
CLIENT_SECRET = "YH2VpZcFJHYNnV6vLfHQXDBhcE7ZChyE"
REDIRECT_URI = "https://alistgo.com/tool/baidu/callback"

# Logger
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

# === Baidu OAuth 2.0 Client ===
class BaiduClient:
    """
    Uses OAuth 2.0 access_token - Official API, stable and reliable.
    Same method as AList project.
    """
    
    def __init__(self, refresh_token):
        self.initial_refresh_token = refresh_token
        self.refresh_token = refresh_token
        self.access_token = None
        self.token_expires = 0
        
        # Try to load cached token
        self._load_cached_token()
        
        # Refresh if needed
        if not self.access_token or time.time() >= self.token_expires - 600:
            self._refresh_access_token()
        
        if not self.access_token:
            raise Exception("Failed to obtain access_token. Please check your refresh_token.")
        
        log(f"BaiduClient initialized. Token: {self.access_token[:10]}...")
    
    def _load_cached_token(self):
        """Load token from cache file, but prioritize config change"""
        try:
            if os.path.exists(TOKEN_FILE):
                with open(TOKEN_FILE, 'r') as f:
                    data = json.load(f)
                    
                    # Key Fix: If user changed config (initial_refresh_token), ignore cache!
                    cached_source = data.get("source_refresh_token", "")
                    if self.initial_refresh_token and cached_source and self.initial_refresh_token != cached_source:
                        log("Configuration changed! Ignoring cache and using new refresh_token.")
                        return 

                    self.access_token = data.get("access_token")
                    self.token_expires = data.get("expires_at", 0)
                    if data.get("refresh_token"):
                        self.refresh_token = data["refresh_token"]
                    log("Loaded cached token.")
        except Exception as e:
            log(f"Failed to load cached token: {e}")
    
    def _save_cached_token(self):
        """Save token to cache file"""
        try:
            os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
            with open(TOKEN_FILE, 'w') as f:
                json.dump({
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "expires_at": self.token_expires,
                    "source_refresh_token": self.initial_refresh_token # Save source to detect changes
                }, f)
        except Exception as e:
            log(f"Failed to save token: {e}")
    
    def _refresh_access_token(self):
        """Use refresh_token to get new access_token"""
        log("Refreshing access_token...")
        url = "https://openapi.baidu.com/oauth/2.0/token"
        params = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET
        }
        headers = {
            "User-Agent": "pan.baidu.com"
        }
        
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            data = resp.json()
            
            if "access_token" in data:
                self.access_token = data["access_token"]
                expires_in = data.get("expires_in", 2592000)  # Default 30 days
                self.token_expires = time.time() + expires_in
                
                # Update refresh_token if provided (it rotates)
                if "refresh_token" in data:
                    self.refresh_token = data["refresh_token"]
                
                self._save_cached_token()
                log(f"Token refreshed. Expires in {expires_in/86400:.1f} days.")
            else:
                log(f"Token refresh failed: {data}")
                err_msg = data.get('error_description', data.get('error', 'unknown error'))
                if 'expired' in str(err_msg) or 'used' in str(err_msg) or 'invalid' in str(err_msg):
                    log("=" * 50)
                    log("CRITICAL ERROR: Your Refresh Token has expired or is invalid!")
                    log("Reason: The token may have been used already (tokens are one-time use).")
                    log("ACTION REQUIRED: Please get a NEW refresh_token and update your config.")
                    log("=" * 50)
                raise Exception(f"Token refresh failed: {err_msg}")
        except requests.RequestException as e:
            log(f"Network error refreshing token: {e}")
            raise
    
    def _ensure_token(self):
        """Ensure we have a valid token"""
        if time.time() >= self.token_expires - 600:
            self._refresh_access_token()
    
    def upload_file(self, local_path, remote_dir):
        """Upload file using official xpan API with access_token"""
        self._ensure_token()
        
        filename = os.path.basename(local_path)
        if remote_dir.endswith("/"):
            remote_dir = remote_dir[:-1]
        
        # Ensure path starts with /
        if not remote_dir.startswith("/"):
            remote_dir = "/" + remote_dir
            
        full_remote_path = f"{remote_dir}/{filename}"
        log(f"Uploading: {filename} -> {full_remote_path}")
        
        try:
            if self._do_upload_sliced(local_path, full_remote_path):
                log(f"Upload SUCCESS: {filename}")
                return True
            else:
                log(f"Upload FAILED: {filename}")
                return False
        except Exception as e:
            log(f"Upload error: {e}")
            return False
    
    def _do_upload_sliced(self, local_path, full_remote_path):
        """Sliced upload: precreate -> upload chunks -> create"""
        CHUNK_SIZE = 4 * 1024 * 1024  # 4MB
        headers = {
            "User-Agent": "pan.baidu.com"
        }
        
        file_size = os.path.getsize(local_path)
        
        # Step 1: Calculate MD5 for each block
        log("Calculating block MD5s...")
        block_list = []
        with open(local_path, 'rb') as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                block_list.append(hashlib.md5(chunk).hexdigest())
        
        log(f"File size: {file_size/1024/1024:.1f}MB, Blocks: {len(block_list)}")
        
        # Step 2: Precreate
        log("Step 1/3: Precreate...")
        precreate_url = f"https://pan.baidu.com/rest/2.0/xpan/file?method=precreate&access_token={self.access_token}"
        precreate_data = {
            "path": full_remote_path,
            "size": str(file_size),
            "isdir": "0",
            "autoinit": "1",
            "block_list": json.dumps(block_list),
            "rtype": "3"  # 3=overwrite
        }
        
        resp = requests.post(precreate_url, data=precreate_data, headers=headers, timeout=60)
        pre_json = resp.json()
        
        if pre_json.get("errno") != 0:
            log(f"Precreate failed: {pre_json}")
            return False
        
        uploadid = pre_json.get("uploadid")
        return_type = pre_json.get("return_type")
        
        # return_type: 1 = need to upload, 2 = rapid upload success (file exists)
        if return_type == 2:
            log("Rapid upload (秒传) successful! File already exists on server.")
            return True
        
        log(f"Precreate OK. return_type={return_type}, UploadID: {uploadid}")
        
        # Step 3: Upload each chunk
        log("Step 2/3: Uploading chunks...")
        with open(local_path, 'rb') as f:
            for i in range(len(block_list)):
                chunk = f.read(CHUNK_SIZE)
                
                for attempt in range(3):
                    try:
                        progress = ((i + 1) / len(block_list)) * 100
                        if i % 5 == 0 or attempt > 0:
                            log(f"  Chunk {i+1}/{len(block_list)} ({progress:.0f}%)" + 
                                (f" retry {attempt+1}" if attempt > 0 else ""))
                        
                        upload_url = (
                            f"https://d.pcs.baidu.com/rest/2.0/pcs/superfile2"
                            f"?method=upload&access_token={self.access_token}"
                            f"&type=tmpfile&path={requests.utils.quote(full_remote_path)}"
                            f"&uploadid={uploadid}&partseq={i}"
                        )
                        
                        files = {"file": ("blob", chunk, "application/octet-stream")}
                        r = requests.post(upload_url, files=files, headers=headers, timeout=300)
                        
                        if r.status_code == 200 and "md5" in r.json():
                            break
                        else:
                            log(f"  Chunk {i} response: {r.text[:100]}")
                    except Exception as e:
                        log(f"  Chunk {i} error: {e}")
                    
                    time.sleep(3)
                else:
                    log(f"Failed to upload chunk {i} after 3 retries")
                    return False
        
        # Step 4: Create (merge)
        log("Step 3/3: Merging...")
        create_url = f"https://pan.baidu.com/rest/2.0/xpan/file?method=create&access_token={self.access_token}"
        create_data = {
            "path": full_remote_path,
            "size": str(file_size),
            "isdir": "0",
            "block_list": json.dumps(block_list),
            "uploadid": uploadid,
            "rtype": "3"
        }
        
        resp = requests.post(create_url, data=create_data, headers=headers, timeout=60)
        result = resp.json()
        
        if result.get("errno") == 0:
            log(f"Merge OK. File ID: {result.get('fs_id')}")
            return True
        else:
            log(f"Merge failed: {result}")
            return False

    def list_remote_files(self, remote_dir):
        """List files in remote directory for debugging"""
        self._ensure_token()
        log(f"Listing files in remote dir: {remote_dir}")
        url = "https://pan.baidu.com/rest/2.0/xpan/file"

        all_items = []
        start = 0
        limit = 1000
        while True:
            params = {
                "method": "list",
                "access_token": self.access_token,
                "dir": remote_dir,
                "limit": limit,
                "start": start
            }
            try:
                resp = requests.get(url, params=params, timeout=30)
                data = resp.json()
            except Exception as e:
                log(f"Error listing remote files: {e}")
                break

            if data.get("errno") != 0:
                log(f"Failed to list remote files: {data}")
                break

            items = data.get("list", [])
            all_items.extend(items)

            has_more = data.get("has_more")
            if has_more is False:
                break

            if len(items) < limit:
                break

            start += len(items)

        log(f"--- Remote Directory Content ({len(all_items)} files) ---")
        for f in all_items:
            log(f"[{'DIR' if f.get('isdir')==1 else 'FILE'}] {f.get('server_filename')} ({f.get('size')} bytes)")
        log("------------------------------------------------")
        return all_items

    def delete_remote_files(self, remote_paths):
        self._ensure_token()
        if not remote_paths:
            return True

        url = "https://pan.baidu.com/rest/2.0/xpan/file"
        params = {
            "method": "filemanager",
            "access_token": self.access_token,
            "opera": "delete"
        }
        headers = {
            "User-Agent": "pan.baidu.com"
        }

        ok = True
        batch_size = 100
        for i in range(0, len(remote_paths), batch_size):
            batch = remote_paths[i:i + batch_size]
            form_data = {
                "async": "0",
                "filelist": json.dumps(batch)
            }
            try:
                resp = requests.post(url, params=params, data=form_data, headers=headers, timeout=60)
                data = resp.json()
                if data.get("errno") == 0:
                    log(f"Deleted {len(batch)} remote files")
                else:
                    ok = False
                    log(f"Failed to delete remote files: {data}")
            except Exception as e:
                ok = False
                log(f"Error deleting remote files: {e}")
        return ok

    def move_remote_files(self, moves):
        self._ensure_token()
        if not moves:
            return True

        url = "https://pan.baidu.com/rest/2.0/xpan/file"
        params = {
            "method": "filemanager",
            "access_token": self.access_token,
            "opera": "move"
        }
        headers = {
            "User-Agent": "pan.baidu.com"
        }

        ok = True
        batch_size = 100
        for i in range(0, len(moves), batch_size):
            batch = moves[i:i + batch_size]
            form_data = {
                "async": "0",
                "filelist": json.dumps(batch)
            }
            try:
                resp = requests.post(url, params=params, data=form_data, headers=headers, timeout=60)
                data = resp.json()
                if data.get("errno") == 0:
                    log(f"Moved {len(batch)} remote files")
                else:
                    ok = False
                    log(f"Failed to move remote files: {data}")
            except Exception as e:
                ok = False
                log(f"Error moving remote files: {e}")
        return ok

    def create_remote_dir(self, remote_dir):
        """Explicitly create remote directory (AList compatible)"""
        self._ensure_token()
        log(f"Ensuring remote directory exists: {remote_dir}")
        # AList style: method and access_token as query params, form data for the rest
        url = "https://pan.baidu.com/rest/2.0/xpan/file"
        params = {
            "method": "create",
            "access_token": self.access_token
        }
        headers = {
            "User-Agent": "pan.baidu.com"
        }
        # rtype=3 is important - same as AList
        form_data = {
            "path": remote_dir,
            "isdir": "1",
            "size": "0",
            "rtype": "3"
        }
        try:
            resp = requests.post(url, params=params, data=form_data, headers=headers, timeout=30)
            res = resp.json()
            log(f"Create dir response: {res}")
            if res.get("errno") == 0:
                log(f"Directory created: {remote_dir}")
                return True
            elif res.get("errno") == -8: # File already exists
                log(f"Directory already exists: {remote_dir}")
                return True
            # Some environments return a non-standard payload without errno when directory exists.
            # Treat it as success if it resembles a directory metadata response.
            elif "path" in res or "fs_id" in res or "category" in res:
                log(f"Directory exists (non-standard response): {remote_dir}")
                return True
            else:
                log(f"Failed to create directory: {res}")
                return False
        except Exception as e:
            log(f"Error creating directory: {e}")
            return False

# === Main Logic ===
_BACKUP_TS_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})_(\d{2})\.(\d{2})_")


def _infer_backup_datetime(item):
    name = (item.get("server_filename") or "").strip()
    m = _BACKUP_TS_RE.search(name)
    if m:
        try:
            date_part = m.group(1)
            hour = int(m.group(2))
            minute = int(m.group(3))
            return datetime.strptime(date_part, "%Y-%m-%d").replace(hour=hour, minute=minute)
        except Exception:
            pass

    mtime = item.get("server_mtime") or item.get("server_ctime")
    if mtime:
        try:
            return datetime.fromtimestamp(int(mtime))
        except Exception:
            return None
    return None


def _infer_backup_timestamp(item):
    dt = _infer_backup_datetime(item)
    if not dt:
        return None
    return int(dt.timestamp())


def _compute_retention_keep_paths(remote_items, daily, weekly, monthly):
    files = []
    for item in remote_items:
        if item.get("isdir") == 1:
            continue
        name = item.get("server_filename") or ""
        if not name.endswith(".tar"):
            continue
        path = item.get("path")
        ts = _infer_backup_timestamp(item)
        if not path or not ts:
            continue
        files.append((path, int(ts)))

    files.sort(key=lambda x: x[1], reverse=True)

    keep = set()

    if daily and daily > 0:
        seen = set()
        for path, mtime in files:
            day = datetime.fromtimestamp(mtime).date()
            if day in seen:
                continue
            keep.add(path)
            seen.add(day)
            if len(seen) >= daily:
                break

    if weekly and weekly > 0:
        seen = set()
        for path, mtime in files:
            dt = datetime.fromtimestamp(mtime)
            key = dt.isocalendar().year, dt.isocalendar().week
            if key in seen:
                continue
            if path in keep:
                seen.add(key)
                if len(seen) >= weekly:
                    break
                continue
            keep.add(path)
            seen.add(key)
            if len(seen) >= weekly:
                break

    if monthly and monthly > 0:
        seen = set()
        for path, mtime in files:
            dt = datetime.fromtimestamp(mtime)
            key = dt.year, dt.month
            if key in seen:
                continue
            if path in keep:
                seen.add(key)
                if len(seen) >= monthly:
                    break
                continue
            keep.add(path)
            seen.add(key)
            if len(seen) >= monthly:
                break

    return keep


def _select_bucket_keep_paths(remote_items, bucket_key_fn, keep_count):
    files = []
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

    files.sort(key=lambda x: x[1], reverse=True)

    keep = set()
    seen = set()
    for path, dt in files:
        key = bucket_key_fn(dt)
        if key in seen:
            continue
        keep.add(path)
        seen.add(key)
        if keep_count and keep_count > 0 and len(seen) >= keep_count:
            break
    return keep


def cleanup_remote_backups(client, upload_path, retention):
    if not retention:
        return

    daily = int(retention.get("daily", 0) or 0)
    weekly = int(retention.get("weekly", 0) or 0)
    monthly = int(retention.get("monthly", 0) or 0)

    if daily <= 0 and weekly <= 0 and monthly <= 0:
        return

    remote_items = client.list_remote_files(upload_path) or []
    keep = _compute_retention_keep_paths(remote_items, daily=daily, weekly=weekly, monthly=monthly)
    candidates = []
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

    log(f"Remote retention: keeping {len(keep)} backups, deleting {len(candidates)} backups")
    client.delete_remote_files(candidates)


def _join_remote_dir(base_dir, name):
    if base_dir.endswith("/"):
        base_dir = base_dir[:-1]
    if not name:
        return base_dir
    if name.startswith("/"):
        name = name[1:]
    return f"{base_dir}/{name}"


def retention_folder_mode(client, base_upload_path, retention):
    daily_n = int(retention.get("daily", 0) or 0)
    weekly_n = int(retention.get("weekly", 0) or 0)
    monthly_n = int(retention.get("monthly", 0) or 0)

    daily_dir = _join_remote_dir(base_upload_path, "daily")
    weekly_dir = _join_remote_dir(base_upload_path, "weekly")
    monthly_dir = _join_remote_dir(base_upload_path, "monthly")

    client.create_remote_dir(base_upload_path)
    client.create_remote_dir(daily_dir)
    client.create_remote_dir(weekly_dir)
    client.create_remote_dir(monthly_dir)

    # Promotion phase
    daily_items = client.list_remote_files(daily_dir) or []
    weekly_items = client.list_remote_files(weekly_dir) or []
    monthly_items = client.list_remote_files(monthly_dir) or []

    weekly_keep_from_daily = _select_bucket_keep_paths(
        daily_items,
        bucket_key_fn=lambda dt: (dt.isocalendar().year, dt.isocalendar().week),
        keep_count=weekly_n,
    )
    monthly_keep_from_daily = _select_bucket_keep_paths(
        daily_items,
        bucket_key_fn=lambda dt: (dt.year, dt.month),
        keep_count=monthly_n,
    )

    existing_week_keys = set()
    for item in weekly_items:
        if item.get("isdir") == 1:
            continue
        dt = _infer_backup_datetime(item)
        if not dt:
            continue
        existing_week_keys.add((dt.isocalendar().year, dt.isocalendar().week))

    existing_month_keys = set()
    for item in monthly_items:
        if item.get("isdir") == 1:
            continue
        dt = _infer_backup_datetime(item)
        if not dt:
            continue
        existing_month_keys.add((dt.year, dt.month))

    path_to_item = {}
    for item in daily_items:
        path = item.get("path")
        if path:
            path_to_item[path] = item

    moves_to_weekly = []
    for path in weekly_keep_from_daily:
        item = path_to_item.get(path) or {}
        dt = _infer_backup_datetime(item)
        if not dt:
            continue
        key = (dt.isocalendar().year, dt.isocalendar().week)
        if key in existing_week_keys:
            continue
        moves_to_weekly.append({"path": path, "dest": weekly_dir, "ondup": "overwrite"})

    if moves_to_weekly:
        log(f"Retention folders: promoting {len(moves_to_weekly)} backups to weekly")
        client.move_remote_files(moves_to_weekly)

    # Refresh daily list after moves
    daily_items = client.list_remote_files(daily_dir) or []
    path_to_item = {}
    for item in daily_items:
        path = item.get("path")
        if path:
            path_to_item[path] = item

    moves_to_monthly = []
    for path in monthly_keep_from_daily:
        item = path_to_item.get(path) or {}
        dt = _infer_backup_datetime(item)
        if not dt:
            continue
        key = (dt.year, dt.month)
        if key in existing_month_keys:
            continue
        moves_to_monthly.append({"path": path, "dest": monthly_dir, "ondup": "overwrite"})

    if moves_to_monthly:
        log(f"Retention folders: promoting {len(moves_to_monthly)} backups to monthly")
        client.move_remote_files(moves_to_monthly)

    # Cleanup phase: enforce per-folder counts
    daily_items = client.list_remote_files(daily_dir) or []
    weekly_items = client.list_remote_files(weekly_dir) or []
    monthly_items = client.list_remote_files(monthly_dir) or []

    daily_keep = _select_bucket_keep_paths(
        daily_items,
        bucket_key_fn=lambda dt: dt.date(),
        keep_count=daily_n,
    )
    weekly_keep = _select_bucket_keep_paths(
        weekly_items,
        bucket_key_fn=lambda dt: (dt.isocalendar().year, dt.isocalendar().week),
        keep_count=weekly_n,
    )
    monthly_keep = _select_bucket_keep_paths(
        monthly_items,
        bucket_key_fn=lambda dt: (dt.year, dt.month),
        keep_count=monthly_n,
    )

    def _paths_to_delete(items, keep_set):
        out = []
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

    del_daily = _paths_to_delete(daily_items, daily_keep)
    del_weekly = _paths_to_delete(weekly_items, weekly_keep)
    del_monthly = _paths_to_delete(monthly_items, monthly_keep)

    if del_daily:
        log(f"Retention folders: deleting {len(del_daily)} backups from daily")
        client.delete_remote_files(del_daily)
    if del_weekly:
        log(f"Retention folders: deleting {len(del_weekly)} backups from weekly")
        client.delete_remote_files(del_weekly)
    if del_monthly:
        log(f"Retention folders: deleting {len(del_monthly)} backups from monthly")
        client.delete_remote_files(del_monthly)


def sync_all_backups(client, upload_path):
    """Sync all .tar files in BACKUP_DIR"""
    if not os.path.exists(BACKUP_DIR):
        log(f"Backup directory {BACKUP_DIR} does not exist.")
        return

    files = glob.glob(f"{BACKUP_DIR}/*.tar")
    if not files:
        log("No backups found in /backup directory.")
        return
    

    # Ensure upload directory exists FIRST
    client.create_remote_dir(upload_path)

    # Sort by time (oldest first, or newest first? Let's do oldest first to catch up)
    files.sort(key=os.path.getctime)
    
    log(f"Found {len(files)} backup files. Starting sync...")
    
    success_count = 0
    for local_path in files:
        try:
            if client.upload_file(local_path, upload_path):
                success_count += 1
        except Exception as e:
            log(f"Error syncing {os.path.basename(local_path)}: {e}")
            
    log(f"Sync completed. {success_count}/{len(files)} files synced.")

def parse_schedule_hour(schedule_str):
    try:
        parts = schedule_str.split()
        if len(parts) >= 2:
            return int(parts[1])
    except:
        pass
    return 3  # Default 3 AM

def main():
    log("=" * 50)
    log("Baidu Netdisk Backup Add-on v1.0.1 (OAuth 2.0)")
    log("Using AList-compatible authentication method")
    log("Mode: Sync ALL backups")
    log("=" * 50)
    
    # 1. Load Config
    try:
        with open(CONFIG_PATH, 'r') as f:
            options = json.load(f)
    except:
        log("Config file not found, using defaults/env")
        options = {}

    refresh_token = options.get("refresh_token", os.environ.get("REFRESH_TOKEN", ""))
    
    # CHANGE: Default path should not include /apps/ prefix as we are already inside the sandbox
    # Users should config: /HomeAssistant/Backup (which maps to /apps/AppName/HomeAssistant/Backup)
    upload_path = options.get("upload_path", "/HomeAssistant/Backup")

    retention = options.get("retention")
    if retention is None:
        retention = {
            "daily": options.get("retention_daily", 0),
            "weekly": options.get("retention_weekly", 0),
            "monthly": options.get("retention_monthly", 0)
        }
    retention_use_folders = bool(
        (isinstance(retention, dict) and retention.get("use_folders"))
        or options.get("retention_use_folders")
    )
    
    schedule_str = options.get("schedule", "0 5 * * *")
    target_hour = parse_schedule_hour(schedule_str)
    
    if not refresh_token:
        log("=" * 50)
        log("ERROR: refresh_token not configured!")
        log("")
        log("How to get refresh_token:")
        log("1. Visit: https://openapi.baidu.com/oauth/2.0/authorize?response_type=code&client_id=hq9yQ9w9kR4YHj1kyYafLygVocobh7Sf&redirect_uri=https://alistgo.com/tool/baidu/callback&scope=basic,netdisk&qrcode=1")
        log("2. Login with your Baidu account")
        log("3. Copy the 'refresh_token' from the result page")
        log("4. Paste it into the add-on configuration")
        log("=" * 50)
        
        # Keep running to show the message
        while True:
            time.sleep(3600)

    try:
        client = BaiduClient(refresh_token)
    except Exception as e:
        log(f"Failed to initialize client: {e}")
        log("Please check your refresh_token and try again.")
        while True:
            time.sleep(3600)

    # 2. Initial Sync
    log("Running initial sync...")
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

    # 3. Loop
    log(f"Entering scheduled mode. Target hour: {target_hour}:00")
    
    while True:
        now = datetime.now()
        target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            from datetime import timedelta
            target += timedelta(days=1)
        
        seconds_to_wait = (target - now).total_seconds()
        log(f"Next run: {target.strftime('%Y-%m-%d %H:%M')} (in {seconds_to_wait/3600:.1f}h)")
        time.sleep(seconds_to_wait)
        
        log("Scheduled execution started")
        # Re-check token before upload
        try:
            client._ensure_token()
            if retention_use_folders:
                daily_dir = _join_remote_dir(upload_path, "daily")
                sync_all_backups(client, daily_dir)
                retention_folder_mode(client, upload_path, retention)
            else:
                sync_all_backups(client, upload_path)
                cleanup_remote_backups(client, upload_path, retention)
        except Exception as e:
            log(f"Scheduled upload error: {e}")
        
        time.sleep(60)

if __name__ == "__main__":
    main()
