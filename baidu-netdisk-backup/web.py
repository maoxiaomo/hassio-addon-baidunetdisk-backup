#!/usr/bin/env python3
"""轻量 Web UI — 在 HA 加载项里点【打开 Web UI】，可对每个通知渠道发送测试。"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

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


_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>百度网盘备份 — 通知测试</title>
<style>
:root{color-scheme:light dark}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
     max-width:760px;margin:24px auto;padding:0 16px;line-height:1.55}
h1{font-size:1.4rem;margin:0 0 4px}
.sub{color:#888;margin-bottom:24px}
.card{border:1px solid #4443;border-radius:10px;padding:16px 18px;margin-bottom:14px;
      display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.card .meta{flex:1;min-width:200px}
.name{font-weight:600;font-size:1.05rem}
.state{font-size:.85rem;color:#888;margin-top:2px}
.state.on{color:#2a8a2a}
.state.off{color:#a05}
button{padding:8px 18px;border:0;border-radius:6px;background:#1f6feb;color:#fff;
       font-size:.95rem;cursor:pointer;min-width:96px}
button:hover{background:#1a5fd1}
button:disabled{background:#888;cursor:wait}
.result{font-size:.9rem;margin-top:8px;width:100%;padding:8px 10px;border-radius:6px;display:none}
.result.ok{display:block;background:#1f8b4c22;color:#1f8b4c}
.result.err{display:block;background:#d62b2b22;color:#d62b2b}
.foot{color:#888;font-size:.85rem;margin-top:18px}
code{background:#8884;padding:1px 5px;border-radius:3px}
</style></head><body>
<h1>百度网盘备份 — 通知渠道测试</h1>
<div class="sub">点击【测试发送】立即向对应渠道发送一条测试消息，无需重启加载项。配置改动后请先在加载项配置页【保存】再来测试。</div>
<div id="list"></div>
<div class="foot">读取自 <code>/data/options.json</code>，只有 <b>enabled=true</b> 且填了必要字段的渠道才会真正发送。</div>
<script>
const CHANNELS = __CHANNELS_JSON__;
const STATE = __STATE_JSON__;
const list = document.getElementById('list');
for (const ch of CHANNELS) {
  const s = STATE[ch] || {};
  const enabled = !!s.enabled;
  const filled = !!s.filled;
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `
    <div class="meta">
      <div class="name">${s.label}</div>
      <div class="state ${enabled ? 'on' : 'off'}">
        ${enabled ? '已启用' : '未启用'} · ${filled ? '配置已填' : '配置缺失'}
      </div>
    </div>
    <button data-ch="${ch}">测试发送</button>
    <div class="result" id="r-${ch}"></div>
  `;
  list.appendChild(card);
}
document.querySelectorAll('button[data-ch]').forEach(btn => {
  btn.addEventListener('click', async () => {
    const ch = btn.dataset.ch;
    const r = document.getElementById('r-' + ch);
    r.className = 'result'; r.textContent = '';
    btn.disabled = true; btn.textContent = '发送中...';
    try {
      const resp = await fetch('./test/' + ch, {method: 'POST'});
      const data = await resp.json();
      if (data.ok) { r.className = 'result ok'; r.textContent = '✅ ' + (data.message || '已发送'); }
      else        { r.className = 'result err'; r.textContent = '❌ ' + (data.message || '失败'); }
    } catch (e) {
      r.className = 'result err'; r.textContent = '❌ 请求失败：' + e;
    } finally {
      btn.disabled = false; btn.textContent = '测试发送';
    }
  });
});
</script></body></html>
"""


def _build_state() -> Dict[str, Dict[str, Any]]:
    """渠道当前的 enabled / 是否填了关键字段。"""
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
        else:  # dingtalk / feishu
            filled = bool(cfg.get("webhook_url"))
        out[ch] = {"enabled": enabled, "filled": filled, "label": CHANNEL_LABELS[ch]}
    return out


def _render_html() -> bytes:
    state = _build_state()
    html = (
        _HTML
        .replace("__CHANNELS_JSON__", json.dumps(CHANNELS))
        .replace("__STATE_JSON__", json.dumps(state, ensure_ascii=False))
    )
    return html.encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Quiet the noisy default access logger; route through our logger.
        log(f"web {self.address_string()} - {fmt % args}")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        # Ingress 会保留路径前缀，所以匹配末尾即可
        if path == "/" or path.endswith("/index.html"):
            body = _render_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json(404, {"ok": False, "message": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        # 兼容 ingress 前缀，匹配 .../test/<channel>
        parts = path.split("/")
        if len(parts) >= 2 and parts[-2] == "test":
            channel = parts[-1]
            if channel not in CHANNELS:
                self._send_json(400, {"ok": False, "message": f"未知渠道：{channel}"})
                return
            opts = _load_options()
            chans = (opts.get("notifications") or {}).get("channels") or {}
            cfg = chans.get(channel) or {}
            if not cfg.get("enabled"):
                self._send_json(
                    200,
                    {"ok": False, "message": "该渠道未启用 (enabled=false)，请先在配置中开启并保存"},
                )
                return
            try:
                ok = test_notification(channel, cfg)
            except Exception as e:
                log(f"web 测试 {channel} 异常：{e}")
                self._send_json(500, {"ok": False, "message": f"发送异常：{e}"})
                return
            self._send_json(
                200,
                {"ok": bool(ok), "message": "测试通知已发送" if ok else "发送失败，请查看加载项日志"},
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
