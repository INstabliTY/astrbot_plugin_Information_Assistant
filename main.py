"""
AstrBot 资讯助理插件 (Information Assistant)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
聚合天气、提醒、纯文本新闻、汇率与 API 余额监控。

Author : INstabliTY
Version: v1.3.9
"""

import asyncio
import datetime
import hashlib
import json
import os
import sqlite3
import traceback

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain


# ---------------------------------------------------------------------------
# 辅助常量
# ---------------------------------------------------------------------------
_DIVIDER = "\n\n---------------------------\n\n"


# ---------------------------------------------------------------------------
# 插件主类
# ---------------------------------------------------------------------------

@register(
    "astrbot_plugin_Information_Assistant",
    "资讯助理",
    "聚合天气、提醒、纯文本新闻与汇率",
    "1.3.9",
)
class InformationAssistantPlugin(Star):
    """资讯助理插件主类。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self._parse_config(config or {})

        # 数据持久化路径：通过框架提供的 StarTools.get_data_dir() 获取规范目录，
        # 返回 pathlib.Path 对象，防止插件更新/重装时数据被覆盖。
        data_dir = StarTools.get_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.reminders_file: str = str(data_dir / "reminders.json")
        self._ensure_reminders_file()
        self.reminder_cache_file: str = str(data_dir / "reminder_cache.json")
        # 文件操作锁：防止并发写入时后写覆盖先写，导致提醒数据丢失。
        self._file_lock: asyncio.Lock = asyncio.Lock()
        # 提醒摘要缓存（内存层），避免每次推送重复调用 LLM
        self._reminder_cache: dict[str, str] = {}
        self._cache_loaded: bool = False

        # 按框架指南推荐，直接在 __init__ 中用 asyncio.create_task() 注册后台任务。
        # AstrBot 的插件加载机制保证 __init__ 在事件循环运行后才被调用，
        # 因此 create_task 在此处始终安全。
        self._push_task: asyncio.Task | None = None
        if self.enable_push:
            self._push_task = asyncio.create_task(self._push_loop())
            logger.info(f"[资讯助理] 定时任务已启动，每天 {self.push_time} 推送。")
        else:
            logger.info("[资讯助理] 定时推送已关闭，以纯被动模式运行。")

    def _parse_config(self, cfg: dict) -> None:
        """
        从配置字典中解析并绑定所有插件参数。
        兼容新版 UI 的嵌套卡片结构（{section: {key: value}}）
        与旧版缓存的扁平结构（{key: value}）。
        """
        def _get(section: str, key: str, default):
            section_data = cfg.get(section)
            if isinstance(section_data, dict) and key in section_data:
                return section_data[key]
            if key in cfg:
                return cfg[key]
            return default

        # 全局推送设置
        self.enable_push: bool = _get("push_settings", "enable_push", True)
        self.push_time: str = _get("push_settings", "push_time", "08:00")
        raw_groups = _get("push_settings", "target_groups", [])
        # 兼容字符串配置（如误填 "12345"）和标准列表两种形式
        if isinstance(raw_groups, list):
            self.target_groups: list[str] = [
                str(g).strip() for g in raw_groups if g and str(g).strip()
            ]
        elif isinstance(raw_groups, str) and raw_groups.strip():
            # 单个字符串视为逗号分隔的列表
            self.target_groups = [g.strip() for g in raw_groups.split(",") if g.strip()]
        else:
            self.target_groups = []
        self.timezone_offset: float = self._parse_tz_offset(
            _get("push_settings", "timezone_offset", "10")
        )

        # 天气模块
        self.enable_weather: bool = _get("weather_settings", "enable_weather", True)
        self.city: str = _get("weather_settings", "city", "北京")

        # 提醒模块
        self.enable_reminders: bool = _get("reminder_settings", "enable_reminders", True)

        # 汇率模块
        self.enable_exchange: bool = _get("exchange_settings", "enable_exchange", True)
        self.exchange_api_key: str = _get("exchange_settings", "exchange_api_key", "")
        self.base_currency: str = _get(
            "exchange_settings", "base_currency", "CNY"
        ).upper()
        raw_currencies = _get(
            "exchange_settings", "target_currencies", "USD,JPY,EUR,GBP,HKD,AUD"
        )
        # 兼容配置中心返回 list（多选框）或 str（逗号分隔）两种形式
        if isinstance(raw_currencies, list):
            self.target_currencies: list[str] = [
                c.strip().upper() for c in raw_currencies if isinstance(c, str) and c.strip()
            ]
        else:
            self.target_currencies = [
                c.strip().upper() for c in str(raw_currencies).split(",") if c.strip()
            ]

        # 余额监控
        self.enable_balance: bool = _get("balance_settings", "enable_balance", True)
        self.deepseek_key: str = _get("balance_settings", "deepseek_key", "")
        self.moonshot_key: str = _get("balance_settings", "moonshot_key", "")

        # 新闻模块
        self.enable_news: bool = _get("news_settings", "enable_news", True)

        # 提醒模块扩展
        self.reminder_provider: str = _get("reminder_settings", "reminder_provider", "")
        self.reminder_lookback_days: int = max(
            1, min(30, int(_get("reminder_settings", "reminder_lookback_days", 7) or 7))
        )

        # 高级设置
        self.request_timeout: int = max(
            5, min(120, int(_get("advanced_settings", "request_timeout", 20) or 20))
        )
        raw_order = _get("advanced_settings", "module_order",
                         "weather,reminders,exchange,balance,news")
        self.module_order: list[str] = [
            m.strip() for m in str(raw_order).split(",") if m.strip()
        ] or ["weather", "reminders", "exchange", "balance", "news"]

    # -----------------------------------------------------------------------
    # 初始化工具方法
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_tz_offset(raw: str) -> float:
        """将时区偏移配置项解析为浮点数，容错范围 UTC-12 ~ UTC+14。"""
        try:
            offset = float(str(raw).strip())
            return max(-12.0, min(14.0, offset))
        except (ValueError, TypeError):
            logger.warning(f"[资讯助理] 时区配置 '{raw}' 无效，已回退至 UTC+10。")
            return 10.0

    def _ensure_reminders_file(self) -> None:
        """若提醒文件不存在则初始化为空列表。"""
        if not os.path.exists(self.reminders_file):
            with open(self.reminders_file, "w", encoding="utf-8") as f:
                json.dump([], f)

    @staticmethod
    def _cache_key(content: str, run_time: str) -> str:
        """以 content+run_time 的 SHA1 前16位作为缓存键，唯一标识一条提醒。"""
        raw = (content + "|" + run_time).encode("utf-8")
        return hashlib.sha1(raw).hexdigest()[:16]

    async def _load_cache(self) -> None:
        """从磁盘加载提醒摘要缓存到内存（只在首次调用时执行）。"""
        if self._cache_loaded:
            return
        def _read():
            if not os.path.exists(self.reminder_cache_file):
                return {}
            with open(self.reminder_cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        try:
            data = await asyncio.to_thread(_read)
            self._reminder_cache = data if isinstance(data, dict) else {}
        except Exception:
            self._reminder_cache = {}
        self._cache_loaded = True

    async def _save_cache(self) -> None:
        """将内存缓存原子写入磁盘。"""
        cache_snapshot = dict(self._reminder_cache)
        tmp = self.reminder_cache_file + ".tmp"
        def _write():
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache_snapshot, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.reminder_cache_file)
        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            logger.warning(f"[资讯助理] 提醒缓存写入失败（非致命）：{exc}")

    # -----------------------------------------------------------------------
    # 1. 天气模块
    # -----------------------------------------------------------------------

    async def fetch_weather(self, session: aiohttp.ClientSession) -> str:
        """获取今日天气及穿衣建议。"""
        if not self.city:
            return ""
        geo_url = (
            f"https://geocoding-api.open-meteo.com/v1/search"
            f"?name={self.city}&count=1&language=zh"
        )
        try:
            async with session.get(geo_url) as resp:
                geo_data = await resp.json()
            results = geo_data.get("results")
            if not results:
                logger.warning(f"[资讯助理] 天气地理编码无结果，城市：{self.city}")
                return f"🌤️ 【{self.city}天气】获取失败，请检查城市名拼写。"
            lat = results[0]["latitude"]
            lon = results[0]["longitude"]

            weather_url = (
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}"
                f"&daily=temperature_2m_max,temperature_2m_min"
                f",precipitation_probability_max,apparent_temperature_max"
                f"&timezone=auto"
            )
            async with session.get(weather_url) as resp:
                data = await resp.json()

            daily = data["daily"]
            temp_max = daily["temperature_2m_max"][0]
            temp_min = daily["temperature_2m_min"][0]
            rain_prob = daily["precipitation_probability_max"][0]

            umbrella = "☔ 降水概率高，出门带伞！" if rain_prob > 40 else "🌂 降水概率低，无需带伞。"
            feels_max = daily.get("apparent_temperature_max", [None])[0]
            # 以体感最高温为穿衣参考（更符合实际感受）；不可用时退回实际最高温
            ref_temp = feels_max if feels_max is not None else temp_max
            if ref_temp >= 32:
                clothes = "🩳 高温酷热，建议清凉短袖/短裤，注意防晒补水。"
            elif ref_temp >= 26:
                clothes = "👕 温热，短袖即可，外出注意防晒。"
            elif ref_temp >= 20:
                clothes = "🧢 温暖舒适，轻薄上衣即可，早晚可备薄外套。"
            elif ref_temp >= 14:
                clothes = "🧥 微凉，建议长袖或薄外套。"
            elif ref_temp >= 6:
                clothes = "🧣 较冷，建议加厚外套或毛衣。"
            else:
                clothes = "🥶 严寒，建议厚羽绒服，注意保暖防风。"

            feels_line = f"  （体感最高 {feels_max:.0f}℃）" if feels_max is not None else ""
            return (
                f"🌤️ 【{self.city}今日天气】\n"
                f"🌡️ 温度：{temp_min}℃ ~ {temp_max}℃{feels_line}\n"
                f"🌧️ 降水概率：{rain_prob}%\n"
                f"{umbrella}\n{clothes}"
            )
        except aiohttp.ClientError as exc:
            logger.warning(f"[资讯助理] 天气请求网络错误：{exc}")
            return f"🌤️ 【{self.city}天气】网络请求失败。"
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning(f"[资讯助理] 天气数据解析错误：{exc}")
            return f"🌤️ 【{self.city}天气】数据解析异常。"
        except asyncio.TimeoutError:
            return f"🌤️ 【{self.city}天气】请求超时。"

    # -----------------------------------------------------------------------
    # 2. 提醒模块
    # -----------------------------------------------------------------------

    async def _load_reminders(self) -> list[dict]:
        """
        从磁盘异步读取提醒列表，并对结构进行校验与清洗。
        确保返回值始终是 list[dict]，且每个元素包含合法的 date/content 字段，
        防止文件损坏或格式异常导致后续操作崩溃。
        """
        def _read():
            if not os.path.exists(self.reminders_file):
                return []
            with open(self.reminders_file, "r", encoding="utf-8") as f:
                return json.load(f)

        try:
            raw = await asyncio.to_thread(_read)
        except json.JSONDecodeError:
            logger.warning("[资讯助理] reminders.json JSON 解析失败，已返回空列表。")
            return []
        except OSError as exc:
            logger.warning(f"[资讯助理] reminders.json 读取 IO 错误：{exc}")
            return []

        # 结构校验：确保是列表，且每个元素是含 date/content 字符串字段的字典
        if not isinstance(raw, list):
            logger.warning(
                f"[资讯助理] reminders.json 根结构非列表（实为 {type(raw).__name__}），已重置。"
            )
            return []

        valid: list[dict] = []
        for item in raw:
            if (
                isinstance(item, dict)
                and isinstance(item.get("date"), str)
                and isinstance(item.get("content"), str)
            ):
                valid.append(item)
            else:
                logger.debug(f"[资讯助理] 跳过不合法的提醒条目：{item!r}")
        kept, dropped = self._purge_expired(valid)
        # 若有过期条目被清除，触发一次持锁原子回写，保持磁盘与内存同步。
        if dropped:
            asyncio.create_task(self._persist_purge(kept))
        return kept

    def _purge_expired(self, reminders: list[dict]) -> list[dict]:
        """
        过滤掉早于今天的过期提醒，防止文件无限膨胀。
        仅返回清理后的内存列表；将结果写回磁盘的职责由调用方（_load_reminders）
        在检测到有条目被删除时通过 _schedule_purge_write() 异步完成，
        保持本方法为纯同步、无 IO 副作用的函数。
        """
        tz = datetime.timezone(datetime.timedelta(hours=self.timezone_offset))
        today = datetime.datetime.now(tz).date()
        kept: list[dict] = []
        dropped = 0
        for r in reminders:
            try:
                item_date = datetime.date.fromisoformat(r["date"])
                if item_date >= today:
                    kept.append(r)
                else:
                    dropped += 1
            except (ValueError, KeyError):
                kept.append(r)  # 格式异常的条目保留，由上游校验处理
        if dropped:
            logger.debug(f"[资讯助理] 内存中清除 {dropped} 条过期提醒，将异步回写磁盘。")
        return kept, dropped  # 返回 dropped 数量供调用方决策是否回写

    async def _persist_purge(self, reminders: list[dict]) -> None:
        """将过期清理后的提醒列表回写磁盘（在 _file_lock 保护下），
        保证磁盘文件与内存状态一致，防止重启后过期数据复活。
        """
        async with self._file_lock:
            try:
                await self._atomic_write(reminders)
                logger.debug("[资讯助理] 过期提醒已回写磁盘。")
            except OSError as exc:
                logger.warning(f"[资讯助理] 过期提醒回写失败（非致命）：{exc}")

    async def _atomic_write(self, reminders: list[dict]) -> None:
        """
        底层原子写入：先写 .tmp 临时文件，再 os.replace() 替换目标文件。
        即使写入途中中断，也不会损坏已有的 reminders.json。
        调用方负责持有 _file_lock，此方法本身不加锁。
        """
        tmp_file = self.reminders_file + ".tmp"

        def _do_write():
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(reminders, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, self.reminders_file)

        await asyncio.to_thread(_do_write)

    async def _add_reminder(self, date_str: str, content: str) -> str:
        """
        将一条用户自定义提醒写入本地 reminders.json。

        此方法服务于 /添加提醒 手动指令，作为 AstrBot 系统 cron_jobs 之外的
        补充通道：用户可以用自然语言指令直接录入不需要定时触发的备忘事项。

        整个「读 → 改 → 写」事务在同一把 _file_lock 下串行执行，防止并发竞态。
        """
        try:
            parsed = datetime.datetime.strptime(date_str.strip(), "%Y-%m-%d")
            standard_date = parsed.strftime("%Y-%m-%d")
        except ValueError:
            return f"❌ 日期格式错误：收到 '{date_str}'，请使用 YYYY-MM-DD 格式。"

        async with self._file_lock:
            # 在锁内完成读→改→写。直接调用不加锁的 _atomic_write，
            # 防止 asyncio.Lock（非可重入）在同一协程中二次 acquire 死锁。
            reminders = await self._load_reminders()
            reminders.append({"date": standard_date, "content": content.strip()})
            reminders.sort(key=lambda r: r["date"])
            try:
                await self._atomic_write(reminders)
            except OSError as exc:
                logger.error(f"[资讯助理] 保存提醒失败：{exc}")
                return "❌ 系统错误：提醒保存失败，请重试。"

        return f"✅ 资讯助理待办已记录：\n📅 {standard_date}\n📝 {content.strip()}"

    def _find_db_path(self) -> str | None:
        """
        定位 AstrBot 主数据库的绝对路径，兼容多种部署方式。

        已知路径规律（基于 AstrBot 源码及日志分析）：
          - 源码/uv/pip 部署：工作目录即 AstrBot 根目录，数据库在 data/data_v3.db 或 data/data.db
          - Docker 部署：通常挂载到 /AstrBot/data/ 或 /app/data/
          - 桌面客户端：~/.astrbot/data/
        """
        home = os.path.expanduser("~")
        candidates = [
            # 桌面客户端 / Windows（实际确认路径）
            os.path.join(home, ".astrbot", "data", "data_v4.db"),
            os.path.join(home, ".astrbot", "data", "data_v3.db"),
            os.path.join(home, ".astrbot", "data", "data.db"),
            # 源码/uv/pip 部署（工作目录为 AstrBot 根）
            os.path.join("data", "data_v4.db"),
            os.path.join("data", "data_v3.db"),
            os.path.join("data", "data.db"),
            os.path.join("data", "astrbot.db"),
            # Docker 常见挂载路径
            os.path.join("/AstrBot", "data", "data_v4.db"),
            os.path.join("/AstrBot", "data", "data_v3.db"),
            os.path.join("/AstrBot", "data", "data.db"),
            os.path.join("/app", "data", "data_v4.db"),
            os.path.join("/app", "data", "data_v3.db"),
            os.path.join("/app", "data", "data.db"),
        ]
        for path in candidates:
            if os.path.exists(path):
                logger.debug(f"[资讯助理] 找到 AstrBot 数据库：{path}")
                return os.path.abspath(path)
        logger.debug(f"[资讯助理] 未找到 AstrBot 数据库，已搜索路径：{candidates}")
        return None


    async def _load_system_tasks(self) -> list[dict]:
        """
        从 AstrBot 主数据库（data_v4.db）的 cron_jobs 表读取系统级提醒，
        归一化为 {date, content} 格式后返回。

        已通过数据库文件确认的真实 Schema：
          表名      : cron_jobs
          content  : description 字段（与 payload.note 相同，始终有值）
          时间     : payload JSON 中的 run_at（ISO 8601 含时区）
                     若 payload 无 run_at，回退到 next_run_time 字段
          过滤条件 : status = 'scheduled' AND enabled = 1

        只读操作，不修改数据库，不干扰框架 cron 执行逻辑。
        """
        db_path = self._find_db_path()
        if not db_path:
            return []

        tz_offset = self.timezone_offset  # 闭包捕获，避免线程中访问 self

        def _query() -> list[dict]:
            results: list[dict] = []
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()

                # 只取待执行、已启用的任务
                cur.execute(
                    "SELECT name, description, next_run_time, payload "
                    "FROM cron_jobs "
                    "WHERE status = 'scheduled' AND enabled = 1"
                )
                rows = cur.fetchall()
                conn.close()

                tz = datetime.timezone(datetime.timedelta(hours=tz_offset))
                today = datetime.datetime.now(tz).date()

                for row in rows:
                    # 优先从 payload.run_at 获取含时区的时间，确保时区转换准确
                    run_at_str: str | None = None
                    try:
                        payload = json.loads(row["payload"] or "{}")
                        run_at_str = payload.get("run_at")
                    except (json.JSONDecodeError, TypeError):
                        payload = {}

                    # 回退到 next_run_time（无时区，视为插件配置时区）
                    if not run_at_str:
                        run_at_str = row["next_run_time"] or ""

                    if not run_at_str:
                        continue

                    try:
                        dt = datetime.datetime.fromisoformat(run_at_str)
                        # next_run_time 无时区信息时，附加配置时区
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=tz)
                        dt_local = dt.astimezone(tz)
                        item_date = dt_local.date()
                        if item_date < today:
                            continue  # 跳过已过期任务
                        date_str = item_date.strftime("%Y-%m-%d")
                    except (ValueError, TypeError):
                        continue

                    # description 即提醒全文，始终有值
                    content = (row["description"] or row["name"] or "").strip()
                    if content:
                        run_time = dt_local.strftime("%H:%M")
                        results.append({
                            "date": date_str,
                            "content": content,
                            "run_time": run_time,
                        })

            except sqlite3.OperationalError as exc:
                logger.debug(f"[资讯助理] 系统任务数据库只读访问：{exc}")
            except sqlite3.Error as exc:
                logger.warning(f"[资讯助理] 读取 cron_jobs 失败：{exc}")
            return results

        try:
            return await asyncio.to_thread(_query)
        except Exception as exc:
            logger.warning(f"[资讯助理] 系统任务查询异常：{exc}")
            return []

    @staticmethod
    def _format_reminder_local(raw: str) -> str:
        """
        纯规则本地格式化，不依赖任何 LLM，作为提醒摘要的终极兜底。
        只返回「标签」内容，不含时间——时间由装配层统一拼接。
        """
        import re

        # ── 1. 提取标签（兼容【】[]/「」三种括号）────────────────────────────
        tag = ""
        tag_match = re.search(r"[【\[「]([^】\]」]{1,25})[】\]」]", raw)
        if tag_match:
            inner = tag_match.group(1)
            inner = re.sub(
                r"(预警|提醒|通知|警告|警报|alert)", "", inner, flags=re.IGNORECASE
            ).strip()
            if inner:
                tag = "「" + inner + "」"

        # ── 2. 提取核心事项 ──────────────────────────────────────────────────
        core = raw
        # 删去所有标签括号内容（含「」）
        core = re.sub(r"[【\[「][^】\]」]{1,30}[】\]」]", "", core)
        # 删去括号内时间注释，如（3月20日周五）
        core = re.sub(r"[（(][^）)]{1,30}[）)]", "", core)
        # 删去时间表达式（含 HH:MM-HH:MM 时间范围）
        core = re.sub(
            r"(大后天|本周[一二三四五六七日天]|下周[一二三四五六七日天]|"
            r"今天|明天|后天|本周|下周|这周|今晚|今早|"
            r"上午|下午|晚上|早上|凌晨|"
            r"\d{1,2}:\d{2}(-\d{1,2}:\d{2})?|"
            r"\d{1,2}[点时]\d{0,2}(分)?|"
            r"\d{4}-\d{2}-\d{2}|"
            r"\d{1,2}月\d{1,2}[日号]|"
            r"周[一二三四五六七日天])",
            "", core
        )
        # 遇到句末语气词和标点截断（含"吗""呢""啊"等疑问/感叹语气词）
        core = re.split(r"[？！?!。吗呢啊]", core)[0]
        # 删去行末时间尾词和连字符残留
        core = re.sub(r"\s*(之前|以前|前|到|截止|-)\s*$", "", core.strip())
        # 删去开头的称谓和语气词
        core = re.sub(r"^[是的了到在和与兄弟，,、\s]+", "", core).strip()
        # 删去行末"写了""做了"等无实义动补结构
        core = re.sub(r"(写了|做了|完了|好了)\s*$", "", core.strip())
        # 清理多余标点和空白
        core = re.sub(r"[，。、；：,;.\-\s]+$", "", core.strip())
        core = re.sub(r"\s{2,}", " ", core).strip()

        if not core:
            fallback = re.sub(r"[【\[「][^】\]」]{1,30}[】\]」]", "", raw).strip()
            core = fallback[:30]
        else:
            core = core[:40]

        # ── 3. 拼装（只返回内容，不含时间）─────────────────────────────────
        return (tag + core) if tag else core

    async def _parse_reminder_text(self, raw: str, date_str: str, run_time: str = "") -> str:
        """
        将原始提醒文本提炼为一行简洁摘要。

        策略：
        - 优先调用 LLM（1次，不重试）获得高质量摘要
        - LLM 不可用或调用失败（包括 429/过载）→ 立即切换本地规则格式化
        - 本地规则基于正则，零延迟，确保提醒永远能正常展示

        之所以不重试：模型过载时反复重试只会阻塞情报推送，
        本地格式化已能提供可读的结果，没必要为等待 LLM 而延误整份情报。
        """
        # ── 尝试 LLM（仅一次，失败立刻降级）────────────────────────────────
        provider = None
        try:
            if self.reminder_provider:
                provider = self.context.get_provider_by_id(self.reminder_provider)
            if provider is None:
                provider = self.context.get_using_provider()
        except Exception:
            pass

        if provider is not None:
            prompt_lines = [
                "将以下提醒原文提炼为极简一行，只输出内容本身，不含时间。",
                "格式（严格遵守，无方括号则省略「」部分）：「课程/项目名」动宾短语",
                "规则：",
                "1.「」内填原文【】中的课程/项目名，删去「预警」「通知」「警报」等修饰词",
                "2. 动宾短语：5-10字，说明做什么事，删去称呼、感叹、提问、解释",
                "3. 不要输出任何时间信息（时间由系统另行处理）",
                "4. 只输出这一行，不加任何说明",
                "原文：" + raw,
            ]
            try:
                # 超时 20 秒；缓存机制保证大多数情况下不调用 LLM
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt="\n".join(prompt_lines),
                        session_id=None,
                        image_urls=[],
                        func_tool=None,
                    ),
                    timeout=20.0,
                )
                first_line = resp.completion_text.strip().split("\n")[0].strip()
                if first_line:
                    return first_line
            except asyncio.TimeoutError:
                logger.debug("[资讯助理] 提醒格式化 LLM 超时，使用本地规则")
            except Exception as exc:
                logger.debug(f"[资讯助理] 提醒格式化 LLM 失败，使用本地规则：{str(exc)[:80]}")

        # ── 本地规则兜底（零延迟，永不失败）────────────────────────────────
        return self._format_reminder_local(raw)

    async def format_reminders(self) -> str:
        """
        格式化今日及未来提醒，供 /今日情报 手动指令调用。
        委托给 _format_reminders_serial，行为与定时推送完全一致。
        """
        json_reminders, sys_tasks = await asyncio.gather(
            self._load_reminders(),
            self._load_system_tasks(),
        )
        return await self._format_reminders_serial(json_reminders, sys_tasks)


    # -----------------------------------------------------------------------
    # 3. 新闻模块
    # -----------------------------------------------------------------------

    async def fetch_60s_news_text(self, session: aiohttp.ClientSession) -> str:
        """获取 60s 纯文本新闻（多 URL 降级容错）。"""
        urls = [
            "https://60s.viki.moe/v2/60s",
            "https://60s-api.114128.xyz/v2/60s",
        ]
        for url in urls:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(f"[资讯助理] 新闻接口 {url} 返回 HTTP {resp.status}")
                        continue
                    data = await resp.json()
                    news_items: list[str] = data.get("data", {}).get("news", [])
                    if not news_items:
                        continue
                    body = "\n \n".join(
                        f"{i}. {item}" for i, item in enumerate(news_items, 1)
                    )
                    return f"📰 【每日60s纯文本速报】\n\n{body}"
            except aiohttp.ClientError as exc:
                logger.warning(f"[资讯助理] 新闻接口 {url} 网络错误：{exc}")
                continue
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning(f"[资讯助理] 新闻接口 {url} 数据解析错误：{exc}")
                continue
            except asyncio.TimeoutError:
                logger.warning(f"[资讯助理] 新闻接口 {url} 请求超时，尝试备用接口。")
                continue
        return "📰 【新闻速报】获取失败，接口波动。"

    # -----------------------------------------------------------------------
    # 4. 汇率模块
    # -----------------------------------------------------------------------

    async def fetch_exchange_rates(self, session: aiohttp.ClientSession) -> str:
        """获取实时汇率（以 base_currency 为基准）。"""
        if not self.exchange_api_key:
            return "📊 【汇率】⚠️ 未配置 API Key。"
        url = (
            f"https://v6.exchangerate-api.com/v6/{self.exchange_api_key}"
            f"/latest/{self.base_currency}"
        )
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"[资讯助理] 汇率 API 返回 HTTP {resp.status}，"
                        f"接口域名：exchangerate-api.com，货币：{self.base_currency}"
                    )
                    return "📊 【汇率】API 请求失败。"
                data = await resp.json()
            rates = data.get("conversion_rates", {})
            lines: list[str] = []
            for cur in self.target_currencies:
                rate = rates.get(cur)
                if rate and rate != 0:
                    # rate = 1 {base} 能换多少 {cur}
                    # 用户更关心的是"花多少本币换100外币"，即反向汇率
                    cost = 100 / rate  # 100 {cur} 需要多少 {base}
                    lines.append(
                        f"- {cur}：1 {self.base_currency} = {rate:.4f} {cur}"
                        f"  |  100 {cur} ≈ {cost:.2f} {self.base_currency}"
                    )
            if not lines:
                return "📊 【汇率】暂无有效汇率数据。"
            return f"📊 【实时汇率】\n" + "\n".join(lines)
        except aiohttp.ClientError as exc:
            logger.warning(f"[资讯助理] 汇率请求网络错误：{exc}")
            return "📊 【汇率】网络请求失败。"
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning(f"[资讯助理] 汇率数据解析错误：{exc}")
            return "📊 【汇率】数据解析异常。"
        except asyncio.TimeoutError:
            return "📊 【汇率】请求超时。"

    # -----------------------------------------------------------------------
    # 5. API 余额监控
    # -----------------------------------------------------------------------

    async def fetch_deepseek_balance(self, session: aiohttp.ClientSession) -> str:
        """查询 DeepSeek API 余额。"""
        if not self.deepseek_key:
            return "- DeepSeek: 未配置"
        try:
            headers = {"Authorization": f"Bearer {self.deepseek_key}"}
            async with session.get(
                "https://api.deepseek.com/user/balance", headers=headers
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[资讯助理] DeepSeek 余额 API 返回 HTTP {resp.status}")
                    return "- DeepSeek: 查询失败"
                data = await resp.json()
            infos: list[dict] = data.get("balance_infos", [])
            if infos:
                balances = " / ".join(
                    f"{i.get('total_balance')} {i.get('currency')}" for i in infos
                )
                return f"- DeepSeek: {balances}"
        except aiohttp.ClientError as exc:
            logger.warning(f"[资讯助理] DeepSeek 请求网络错误：{exc}")
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning(f"[资讯助理] DeepSeek 数据解析错误：{exc}")
        except asyncio.TimeoutError:
            logger.warning("[资讯助理] DeepSeek 余额查询超时")
        return "- DeepSeek: 查询异常"

    async def fetch_moonshot_balance(self, session: aiohttp.ClientSession) -> str:
        """查询 Moonshot (Kimi) API 余额。"""
        if not self.moonshot_key:
            return "- Kimi: 未配置"
        try:
            headers = {"Authorization": f"Bearer {self.moonshot_key}"}
            async with session.get(
                "https://api.moonshot.cn/v1/users/me/balance", headers=headers
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[资讯助理] Kimi 余额 API 返回 HTTP {resp.status}")
                    return "- Kimi: 查询失败"
                data = await resp.json()
            available = data.get("data", {}).get("available_balance", 0)
            return f"- Kimi: ￥{available:.2f}"
        except aiohttp.ClientError as exc:
            logger.warning(f"[资讯助理] Kimi 请求网络错误：{exc}")
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning(f"[资讯助理] Kimi 数据解析错误：{exc}")
        except asyncio.TimeoutError:
            logger.warning("[资讯助理] Kimi 余额查询超时")
        return "- Kimi: 查询异常"

    # -----------------------------------------------------------------------
    # 核心情报组装
    # -----------------------------------------------------------------------

    async def build_news_text(self) -> str:
        """
        组装完整情报文本，保证只输出一条消息。

        执行顺序（解决 LLM 限流问题的关键）：
        1. HTTP 请求（天气/汇率/余额/新闻）与提醒数据加载并发——互不调用 LLM。
        2. HTTP 全部返回后 API 压力最低，再串行逐条 LLM 格式化提醒（每条间隔 2 秒）。
        3. 所有内容就绪后一次性组装，返回单条完整文本。
        """
        logger.info("[资讯助理] 开始并发拉取情报...")

        async def _skip() -> str:
            return ""

        # ── 步骤1：HTTP 请求与提醒加载并发（均不调用 LLM）──────────────────
        # 注意：HTTP 请求必须在 session 上下文内 await 完毕；
        # 提醒加载（读本地文件/SQLite）不依赖 session，可以同步并发。
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        reminder_task = asyncio.gather(
            self._load_reminders(),
            self._load_system_tasks(),
        ) if self.enable_reminders else _skip()

        async with aiohttp.ClientSession(timeout=timeout) as session:
            http_results, reminder_raw = await asyncio.gather(
                asyncio.gather(
                    self.fetch_weather(session)          if self.enable_weather  else _skip(),
                    self.fetch_exchange_rates(session)   if self.enable_exchange else _skip(),
                    self.fetch_deepseek_balance(session) if self.enable_balance  else _skip(),
                    self.fetch_moonshot_balance(session) if self.enable_balance  else _skip(),
                    self.fetch_60s_news_text(session)    if self.enable_news     else _skip(),
                    return_exceptions=True,
                ),
                reminder_task,
                return_exceptions=False,
            )

        def _safe(result, fallback: str) -> str:
            return result if not isinstance(result, Exception) else fallback

        weather_text  = _safe(http_results[0], "🌤️ 【天气】获取超时")
        exchange_text = _safe(http_results[1], "📊 【汇率】获取超时")
        ds_balance    = _safe(http_results[2], "- DeepSeek: 超时")
        ms_balance    = _safe(http_results[3], "- Kimi: 超时")
        news_text     = _safe(http_results[4], "📰 【新闻】获取超时")

        # ── 步骤2：串行 LLM 格式化提醒（HTTP 已全部返回，API 此时最空闲）──
        reminders_text = ""
        if self.enable_reminders and isinstance(reminder_raw, (list, tuple)):
            json_reminders, sys_tasks = reminder_raw
            reminders_text = await self._format_reminders_serial(json_reminders, sys_tasks)

        # ── 步骤3：按配置顺序一次性组装 ─────────────────────────────────────
        balance_block = f"💰 【API 资产监控】\n{ds_balance}\n{ms_balance}"
        module_map: dict[str, str] = {
            "weather":   weather_text   if self.enable_weather   else "",
            "reminders": reminders_text if self.enable_reminders else "",
            "exchange":  exchange_text  if self.enable_exchange  else "",
            "balance":   balance_block  if self.enable_balance   else "",
            "news":      news_text      if self.enable_news      else "",
        }
        blocks: list[str] = [
            module_map[m] for m in self.module_order
            if m in module_map and module_map[m]
        ]

        if not blocks:
            return "📭 资讯助理：所有情报模块已在后台关闭。"
        return _DIVIDER.join(blocks)

    async def _format_reminders_serial(
        self,
        json_reminders: list[dict],
        sys_tasks: list[dict],
    ) -> str:
        """
        合并两路提醒数据，带缓存地格式化每条提醒摘要。

        缓存机制（解决 LLM 限流的根本方案）：
        - 每条提醒以 SHA1(content+run_time) 为缓存键，持久化存储在 reminder_cache.json
        - 缓存命中 → 直接使用，零 LLM 调用
        - 缓存未命中（新提醒）→ 调用 LLM 一次，成功或失败均写入缓存
        - 结果：第一次遇到新提醒时调用一次 LLM，之后每次推送对该条目零调用
        - 新旧提醒条目的缓存自然随提醒本身过期而不再被读取（无需主动清理）
        """
        await self._load_cache()

        seen: set[tuple[str, str]] = set()
        reminders: list[dict] = []
        for r in json_reminders + sys_tasks:
            key = (r.get("date", ""), r.get("content", ""))
            if key not in seen:
                seen.add(key)
                reminders.append(r)

        tz = datetime.timezone(datetime.timedelta(hours=self.timezone_offset))
        now = datetime.datetime.now(tz)
        today_str = now.strftime("%Y-%m-%d")
        this_week = {
            (now + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(1, self.reminder_lookback_days + 1)
        }

        today_items: list[dict] = []
        week_items:  list[dict] = []
        for r in reminders:
            try:
                d = datetime.datetime.strptime(r["date"], "%Y-%m-%d").strftime("%Y-%m-%d")
            except (ValueError, KeyError, TypeError):
                continue
            if d == today_str:
                today_items.append(r)
            elif d in this_week:
                week_items.append(r)

        if not today_items and not week_items:
            return "📝 【提醒事项】近期无安排，享受生活吧！"

        def _relative_label(item_date_str: str) -> str:
            try:
                item_date = datetime.date.fromisoformat(item_date_str)
            except ValueError:
                return item_date_str[5:]
            delta = (item_date - now.date()).days
            mm_dd = item_date.strftime("%m-%d")
            wday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            if delta == 1:
                rel = "明天"
            elif delta == 2:
                rel = "后天"
            elif delta <= 6:
                rel = wday[item_date.weekday()]
            else:
                rel = "下" + wday[item_date.weekday()]
            return mm_dd + "（" + rel + "）"

        def _strip_trailing_time(text: str) -> str:
            """删除 LLM 可能在末尾多输出的时间（如 19:00 / 08:00-11:00）。"""
            import re as _re
            return _re.sub(
                r"\s+\d{1,2}:\d{2}(-\d{1,2}:\d{2})?\s*$", "", text
            ).strip()

        all_items = (
            [(r, today_str, True) for r in today_items] +
            [(r, r.get("date", today_str), False) for r in week_items]
        )
        fmt_today:  list[tuple[str, str]] = []         # (run_time, text)
        fmt_future: list[tuple[str, str, str]] = []    # (label, run_time, text)
        cache_dirty = False

        for idx, (r, date_ctx, is_today) in enumerate(all_items):
            content_raw = r.get("content", "")
            run_time    = r.get("run_time", "")
            cache_k     = self._cache_key(content_raw, run_time)

            if cache_k in self._reminder_cache:
                # 缓存命中：直接使用，不调用 LLM
                text = self._reminder_cache[cache_k]
                logger.debug(f"[资讯助理] 提醒缓存命中：{cache_k}")
            else:
                # 缓存未命中：调用 LLM（只有新提醒才会走到这里）
                if idx > 0:
                    await asyncio.sleep(2)  # 仅在需要 LLM 时才等待
                text = await self._parse_reminder_text(content_raw, date_ctx, run_time)
                self._reminder_cache[cache_k] = text
                cache_dirty = True
                logger.debug(f"[资讯助理] 提醒新增缓存：{cache_k}")

            if is_today:
                fmt_today.append((run_time, text))
            else:
                fmt_future.append((_relative_label(r.get("date", "")), run_time, text))

        # 有新缓存条目时异步写盘，不阻塞返回
        if cache_dirty:
            asyncio.create_task(self._save_cache())

        parts: list[str] = []
        if fmt_today:
            fmt_today.sort(key=lambda x: x[0] if x[0] else "99:99")
            lines = []
            for rt, t in fmt_today:
                t_clean = _strip_trailing_time(t)
                lines.append((rt + " " + t_clean) if rt else t_clean)
            parts.append("📝 【今日待办】\n" + "\n".join("🔔 " + t for t in lines))
        if fmt_future:
            lines = []
            for lbl, rt, t in fmt_future:
                t_clean = _strip_trailing_time(t)
                time_part = rt + "：" if rt else "："
                lines.append(lbl + " " + time_part + t_clean)
            parts.append("📅 【未来提醒】\n" + "\n".join("📌 " + t for t in lines))
        return "\n\n".join(parts)

    # -----------------------------------------------------------------------
    # 定时推送循环（asyncio 原生实现，无需第三方调度器）
    # -----------------------------------------------------------------------

    async def _push_loop(self) -> None:
        """
        持续运行的定时推送循环。
        每次计算距下次推送时间的剩余秒数并 sleep，避免漂移。
        """
        hour, minute = self._parse_push_time(self.push_time)
        logger.info(f"[资讯助理] 推送循环启动，目标时间 {hour:02d}:{minute:02d}。")

        while True:
            seconds = self._seconds_until(hour, minute)
            logger.debug(f"[资讯助理] 距下次推送还有 {seconds:.0f} 秒。")
            await asyncio.sleep(seconds)
            try:
                await self._broadcast()
            except Exception:
                logger.error(f"[资讯助理] 广播异常：{traceback.format_exc()}")

    @staticmethod
    def _parse_push_time(push_time: str) -> tuple[int, int]:
        """将 'HH:MM' 字符串解析为 (hour, minute)，格式错误时返回默认值 (8, 0)。"""
        try:
            parts = push_time.strip().split(":")
            h, m = int(parts[0]), int(parts[1])
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h, m
        except (ValueError, IndexError, AttributeError):
            pass
        logger.warning(f"[资讯助理] 推送时间 '{push_time}' 格式无效，已回退至 08:00。")
        return 8, 0

    def _seconds_until(self, hour: int, minute: int) -> float:
        """计算从当前时刻到指定时刻的剩余秒数（始终为正，跨越午夜时 +24h）。"""
        tz = datetime.timezone(datetime.timedelta(hours=self.timezone_offset))
        now = datetime.datetime.now(tz)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        return (target - now).total_seconds()

    async def _broadcast(self) -> None:
        """向所有目标群组推送今日情报（单条消息）。"""
        if not self.target_groups:
            logger.warning("[资讯助理] 定时推送已触发，但未配置 target_groups，跳过。")
            return
        final_text = await self.build_news_text()
        chain = MessageChain([Plain(final_text)])
        for target in self.target_groups:
            try:
                await self.context.send_message(target, chain)
                logger.info(f"[资讯助理] 早报已送达：{target}")
            except Exception as exc:
                logger.error(f"[资讯助理] 送达 '{target}' 失败：{exc}")
            await asyncio.sleep(1)

    # -----------------------------------------------------------------------
    # 指令处理器
    # -----------------------------------------------------------------------

    @filter.command("今日情报")
    async def manual_trigger(self, event: AstrMessageEvent):
        """手动触发一次完整情报推送。"""
        yield event.plain_result("🚀 资讯助理正在拉取情报（约 3-5 秒），请稍候...")
        text = await self.build_news_text()
        yield event.plain_result(text)

    @filter.command("提醒诊断")
    async def diagnose_reminders(self, event: AstrMessageEvent):
        """
        诊断指令：显示数据库路径、cron_jobs 任务列表及本地提醒状态。
        用法：/提醒诊断
        """
        lines: list[str] = ["🔍 【资讯助理提醒诊断 v2】"]

        # 1. 数据库路径
        db_path = self._find_db_path()
        if db_path:
            lines.append(f"\n✅ 数据库：{db_path}")
        else:
            lines.append("\n❌ 未找到数据库，请告知你的部署方式（源码/Docker/桌面版）")

        # 2. 直接查询 cron_jobs 表内容（修复：之前误标无关表，漏显 cron_jobs）
        if db_path:
            def _inspect():
                result = []
                try:
                    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
                    conn.row_factory = sqlite3.Row
                    cur = conn.cursor()

                    cur.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='cron_jobs'"
                    )
                    if not cur.fetchone():
                        result.append("\n⚠️ cron_jobs 表不存在，数据库版本可能不同")
                        conn.close()
                        return result

                    cur.execute(
                        "SELECT name, description, next_run_time, payload, status, enabled "
                        "FROM cron_jobs ORDER BY next_run_time"
                    )
                    rows = cur.fetchall()
                    conn.close()

                    result.append(f"\n📋 cron_jobs 表共 {len(rows)} 条任务：")
                    if not rows:
                        result.append("  （表为空，换平台前的提醒已不存在）")
                    for row in rows:
                        run_at = ""
                        try:
                            payload = json.loads(row["payload"] or "{}")
                            run_at = payload.get("run_at", "") or row["next_run_time"] or ""
                        except Exception:
                            run_at = row["next_run_time"] or ""
                        status_str = f"[{row['status']}|{'启用' if row['enabled'] else '禁用'}]"
                        name_str = (row["name"] or "")[:30]
                        desc_str = (row["description"] or "")[:50]
                        result.append(
                            f"  {status_str} {str(run_at)[:16]}  {name_str}"
                            + (f"\n    {desc_str}" if desc_str else "")
                        )
                except Exception as exc:
                    result.append(f"\n❌ cron_jobs 查询失败：{exc}")
                return result
            inspection = await asyncio.to_thread(_inspect)
            lines.extend(inspection)

        # 3. 本地 JSON 提醒
        json_reminders = await self._load_reminders()
        lines.append(f"\n📄 reminders.json：{len(json_reminders)} 条")
        for r in json_reminders[:5]:
            lines.append(f"  [{r.get('date')}] {r.get('content', '')[:50]}")

        # 4. 时区
        tz = datetime.timezone(datetime.timedelta(hours=self.timezone_offset))
        now = datetime.datetime.now(tz)
        lines.append(f"\n🕐 时区 UTC+{self.timezone_offset}，当前：{now.strftime('%Y-%m-%d %H:%M')}")

        yield event.plain_result("\n".join(lines))

    @filter.command("添加提醒")
    async def add_reminder_cmd(self, event: AstrMessageEvent):
        """
        手动添加提醒指令。
        用法：/添加提醒 YYYY-MM-DD 事项内容
        示例：/添加提醒 2026-04-01 记得交作业
        """
        raw: str = event.message_str.strip()
        # 去掉命令前缀后的剩余部分
        body = raw.removeprefix("/添加提醒").removeprefix("添加提醒").strip()
        parts = body.split(None, 1)  # 按空白字符分割，最多分成 2 份
        if len(parts) < 2:
            yield event.plain_result(
                "❌ 格式错误。\n用法：/添加提醒 YYYY-MM-DD 事项内容\n"
                "示例：/添加提醒 2026-04-01 记得交作业"
            )
            return
        date_str, content = parts[0], parts[1]
        result = await self._add_reminder(date_str, content)
        yield event.plain_result(result)

    # -----------------------------------------------------------------------
    # 生命周期
    # -----------------------------------------------------------------------

    async def terminate(self) -> None:
        """插件卸载/重载时，取消后台推送任务。"""
        if self._push_task and not self._push_task.done():
            self._push_task.cancel()
            try:
                await self._push_task
            except asyncio.CancelledError:
                pass
        logger.info("[资讯助理] 插件已安全停止。")
