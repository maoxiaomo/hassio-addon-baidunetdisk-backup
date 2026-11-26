#!/usr/bin/env python3
"""Web UI — 配置编辑 + 通知测试（中文界面）。"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

import requests

from client import log
from notifier import test_notification

CONFIG_PATH = "/data/options.json"
CHANNELS = ["email", "wechat", "dingtalk", "feishu"]
CHANNEL_LABELS = {
    "email": "邮箱 (SMTP)",
    "wechat": "企业微信机器人",
    "dingtalk": "钉钉机器人",
    "feishu": "飞书机器人",
}

# 热加载：主进程可通过此事件感知配置变更
_config_reload_event = threading.Event()

# v1.3.0：Web UI 刚提交的 options（热加载优先用它，避免回读到旧文件）
_pending_options: Optional[Dict[str, Any]] = None
_pending_lock = threading.Lock()

# v1.3.0：手动同步请求 + 运行状态
_manual_sync_event = threading.Event()
_status_lock = threading.Lock()
_sync_status: Dict[str, Any] = {
    "running": False,
    "trigger": None,
    "started": None,
    "finished": None,
    "outcome": None,
    "failed_count": 0,
}


def take_pending_options() -> Optional[Dict[str, Any]]:
    """取走"Web UI 刚保存的那份配置"，没有则返回 None。"""
    global _pending_options
    with _pending_lock:
        opts = _pending_options
        _pending_options = None
    return opts


def publish_sync_status(partial: Dict[str, Any]) -> None:
    """主进程回写同步状态（供页面展示）。"""
    with _status_lock:
        _sync_status.update(partial or {})


def get_sync_status() -> Dict[str, Any]:
    with _status_lock:
        return dict(_sync_status)


# 手动同步的最小间隔（秒）：防止"同步中再点一次 → 本轮结束立刻又跑一整轮"
MANUAL_SYNC_MIN_INTERVAL: float = 600.0
_manual_sync_last_at: Optional[float] = None


def request_manual_sync(
    min_interval: float = MANUAL_SYNC_MIN_INTERVAL,
) -> Dict[str, Any]:
    """请求一次手动同步。

    Returns:
        ``{"ok": bool, "message": str}`` —— 正在运行 / 过于频繁 / 已排队。

    v1.3.1（P1-3）：加最小间隔。v1.3.0 只挡"running"，同步中再点一次会把
    Event 置位，本轮一结束立刻又跑一整轮，无节流。
    """
    global _manual_sync_last_at
    now = time.monotonic()
    with _status_lock:
        if _sync_status.get("running"):
            return {"ok": False, "message": "已有同步任务在运行，请稍后再试"}
        if _manual_sync_last_at is not None and (now - _manual_sync_last_at) < min_interval:
            wait = int(min_interval - (now - _manual_sync_last_at)) + 1
            return {
                "ok": False,
                "message": f"手动同步过于频繁，请 {wait} 秒后再试（最小间隔 {int(min_interval)} 秒）",
            }
        _manual_sync_last_at = now
        _manual_sync_event.set()
    return {"ok": True, "message": "已排队，将在数秒内开始同步"}


def wait_for_manual_sync(timeout: float) -> bool:
    """阻塞至多 *timeout* 秒；被手动请求唤醒时返回 True。"""
    fired = _manual_sync_event.wait(timeout)
    if fired:
        _manual_sync_event.clear()
    return fired


def register_config_reload_callback(callback) -> None:
    """供 main.py 注册配置热加载回调。"""
    def _watcher():
        while True:
            _config_reload_event.wait()
            _config_reload_event.clear()
            try:
                callback()
            except Exception as e:
                log(f"配置热加载失败: {e}")
    t = threading.Thread(target=_watcher, name="config-reload", daemon=True)
    t.start()


def _load_options() -> Dict[str, Any]:
    """Load options: Supervisor API first (authoritative), fallback to file."""
    token = _get_supervisor_token()
    if token:
        try:
            r = requests.get(
                "http://supervisor/addons/self/info",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
            log(f"[debug] GET /addons/self/info → {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                opts = data.get("data", {}).get("options", {})
                log(f"[debug] API 返回 {len(opts)} 个配置项")
                if opts:
                    return opts
        except Exception as e:
            log(f"[debug] API 读取失败: {e}")
    # fallback
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _get_supervisor_token() -> str:
    """Get SUPERVISOR_TOKEN: env → s6 container_environment → /proc/1/environ."""
    # 1. 进程自身环境
    token = os.environ.get("SUPERVISOR_TOKEN", "") or os.environ.get("HASSIO_TOKEN", "")
    if token:
        return token

    # 2. s6-overlay 容器环境文件（Supervisor 注入的 Docker env var 被 s6 存储在此）
    for var_name in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
        for env_dir in ("/run/s6/container_environment", "/var/run/s6/container_environment"):
            try:
                fpath = os.path.join(env_dir, var_name)
                with open(fpath, "r") as f:
                    val = f.read().strip()
                    if val:
                        return val
            except Exception:
                pass

    # 3. PID 1 环境
    try:
        with open("/proc/1/environ", "rb") as f:
            for part in f.read().split(b"\x00"):
                if part.startswith(b"SUPERVISOR_TOKEN=") or part.startswith(b"HASSIO_TOKEN="):
                    val = part.split(b"=", 1)[1].decode("utf-8", errors="ignore")
                    if val:
                        return val
    except Exception:
        pass

    return ""


def _write_options_file(opts: Dict[str, Any]) -> None:
    """原子写入 /data/options.json。"""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(opts, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def _save_options(opts: Dict[str, Any]) -> Dict[str, Any]:
    """Save options: Supervisor API + 本地落盘 + 内存 pending（三者都做）。

    Returns:
        ``{"api_ok": bool, "file_ok": bool, "api_error": str}``

    v1.3.1（P1-10）：API 失败不得回"已保存并立即生效"。Supervisor 只在重启/重建
    时用自己那份 options 覆盖 /data/options.json，所以"仅本地生效"必须写明
    "重启会回退"。
    """
    global _pending_options
    token = _get_supervisor_token()
    api_ok = False
    api_error = ""
    if token:
        log(f"Web UI: 检测到 Supervisor Token（{len(token)} 字符），尝试 API 同步")
        try:
            r = requests.post(
                "http://supervisor/addons/self/options",
                headers={"Authorization": f"Bearer {token}"},
                json={"options": opts},
                timeout=10,
            )
            if r.status_code == 200:
                api_ok = True
                log("Web UI: 配置已同步保存（HA 原生设置页已更新）")
            else:
                api_error = f"Supervisor API 返回 {r.status_code}"
                log(f"Web UI: {api_error}，改用文件写入")
        except Exception as e:
            api_error = f"Supervisor API 异常：{e}"
            log(f"Web UI: {api_error}，改用文件写入")
    else:
        api_error = "未找到 Supervisor Token"
        log("Web UI: 未找到 Supervisor Token，直接写文件（HA 原生设置页不会同步）")

    # 无论 API 是否成功，都把同一份 payload 落到本地文件：
    # 这样 load_config() 读文件时不会拿到旧值。
    file_ok = False
    try:
        _write_options_file(opts)
        file_ok = True
        if api_ok:
            log("Web UI: 配置已同步写入 options.json（与 API 内容一致）")
        else:
            log("Web UI: 配置已保存到文件（仅本地生效，重建加载项后会回退）")
    except Exception as e:
        if not api_ok:
            raise
        log(f"Web UI: 本地文件写入失败（API 已成功）：{e}")

    with _pending_lock:
        _pending_options = dict(opts)
    _config_reload_event.set()
    return {"api_ok": api_ok, "file_ok": file_ok, "api_error": api_error}




_HTML = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>百度网盘备份</title>
<style>
:root{color-scheme:dark}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
     max-width:860px;margin:20px auto;padding:0 16px;line-height:1.55;
     background:#0d1117;color:#fff}
h1{font-size:1.4rem;margin:0 0 4px}
.sub{color:#9aa4b2;margin-bottom:18px;font-size:.9rem}
.card{border:1px solid #4443;border-radius:10px;padding:14px 18px;margin-bottom:14px}
.section-title{font-weight:600;font-size:1.05rem;margin:0 0 12px;display:flex;
               align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.section-title .right{display:flex;align-items:center;gap:10px}
.state{font-size:.82rem;color:#9aa4b2}
.state.on{color:#3fb950}.state.off{color:#f85149}
.field{display:grid;grid-template-columns:160px 1fr;gap:10px;align-items:center;margin-bottom:9px}
.field label{color:#9aa4b2;font-size:.88rem}
.field .desc{color:#8b95a3;font-size:.78rem;grid-column:2;margin-top:-4px}
.field input[type=text],.field input[type=number],.field input[type=password]{
  width:100%;padding:7px 10px;border:1px solid #8884;border-radius:5px;
  background:transparent;color:inherit;font-size:.92rem;font-family:inherit;box-sizing:border-box}
.field input[type=checkbox]{transform:scale(1.15)}
button{padding:7px 14px;border:0;border-radius:6px;background:#1f6feb;color:#fff;
       font-size:.88rem;cursor:pointer;font-family:inherit}
button:hover{background:#1a5fd1}
button:disabled{background:#888;cursor:wait}
button.secondary{background:#888}
button.secondary:hover{background:#666}
button.big{padding:9px 20px;font-size:.95rem;min-width:160px}
.result{font-size:.85rem;margin-top:8px;padding:7px 10px;border-radius:6px;display:none}
.result.ok{display:block;background:#1f8b4c22;color:#3fb950}
.result.err{display:block;background:#d62b2b22;color:#ff6b6b}
.foot{color:#8b95a3;font-size:.83rem;margin-top:18px}
code{background:#8884;padding:1px 5px;border-radius:3px}
.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;
         position:fixed;left:0;right:0;bottom:0;z-index:50;
         background:#161b22ee;backdrop-filter:blur(10px);
         padding:12px 16px;border-top:1px solid #30363d;
         box-shadow:0 -4px 12px #0008}
@media (prefers-color-scheme:dark){.actions{background:#161b22ee;border-top-color:#30363d}}
.actions .inner{max-width:860px;margin:0 auto;display:flex;gap:10px;align-items:center;flex-wrap:wrap;width:100%}
.actions .grow{flex:1}
body{padding-bottom:90px}  /* 给底部按钮栏留空间 */
@media (max-width:600px){.field{grid-template-columns:1fr}.field label{margin-bottom:2px}}
</style></head><body>
<h1>百度网盘备份</h1>
<div class="sub">所有配置项均可在此修改并保存；通知渠道支持【测试发送】实时验证。</div>

<div id="config-form"></div>

<div class="actions">
  <div class="inner">
    <span class="state" id="sync-state">尚未同步</span>
    <div class="grow"></div>
    <button class="secondary" id="btn-sync">立即同步</button>
    <button class="secondary" id="btn-reload">重新加载</button>
    <button class="big" id="btn-save">保存配置</button>
  </div>
</div>
<div class="result" id="r-save"></div>
<div class="result" id="r-sync"></div>
<div class="foot">保存后配置<strong>立即生效</strong>（无需重启）。测试通知按钮基于<b>当前已保存</b>的配置发送，未保存的修改不影响测试结果。<br>注意：本页面保存的配置不会同步到 HA 原生【配置】标签页；如需一致，请在两个页面分别保存。</div>

<script>
const CHANNELS = __CHANNELS_JSON__;
const CHANNEL_LABELS = __CHANNEL_LABELS_JSON__;

// ============= 字段定义 =============
// section: 显示标题
// testChannel: 若非空，该 section 标题右侧显示 [测试发送] 按钮并显示当前状态
// items: 字段列表
const FIELDS = [
  {section: '基础配置', items: [
    {key: 'refresh_token', label: 'refresh_token', type: 'password', desc: '百度 OAuth 授权刷新令牌（必填）'},
    {key: 'upload_path', label: '上传路径', type: 'text', desc: '网盘中的目标目录，例如 /HomeAssistant/Backup'},
    {key: 'schedule', label: '定时任务 (Cron)', type: 'text', desc: '5 字段 Cron，例如 0 5 * * * 表示每天凌晨 5 点'},
  ]},
  {section: '远端保留策略 (retention)', items: [
    {key: 'retention.use_folders', label: '启用目录模式', type: 'bool', desc: '开启后按 每日/每周/每月 三个中文目录分类存放'},
    {key: 'retention.daily', label: '每日保留份数', type: 'number', desc: '同一天多份只保留最新；<=0 表示不启用'},
    {key: 'retention.weekly', label: '每周保留份数', type: 'number', desc: '同一周只保留最新；<=0 表示不启用'},
    {key: 'retention.monthly', label: '每月保留份数', type: 'number', desc: '同一月只保留最新；<=0 表示不启用'},
  ]},
  {section: '保留策略护栏', items: [
    {key: 'retention.protect_manual', label: '保护手工备份', type: 'bool', desc: '只删除 automatic_backup_* 且名字时间与文件时间一致（±6h）的自动备份；手工备份永不删除'},
    {key: 'retention.max_delete_per_run', label: '单次最多删除', type: 'number', min: 1, fallback: 10, desc: '每轮最多删这么多个（最旧优先），剩余的下轮继续；最小 1，清空按 10 处理'},
    {key: 'retention.dry_run', label: '演练模式 (dry-run)', type: 'bool', desc: '只记录将删除/将移动的文件，不删不移（含旧目录迁移与层级晋升）'},
    {key: 'sync.recheck_days', label: '缓存核对周期 (天)', type: 'number', desc: '每隔 N 天与云端核对一次缓存；发现云端缺失则重传'},
    {key: 'retry.deadline_seconds', label: '单操作重试预算 (秒)', type: 'number', desc: '默认 90；DNS/RST/超时在该预算内重试，永久错误立即失败'},
  ]},
  {section: '通知 — 全局', items: [
    {key: 'notifications.enabled', label: '启用通知', type: 'bool', desc: '全局开关；关闭后所有渠道都不发送'},
    {key: 'notifications.events.backup_empty', label: '备份目录为空', type: 'bool', desc: '默认开启；/backup 无 .tar 时提示（空目录同时会跳过保留策略）'},
    {key: 'notifications.events.retention_guard', label: '保留策略护栏告警', type: 'bool', desc: '跳过删除时提示（默认开启）'},
  ]},
  {section: '通知 — 事件开关 (events)', items: [
    {key: 'notifications.events.backup_success', label: '备份成功', type: 'bool'},
    {key: 'notifications.events.backup_failure', label: '备份失败', type: 'bool'},
    {key: 'notifications.events.migration_done', label: '目录迁移完成', type: 'bool'},
    {key: 'notifications.events.manifest_generated', label: '清单文件生成', type: 'bool'},
    {key: 'notifications.events.storage_warning', label: '存储空间告警', type: 'bool'},
    {key: 'notifications.storage_warning_threshold', label: '存储告警阈值 (%)', type: 'percent', desc: '已用比例 >= 该百分比时触发上面的"存储空间告警"事件'},
  ]},
  {section: '通知 — 邮箱 (SMTP)', testChannel: 'email', items: [
    {key: 'notifications.channels.email.enabled', label: '启用邮箱', type: 'bool'},
    {key: 'notifications.channels.email.smtp_host', label: 'SMTP 服务器', type: 'text', desc: '例如 smtp.gmail.com'},
    {key: 'notifications.channels.email.smtp_port', label: 'SMTP 端口', type: 'number', desc: 'TLS 用 587，SSL 用 465'},
    {key: 'notifications.channels.email.use_ssl', label: '使用 SSL', type: 'bool', desc: 'true=SSL(465)，false=TLS(587)'},
    {key: 'notifications.channels.email.username', label: '用户名', type: 'text'},
    {key: 'notifications.channels.email.password', label: '密码', type: 'password', desc: '邮箱授权码（不是登录密码）'},
    {key: 'notifications.channels.email.to_emails', label: '收件人', type: 'text', desc: '多个收件人用逗号分隔'},
  ]},
  {section: '通知 — 企业微信', testChannel: 'wechat', items: [
    {key: 'notifications.channels.wechat.enabled', label: '启用企业微信', type: 'bool'},
    {key: 'notifications.channels.wechat.webhook_key', label: 'Webhook Key', type: 'text', desc: '可填 key 本身，也可粘贴完整 Webhook URL'},
  ]},
  {section: '通知 — 钉钉', testChannel: 'dingtalk', items: [
    {key: 'notifications.channels.dingtalk.enabled', label: '启用钉钉', type: 'bool'},
    {key: 'notifications.channels.dingtalk.webhook_url', label: 'Webhook URL', type: 'text', desc: '完整 URL，含 access_token 参数'},
    {key: 'notifications.channels.dingtalk.secret', label: '加签密钥 (可选)', type: 'password'},
    {key: 'notifications.channels.dingtalk.at_all', label: '@所有人', type: 'bool'},
  ]},
  {section: '通知 — 飞书', testChannel: 'feishu', items: [
    {key: 'notifications.channels.feishu.enabled', label: '启用飞书', type: 'bool'},
    {key: 'notifications.channels.feishu.webhook_url', label: 'Webhook URL', type: 'text', desc: '完整 URL'},
    {key: 'notifications.channels.feishu.secret', label: '签名密钥 (可选)', type: 'password'},
  ]},
];

let currentOptions = {};
let currentState = {};

function getPath(obj, path) {
  return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
}
function setPath(obj, path, value) {
  const parts = path.split('.');
  const last = parts.pop();
  let cur = obj;
  for (const p of parts) {
    if (cur[p] == null || typeof cur[p] !== 'object') cur[p] = {};
    cur = cur[p];
  }
  cur[last] = value;
}
function inputId(key) { return 'f_' + key.replace(/\./g, '_'); }
function esc(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

async function renderConfig() {
  const [cRes, sRes] = await Promise.all([fetch('./api/config'), fetch('./api/state')]);
  currentOptions = await cRes.json();
  currentState = await sRes.json();
  renderSyncState();
  const form = document.getElementById('config-form');
  form.innerHTML = '';
  for (const sec of FIELDS) {
    const card = document.createElement('div');
    card.className = 'card';
    let titleRight = '';
    if (sec.testChannel) {
      const st = currentState[sec.testChannel] || {};
      titleRight = `
        <div class="right">
          <span class="state ${st.enabled ? 'on' : 'off'}">
            ${st.enabled ? '已启用' : '未启用'} · ${st.filled ? '配置已填' : '配置缺失'}
          </span>
          <button data-test="${sec.testChannel}">测试发送</button>
        </div>`;
    }
    let html = `<div class="section-title"><span>${esc(sec.section)}</span>${titleRight}</div>`;
    if (sec.testChannel) html += `<div class="result" id="r-${sec.testChannel}"></div>`;
    for (const f of sec.items) {
      const cur = getPath(currentOptions, f.key);
      const id = inputId(f.key);
      if (f.type === 'bool') {
        html += `<div class="field">
          <label for="${id}">${esc(f.label)}</label>
          <div><input type="checkbox" id="${id}" data-key="${f.key}" data-type="bool" ${cur ? 'checked' : ''}></div>
          ${f.desc ? `<div class="desc">${esc(f.desc)}</div>` : ''}
        </div>`;
      } else {
        const t = (f.type === 'number' || f.type === 'percent') ? 'number' : (f.type === 'password' ? 'password' : 'text');
        let step = f.step ? ` step="${f.step}"` : (f.type === 'percent' ? ' step="1" min="0" max="100"' : '');
        if (f.type === 'number' && f.min != null) step += ` min="${f.min}"`;
        let val;
        if (f.type === 'percent') {
          const n = (cur == null || cur === '') ? 90 : Math.round(Number(cur) * 100);
          val = String(isNaN(n) ? 90 : n);
        } else {
          val = cur == null ? '' : String(cur);
        }
        html += `<div class="field">
          <label for="${id}">${esc(f.label)}</label>
          <input type="${t}"${step} id="${id}" data-key="${f.key}" data-type="${f.type}" data-fallback="${f.fallback != null ? f.fallback : ''}" value="${esc(val)}">
          ${f.desc ? `<div class="desc">${esc(f.desc)}</div>` : ''}
        </div>`;
      }
    }
    card.innerHTML = html;
    form.appendChild(card);
  }
  // 绑定测试按钮
  document.querySelectorAll('button[data-test]').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.preventDefault();
      const ch = btn.dataset.test;
      const r = document.getElementById('r-' + ch);
      r.className = 'result'; r.textContent = '';
      btn.disabled = true; btn.textContent = '发送中...';
      try {
        const resp = await fetch('./api/test/' + ch, {method: 'POST'});
        const data = await resp.json();
        r.className = 'result ' + (data.ok ? 'ok' : 'err');
        r.textContent = (data.ok ? '✅ ' : '❌ ') + (data.message || (data.ok ? '已发送' : '失败'));
      } catch (e) {
        r.className = 'result err'; r.textContent = '❌ 请求失败：' + e;
      } finally {
        btn.disabled = false; btn.textContent = '测试发送';
      }
    });
  });
}

function collectConfig() {
  const out = JSON.parse(JSON.stringify(currentOptions));
  document.querySelectorAll('#config-form [data-key]').forEach(el => {
    const key = el.dataset.key;
    const t = el.dataset.type;
    let v;
    if (t === 'bool') v = el.checked;
    else if (t === 'percent') {
      const raw = el.value.trim();
      let n = raw === '' ? 90 : Number(raw);
      if (isNaN(n)) n = 90;
      n = Math.max(0, Math.min(100, n));
      v = Math.round(n) / 100;  // 写回 options 的仍是 0-1 小数
    }
    else if (t === 'number') {
      // v1.3.1：数字框被清空时不能变成 0（0 曾等于"不限"，等于一键关掉护栏）
      const raw = el.value.trim();
      const fb = el.dataset.fallback === '' ? null : Number(el.dataset.fallback);
      let n = raw === '' ? (fb == null ? 0 : fb) : Number(raw);
      if (!Number.isFinite(n)) n = (fb == null ? 0 : fb);
      if (fb != null && n < fb) n = fb;
      v = n;
    } else v = el.value;
    setPath(out, key, v);
  });
  return out;
}

function renderSyncState() {
  const st = (currentState && currentState.sync) || {};
  const el = document.getElementById('sync-state');
  if (!el) return;
  if (st.running) {
    el.className = 'state on';
    el.textContent = '同步中…（' + (st.trigger || '') + '）';
  } else if (st.finished) {
    el.className = 'state';
    el.textContent = '上次同步：' + st.finished + ' → ' + (st.outcome || '?');
  } else {
    el.className = 'state';
    el.textContent = '尚未同步';
  }
}

document.getElementById('btn-sync').addEventListener('click', async () => {
  const btn = document.getElementById('btn-sync');
  const r = document.getElementById('r-sync');
  r.className = 'result'; r.textContent = '';
  btn.disabled = true; btn.textContent = '同步中...';
  try {
    const resp = await fetch('./api/sync', {method: 'POST'});
    const data = await resp.json();
    r.className = 'result ' + (data.ok ? 'ok' : 'err');
    r.textContent = (data.ok ? '✅ ' : '❌ ') + (data.message || '');
    setTimeout(renderConfig, 1500);
  } catch (e) {
    r.className = 'result err'; r.textContent = '❌ 请求失败：' + e;
  } finally {
    btn.disabled = false; btn.textContent = '立即同步';
  }
});

document.getElementById('btn-reload').addEventListener('click', renderConfig);
document.getElementById('btn-save').addEventListener('click', async () => {
  const btn = document.getElementById('btn-save');
  const r = document.getElementById('r-save');
  r.className = 'result'; r.textContent = '';
  btn.disabled = true; btn.textContent = '保存中...';
  try {
    const body = JSON.stringify(collectConfig());
    const resp = await fetch('./api/config', {method: 'POST', headers: {'Content-Type': 'application/json'}, body});
    const data = await resp.json();
    r.className = 'result ' + (data.ok ? 'ok' : 'err');
    r.textContent = (data.ok ? '✅ ' : '❌ ') + (data.message || '');
  } catch (e) {
    r.className = 'result err'; r.textContent = '❌ 请求失败：' + e;
  } finally {
    btn.disabled = false; btn.textContent = '保存配置';
  }
});

renderConfig();
</script></body></html>
"""


def _build_state() -> Dict[str, Dict[str, Any]]:
    opts = _load_options()
    chans = (opts.get("notifications") or {}).get("channels") or {}
    out: Dict[str, Dict[str, Any]] = {}
    for ch in CHANNELS:
        cfg = chans.get(ch) or {}
        enabled = bool(cfg.get("enabled"))
        if ch == "email":
            filled = all(cfg.get(k) for k in ("smtp_host", "username", "password", "to_emails"))
        elif ch == "wechat":
            filled = bool(cfg.get("webhook_key"))
        else:
            filled = bool(cfg.get("webhook_url"))
        out[ch] = {"enabled": enabled, "filled": filled, "label": CHANNEL_LABELS[ch]}
    out["sync"] = get_sync_status()
    return out


def _render_html() -> bytes:
    return (
        _HTML
        .replace("__CHANNELS_JSON__", json.dumps(CHANNELS))
        .replace("__CHANNEL_LABELS_JSON__", json.dumps(CHANNEL_LABELS, ensure_ascii=False))
    ).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        log(f"web {self.address_string()} - {fmt % args}")

    def _route(self) -> str:
        return self.path.split("?", 1)[0].rstrip("/")

    def do_GET(self) -> None:
        path = self._route() or "/"
        if path == "/" or path.endswith("/index.html"):
            body = _render_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path.endswith("/api/state"):
            self._send_json(200, _build_state())
            return
        if path.endswith("/api/config"):
            self._send_json(200, _load_options())
            return
        self._send_json(404, {"ok": False, "message": "not found"})

    def do_POST(self) -> None:
        path = self._route()
        parts = path.split("/")
        # POST /api/test/<channel>
        if len(parts) >= 2 and parts[-2] == "test":
            channel = parts[-1]
            if channel not in CHANNELS:
                self._send_json(400, {"ok": False, "message": f"未知渠道：{channel}"})
                return
            cfg = ((_load_options().get("notifications") or {}).get("channels") or {}).get(channel) or {}
            if not cfg.get("enabled"):
                self._send_json(200, {"ok": False, "message": "该渠道未启用，请先在配置中开启并保存"})
                return
            try:
                ok = test_notification(channel, cfg)
            except Exception as e:
                log(f"web 测试 {channel} 异常：{e}")
                self._send_json(500, {"ok": False, "message": f"发送异常：{e}"})
                return
            self._send_json(200, {"ok": bool(ok), "message": "测试通知已发送" if ok else "发送失败，请查看加载项日志"})
            return

        # POST /api/sync — 手动触发一次同步（不新增依赖，走内嵌页）
        if path.endswith("/api/sync"):
            result = request_manual_sync()
            self._send_json(200, result)
            return

        # POST /api/config — 保存并重启
        if path.endswith("/api/config"):
            try:
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                new_opts = json.loads(raw)
                if not isinstance(new_opts, dict):
                    raise ValueError("配置必须是 JSON 对象")
            except Exception as e:
                self._send_json(400, {"ok": False, "message": f"请求体解析失败：{e}"})
                return
            try:
                saved = _save_options(new_opts)
            except Exception as e:
                log(f"Web UI: 配置保存失败：{e}")
                self._send_json(500, {"ok": False, "message": f"配置保存失败：{e}"})
                return
            if saved.get("api_ok"):
                self._send_json(
                    200,
                    {"ok": True, "message": "配置已保存到 HA 并立即生效（重启后仍然有效）"},
                )
            else:
                reason = saved.get("api_error") or "未知原因"
                self._send_json(
                    200,
                    {
                        "ok": False,
                        "message": (
                            f"配置仅在本进程内生效并已写入 /data/options.json，"
                            f"但未能同步到 HA（{reason}）。"
                            "重建/重启加载项后会静默回退到 HA 的旧配置，请到 HA 原生"
                            "【配置】页确认保存。"
                        ),
                    },
                )
            return

        self._send_json(404, {"ok": False, "message": "not found"})


def start_web_server(port: int = 8099) -> None:
    """在后台线程启动 Web UI。失败仅记日志，不影响主流程。"""
    def _run() -> None:
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
            log(f"Web UI listening on 0.0.0.0:{port} (ingress)")
            srv.serve_forever()
        except Exception as e:
            log(f"Web UI 启动失败（不影响备份功能）：{e}")

    t = threading.Thread(target=_run, name="web-ui", daemon=True)
    t.start()
