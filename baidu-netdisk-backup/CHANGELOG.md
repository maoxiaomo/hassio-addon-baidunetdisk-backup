# Changelog

## 1.2.0

### 新增
- **Web UI 增加"配置编辑"标签页**：所有 HA Add-on 配置项以中文表单形式呈现（基础配置、保留策略、通知全局、事件开关、4 个渠道），可直接修改并点击【保存并重启加载项】，无需到加载项配置页操作
- **保存后自动重启**：通过 Supervisor API (`POST /addons/self/restart`) 重启本加载项以使配置生效；Supervisor 不可用时给出明确提示
- **`config.yaml` 增加 `hassio_api: true`**：注入 `SUPERVISOR_TOKEN` 以便调用 Supervisor API

### 改进
- Web UI 改为双标签页布局：【通知测试】+【配置编辑】
- 测试与状态接口路径调整为 `./api/test/<channel>` / `./api/state` / `./api/config`
- 中文标签 + 字段说明，覆盖所有配置项；密码类字段使用 `<input type=password>`

## 1.1.4

### 修复
- **企业微信 `webhook_key` 兼容完整 URL**：当用户在 `webhook_key` 字段粘贴完整 Webhook URL（如 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx`）时，自动用 `urlparse` 提取 `?key=` 参数，避免出现 `invalid webhook url` 错误。原有只填 key 的写法仍然兼容

## 1.1.3

### 新增
- **Web UI 测试按钮**：加载项启用 HA Ingress，加载项页面顶部出现【打开 Web UI】按钮；点击进入页面后，4 个通知渠道（邮箱 / 企业微信 / 钉钉 / 飞书）各有一个【测试发送】按钮，可直接验证配置是否正确，无需重启加载项
- **新增模块 `web.py`**：基于 Python 内置 `http.server` 的轻量 Web 服务（端口 8099），后台守护线程运行，启动失败不影响主备份流程
- **页面状态显示**：实时显示各渠道的 `enabled` 和"配置已填/缺失"状态

### 备注
- Web UI 通过 HA Ingress 安全访问，不暴露端口到主机
- 测试通知内容为固定文案：`【测试通知】百度网盘备份插件 — <渠道> 渠道测试`
- 测试发送前会校验渠道 `enabled` 与必要字段；未启用或字段缺失时按钮会返回相应提示

## 1.1.2

### 新增
- **`storage_warning` 事件实装**：每次同步周期结束后调用百度官方容量接口 `/api/quota` 查询网盘使用率，超过阈值时触发通知
- **新增配置 `notifications.storage_warning_threshold`**：告警阈值，0-1 小数，默认 `0.9`（已用 90%）
- **`BaiduClient.get_quota()`**：封装 `https://pan.baidu.com/api/quota?checkfree=1&checkexpire=1`，返回 `{total, used, free, expire}`

### 备注
- 至此 5 个通知事件（`backup_success` / `backup_failure` / `migration_done` / `manifest_generated` / `storage_warning`）全部实装完成
- 默认 `storage_warning` 事件仍是关闭的，需要在配置中显式打开

## 1.1.1

### 新增
- **通知事件 `manifest_generated` 实装**：清单文件上传完成后会触发通知，内容包含清单路径、汇总文件数与总占用空间
- **通知事件 `migration_done` 实装**：旧版英文目录迁移到中文目录时触发通知，按源目录粒度逐一发送（含源/目标目录、迁移文件数）

### 备注
- `storage_warning` 仍未实装（需额外查询百度网盘容量 API）
- 默认配置仍将这两个事件关闭，需在配置中显式打开 `notifications.events.manifest_generated` / `notifications.events.migration_done`

## 1.1.0

### ⚠️ 破坏性变更
- **移除扁平配置方式**：不再支持顶层 `retention_use_folders` / `retention_daily` / `retention_weekly` / `retention_monthly` 字段，统一使用嵌套 `retention:` 块
- **默认开启目录模式**：`retention.use_folders` 默认值改为 `true`（之前是 `false`）

### 迁移指南
老用户需手动迁移配置：

旧配置（不再支持）：
```yaml
retention_use_folders: true
retention_daily: 7
retention_weekly: 4
retention_monthly: 12
```

新配置：
```yaml
retention:
  use_folders: true
  daily: 7
  weekly: 4
  monthly: 12
```

## 1.0.9

### 修复
- **容器时区错误**：Dockerfile 设置 `TZ=Asia/Shanghai` 并同步 `/etc/localtime`，修复日志时间显示为 UTC（比本地慢 8 小时）的问题

## 1.0.8

### 修复
- **s6-overlay 启动报错**：`config.yaml` 添加 `init: false`，让 `python3 /main.py` 直接作为 PID 1 运行，修复 `s6-overlay-suexec: fatal: can only run as pid 1` 错误

## 1.0.7

### 修复
- **配置保存失败 `Missing option 'retention' in root`**：移除 `config.yaml` schema 中嵌套的 `retention:` 定义（未标可选导致必填校验失败）。顶层 `retention_daily` / `retention_weekly` / `retention_monthly` / `retention_use_folders` 已覆盖所有场景；代码层仍兼容嵌套写法

## 1.0.6

### 新增
- **添加 `build.yaml`**：明确指定各架构基础镜像（Home Assistant 官方 Alpine 3.19 base），修复 Supervisor 构建报错 `base name (${BUILD_FROM}) should not be blank`

### 变更
- 仓库地址统一更新为 Gitee：`https://gitee.com/mxmaimooo/hassio-addon-baidunetdisk-backup`

## 1.0.5

### 修复（P0 严重）
- **钉钉加签算法错误**：修复 `notifier.py` 中钉钉签名使用 `.hex()` 而非 `base64.b64encode()` 的错误，导致加签模式 100% 失败。现已对齐官方算法，加签用户可正常收到通知

### 修复（P1 重要）
- **通知重试嵌套**：移除 `_send_wechat` / `_send_dingtalk` / `_send_feishu` 中的外层重试循环，避免与 `_retry_request` 内部重试叠加造成最多 9 次尝试 + 40+ 秒阻塞
- **邮件连接泄露**：`_send_email` 增加异常时的 `server.quit()` 清理，防止 login/sendmail 失败时 socket 泄露
- **空备份目录误报成功**：`run_sync_cycle` 增加 `success_count > 0` 判断，避免 `/backup` 为空时发送"备份成功"通知（total=0 / success=0 会让用户误以为异常）

### 文档
- README 通知配置示例补充 SMTP 端口说明（587=TLS / 465=SSL）和 `use_ssl` 对应关系
- README 和 `config.yaml` 标注 `migration_done` / `manifest_generated` / `storage_warning` 三个事件类型当前版本未实现触发逻辑，默认关闭

### 版本号
- `config.yaml` / `main.py` / `README.md` 版本号统一更新为 1.0.5

## 1.0.4

### 修复（P0 严重）
- **钉钉加签算法错误**：修复 `notifier.py` 中钉钉签名使用 `.hex()` 而非 `base64.b64encode()` 的错误，导致加签模式 100% 失败。现已对齐官方算法，加签用户可正常收到通知

### 修复（P1 重要）
- **通知重试嵌套**：移除 `_send_wechat` / `_send_dingtalk` / `_send_feishu` 中的外层重试循环，避免与 `_retry_request` 内部重试叠加造成最多 9 次尝试 + 40+ 秒阻塞
- **邮件连接泄露**：`_send_email` 增加异常时的 `server.quit()` 清理，防止 login/sendmail 失败时 socket 泄露
- **空备份目录误报成功**：`run_sync_cycle` 增加 `success_count > 0` 判断，避免 `/backup` 为空时发送"备份成功"通知（total=0 / success=0 会让用户误以为异常）

### 文档
- README 通知配置示例补充 SMTP 端口说明（587=TLS / 465=SSL）和 `use_ssl` 对应关系
- README 和 `config.yaml` 标注 `migration_done` / `manifest_generated` / `storage_warning` 三个事件类型当前版本未实现触发逻辑，默认关闭

### 版本号
- `config.yaml` / `main.py` / `README.md` 版本号统一更新为 1.0.4

## 1.0.3

### 修复
- **Cron 解析**：`schedule` 现在真正支持 5 字段 cron 表达式（`min hour dom mon dow`，含 `*` / `N` / `N-M` / `*/S` / `N,M,...`）。之前只解析 hour 字段，导致 `30 2 * * *`、`0 */4 * * *`、`0 1 * * 1` 等示例都不按预期触发；现已修正
- **清单文件名**：folder 模式下 `清单文件.txt` 现在使用固定文件名上传（之前是 `manifest_xxxxxx.txt` 随机名），不再在远端无限累积。**建议手动登录百度网盘删除 `upload_path/` 下历史遗留的 `manifest_*.txt` 文件**
- **folder 模式 `weekly/monthly=0` 误归档**：当用户只设了 `daily` 而未设 `weekly`/`monthly` 时，之前会错误地将每月/每周最新一份从 `每日/` 归档到 `每月/`/`每周/`；现在 `<=0` 表示"未启用 = 不归档/不清理"，符合直觉

### 新增
- **folder 模式顶层 `.tar` 下沉**：每次 retention 时，自动将 `upload_path/` 顶层遗留的 `.tar` 备份移动到 `每日/`（适用于 flat → folder 模式切换的存量数据；清单文件.txt 不受影响）
- **迁移完成标记**：`migrate_old_dirs` 增加 `/data/migration_done.flag`，首次迁移成功后跳过后续扫描，减少不必要的 API 调用

### 文档
- README 标注顶层 `retention_xxx` 字段为向后兼容字段，推荐使用嵌套 `retention:` 块
- README 修正分片上传描述（"自动重试 3 次"，而非"断点续传"）
- README 新增警告：手动在百度网盘删除备份后，需删除 `/data/upload_cache.json` 才能让插件重传

### 内部清理
- `schedule_loop` 移除对 `client._ensure_token()` 的越界调用（公开方法内部已自动 ensure）

## 1.0.2

### 变更
- Add-on 配置项补全：在 `config.yaml` 中暴露 `retention_use_folders` / `retention_daily` / `retention_weekly` / `retention_monthly`，并设置默认值（7/4/12）
- 目录模式归档顺序调整：优先归档到 `monthly/`，再归档到 `weekly/`，避免只有一份备份时 `monthly/` 为空
- 统一默认定时：`schedule` 默认值与配置/文档保持一致（`0 5 * * *`）

## 1.0.1

### 新增
- 支持远端备份分层保留策略（daily/weekly/monthly）
- 可选目录模式：在 `upload_path` 下自动创建 `daily/`、`weekly/`、`monthly/` 三个目录并分类存放（通过移动归档，不重复占用空间）

## 1.0.0 (正式版)

**百度网盘备份插件正式发布！**

### 核心功能
- **OAuth 2.0 认证**：采用与 AList 相同的百度官方接口，稳定可靠
- **自动续期**：Token 自动刷新，一次登录永久使用
- **秒传支持**：相同文件秒传，不重复消耗流量
- **分片上传**：大文件自动分片，支持 GB 级备份
- **定时任务**：支持 Cron 表达式，灵活控制备份时间
