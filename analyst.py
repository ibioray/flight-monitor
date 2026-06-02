import logging
import json
import httpx
import google.generativeai as genai
from datetime import datetime
from config import GEMINI_API_KEY, OPENROUTER_API_KEY

logger = logging.getLogger("analyst")

def make_aviasales_link(origin: str, destination: str, date_str: str) -> str:
    """Generates a standard Aviasales booking link for a given route and date."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        day_month = dt.strftime("%d%m")
        return f"https://www.aviasales.ru/search/{origin}{day_month}{destination}1"
    except Exception:
        return "https://www.aviasales.ru"

class LLMCognitiveAnalyst:
    def __init__(self, api_key: str = GEMINI_API_KEY, openrouter_key: str = OPENROUTER_API_KEY):
        self.api_key = api_key
        self.openrouter_key = openrouter_key
        
        if self.openrouter_key and "sk-or-" in self.openrouter_key:
            self.model_type = "openrouter"
            self.model = None
            logger.info("OpenRouter configuration detected for LLM Analyst.")
        elif self.api_key and self.api_key != "your_gemini_api_key_here":
            self.model_type = "gemini"
            genai.configure(api_key=self.api_key)
            self.model = genai.GenerativeModel("gemini-1.5-flash")
            logger.info("Native Gemini API configuration detected for LLM Analyst.")
        else:
            self.model_type = "mock"
            self.model = None
            logger.warning("No LLM keys configured! Running in mock mode.")

    def format_route_json(self, routes: list[dict]) -> str:
        """Formats raw route dicts with booking links to feed to the LLM."""
        formatted_list = []
        for index, r in enumerate(routes):
            route_detail = {
                "number": index + 1,
                "total_price": f"{r['total_price']:,.0f} ₽".replace(",", " "),
                "base_tickets_price": f"{r['base_price']:,.0f} ₽".replace(",", " "),
                "lodging_price": f"{r['lodging_price']:,.0f} ₽".replace(",", " "),
                "duration_days": r["duration_days"],
                "stopovers": [f"{s['name']} на {s['days']} дн." for s in r["stopovers"]],
                "risks": r["risk_warnings"],
                "legs": []
            }
            
            for leg in r["segments"]:
                leg_info = {
                    "from": leg["origin"],
                    "to": leg["destination"],
                    "date": leg["depart_date"],
                    "price": f"{leg['price']:,.0f} ₽".replace(",", " "),
                    "airline": leg["airline"],
                    "type": "Поезд/Наземный" if leg.get("is_manual") else "Авиа",
                    "booking_link": make_aviasales_link(leg["origin"], leg["destination"], leg["depart_date"]) if not leg.get("is_manual") else "Купить на вокзале"
                }
                route_detail["legs"].append(leg_info)
                
            formatted_list.append(route_detail)
        return json.dumps(formatted_list, ensure_ascii=False, indent=2)

    async def analyze_routes(self, origin: str, destination: str, date_range: str, 
                             max_budget: float, solved_data: dict) -> str:
        """
        Takes the categorized paths from GraphSolver, runs them through Gemini,
        and returns a beautiful, formatted Russian summary message.
        """
        # Format the top categories to present to LLM
        cheapest_json = self.format_route_json(solved_data.get("cheapest", []))
        fastest_json = self.format_route_json(solved_data.get("fastest", []))
        stopovers_json = self.format_route_json(solved_data.get("stopovers", []))
        
        if not solved_data.get("cheapest") and not solved_data.get("fastest") and not solved_data.get("stopovers"):
            return "❌ К сожалению, по вашему запросу не найдено ни одного подходящего билета. Попробуйте изменить диапазон дат или увеличить бюджет!"

        prompt = f"""
Ты — умный тревел-аналитик и эксперт по сложным каскадным маршрутам. Твоя задача — помочь путешественнику выбрать лучший способ добраться из города {origin} в {destination} в даты {date_range} с бюджетом до {max_budget} рублей.

Перед тобой результаты работы математического графового построителя маршрутов. Он нашел реальные варианты билетов через API и скомпоновал их. Цены и даты абсолютно точны, не выдумывай свои!

Вот варианты по трем категориям:

### 🌟 САМЫЕ ДЕШЕВЫЕ ВАРИАНТЫ:
{cheapest_json}

### ⚡ САМЫЕ БЫСТРЫЕ ВАРИАНТЫ (минимум времени в пути):
{fastest_json}

### 🏝️ ВАРИАНТЫ С ИНТЕРЕСНЫМИ СТОПОВЕРАМИ (зависнуть в городах на 2-5 дней по пути):
{stopovers_json}

Твоя задача — написать красивый, структурированный отчет на русском языке для отправки в Telegram:
1. Сделай краткое вступление.
2. Представь топ-3 лучших/интересных вариантов (выбери самые адекватные из списков выше). Для каждого варианта:
   - Понятно распиши цепочку: Откуда -> Куда (дата, цена, вид транспорта).
   - Укажи полную стоимость (билеты + условное жилье в хабах).
   - Выдели плюсы и минусы/риски (особенно если это самостоятельная стыковка с получением багажа, смена аэропорта в Москве или короткий транзит).
   - Обязательно вставь кликабельные Markdown ссылки на покупку билетов (booking_link) для каждого авиа-сегмента! Ссылки должны выглядеть аккуратно, например: [Купить билет UFA -> MOW](ссылка).
3. Дай финальную рекомендацию: какой вариант выбрать в зависимости от целей (сэкономить, долететь без нервов или устроить мини-путешествие).

Правила оформления:
- Пиши живым, экспертным языком, без канцеляризмов.
- Используй эмодзи для наглядности.
- Соблюдай Markdown разметку для Telegram.
- Если у варианта высокий риск (например, airport change или короткая стыковка), выдели это жирным шрифтом и предупреди пользователя!
"""

        if self.model_type == "openrouter":
            try:
                logger.info("Sending routes to OpenRouter (google/gemini-2.5-flash) for cognitive analysis...")
                headers = {
                    "Authorization": f"Bearer {self.openrouter_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/sickn33/flight-cascading-monitor",
                    "X-Title": "Flight Cascading Monitor"
                }
                payload = {
                    "model": "google/gemini-2.5-flash",
                    "messages": [
                        {"role": "user", "content": prompt}
                    ]
                }
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers)
                    response.raise_for_status()
                    result = response.json()
                    choices = result.get("choices", [])
                    if choices:
                        return choices[0]["message"]["content"]
                    else:
                        logger.error(f"OpenRouter returned empty choices: {result}")
            except Exception as e:
                logger.error(f"OpenRouter API error: {e}")
            return "⚠️ Произошла ошибка OpenRouter при анализе ИИ. Вот сырые варианты:\n\n" + self._generate_mock_analysis(solved_data)

        elif self.model_type == "gemini" and self.model:
            try:
                logger.info("Sending routes to Gemini API for cognitive analysis...")
                response = self.model.generate_content(prompt)
                return response.text
            except Exception as e:
                logger.error(f"Gemini API error: {e}")
                return "⚠️ Произошла ошибка при анализе маршрутов нейросетью. Но вот сырые варианты:\n\n" + self._generate_mock_analysis(solved_data)

        else:
            logger.info("Running analyst in mock mode.")
            return self._generate_mock_analysis(solved_data)

    def _generate_mock_analysis(self, solved_data: dict) -> str:
        """Fallback markdown generator if LLM is offline or unconfigured."""
        lines = ["✈️ **Найденные варианты маршрутов:**\n"]
        
        categories = [
            ("🌟 Самый дешевый", "cheapest"),
            ("⚡ Самый быстрый", "fastest"),
            ("🏝️ Умный стоповер", "stopovers")
        ]
        
        for name, key in categories:
            routes = solved_data.get(key, [])
            if not routes:
                continue
            lines.append(f"### {name}:")
            for index, r in enumerate(routes[:2]):
                leg_desc = []
                for leg in r["segments"]:
                    link = make_aviasales_link(leg["origin"], leg["destination"], leg["depart_date"]) if not leg.get("is_manual") else None
                    transport = "🚆 поезд" if leg.get("is_manual") else "✈️ самолет"
                    if link:
                        leg_desc.append(f"{leg['origin']} ➔ {leg['destination']} ({leg['depart_date']}, [{leg['price']:,.0f} ₽]({link}), {transport})")
                    else:
                        leg_desc.append(f"{leg['origin']} ➔ {leg['destination']} ({leg['depart_date']}, {leg['price']:,.0f} ₽, {transport})")
                        
                chain = " ➔ ".join(leg_desc)
                lines.append(f"{index+1}. **Итого: {r['total_price']:,.0f} ₽** (Билеты: {r['base_price']:,.0f} ₽, Жилье: {r['lodging_price']:,.0f} ₽)")
                lines.append(f"   Маршрут: {chain}")
                if r["risk_warnings"]:
                    lines.append(f"   ⚠️ *Риски:* {', '.join(r['risk_warnings'])}")
                lines.append("")
                
        return "\n".join(lines)
