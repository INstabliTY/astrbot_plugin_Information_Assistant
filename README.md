<div align="center">

<img src="./logo.png?v=2" alt="Plugin Logo" width="150" height="150" />

# AstrBot 资讯助理 (Information Assistant)

**让你的 AstrBot 化身个人情报中枢：天气、日程、新闻速报、汇率、资产监控一网打尽**

[![version](https://img.shields.io/badge/version-1.3.2-blue.svg)](#) [![license](https://img.shields.io/badge/license-MIT-green.svg)](#) [![AstrBot](https://img.shields.io/badge/AstrBot->=4.20.0-orange.svg)](#)

</div>

## 📑 目录
- [✨ 功能特性](#-功能特性)
- [🚀 安装方式](#-安装方式)
- [💻 指令列表](#-指令列表)
- [⚙️ 配置说明](#️-配置说明)
- [📊 数据来源](#-数据来源)
- [⚠️ 免责声明](#️-免责声明)
- [📅 更新日志](#-更新日志)

---

## ✨ 功能特性

本插件专为对时间管理与信息密度有高要求的用户打造，每天定时推送纯文本情报看板：

- ⛅ **智能天气与穿衣建议**：无需 API Key，根据城市返回当日温度区间、降水概率及穿衣防雨建议。
- 📅 **AI 智能提醒摘要**：自动读取 AstrBot 系统定时任务（通过与 AI 对话添加的提醒）与本地备忘录，经 AI 提炼为简洁一行摘要展示，告别冗长原文。
- 📰 **纯文本新闻速递**：抓取「60秒读懂世界」数据源，去除图片与冗余信息，纯文字呈现。
- 💱 **多币种实时汇率**：双向展示（`1 本币 = x 外币 | 100 外币 ≈ y 本币`），支持自定义本币与关注币种。
- 💰 **AI API 余额监控**：同步展示 DeepSeek（CNY/USD 双币种）与 Moonshot (Kimi) 的实时余额，避免余额耗尽。
- 🔀 **自定义板块顺序**：通过配置文件自由调整情报各板块的展示顺序，无需修改代码。
- 🧩 **全模块独立开关**：每个功能模块均可单独开启/关闭，按需组合。

---

## 🚀 安装方式

### 方式一：插件市场（推荐）
在 AstrBot WebUI 管理面板的「插件市场」中搜索 `资讯助理` 点击安装即可。

### 方式二：指令安装
向机器人发送：
```
/plugin install astrbot_plugin_Information_Assistant
```

### 方式三：Git 克隆
```bash
cd data/plugins/
git clone https://github.com/INstabliTY/astrbot_plugin_Information_Assistant.git
```

> ⚠️ 安装完成后请重启 AstrBot 主程序使插件生效。

---

## 💻 指令列表

| 指令 | 说明 | 示例 |
| :--- | :--- | :--- |
| `/今日情报` | 立即手动触发一次完整的情报推送 | `/今日情报` |
| `/添加提醒` | 向本地备忘录添加一条提醒事项 | `/添加提醒 2026-04-01 记得交作业` |
| `/提醒诊断` | 诊断提醒模块状态，显示数据库路径、提醒条目及时区信息，排查提醒不显示问题 | `/提醒诊断` |

---

## ⚙️ 配置说明

所有配置均可在 AstrBot WebUI「插件配置」页面可视化修改，无需手动编辑文件。

### 🚀 推送设置
| 配置项 | 说明 |
| :--- | :--- |
| 启用每日定时推送 | 主开关，关闭后只响应手动指令 |
| 每日推送时间 | 24 小时制，格式 `HH:MM`，如 `08:30` |
| 推送目标 ID 列表 | 格式：`平台:消息类型:目标ID`。Telegram 私聊填 `telegram:FriendMessage:你的数字ID`；QQ 群填 `aiocqhttp:GroupMessage:群号` |
| 运行时区（UTC 偏移量） | 中国大陆 `8`；澳洲东部标准时 `10`，夏令时 `11` |

### 🌤️ 天气模块
| 配置项 | 说明 |
| :--- | :--- |
| 启用天气模块 | 开关 |
| 城市名称 | 支持中英文，如 `上海` / `Brisbane`。无需 API Key |

### 📝 提醒模块
| 配置项 | 说明 |
| :--- | :--- |
| 启用待办提醒模块 | 开关 |
| 提醒摘要 AI 模型 | 下拉选择用于将提醒原文提炼为简洁摘要的模型，留空使用当前默认模型 |
| 本周预警展示范围（天） | 从今天起往后几天内的提醒纳入展示，默认 7 天 |

> **提醒来源说明：** 插件会自动合并两路数据——① 通过与 AI 对话添加的系统定时任务（存入 AstrBot 内置数据库）；② 通过 `/添加提醒` 指令手动录入的本地备忘（存入插件目录 `reminders.json`）。两路数据去重后，经 AI 提炼为简洁格式统一展示。

### 📊 汇率模块
| 配置项 | 说明 |
| :--- | :--- |
| 启用汇率模块 | 开关 |
| ExchangeRate-API Key | 前往 [exchangerate-api.com](https://www.exchangerate-api.com) 免费注册获取，每月 1500 次免费额度 |
| 本币代码 | 你持有的货币，如 `CNY`、`AUD`、`USD` |
| 关注的外币代码 | 英文逗号分隔，如 `USD,JPY,EUR,AUD`，需符合 ISO 4217 标准 |

### 💰 AI 余额监控
| 配置项 | 说明 |
| :--- | :--- |
| 启用余额监控 | 开关，两个 Key 均未填写时即使开启也不显示 |
| DeepSeek API Key | 前往 [platform.deepseek.com](https://platform.deepseek.com) 获取 |
| Moonshot (Kimi) API Key | 前往 [platform.moonshot.cn](https://platform.moonshot.cn) 获取 |

### ⚙️ 高级设置
| 配置项 | 说明 |
| :--- | :--- |
| 网络请求超时（秒） | 所有外部接口的统一超时时长，默认 20 秒，建议范围 10～60 |
| 情报板块展示顺序 | 用英文逗号分隔模块名，调整情报中各板块排列顺序。可用名称：`weather` / `reminders` / `exchange` / `balance` / `news`。默认：`weather,reminders,exchange,balance,news` |

---

## 📊 数据来源

| 模块 | 数据内容 | 来源 | 地址 | 获取方式 |
| :--- | :--- | :--- | :--- | :--- |
| 天气预报 | 气温、降水概率 | Open-Meteo | [open-meteo.com](https://open-meteo.com) | 免费，无需 Key |
| 新闻速读 | 每日 60s 文本新闻 | 社区公益 API | [60s.viki.moe](https://60s.viki.moe) | 免费，无需 Key |
| 实时汇率 | 法币汇率换算 | ExchangeRate-API | [exchangerate-api.com](https://www.exchangerate-api.com) | 需注册获取 Key |
| AI 余额（DeepSeek） | 账户可用余额 | DeepSeek 官方 | [platform.deepseek.com](https://platform.deepseek.com) | 需填入 API Key |
| AI 余额（Kimi） | 账户可用余额 | Moonshot 官方 | [platform.moonshot.cn](https://platform.moonshot.cn) | 需填入 API Key |
| 系统提醒 | 定时任务数据 | AstrBot 内置数据库 | — | 自动读取，无需配置 |

---

## ⚠️ 免责声明

本项目由个人开发者维护，目前仅在 **Telegram** 平台完整测试。QQ、飞书、微信等平台的长文本换行机制存在差异，若出现排版问题请提 Issue 反馈。

新闻数据均来自开源公益接口，对内容的真实性及时效性不作保证，仅供参考。

---

## 📅 更新日志

### 🚀 v1.3.1（当前版本）

**新功能**

- ✨ **AI 智能提醒摘要**：每条提醒原文自动经 LLM 提炼为简洁一行摘要（格式：`【标签】核心事项  时间描述`），告别冗长原文。LLM 不可用时自动降级为截取原文前 60 字，不影响情报推送。
- ✨ **直读系统定时任务**：直接读取 AstrBot 内置数据库（`cron_jobs`）中通过 AI 对话添加的定时提醒，无需任何迁移操作，历史提醒自动呈现。
- ✨ **自定义板块展示顺序**：新增 `module_order` 配置项，用逗号分隔模块名即可自由排列情报各板块顺序。
- ✨ **指定提醒摘要模型**：可在配置面板快速选择专门用于提炼摘要的 AI 模型，与对话模型解耦。
- ✨ **本周预警范围可配置**：新增 `reminder_lookback_days`，自定义「本周预警」板块的展示天数（默认 7 天）。
- ✨ **网络超时可配置**：新增 `request_timeout`，统一控制所有外部接口的超时时长。
- ✨ **新增 `/提醒诊断` 指令**：一键显示数据库路径、提醒条目及时区状态，快速定位"有提醒却不显示"等问题。

**改动**

- 🔧 汇率展示改为双向格式（`1 本币 = x 外币 | 100 外币 ≈ y 本币`），消除歧义。
- 🗑️ 移除 `add_information_reminder` LLM 工具（现已直接读取系统数据库，该工具不再必要）。

---

<details>
<summary><b>📜 点击展开查看历史版本</b></summary>

<br>

### v1.3.0

- ✨ AI 提醒摘要功能初版上线。
- ✨ 直读 AstrBot 系统 `cron_jobs` 数据库，彻底解决"有提醒却不显示"问题。
- 🔧 `/提醒诊断` 指令初版上线。

---

### v1.2.x 系列

**v1.2.4 – v1.2.8**（多轮迭代）

- 🔒 提醒文件读写引入 `asyncio.Lock` 全事务保护，消除并发竞态。
- 💾 写入改为「先写 `.tmp` 再原子替换」策略，防止中断损坏文件。
- 🗑️ 读取时自动清理过期提醒，写回磁盘保持同步。
- 🛡️ 各网络接口精细化异常捕获（`aiohttp.ClientError` / `TimeoutError` / `JSONDecodeError`），HTTP 状态码记录至日志，不再静默失败。
- 🔒 API Key 不再出现在任何日志输出中。
- ✅ `target_groups` 与 `target_currencies` 兼容列表/字符串两种配置形式并自动 `strip()`。
- 🔧 `on_loaded()` 生命周期钩子延迟启动定时任务，避免 `RuntimeError`。
- 🗑️ 移除未使用的 `_save_reminders` 方法，统一写入入口为 `_atomic_write`。
- 🔧 汇率展示初版（此版本格式后经 v1.3.1 优化）。

**v1.2.3**

- 🔧 `_parse_config` 抽取为独立方法，`__init__` 职责清晰化。
- 🛡️ 时区偏移、推送时间格式均加入合法性校验与安全回退。
- 📁 数据持久化路径改为 `StarTools.get_data_dir()`，符合框架规范，升级重装不丢数据。
- 🔧 `llm_tool` 注册的 `add_reminder_tool` 改为 `return` 代替 `yield`，修复大模型无法收到工具结果的问题。

**v1.2.0 – v1.2.2**

- 🏗️ 移除 APScheduler，改用原生 `asyncio.create_task` + 定时循环，与 AstrBot 事件循环解耦。
- ♻️ 文件 I/O 改为 `asyncio.to_thread` 异步化，不再阻塞事件循环。
- 🔧 `/添加提醒` 指令参数解析逻辑重构，对用户输入格式更鲁棒。
- 🛡️ `terminate()` 改为 `async def`，正确支持异步任务取消。

---

### v1.1.0

- ✏️ 待办事项双通道写入机制，LLM 工具与手动指令统一入口。
- ⚙️ 升级为卡片式可视化配置面板，支持模块级独立开关。
- 🛠️ 修复时区读取异常与 JSON 解析崩溃。

---

### v1.0.x

**v1.0.3** — 加入定时推送全局总开关与模块独立开关。

**v1.0.2** — 插件正式更名为「资讯助理 Information_Assistant」。

**v1.0.1** — 新增 API 余额监控（DeepSeek / Moonshot Kimi）。

**v1.0.0** — 首次发布。天气、本地提醒、60s 新闻、并发请求架构。

</details>
