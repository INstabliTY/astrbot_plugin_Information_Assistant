"""
AstrBot 资讯助理插件 (Information Assistant)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
聚合天气、提醒、纯文本新闻、汇率与 API 余额监控。

Author : INstabliTY
Version: v1.4.0
"""

import asyncio
import datetime
import hashlib
import json
import os
import re
import sqlite3
import traceback

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
_DIVIDER = "\n\n---------------------------\n\n"
# 缓存前缀：标识本次摘要由本地规则生成（非 LLM），下次推送应重试 LLM
_LOCAL_PREFIX = "local:"
# 衣着建议阈值（体感最高温，℃）
_CLOTHES_THRESHOLDS = [
    (32, "🩳 高温酷热，建议清凉短袖/短裤，注意防晒补水。"),
    (26, "👕 温热，短袖即可，外出注意防晒。"),
    (20, "🧢 温暖舒适，轻薄上衣即可，早晚可备薄外套。"),
    (14, "🧥 微凉，建议长袖或薄外套。"),
    ( 6, "🧣 较冷，建议加厚外套或毛衣。"),
]
_CLOTHES_DEFAULT = "🥶 严寒，建议厚羽绒服，注意保暖防风。"
# 相对日期标签
_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ---------------------------------------------------------------------------
# 插件主类
# ---------------------------------------------------------------------------

@register(
    "astrbot_plugin_Information_Assistant",
    "资讯助理",
    "聚合天气、提醒、纯文本新闻与汇率",
    "1.5.0",
)
class InformationAssistantPlugin(Star):
    """资讯助理插件主类。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self._parse_config(config or {})

        data_dir = StarTools.get_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.reminders_file    = str(data_dir / "reminders.json")
        self.reminder_cache_file = str(data_dir / "reminder_cache.json")
        self._ensure_reminders_file()

        self._file_lock: asyncio.Lock = asyncio.Lock()
        # 提醒摘要内存缓存，键为 SHA1(content|run_time)[:16]
        self._reminder_cache: dict[str, str] = {}
        self._cache_loaded = False

        # 定时推送任务（框架保证 __init__ 在事件循环运行后被调用）
        self._push_task: asyncio.Task | None = None
        if self.enable_push:
            self._push_task = asyncio.create_task(self._push_loop())
            logger.info(f"[资讯助理] 定时任务已启动，每天 {self.push_time} 推送。")
        else:
            logger.info("[资讯助理] 定时推送已关闭，以纯被动模式运行。")

    # -----------------------------------------------------------------------
    # 配置解析
    # -----------------------------------------------------------------------

    def _parse_config(self, cfg: dict) -> None:
        """
        解析配置字典，兼容嵌套卡片结构（{section: {key: val}}）
        与旧版扁平结构（{key: val}）。
        """
        def _get(section: str, key: str, default):
            sec = cfg.get(section)
            if isinstance(sec, dict) and key in sec:
                return sec[key]
            return cfg.get(key, default)

        def _int(section: str, key: str, default: int, lo: int, hi: int) -> int:
            try:
                return max(lo, min(hi, int(_get(section, key, default) or default)))
            except (ValueError, TypeError):
                return default

        def _parse_str_list(raw) -> list[str]:
            if isinstance(raw, list):
                return [str(g).strip() for g in raw if g and str(g).strip()]
            if isinstance(raw, str) and raw.strip():
                return [g.strip() for g in raw.split(",") if g.strip()]
            return []

        # 推送
        self.enable_push   : bool      = _get("push_settings", "enable_push", True)
        self.push_time     : str       = _get("push_settings", "push_time", "08:00")
        self.target_groups : list[str] = _parse_str_list(_get("push_settings", "target_groups", []))
        self.timezone_offset: float    = self._parse_tz_offset(_get("push_settings", "timezone_offset", "10"))

        # 天气
        self.enable_weather: bool = _get("weather_settings", "enable_weather", True)
        self.city          : str  = _get("weather_settings", "city", "Brisbane")

        # 提醒
        self.enable_reminders    : bool = _get("reminder_settings", "enable_reminders", True)
        self.reminder_provider   : str  = _get("reminder_settings", "reminder_provider", "")
        self.reminder_lookback_days: int = _int("reminder_settings", "reminder_lookback_days", 7, 1, 30)

        # 汇率
        self.enable_exchange  : bool      = _get("exchange_settings", "enable_exchange", True)
        self.exchange_api_key : str       = _get("exchange_settings", "exchange_api_key", "")
        self.base_currency    : str       = _get("exchange_settings", "base_currency", "CNY").upper()
        self.target_currencies: list[str] = _parse_str_list(
            _get("exchange_settings", "target_currencies", "USD,JPY,EUR,GBP,HKD,AUD")
        ) or [c.upper() for c in ["USD", "JPY", "EUR", "GBP", "HKD", "AUD"]]

        # 余额监控
        self.enable_balance: bool = _get("balance_settings", "enable_balance", True)
        self.deepseek_key  : str  = _get("balance_settings", "deepseek_key", "")
        self.moonshot_key  : str  = _get("balance_settings", "moonshot_key", "")

        # 新闻
        self.enable_news: bool = _get("news_settings", "enable_news", True)

        # 高级
        self.request_timeout: int = _int("advanced_settings", "request_timeout", 20, 5, 120)
        raw_order = _get("advanced_settings", "module_order", "weather,reminders,exchange,balance,news")
        self.module_order: list[str] = (
            [m.strip() for m in str(raw_order).split(",") if m.strip()]
            or ["weather", "reminders", "exchange", "balance", "news"]
        )

    @staticmethod
    def _parse_tz_offset(raw) -> float:
        try:
            return max(-12.0, min(14.0, float(str(raw).strip())))
        except (ValueError, TypeError):
            logger.warning(f"[资讯助理] 时区配置 '{raw}' 无效，已回退至 UTC+10。")
            return 10.0

    # -----------------------------------------------------------------------
    # 工具方法
    # -----------------------------------------------------------------------

    def _ensure_reminders_file(self) -> None:
        if not os.path.exists(self.reminders_file):
            with open(self.reminders_file, "w", encoding="utf-8") as f:
                json.dump([], f)

    def _tz(self) -> datetime.timezone:
        return datetime.timezone(datetime.timedelta(hours=self.timezone_offset))

    def _now(self) -> datetime.datetime:
        return datetime.datetime.now(self._tz())

    @staticmethod
    def _cache_key(content: str, run_time: str) -> str:
        """SHA1(content|run_time) 前16位作为缓存键。"""
        return hashlib.sha1(f"{content}|{run_time}".encode()).hexdigest()[:16]

    @staticmethod
    def _strip_trailing_time(text: str) -> str:
        """删除 LLM 可能在末尾多输出的时间（如 19:00 或 08:00-11:00）。"""
        return re.sub(r"\s+\d{1,2}:\d{2}(-\d{1,2}:\d{2})?\s*$", "", text).strip()

    @staticmethod
    def _relative_label(item_date: datetime.date, today: datetime.date) -> str:
        """将日期转为 MM-DD（明天/后天/周几/下周几）格式。"""
        delta = (item_date - today).days
        mm_dd = item_date.strftime("%m-%d")
        if delta == 1:
            rel = "明天"
        elif delta == 2:
            rel = "后天"
        elif delta <= 6:
            rel = _WEEKDAY_CN[item_date.weekday()]
        else:
            rel = "下" + _WEEKDAY_CN[item_date.weekday()]
        return f"{mm_dd}（{rel}）"

    # -----------------------------------------------------------------------
    # 缓存管理（提醒摘要持久化）
    # -----------------------------------------------------------------------

    async def _load_cache(self) -> None:
        """从磁盘懒加载缓存到内存（首次调用时执行）。"""
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
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"[资讯助理] 缓存加载失败，从空缓存启动：{exc}")
            self._reminder_cache = {}
        self._cache_loaded = True

    async def _save_cache(self, active_keys: set[str]) -> None:
        """
        原子写入缓存到磁盘，同时清理不再活跃的条目，防止文件无限增长。
        active_keys：本次推送实际用到的缓存键集合。
        """
        # 只保留本次推送涉及的键（活跃提醒的缓存），其余自动丢弃
        pruned = {k: v for k, v in self._reminder_cache.items() if k in active_keys}
        self._reminder_cache = pruned

        snapshot = dict(pruned)
        tmp = self.reminder_cache_file + ".tmp"
        def _write():
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.reminder_cache_file)
        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            logger.warning(f"[资讯助理] 缓存写入失败（非致命）：{exc}")

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
            lat, lon = results[0]["latitude"], results[0]["longitude"]

            weather_url = (
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}"
                f"&daily=temperature_2m_max,temperature_2m_min"
                f",precipitation_probability_max,apparent_temperature_max"
                f"&timezone=auto"
            )
            async with session.get(weather_url) as resp:
                data = await resp.json()

            daily     = data["daily"]
            temp_max  = daily["temperature_2m_max"][0]
            temp_min  = daily["temperature_2m_min"][0]
            rain_prob = daily["precipitation_probability_max"][0]
            feels_max = daily.get("apparent_temperature_max", [None])[0]
            ref_temp  = feels_max if feels_max is not None else temp_max

            umbrella = "☔ 降水概率高，出门带伞！" if rain_prob > 40 else "🌂 降水概率低，无需带伞。"
            clothes  = next((msg for threshold, msg in _CLOTHES_THRESHOLDS if ref_temp >= threshold), _CLOTHES_DEFAULT)
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
        """从 reminders.json 读取、校验并清除过期条目。"""
        def _read():
            if not os.path.exists(self.reminders_file):
                return []
            with open(self.reminders_file, "r", encoding="utf-8") as f:
                return json.load(f)

        try:
            raw = await asyncio.to_thread(_read)
        except json.JSONDecodeError:
            logger.warning("[资讯助理] reminders.json 解析失败，已返回空列表。")
            return []
        except OSError as exc:
            logger.warning(f"[资讯助理] reminders.json 读取失败：{exc}")
            return []

        if not isinstance(raw, list):
            logger.warning(f"[资讯助理] reminders.json 格式错误（{type(raw).__name__}），已重置。")
            return []

        valid = [
            item for item in raw
            if isinstance(item, dict)
            and isinstance(item.get("date"), str)
            and isinstance(item.get("content"), str)
        ]

        today = self._now().date()
        kept, dropped = [], 0
        for r in valid:
            try:
                if datetime.date.fromisoformat(r["date"]) >= today:
                    kept.append(r)
                else:
                    dropped += 1
            except (ValueError, KeyError):
                kept.append(r)

        if dropped:
            logger.debug(f"[资讯助理] 清除 {dropped} 条过期本地提醒，回写磁盘。")
            asyncio.create_task(self._write_reminders(kept))
        return kept

    async def _write_reminders(self, reminders: list[dict]) -> None:
        """在文件锁保护下原子写入 reminders.json。"""
        async with self._file_lock:
            tmp = self.reminders_file + ".tmp"
            def _do():
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(reminders, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.reminders_file)
            try:
                await asyncio.to_thread(_do)
            except OSError as exc:
                logger.error(f"[资讯助理] 写入 reminders.json 失败：{exc}")

    async def _add_reminder(self, date_str: str, content: str) -> str:
        """将一条本地提醒写入 reminders.json（读→改→写事务在锁内串行）。"""
        try:
            standard_date = datetime.datetime.strptime(date_str.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return f"❌ 日期格式错误：'{date_str}'，请使用 YYYY-MM-DD 格式。"

        async with self._file_lock:
            reminders = await self._load_reminders()
            reminders.append({"date": standard_date, "content": content.strip()})
            reminders.sort(key=lambda r: r["date"])
            tmp = self.reminders_file + ".tmp"
            def _do():
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(reminders, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.reminders_file)
            try:
                await asyncio.to_thread(_do)
            except OSError as exc:
                logger.error(f"[资讯助理] 保存提醒失败：{exc}")
                return "❌ 系统错误：提醒保存失败，请重试。"

        return f"✅ 已记录：\n📅 {standard_date}\n📝 {content.strip()}"

    def _find_db_path(self) -> str | None:
        """定位 AstrBot 主数据库路径，兼容桌面/源码/Docker 多种部署。"""
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".astrbot", "data", "data_v4.db"),
            os.path.join(home, ".astrbot", "data", "data_v3.db"),
            os.path.join(home, ".astrbot", "data", "data.db"),
            os.path.join("data", "data_v4.db"),
            os.path.join("data", "data_v3.db"),
            os.path.join("data", "data.db"),
            os.path.join("data", "astrbot.db"),
            os.path.join("/AstrBot", "data", "data_v4.db"),
            os.path.join("/AstrBot", "data", "data.db"),
            os.path.join("/app",     "data", "data_v4.db"),
            os.path.join("/app",     "data", "data.db"),
        ]
        for path in candidates:
            if os.path.exists(path):
                logger.debug(f"[资讯助理] 找到 AstrBot 数据库：{path}")
                return os.path.abspath(path)
        logger.debug("[资讯助理] 未找到 AstrBot 数据库。")
        return None

    async def _load_system_tasks(self) -> list[dict]:
        """
        只读查询 cron_jobs 表中 status='scheduled' & enabled=1 的任务，
        归一化为 {date, content, run_time} 格式。
        """
        db_path = self._find_db_path()
        if not db_path:
            return []

        tz_offset = self.timezone_offset

        def _query() -> list[dict]:
            results: list[dict] = []
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(
                    "SELECT name, description, next_run_time, payload "
                    "FROM cron_jobs WHERE status = 'scheduled' AND enabled = 1"
                )
                rows = cur.fetchall()
                conn.close()

                tz    = datetime.timezone(datetime.timedelta(hours=tz_offset))
                today = datetime.datetime.now(tz).date()

                for row in rows:
                    # 优先从 payload.run_at 取含时区时间
                    run_at_str = None
                    try:
                        run_at_str = json.loads(row["payload"] or "{}").get("run_at")
                    except (json.JSONDecodeError, TypeError):
                        pass
                    run_at_str = run_at_str or row["next_run_time"] or ""
                    if not run_at_str:
                        continue

                    try:
                        dt = datetime.datetime.fromisoformat(run_at_str)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=tz)
                        dt_local  = dt.astimezone(tz)
                        item_date = dt_local.date()
                        if item_date < today:
                            continue
                    except (ValueError, TypeError):
                        continue

                    content = (row["description"] or row["name"] or "").strip()
                    if content:
                        results.append({
                            "date":     item_date.strftime("%Y-%m-%d"),
                            "content":  content,
                            "run_time": dt_local.strftime("%H:%M"),
                        })
            except sqlite3.OperationalError as exc:
                logger.debug(f"[资讯助理] cron_jobs 只读访问受限：{exc}")
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
        纯正则本地格式化，LLM 不可用时的终极兜底。
        只返回「标签」内容，不含时间（时间由装配层拼接）。
        """
        # 1. 提取标签（兼容【】[] 「」）
        tag = ""
        m = re.search(r"[【\[「]([^】\]」]{1,25})[】\]」]", raw)
        if m:
            inner = re.sub(r"(预警|提醒|通知|警告|警报|alert)", "", m.group(1), flags=re.IGNORECASE).strip()
            if inner:
                tag = f"「{inner}」"

        # 2. 提取核心事项
        core = raw
        core = re.sub(r"[【\[「][^】\]」]{1,30}[】\]」]", "", core)          # 删标签
        core = re.sub(r"[（(][^）)]{1,30}[）)]", "", core)                    # 删括号注释
        core = re.sub(                                                          # 删时间词
            r"(大后天|本周[一二三四五六七日天]|下周[一二三四五六七日天]|"
            r"今天|明天|后天|本周|下周|这周|今晚|今早|"
            r"上午|下午|晚上|早上|凌晨|"
            r"\d{1,2}:\d{2}(-\d{1,2}:\d{2})?|\d{1,2}[点时]\d{0,2}(分)?|"
            r"\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}[日号]|周[一二三四五六七日天])",
            "", core
        )
        core = re.split(r"[？！?!。吗呢啊]", core)[0]                          # 截断语气词
        core = re.sub(r"\s*(之前|以前|前|到|截止|-)\s*$", "", core.strip())    # 删尾词
        core = re.sub(r"^[是的了到在和与兄弟，,、\s]+", "", core).strip()      # 删开头虚词
        core = re.sub(r"(写了|做了|完了|好了)\s*$", "", core.strip())          # 删无实义尾
        core = re.sub(r"[，。、；：,;.\-\s]+$", "", core.strip())              # 删尾标点
        core = re.sub(r"\s{2,}", " ", core).strip()

        if not core:
            core = re.sub(r"[【\[「][^】\]」]{1,30}[】\]」]", "", raw).strip()[:30]
        else:
            core = core[:40]

        return tag + core if tag else core

    async def _llm_format_reminder(self, raw: str) -> str | None:
        """
        调用 LLM 格式化一条提醒，15 秒超时。
        成功返回摘要字符串，失败返回 None。
        """
        provider = None
        try:
            if self.reminder_provider:
                provider = self.context.get_provider_by_id(self.reminder_provider)
            if provider is None:
                provider = self.context.get_using_provider()
        except Exception:
            pass
        if provider is None:
            return None

        prompt = "\n".join([
            "将以下提醒原文提炼为极简一行，只输出内容本身，不含时间。",
            "格式（无方括号则省略「」）：「课程/项目名」动宾短语",
            "规则：",
            "1. 「」内填原文【】中的课程/项目名，删去「预警」「警报」等修饰词",
            "2. 动宾短语 5-10 字，说明做什么事，删去称呼、感叹、提问",
            "3. 不输出任何时间信息",
            "4. 只输出这一行",
            f"原文：{raw}",
        ])
        try:
            # create_task + shield 确保 wait_for 超时能真正中断调用
            task = asyncio.create_task(
                provider.text_chat(prompt=prompt, session_id=None, image_urls=[], func_tool=None)
            )
            resp = await asyncio.wait_for(asyncio.shield(task), timeout=15.0)
            result = resp.completion_text.strip().split("\n")[0].strip()
            return result if result else None
        except asyncio.TimeoutError:
            logger.debug("[资讯助理] LLM 格式化超时（>15s）")
        except asyncio.CancelledError:
            logger.debug("[资讯助理] LLM 格式化任务被取消")
        except Exception as exc:
            logger.debug(f"[资讯助理] LLM 格式化失败：{str(exc)[:80]}")
        return None

    async def _get_reminder_summary(
        self, content: str, run_time: str, date_ctx: str, is_first: bool
    ) -> tuple[str, bool]:
        """
        获取一条提醒的格式化摘要，优先读缓存。
        返回 (摘要文本, 缓存是否变脏)。
        """
        key = self._cache_key(content, run_time)
        cached = self._reminder_cache.get(key)

        # 永久缓存命中（LLM 成功结果）
        if cached is not None and not cached.startswith(_LOCAL_PREFIX):
            logger.debug(f"[资讯助理] 缓存命中：{key}")
            return cached, False

        # 临时缓存（上次 LLM 失败）或未缓存 → 重试 LLM
        if not is_first:
            await asyncio.sleep(2)

        llm_result = await self._llm_format_reminder(content)
        local_result = self._format_reminder_local(content)

        if llm_result and llm_result.strip() != local_result.strip():
            # LLM 成功：永久缓存
            self._reminder_cache[key] = llm_result
            logger.debug(f"[资讯助理] LLM 摘要已缓存：{key}")
            return llm_result, True
        else:
            # LLM 失败/无改进：临时缓存，下次仍会重试
            self._reminder_cache[key] = _LOCAL_PREFIX + local_result
            logger.debug(f"[资讯助理] 本地规则临时缓存（下次重试）：{key}")
            return local_result, True

    async def format_reminders(self) -> str:
        """格式化今日及未来提醒，供 /今日情报 手动指令调用。"""
        json_reminders, sys_tasks = await asyncio.gather(
            self._load_reminders(),
            self._load_system_tasks(),
        )
        return await self._format_reminders_serial(json_reminders, sys_tasks)

    async def _format_reminders_serial(
        self,
        json_reminders: list[dict],
        sys_tasks: list[dict],
    ) -> str:
        """
        合并两路提醒数据，逐条格式化摘要（带缓存）后组装展示文本。

        缓存策略：
        - LLM 成功 → 永久缓存，后续推送零 LLM 调用
        - LLM 失败 → 临时缓存（前缀 local:），下次推送重试
        - 本次未涉及的旧缓存条目 → 写盘时自动清除（防文件堆积）
        """
        await self._load_cache()

        # 去重合并
        seen: set[tuple[str, str]] = set()
        reminders: list[dict] = []
        for r in json_reminders + sys_tasks:
            key = (r.get("date", ""), r.get("content", ""))
            if key not in seen:
                seen.add(key)
                reminders.append(r)

        now       = self._now()
        today     = now.date()
        today_str = today.strftime("%Y-%m-%d")
        future_dates = {
            (today + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(1, self.reminder_lookback_days + 1)
        }

        today_items: list[dict] = []
        week_items : list[dict] = []
        for r in reminders:
            d = r.get("date", "")
            if d == today_str:
                today_items.append(r)
            elif d in future_dates:
                week_items.append(r)

        if not today_items and not week_items:
            return "📝 【提醒事项】近期无安排，享受生活吧！"

        all_items = today_items + week_items
        active_keys: set[str] = set()
        cache_dirty = False

        fmt_today : list[tuple[str, str]] = []          # (run_time, text)
        fmt_future: list[tuple[str, str, str]] = []     # (label, run_time, text)

        for i, r in enumerate(all_items):
            content  = r.get("content", "")
            run_time = r.get("run_time", "")
            date_str = r.get("date", today_str)
            is_today = date_str == today_str

            text, dirty = await self._get_reminder_summary(content, run_time, date_str, i == 0)
            text = self._strip_trailing_time(text)
            cache_dirty = cache_dirty or dirty
            active_keys.add(self._cache_key(content, run_time))

            if is_today:
                fmt_today.append((run_time, text))
            else:
                try:
                    label = self._relative_label(datetime.date.fromisoformat(date_str), today)
                except ValueError:
                    label = date_str[5:]
                fmt_future.append((label, run_time, text))

        if cache_dirty:
            asyncio.create_task(self._save_cache(active_keys))

        parts: list[str] = []
        if fmt_today:
            fmt_today.sort(key=lambda x: x[0] or "99:99")
            lines = [(f"{rt} {t}" if rt else t) for rt, t in fmt_today]
            parts.append("📝 【今日待办】\n" + "\n".join(f"🔔 {t}" for t in lines))
        if fmt_future:
            lines = [f"{lbl} {rt}：{t}" if rt else f"{lbl}：{t}" for lbl, rt, t in fmt_future]
            parts.append("📅 【未来提醒】\n" + "\n".join(f"📌 {t}" for t in lines))
        return "\n\n".join(parts)

    # -----------------------------------------------------------------------
    # 3. 新闻模块
    # -----------------------------------------------------------------------

    async def fetch_60s_news_text(self, session: aiohttp.ClientSession) -> str:
        """获取 60s 纯文本新闻（双 URL 降级容错）。"""
        urls = [
            "https://60s.viki.moe/v2/60s",
            "https://60s-api.114128.xyz/v2/60s",
        ]
        for url in urls:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(f"[资讯助理] 新闻接口 HTTP {resp.status}：{url}")
                        continue
                    data = await resp.json()
                    items: list[str] = data.get("data", {}).get("news", [])
                    if not items:
                        continue
                    body = "\n \n".join(f"{i}. {item}" for i, item in enumerate(items, 1))
                    return f"📰 【每日60s纯文本速报】\n\n{body}"
            except (aiohttp.ClientError, json.JSONDecodeError, KeyError) as exc:
                logger.warning(f"[资讯助理] 新闻接口失败 {url}：{exc}")
            except asyncio.TimeoutError:
                logger.warning(f"[资讯助理] 新闻接口超时：{url}")
        return "📰 【新闻速报】获取失败，接口波动。"

    # -----------------------------------------------------------------------
    # 4. 汇率模块
    # -----------------------------------------------------------------------

    async def fetch_exchange_rates(self, session: aiohttp.ClientSession) -> str:
        """获取实时汇率（以 base_currency 为基准，双向展示）。"""
        if not self.exchange_api_key:
            return "📊 【汇率】⚠️ 未配置 API Key。"
        url = f"https://v6.exchangerate-api.com/v6/{self.exchange_api_key}/latest/{self.base_currency}"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"[资讯助理] 汇率 API HTTP {resp.status}，货币：{self.base_currency}")
                    return "📊 【汇率】API 请求失败。"
                data = await resp.json()
            rates = data.get("conversion_rates", {})
            lines = []
            for cur in self.target_currencies:
                rate = rates.get(cur)
                if rate:
                    lines.append(
                        f"- {cur}：1 {self.base_currency} = {rate:.4f} {cur}"
                        f"  |  100 {cur} ≈ {100/rate:.2f} {self.base_currency}"
                    )
            return ("📊 【实时汇率】\n" + "\n".join(lines)) if lines else "📊 【汇率】暂无有效数据。"
        except aiohttp.ClientError as exc:
            logger.warning(f"[资讯助理] 汇率网络错误：{exc}")
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
            async with session.get(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": f"Bearer {self.deepseek_key}"},
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[资讯助理] DeepSeek 余额 HTTP {resp.status}")
                    return "- DeepSeek: 查询失败"
                data = await resp.json()
            infos: list[dict] = data.get("balance_infos", [])
            if infos:
                return "- DeepSeek: " + " / ".join(
                    f"{i.get('total_balance')} {i.get('currency')}" for i in infos
                )
        except aiohttp.ClientError as exc:
            logger.warning(f"[资讯助理] DeepSeek 网络错误：{exc}")
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
            async with session.get(
                "https://api.moonshot.cn/v1/users/me/balance",
                headers={"Authorization": f"Bearer {self.moonshot_key}"},
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[资讯助理] Kimi 余额 HTTP {resp.status}")
                    return "- Kimi: 查询失败"
                data = await resp.json()
            available = data.get("data", {}).get("available_balance", 0)
            return f"- Kimi: ￥{available:.2f}"
        except aiohttp.ClientError as exc:
            logger.warning(f"[资讯助理] Kimi 网络错误：{exc}")
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
        组装完整情报文本（单条消息）。

        执行顺序：
        1. HTTP 请求（天气/汇率/余额/新闻）与提醒数据加载并发——均不调用 LLM。
        2. HTTP 全部返回后，串行 LLM 格式化提醒（API 此时最空闲）。
        3. 按 module_order 一次性组装，返回单条文本。
        """
        logger.info("[资讯助理] 开始并发拉取情报...")

        async def _skip() -> str:
            return ""

        timeout = aiohttp.ClientTimeout(total=self.request_timeout)

        # 提醒数据加载与 HTTP 请求并发（均无 LLM 调用）
        reminder_coro = asyncio.gather(
            self._load_reminders(), self._load_system_tasks()
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
                reminder_coro,
            )

        def _safe(r, fallback: str) -> str:
            return r if isinstance(r, str) else fallback

        weather_text  = _safe(http_results[0], "🌤️ 【天气】获取超时")
        exchange_text = _safe(http_results[1], "📊 【汇率】获取超时")
        ds_balance    = _safe(http_results[2], "- DeepSeek: 超时")
        ms_balance    = _safe(http_results[3], "- Kimi: 超时")
        news_text     = _safe(http_results[4], "📰 【新闻】获取超时")

        # 串行 LLM 格式化（HTTP 已全部返回，API 压力最低）
        reminders_text = ""
        if self.enable_reminders and isinstance(reminder_raw, (list, tuple)):
            json_reminders, sys_tasks = reminder_raw
            reminders_text = await self._format_reminders_serial(json_reminders, sys_tasks)

        balance_block = f"💰 【API 资产监控】\n{ds_balance}\n{ms_balance}"
        module_map = {
            "weather":   weather_text   if self.enable_weather   else "",
            "reminders": reminders_text if self.enable_reminders else "",
            "exchange":  exchange_text  if self.enable_exchange  else "",
            "balance":   balance_block  if self.enable_balance   else "",
            "news":      news_text      if self.enable_news      else "",
        }
        blocks = [module_map[m] for m in self.module_order if module_map.get(m)]
        return _DIVIDER.join(blocks) if blocks else "📭 资讯助理：所有情报模块已在后台关闭。"

    # -----------------------------------------------------------------------
    # 定时推送
    # -----------------------------------------------------------------------

    async def _push_loop(self) -> None:
        """持续运行的定时推送循环，精确计算每次剩余秒数避免漂移。"""
        hour, minute = self._parse_push_time(self.push_time)
        logger.info(f"[资讯助理] 推送循环启动，目标时间 {hour:02d}:{minute:02d}。")
        while True:
            seconds = self._seconds_until(hour, minute)
            logger.debug(f"[资讯助理] 距下次推送 {seconds:.0f} 秒。")
            await asyncio.sleep(seconds)
            try:
                await self._broadcast()
            except Exception:
                logger.error(f"[资讯助理] 广播异常：{traceback.format_exc()}")

    @staticmethod
    def _parse_push_time(push_time: str) -> tuple[int, int]:
        try:
            h, m = map(int, push_time.strip().split(":"))
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h, m
        except (ValueError, AttributeError):
            pass
        logger.warning(f"[资讯助理] 推送时间 '{push_time}' 格式无效，已回退至 08:00。")
        return 8, 0

    def _seconds_until(self, hour: int, minute: int) -> float:
        now    = self._now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        return (target - now).total_seconds()

    async def _broadcast(self) -> None:
        """向所有目标群组推送情报（单条消息）。"""
        if not self.target_groups:
            logger.warning("[资讯助理] 未配置推送目标，跳过。")
            return
        text  = await self.build_news_text()
        chain = MessageChain([Plain(text)])
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
        yield event.plain_result("🚀 资讯助理正在拉取情报，请稍候...")
        yield event.plain_result(await self.build_news_text())

    @filter.command("提醒诊断")
    async def diagnose_reminders(self, event: AstrMessageEvent):
        """显示数据库路径、cron_jobs 任务列表及本地提醒状态。用法：/提醒诊断"""
        lines = ["🔍 【资讯助理提醒诊断】"]

        db_path = self._find_db_path()
        lines.append(f"\n✅ 数据库：{db_path}" if db_path else "\n❌ 未找到数据库")

        if db_path:
            def _inspect():
                out = []
                try:
                    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
                    conn.row_factory = sqlite3.Row
                    cur = conn.cursor()
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cron_jobs'")
                    if not cur.fetchone():
                        conn.close()
                        return ["\n⚠️ cron_jobs 表不存在"]
                    cur.execute(
                        "SELECT name, description, next_run_time, payload, status, enabled "
                        "FROM cron_jobs ORDER BY next_run_time"
                    )
                    rows = cur.fetchall()
                    conn.close()
                    out.append(f"\n📋 cron_jobs：{len(rows)} 条任务")
                    for row in rows:
                        run_at = ""
                        try:
                            run_at = json.loads(row["payload"] or "{}").get("run_at", "") or row["next_run_time"] or ""
                        except Exception:
                            run_at = row["next_run_time"] or ""
                        flag = "启用" if row["enabled"] else "禁用"
                        out.append(f"  [{row['status']}|{flag}] {str(run_at)[:16]}  {(row['name'] or '')[:30]}")
                        if row["description"]:
                            out.append(f"    {row['description'][:50]}")
                except Exception as exc:
                    out.append(f"\n❌ 查询失败：{exc}")
                return out
            lines.extend(await asyncio.to_thread(_inspect))

        local = await self._load_reminders()
        lines.append(f"\n📄 reminders.json：{len(local)} 条")
        for r in local[:5]:
            lines.append(f"  [{r.get('date')}] {r.get('content', '')[:50]}")

        now = self._now()
        lines.append(f"\n🕐 UTC+{self.timezone_offset}，当前：{now.strftime('%Y-%m-%d %H:%M')}")
        yield event.plain_result("\n".join(lines))

    @filter.command("添加提醒")
    async def add_reminder_cmd(self, event: AstrMessageEvent):
        """手动添加提醒。用法：/添加提醒 YYYY-MM-DD 事项内容"""
        body = (
            event.message_str.strip()
            .removeprefix("/添加提醒")
            .removeprefix("添加提醒")
            .strip()
        )
        parts = body.split(None, 1)
        if len(parts) < 2:
            yield event.plain_result(
                "❌ 格式错误。\n用法：/添加提醒 YYYY-MM-DD 事项内容\n"
                "示例：/添加提醒 2026-04-01 记得交作业"
            )
            return
        yield event.plain_result(await self._add_reminder(parts[0], parts[1]))

    # -----------------------------------------------------------------------
    # 生命周期
    # -----------------------------------------------------------------------

    async def terminate(self) -> None:
        """插件卸载时取消后台推送任务。"""
        if self._push_task and not self._push_task.done():
            self._push_task.cancel()
            try:
                await self._push_task
            except asyncio.CancelledError:
                pass
        logger.info("[资讯助理] 插件已安全停止。")
