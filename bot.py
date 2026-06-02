import os
import asyncio
import logging
import json
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import ReplyKeyboardBuilder

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, OPENROUTER_API_KEY
from db import (
    register_user, save_user_search, get_user_searches, delete_user_search,
    get_all_active_searches, update_last_checked_price, get_all_transit_hubs,
    get_all_manual_legs
)
from providers import TravelpayoutsProvider
from solver import GraphSolver
from analyst import LLMCognitiveAnalyst
from discovery import RouteDiscoveryService

logger = logging.getLogger("bot")

# Define States for Search Wizard
class SearchWizard(StatesGroup):
    waiting_for_origin = State()
    waiting_for_destination = State()
    waiting_for_dates = State()
    waiting_for_budget = State()
    waiting_for_baggage = State()
    waiting_for_max_legs = State()
    waiting_for_stopovers = State()
    waiting_for_exclusions = State()

router = Router()
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)
scheduler = AsyncIOScheduler()

# Modules
provider = TravelpayoutsProvider()
solver = GraphSolver()
analyst = LLMCognitiveAnalyst()
discovery_service = RouteDiscoveryService(provider)

# Helpers for dynamic LLM Parsing
async def call_llm(prompt: str) -> str:
    """Helper to query either OpenRouter or Google Gemini API depending on available keys."""
    import httpx
    
    if OPENROUTER_API_KEY and "sk-or-" in OPENROUTER_API_KEY:
        try:
            headers = {
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
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
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers)
                response.raise_for_status()
                result = response.json()
                choices = result.get("choices", [])
                if choices:
                    return choices[0]["message"]["content"]
        except Exception as e:
            logger.error(f"OpenRouter call error in helper: {e}")
            
    if GEMINI_API_KEY and GEMINI_API_KEY != "your_gemini_api_key_here":
        try:
            import google.generativeai as genai
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel("gemini-1.5-flash")
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            logger.error(f"Gemini API call error in helper: {e}")
            
    return ""

async def parse_location_with_llm(text: str, is_country: bool = False) -> dict:
    """Uses LLM to resolve text names to airport/city/country IATA codes."""
    role = "код страны ISO 3166-1 alpha-2 (например, CN для Китая, VN для Вьетнама, TH для Таиланда)" if is_country else "3-буквенный IATA код аэропорта/города (например, UFA для Уфы, MOW для Москвы, PEK для Пекина)"
    prompt = f"""
Преобразуй название '{text}' в {role}. 
Ответь строго в формате JSON, ничего кроме JSON не выводи:
{{"iata": "КОД_ЗДЕСЬ", "resolved_name": "Красивое название на русском"}}
"""
    resp_text = await call_llm(prompt)
    if resp_text:
        try:
            cleaned = resp_text.strip().replace("```json", "").replace("```", "")
            return json.loads(cleaned)
        except Exception as e:
            logger.error(f"Failed to parse LLM location response JSON: {e}. Raw response: {resp_text}")

    # Fallback if no LLM key or call failed
    if is_country:
        return {"iata": "CN" if "кит" in text.lower() else text.upper()[:2], "resolved_name": text}
    return {"iata": "UFA" if "уф" in text.lower() else text.upper()[:3], "resolved_name": text}

async def parse_dates_with_llm(text: str) -> dict:
    """Uses LLM to resolve flexible date strings to concrete YYYY-MM-DD ranges."""
    current_year = datetime.now().year
    current_date = datetime.now().strftime("%Y-%m-%d")
    
    prompt = f"""
Текущая дата: {current_date}. Текущий год: {current_year}.
Преобразуй человеческое описание дат '{text}' в две конкретные даты начала и конца периода (диапазон до 30 дней максимум).
Примеры:
- 'ближайшие 2 недели' -> start: завтра, end: через 14 дней
- 'середина июня' -> start: 2026-06-12, end: 2026-06-25
Ответь строго в формате JSON, без лишнего текста:
{{"date_start": "YYYY-MM-DD", "date_end": "YYYY-MM-DD", "desc": "Описание диапазона на русском"}}
"""
    resp_text = await call_llm(prompt)
    if resp_text:
        try:
            cleaned = resp_text.strip().replace("```json", "").replace("```", "")
            return json.loads(cleaned)
        except Exception as e:
            logger.error(f"Failed to parse LLM dates response JSON: {e}. Raw response: {resp_text}")

    # Fallback if no LLM key or call failed
    today = datetime.now()
    return {
        "date_start": (today + timedelta(days=1)).strftime("%Y-%m-%d"),
        "date_end": (today + timedelta(days=15)).strftime("%Y-%m-%d"),
        "desc": text
    }

# Command Handlers
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    register_user(message.from_user.id, message.chat.id)
    welcome_text = (
        "👋 Привет! Я умный бот-маршрутизатор билетов.\n\n"
        "Я умею искать хитрые каскадные маршруты с пересадками и стоповерами (остановками в городах на 2-5 дней), "
        "чтобы ты мог дешево долететь в любую точку мира и посмотреть новые города по пути.\n\n"
        "Цены я беру из кэша Aviasales, а анализирую и объясняю маршруты с помощью ИИ.\n\n"
        "ℹ️ **Доступные команды:**\n"
        "✈️ /new_search — Запустить настройку нового мониторинга\n"
        "📋 /my_searches — Список твоих активных поисков\n"
        "❌ /cancel — Отменить настройку в любой момент"
    )
    await message.answer(welcome_text, parse_mode="Markdown")

@router.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Настройка мониторинга отменена. Возвращаемся в меню.", reply_markup=types.ReplyKeyboardRemove())

# Search Wizard Implementation
@router.message(Command("new_search", "newsearch"))
async def cmd_new_search(message: types.Message, state: FSMContext):
    register_user(message.from_user.id, message.chat.id)
    await state.clear()
    await state.set_state(SearchWizard.waiting_for_origin)
    await message.answer("🏙️ **Шаг 1 из 7:** Откуда летим?\nНапишите название города (например, Уфа) или его IATA-код (UFA):")

@router.message(SearchWizard.waiting_for_origin)
async def process_origin(message: types.Message, state: FSMContext):
    origin_text = message.text.strip()
    status_msg = await message.answer("🔍 Распознаю город...")
    resolved = await parse_location_with_llm(origin_text, is_country=False)
    await status_msg.delete()
    
    await state.update_data(origin_iata=resolved["iata"], origin_name=resolved.get("resolved_name", origin_text))
    await state.set_state(SearchWizard.waiting_for_destination)
    await message.answer(
        f"✅ Город отправления определен как: **{resolved.get('resolved_name', origin_text)} ({resolved['iata']})**\n\n"
        f"🌏 **Шаг 2 из 7:** Куда летим?\nНапишите название страны назначения (например, Китай, Вьетнам, Таиланд) или код IATA:"
    )

@router.message(SearchWizard.waiting_for_destination)
async def process_destination(message: types.Message, state: FSMContext):
    dest_text = message.text.strip()
    status_msg = await message.answer("🔍 Распознаю страну...")
    resolved = await parse_location_with_llm(dest_text, is_country=True)
    await status_msg.delete()
    
    await state.update_data(dest_iata=resolved["iata"], dest_name=resolved.get("resolved_name", dest_text))
    await state.set_state(SearchWizard.waiting_for_dates)
    await message.answer(
        f"✅ Страна назначения определена как: **{resolved.get('resolved_name', dest_text)} ({resolved['iata']})**\n\n"
        f"📅 **Шаг 3 из 7:** Когда летим?\nНапишите дату или диапазон человеческим языком (например, *середина июня*, *ближайшие 2 недели*, *с 15 по 30 июня 2026*):"
    )

@router.message(SearchWizard.waiting_for_dates)
async def process_dates(message: types.Message, state: FSMContext):
    date_text = message.text.strip()
    status_msg = await message.answer("🔍 Парсю даты с помощью ИИ...")
    resolved = await parse_dates_with_llm(date_text)
    await status_msg.delete()
    
    await state.update_data(
        date_start=resolved["date_start"], 
        date_end=resolved["date_end"],
        dates_desc=resolved["desc"]
    )
    await state.set_state(SearchWizard.waiting_for_budget)
    await message.answer(
        f"✅ Диапазон дат определен как: **{resolved['date_start']} — {resolved['date_end']} ({resolved['desc']})**\n\n"
        f"💰 **Шаг 4 из 7:** Максимальный бюджет?\nВведите максимальную стоимость билетов в рублях (например, 50000):"
    )

def parse_budget(text: str) -> int | None:
    import re
    # 1. Clean whitespace, lowercase and normalize commas to dots
    cleaned = text.lower().strip().replace("\xa0", "").replace(" ", "")
    cleaned = cleaned.replace(",", ".")
    
    # 2. Check for numeric values with k/к/тыс/тысяч/т multipliers
    match_multiplier = re.match(r"^(\d+(?:\.\d+)?)(?:к|k|тыс|тысяч|т)", cleaned)
    if match_multiplier:
        try:
            val = float(match_multiplier.group(1))
            return int(val * 1000)
        except ValueError:
            pass
            
    # 3. Clean up non-digits to get just the number part if it is at the beginning (e.g. 20000рублей -> 20000)
    match_digits = re.match(r"^(\d+)", cleaned)
    if match_digits:
        try:
            return int(match_digits.group(1))
        except ValueError:
            pass
            
    return None

@router.message(SearchWizard.waiting_for_budget)
async def process_budget(message: types.Message, state: FSMContext):
    budget = parse_budget(message.text)
    if budget is None or budget <= 0:
        await message.answer("❌ Пожалуйста, введите корректное число (например: 50000, 50к, 20 тысяч рублей):")
        return
        
    await state.update_data(max_budget=budget)
    await state.set_state(SearchWizard.waiting_for_baggage)
    
    kb = ReplyKeyboardBuilder()
    kb.button(text="Да")
    kb.button(text="Нет (только ручная кладь)")
    kb.adjust(2)
    
    await message.answer(
        "🎒 **Шаг 5 из 7:** Нужен ли багаж?\nЭто влияет на риски при самостоятельных пересадках (короткие стыки не пройдут с багажом):",
        reply_markup=kb.as_markup(resize_keyboard=True, one_time_keyboard=True)
    )

@router.message(SearchWizard.waiting_for_baggage)
async def process_baggage(message: types.Message, state: FSMContext):
    text = message.text.strip().lower()
    baggage_needed = 1 if "да" in text else 0
    await state.update_data(baggage_needed=baggage_needed)
    await state.set_state(SearchWizard.waiting_for_max_legs)
    
    kb = ReplyKeyboardBuilder()
    kb.button(text="1 (Прямой)")
    kb.button(text="2 (До 1 пересадки)")
    kb.button(text="3 (До 2 пересадок)")
    kb.button(text="4 (До 3 пересадок)")
    kb.button(text="5 (До 4 пересадок)")
    kb.adjust(3, 2)
    
    await message.answer(
        "✈️ **Шаг 6 из 8:** Сколько перелетов максимум вы готовы сделать?\n"
        "1 — только прямые рейсы\n"
        "2 — максимум 1 пересадка (2 перелета)\n"
        "3 — максимум 2 пересадки (3 перелета)\n"
        "4 — максимум 3 пересадки (4 перелета)\n"
        "5 — максимум 4 пересадки (5 перелетов)",
        reply_markup=kb.as_markup(resize_keyboard=True, one_time_keyboard=True)
    )

@router.message(SearchWizard.waiting_for_max_legs)
async def process_max_legs(message: types.Message, state: FSMContext):
    text = message.text.strip()
    import re
    match = re.search(r"([12345])", text)
    if not match:
        await message.answer("❌ Пожалуйста, выберите один из вариантов: 1, 2, 3, 4 или 5.")
        return
        
    max_legs = int(match.group(1))
    max_transfers = max_legs - 1
    await state.update_data(max_transfers=max_transfers)
    
    await state.set_state(SearchWizard.waiting_for_stopovers)
    await message.answer(
        "🏝️ **Шаг 7 из 8:** Стоповеры (Остановки по пути)\n"
        "Где вы хотите задержаться на 2-5 дней для прогулки?\n"
        "Напишите города через запятую (например: *Ереван, Алматы*) или отправьте **Все**, чтобы разрешить любые транзитные хабы:",
        reply_markup=types.ReplyKeyboardRemove()
    )

@router.message(SearchWizard.waiting_for_stopovers)
async def process_stopovers(message: types.Message, state: FSMContext):
    text = message.text.strip()
    stopovers = [] if text.lower() in ["все", "всё"] else [s.strip() for s in text.split(",")]
    
    await state.update_data(stopovers=stopovers)
    await state.set_state(SearchWizard.waiting_for_exclusions)
    await message.answer(
        "🛡️ **Шаг 8 из 8:** Исключения\n"
        "Какие страны/города транзита нужно точно исключить (например: *Стамбул, Дубай*)?\n"
        "Отправьте **Нет**, если ничего исключать не нужно:"
    )

@router.message(SearchWizard.waiting_for_exclusions)
async def process_exclusions(message: types.Message, state: FSMContext):
    text = message.text.strip()
    exclusions = [] if text.lower() in ["нет", "не надо"] else [s.strip() for s in text.split(",")]
    
    # Finalize search wizard
    data = await state.get_data()
    await state.clear()
    
    # Save search preferences in DB
    save_user_search(
        user_id=message.from_user.id,
        origin_iata=data["origin_iata"],
        destination_text=data["dest_iata"],  # Using country code or resolved destination
        date_start=data["date_start"],
        date_end=data["date_end"],
        max_transfers=data["max_transfers"],
        visa_allowed=1,  # Default to allowed, user can toggle in settings later
        lodging_exceptions={},  # Can be expanded
        max_budget=data["max_budget"]
    )
    
    await message.answer(
        "🎉 **Мониторинг успешно настроен!**\n\n"
        f"📍 Маршрут: `{data['origin_name']} ({data['origin_iata']})` ➔ `{data['dest_name']} ({data['dest_iata']})`\n"
        f"📅 Период: {data['date_start']} — {data['date_end']}\n"
        f"💰 Бюджет: до {data['max_budget']:,} ₽\n"
        f"🎒 Багаж: {'Да' if data['baggage_needed'] else 'Нет'}\n"
        f"✈️ Макс. перелетов: {data['max_transfers'] + 1}\n\n"
        "🤖 Я запускаю первый фоновый расчет билетов через API и нейросеть. Это займет около 30-60 секунд..."
    )
    
    # Trigger immediate calculation
    asyncio.create_task(run_single_search_and_send(message.from_user.id, message.chat.id, data))

@router.message(Command("my_searches", "mysearches"))
async def cmd_my_searches(message: types.Message):
    searches = get_user_searches(message.from_user.id)
    if not searches:
        await message.answer("📋 У вас пока нет настроенных активных поисков. Используйте /new_search для добавления!")
        return
        
    text = ["📋 **Ваши активные мониторинги:**\n"]
    for s in searches:
        text.append(
            f"🆔 ID: {s['id']}\n"
            f"✈️ Маршрут: `{s['origin_iata']}` ➔ `{s['destination_text']}`\n"
            f"📅 Даты: {s['date_start']} — {s['date_end']}\n"
            f"💰 Бюджет: до {s['max_budget']:,} ₽\n"
            f"Удалить: /del_{s['id']}\n"
            "---"
        )
    await message.answer("\n".join(text), parse_mode="Markdown")

@router.message(lambda msg: msg.text and msg.text.startswith("/del_"))
async def cmd_delete_search(message: types.Message):
    try:
        search_id = int(message.text.split("_")[1])
        delete_user_search(search_id, message.from_user.id)
        await message.answer(f"✅ Мониторинг ID {search_id} успешно удален!")
    except Exception as e:
        await message.answer("❌ Неверный ID поиска. Попробуйте еще раз.")

# Logic execution & background task
async def run_single_search_and_send(user_id: int, chat_id: int, search_config: dict):
    """Executes a single flight search cascade, path solver, LLM analyst, and sends result."""
    origin = search_config["origin_iata"]
    destination_country = search_config["dest_iata"]
    date_start = search_config["date_start"]
    date_end = search_config["date_end"]
    max_budget = search_config["max_budget"]
    max_transfers = search_config.get("max_transfers", 2)
    
    # Send initial status message to user
    status_msg = None
    try:
        status_msg = await bot.send_message(
            chat_id,
            "🔍 *Идет поиск авиабилетов...*\n\n"
            "1️⃣ [Шаг 1/3] Построение динамического графа пересадок (bidirectional discovery)..."
        )
    except Exception as e:
        logger.error(f"Failed to send status message: {e}")
        
    # Hardcode Chinese destination airports if target is CN (China)
    if destination_country == "CN":
        china_destinations = ["PEK", "PVG", "CAN", "CTU", "URC", "XIY", "HGH", "HRB"]
    else:
        china_destinations = [destination_country] # If specific airport was target
        
    # Level 1: Fetch flight segment caches
    # We query the whole month range if dates span multiple months, or just the specific month YYYY-MM
    start_dt = datetime.strptime(date_start, "%Y-%m-%d")
    end_dt = datetime.strptime(date_end, "%Y-%m-%d")
    months_to_query = set()
    
    curr = start_dt
    while curr <= end_dt:
        months_to_query.add(curr.strftime("%Y-%m"))
        curr += timedelta(days=28)
    months_to_query.add(end_dt.strftime("%Y-%m"))
    months_list = list(months_to_query)
    
    # Run dynamic bidirectional discovery
    try:
        candidate_edges = await discovery_service.discover_candidate_edges(
            origin=origin,
            destination_country=destination_country,
            destination_iatas=china_destinations,
            months=months_list,
            max_transfers=max_transfers
        )
    except Exception as e:
        logger.error(f"Error in discovery service: {e}")
        candidate_edges = set()
        
    # If no candidate edges were discovered, fallback to static hubs so we don't return zero flights
    if not candidate_edges:
        logger.warning("No dynamic candidates found. Falling back to default transit hubs...")
        hubs = get_all_transit_hubs()
        candidate_edges = set()
        for hub in hubs:
            candidate_edges.add((origin, hub["iata"]))
            for dest in china_destinations:
                candidate_edges.add((hub["iata"], dest))
        for dest in china_destinations:
            candidate_edges.add((origin, dest))
            
    # Compile actual query tasks
    tasks = []
    for edge in candidate_edges:
        edge_from, edge_to = edge
        for m in months_list:
            # Enforce direct_only=True for clean cascade building
            tasks.append(provider.get_prices(edge_from, edge_to, m, direct_only=True))
            
    # Throttled execution of tasks to prevent rate limits
    logger.info(f"Triggering {len(tasks)} flight segment API queries...")
    
    # Update status message with discovery results
    if status_msg:
        try:
            await status_msg.edit_text(
                f"🔍 *Идет поиск авиабилетов...*\n\n"
                f"1️⃣ [Шаг 1/3] Построен динамический граф: найдено `{len(candidate_edges)}` сегментов.\n"
                f"2️⃣ [Шаг 2/3] Загрузка цен для прямых рейсов: `0%` (0/{len(tasks)} запросов)..."
            )
        except Exception:
            pass
            
    chunk_size = 5
    total_chunks = (len(tasks) + chunk_size - 1) // chunk_size
    for i in range(0, len(tasks), chunk_size):
        chunk = tasks[i:i+chunk_size]
        await asyncio.gather(*chunk)
        
        # Update progress to user
        if status_msg:
            try:
                current_chunk = i // chunk_size + 1
                progress_pct = int((current_chunk / total_chunks) * 100)
                await status_msg.edit_text(
                    f"🔍 *Идет поиск авиабилетов...*\n\n"
                    f"1️⃣ [Шаг 1/3] Построен динамический граф: найдено `{len(candidate_edges)}` сегментов.\n"
                    f"2️⃣ [Шаг 2/3] Загрузка цен для прямых рейсов: `{progress_pct}%` ({min(i + chunk_size, len(tasks))}/{len(tasks)})..."
                )
            except Exception:
                pass
                
        await asyncio.sleep(1.0) # Rate limit cooling
        
    # Update status message before running analyst
    if status_msg:
        try:
            await status_msg.edit_text(
                f"🔍 *Идет поиск авиабилетов...*\n\n"
                f"1️⃣ [Шаг 1/3] Построен динамический граф: найдено `{len(candidate_edges)}` сегментов.\n"
                f"2️⃣ [Шаг 2/3] Цены успешно загружены из кэша!\n"
                f"3️⃣ [Шаг 3/3] Подготовка ИИ-анализа лучших маршрутов..."
            )
        except Exception:
            pass
            
    # Level 2: Solve DAG & Scorer
    solved_data = solver.solve(
        origin_iata=origin,
        destination_country_code=destination_country,
        destination_iatas=china_destinations,
        date_start_str=date_start,
        date_end_str=date_end,
        max_transfers=max_transfers,
        visa_allowed=1,
        lodging_exceptions=search_config.get("lodging_exceptions", {}),
        max_budget=max_budget
    )
    
    # Extract explored transit hubs from candidate edges
    explored_hubs = set()
    for edge in candidate_edges:
        u, v = edge
        if u != origin and u not in china_destinations:
            explored_hubs.add(u)
        if v != origin and v not in china_destinations:
            explored_hubs.add(v)
            
    # Build search metadata for transparency report
    metadata = {
        "hubs": list(explored_hubs),
        "segments_count": len(candidate_edges),
        "total_routes_found": solved_data.get("total_routes_found_before_filter", 0),
        "is_fallback_active": solved_data.get("is_fallback_active", False),
        "max_transfers": max_transfers,
        "china_destinations": china_destinations
    }
    
    # LLM Cognitive Analysis
    date_range = f"{date_start} — {date_end}"
    analysis_text = await analyst.analyze_routes(
        origin=origin,
        destination=destination_country,
        date_range=date_range,
        max_budget=max_budget,
        solved_data=solved_data,
        search_metadata=metadata
    )
    
    # Delete status message before sending final report
    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass
            
    # Send report
    try:
        # Check cheapest path price to update last_checked_price
        cheapest_routes = solved_data.get("cheapest", [])
        best_price = cheapest_routes[0]["total_price"] if cheapest_routes else 0
        
        # Split message if it is too long for Telegram (4096 chars limit)
        if len(analysis_text) > 4000:
            for x in range(0, len(analysis_text), 4000):
                await bot.send_message(chat_id, analysis_text[x:x+4000], parse_mode="Markdown", disable_web_page_preview=True)
        else:
            await bot.send_message(chat_id, analysis_text, parse_mode="Markdown", disable_web_page_preview=True)
            
        return best_price
    except Exception as e:
        logger.error(f"Failed to send telegram message: {e}")
        return 0

# Scheduler job
async def run_daily_monitoring_job():
    logger.info("Starting scheduled flight monitoring job...")
    searches = get_all_active_searches()
    for s in searches:
        logger.info(f"Processing monitoring search ID {s['id']} for user {s['user_id']}")
        config_data = {
            "origin_iata": s["origin_iata"],
            "dest_iata": s["destination_text"],
            "date_start": s["date_start"],
            "date_end": s["date_end"],
            "max_budget": s["max_budget"],
            "max_transfers": s.get("max_transfers", 2),
            "lodging_exceptions": json.loads(s.get("lodging_exceptions_json", "{}"))
        }
        
        # Run search
        best_price = await run_single_search_and_send(s["user_id"], s["chat_id"], config_data)
        
        # Update last checked price
        if best_price > 0:
            update_last_checked_price(s["id"], best_price)
            
        await asyncio.sleep(5.0) # Gap between users to reduce rate load

# Scheduler triggers configuration
def setup_scheduler():
    # Run every day at 10:00 local time
    scheduler.add_job(run_daily_monitoring_job, 'cron', hour=10, minute=0)
    scheduler.start()
    logger.info("Scheduler setup complete. Daily job scheduled at 10:00.")

async def main():
    # Configure scheduler and db
    setup_scheduler()
    logger.info("Starting Telegram Bot long-polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    from db import init_db
    init_db()
    asyncio.run(main())
