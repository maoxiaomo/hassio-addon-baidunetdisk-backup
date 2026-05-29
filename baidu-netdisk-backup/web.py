#!/usr/bin/env python3
"""Web UI — 两个标签页：通知测试 + 配置编辑（中文界面）。"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

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


def _load_options() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_options(opts: Dict[str, Any]) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(opts, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def _restart_addon() -> Dict[str, Any]:
    """通过 HA Supervisor API 重启本加载项。"""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        return {"ok": False, "message": "SUPERVISOR_TOKEN 不存在，无法自动重启；请到加载项页面手动重启"}
    try:
        r = requests.post(
            "http://supervisor/addons/self/restart",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code == 200:
            return {"ok": True, "message": "已请求 Supervisor 重启加载项"}
        return {"ok": False, "message": f"Supervisor 返回 {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "message": f"调用 Supervisor 失败：{e}"}


_HTML = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>百度网盘备份</title>
<style>
:root{color-scheme:light dark}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
     max-width:860px;margin:20px auto;padding:0 16px;line-height:1.55}
h1{font-size:1.4rem;margin:0 0 4px}
.sub{color:#888;margin-bottom:18px;font-size:.9rem}
.card{border:1px solid #4443;border-radius:10px;padding:14px 18px;margin-bottom:14px}
.section-title{font-weight:600;font-size:1.05rem;margin:0 0 12px;display:flex;
               align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.section-title .right{display:flex;align-items:center;gap:10px}
.state{font-size:.82rem;color:#888}
.state.on{color:#2a8a2a}.state.off{color:#a05}
.field{display:grid;grid-template-columns:160px 1fr;gap:10px;align-items:center;margin-bottom:9px}
.field label{color:#888;font-size:.88rem}
.field .desc{color:#888;font-size:.78rem;grid-column:2;margin-top:-4px}
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
.result.ok{display:block;background:#1f8b4c22;color:#1f8b4c}
.result.err{display:block;background:#d62b2b22;color:#d62b2b}
.foot{color:#888;font-size:.83rem;margin-top:18px}
code{background:#8884;padding:1px 5px;border-radius:3px}
.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;
         position:fixed;left:0;right:0;bottom:0;z-index:50;
         background:#fffd;backdrop-filter:blur(10px);
         padding:12px 16px;border-top:1px solid #4443;
         box-shadow:0 -4px 12px #0002}
@media (prefers-color-scheme:dark){.actions{background:#0d1117ee;border-top-color:#fff2}}
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
    <div class="grow"></div>
    <button class="secondary" id="btn-reload">重新加载</button>
    <button class="big" id="btn-save">保存并重启加载项</button>
  </div>
</div>
<div class="result" id="r-save"></div>
<div class="foot">保存后会自动调用 HA Supervisor 重启本加载项使配置生效；若 Supervisor 不可用，请手动到加载项页面【重启】。测试通知按钮基于<b>当前已保存</b>的配置发送，未保存的修改不影响测试结果。</div>

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
  {section: '通知 — 全局', items: [
    {key: 'notifications.enabled', label: '启用通知', type: 'bool', desc: '全局开关；关闭后所有渠道都不发送'},
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
        const step = f.step ? ` step="${f.step}"` : (f.type === 'percent' ? ' step="1" min="0" max="100"' : '');
        let val;
        if (f.type === 'percent') {
          const n = (cur == null || cur === '') ? 90 : Math.round(Number(cur) * 100);
          val = String(isNaN(n) ? 90 : n);
        } else {
          val = cur == null ? '' : String(cur);
        }
        html += `<div class="field">
          <label for="${id}">${esc(f.label)}</label>
          <input type="${t}"${step} id="${id}" data-key="${f.key}" data-type="${f.type}" value="${esc(val)}">
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
      const raw = el.value.trim();
      v = raw === '' ? 0 : Number(raw);
    } else v = el.value;
    setPath(out, key, v);
  });
  return out;
}

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
    btn.disabled = false; btn.textContent = '保存并重启加载项';
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
                _save_options(new_opts)
                log("Web UI: 配置已保存，准备重启加载项")
            except Exception as e:
                log(f"Web UI: 配置保存失败：{e}")
                self._send_json(500, {"ok": False, "message": f"配置保存失败：{e}"})
                return
            r = _restart_addon()
            msg = ("配置已保存，" + r["message"]) if r["ok"] else ("配置已保存，但 " + r["message"])
            self._send_json(200, {"ok": True, "message": msg})
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
