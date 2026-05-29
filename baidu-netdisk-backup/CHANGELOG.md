# Changelog

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
