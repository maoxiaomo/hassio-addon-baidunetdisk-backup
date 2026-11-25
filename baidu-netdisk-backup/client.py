#!/usr/bin/env python3
"""Baidu Netdisk OAuth 2.0 Client — upload, list, delete, move, directory management."""
import json
import os
import time
import hashlib
import errno
import socket
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

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

# ── v1.3.0 重试预算 ─────────────────────────────────────────────────
CONNECT_TIMEOUT: int = 10               # 建连/DNS 上限（对齐容器 resolv timeout=10）
TIMEOUT_API: tuple = (CONNECT_TIMEOUT, DEFAULT_TIMEOUT)
TIMEOUT_CHUNK: tuple = (CONNECT_TIMEOUT, LONG_TIMEOUT)
RETRY_DEADLINE: float = 90.0            # 单个网络操作的墙钟预算（秒）
MAX_ATTEMPTS: int = 8                   # 单操作尝试次数硬上限（防止快速失败空转）

# AList's Client Credentials  (Public, widely used)
CLIENT_ID: str = "hq9yQ9w9kR4YHj1kyYafLygVocobh7Sf"
CLIENT_SECRET: str = "YH2VpZcFJHYNnV6vLfHQXDBhcE7ZChyE"
REDIRECT_URI: str = "https://alistgo.com/tool/baidu/callback"

TOKEN_FILE: str = "/data/baidu_token.json"
UPLOAD_CACHE_FILE: str = "/data/upload_cache.json"
# v1.3.1：远端核对信息走旁路文件，主缓存文件的值类型永远是 bool(true)。
# 这样回滚到 v1.2.7 时旧代码读到的是 true，语义与升级前完全一致，零手工步骤。
UPLOAD_CACHE_META_FILE: str = "/data/upload_cache_meta.json"
CACHE_SCHEMA_VERSION: int = 2


# ============================================================================
# Logger
# ============================================================================
def log(msg: str) -> None:
    """Print a timestamped log message and flush immediately."""
    print(f"[{datetime.now().strftime(TIME_FORMAT)}] {msg}", flush=True)


# ============================================================================
# 可重试性判定 & 墙钟预算重试（v1.3.0）
# ============================================================================
class PermanentError(Exception):
    """不可重试错误（参数非法 / 权限 / 配额 / 明确的 API 拒绝）。

    抛出即代表"再试也没用"，不消耗重试预算。
    """


class RetryBudgetExhausted(Exception):
    """墙钟预算（或尝试次数上限）耗尽 —— 已尽力，放弃并上报。"""


class UploadError(Exception):
    """单个文件上传最终失败；message 里带最后一次的具体原因。"""


class RemoteListError(Exception):
    """远端目录列举失败。**绝不能**退化成"空目录"。"""


# 网络层可重试的 errno：连接重置/拒绝、超时、网络不可达、DNS 临时失败
_TRANSIENT_ERRNOS = frozenset(
    e
    for e in (
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "ECONNREFUSED", None),
        getattr(errno, "ETIMEDOUT", None),
        getattr(errno, "EPIPE", None),
        getattr(errno, "ENETUNREACH", None),
        getattr(errno, "EHOSTUNREACH", None),
        getattr(errno, "ENETDOWN", None),
        getattr(errno, "EAGAIN", None),
        -3,   # EAI_AGAIN：临时 DNS 失败（本机 127.0.0.11 的典型表现）
        -4,   # EAI_FAIL
        -5,   # EAI_NODATA
    )
    if e is not None
)

# requests 侧的可重试异常类（requests 缺失时退化为空元组，不抛 NameError）
_TRANSIENT_REQUESTS = tuple(
    k
    for k in (
        getattr(requests, "Timeout", None),
        getattr(requests, "ConnectionError", None),
        getattr(getattr(requests, "exceptions", None), "ChunkedEncodingError", None),
        getattr(getattr(requests, "exceptions", None), "ContentDecodingError", None),
    )
    if isinstance(k, type)
)


def is_transient_error(exc: BaseException) -> bool:
    """True = 值得重试；False = 永久错误，立刻放弃。"""
    if isinstance(exc, (PermanentError, RetryBudgetExhausted)):
        return False
    if _TRANSIENT_REQUESTS and isinstance(exc, _TRANSIENT_REQUESTS):
        return True
    if isinstance(exc, socket.gaierror):
        return exc.errno in _TRANSIENT_ERRNOS
    if isinstance(exc, OSError):
        return exc.errno in _TRANSIENT_ERRNOS
    if isinstance(exc, ValueError):
        # 响应不是合法 JSON（网关错误页 / 被截断）—— 视为瞬态
        return True
    return False


def retry_call(what: str, fn, deadline: float, attempt_limit: int = MAX_ATTEMPTS):
    """在墙钟 *deadline*（``time.monotonic()`` 绝对时刻）之前重试 ``fn(attempt)``。

    * 第一次尝试总会执行；
    * 不可重试异常包成 :class:`PermanentError` 立刻抛出；
    * 预算或尝试次数耗尽抛 :class:`RetryBudgetExhausted`。
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn(attempt)
        except PermanentError:
            raise
        except Exception as e:                      # noqa: BLE001
            if not is_transient_error(e):
                raise PermanentError(f"{what}: {type(e).__name__}: {e}") from e
            remaining = deadline - time.monotonic()
            if attempt >= attempt_limit:
                raise RetryBudgetExhausted(
                    f"{what}: 达到尝试上限 {attempt_limit} 次；最后错误 "
                    f"{type(e).__name__}: {e}"
                ) from e
            if remaining <= RETRY_DELAY:
                raise RetryBudgetExhausted(
                    f"{what}: 墙钟预算耗尽（已试 {attempt} 次，剩余 {remaining:.1f}s）；"
                    f"最后错误 {type(e).__name__}: {e}"
                ) from e
            delay = min(RETRY_DELAY * attempt, remaining - 1.0)
            log(
                f"{what}: 第 {attempt} 次失败（{type(e).__name__}: {e}），"
                f"{delay:.0f}s 后重试（剩余预算 {remaining:.0f}s）"
            )
            time.sleep(delay)


# ============================================================================
# Baidu OAuth 2.0 Client
# ============================================================================
class BaiduClient:
    """Uses OAuth 2.0 access_token — official API, same method as AList."""

    def __init__(self, refresh_token: str, retry_deadline: float = RETRY_DEADLINE) -> None:
        self.initial_refresh_token: str = refresh_token
        self.refresh_token: str = refresh_token
        self.access_token: Optional[str] = None
        self.token_expires: float = 0.0

        # 单个网络操作的墙钟重试预算（秒）
        self.retry_deadline: float = float(retry_deadline or RETRY_DEADLINE)
        # 目录列举结果的内存缓存：同一周期内复用，避免重复 list
        self._dir_index: Dict[str, Set[str]] = {}
        self._dir_index_at: Dict[str, float] = {}
        self.dir_cache_ttl: float = 600.0
        # 观测：本轮实际发出的 list 请求次数（用于核对"结构性 list 7 次/轮"）
        self.list_calls: int = 0

        # Upload dedup cache：主文件值恒为 true；dir/verified 放旁路 meta 文件
        self._upload_cache: Dict[str, Any] = {}
        self._cache_meta: Dict[str, Dict[str, Any]] = {}
        self._legacy_meta: Dict[str, Any] = {}
        self._load_upload_cache()
        self._load_cache_meta()

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
        """读取主缓存文件。

        v1.3.1 契约：主文件的值恒为 ``true``（与 v1.2.7 完全一致，回滚零步骤）。
        v1.3.0 曾把值写成 ``{"dir","verified"}`` —— 读到这种旧格式时**就地降级**
        成 true + 旁路 meta，保证升级方向可自愈。
        """
        self._upload_cache = {}
        try:
            if os.path.exists(UPLOAD_CACHE_FILE):
                with open(UPLOAD_CACHE_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    for key, value in raw.items():
                        if isinstance(value, dict):
                            # v1.3.0 的格式：降级为主文件 true + meta 条目
                            self._legacy_meta[key] = value
                            self._upload_cache[key] = True
                        elif value:
                            self._upload_cache[key] = True
        except Exception as e:                       # noqa: BLE001
            log(f"ERROR: 读取上传缓存失败（本轮按空缓存处理，会触发远端核对）：{e}")
            self._upload_cache = {}

    def _load_cache_meta(self) -> None:
        """读取旁路核对信息（schema 2）。"""
        self._cache_meta = {}
        try:
            if os.path.exists(UPLOAD_CACHE_META_FILE):
                with open(UPLOAD_CACHE_META_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    entries = raw.get("entries")
                    if isinstance(entries, dict):
                        self._cache_meta = {
                            str(k): dict(v)
                            for k, v in entries.items()
                            if isinstance(v, dict)
                        }
                    elif raw:
                        # 无 schema 包装的裸 dict，同样接受
                        self._cache_meta = {
                            str(k): dict(v)
                            for k, v in raw.items()
                            if isinstance(v, dict)
                        }
        except Exception as e:                       # noqa: BLE001
            log(f"ERROR: 读取上传缓存旁路文件失败（将退化为按未核对处理）：{e}")
            self._cache_meta = {}

        # v1.3.0 旧格式（dict 值）迁移进来
        legacy = getattr(self, "_legacy_meta", None) or {}
        migrated = 0
        for key, value in legacy.items():
            if key not in self._cache_meta and isinstance(value, dict):
                self._cache_meta[key] = {
                    "dir": self._norm_dir(value.get("dir") or ""),
                    "verified": int(value.get("verified") or 0),
                }
                migrated += 1
        self._legacy_meta = {}
        if migrated:
            log(f"上传缓存已从 v1.3.0 格式迁移到旁路文件（{migrated} 条）")
            self._save_upload_cache()
            self._save_cache_meta()

    def _save_upload_cache(self) -> None:
        """原子写主缓存。写失败必须留 ERROR —— 缓存是"传过没有"的唯一依据。"""
        try:
            os.makedirs(os.path.dirname(UPLOAD_CACHE_FILE), exist_ok=True)
            tmp = f"{UPLOAD_CACHE_FILE}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._upload_cache, f)
            os.replace(tmp, UPLOAD_CACHE_FILE)
        except Exception as e:                       # noqa: BLE001
            log(f"ERROR: 写入上传缓存失败（下次可能重复上传）：{type(e).__name__}: {e}")

    def _save_cache_meta(self) -> None:
        try:
            os.makedirs(os.path.dirname(UPLOAD_CACHE_META_FILE), exist_ok=True)
            tmp = f"{UPLOAD_CACHE_META_FILE}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {"schema": CACHE_SCHEMA_VERSION, "entries": self._cache_meta}, f
                )
            os.replace(tmp, UPLOAD_CACHE_META_FILE)
        except Exception as e:                       # noqa: BLE001
            log(f"ERROR: 写入上传缓存旁路文件失败：{type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    # 上传去重缓存（v1.3.1：主文件恒 true，核对信息走 upload_cache_meta.json）
    # ------------------------------------------------------------------
    @staticmethod
    def _norm_dir(remote_dir: str) -> str:
        d = (remote_dir or "").strip()
        if d and not d.startswith("/"):
            d = "/" + d
        return d.rstrip("/") or "/"

    @staticmethod
    def _norm_dir_opt(remote_dir: str) -> str:
        """同 ``_norm_dir``，但空输入返回 ""（而不是 "/"）。

        ``_norm_dir("")`` 会返回 "/"，若拿它和真实目标目录比较，每条没有旁路信息
        的老缓存都会被判成 "dir-changed" → 全量重传。这里必须区分"没记录"与"根目录"。
        """
        d = (remote_dir or "").strip()
        return BaiduClient._norm_dir(d) if d else ""

    def _cache_key(self, local_path: str) -> Optional[str]:
        try:
            st = os.stat(local_path)
            return f"{os.path.basename(local_path)}:{st.st_size}:{int(st.st_mtime)}"
        except OSError:
            return None

    def _is_already_uploaded(self, local_path: str) -> bool:
        """兼容旧接口：只看缓存里是否有这条记录（不做远端核对）。"""
        key = self._cache_key(local_path)
        return bool(key and self._upload_cache.get(key))

    def _meta_for(self, key: str) -> Dict[str, Any]:
        """取旁路核对信息。主/旁路不一致（只有一边有）时视为未核对。"""
        if key not in self._upload_cache:
            return {}
        entry = self._cache_meta.get(key)
        return entry if isinstance(entry, dict) else {}

    def _mark_uploaded(self, local_path: str, remote_dir: str = "") -> None:
        """记录一次成功上传：主文件写 true，目录与核对时间写旁路文件。"""
        key = self._cache_key(local_path)
        if not key:
            return
        self._upload_cache[key] = True
        self._cache_meta[key] = {
            "dir": self._norm_dir(remote_dir),
            "verified": int(time.time()),
        }
        self._save_upload_cache()
        self._save_cache_meta()

    def mark_policy_deleted(self, remote_paths: List[str]) -> int:
        """保留策略删掉远端文件后调用。

        目的：本地文件还在、云端已被策略删除时，**不要**在下一个核对周期把
        300MB 重新传上去再被删一次（v1.3.0 的七天往返）。被标记的条目在文件
        本身未变（name:size:mtime 不变）时不再触发上传；文件一变就是新 key，
        正常上传。
        """
        if not remote_paths:
            return 0
        now = int(time.time())
        names = {os.path.basename(p.rstrip("/")): p for p in remote_paths if p}
        touched = 0
        for key in list(self._upload_cache.keys()):
            name = key.split(":", 1)[0]
            if name in names:
                entry = self._cache_meta.get(key) or {}
                entry["dir"] = self._norm_dir_opt(entry.get("dir") or "")
                entry["verified"] = int(entry.get("verified") or 0)
                entry["policy_deleted"] = now
                self._cache_meta[key] = entry
                touched += 1
        if touched:
            self._save_cache_meta()
            log(f"保留策略：已标记 {touched} 个本地仍存在的文件为\"策略删除\"，不再自动重传")
        for path in remote_paths:
            if path:
                self.invalidate_dir_cache(os.path.dirname(path) or "/")
        return touched

    def needs_upload(
        self,
        local_path: str,
        remote_dir: str,
        recheck_days: float = 7.0,
        force: bool = False,
    ) -> Tuple[bool, str]:
        """判断本地文件本次是否需要上传。

        Returns: ``(need_upload, reason)``，reason 用于日志与通知：
            stat-failed / not-cached / dir-changed / forced / remote-missing /
            remote-unknown / cache-fresh / remote-confirmed / retention-deleted
        """
        key = self._cache_key(local_path)
        if key is None:
            return True, "stat-failed"
        entry = self._upload_cache.get(key)
        if not entry:
            return True, "not-cached"
        if force:
            return True, "forced"

        target = self._norm_dir(remote_dir)
        meta = self._meta_for(key)
        cached_dir = self._norm_dir_opt(meta.get("dir") or "")
        verified = int(meta.get("verified") or 0)

        if meta.get("policy_deleted"):
            # 云端副本是保留策略按规则删掉的（不是丢失），本地文件没变 → 不重传
            return False, "retention-deleted"

        if cached_dir and cached_dir != target:
            # 目标目录变了（改 upload_path / 切换 use_folders）。
            # v1.3.1：先核对新目录，已在则直接确认，避免"全量重传"。
            exists = self.remote_has_file(target, os.path.basename(local_path))
            if exists is True:
                self._cache_meta[key] = {"dir": target, "verified": int(time.time())}
                self._save_cache_meta()
                return False, "remote-confirmed"
            if exists is None:
                return True, "remote-unknown"
            return True, "dir-changed"

        age_days = (time.time() - verified) / 86400.0 if verified else 1e9
        if verified and age_days <= float(recheck_days):
            return False, "cache-fresh"

        exists = self.remote_has_file(target, os.path.basename(local_path))
        if exists is True:
            self._cache_meta[key] = {"dir": target, "verified": int(time.time())}
            self._save_cache_meta()
            return False, "remote-confirmed"
        if exists is None:
            # 核不了 ≠ 已存在：宁可重传（同路径覆盖，秒传通常免流量）
            return True, "remote-unknown"
        self._upload_cache.pop(key, None)
        self._cache_meta.pop(key, None)
        self._save_upload_cache()
        self._save_cache_meta()
        return True, "remote-missing"

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

    def _ensure_token(self, deadline: Optional[float] = None) -> None:
        """Ensure access_token is valid (refresh if near expiry).

        v1.3.1：刷新动作本身也走 ``retry_call``，并受调用方的 cycle deadline 约束。
        v1.3.0 里 ``_ensure_token`` 在 ``retry_call`` 之外，一次 DNS 抖动/30s 超时
        会把整个文件判失败且一次重试都没有（11-14 / 11-21 的失败签名）。
        """
        if time.time() < self.token_expires - 600:
            return
        hard_deadline = time.monotonic() + self.retry_deadline
        if deadline is not None:
            hard_deadline = min(hard_deadline, deadline)

        def _do(attempt: int) -> None:
            self._refresh_access_token()

        retry_call("token refresh", _do, hard_deadline)

    def update_refresh_token(self, new_refresh_token: str) -> bool:
        """用户改了 refresh_token：丢弃旧 access_token 并立即换新。

        v1.3.0 的 ``main._apply_token_if_changed`` 调用了这个方法，但 50 条补丁里
        从未定义它 —— 用户在 Web UI 改 token 后每轮 AttributeError，同步全停。
        返回 True 表示已用新 token 拿到 access_token。
        """
        token = (new_refresh_token or "").strip()
        if not token:
            return False
        if token == self.initial_refresh_token and self.access_token:
            return True
        self.initial_refresh_token = token
        self.refresh_token = token
        self.access_token = None
        self.token_expires = 0.0
        self._refresh_access_token()
        return bool(self.access_token)

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
    def upload_file(
        self,
        local_path: str,
        remote_dir: str,
        deadline: Optional[float] = None,
    ) -> bool:
        """Upload a single file to *remote_dir* using the official xpan API.

        *deadline* 是整轮 cycle 的 ``time.monotonic()`` 绝对截止时刻；传进来后
        每个 precreate / chunk / merge 的预算都收缩到 ``min(now+retry, cycle)``，
        并在每个分片前检查一次 —— 一个 83 分片的文件无法把 1800s 预算拖成 2.3h。
        """
        self._ensure_token(deadline=deadline)

        filename = os.path.basename(local_path)
        if remote_dir.endswith("/"):
            remote_dir = remote_dir[:-1]
        if not remote_dir.startswith("/"):
            remote_dir = "/" + remote_dir

        full_remote_path = f"{remote_dir}/{filename}"
        log(f"Uploading: {filename} -> {full_remote_path}")

        try:
            self._do_upload_sliced(local_path, full_remote_path, deadline=deadline)
        except (PermanentError, RetryBudgetExhausted, UploadError) as e:
            log(f"Upload FAILED: {filename} — {e}")
            raise UploadError(str(e)) from e
        except Exception as e:                       # noqa: BLE001
            log(f"Upload FAILED: {filename} — {type(e).__name__}: {e}")
            raise UploadError(f"{type(e).__name__}: {e}") from e

        self._mark_uploaded(local_path, remote_dir)  # 缓存带上目标目录
        log(f"Upload SUCCESS: {filename}")
        return True

    def _op_deadline(self, cycle_deadline: Optional[float]) -> float:
        """单个网络操作的截止时刻 = min(now + retry_deadline, cycle_deadline)。"""
        op_deadline = time.monotonic() + self.retry_deadline
        if cycle_deadline is not None:
            op_deadline = min(op_deadline, cycle_deadline)
        return op_deadline

    def _check_cycle_deadline(self, cycle_deadline: Optional[float], what: str) -> None:
        if cycle_deadline is not None and time.monotonic() >= cycle_deadline:
            raise RetryBudgetExhausted(
                f"{what}: 整轮时间预算已耗尽（cycle deadline 到达），中止本文件"
            )

    def _do_upload_sliced(
        self,
        local_path: str,
        full_remote_path: str,
        deadline: Optional[float] = None,
    ) -> bool:
        """Sliced upload: precreate → upload chunks → merge."""
        cycle_deadline = deadline
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

        # Step 2 — precreate（v1.3.0：墙钟预算内重试，DNS/RST 可自愈）
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

        def _precreate(attempt: int) -> Dict[str, Any]:
            resp = requests.post(
                precreate_url,
                data=precreate_data,
                headers=headers,
                timeout=TIMEOUT_API,
            )
            data = resp.json()
            errno_val = data.get("errno")
            if errno_val == 0:
                return data
            detail = f"precreate errno={errno_val} errmsg={data.get('errmsg', '')}"
            if errno_val == -1:            # 未知错误 → 值得再试
                raise ValueError(detail)
            raise PermanentError(detail)   # 权限/配额/参数类 → 立刻失败

        pre_json = retry_call(
            "precreate", _precreate, self._op_deadline(cycle_deadline)
        )

        uploadid = pre_json.get("uploadid")
        return_type = pre_json.get("return_type")

        if return_type == 2:
            log("Rapid upload (秒传) successful! File already exists on server.")
            return True
        if not uploadid:
            raise PermanentError(f"precreate 未返回 uploadid：{pre_json}")

        log(f"Precreate OK. return_type={return_type}, UploadID: {uploadid}")

        # Step 3 — upload chunks（每个分片独立墙钟预算）
        log("Step 2/3: Uploading chunks...")
        total_blocks = len(block_list)
        with open(local_path, "rb") as f:
            for i in range(total_blocks):
                # v1.3.1：每个分片前检查整轮预算，避免单文件拖穿 cycle deadline
                self._check_cycle_deadline(
                    cycle_deadline, f"chunk {i + 1}/{total_blocks}"
                )
                chunk = f.read(CHUNK_SIZE)
                progress = ((i + 1) / total_blocks) * 100

                def _send_chunk(
                    attempt: int,
                    _i: int = i,
                    _chunk: bytes = chunk,
                    _progress: float = progress,
                ) -> bool:
                    if _i % 5 == 0 or attempt > 1:
                        log(
                            f"  Chunk {_i + 1}/{total_blocks} ({_progress:.0f}%)"
                            + (f" retry {attempt}" if attempt > 1 else "")
                        )
                    upload_url = (
                        f"https://d.pcs.baidu.com/rest/2.0/pcs/superfile2"
                        f"?method=upload&access_token={self.access_token}"
                        f"&type=tmpfile&path={requests.utils.quote(full_remote_path)}"
                        f"&uploadid={uploadid}&partseq={_i}"
                    )
                    files = {"file": ("blob", _chunk, "application/octet-stream")}
                    r = requests.post(
                        upload_url, files=files, headers=headers, timeout=TIMEOUT_CHUNK
                    )
                    data = r.json()
                    if r.status_code == 200 and "md5" in data:
                        return True
                    detail = f"HTTP {r.status_code}: {str(getattr(r, 'text', ''))[:200]}"
                    if 400 <= int(r.status_code) < 500:
                        raise PermanentError(f"chunk {_i + 1} 被拒绝（{detail}）")
                    raise ValueError(f"chunk {_i + 1} 响应异常（{detail}）")

                retry_call(
                    f"chunk {i + 1}/{total_blocks}",
                    _send_chunk,
                    self._op_deadline(cycle_deadline),
                )
        log(f"All {total_blocks} chunks uploaded.")

        # Step 4 — merge（v1.3.0：同样受墙钟预算保护）
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

        def _merge(attempt: int) -> Dict[str, Any]:
            resp = requests.post(
                create_url, data=create_data, headers=headers, timeout=TIMEOUT_API
            )
            result = resp.json()
            errno_val = result.get("errno")
            if errno_val == 0:
                return result
            detail = f"merge errno={errno_val} errmsg={result.get('errmsg', '')}"
            if errno_val == -1:            # 未知错误 → 值得再试
                raise ValueError(detail)
            raise PermanentError(detail)

        result = retry_call("merge", _merge, self._op_deadline(cycle_deadline))
        log(f"Merge OK. File ID: {result.get('fs_id')}")
        return True

    # ------------------------------------------------------------------
    # Remote file listing
    # ------------------------------------------------------------------
    def list_remote_files(
        self, remote_dir: str, strict: bool = True
    ) -> List[Dict[str, Any]]:
        """列举 *remote_dir*（分页）。

        v1.3.0：失败不再返回 ``[]``（那与"空目录"无法区分）。
        默认 ``strict=True`` 抛 :class:`RemoteListError`；``strict=False`` 时
        返回 ``[]`` 并打 ERROR 日志，仅用于"拿不到就算了"的核对路径。
        远端目录不存在（errno=-9）按"确认的空目录"处理，返回 ``[]``。
        """
        self._ensure_token()
        self.list_calls += 1
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

            def _fetch(attempt: int, _params: Dict[str, Any] = params) -> Dict[str, Any]:
                resp = requests.get(url, params=_params, timeout=TIMEOUT_API)
                data = resp.json()
                errno_val = data.get("errno")
                if errno_val == 0:
                    return data
                if errno_val == -9:                     # 目录不存在
                    return {"errno": 0, "list": [], "has_more": 0, "_absent": True}
                detail = f"list errno={errno_val} errmsg={data.get('errmsg', '')}"
                if errno_val == -1:
                    raise ValueError(detail)
                raise PermanentError(detail)

            try:
                data = retry_call(
                    f"list {remote_dir}", _fetch, time.monotonic() + self.retry_deadline
                )
            except (PermanentError, RetryBudgetExhausted) as e:
                if strict:
                    raise RemoteListError(f"{remote_dir}: {e}") from e
                log(f"ERROR: 列举 {remote_dir} 失败（按未知处理，不当成空目录）：{e}")
                return []

            items: List[Dict[str, Any]] = data.get("list") or []
            all_items.extend(items)

            # has_more 官方是 int(0/1)，个别网关返回 bool/字符串，一并兼容
            # 注：Python 里 True == 1，(1, "1") 已覆盖 bool 与 int 两种官方返回
            has_more = data.get("has_more")
            if has_more not in (1, "1"):
                break
            if not items:                               # 防御：has_more=1 但空页
                log(f"WARNING: {remote_dir} has_more=1 但本页为空，停止翻页")
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
    # 远端存在性核对（v1.3.0：解决"缓存说传过、云端其实没了"）
    # ------------------------------------------------------------------
    def invalidate_dir_cache(self, remote_dir: Optional[str] = None) -> None:
        if remote_dir is None:
            self._dir_index.clear()
            self._dir_index_at.clear()
            return
        key = self._norm_dir(remote_dir)
        self._dir_index.pop(key, None)
        self._dir_index_at.pop(key, None)

    def _list_dir_names(self, remote_dir: str) -> Optional[Set[str]]:
        """返回目录内文件名集合；拿不到返回 None（≠ 空集合）。"""
        key = self._norm_dir(remote_dir)
        now = time.monotonic()
        if (
            key in self._dir_index
            and (now - self._dir_index_at.get(key, 0.0)) < self.dir_cache_ttl
        ):
            return self._dir_index[key]
        try:
            items = self.list_remote_files(remote_dir, strict=True)
        except RemoteListError as e:
            log(f"WARNING: 无法核对 {remote_dir}（本次按未知处理）：{e}")
            return None
        names = {str(i.get("server_filename") or "") for i in items}
        self._dir_index[key] = names
        self._dir_index_at[key] = now
        return names

    def remote_has_file(self, remote_dir: str, filename: str) -> Optional[bool]:
        """True / False / None（None = 无法判断，调用方不得当成"不存在"）。"""
        names = self._list_dir_names(remote_dir)
        if names is None:
            return None
        return filename in names

    # ------------------------------------------------------------------
    # Batch filemanager  (Issue 6: deduplicate delete / move)
    # ------------------------------------------------------------------
    def _batch_filemanager(
        self,
        remote_paths: List[Any],
        opera: str,
        action_name: str,
        stats: Optional[Dict[str, int]] = None,
    ) -> bool:
        """Generic batched filemanager call (delete / move / copy etc.).

        *stats*（可选）会被累加 ``{"ok","failed","failed_batches"}``，用于在
        通知里如实写出"删了 N 个、失败 M 个"，而不是笼统的"跳过删除"（E12）。
        """
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

            def _do_batch(attempt: int, _batch=batch) -> bool:
                resp = requests.post(
                    url,
                    params=params,
                    data=form_data,
                    headers=headers,
                    timeout=TIMEOUT_API,
                )
                data = resp.json()
                if data.get("errno") == 0:
                    log(f"{action_name} {len(_batch)} remote files")
                    return True
                detail = f"errno={data.get('errno')} errmsg={data.get('errmsg', '')}"
                if data.get("errno") == -1:
                    raise ValueError(detail)
                raise PermanentError(detail)

            try:
                retry_call(
                    f"filemanager {opera}",
                    _do_batch,
                    time.monotonic() + min(self.retry_deadline, 45.0),
                )
                if stats is not None:
                    stats["ok"] = stats.get("ok", 0) + len(batch)
            except (PermanentError, RetryBudgetExhausted) as e:
                ok = False
                if stats is not None:
                    stats["failed"] = stats.get("failed", 0) + len(batch)
                    stats["failed_batches"] = stats.get("failed_batches", 0) + 1
                log(f"Failed to {action_name.lower()} remote files: {e}")

        # 目录索引失效：删除/移动后必须让下一次 list 重新取（v1.3.0 定义了
        # invalidate_dir_cache 却没有任何调用点 → 索引与实际不一致）
        if opera == "delete":
            for path in remote_paths:
                if isinstance(path, str) and path:
                    self.invalidate_dir_cache(os.path.dirname(path) or "/")
        elif opera == "move":
            for move in remote_paths:
                if not isinstance(move, dict):
                    continue
                src = move.get("path") or ""
                if src:
                    self.invalidate_dir_cache(os.path.dirname(src) or "/")
                dest = move.get("dest") or ""
                if dest:
                    self.invalidate_dir_cache(dest)
        return ok

    def delete_remote_files(self, remote_paths: List[str]) -> bool:
        """Delete *remote_paths* on Baidu Netdisk (batched)."""
        return self._batch_filemanager(remote_paths, "delete", "Deleted")

    def delete_remote_files_detailed(self, remote_paths: List[str]) -> Dict[str, int]:
        """删除并返回逐批统计：``{"ok","failed","failed_batches","requested"}``。"""
        stats: Dict[str, int] = {"ok": 0, "failed": 0, "failed_batches": 0}
        self._batch_filemanager(remote_paths, "delete", "Deleted", stats)
        stats["requested"] = len(remote_paths)
        return stats

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

        def _create(attempt: int) -> bool:
            resp = requests.post(
                url,
                params=params,
                data=form_data,
                headers=headers,
                timeout=TIMEOUT_API,
            )
            res = resp.json()
            log(f"Create dir response: {res}")

            errno_ = res.get("errno")
            if errno_ == 0:
                log(f"Directory created: {remote_dir}")
                return True
            if errno_ == -8:
                log(f"Directory already exists: {remote_dir}")
                return True
            if errno_ is None and ("category" in res or "fs_id" in res):
                # 百度对「已存在」有时返回 {'category': 6}，不带 errno 字段。
                # 不能用 `"path" in res` 判断：鉴权失败时也返回
                # {'errno': -6, 'path': '', 'request_id': ...}，会把它误判成
                # 「目录已存在」，从而把 token 失效静默掉（真机冒烟 2026-11-25 抓到）。
                log(f"Directory exists (non-standard response, no errno): {remote_dir}")
                return True
            detail = f"create dir errno={errno_} errmsg={res.get('errmsg', '')}"
            if errno_ == -1:
                raise ValueError(detail)
            raise PermanentError(detail)

        try:
            return retry_call(
                f"create dir {remote_dir}",
                _create,
                time.monotonic() + min(self.retry_deadline, 45.0),
            )
        except (PermanentError, RetryBudgetExhausted) as e:
            log(f"Failed to create directory {remote_dir}: {e}")
            return False