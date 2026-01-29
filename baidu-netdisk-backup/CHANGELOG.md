# Changelog

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
