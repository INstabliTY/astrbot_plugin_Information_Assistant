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

@register("astrbot_plugin_mkt_daily_news", "全能商业助理", "聚合天气、提醒、纯文本新闻与汇率", "1.0.0")
class MorningNewsPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        config = config or {}
        self.config = config
        
        # 基础配置
        self.target_groups = config.get("target_groups", [])
        self.push_time = config.get("push_time", "08:00")
        self.city = config.get("city", "北京")
        self.exchange_api_key = config.get("exchange_api_key", "")
        self.base_currency = config.get("base_currency", "CNY").upper()
        self.target_currencies = config.get("target_currencies", "USD,JPY,EUR,GBP,HKD,AUD").upper().split(',')
        
        # AI API 配置
        self.deepseek_key = config.get("deepseek_key", "")
        self.moonshot_key = config.get("moonshot_key", "")

        # 【修复1：规范化数据持久化路径】
        data_dir = StarTools.get_data_dir()
        self.reminders_file = data_dir / "reminders.json"
        
        if not os.path.exists(self.reminders_file):
            with open(self.reminders_file, "w", encoding="utf-8") as f:
                json.dump([], f)

        # 启动定时调度器
        self.scheduler = AsyncIOScheduler()
        try:
            hour, minute = self.push_time.split(":")
            self.scheduler.add_job(
                self.broadcast_news,
                'cron',
                hour=int(hour),
                minute=int(minute),
                id="daily_morning_news_job"
            )
            self.scheduler.start()
            logger.info(f"[全能商业助理] 定时任务已设定，每天 {self.push_time} 推送")
        except Exception as e:
            logger.error(f"[全能商业助理] 定时任务创建失败: {e}")

    # ================= 1. 天气与穿衣模块 =================
    async def fetch_weather(self, session):
        # 【修复2：严格遵守 PEP 8，杜绝单行复合语句】
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

    # ================= 2. 提醒事项模块 =================
    @filter.command("添加提醒")
    async def add_reminder(self, event: AstrMessageEvent, date: str, *, content: str):
        # 【修复4：分离日期格式错误与 JSON 解析错误，精准定位潜在缺陷】
        try:
            datetime.datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            yield event.plain_result("❌ 格式错误！请使用：/添加提醒 YYYY-MM-DD 内容")
            return

        try:
            with open(self.reminders_file, "r", encoding="utf-8") as f:
                reminders = json.load(f)
        except json.JSONDecodeError:
            logger.error("reminders.json 文件损坏，尝试重置。")
            reminders = []
        except Exception as e:
            logger.error(f"读取提醒事项文件失败: {e}")
            yield event.plain_result("❌ 系统错误：无法读取提醒文件。")
            return

        reminders.append({"date": date, "content": content})
        reminders.sort(key=lambda x: x["date"])
        
        try:
            with open(self.reminders_file, "w", encoding="utf-8") as f:
                json.dump(reminders, f, ensure_ascii=False, indent=2)
            yield event.plain_result(f"✅ 成功添加提醒：\n日期：{date}\n内容：{content}")
        except Exception as e:
            logger.error(f"写入提醒事项文件失败: {e}")
            yield event.plain_result("❌ 系统错误：无法保存提醒文件。")

    def format_reminders(self):
        try:
            with open(self.reminders_file, "r", encoding="utf-8") as f:
                reminders = json.load(f)
        except json.JSONDecodeError:
            return "📝 【提醒事项】数据文件损坏。"
        except Exception:
            return "📝 【提醒事项】读取失败。"

        today = datetime.datetime.now().strftime("%Y-%m-%d")
        this_week = [(datetime.datetime.now() + datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(8)]
        
        today_list = [r for r in reminders if r['date'] == today]
        if today_list:
            res = "📝 【今日待办】\n"
            for r in today_list:
                res += f"✅ {r['content']}\n"
            return res.strip()
            
        week_list = [r for r in reminders if r['date'] in this_week]
        if week_list:
            res = "📝 【本周预警】\n"
            for r in week_list:
                res += f"📅 {r['date'][5:]}: {r['content']}\n"
            return res.strip()
            
        return "📝 【提醒事项】近期无安排，享受生活吧！"

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
                            # 带有物理空格的换行，防止 Telegram 自动吞空行
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

    # ================= 核心装配与推送逻辑 =================
    async def broadcast_news(self):
        logger.info("[全能商业助理] 开始组装并推送纯文本战报...")
        
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # 【修复3：同步逻辑与异步并发彻底分离】
            # 先单独执行极速的同步操作
            reminders_text = self.format_reminders()
            
            # 再打包需要网络请求的异步任务
            tasks = [
                self.fetch_weather(session),
                self.fetch_exchange_rates(session),
                self.fetch_deepseek_balance(session),
                self.fetch_moonshot_balance(session),
                self.fetch_60s_news_text(session)
            ]
            
            # 并发执行
            results = await asyncio.gather(*tasks, return_exceptions=True)

        # 按并发顺序解包
        weather_text = results[0] if not isinstance(results[0], Exception) else "🌤️ 【天气】获取超时"
        exchange_text = results[1] if not isinstance(results[1], Exception) else "📊 【汇率】获取超时"
        ds_balance = results[2] if not isinstance(results[2], Exception) else "- DeepSeek: 超时"
        ms_balance = results[3] if not isinstance(results[3], Exception) else "- Kimi: 超时"
        news_text = results[4] if not isinstance(results[4], Exception) else "📰 【新闻】获取超时"
        
        balance_text = f"💰 【API 资产监控】\n{ds_balance}\n{ms_balance}"
        
        divider = "\n\n---------------------------\n\n"
        final_text = (
            weather_text + divider + 
            reminders_text + divider + 
            exchange_text + divider + 
            balance_text + divider + 
            news_text
        )
        
        message_chain = MessageChain([Plain(final_text)])

        for target in self.target_groups:
            try:
                await self.context.send_message(target, message_chain)
                logger.info(f"[全能商业助理] 成功送达: {target}")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"[全能商业助理] 送达失败: {e}")

    @filter.command("今日情报")
    async def manual_trigger(self, event: AstrMessageEvent):
        """交互指令：手动触发"""
        yield event.plain_result("🚀 正在拉取宏观数据与全网简报 (约3秒)，请稍候...")
        await self.broadcast_news()

    async def terminate(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
