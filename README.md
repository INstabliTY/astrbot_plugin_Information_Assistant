<div align="center">

<img src="./logo.png" alt="Plugin Logo" width="150" height="150" />

# AstrBot 全能商业助理插件 (Market Daily News)

**让你的 AstrBot 化身最强个人情报中枢：天气、日程、商业速报、资产监控一网打尽！**

[![version](https://img.shields.io/badge/version-1.0.0-blue.svg)](#) [![license](https://img.shields.io/badge/license-MIT-green.svg)](#) [![AstrBot](https://img.shields.io/badge/AstrBot->=4.20.0-orange.svg)](#)

</div>

## 📑 目录
- [✨ 功能特性 (快速了解)](#-功能特性-快速了解)
- [🚀 如何安装 (快速开始)](#-如何安装-快速开始)
- [💻 核心指令](#-核心指令)
- [⚙️ 配置说明](#️-配置说明)
- [📊 数据来源](#-数据来源)
- [⚠️ 免责声明](#️-免责声明)
- [📅 更新日志](#-更新日志)

---

## ✨ 功能特性 (快速了解)

本插件专为对时间管理与信息密度有极高要求的打工人/商科生打造，每天早晨自动推送极致排版的纯文本情报看板：
- ⛅ **智能天气与穿衣指南**：无需配置 Key，自动根据城市返回最高/低温度、降水概率及穿衣防雨建议。
- 📅 **超强本地日程防忘**：内置轻量级本地存储，支持添加备忘，自动播报“今日待办”与“本周预警”。
- 📰 **极速纯文本新闻速递**：直接抓取 60s 读懂世界数据源，去除冗余图片与微语，打造极简公众号级排版体验。
- 💱 **多币种实时汇率播报**：支持自定义基础货币，实时监控外汇波动。
- 💰 **AI 资产大盘监控**：同时展示 DeepSeek（支持 CNY/USD 双币种解析）与 Moonshot (Kimi) 的实时 API 余额。
- ⚡ **毫秒级并发请求**：底层采用 `asyncio.gather` 与会话连接池重构，5大接口并发请求，告别平台超时断联！

---

## 🚀 如何安装 (快速开始)

提供以下三种安装方式，推荐使用方式一：

### 1. AstrBot 插件市场下载安装 (推荐)
直接在 AstrBot 的 WebUI 管理面板的“插件市场”中，搜索 `astrbot_plugin_mkt_daily_news` 并点击安装即可。

### 2. 指令安装
向你的机器人发送以下指令直接拉取安装：
```text
/plugin install astrbot_plugin_mkt_daily_news
```

### 3. GitHub 源码下载安装
进入 AstrBot 的插件目录，使用 Git 克隆本仓库：
```text
cd data/plugins/
git clone [https://github.com/INstabliTY/astrbot_plugin_mkt_daily_news.git](https://github.com/INstabliTY/astrbot_plugin_mkt_daily_news.git)
```
   📝 注意：
> 请确保克隆后的文件夹名称严格为 astrbot_plugin_mkt_daily_news
> 
> ⚠️ 安装完成后，请务必重启 AstrBot 主程序使插件生效！

---

## 💻 核心指令
指令,说明,示例
/今日情报,立即手动触发一次完整的商业情报推送,/今日情报
/添加提醒,向本地日历中添加一条待办事项,/添加提醒 2026-03-16 记得微观经济学小测

---

## ⚙️ 配置说明
在 AstrBot WebUI 的“插件配置”页面，你可以可视化修改以下参数：

每日定时推送时间: 24小时制，例如 08:00。

推送目标群组/用户 ID: 极其关键！请带上平台前缀。例如向 Telegram 个人推送请填写：telegram:FriendMessage:你的UID；向 QQ 群推送请填写 aiocqhttp:GroupMessage:你的群号。

所在城市: 用于天气预报，如 北京。

汇率 API Key: 前往 ExchangeRate-API 免费注册获取。

大模型 API Key: 选填 DeepSeek 和 Kimi 的 API Key 用于资产监控。

---

## 📊 数据来源 (Data Sources)
本插件的数据来源于互联网公开接口及官方 API，具体如下：

| 模块 | 数据内容 | 数据来源 | 来源网址 | 获取方式 |
| :--- | :--- | :--- | :--- | :--- |
| 天气预报 | 经纬度及气象数据 | Open-Meteo | https://open-meteo.com/ | API 调用 (免Key)
| 新闻速读 | 每日60s文本新闻 | 社区公益 API | https://60s.viki.moe/ | API 调用 (免Key)
| 实时汇率 | 法币汇率换算 | ExchangeRate-API | https://www.exchangerate-api.com/ | API 调用
| AI 额度 | DeepSeek 账户余额 | DeepSeek 官方 | https://platform.deepseek.com/ | 官方 API
| AI 额度 | Kimi 账户余额 | Moonshot 官方 | https://platform.moonshot.cn/ | 官方 API

   📝 注意：

> 汇率 数据来源 ExchangeRate-API，请自行注册并获取 API 密钥填入配置。
> 
> AI 额度 数据来源使用官方 API，请在配置页面填写您自己的 API 密钥。如果未填写，看板将自动跳过报错并显示“未配置”。

---

## ⚠️ 免责声明
我还是个初学者：这是我初次尝试编写并开源 AstrBot 插件，代码如有不优雅之处，欢迎各位大佬提 PR 指正！

测试环境声明：目前本插件仅在 Telegram 平台上测试成功并完美排版。QQ、飞书、微信等其他平台的长文本换行解析机制可能存在差异，若出现排版问题，请见谅或提交 Issue 反馈。

本插件仅用于学习和交流，新闻数据均来自开源公益接口，对内容的真实性及有效性不作保证。

---

## 📅 更新日志
v1.0.0 (当前版本)
*🎉 首次正式发布！

*✨ 新增 Open-Meteo 天气与防雨穿衣提醒。

*✨ 新增本地超强 reminders.json 待办事项管理模块。

*✨ 优化 60s 新闻为纯文本输出。

*✨ 修复网络堵塞问题，采用全局 ClientSession 和 asyncio.gather 实现高并发极速请求。

*✨ 修复 DeepSeek 仅显示单币种的问题，现支持 CNY/USD 双币种余额同时展示。
