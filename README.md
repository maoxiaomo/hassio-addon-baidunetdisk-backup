# 百度网盘备份 - Home Assistant Add-on

![Version](https://img.shields.io/badge/version-1.0.2-blue.svg)
![Auth](https://img.shields.io/badge/认证方式-OAuth_2.0-green.svg)
![Python](https://img.shields.io/badge/Python-3.11-yellow.svg)

一个用于 Home Assistant 的 Supervisor 加载项 (Add-on)，可以自动将您的 HA 备份文件同步上传到百度网盘。

> **v1.0.2 更新发布！**
>
> 采用与 **AList** 完全相同的百度官方 OAuth 2.0 接口和 API 调用方式，稳定可靠。

---

## ✨ 核心功能

- **🚀 官方认证**：使用百度官方 OAuth 2.0 授权接口，稳定可靠，无封锁风险。
- **🔄 自动续期**：内置 Token 自动刷新机制，一次登录，永久有效。
- **⚡ 极速秒传**：利用百度网盘秒传机制，GB 级文件也能瞬间完成备份。
- **📦 分片上传**：大文件自动分片上传，网络波动也能断点续传。
- **⏰ 灵活定时**：支持 Cron 表达式配置，精确控制备份时间。
- **🧹 分层保留**：支持远端备份分层保留策略（按日/周/月），自动清理旧备份，避免网盘容量持续增长。
- **🗂️ 目录模式（可选）**：在 `upload_path` 下自动创建 `每日/`、`每周/`、`每月/` 三个目录并分类存放（通过移动归档，不重复占用空间）。
- **🔀 目录迁移**：自动检测旧版英文目录（`daily/`、`weekly/`、`monthly/`）并将其中的备份文件迁移到对应的中文目录（`每日/`、`每周/`、`每月/`），升级用户无需手动干预。
- **📋 清单文件生成**：每次同步完成后，在网盘根目录自动生成 `清单文件.txt`，汇总各子目录的文件数量、总大小和日期范围，方便快速了解备份状态。
- **🔔 通知功能（待添加）**：支持 4 种通知渠道 — 邮箱、企业微信机器人、钉钉机器人、飞书机器人；覆盖 5 种事件类型 — 备份成功、备份失败、保留清理完成、Token 即将过期、异常错误。

---

## 🛠️ 安装方法

**地址**：https://gitee.com/mxmaimooo/hassio-addon-baidunetdisk-backup

**Github源**：https://github.com/maoxiaomo/hassio-addon-baidunetdisk-backup

### 🚀 一键安装（推荐）

点击下方按钮，直接在 Home Assistant 中添加本插件（使用 Gitee 国内源）：

[![Open your Home Assistant instance and show the add-on store.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgitee.com%2Fmxmaimooo%2Fhassio-addon-baidunetdisk-backup)

### 手动安装
1. 复制上面的 Gitee 地址。
2. 进入 Home Assistant：**配置** > **加载项** > **加载项商店**。
3. 点击右上角菜单（三个点） > **仓库**。
4. 粘贴地址并点击 **添加**。
5. 刷新页面，找到 **百度网盘备份** 并点击安装。

---

## 🔑 获取 Refresh Token（重要！）

> 🛑 **操作前必读**：请严格按照以下顺序操作，否则可能导致授权失败。

### 第一步：登录百度网盘
请先在浏览器中打开 [百度网盘官网](https://pan.baidu.com) 并**登录您的百度账号**。
*(建议使用 PC 端浏览器操作)*

### 第二步：获取授权
登录成功后，点击下方链接进行授权：

👉 **[点击这里获取 Refresh Token](https://openapi.baidu.com/oauth/2.0/authorize?response_type=code&client_id=hq9yQ9w9kR4YHj1kyYafLygVocobh7Sf&redirect_uri=https://alistgo.com/tool/baidu/callback&scope=basic,netdisk&qrcode=1)**

### 第三步：复制 Token
授权页面会显示如下 JSON 信息，请复制 **`refresh_token`** 后面的字符串：

```json
{
  "access_token": "121.xxxxxxxx...",
  "refresh_token": "122.a0b1c2d3...",   <-- 复制这一长串字符
  "expires_in": 2592000
}
```

> **注意**：只复制 `122.` 开头的那一长串字符，不要包含引号。

### 第四步：填入配置
回到 Home Assistant Add-on 的 **配置** 页面，将复制的字符粘贴到 `refresh_token` 字段中。

---

## ⚙️ 配置说明

| 配置项 | 必填 | 默认值 | 说明 |
| :--- | :---: | :--- | :--- |
| `refresh_token` | ✅ | (无) | **必填**。从上一步获取的令牌。 |
| `upload_path` | ❌ | `/HomeAssistant/Backup` | 网盘中的目标文件夹路径。会自动创建。 |
| `schedule` | ❌ | `0 5 * * *` | 定时任务的 Cron 表达式。 |
| `retention.daily` | ❌ | (不启用) | 远端保留：按"天"保留最近 N 份（同一天多份只保留最新一份）。 |
| `retention.weekly` | ❌ | (不启用) | 远端保留：按"周"保留最近 N 份（同一周只保留最新一份）。 |
| `retention.monthly` | ❌ | (不启用) | 远端保留：按"月"保留最近 N 份（同一月只保留最新一份）。 |
| `retention.use_folders` | ❌ | `false` | 是否启用目录模式。启用后会在 `upload_path` 下使用 `每日/`、`每周/`、`每月/` 三个中文子目录。**首次启用时会自动将旧版英文目录（`daily/`、`weekly/`、`monthly/`）中的文件迁移到新目录**。 |
| `retention_use_folders` | ❌ | `false` | （扁平配置方式）同 `retention.use_folders`，二选一即可。 |
| `retention_daily` | ❌ | `7` | （扁平配置方式）同 `retention.daily`。 |
| `retention_weekly` | ❌ | `4` | （扁平配置方式）同 `retention.weekly`。 |
| `retention_monthly` | ❌ | `12` | （扁平配置方式）同 `retention.monthly`。 |

> **配置方式说明**：支持两种配置方式 — **嵌套方式**（`retention.daily` 等）和 **扁平方式**（`retention_daily` 等）。嵌套方式优先级更高，两种方式二选一即可，不要同时使用。

### 📝 配置示例

**嵌套配置方式（推荐）**：

```yaml
refresh_token: "122.a0b1c2d3e4f5g6h7i8j9k0l1m2n3o4p5..."
upload_path: "/HomeAssistant/Backup"
schedule: "0 5 * * *"  # 每天凌晨5点执行

# 远端分层保留（推荐：daily=7、weekly=4、monthly=12）
retention:
  daily: 7
  weekly: 4
  monthly: 12

# 可选：开启目录模式（云端使用 /HomeAssistant/Backup/每日|每周|每月 三个目录）
  use_folders: true
```

**扁平配置方式**：

```yaml
refresh_token: "122.a0b1c2d3e4f5g6h7i8j9k0l1m2n3o4p5..."
upload_path: "/HomeAssistant/Backup"
schedule: "0 5 * * *"
retention_use_folders: true
retention_daily: 7
retention_weekly: 4
retention_monthly: 12
```

### ⏰ Cron 表达式参考

- `0 5 * * *` : 每天凌晨 5:00 (默认)
- `30 2 * * *` : 每天凌晨 2:30
- `0 */4 * * *` : 每 4 小时执行一次
- `0 1 * * 1` : 每周一凌晨 1:00

---

## 📂 工作机制

每次任务执行时，插件会按以下顺序完成完整的同步和处理流程：

### 1. 启动检查
插件启动时，会立即扫描 `/backup` 目录，并尝试上传**所有** `.tar` 备份文件。

### 2. 定时轮询
根据 `schedule` 设定的时间，定期唤醒并执行全量同步任务。

### 3. 智能上传
- 上传前会自动计算文件 MD5。
- 如果网盘中已存在相同文件，则触发**秒传**，无需消耗流量。
- 只有新文件或未上传的文件才会进行实际传输。
- 已上传过的文件通过本地缓存（文件名 + 大小 + 修改时间）识别，跳过重复上传。

### 4. 目录迁移（自动）
- 启用目录模式后，首次运行会自动检测 `upload_path` 下是否存在旧版英文目录（`daily/`、`weekly/`、`monthly/`）。
- 如果存在，插件会自动将其中的文件迁移到对应的中文目录（`每日/`、`每周/`、`每月/`），然后删除旧目录。
- 迁移过程不重复占用云端存储空间（通过移动操作实现），且迁移完成后后续所有操作均基于中文目录进行。
- 如果当前系统已经是中文目录，则跳过此步骤。

### 5. 清单文件生成
- 同步和目录迁移完成后，插件会扫描 `upload_path` 下的所有子目录。
- 自动生成 `清单文件.txt` 并上传到 `upload_path` 根目录。
- 清单内容包括：每个子目录的文件数量、总占用空间、最早/最晚备份日期，以及整体汇总统计。

### 6. 分层保留策略

插件支持两种保留模式，通过 `retention.use_folders` 切换：

#### 模式 A：扁平模式（`use_folders: false`，默认）
- 所有备份存放在 `upload_path` 根目录下。
- 按日/周/月三个维度保留：每天保留最新 1 份、每周保留最新 1 份、每月保留最新 1 份。
- 不符合保留规则的文件会被自动删除。
- 三层保留策略之间存在去重：已被高层级（如"月"）保留的文件不再被低层级重复保留。

#### 模式 B：目录模式（`use_folders: true`）
- 备份文件按层级自动归档到三个中文子目录：
  - `每日/` — 按日保留的近期备份
  - `每周/` — 按周保留的中期备份
  - `每月/` — 按月保留的长期备份
- 归档流程采用"逐级晋升"机制：
  1. 先从 `每日/` 中挑选符合条件的备份，**晋升**到 `每月/`
  2. 再从剩余 `每日/` 文件中，挑选符合条件的备份，**晋升**到 `每周/`
  3. 最后对三个目录分别执行数量上限清理
- 晋升通过**移动操作**完成，不额外占用云端存储空间。
- 优先检测 `每月/` 是否已有同月备份，避免重复晋升。

---

## 🔔 通知功能（待添加）

> ⚠️ 此功能尚未实现，以下为计划中的设计说明。

### 支持的通知渠道

| 渠道 | 说明 |
| :--- | :--- |
| **邮箱 (SMTP)** | 通过 SMTP 服务器发送邮件通知，支持自定义发件人和收件人。 |
| **企业微信机器人** | 通过 Webhook 地址向企业微信群机器人推送消息。 |
| **钉钉机器人** | 通过 Webhook 地址向钉钉群机器人推送消息，支持加签安全设置。 |
| **飞书机器人** | 通过 Webhook 地址向飞书群机器人推送消息，支持签名校验。 |

### 支持的事件类型

| 事件 | 触发条件 | 用途 |
| :--- | :--- | :--- |
| **备份成功** | 同步任务成功完成（无错误） | 确认备份正常运行 |
| **备份失败** | 同步过程中出现异常或错误 | 及时发现并排查问题 |
| **保留清理完成** | 分层保留策略执行完毕 | 了解云端空间释放情况 |
| **Token 即将过期** | Access Token 刷新失败或即将过期 | 提前干预避免备份中断 |
| **异常错误** | 插件运行中发生未预期的错误 | 紧急问题快速响应 |

### 计划配置示例

```yaml
notifications:
  email:
    enabled: true
    smtp_host: "smtp.gmail.com"
    smtp_port: 587
    smtp_user: "your-email@gmail.com"
    smtp_password: "your-password"
    to: "receiver@example.com"

  wecom_bot:
    enabled: false
    webhook_url: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"

  dingtalk_bot:
    enabled: false
    webhook_url: "https://oapi.dingtalk.com/robot/send?access_token=xxx"
    secret: ""  # 加签密钥（可选）

  feishu_bot:
    enabled: false
    webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
    secret: ""  # 签名校验密钥（可选）

  events:
    - backup_success
    - backup_failure
    - retention_cleanup
    - token_expiring
    - error
```

---

## ❓ 常见问题 (FAQ)

#### Q: 点击授权链接提示错误？
**A:** 这通常是因为您没有先在浏览器中登录百度网盘。请先访问 pan.baidu.com 登录，然后再点击授权链接。

#### Q: 备份文件在哪里？
**A:** 默认情况下文件位于百度网盘的 **`/HomeAssistant/Backup`** 目录下。若开启目录模式（`retention.use_folders: true`），则会存放在中文子目录中：

- `upload_path/每日/`
- `upload_path/每周/`
- `upload_path/每月/`

#### Q: 我之前用的是英文目录（daily/weekly/monthly），升级后怎么办？
**A:** 无需手动操作。启用目录模式后，插件首次运行时会**自动检测**旧版英文目录，并将其中所有备份文件迁移到对应的中文目录。迁移通过**移动**操作完成，不会重复占用网盘空间，迁移完成后旧目录会被自动删除。

#### Q: 清单文件是什么？在哪里查看？
**A:** 清单文件（`清单文件.txt`）是插件每次同步完成后自动生成的一份汇总文件，存放在 `upload_path` 根目录。内容包括每个子目录的文件数量、总大小和日期范围。您可以直接在百度网盘中打开查看，快速了解当前备份的整体状态。

#### Q: Token 会过期吗？需要定期更换吗？
**A:** 不需要。插件内置了 Token 自动刷新机制，只要您不取消授权，Token 会一直自动续期。

#### Q: 为什么日志显示 "Rapid upload (秒传) successful"？
**A:** 这表示您的备份文件在百度网盘云端已经存在（可能是您手动上传过，或者之前的任务已经上传成功），因此插件跳过了实际传输，直接完成了"上传"。这是正常且高效的表现。

#### Q: 本地删除了备份，网盘上也会删除吗？
**A:** 分两种情况：

- **未启用保留策略**（不配置 `retention.daily/weekly/monthly`）：网盘不会自动删除，表现为"只增不减"。
- **启用保留策略**（配置了 `retention.daily/weekly/monthly`）：插件会在云端按保留规则自动清理旧备份。

#### Q: 目录模式和扁平模式有什么区别？该选哪个？
**A:**

| 对比维度 | 扁平模式 | 目录模式 |
| :--- | :--- | :--- |
| 文件存放 | 全部在 `upload_path` 根目录 | 按日/周/月分目录存放 |
| 目录语言 | 无子目录 | 中文目录名（`每日/每周/每月`） |
| 视觉直观 | 文件混在一起，不易区分 | 一目了然，按时间层级分类 |
| 晋升机制 | 无（仅保留/删除） | 有（文件在目录间晋升移动） |
| 适用场景 | 简单场景，备份量少 | 备份量大，需要长期归档 |

如果启用了分层保留策略（daily/weekly/monthly），推荐使用目录模式以获得更好的组织性。

---

## 🛠️ 技术栈与致谢

本项目基于以下优秀的开源技术构建：

- **Python 3**: 核心逻辑实现
- **Requests**: 处理 HTTP 通讯与 API 交互
- **Alpine Linux**: 提供超轻量级的 Docker 运行环境
- **AList**: 感谢 [AList](https://github.com/alist-org/alist) 项目提供的百度网盘 OAuth 2.0 鉴权方案，极大地简化了开发流程。

---

## 问题反馈

> ⚠️ **声明**：本项目目前处于**维护阶段**，遵循「能用就不改」原则。
> 
> 作者只会修复影响**自己使用**的问题，一切以个人利益为重。欢迎 Fork 自行修改。