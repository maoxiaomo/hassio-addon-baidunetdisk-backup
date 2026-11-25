#!/usr/bin/env python3
"""v1.3.1 修复的离线 pytest 套件（stub 掉 requests，不联网、不读 /data、不读 /backup）。

运行：
    python -m pytest -q tests/test_v130_fixes.py

覆盖（原 24 条 + 新增）：
  1. 四条失败路径（precreate / chunk / merge / 清单列举）各自触发 backup_failure
  2. precreate / merge 第 4 次尝试成功
  3. 单操作预算有界（不无限重试、不卡死）
  4. 保留策略：手工备份不删 / 有界进度 / dry-run / list 失败不删
  5. list 失败不会写出"0 个文件"的清单
  6. 空 /backup 的语义（既不是成功也不是失败；且跳过 retention）
  7. 缓存与远端脱节后的重传（自愈）
  8. 老 options.json 兼容
  v1.3.1 新增（对应 review_findings D 节 6 条 + 逐条关闭证据）：
  D1  run_sync_cycle 端到端：上传失败恰好一条 backup_failure；retention 抛异常
      也变成 failure 且不补发 success
  D2  retention_folder_mode（默认模式）下"手工备份是最旧的一份"→ 不删
  D3  ≥80 分片 + 某片持续失败 → 受 cycle deadline 约束，总耗时有上界
  D4  _cycle_budget 在 `0 5 * * *` 与 `*/5 * * * *` 下的取值
  D5  _apply_token_if_changed：改 token 后下一次上传用新 token
  D6  锁竞争：cron 撞上手动同步 → 排队且发通知，不静默丢弃
  +   P1-5/P1-6/P1-7/P1-9/P1-10/E3/E6/E7/E8/E12 的直接用例
"""
import json
import os
import re
import sys
import threading
import time as _real_time
import types
import urllib.parse
import urllib.request
from datetime import datetime

import pytest

# ─────────────────────────────────────────────────────────────────────
# 0. 在被测模块 import 之前塞入 fake requests
# ─────────────────────────────────────────────────────────────────────
class FakeRequestException(Exception):
    pass


class FakeConnectionError(FakeRequestException):
    pass


class FakeTimeout(FakeRequestException):
    pass


class FakeHTTPError(FakeRequestException):
    pass


_fake_exceptions = types.SimpleNamespace(
    RequestException=FakeRequestException,
    ConnectionError=FakeConnectionError,
    Timeout=FakeTimeout,
    HTTPError=FakeHTTPError,
    ChunkedEncodingError=FakeRequestException,
    ContentDecodingError=FakeRequestException,
)

_fake_requests = types.ModuleType("requests")
_fake_requests.RequestException = FakeRequestException
_fake_requests.ConnectionError = FakeConnectionError
_fake_requests.Timeout = FakeTimeout
_fake_requests.HTTPError = FakeHTTPError
_fake_requests.exceptions = _fake_exceptions
_fake_requests.utils = types.SimpleNamespace(quote=urllib.parse.quote)
_fake_requests.Response = type("Response", (), {})


def _no_transport(*args, **kwargs):
    raise AssertionError("测试未安装 transport，却发生了网络请求")


_fake_requests.get = _no_transport
_fake_requests.post = _no_transport
sys.modules["requests"] = _fake_requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import client as client_mod          # noqa: E402
import sync as sync_mod              # noqa: E402
import retention as retention_mod    # noqa: E402
import notifier as notifier_mod      # noqa: E402
import main as main_mod              # noqa: E402
import web as web_mod                # noqa: E402

# 测试用的假凭据（不是真实 token；本文件不含任何真实凭据）
ACCESS_TOKEN = "access-token-for-tests"
REFRESH_TOKEN = "refresh-token-for-tests"
NEW_ACCESS_TOKEN = "new-access-token-for-tests"
NEW_REFRESH_TOKEN = "new-refresh-token-for-tests"


# ─────────────────────────────────────────────────────────────────────
# 1. 基础设施：假响应、假 transport、假时钟、假云盘
# ─────────────────────────────────────────────────────────────────────
class Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = (
            json.dumps(payload)
            if not isinstance(payload, Exception)
            else str(payload)
        )[:400]

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class Transport:
    """按脚本顺序消费 POST/GET，并记录所有请求。"""

    def __init__(self):
        self.requests = []
        self.post_plan = []
        self.get_plan = []

    def post(self, url, **kwargs):
        self.requests.append({"method": "POST", "url": url, **kwargs})
        if not self.post_plan:
            raise AssertionError("未预期的 POST：%s" % url)
        return self.post_plan.pop(0)(url)

    def get(self, url, **kwargs):
        self.requests.append({"method": "GET", "url": url, **kwargs})
        if not self.get_plan:
            raise AssertionError("未预期的 GET：%s" % url)
        return self.get_plan.pop(0)(url)


def install_transport(monkeypatch, transport):
    monkeypatch.setattr(client_mod.requests, "post", transport.post, raising=False)
    monkeypatch.setattr(client_mod.requests, "get", transport.get, raising=False)


class Clock:
    """虚拟时钟：sleep 推进 offset，monotonic/time 跟随，其余属性透传真实 time。"""

    def __init__(self):
        self.offset = 0.0

    def monotonic(self):
        return _real_time.monotonic() + self.offset

    def time(self):
        return _real_time.time() + self.offset

    def sleep(self, seconds):
        self.offset += max(0.0, float(seconds))

    def __getattr__(self, name):
        return getattr(_real_time, name)


# ─────────────────────────────────────────────────────────────────────
# F3：宿主 /data 隔离（autouse）
# ─────────────────────────────────────────────────────────────────────
# 真实的 /data（Linux 容器）与 C:\data（Windows 宿主上 "/data" 的落点）
_HOST_DATA_DIRS = ("/data", "C:\\data")


def _host_data_snapshot():
    """记录宿主真实 /data 的 (mtime_ns, size) 快照。

    只用来在用例结束后断言"没有新文件、没有文件被改写"——读 /data 是无害的，
    写 /data 才是事故（上真机会覆盖用户配置 / token / 上传缓存）。
    """
    snapshot = {}
    roots = {os.path.abspath(raw) for raw in _HOST_DATA_DIRS}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                snapshot[full] = (st.st_mtime_ns, st.st_size)
    return snapshot


@pytest.fixture(autouse=True)
def isolate_host_data(tmp_path, monkeypatch):
    """每个用例都把 /data 相关路径指到 tmp_path，并断言宿主 /data 没被写。

    终审 FIX-2 第 4 条：``test_web_save_reports_api_failure_honestly`` 在 patch
    ``web_mod.CONFIG_PATH`` **之前**就调了 ``web_mod._save_options(...)``，会写宿主
    真实的 ``/data/options.json``（Windows 上即 ``C:\\data\\options.json``）——上真机
    就是拿测试 payload 覆盖用户配置。

    修法不是"给那一条补 monkeypatch"，而是**所有**用例统一隔离：漏一条就可能
    写穿到宿主。``/data`` 下的全部已知路径都指向 *tmp_path*：

      * ``web.CONFIG_PATH`` / ``main.CONFIG_PATH`` → options.json
      * ``client.TOKEN_FILE`` / ``UPLOAD_CACHE_FILE`` / ``UPLOAD_CACHE_META_FILE``
      * ``retention._MIGRATION_FLAG_FILE``
    """
    data = tmp_path / "host_data"
    data.mkdir(parents=True, exist_ok=True)
    options = data / "options.json"
    options.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(web_mod, "CONFIG_PATH", str(options), raising=False)
    monkeypatch.setattr(main_mod, "CONFIG_PATH", str(options), raising=False)
    monkeypatch.setattr(
        client_mod, "TOKEN_FILE", str(data / "baidu_token.json"), raising=False
    )
    monkeypatch.setattr(
        client_mod, "UPLOAD_CACHE_FILE", str(data / "upload_cache.json"), raising=False
    )
    monkeypatch.setattr(
        client_mod,
        "UPLOAD_CACHE_META_FILE",
        str(data / "upload_cache_meta.json"),
        raising=False,
    )
    monkeypatch.setattr(
        retention_mod,
        "_MIGRATION_FLAG_FILE",
        str(data / "migration_done.flag"),
        raising=False,
    )

    before = _host_data_snapshot()
    yield
    after = _host_data_snapshot()
    if after != before:
        created = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
        raise AssertionError(
            "用例向宿主真实 /data 写了文件（F3 隔离失效）："
            "新建=%s 删除=%s 改写=%s" % (created, removed, changed)
        )


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """隔离 /data：token 文件、上传缓存（主 + 旁路）、虚拟时钟。

    token 文件里的 refresh_token 必须与 ``BaiduClient(...)`` 用的那个一致，
    否则 ``_load_cached_token`` 会判定"配置变了"，丢弃缓存并联网刷新
    —— 这正是 v1.3.0 交付物里 11 条用例红掉的根因（B3）。
    """
    monkeypatch.setattr(client_mod, "TOKEN_FILE", str(tmp_path / "token.json"))
    monkeypatch.setattr(
        client_mod, "UPLOAD_CACHE_FILE", str(tmp_path / "upload_cache.json")
    )
    monkeypatch.setattr(
        client_mod, "UPLOAD_CACHE_META_FILE", str(tmp_path / "upload_cache_meta.json")
    )
    token = {
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "expires_at": _real_time.time() + 86400,
        "source_refresh_token": REFRESH_TOKEN,
    }
    (tmp_path / "token.json").write_text(json.dumps(token), encoding="utf-8")
    monkeypatch.setattr(client_mod, "time", Clock())
    return tmp_path


def _norm(remote_dir):
    d = (remote_dir or "").strip()
    if d and not d.startswith("/"):
        d = "/" + d
    return d.rstrip("/") or "/"


class FakeCloud:
    """扁平模式假云盘：所有目录返回同一份 items。"""

    def __init__(self, items=None):
        self.items = list(items or [])
        self.deleted = []
        self.uploads = []
        self.moves = []
        self.created = []
        self.fail_delete = False
        self.list_error = None

    def list_remote_files(self, remote_dir, strict=True):
        if self.list_error:
            raise self.list_error
        return list(self.items)

    def delete_remote_files(self, paths):
        self.deleted.extend(paths)
        return not self.fail_delete

    def upload_file(self, local_path, remote_dir, **kwargs):
        self.uploads.append((local_path, remote_dir))
        return True

    def create_remote_dir(self, remote_dir):
        self.created.append(remote_dir)
        return True

    def move_remote_files(self, moves):
        self.moves.extend(moves)
        return True


class DirCloud:
    """目录感知的假云盘（folder 模式必须用这个）。"""

    def __init__(self, dirs=None):
        self.dirs = {_norm(k): list(v) for k, v in (dirs or {}).items()}
        self.deleted = []
        self.uploads = []
        self.moves = []
        self.created = []
        self.move_ok = True
        self.list_error = None

    def list_remote_files(self, remote_dir, strict=True):
        if self.list_error:
            raise self.list_error
        return list(self.dirs.get(_norm(remote_dir), []))

    def delete_remote_files(self, paths):
        self.deleted.extend(paths)
        return True

    def upload_file(self, local_path, remote_dir, **kwargs):
        self.uploads.append((local_path, remote_dir))
        return True

    def create_remote_dir(self, remote_dir):
        self.created.append(remote_dir)
        return True

    def move_remote_files(self, moves):
        self.moves.extend(moves)
        return self.move_ok


class CycleCloud:
    """供 run_sync_cycle 端到端使用的假 client（含配额/检索计数）。"""

    def __init__(self, dirs=None, upload_error=None, list_error_dirs=None):
        self.dirs = {_norm(k): list(v) for k, v in (dirs or {}).items()}
        self.upload_error = upload_error
        self.list_error_dirs = {_norm(d) for d in (list_error_dirs or [])}
        self.deleted = []
        self.uploads = []
        self.moves = []
        self.created = []
        self.list_calls = 0

    def list_remote_files(self, remote_dir, strict=True):
        self.list_calls += 1
        if _norm(remote_dir) in self.list_error_dirs:
            raise RuntimeError("list down: %s" % remote_dir)
        return list(self.dirs.get(_norm(remote_dir), []))

    def needs_upload(self, local_path, remote_dir, recheck_days=7.0, force=False):
        return True, "not-cached"

    def create_remote_dir(self, remote_dir):
        self.created.append(remote_dir)
        return True

    def upload_file(self, local_path, remote_dir, deadline=None):
        if self.upload_error:
            raise self.upload_error
        self.uploads.append((local_path, remote_dir))
        return True

    def move_remote_files(self, moves):
        self.moves.extend(moves)
        return True

    def delete_remote_files(self, paths):
        self.deleted.extend(paths)
        return True

    def get_quota(self):
        return None


def auto_item(day, hour=4):
    name = "automatic_backup_2026_11_%02d_04.45_%04d.tar" % (day, day)
    return {
        "server_filename": name,
        "path": "/B/每日/" + name,
        "size": 100,
        "isdir": 0,
        "server_mtime": 1764000000 + day * 86400 + hour * 3600,
    }


def auto_dash(dt, seq=1):
    """名字里带 _YYYY-MM-DD_HH.MM_ 的自动备份，mtime 与名字一致（±1 分钟）。"""
    name = "automatic_backup_%s_%04d.tar" % (dt.strftime("%Y-%m-%d_%H.%M"), seq)
    return {
        "server_filename": name,
        "path": "/B/每日/" + name,
        "size": 100,
        "isdir": 0,
        "server_mtime": int(dt.timestamp()) + 60,
    }


def manual_item(dt=None):
    dt = dt or datetime(2026, 11, 3, 16, 1)
    name = "2026nian_11yue_3ri_16_01_03_2026-11-03_16.01_07289880.tar"
    return {
        "server_filename": name,
        "path": "/B/每日/" + name,
        "size": 297492480,
        "isdir": 0,
        "server_mtime": int(dt.timestamp()),
    }


def raise_reset(url):
    raise FakeConnectionError("('Connection aborted.', ConnectionResetError(104, 'reset'))")


def raise_timeout(url):
    raise FakeTimeout("HTTPSConnectionPool: connect timed out")


def dir_ok(url):
    return Resp({"errno": 0})


def precreate_ok(url):
    return Resp({"errno": 0, "uploadid": "UID1", "return_type": 1})


def chunk_ok(url):
    return Resp({"md5": "deadbeef"}, 200)


def merge_ok(url):
    return Resp({"errno": 0, "fs_id": 42})


def token_ok(url):
    return Resp(
        {
            "access_token": NEW_ACCESS_TOKEN,
            "expires_in": 2592000,
            "refresh_token": NEW_REFRESH_TOKEN,
        }
    )


def make_client(tmp_path, deadline=20.0):
    return client_mod.BaiduClient(REFRESH_TOKEN, retry_deadline=deadline)


def make_tar(directory, name="automatic_backup_2026_11_25_04.45_0001.tar"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"0123456789")
    return path


def run_sync_with_plan(monkeypatch, sandbox, transport, deadline=20.0):
    backup_dir = sandbox / "backup"
    make_tar(backup_dir)
    monkeypatch.setattr(sync_mod, "BACKUP_DIR", str(backup_dir))
    install_transport(monkeypatch, transport)
    client = make_client(sandbox, deadline=deadline)
    result = sync_mod.sync_all_backups(client, "/HomeAssistant/Backup/每日")
    return client, result


def record_notifications(monkeypatch):
    events = []

    def fake_notify(notifications, event_type, event_data=None):
        events.append((event_type, event_data))
        return {"sent": 1, "failed": 0, "skipped": 0, "results": {}}

    monkeypatch.setattr(main_mod, "notify_event", fake_notify)
    monkeypatch.setattr(notifier_mod, "notify_event", fake_notify)
    return events


def cycle_cfg(use_folders=True, **retention_over):
    retention = {
        "use_folders": use_folders,
        "daily": 7,
        "weekly": 4,
        "monthly": 12,
        "protect_manual": True,
        "max_delete_per_run": 10,
        "dry_run": False,
    }
    retention.update(retention_over)
    return {
        "upload_path": "/B",
        "retention": retention,
        "notifications": {"enabled": True, "channels": {}},
        "recheck_days": 7.0,
        "cycle_budget": 1800.0,
        "refresh_token": REFRESH_TOKEN,
        "schedule": "0 5 * * *",
        "cron": main_mod.CronSchedule("0 5 * * *"),
    }


# ─────────────────────────────────────────────────────────────────────
# 2. ① 四条失败路径各自触发 backup_failure
# ─────────────────────────────────────────────────────────────────────
def test_precreate_failure_triggers_backup_failure(monkeypatch, sandbox):
    transport = Transport()
    transport.post_plan = [dir_ok] + [raise_reset] * 10
    _client, result = run_sync_with_plan(monkeypatch, sandbox, transport)

    assert result["outcome"] == "failed"
    assert result["failed_count"] == 1
    assert result["failed_files"][0]["name"].endswith(".tar")

    events = record_notifications(monkeypatch)
    main_mod._notify_sync_result(
        result, {"enabled": True}, "/HomeAssistant/Backup/每日", [], "schedule"
    )
    assert [e for e, _ in events] == ["backup_failure"]


def test_chunk_failure_triggers_backup_failure(monkeypatch, sandbox):
    transport = Transport()
    transport.post_plan = [dir_ok, precreate_ok] + [raise_reset] * 10
    _client, result = run_sync_with_plan(monkeypatch, sandbox, transport)

    assert result["outcome"] == "failed"
    assert result["failed_count"] == 1
    assert "chunk" in result["failed_files"][0]["error"].lower()

    events = record_notifications(monkeypatch)
    main_mod._notify_sync_result(result, {"enabled": True}, "/B", [], "schedule")
    assert [e for e, _ in events] == ["backup_failure"]


def test_merge_failure_triggers_backup_failure(monkeypatch, sandbox):
    transport = Transport()
    transport.post_plan = [dir_ok, precreate_ok, chunk_ok] + [raise_timeout] * 10
    _client, result = run_sync_with_plan(monkeypatch, sandbox, transport)

    assert result["outcome"] == "failed"
    assert result["failed_count"] == 1
    assert "merge" in result["failed_files"][0]["error"].lower()

    events = record_notifications(monkeypatch)
    main_mod._notify_sync_result(result, {"enabled": True}, "/B", [], "schedule")
    assert [e for e, _ in events] == ["backup_failure"]


def test_manifest_list_failure_triggers_backup_failure(monkeypatch):
    class BoomCloud:
        def __init__(self):
            self.uploads = []

        def list_remote_files(self, remote_dir, strict=True):
            raise RuntimeError("list down")

        def upload_file(self, local_path, remote_dir):
            self.uploads.append((local_path, remote_dir))
            return True

    cloud = BoomCloud()
    with pytest.raises(RuntimeError):
        retention_mod.generate_manifest(cloud, "/B")
    assert cloud.uploads == []          # ⑤ 清单没有被写出去

    events = record_notifications(monkeypatch)
    sync_result = {
        "outcome": "ok",
        "success": True,
        "total_count": 1,
        "uploaded_count": 1,
        "skipped_count": 0,
        "failed_count": 0,
    }
    main_mod._notify_sync_result(
        sync_result, {"enabled": True}, "/B", ["清单生成：list down"], "schedule"
    )
    assert [e for e, _ in events] == ["backup_failure"]
    assert events[0][1]["stage"].startswith("清单生成")


# ─────────────────────────────────────────────────────────────────────
# 3. ② 第 4 次尝试成功
# ─────────────────────────────────────────────────────────────────────
def test_precreate_succeeds_on_fourth_attempt(monkeypatch, sandbox):
    transport = Transport()
    transport.post_plan = [
        dir_ok,
        raise_reset,
        raise_reset,
        raise_reset,
        precreate_ok,
        chunk_ok,
        merge_ok,
    ]
    client, result = run_sync_with_plan(monkeypatch, sandbox, transport, deadline=90.0)

    assert result["outcome"] == "ok"
    assert result["uploaded_count"] == 1
    precreate_calls = [r for r in transport.requests if "method=precreate" in r["url"]]
    assert len(precreate_calls) == 4


def test_merge_succeeds_on_fourth_attempt(monkeypatch, sandbox):
    transport = Transport()
    transport.post_plan = [
        dir_ok,
        precreate_ok,
        chunk_ok,
        raise_timeout,
        raise_timeout,
        raise_timeout,
        merge_ok,
    ]
    _client, result = run_sync_with_plan(monkeypatch, sandbox, transport, deadline=90.0)

    assert result["outcome"] == "ok"
    merge_calls = [r for r in transport.requests if "method=create" in r["url"]]
    assert len(merge_calls) == 4


# ─────────────────────────────────────────────────────────────────────
# 4. ③ 预算耗尽有界
# ─────────────────────────────────────────────────────────────────────
def test_retry_budget_is_bounded(monkeypatch, sandbox):
    transport = Transport()
    # 全部超时：3s/9s/15s… 由 retry_call 的墙钟预算兜底
    transport.post_plan = [raise_timeout] * 50
    install_transport(monkeypatch, transport)

    client = make_client(sandbox, deadline=90.0)
    clock = client_mod.time
    started = clock.monotonic()
    target = sandbox / "a.tar"
    target.write_bytes(b"0123456789")

    with pytest.raises(client_mod.UploadError):
        client.upload_file(str(target), "/B")

    elapsed = clock.monotonic() - started
    assert elapsed <= 100.0, "预算失控：%s 秒" % elapsed
    assert len(transport.requests) <= client_mod.MAX_ATTEMPTS


def test_permanent_error_is_not_retried(monkeypatch, sandbox):
    transport = Transport()
    transport.post_plan = [
        lambda url: Resp({"errno": -6, "errmsg": "no permission"})
    ]
    install_transport(monkeypatch, transport)

    client = make_client(sandbox, deadline=90.0)
    target = sandbox / "a.tar"
    target.write_bytes(b"0123456789")

    with pytest.raises(client_mod.UploadError):
        client.upload_file(str(target), "/B")
    precreate_calls = [r for r in transport.requests if "method=precreate" in r["url"]]
    assert len(precreate_calls) == 1           # 永久错误不重试


# ─────────────────────────────────────────────────────────────────────
# 5. ④ 保留策略护栏
# ─────────────────────────────────────────────────────────────────────
def test_retention_never_deletes_manual_backup():
    items = [auto_item(day) for day in range(1, 9)] + [manual_item()]
    cloud = FakeCloud(items)
    summary = retention_mod.cleanup_remote_backups(
        cloud,
        "/B/每日",
        {"daily": 7, "weekly": 0, "monthly": 0},
    )

    assert manual_item()["path"] not in cloud.deleted
    assert all("automatic_backup" in p for p in cloud.deleted)
    assert summary["deleted"] == len(cloud.deleted)
    assert summary["protected"] >= 1


def test_retention_cap_deletes_progressively():
    """P1-2：上限不再是"超了就一个都不删"，而是每轮删 N 个、剩余下次继续。"""
    items = [auto_item(day) for day in range(1, 15)]     # 14 个自动，daily=7
    cloud = FakeCloud(items)
    summary = retention_mod.cleanup_remote_backups(
        cloud,
        "/B/每日",
        {"daily": 7, "weekly": 0, "monthly": 0, "max_delete_per_run": 2},
    )

    assert len(cloud.deleted) == 2                       # 只删 2 个（最旧优先）
    assert summary["deleted"] == 2
    assert summary["candidates"] == 7
    assert summary["remaining"] == 5
    assert summary["guard"] and "剩余 5" in summary["guard"]
    assert "一个都不删" not in (summary["guard"] or "")
    oldest = sorted(items, key=lambda i: i["server_mtime"])[:2]
    assert cloud.deleted == [i["path"] for i in oldest]


def test_retention_dry_run_deletes_nothing():
    items = [auto_item(day) for day in range(1, 9)]
    cloud = FakeCloud(items)
    summary = retention_mod.cleanup_remote_backups(
        cloud,
        "/B/每日",
        {"daily": 7, "weekly": 0, "monthly": 0, "dry_run": True},
    )

    assert cloud.deleted == []
    assert summary["guard"] and "dry-run" in summary["guard"]
    assert summary["remaining"] == summary["candidates"] == 1


def test_retention_list_failure_raises_instead_of_deleting():
    cloud = FakeCloud([auto_item(1)])
    cloud.list_error = RuntimeError("list down")
    with pytest.raises(RuntimeError):
        retention_mod.cleanup_remote_backups(
            cloud, "/B/每日", {"daily": 1, "weekly": 0, "monthly": 0}
        )
    assert cloud.deleted == []


# ─────────────────────────────────────────────────────────────────────
# 6. ⑥ 空 /backup 语义
# ─────────────────────────────────────────────────────────────────────
def test_empty_backup_dir_is_neither_success_nor_failure(tmp_path, monkeypatch):
    empty = tmp_path / "backup"
    empty.mkdir()
    monkeypatch.setattr(sync_mod, "BACKUP_DIR", str(empty))

    result = sync_mod.sync_all_backups(FakeCloud(), "/B")

    assert result["outcome"] == "empty"
    assert result["success"] is False
    assert result["error"] is None
    assert result["total_count"] == 0

    events = record_notifications(monkeypatch)
    main_mod._notify_sync_result(result, {"enabled": True}, "/B", [], "schedule")
    assert [e for e, _ in events] == ["backup_empty"]


def test_missing_backup_dir_is_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_mod, "BACKUP_DIR", str(tmp_path / "nope"))
    result = sync_mod.sync_all_backups(FakeCloud(), "/B")

    assert result["outcome"] == "failed"
    assert result["error"]

    events = record_notifications(monkeypatch)
    main_mod._notify_sync_result(result, {"enabled": True}, "/B", [], "schedule")
    assert [e for e, _ in events] == ["backup_failure"]


# ─────────────────────────────────────────────────────────────────────
# 7. 缓存与远端脱节后的自愈
# ─────────────────────────────────────────────────────────────────────
def test_cache_fresh_skips_without_listing(monkeypatch, sandbox):
    client = make_client(sandbox)
    target = sandbox / "automatic_backup_2026_11_25_04.45_0001.tar"
    target.write_bytes(b"0123456789")
    client._mark_uploaded(str(target), "/B/每日")

    called = {"n": 0}

    def boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("不应发起 list")

    monkeypatch.setattr(client, "list_remote_files", boom)
    need, reason = client.needs_upload(str(target), "/B/每日", recheck_days=7.0)
    assert (need, reason) == (False, "cache-fresh")
    assert called["n"] == 0


def test_stale_cache_reaploads_when_remote_missing(monkeypatch, sandbox):
    client = make_client(sandbox)
    target = sandbox / "automatic_backup_2026_11_25_04.45_0001.tar"
    target.write_bytes(b"0123456789")
    client._mark_uploaded(str(target), "/B/每日")
    key = client._cache_key(str(target))
    client._cache_meta[key] = {"dir": "/B/每日", "verified": 0}   # 已过期
    client._save_cache_meta()

    monkeypatch.setattr(client, "list_remote_files", lambda *a, **k: [])
    need, reason = client.needs_upload(str(target), "/B/每日", recheck_days=7.0)
    assert (need, reason) == (True, "remote-missing")
    assert key not in client._upload_cache
    assert key not in client._cache_meta


def test_stale_cache_confirms_when_remote_present(monkeypatch, sandbox):
    client = make_client(sandbox)
    target = sandbox / "automatic_backup_2026_11_25_04.45_0001.tar"
    target.write_bytes(b"0123456789")
    client._mark_uploaded(str(target), "/B/每日")
    key = client._cache_key(str(target))
    client._cache_meta[key] = {"dir": "/B/每日", "verified": 0}
    client._save_cache_meta()

    items = [{"server_filename": target.name, "path": "/B/每日/" + target.name}]
    monkeypatch.setattr(client, "list_remote_files", lambda *a, **k: items)
    need, reason = client.needs_upload(str(target), "/B/每日", recheck_days=7.0)
    assert (need, reason) == (False, "remote-confirmed")
    assert client._cache_meta[key]["verified"] > 0


def test_list_failure_treated_as_unknown_and_reuploads(monkeypatch, sandbox):
    client = make_client(sandbox)
    target = sandbox / "automatic_backup_2026_11_25_04.45_0001.tar"
    target.write_bytes(b"0123456789")
    client._mark_uploaded(str(target), "/B/每日")
    key = client._cache_key(str(target))
    client._cache_meta[key] = {"dir": "/B/每日", "verified": 0}
    client._save_cache_meta()

    def boom(*args, **kwargs):
        raise client_mod.RemoteListError("list down")

    monkeypatch.setattr(client, "list_remote_files", boom)
    need, reason = client.needs_upload(str(target), "/B/每日", recheck_days=7.0)
    assert (need, reason) == (True, "remote-unknown")


# ─────────────────────────────────────────────────────────────────────
# 8. 消息自洽 + 配置兼容
# ─────────────────────────────────────────────────────────────────────
def test_failure_message_numbers_are_consistent():
    payload = {
        "total_count": 3,
        "uploaded_count": 2,
        "skipped_count": 1,
        "failed_count": 1,
        "error": "1/3 个文件同步失败",
        "failed_files": [{"name": "a.tar", "error": "precreate: 预算耗尽"}],
    }
    msg = notifier_mod._format_event_message("backup_failure", payload)
    content = msg["content"]

    assert "失败：1" in content
    assert "1/3 个文件未完成上传" in content
    assert "成功上传" not in content
    assert "a.tar" in content


def test_failure_message_for_post_stage_error_has_no_zero_counter():
    payload = {"error": "列举失败", "stage": "清单生成"}
    content = notifier_mod._format_event_message("backup_failure", payload)["content"]
    assert "文件上传已完成，失败发生在后续步骤" in content
    assert "失败：0" not in content


def test_success_message_does_not_subtract_skipped():
    payload = {
        "total_count": 3,
        "uploaded_count": 1,
        "skipped_count": 2,
        "failed_count": 0,
    }
    content = notifier_mod._format_event_message("backup_success", payload)["content"]
    assert "失败：0" in content
    assert "本次上传：1" in content
    assert "跳过（已上传）：2" in content
    assert "不一致" not in content


def test_backup_empty_event_is_enabled_by_default(monkeypatch):
    """P1-8：空目录是异常态，默认必须提示（v1.3.0 默认关 = 静默）。"""
    sent = []
    monkeypatch.setitem(
        notifier_mod._CHANNEL_SENDERS, "wechat", lambda cfg, t, c: sent.append(t) or True
    )
    notifier_mod.notify_event(
        {"enabled": True, "channels": {"wechat": {"enabled": True, "webhook_key": "k"}}},
        "backup_empty",
        {"upload_path": "/B"},
    )
    assert len(sent) == 1

    # 显式关掉时必须真的不发（配置仍然生效）
    sent.clear()
    notifier_mod.notify_event(
        {
            "enabled": True,
            "events": {"backup_empty": False},
            "channels": {"wechat": {"enabled": True, "webhook_key": "k"}},
        },
        "backup_empty",
        {"upload_path": "/B"},
    )
    assert sent == []


OLD_OPTIONS_V127 = {
    "refresh_token": REFRESH_TOKEN,
    "upload_path": "/HomeAssistant/Backup",
    "schedule": "0 5 * * *",
    "retention": {"use_folders": True, "daily": 7, "weekly": 4, "monthly": 12},
    "notifications": {
        "enabled": True,
        "events": {"backup_failure": True},
        "channels": {"wechat": {"enabled": True, "webhook_key": "k"}},
    },
}


def test_old_options_json_still_runs():
    cfg = main_mod.parse_options(OLD_OPTIONS_V127)

    assert cfg["upload_path"] == "/HomeAssistant/Backup"
    assert cfg["cron"].expr == "0 5 * * *"
    assert cfg["retention"]["daily"] == 7
    assert cfg["retention"]["protect_manual"] is True
    assert cfg["retention"]["max_delete_per_run"] == 10
    assert cfg["retention"]["dry_run"] is False
    assert cfg["retry_deadline"] == 90.0
    assert cfg["cycle_budget"] == 1800.0
    assert cfg["recheck_days"] == 7.0


def test_garbage_options_do_not_crash():
    cfg = main_mod.parse_options(
        {
            "retention": "not-a-dict",
            "retry": 5,
            "sync": [],
            "schedule": "bad cron",
            "notifications": None,
        }
    )
    assert cfg["retention"]["protect_manual"] is True
    assert cfg["notifications"] == {}
    assert cfg["cron"].expr == "0 5 * * *"      # parse_cron 的既有回退行为


# ═════════════════════════════════════════════════════════════════════
# 9. v1.3.1 新增：D1 run_sync_cycle 端到端
# ═════════════════════════════════════════════════════════════════════
def _prepare_backup_dir(tmp_path, monkeypatch, count=1):
    backup = tmp_path / "backup"
    backup.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        make_tar(backup, "automatic_backup_2026_11_25_04.%02d_%04d.tar" % (45 - i, i))
    monkeypatch.setattr(sync_mod, "BACKUP_DIR", str(backup))
    return backup


def test_run_sync_cycle_upload_failure_sends_exactly_one_notification(
    tmp_path, monkeypatch
):
    _prepare_backup_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(
        retention_mod, "_MIGRATION_FLAG_FILE", str(tmp_path / "migration_done.flag")
    )
    cloud = CycleCloud(upload_error=client_mod.UploadError("precreate: 预算耗尽"))
    events = record_notifications(monkeypatch)

    result = main_mod.run_sync_cycle(cloud, cycle_cfg(), "schedule", budget_seconds=600)

    assert result["outcome"] == "failed"
    assert result["failed_count"] == 1
    assert [e for e, _ in events] == ["backup_failure"]
    assert events[0][1]["failed_count"] == 1
    assert events[0][1]["trigger"] == "schedule"


def test_run_sync_cycle_retention_error_becomes_failure_without_success(
    tmp_path, monkeypatch
):
    """D1 后半：retention 抛异常 → 也是 failure，且不再补发一条 success。"""
    _prepare_backup_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(
        retention_mod, "_MIGRATION_FLAG_FILE", str(tmp_path / "migration_done.flag")
    )
    # 只有 retention 的顶层 list 会失败（sync 与 manifest 不 list 顶层）
    cloud = CycleCloud(list_error_dirs=["/B"])
    events = record_notifications(monkeypatch)

    result = main_mod.run_sync_cycle(cloud, cycle_cfg(), "schedule", budget_seconds=600)

    assert result["outcome"] == "ok"           # 上传本身成功了
    assert [e for e, _ in events] == ["backup_failure"]      # 但只发一条，且是失败
    payload = events[0][1]
    assert payload["outcome"] == "partial"
    assert "远端保留策略" in payload["stage"]
    content = notifier_mod._format_event_message("backup_failure", payload)["content"]
    assert "文件上传已完成，失败发生在后续步骤" in content


def test_guard_is_folded_into_single_success_notification(monkeypatch):
    """E5：护栏 + 成功不再发两条通知；主事件被关掉时用 retention_guard 兜底。"""
    result = {
        "outcome": "ok",
        "success": True,
        "total_count": 1,
        "uploaded_count": 1,
        "skipped_count": 0,
        "failed_count": 0,
    }
    guard = "Retention folders: 本轮删除 1 个（单次上限 1），剩余 3 个留到下次继续"

    events = record_notifications(monkeypatch)
    event = main_mod._notify_sync_result(
        result, {"enabled": True}, "/B", [], "schedule", [guard]
    )
    assert event == "backup_success"
    assert [e for e, _ in events] == ["backup_success"]        # 恰好一条
    content = notifier_mod._format_event_message("backup_success", events[0][1])["content"]
    assert "剩余 3 个" in content                              # 护栏说明没有丢

    # 主事件被用户关掉 → 用 retention_guard 兜底，仍然只有一条，且不静默
    events.clear()
    event2 = main_mod._notify_sync_result(
        result, {"enabled": True, "events": {"backup_success": False}}, "/B", [], "schedule", [guard]
    )
    assert event2 == "retention_guard"
    assert [e for e, _ in events] == ["retention_guard"]


# ═════════════════════════════════════════════════════════════════════
# 10. D2 folder 模式：手工备份是最旧的一份
# ═════════════════════════════════════════════════════════════════════
def test_retention_folder_mode_never_deletes_manual_backup():
    autos = [auto_dash(datetime(2026, 11, 5 + i, 4, 45), seq=i) for i in range(8)]
    manual = manual_item(datetime(2026, 11, 3, 16, 1))     # 最旧的一份
    cloud = DirCloud({"/B/每日": autos + [manual]})

    summary = retention_mod.retention_folder_mode(
        cloud, "/B", {"daily": 7, "weekly": 0, "monthly": 0}
    )

    assert manual["path"] not in cloud.deleted
    assert len(cloud.deleted) == 1                      # 只淘汰最老的那份自动备份
    assert summary["deleted"] == 1
    assert summary["protected"] == 1

    # 反向对照：关掉护栏时，手工备份会进桶并一起被删（证明这条用例真的测到了护栏）
    cloud2 = DirCloud({"/B/每日": autos + [manual]})
    retention_mod.retention_folder_mode(
        cloud2, "/B", {"daily": 7, "weekly": 0, "monthly": 0, "protect_manual": False}
    )
    assert manual["path"] in cloud2.deleted
    assert len(cloud2.deleted) == 2


def test_promotion_move_failure_keeps_daily_cache():
    """E4：晋升移动失败后不能把文件从两个视图里同时抹掉。"""
    old = auto_dash(datetime(2026, 9, 20, 4, 45), 1)      # 上个月
    new = auto_dash(datetime(2026, 11, 10, 4, 45), 2)     # 本月
    cloud = DirCloud({"/B/每日": [old, new]})
    cloud.move_ok = False

    summary = retention_mod.retention_folder_mode(
        cloud, "/B", {"daily": 1, "weekly": 0, "monthly": 12}
    )

    # 移动失败 → 两份都还留在 daily 视图里 → daily=1 只保留最新，最旧的成为候选
    assert cloud.moves, "应该尝试过晋升"
    assert summary["candidates"] == 1
    assert cloud.deleted == [old["path"]]
    assert "失败" in (summary["guard"] or "")


# ═════════════════════════════════════════════════════════════════════
# 11. D3 多分片 + 持续失败 → cycle deadline 有界
# ═════════════════════════════════════════════════════════════════════
def test_many_slices_respect_cycle_deadline(monkeypatch, sandbox):
    monkeypatch.setattr(client_mod, "CHUNK_SIZE", 1024)     # 80KB → ≥80 片
    attempts = {}
    chunk_posts = {"n": 0}

    def fake_post(url, **kwargs):
        if "method=precreate" in url:
            return precreate_ok(url)
        if "method=create" in url:
            return merge_ok(url)
        if "superfile2" in url:
            seq = int(re.search(r"partseq=(\d+)", url).group(1))
            attempts[seq] = attempts.get(seq, 0) + 1
            chunk_posts["n"] += 1
            if attempts[seq] <= 2:            # 每片都先失败两次再成功
                raise FakeTimeout("chunk connect timed out")
            return chunk_ok(url)
        raise AssertionError("未预期的 POST：%s" % url)

    monkeypatch.setattr(client_mod.requests, "post", fake_post, raising=False)
    monkeypatch.setattr(client_mod.requests, "get", _no_transport, raising=False)

    client = make_client(sandbox, deadline=90.0)
    target = sandbox / "big.tar"
    target.write_bytes(b"0" * (80 * 1024))

    clock = client_mod.time
    started = clock.monotonic()
    cycle_deadline = started + 60.0

    with pytest.raises(client_mod.UploadError) as excinfo:
        client.upload_file(str(target), "/B", deadline=cycle_deadline)

    elapsed = clock.monotonic() - started
    assert elapsed <= 60.0 + 0.001, "单文件拖穿了 cycle deadline：%s 秒" % elapsed
    assert "预算" in str(excinfo.value)
    assert chunk_posts["n"] < 80, "没有在 deadline 处中止，仍发了 %d 个分片" % chunk_posts["n"]


def test_cycle_deadline_stops_many_successful_slices(monkeypatch, sandbox):
    """每片都成功、每片耗 1s：80 片必须被 cycle deadline 截断（变异 M5 的杀手）。

    为什么需要这条：终审的变异 M5 把 ``_do_upload_sliced`` 里每片前的
    ``_check_cycle_deadline`` 删掉，49 条用例**全绿**——``test_many_slices_
    respect_cycle_deadline`` 是被 ``retry_call`` 的 op_deadline 触发的（每片先
    失败两次、重试预算耗尽），从没走过"每片都成功、但累计时间超过 cycle
    deadline"这条路径。而现场最可能的形态正是这个：网速慢，每片都成功，83 片
    的一片 tar 把 1800s 的轮次拖成 2.3 小时。

    期望（终审实测的同一组数字）：60s deadline、80 片 × 1s →
      * 抛 ``UploadError``（等价的可读失败，而不是"成功"）；
      * 实际发出的分片 ≤ 62（必须在 deadline 处停，而不是发满 80）；
      * 总耗时 ≤ 65s（有上界，不能是 80s）。
    """
    monkeypatch.setattr(client_mod, "CHUNK_SIZE", 1024)      # 80KB → 80 片
    slices = {"attempted": 0, "ok": 0}

    def fake_post(url, **kwargs):
        if "method=precreate" in url:
            return precreate_ok(url)
        if "method=create" in url:
            return merge_ok(url)
        if "superfile2" in url:
            slices["attempted"] += 1
            # 每片都**成功**，但耗时 1s。用被测代码自己的（假）时钟推进，
            # 否则这条用例要真跑 60s。
            client_mod.time.sleep(1.0)
            slices["ok"] += 1
            return chunk_ok(url)
        raise AssertionError("未预期的 POST：%s" % url)

    monkeypatch.setattr(client_mod.requests, "post", fake_post, raising=False)
    monkeypatch.setattr(client_mod.requests, "get", _no_transport, raising=False)

    client = make_client(sandbox, deadline=90.0)
    target = sandbox / "big.tar"
    target.write_bytes(b"0" * (80 * 1024))

    clock = client_mod.time
    started = clock.monotonic()

    with pytest.raises(client_mod.UploadError) as excinfo:
        client.upload_file(str(target), "/B", deadline=started + 60.0)

    elapsed = clock.monotonic() - started
    assert "预算" in str(excinfo.value), (
        "失败原因不是 cycle deadline（应含\"预算\"）：%s" % excinfo.value
    )
    assert slices["ok"] == slices["attempted"], "本用例要求每片都成功，不应有重试失败"
    assert slices["attempted"] <= 62, (
        "cycle deadline 到达后仍在发分片：发了 %d 片（期望 ≤ 62）"
        % slices["attempted"]
    )
    assert slices["attempted"] < 80, (
        "80 片全发完了 —— 每片前的 _check_cycle_deadline 没有生效"
    )
    assert elapsed <= 65.0, "总耗时 %s 秒超过 65s 上界（cycle deadline 是 60s）" % elapsed


# ═════════════════════════════════════════════════════════════════════
# 12. D4 _cycle_budget
# ═════════════════════════════════════════════════════════════════════
def test_cycle_budget_daily_and_five_minute_cron():
    daily = {"cycle_budget": 1800.0, "cron": main_mod.CronSchedule("0 5 * * *")}
    every5 = {"cycle_budget": 1800.0, "cron": main_mod.CronSchedule("*/5 * * * *")}

    # 日 cron：刚跑完时下一次在 24h 外 → 不收缩
    assert main_mod._cycle_budget(
        daily, "schedule", datetime(2026, 11, 25, 5, 0, 0)
    ) == 1800.0
    # 5 分钟 cron：距下一次 300s，减去 120s 保护带 → 180s
    assert main_mod._cycle_budget(
        every5, "schedule", datetime(2026, 11, 25, 5, 0, 0)
    ) == 180.0
    # 5 分钟 cron：距下一次 270s，减去 120s 保护带 → 150s
    assert main_mod._cycle_budget(
        every5, "schedule", datetime(2026, 11, 25, 5, 0, 30)
    ) == 150.0
    # 手动触发不收缩（否则 04:50 点一次会先给一条"预算耗尽"失败）
    assert main_mod._cycle_budget(
        daily, "manual", datetime(2026, 11, 25, 4, 50, 0)
    ) == 1800.0


# ═════════════════════════════════════════════════════════════════════
# 13. D5 token 变更
# ═════════════════════════════════════════════════════════════════════
def test_apply_token_if_changed_next_upload_uses_new_token(monkeypatch, sandbox):
    transport = Transport()
    transport.get_plan = [token_ok]
    transport.post_plan = [precreate_ok, chunk_ok, merge_ok]
    install_transport(monkeypatch, transport)

    client = make_client(sandbox)
    assert client.access_token == ACCESS_TOKEN

    main_mod._apply_token_if_changed(client, {"refresh_token": NEW_REFRESH_TOKEN})

    assert client.initial_refresh_token == NEW_REFRESH_TOKEN
    assert client.access_token == NEW_ACCESS_TOKEN
    refresh_calls = [r for r in transport.requests if r["method"] == "GET"]
    assert refresh_calls[0]["params"]["refresh_token"] == NEW_REFRESH_TOKEN

    target = sandbox / "a.tar"
    target.write_bytes(b"0123456789")
    client.upload_file(str(target), "/B")

    post_urls = [r["url"] for r in transport.requests if r["method"] == "POST"]
    assert post_urls, "没有任何上传请求"
    assert all("access_token=%s" % NEW_ACCESS_TOKEN in u for u in post_urls)


def test_update_refresh_token_is_defined_and_swaps(monkeypatch, sandbox):
    """P1-1：v1.3.0 调用了不存在的方法。这里断言它真的存在且可用。"""
    assert hasattr(client_mod.BaiduClient, "update_refresh_token")

    transport = Transport()
    transport.get_plan = [token_ok]
    install_transport(monkeypatch, transport)
    client = make_client(sandbox)

    assert client.update_refresh_token(NEW_REFRESH_TOKEN) is True
    assert client.refresh_token == NEW_REFRESH_TOKEN
    assert client.access_token == NEW_ACCESS_TOKEN
    # 同一个 token 再调一次是 no-op（不再打网络）
    assert client.update_refresh_token(NEW_REFRESH_TOKEN) is True
    assert len(transport.requests) == 1
    assert client.update_refresh_token("") is False


# ═════════════════════════════════════════════════════════════════════
# 14. D6 锁竞争：排队 + 通知
# ═════════════════════════════════════════════════════════════════════
def test_schedule_lock_contention_queues_instead_of_dropping(monkeypatch):
    events = record_notifications(monkeypatch)
    cloud = CycleCloud()
    cfg = cycle_cfg()

    assert main_mod._sync_lock.acquire(blocking=False)
    try:
        ran = main_mod._run_once(cloud, cfg, trigger="schedule", wait_seconds=0.01)
    finally:
        main_mod._sync_lock.release()

    assert ran is False
    assert [e for e, _ in events] == ["backup_failure"]
    assert events[0][1]["stage"] == "调度"
    assert "排队" in events[0][1]["error"]
    # 排队恰好一次（不叠加），并且能被主循环取走
    assert main_mod.take_deferred_trigger() == "schedule"
    assert main_mod.take_deferred_trigger() is None


# ═════════════════════════════════════════════════════════════════════
# 15. P1-6 缓存旁路文件 + 回滚语义
# ═════════════════════════════════════════════════════════════════════
def test_upload_cache_main_file_stays_bool_with_meta_sidecar(monkeypatch, sandbox):
    client = make_client(sandbox)
    target = sandbox / "a.tar"
    target.write_bytes(b"0123456789")
    client._mark_uploaded(str(target), "/B/每日")

    key = client._cache_key(str(target))
    main_data = json.loads((sandbox / "upload_cache.json").read_text("utf-8"))
    meta_data = json.loads((sandbox / "upload_cache_meta.json").read_text("utf-8"))

    # 主文件的值必须是 true —— 旧代码 dict.get(key, False) 行为与 v1.2.7 完全一致
    assert main_data[key] is True
    assert meta_data["schema"] == client_mod.CACHE_SCHEMA_VERSION
    assert meta_data["entries"][key]["dir"] == "/B/每日"
    assert meta_data["entries"][key]["verified"] > 0
    assert client._is_already_uploaded(str(target)) is True


def test_v130_dict_cache_is_migrated_to_bool_plus_meta(monkeypatch, sandbox):
    """v1.3.0 曾把 dict 写进主文件；升级时必须就地降级（向上可自愈）。"""
    key = "a.tar:10:1793692923"
    (sandbox / "upload_cache.json").write_text(
        json.dumps({key: {"dir": "/B/每日", "verified": 1793692923}}), encoding="utf-8"
    )

    client = make_client(sandbox)

    main_data = json.loads((sandbox / "upload_cache.json").read_text("utf-8"))
    meta_data = json.loads((sandbox / "upload_cache_meta.json").read_text("utf-8"))
    assert main_data[key] is True
    assert meta_data["entries"][key]["dir"] == "/B/每日"
    assert client._upload_cache[key] is True


def test_v127_bool_cache_triggers_remote_recheck(monkeypatch, sandbox):
    """E11：v1.2.7 的 `true` 条目没有核对时间 → 首轮就会核对，云端缺失即重传。

    这也解释了"升级首轮会把 11-12 误删的两份手工备份重新传上去"。
    """
    target = sandbox / "2026nian_11yue_3ri_16_01_03_2026-11-03_16.01_07289880.tar"
    target.write_bytes(b"0123456789")
    key = "%s:10:%d" % (target.name, int(os.stat(target).st_mtime))
    (sandbox / "upload_cache.json").write_text(json.dumps({key: True}), encoding="utf-8")

    client = make_client(sandbox)
    assert client._upload_cache[key] is True
    assert client._meta_for(key) == {}              # 没有核对信息

    monkeypatch.setattr(client, "list_remote_files", lambda *a, **k: [])
    need, reason = client.needs_upload(str(target), "/B/每日", recheck_days=7.0)
    assert (need, reason) == (True, "remote-missing")


# ═════════════════════════════════════════════════════════════════════
# 16. E7 保留策略删除后不重传 / E6 目录变更先核对
# ═════════════════════════════════════════════════════════════════════
def test_retention_deleted_file_is_not_reuploaded(monkeypatch, sandbox):
    client = make_client(sandbox)
    target = sandbox / "automatic_backup_2026_11_25_04.45_0001.tar"
    target.write_bytes(b"0123456789")
    client._mark_uploaded(str(target), "/B/每日")

    touched = client.mark_policy_deleted(["/B/每日/" + target.name])
    assert touched == 1

    listed = {"n": 0}

    def count(*args, **kwargs):
        listed["n"] += 1
        return []

    monkeypatch.setattr(client, "list_remote_files", count)
    need, reason = client.needs_upload(str(target), "/B/每日", recheck_days=0.0)
    assert (need, reason) == (False, "retention-deleted")
    assert listed["n"] == 0            # 连一次 list 都不发


def test_needs_upload_dir_changed_verifies_remote_first(monkeypatch, sandbox):
    client = make_client(sandbox)
    target = sandbox / "automatic_backup_2026_11_25_04.45_0001.tar"
    target.write_bytes(b"0123456789")
    client._mark_uploaded(str(target), "/B/每日")

    calls = []

    def list_new_dir(remote_dir, strict=True):
        calls.append(remote_dir)
        return [{"server_filename": target.name, "path": "/B/新/" + target.name}]

    monkeypatch.setattr(client, "list_remote_files", list_new_dir)
    need, reason = client.needs_upload(str(target), "/B/新", recheck_days=7.0)
    assert (need, reason) == (False, "remote-confirmed")
    assert calls == ["/B/新"]

    # 新目录里确实没有 → 才重传
    client2 = make_client(sandbox)
    target2 = sandbox / "b.tar"
    target2.write_bytes(b"0123456789")
    client2._mark_uploaded(str(target2), "/B/每日")
    monkeypatch.setattr(client2, "list_remote_files", lambda *a, **k: [])
    need2, reason2 = client2.needs_upload(str(target2), "/B/另一个", recheck_days=7.0)
    assert (need2, reason2) == (True, "dir-changed")


# ═════════════════════════════════════════════════════════════════════
# 17. P1-7 protect_manual 第二判据 / P1-9 max_delete=0
# ═════════════════════════════════════════════════════════════════════
def test_protect_manual_requires_name_mtime_agreement():
    base = {
        "server_filename": "automatic_backup_2026-11-03_16.01_9999.tar",
        "path": "/B/每日/automatic_backup_2026-11-03_16.01_9999.tar",
        "size": 100,
        "isdir": 0,
    }
    genuine = dict(base, server_mtime=int(datetime(2026, 11, 3, 16, 1).timestamp()) + 90)
    renamed = dict(base, server_mtime=int(datetime(2026, 9, 1, 12, 0).timestamp()))

    assert retention_mod.is_deletable(genuine) is True
    assert retention_mod.is_deletable(renamed) is False
    # 关掉护栏时不再做名字/mtime 比对（保持"用户明确选择"的语义）
    assert retention_mod.is_deletable(renamed, protect_manual=False) is True


def test_max_delete_zero_does_not_mean_unlimited():
    assert retention_mod._retention_flags({"max_delete_per_run": 0})["max_delete"] == 10
    assert retention_mod._retention_flags({"max_delete_per_run": -5})["max_delete"] == 10
    assert retention_mod._retention_flags({"max_delete_per_run": ""})["max_delete"] == 10
    assert retention_mod._retention_flags({"max_delete_per_run": None})["max_delete"] == 10
    assert retention_mod._retention_flags({"max_delete_per_run": 3})["max_delete"] == 3

    items = [auto_item(day) for day in range(1, 22)]      # 21 个自动，daily=7
    cloud = FakeCloud(items)
    summary = retention_mod.cleanup_remote_backups(
        cloud, "/B/每日", {"daily": 7, "weekly": 0, "monthly": 0, "max_delete_per_run": 0}
    )
    assert len(cloud.deleted) == 10                        # 上限回落到 10，而不是"不限"
    assert summary["remaining"] == 4


# ═════════════════════════════════════════════════════════════════════
# 18. P1-5 dry-run 覆盖 move / 迁移；E3 迁移失败不写 flag
# ═════════════════════════════════════════════════════════════════════
def test_dry_run_covers_folder_moves_and_migration(tmp_path, monkeypatch):
    flag = tmp_path / "migration_done.flag"
    monkeypatch.setattr(retention_mod, "_MIGRATION_FLAG_FILE", str(flag))

    # 迁移：dry-run 不移动、不删旧目录、不写 flag
    cloud = DirCloud({"/B/daily": [auto_dash(datetime(2026, 11, 1, 4, 45), 1)]})
    assert retention_mod.migrate_old_dirs(cloud, "/B", dry_run=True) == []
    assert cloud.moves == []
    assert cloud.deleted == []
    assert cloud.created == []
    assert not flag.exists()

    # folder 模式：dry-run 不搬顶层、不晋升
    cloud2 = DirCloud(
        {
            "/B": [auto_dash(datetime(2026, 10, 30, 4, 45), 2)],
            "/B/每日": [
                auto_dash(datetime(2026, 11, 10, 4, 45), 3),
                auto_dash(datetime(2026, 11, 11, 4, 45), 4),
            ],
        }
    )
    summary = retention_mod.retention_folder_mode(
        cloud2,
        "/B",
        {"daily": 1, "weekly": 0, "monthly": 12, "dry_run": True},
    )
    assert cloud2.moves == []
    assert summary["moves_skipped"] >= 2
    assert "本应被移动" in summary["guard"]


def test_migration_move_failure_does_not_write_flag(tmp_path, monkeypatch):
    flag = tmp_path / "migration_done.flag"
    monkeypatch.setattr(retention_mod, "_MIGRATION_FLAG_FILE", str(flag))

    cloud = DirCloud(
        {
            "/B/daily": [
                auto_dash(datetime(2026, 11, 1, 4, 45), 1),
                auto_dash(datetime(2026, 11, 2, 4, 45), 2),
            ]
        }
    )
    cloud.move_ok = False
    assert retention_mod.migrate_old_dirs(cloud, "/B") == []
    assert not flag.exists()               # E3：move 失败绝不写 flag

    # 对照组：move 成功 → 写 flag
    cloud2 = DirCloud({"/B/daily": [auto_dash(datetime(2026, 11, 1, 4, 45), 1)]})
    migrated = retention_mod.migrate_old_dirs(cloud2, "/B")
    assert migrated and migrated[0]["count"] == 1
    assert flag.exists()


# ═════════════════════════════════════════════════════════════════════
# 19. P1-8 空目录跳过 retention
# ═════════════════════════════════════════════════════════════════════
def test_empty_backup_dir_skips_retention(tmp_path, monkeypatch):
    empty = tmp_path / "backup"
    empty.mkdir()
    monkeypatch.setattr(sync_mod, "BACKUP_DIR", str(empty))
    monkeypatch.setattr(
        retention_mod, "_MIGRATION_FLAG_FILE", str(tmp_path / "migration_done.flag")
    )
    cloud = CycleCloud(
        dirs={
            "/B/每日": [
                auto_dash(datetime(2026, 11, 1, 4, 45), i) for i in range(1, 12)
            ]
        }
    )
    events = record_notifications(monkeypatch)

    result = main_mod.run_sync_cycle(cloud, cycle_cfg(), "schedule", budget_seconds=600)

    assert result["outcome"] == "empty"
    assert cloud.deleted == []
    assert cloud.moves == []
    assert cloud.created == []          # retention_folder_mode 一次都没进来
    assert [e for e, _ in events] == ["backup_empty"]


def test_folder_round_makes_seven_structural_list_calls(tmp_path, monkeypatch):
    """A4：默认 folder 模式的 list 次数是结构性的 7 次/轮，不是 0 次。

    现场日志 77 次 / 11 轮 = 7.0；v1.3.0 的验收标准写成"正常每天 0 次"是错的。
    """
    _prepare_backup_dir(tmp_path, monkeypatch)
    flag = tmp_path / "migration_done.flag"
    flag.write_bytes(b"done\n")                        # 迁移已做过 → 不额外 list 旧目录
    monkeypatch.setattr(retention_mod, "_MIGRATION_FLAG_FILE", str(flag))
    cloud = CycleCloud()
    record_notifications(monkeypatch)

    main_mod.run_sync_cycle(cloud, cycle_cfg(), "schedule", budget_seconds=600)

    # 顶层 1 + 每日/每周/每月 3 + 清单三档 3 = 7
    assert cloud.list_calls == 7


# ═════════════════════════════════════════════════════════════════════
# 20. E12 部分批次删除如实报告
# ═════════════════════════════════════════════════════════════════════
def test_partial_batch_delete_is_reported_as_partial():
    class PartialCloud(FakeCloud):
        def delete_remote_files_detailed(self, paths):
            self.deleted.extend(paths)
            return {
                "ok": len(paths) - 1,
                "failed": 1,
                "failed_batches": 1,
                "requested": len(paths),
            }

    items = [auto_item(day) for day in range(1, 11)]        # daily=7 → 3 个候选
    cloud = PartialCloud(items)
    summary = retention_mod.cleanup_remote_backups(
        cloud, "/B/每日", {"daily": 7, "weekly": 0, "monthly": 0}
    )

    assert summary["deleted"] == 2
    assert summary["failed"] == 1
    assert "已删除 2" in summary["guard"]
    assert "失败" in summary["guard"]
    assert "跳过删除" not in summary["guard"]


# ═════════════════════════════════════════════════════════════════════
# 21. P1-10 Web UI 保存：API 失败不得谎报成功
# ═════════════════════════════════════════════════════════════════════
def _serve_web(monkeypatch, tmp_path):
    from http.server import ThreadingHTTPServer

    monkeypatch.setattr(web_mod, "CONFIG_PATH", str(tmp_path / "options.json"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web_mod._Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


def _post_json(port, path, payload):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_web_save_reports_api_failure_honestly(tmp_path, monkeypatch):
    class Boom:
        status_code = 500

        def json(self):
            return {}

    monkeypatch.setattr(web_mod, "_get_supervisor_token", lambda: "fake-supervisor-token")
    monkeypatch.setattr(web_mod.requests, "post", lambda *a, **k: Boom())

    saved = web_mod._save_options({"refresh_token": REFRESH_TOKEN})
    assert saved["api_ok"] is False
    assert saved["file_ok"] is True
    assert "500" in saved["api_error"]

    srv, port = _serve_web(monkeypatch, tmp_path)
    try:
        body = _post_json(port, "/api/config", {"refresh_token": REFRESH_TOKEN})
    finally:
        srv.shutdown()
        srv.server_close()

    assert body["ok"] is False
    assert "重启" in body["message"] and "回退" in body["message"]
    assert "已保存并立即生效" not in body["message"]


def test_web_save_reports_success_when_api_ok(tmp_path, monkeypatch):
    class Ok:
        status_code = 200

        def json(self):
            return {}

    monkeypatch.setattr(web_mod, "_get_supervisor_token", lambda: "fake-supervisor-token")
    monkeypatch.setattr(web_mod.requests, "post", lambda *a, **k: Ok())

    srv, port = _serve_web(monkeypatch, tmp_path)
    try:
        body = _post_json(port, "/api/config", {"refresh_token": REFRESH_TOKEN})
    finally:
        srv.shutdown()
        srv.server_close()

    assert body["ok"] is True
    assert "重启后仍然有效" in body["message"]


def test_manual_sync_has_minimum_interval(monkeypatch):
    monkeypatch.setattr(web_mod, "_manual_sync_last_at", None, raising=False)
    with web_mod._status_lock:
        web_mod._sync_status["running"] = False
    web_mod._manual_sync_event.clear()

    first = web_mod.request_manual_sync(min_interval=600)
    second = web_mod.request_manual_sync(min_interval=600)

    assert first["ok"] is True
    assert second["ok"] is False
    assert "频繁" in second["message"]

    web_mod._manual_sync_event.clear()
    monkeypatch.setattr(web_mod, "_manual_sync_last_at", None, raising=False)


# ─────────────────────────────────────────────────────────────────────
# 真机冒烟 2026-11-25 11:05 抓到：errno=-6 被 "path" in res 误判成「目录已存在」
# ─────────────────────────────────────────────────────────────────────
def test_create_dir_errno_minus6_is_not_treated_as_exists(monkeypatch, sandbox, capsys):
    """真实百度对无效 token 返回 {'errno': -6, 'path': '', 'request_id': ...}。

    v1.2.7 的启发式 `"path" in res` 会把它判成「目录已存在」并返回 True，
    从而把鉴权失败静默掉。锁死：判失败、只尝试一次、且日志里不得说 exists。
    """
    transport = Transport()
    transport.post_plan = [
        lambda url: Resp({"errno": -6, "path": "", "request_id": 8770893026094533252})
    ]
    install_transport(monkeypatch, transport)

    client = client_mod.BaiduClient(REFRESH_TOKEN, retry_deadline=20.0)
    assert client.create_remote_dir("/HomeAssistant/_smoke/每日") is False
    assert len(transport.requests) == 1, "永久错误不应重试"

    out = capsys.readouterr().out
    assert "create dir errno=-6" in out, "失败原因必须带出 errno"
    assert "Directory exists" not in out, "errno=-6 不得被判成目录已存在"


def test_create_dir_category_only_still_means_exists(monkeypatch, sandbox, capsys):
    """对照组：真实「已存在」响应是 {'category': 6}（无 errno），必须仍判成功。"""
    transport = Transport()
    transport.post_plan = [lambda url: Resp({"category": 6})]
    install_transport(monkeypatch, transport)

    client = client_mod.BaiduClient(REFRESH_TOKEN, retry_deadline=20.0)
    assert client.create_remote_dir("/HomeAssistant/Backup") is True
    assert len(transport.requests) == 1

    out = capsys.readouterr().out
    assert "Directory exists (non-standard response, no errno)" in out
