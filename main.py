import os
import json
import asyncio
import traceback
import aiohttp
import datetime
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from apscheduler.schedulers.asyncio import AsyncIOScheduler

@register("astrbot_plugin_Information_Assistant", "资讯助理", "聚合天气、提醒、纯文本新闻与汇率", "1.0.0")
class InformationAssistantPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        config = config or {}
        self.config = config
        
        # 🛡️ 终极兼容提取引擎：无视底层存储是扁平还是嵌套，全量精准捕获！
        def get_val(section, key, default):
            # 1. 尝试从新版 UI 的卡片（嵌套字典）里读
            if section in config and isinstance(config[section], dict) and key in config[section]:
                return config[section][key]
            # 2. 尝试从旧版缓存（扁平结构）里读
            if key in config:
                return config[key]
            # 3. 兜底默认值
            return default

        # 📦 1. 全局推送设置
        self.enable_push = get_val("push_settings", "enable_push", True)
        self.push_time = get_val("push_settings", "push_time", "08:00")
        self.target_groups = get_val("push_settings", "target_groups", [])
        self.timezone_offset = get_val("push_settings", "timezone_offset", "10")
        
        # 📦 2. 天气模块
        self.enable_weather = get_val("weather_settings", "enable_weather", True)
        self.city = get_val("weather_settings", "city", "北京")
        
        # 📦 3. 提醒模块
        self.enable_reminders = get_val("reminder_settings", "enable_reminders", True)
        
        # 📦 4. 汇率模块
        self.enable_exchange = get_val("exchange_settings", "enable_exchange", True)
        self.exchange_api_key = get_val("exchange_settings", "exchange_api_key", "")
        self.base_currency = get_val("exchange_settings", "base_currency", "CNY").upper()
        target_curr_str = get_val("exchange_settings", "target_currencies", "USD,JPY,EUR,GBP,HKD,AUD")
        self.target_currencies = target_curr_str.upper().split(',')
        
        # 📦 5. 余额监控
        self.enable_balance = get_val("balance_settings", "enable_balance", True)
        self.deepseek_key = get_val("balance_settings", "deepseek_key", "")
        self.moonshot_key = get_val("balance_settings", "moonshot_key", "")
        
        # 📦 6. 新闻模块
        news_cfg = config.get("news_settings", {})
        self.enable_news = news_cfg.get("enable_news", True)

        # 🚨 规范化数据持久化路径 (回归旧版指南中最稳妥的绝对路径)
        # 直接把账本焊死在插件自己的文件夹里，系统绝对删不掉！
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.reminders_file = os.path.join(plugin_dir, "reminders.json")
        
        if not os.path.exists(self.reminders_file):
            with open(self.reminders_file, "w", encoding="utf-8") as f:
                json.dump([], f)

        # 启动定时调度器
        self.scheduler = AsyncIOScheduler()
        if self.enable_push:
            try:
                hour, minute = self.push_time.split(":")
                self.scheduler.add_job(
                    self.broadcast_news,
                    'cron',
                    hour=int(hour),
                    minute=int(minute),
                    id="daily_information_push_job"
                )
                self.scheduler.start()
                logger.info(f"[资讯助理] 定时任务已设定，每天 {self.push_time} 推送")
            except Exception as e:
                logger.error(f"[资讯助理] 定时任务创建失败: {e}")
        else:
            logger.info("[资讯助理] 定时推送功能已在配置中关闭，将以纯被动模式运行。")

    # ================= 1. 天气与穿衣模块 =================
    async def fetch_weather(self, session):
        if not self.city:
            return ""
            
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={self.city}&count=1&language=zh"
        try:
            async with session.get(geo_url) as resp:
                geo_data = await resp.json()
                if not geo_data.get("results"):
                    return f"🌤️ 【{self.city}天气】获取失败，请检查拼写。"
                lat = geo_data["results"][0]["latitude"]
                lon = geo_data["results"][0]["longitude"]
            
            weather_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=auto"
            async with session.get(weather_url) as resp:
                data = await resp.json()
                temp_max = data["daily"]["temperature_2m_max"][0]
                temp_min = data["daily"]["temperature_2m_min"][0]
                rain_prob = data["daily"]["precipitation_probability_max"][0]
                
                if rain_prob > 40:
                    umbrella = "☔ 降水概率高，出门带伞！"
                else:
                    umbrella = "🌂 降水概率低，无需带伞。"
                    
                avg_temp = (temp_max + temp_min) / 2
                if avg_temp < 10:
                    clothes = "🧥 天寒，建议厚外套/羽绒服。"
                elif avg_temp < 20:
                    clothes = "🧣 微凉，建议夹克/薄毛衣。"
                elif avg_temp < 28:
                    clothes = "👕 舒适，建议长袖/薄外套。"
                else:
                    clothes = "🩳 炎热，建议清凉夏装。"
                
                return f"🌤️ 【{self.city}今日天气】\n🌡️ 温度：{temp_min}℃ ~ {temp_max}℃\n🌧️ 降水概率：{rain_prob}%\n{umbrella}\n{clothes}"
        except Exception:
            return f"🌤️ 【{self.city}天气】数据获取异常。"

    # ================= 2. 提醒事项模块 (终极双通道版) =================
    async def _save_to_json(self, date: str, content: str) -> str:
        """底层的统一写入引擎，同时服务于大模型和手动指令"""
        try:
            parsed_date = datetime.datetime.strptime(date, "%Y-%m-%d")
            standard_date = parsed_date.strftime("%Y-%m-%d")
        except ValueError:
            return f"❌ 解析时间失败，收到格式为：{date}。请使用 YYYY-MM-DD。"

        reminders = []
        if os.path.exists(self.reminders_file):
            try:
                with open(self.reminders_file, "r", encoding="utf-8") as f:
                    reminders = json.load(f)
            except Exception:
                pass

        reminders.append({"date": standard_date, "content": content})
        reminders.sort(key=lambda x: x["date"])
        
        try:
            with open(self.reminders_file, "w", encoding="utf-8") as f:
                json.dump(reminders, f, ensure_ascii=False, indent=2)
            return f"✅ 资讯助理待办已记录：\n📅 {standard_date}\n📝 {content}"
        except Exception as e:
            logger.error(f"写入提醒文件失败: {e}")
            return "❌ 系统错误：无法保存提醒文件。"

    @filter.llm_tool(name="add_information_reminder")
    async def add_reminder_tool(self, event: AstrMessageEvent, date: str, content: str):
        '''
        将用户的待办事项添加到资讯助理的本地日程表中。
        当用户在自然语言聊天中要求“添加提醒”、“记一下待办”或“安排日程”时，你必须调用此工具。
        
        Args:
            date(string): 严格转换为 YYYY-MM-DD 格式的未来日期。请根据当前系统推算。
            content(string): 待办事项的具体精简内容。
        '''
        res = await self._save_to_json(date, content)
        yield event.plain_result(res)

    @filter.command("添加提醒")
    async def add_reminder_cmd(self, event: AstrMessageEvent, date: str, *, content: str):
        """交互指令：手动添加提醒的兜底通道"""
        res = await self._save_to_json(date, content)
        yield event.plain_result(res)

    def format_reminders(self):
        reminders = []
        if os.path.exists(self.reminders_file):
            try:
                with open(self.reminders_file, "r", encoding="utf-8") as f:
                    reminders = json.load(f)
            except Exception:
                return "📝 【提醒事项】数据文件格式损坏，请重新添加。"

        tz_offset = float(self.timezone_offset) 
        tz_dynamic = datetime.timezone(datetime.timedelta(hours=tz_offset))
        today_dt = datetime.datetime.now(tz_dynamic)
        today_str = today_dt.strftime("%Y-%m-%d")
        
        this_week_strs = [(today_dt + datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 8)]

        today_list = []
        week_list = []

        for r in reminders:
            try:
                saved_date_obj = datetime.datetime.strptime(r['date'], "%Y-%m-%d")
                normalized_date_str = saved_date_obj.strftime("%Y-%m-%d")
                
                if normalized_date_str == today_str:
                    today_list.append(r)
                elif normalized_date_str in this_week_strs:
                    week_list.append(r)
            except ValueError:
                continue

        res = ""
        if today_list:
            res += "📝 【今日待办】\n"
            for r in today_list:
                res += f"✅ {r['content']}\n"
            res += "\n"
            
        if week_list:
            res += "📝 【本周预警】\n"
            for r in week_list:
                try:
                    nd = datetime.datetime.strptime(r['date'], "%Y-%m-%d").strftime("%Y-%m-%d")
                    res += f"📅 {nd[5:]}: {r['content']}\n"
                except Exception:
                    res += f"📅 {r['date'][5:]}: {r['content']}\n"
                
        if not res:
            return "📝 【提醒事项】近期无安排，享受生活吧！"
            
        return res.strip()

    # ================= 3. 纯文本新闻与汇率模块 =================
    async def fetch_60s_news_text(self, session):
        urls = ["https://60s.viki.moe/v2/60s", "https://60s-api.114128.xyz/v2/60s"]
        for url in urls:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        news_items = data.get("data", {}).get("news", [])
                        
                        if not news_items:
                            continue
                            
                        text = "📰 【每日60s纯文本速报】\n\n"
                        for i, item in enumerate(news_items, 1):
                            text += f"{i}. {item}\n \n"
                        
                        return text.strip()
            except Exception:
                continue
        return "📰 【新闻速报】获取失败，接口波动。"

    async def fetch_exchange_rates(self, session):
        if not self.exchange_api_key:
            return "📊 【汇率】⚠️ 未配置 API Key"
            
        url = f"https://v6.exchangerate-api.com/v6/{self.exchange_api_key}/latest/{self.base_currency}"
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    rates = data.get("conversion_rates", {})
                    text = f"📊 【实时汇率】(100外币 兑 {self.base_currency})\n"
                    for cur in self.target_currencies:
                        cur = cur.strip()
                        if cur in rates and rates[cur] != 0:
                            rate_value = 100 / rates[cur]
                            text += f"- {cur}: {rate_value:.2f}\n"
                    return text.strip()
        except Exception:
            pass
        return "📊 【汇率】数据获取失败。"

    # ================= 4. API 余额监控模块 =================
    async def fetch_deepseek_balance(self, session):
        if not self.deepseek_key:
            return "- DeepSeek: 未配置"
            
        url = "https://api.deepseek.com/user/balance"
        try:
            async with session.get(url, headers={"Authorization": f"Bearer {self.deepseek_key}"}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    infos = data.get("balance_infos", [])
                    if infos:
                        balances = [f"{info.get('total_balance')} {info.get('currency')}" for info in infos]
                        return f"- DeepSeek: {' / '.join(balances)}"
        except Exception:
            pass
        return "- DeepSeek: 查询异常"

    async def fetch_moonshot_balance(self, session):
        if not self.moonshot_key:
            return "- Kimi: 未配置"
            
        url = "https://api.moonshot.cn/v1/users/me/balance"
        try:
            async with session.get(url, headers={"Authorization": f"Bearer {self.moonshot_key}"}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    available = data.get("data", {}).get("available_balance", 0)
                    return f"- Kimi: ￥{available:.2f}"
        except Exception:
            pass
        return "- Kimi: 查询异常"

    # ================= 核心情报组装引擎 =================
    async def build_news_text(self) -> str:
        """纯粹的数据拉取与组装中心，不负责发送"""
        logger.info("[资讯助理] 开始组装并拉取情报...")
        
        async def skip_task(): return ""

        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = [
                self.fetch_weather(session) if self.enable_weather else skip_task(),
                self.fetch_exchange_rates(session) if self.enable_exchange else skip_task(),
                self.fetch_deepseek_balance(session) if self.enable_balance else skip_task(),
                self.fetch_moonshot_balance(session) if self.enable_balance else skip_task(),
                self.fetch_60s_news_text(session) if self.enable_news else skip_task()
            ]
            
            results = await asyncio.gather(*tasks, return_exceptions=True)

        weather_text = results[0] if not isinstance(results[0], Exception) else "🌤️ 【天气】获取超时"
        exchange_text = results[1] if not isinstance(results[1], Exception) else "📊 【汇率】获取超时"
        ds_balance = results[2] if not isinstance(results[2], Exception) else "- DeepSeek: 超时"
        ms_balance = results[3] if not isinstance(results[3], Exception) else "- Kimi: 超时"
        news_text = results[4] if not isinstance(results[4], Exception) else "📰 【新闻】获取超时"
        
        reminders_text = self.format_reminders() if self.enable_reminders else ""
        
        blocks = []
        if self.enable_weather and weather_text: blocks.append(weather_text)
        if self.enable_reminders and reminders_text: blocks.append(reminders_text)
        if self.enable_exchange and exchange_text: blocks.append(exchange_text)
        if self.enable_balance:
            blocks.append(f"💰 【API 资产监控】\n{ds_balance}\n{ms_balance}")
        if self.enable_news and news_text: blocks.append(news_text)
        
        if not blocks:
            return "📭 资讯助理：所有情报模块已在后台关闭。"
        else:
            divider = "\n\n---------------------------\n\n"
            return divider.join(blocks)

    # ================= 定时推送逻辑 =================
    async def broadcast_news(self):
        if not self.target_groups:
            logger.warning("[资讯助理] 定时推送已触发，但未配置推送目标群组 (target_groups)。")
            return
            
        final_text = await self.build_news_text()
        message_chain = MessageChain([Plain(final_text)])

        for target in self.target_groups:
            try:
                await self.context.send_message(target, message_chain)
                logger.info(f"[资讯助理] 定时早报成功送达: {target}")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"[资讯助理] 送达失败: {e}")

    # ================= 交互触发逻辑 =================
    @filter.command("今日情报")
    async def manual_trigger(self, event: AstrMessageEvent):
        """交互指令：手动触发"""
        yield event.plain_result("🚀 资讯助理正在为您拉取最新情报 (约需3-5秒)，请稍候...")
        
        final_text = await self.build_news_text()
        yield event.plain_result(final_text)

    async def terminate(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)