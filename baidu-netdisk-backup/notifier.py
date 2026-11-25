#!/usr/bin/env python3
"""消息通知模块 — 支持邮箱、企业微信、钉钉、飞书四种渠道。

事件类型：
    - backup_success：备份同步成功完成
    - backup_failure：备份同步过程中出现异常
    - migration_done：旧版目录迁移完成
    - manifest_generated：清单文件生成完毕
    - storage_warning：网盘存储空间告警

特性：
    - per-channel 异常隔离（一个渠道崩溃不影响其他渠道）
    - 全局 / 事件级开关
    - 重试机制（3 次，间隔 2 秒）
    - 超时 15 秒
"""
import base64
import hashlib
import hmac
import json
import smtplib
import time
import urllib.parse
from datetime import datetime
from email.mime.text import MIMEText
from typing import Any, Callable, Dict, Optional, Union

import requests


# ============================================================================
# 常量
# ============================================================================
MAX_RETRIES: int = 3                # 最大重试次数
RETRY_DELAY: float = 2.0            # 重试间隔（秒）
TIMEOUT: int = 15                   # HTTP 请求超时（秒）
TIME_FORMAT: str = "%Y-%m-%d %H:%M:%S"

# 通知渠道列表（供外部引用）
AVAILABLE_CHANNELS: list = ["email", "wechat", "dingtalk", "feishu"]

# 事件列表
EVENT_TYPES: list = [
    "backup_success",
    "backup_failure",
    "migration_done",
    "manifest_generated",
    "storage_warning",
    "backup_empty",
    "retention_guard",
]

# 事件级开关的默认值（options.json 里缺这个键时的兜底）
# 与 config.yaml 的 options 默认值对齐的部分：backup_empty / retention_guard /
# migration_done / manifest_generated。storage_warning 保持 True（老配置基本都显式
# 设置了它；这里只影响"从未设置"的配置，取安全侧 = 会告警）。
DEFAULT_EVENT_ENABLED: dict = {
    "backup_success": True,
    "backup_failure": True,
    "storage_warning": True,
    # 空目录是异常态而不是正常态：源端变空必须让人知道（P1-8）
    "backup_empty": True,
    "retention_guard": True,   # 护栏跳过/部分删除必须让人知道
    # 这两个是"信息性"事件，默认关，避免把结果通知淹没
    "migration_done": False,
    "manifest_generated": False,
}


def event_enabled(notifications: Dict[str, Any], event_type: str) -> bool:
    """判断某事件当前是否会真的发出去（供调用方决定"兜底通知"）。

    与 ``notify_event`` 内部判定保持同一逻辑，避免"主事件被用户关掉 +
    有护栏事件"时整轮静默。
    """
    if not isinstance(notifications, dict):
        return False
    if not notifications.get("enabled", True):
        return False
    events_cfg = notifications.get("events", {})
    default = DEFAULT_EVENT_ENABLED.get(event_type, True)
    if isinstance(events_cfg, dict):
        return bool(events_cfg.get(event_type, default))
    return default


# ============================================================================
# 日志
# ============================================================================
def _log(msg: str) -> None:
    """输出带时间戳的日志消息。"""
    print(f"[{datetime.now().strftime(TIME_FORMAT)}] {msg}", flush=True)


# ============================================================================
# 工具函数
# ============================================================================
def _retry_request(
    method: str,
    url: str,
    payload: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    retries: int = MAX_RETRIES,
    delay: float = RETRY_DELAY,
    timeout: int = TIMEOUT,
) -> requests.Response:
    """带重试的 HTTP 请求，返回最后一次响应对象。

    Args:
        method: HTTP 方法（"POST" / "GET"）
        url: 请求地址
        payload: JSON 请求体
        headers: 请求头
        retries: 最大重试次数
        delay: 重试间隔（秒）
        timeout: 超时时间（秒）

    Returns:
        requests.Response 对象（可能为失败响应）
    """
    last_resp: Optional[requests.Response] = None
    for attempt in range(1, retries + 1):
        try:
            if method.upper() == "POST":
                resp = requests.post(
                    url, json=payload, headers=headers or {}, timeout=timeout
                )
            else:
                resp = requests.get(
                    url, json=payload, headers=headers or {}, timeout=timeout
                )
            if resp.status_code < 500:
                return resp
            last_resp = resp
        except requests.RequestException as e:
            _log(f"请求异常（第 {attempt}/{retries} 次）：{e}")
        if attempt < retries:
            time.sleep(delay)
    if last_resp is not None:
        return last_resp
    # 全部网络异常，返回一个假的 error response
    resp = requests.Response()
    resp.status_code = 0
    resp._content = b'{"errcode": -1, "errmsg": "network error after retries"}'
    return resp


# ============================================================================
# 发送器注册表
# ============================================================================
_CHANNEL_SENDERS: Dict[str, Callable[[Dict[str, Any], str, str], bool]] = {}


def _register(channel: str):
    """装饰器：将发送函数注册到 _CHANNEL_SENDERS。"""
    def decorator(func):
        _CHANNEL_SENDERS[channel] = func
        return func
    return decorator


# ============================================================================
# 邮箱通知
# ============================================================================
@_register("email")
def _send_email(config: Dict[str, Any], title: str, content: str) -> bool:
    """通过 SMTP 发送邮件通知。

    配置项：
        smtp_host: SMTP 服务器地址
        smtp_port: SMTP 端口（默认 587 TLS / 465 SSL）
        username: 邮箱账号
        password: 邮箱密码 / 授权码
        to_emails: 收件人列表，逗号分隔
        use_ssl: 是否使用 SSL（默认 False = TLS）
    """
    smtp_host = (config.get("smtp_host") or "").strip()
    smtp_port = config.get("smtp_port", 587)
    username = (config.get("username") or "").strip()
    password = (config.get("password") or "").strip()
    to_emails_str = (config.get("to_emails") or "").strip()

    if not all([smtp_host, username, password, to_emails_str]):
        _log("邮箱通知配置不完整（smtp_host/username/password/to_emails 缺失），跳过发送")
        return False

    to_emails = [e.strip() for e in to_emails_str.split(",") if e.strip()]
    if not to_emails:
        return False

    use_ssl = bool(config.get("use_ssl"))

    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"] = title
    msg["From"] = username
    msg["To"] = ", ".join(to_emails)

    for attempt in range(1, MAX_RETRIES + 1):
        server = None
        try:
            if use_ssl:
                server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=TIMEOUT)
            else:
                server = smtplib.SMTP(smtp_host, smtp_port, timeout=TIMEOUT)
                server.starttls()
            server.login(username, password)
            server.sendmail(username, to_emails, msg.as_string())
            server.quit()
            _log(f"邮件通知发送成功 → {', '.join(to_emails)}")
            return True
        except Exception as e:
            _log(f"邮件发送失败（第 {attempt}/{MAX_RETRIES} 次）：{e}")
            if server:
                try:
                    server.quit()
                except Exception:
                    pass
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    _log(f"邮件通知最终失败")
    return False


# ============================================================================
# 企业微信机器人通知
# ============================================================================
@_register("wechat")
def _send_wechat(config: Dict[str, Any], title: str, content: str) -> bool:
    """通过企业微信群机器人 Webhook 发送通知。

    配置项：
        webhook_key: 企业微信群机器人 Webhook Key（从 Webhook URL 中提取）

    消息体格式：{"msgtype": "text", "text": {"content": "..."}}
    """
    webhook_key = (config.get("webhook_key") or "").strip()
    if not webhook_key:
        _log("企业微信 Webhook Key 未配置，跳过发送")
        return False

    # 兼容用户粘贴完整 Webhook URL 的情况：自动提取 ?key=... 参数
    if "://" in webhook_key or webhook_key.lower().startswith("http"):
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(webhook_key)
            extracted = parse_qs(parsed.query).get("key", [""])[0].strip()
            if extracted:
                _log("检测到完整 Webhook URL，已自动提取 key")
                webhook_key = extracted
            else:
                _log("企业微信 Webhook Key 看起来是 URL 但未找到 ?key= 参数，请检查配置")
                return False
        except Exception as e:
            _log(f"解析企业微信 Webhook URL 失败：{e}")
            return False

    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={webhook_key}"
    full_content = f"{title}\n\n{content}"
    payload: Dict[str, Any] = {
        "msgtype": "text",
        "text": {"content": full_content},
    }

    resp = _retry_request("POST", url, payload)
    try:
        data = resp.json()
        if data.get("errcode") == 0:
            _log("微信通知发送成功")
            return True
        _log(f"微信通知失败：{data.get('errmsg', data)}")
    except (json.JSONDecodeError, ValueError) as e:
        _log(f"微信通知返回解析失败：{e}")
    return False


# ============================================================================
# 钉钉机器人通知
# ============================================================================
@_register("dingtalk")
def _send_dingtalk(config: Dict[str, Any], title: str, content: str) -> bool:
    """通过钉钉群机器人 Webhook 发送通知，支持加签模式。

    配置项：
        webhook_url: 钉钉机器人 Webhook 完整 URL
        secret: 加签密钥（可选，留空则不启用加签）
        at_all: 是否 @所有人（默认 False）
        at_mobiles: 要 @的手机号列表
    """
    webhook_url = (config.get("webhook_url") or "").strip()
    if not webhook_url:
        _log("钉钉 Webhook URL 未配置，跳过发送")
        return False

    secret = (config.get("secret") or "").strip()
    at_all = bool(config.get("at_all"))
    at_mobiles = config.get("at_mobiles") or []

    full_content = f"**{title}**\n\n{content}"

    payload: Dict[str, Any] = {
        "msgtype": "text",
        "text": {"content": full_content},
        "at": {
            "atMobiles": at_mobiles,
            "isAtAll": at_all,
        },
    }

    headers: Dict[str, str] = {"Content-Type": "application/json"}

    url = webhook_url
    # 加签逻辑
    if secret:
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(
            base64.b64encode(hmac_code).decode("utf-8")
        )
        # 如果 URL 已含 ? 则用 &，否则用 ?
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}timestamp={timestamp}&sign={sign}"

    resp = _retry_request("POST", url, payload, headers=headers)
    try:
        data = resp.json()
        if data.get("errcode") == 0:
            _log("钉钉通知发送成功")
            return True
        _log(f"钉钉通知失败：{data.get('errmsg', data)}")
    except (json.JSONDecodeError, ValueError) as e:
        _log(f"钉钉通知返回解析失败：{e}")
    return False


# ============================================================================
# 飞书机器人通知
# ============================================================================
@_register("feishu")
def _send_feishu(config: Dict[str, Any], title: str, content: str) -> bool:
    """通过飞书群机器人 Webhook 发送通知，支持签名校验。

    配置项：
        webhook_url: 飞书机器人 Webhook 完整 URL
        secret: 签名校验密钥（可选，留空则不启用签名校验）
    """
    webhook_url = (config.get("webhook_url") or "").strip()
    if not webhook_url:
        _log("飞书 Webhook URL 未配置，跳过发送")
        return False

    secret = (config.get("secret") or "").strip()

    full_content = f"{title}\n\n{content}"

    payload: Dict[str, Any] = {
        "msg_type": "text",
        "content": {"text": full_content},
    }

    # 签名校验逻辑
    if secret:
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = base64.b64encode(hmac_code).decode("utf-8")
        payload["timestamp"] = timestamp
        payload["sign"] = sign

    resp = _retry_request("POST", webhook_url, payload)
    try:
        data = resp.json()
        # 飞书成功返回 code=0 或 StatusCode=0
        code = data.get("code", data.get("StatusCode", -1))
        if code == 0:
            _log("飞书通知发送成功")
            return True
        _log(f"飞书通知失败：{data.get('msg', data)}")
    except (json.JSONDecodeError, ValueError) as e:
        _log(f"飞书通知返回解析失败：{e}")
    return False


# ============================================================================
# 事件消息格式化
# ============================================================================
def _format_event_message(
    event_type: str, event_data: Optional[Dict[str, Any]] = None
) -> Dict[str, str]:
    """根据事件类型生成通知标题和正文。

    Args:
        event_type: 事件类型（backup_success / backup_failure / ...）
        event_data: 事件相关数据（统计信息、错误信息等）

    Returns:
        {"title": str, "content": str}
    """
    if event_data is None:
        event_data = {}

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if event_type == "backup_success":
        total = event_data.get("total_count", 0)
        uploaded = event_data.get("uploaded_count")
        if uploaded is None:               # 兼容旧调用方
            uploaded = event_data.get("success_count", 0)
        skipped = event_data.get("skipped_count", 0)
        failed = event_data.get("failed_count", 0)
        upload_path = event_data.get("upload_path", "/HomeAssistant/Backup")
        title = "✅ HA 备份同步成功"
        lines = [
            f"时间：{now_str}",
            f"目标路径：{upload_path}",
            f"扫描文件：{total}",
            f"本次上传：{uploaded}",
            f"跳过（已上传）：{skipped}",
            f"失败：{failed}",
        ]
        if total and total != uploaded + skipped + failed:
            lines.append(
                f"（注意：分项之和 {uploaded + skipped + failed} 与扫描数 {total} 不一致，"
                f"请查看加载项日志）"
            )
        guard = event_data.get("guard")
        if guard:
            lines.append("")
            lines.append(f"⚠️ 保留策略：{guard}")
        content = "\n".join(lines)

    elif event_type == "backup_failure":
        error_msg = event_data.get("error") or "未知错误"
        upload_path = event_data.get("upload_path", "/HomeAssistant/Backup")
        stage = event_data.get("stage") or "上传"
        total = event_data.get("total_count", 0)
        uploaded = event_data.get("uploaded_count", 0)
        skipped = event_data.get("skipped_count", 0)
        failed = event_data.get("failed_count", 0)
        title = "❌ HA 备份同步失败"
        verdict = (
            f"{failed}/{total} 个文件未完成上传"
            if failed
            else "文件上传已完成，失败发生在后续步骤"
        )
        lines = [
            f"时间：{now_str}",
            f"目标路径：{upload_path}",
            f"阶段：{stage}",
            f"结论：{verdict}",
        ]
        if total:
            lines.extend(
                [
                    f"扫描文件：{total}",
                    f"本次上传：{uploaded}",
                    f"跳过（已上传）：{skipped}",
                    f"失败：{failed}",
                ]
            )
        lines.append(f"错误：{error_msg}")
        failed_files = event_data.get("failed_files") or []
        for item in failed_files[:10]:
            lines.append(f"  - {item.get('name')}：{item.get('error')}")
        if len(failed_files) > 10:
            lines.append(f"  ……另有 {len(failed_files) - 10} 个，详见日志")
        guard = event_data.get("guard")
        if guard:
            lines.append("")
            lines.append(f"⚠️ 保留策略：{guard}")
        content = "\n".join(lines)

    elif event_type == "backup_empty":
        upload_path = event_data.get("upload_path", "/HomeAssistant/Backup")
        title = "⚠️ HA 备份目录为空"
        content = "\n".join(
            [
                f"时间：{now_str}",
                f"目标路径：{upload_path}",
                "说明：/backup 下没有任何 .tar 文件，本次未上传任何内容。",
                "若这不是预期行为，请检查 HA 的备份计划或 /backup 挂载。",
            ]
        )

    elif event_type == "retention_guard":
        title = "⚠️ 远端保留策略未按预期执行"
        content = "\n".join(
            [
                f"时间：{now_str}",
                f"目标路径：{event_data.get('upload_path', '/HomeAssistant/Backup')}",
                f"待删除候选：{event_data.get('candidates', 0)}",
                f"受保护文件：{event_data.get('protected', 0)}",
                f"原因：{event_data.get('guard') or event_data.get('error') or '未知'}",
                (
                    f"同步结果：outcome={event_data.get('outcome')} "
                    f"扫描 {event_data.get('total_count', 0)} / "
                    f"上传 {event_data.get('uploaded_count', 0)} / "
                    f"失败 {event_data.get('failed_count', 0)}"
                ),
            ]
        )

    elif event_type == "migration_done":
        from_dir = event_data.get("from_dir", "")
        to_dir = event_data.get("to_dir", "")
        count = event_data.get("count", 0)
        title = "🔀 目录迁移完成"
        content = (
            f"时间：{now_str}\n"
            f"从：{from_dir}\n"
            f"到：{to_dir}\n"
            f"迁移文件数：{count}"
        )

    elif event_type == "manifest_generated":
        manifest_path = event_data.get("manifest_path", "")
        file_count = event_data.get("file_count", 0)
        total_size = event_data.get("total_size", 0)
        title = "📋 清单文件已生成"
        content = (
            f"时间：{now_str}\n"
            f"清单路径：{manifest_path}\n"
            f"汇总文件数：{file_count}\n"
            f"总占用空间：{total_size / 1024 / 1024:.1f} MB"
        )

    elif event_type == "storage_warning":
        used = event_data.get("used", 0)
        total = event_data.get("total", 0)
        ratio = (used / total * 100) if total > 0 else 0
        title = "⚠️ 网盘存储空间告警"
        content = (
            f"时间：{now_str}\n"
            f"已用空间：{used / 1024 / 1024 / 1024:.2f} GB\n"
            f"总空间：{total / 1024 / 1024 / 1024:.2f} GB\n"
            f"使用率：{ratio:.1f}%"
        )

    else:
        title = f"HA 备份通知：{event_type}"
        content = f"时间：{now_str}\n事件类型：{event_type}"

    return {"title": title, "content": content}


# ============================================================================
# 统一发送接口
# ============================================================================
def send_notification(
    channel: str, config: Dict[str, Any], title: str, content: str
) -> bool:
    """向指定渠道发送通知。

    Args:
        channel: 通知渠道名称（"email" / "wechat" / "dingtalk" / "feishu"）
        config: 该渠道的配置字典
        title: 通知标题
        content: 通知正文

    Returns:
        True 表示发送成功，False 表示失败或跳过
    """
    sender = _CHANNEL_SENDERS.get(channel)
    if sender is None:
        _log(f"不支持的通知渠道：{channel}")
        return False
    return sender(config, title, content)


def test_notification(channel: str, config: Dict[str, Any]) -> bool:
    """测试指定通知渠道的连接和发送。

    Args:
        channel: 通知渠道名称
        config: 该渠道的配置字典

    Returns:
        True 表示测试发送成功
    """
    test_title = f"【测试通知】百度网盘备份插件 — {channel} 渠道测试"
    test_content = f"这是一条测试消息，来自百度网盘备份插件 v1.1.3。\n渠道：{channel}\n时间：{datetime.now().strftime(TIME_FORMAT)}"
    return send_notification(channel, config, test_title, test_content)


def notify_event(
    notifications: Dict[str, Any],
    event_type: str,
    event_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """统一通知入口 — 遍历所有已启用的通知渠道，向每个渠道发送事件通知。

    配置结构：
        notifications = {
            "enabled": True,           # 全局开关
            "events": {                # 事件级开关
                "backup_success": True,
                ...
            },
            "channels": {
                "email": {
                    "enabled": True,
                    ...
                },
                "wechat": { ... },
                "dingtalk": { ... },
                "feishu": { ... },
            }
        }

    Args:
        notifications: notifications 配置段
        event_type: 事件类型
        event_data: 事件数据

    Returns:
        {"sent": int, "failed": int, "skipped": int, "results": dict}
    """
    if not isinstance(notifications, dict):
        _log("通知配置缺失，跳过通知发送")
        return {"sent": 0, "failed": 0, "skipped": 0, "results": {}}
    notif = notifications

    # 全局开关
    if not notif.get("enabled", True):
        _log("全局通知已禁用，跳过通知发送")
        return {"sent": 0, "failed": 0, "skipped": 0, "results": {}}

    # 事件级开关（新事件用 DEFAULT_EVENT_ENABLED 的默认值，既有事件仍默认 True）
    events_cfg = notif.get("events", {})
    default_enabled = DEFAULT_EVENT_ENABLED.get(event_type, True)
    if isinstance(events_cfg, dict) and not events_cfg.get(event_type, default_enabled):
        _log(f"事件类型 {event_type} 的通知已禁用，跳过")
        return {"sent": 0, "failed": 0, "skipped": 0, "results": {}}

    # 格式化消息
    msg = _format_event_message(event_type, event_data)
    title = msg["title"]
    content = msg["content"]

    # 遍历渠道
    channels_cfg = notif.get("channels", {})
    if not isinstance(channels_cfg, dict):
        return {"sent": 0, "failed": 0, "skipped": 0, "results": {}}

    sent = 0
    failed = 0
    skipped = 0
    results: Dict[str, str] = {}

    for channel_name, chan_cfg in channels_cfg.items():
        if not isinstance(chan_cfg, dict):
            continue
        if not chan_cfg.get("enabled", False):
            skipped += 1
            continue

        _log(f"发送 {event_type} 通知 → {channel_name} 渠道")
        try:
            ok = send_notification(channel_name, chan_cfg, title, content)
            if ok:
                sent += 1
                results[channel_name] = "success"
            else:
                failed += 1
                results[channel_name] = "failed"
        except Exception as e:
            _log(f"渠道 {channel_name} 发送异常：{e}")
            failed += 1
            results[channel_name] = f"error: {e}"

    return {"sent": sent, "failed": failed, "skipped": skipped, "results": results}