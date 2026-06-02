import os
import asyncio
import logging
import json
from datetime import datetime, timezone, timedelta
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
    get_all_manual_legs, save_search_snapshot, get_route_snapshot,
    get_snapshot_routes, get_discovery_cache, save_discovery_cache,
    clear_price_cache_for_edges, get_cache_status_for_search,
    get_db_connection, update_price_drop_threshold
)
from providers import TravelpayoutsProvider
from solver import GraphSolver
from analyst import LLMCognitiveAnalyst
from discovery import RouteDiscoveryService
from monitoring import (
    DEFAULT_PRICE_DROP_THRESHOLD_PCT,
    PRICE_DROP_THRESHOLD_OPTIONS,
    normalize_price_drop_threshold,
    price_drop_alert_decision,
)

logger = logging.getLogger("bot")

# Define States for Search Wizard
class SearchWizard(StatesGroup):
    waiting_for_origin = State()
    waiting_for_destination = State()
    waiting_for_dates = State()
    waiting_for_budget = State()
    waiting_for_price_alert = State()
    waiting_for_baggage = State()
    waiting_for_max_legs = State()
    waiting_for_stopovers = State()
    waiting_for_stopover_preset = State()
    waiting_for_visa_mode = State()
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

# Curator list of top airports for common countries (Codex B)
COUNTRY_AIRPORTS = {
    "CN": ["PEK", "PKX", "PVG", "SHA", "CAN", "SZX", "CTU", "TFU", "URC", "XIY", "HGH", "HRB"],
    "TH": ["BKK", "DMK", "HKT", "CNX", "USM", "KBV"],
    "VN": ["HAN", "SGN", "DAD", "CXR", "PQC"],
    "TR": ["IST", "SAW", "AYT", "ESB", "ADB"],
    "AE": ["DXB", "AUH", "SHJ", "DWC"],
    "AM": ["EVN"],
    "KZ": ["ALA", "NQZ", "CIT", "SCO"],
    "UZ": ["TAS", "SKD", "BHK", "UGC"],
    "KG": ["FRU", "OSS"],
    "AZ": ["GYD"],
    "GE": ["TBS", "BUS", "KUT"],
    "JP": ["TYO", "HND", "NRT", "KIX", "ITM", "NGO", "FUK", "CTS", "OKA"],
    "KR": ["SEL", "ICN", "GMP", "PUS", "CJU"],
    "ID": ["CGK", "DPS", "SUB", "KNO", "UPG"],
    "MY": ["KUL", "PEN", "BKI", "KCH", "LGK"],
    "SG": ["SIN"],
    "IN": ["DEL", "BOM", "BLR", "MAA", "HYD", "CCU", "GOI", "COK"],
    "LK": ["CMB"],
    "MV": ["MLE"],
    "PH": ["MNL", "CEB", "CRK", "DVO"],
    "AU": ["SYD", "MEL", "BNE", "PER", "ADL"],
    "US": ["NYC", "JFK", "EWR", "LAX", "SFO", "MIA", "ORD", "DFW", "SEA", "BOS"],
    "DE": ["FRA", "MUC", "BER", "DUS", "HAM"],
    "FR": ["PAR", "CDG", "ORY", "NCE", "LYS", "MRS"],
    "IT": ["ROM", "FCO", "MXP", "LIN", "VCE", "NAP"],
    "ES": ["MAD", "BCN", "AGP", "ALC", "PMI"],
    "GB": ["LON", "LHR", "LGW", "STN", "MAN", "EDI"],
    "EG": ["CAI", "HRG", "SSH", "LXR", "HBE"]
}

STOPOVER_PRESETS = {
    "fast": {
        "label": "Быстро",
        "min_stopover_hours": 0,
        "max_stopover_days": 1,
        "allow_awkward_layovers": 0,
    },
    "walk": {
        "label": "Погулять",
        "min_stopover_hours": 24,
        "max_stopover_days": 3,
        "allow_awkward_layovers": 0,
    },
    "mini_trip": {
        "label": "Мини-путешествие",
        "min_stopover_hours": 48,
        "max_stopover_days": 5,
        "allow_awkward_layovers": 0,
    },
    "price_only": {
        "label": "Только цена",
        "min_stopover_hours": 0,
        "max_stopover_days": 5,
        "allow_awkward_layovers": 1,
    },
    "balanced": {
        "label": "Баланс",
        "min_stopover_hours": 18,
        "max_stopover_days": 3,
        "allow_awkward_layovers": 1,
    },
}

VISA_MODES = {
    "visa_free_only": "Только известные безвизовые",
    "warn": "Предупреждать о визовых рисках",
    "ignore": "Не фильтровать визы",
}

def stopover_settings_for_preset(preset: str) -> dict:
    return dict(STOPOVER_PRESETS.get(preset, STOPOVER_PRESETS["balanced"]))

def format_stopover_settings(search: dict) -> str:
    preset = search.get("stopover_preset", "balanced")
    settings = stopover_settings_for_preset(preset)
    min_hours = int(search.get("min_stopover_hours", settings["min_stopover_hours"]) or 0)
    max_days = int(search.get("max_stopover_days", settings["max_stopover_days"]) or settings["max_stopover_days"])
    allow_awkward = int(search.get("allow_awkward_layovers", settings["allow_awkward_layovers"]) or 0)
    awkward_text = "да" if allow_awkward else "нет"
    min_text = f"{min_hours} ч" if min_hours else "нет"
    return f"{settings['label']} | мин. прогулка: {min_text} | макс: {max_days} дн. | неудобные: {awkward_text}"

def format_visa_mode(mode: str) -> str:
    return VISA_MODES.get(mode, VISA_MODES["visa_free_only"])

def parse_price_drop_threshold(text: str) -> float | None:
    cleaned = (text or "").lower().replace("%", "").replace("процентов", "").replace("процента", "").strip()
    import re
    match = re.search(r"(\d+(?:[\.,]\d+)?)", cleaned)
    if not match:
        return None
    return normalize_price_drop_threshold(match.group(1))

def format_price_drop_threshold(value) -> str:
    threshold = normalize_price_drop_threshold(value)
    return f"{threshold:g}%"

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
            from google import genai
            client = genai.Client(api_key=GEMINI_API_KEY)
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt
            )
            return response.text
        except Exception as e:
            logger.error(f"Google GenAI call error in helper: {e}")

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

async def get_airports_for_country_with_llm(country_code: str) -> list[str]:
    """Uses LLM to resolve country code to top 5-8 airport IATA codes in that country (Codex B)."""
    prompt = f"""
Напиши список из 5-8 крупнейших международных аэропортов (IATA коды) для страны с кодом {country_code}.
Ответь строго в формате JSON списка строк, без какого-либо дополнительного текста, например:
["BKK", "DMK", "HKT", "CNX", "UTP"]
"""
    resp_text = await call_llm(prompt)
    if resp_text:
        try:
            cleaned = resp_text.strip().replace("```json", "").replace("```", "")
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                return [
                    str(code).upper().strip()
                    for code in parsed
                    if isinstance(code, str) and len(code.strip()) == 3 and code.strip().isalpha()
                ][:8]
        except Exception as e:
            logger.error(f"Failed to parse LLM airports response JSON: {e}. Raw response: {resp_text}")
    return []

async def resolve_destination_airports(country_code: str) -> list[str]:
    """Returns top airport IATA codes for a country using deterministic catalog first (Codex B)."""
    code = country_code.upper()
    if code in COUNTRY_AIRPORTS:
        logger.info(f"Resolved airports for {code} deterministically: {COUNTRY_AIRPORTS[code]}")
        return COUNTRY_AIRPORTS[code]

    # Allow explicit IATA airport/city codes if the user entered one instead of a country code.
    if len(code) == 3 and code.isalpha():
        logger.info(f"Treating {code} as an explicit airport/city IATA code.")
        return [code]

    # LLM fallback
    logger.info(f"Resolving airports for {code} dynamically via LLM...")
    resolved = await get_airports_for_country_with_llm(code)
    logger.info(f"LLM resolved airports for {code}: {resolved}")
    return resolved

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

async def send_message_safely(chat_id: int, text: str):
    """Sends long messages safely, splitting by newlines and falling back to plain text if Markdown fails (DOP-4)."""
    chunks = []
    current_chunk = []
    current_len = 0

    for line in text.split("\n"):
        if current_len + len(line) + 1 > 4000:
            chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_len = len(line)
        else:
            current_chunk.append(line)
            current_len += len(line) + 1

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    for chunk in chunks:
        try:
            await bot.send_message(chat_id, chunk, parse_mode="Markdown", disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Markdown send failed: {e}. Falling back to plain text.")
            try:
                # Fallback without parse_mode
                await bot.send_message(chat_id, chunk, disable_web_page_preview=True)
            except Exception as e2:
                logger.error(f"Plain text send failed as well: {e2}")

def _parse_route_command_args(text: str) -> tuple[str | None, int | None]:
    parts = text.split()
    if len(parts) < 2:
        return None, None
    route_id = parts[1].strip().upper()
    search_id = None
    if len(parts) >= 3:
        try:
            search_id = int(parts[2])
        except ValueError:
            search_id = None
    return route_id, search_id

def _parse_more_routes_args(text: str) -> tuple[int | None, int, str]:
    parts = text.split()[1:]
    search_id = None
    offset = 5
    sort_mode = "balanced"
    aliases = {
        "price": "price", "цена": "price", "дешевле": "price",
        "duration": "duration", "time": "duration", "быстрее": "duration",
        "comfort": "comfort", "удобнее": "comfort",
        "stopover": "stopover", "стоповер": "stopover",
        "balanced": "balanced", "баланс": "balanced",
    }

    for part in parts:
        cleaned = part.strip().lower()
        if cleaned.startswith("search="):
            try:
                search_id = int(cleaned.split("=", 1)[1])
            except ValueError:
                pass
        elif cleaned.startswith("offset="):
            try:
                offset = max(0, int(cleaned.split("=", 1)[1]))
            except ValueError:
                pass
        elif cleaned in aliases:
            sort_mode = aliases[cleaned]
        else:
            try:
                offset = max(0, int(cleaned))
            except ValueError:
                pass

    return search_id, offset, sort_mode

async def _render_snapshot_routes(rows: list[dict], metadata: dict | None = None) -> str:
    routes = [json.loads(row["route_json"]) for row in rows]
    solved_data = {
        "recommended": routes,
        "is_fallback_active": False,
        "total_routes_after_filter": metadata.get("total_routes_after_filter", 0) if metadata else 0,
        "rendered_routes_count": len(routes)
    }
    return await analyst.analyze_routes(
        origin="",
        destination="",
        date_range="",
        max_budget=0,
        solved_data=solved_data,
        search_metadata=None
    )

def _months_between(date_start: str, date_end: str) -> list[str]:
    months = set()
    try:
        start_dt = datetime.strptime(date_start, "%Y-%m-%d")
        end_dt = datetime.strptime(date_end, "%Y-%m-%d")
        current = start_dt.replace(day=1)
        while current <= end_dt:
            months.add(current.strftime("%Y-%m"))
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1)
            else:
                current = current.replace(month=current.month + 1)
        months.add(end_dt.strftime("%Y-%m"))
    except ValueError:
        pass
    return sorted(months)

async def _candidate_edges_for_saved_search(user_id: int, search: dict, destination_iatas: list[str]) -> tuple[list[tuple[str, str]], list[str], str]:
    origin = search["origin_iata"]
    destination_country = search["destination_text"]
    max_transfers = search.get("max_transfers", 2)
    months = _months_between(search["date_start"], search["date_end"])

    cached_edges = await asyncio.to_thread(
        get_discovery_cache,
        origin,
        destination_country,
        destination_iatas,
        months,
        max_transfers
    )
    if cached_edges:
        return sorted(cached_edges), months, "discovery_cache"

    _, route_rows = await asyncio.to_thread(
        get_snapshot_routes,
        user_id,
        search.get("id"),
        0,
        1000,
        "balanced"
    )
    snapshot_edges = set()
    for row in route_rows:
        route = json.loads(row["route_json"])
        for leg in route.get("segments", []):
            if not leg.get("is_manual"):
                snapshot_edges.add((leg["origin"], leg["destination"]))
    if snapshot_edges:
        return sorted(snapshot_edges), months, "search_snapshot"

    return sorted({(origin, dest) for dest in destination_iatas}), months, "direct_fallback"

# Command Handlers
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await asyncio.to_thread(register_user, message.from_user.id, message.chat.id)
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
    await asyncio.to_thread(register_user, message.from_user.id, message.chat.id)
    await state.clear()
    await state.set_state(SearchWizard.waiting_for_origin)
    await message.answer("🏙️ **Шаг 1 из 10:** Откуда летим?\nНапишите название города (например, Уфа) или его IATA-код (UFA):")

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
        f"🌏 **Шаг 2 из 10:** Куда летим?\nНапишите название страны назначения (например, Китай, Вьетнам, Таиланд) или код IATA:"
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
        f"📅 **Шаг 3 из 10:** Когда летим?\nНапишите дату или диапазон человеческим языком (например, *середина июня*, *ближайшие 2 недели*, *с 15 по 30 июня 2026*):"
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
        f"💰 **Шаг 4 из 10:** Максимальный бюджет?\nВведите максимальную стоимость билетов в рублях (например, 50000, 50к, 20 тысяч):"
    )

def parse_budget(text: str) -> int | None:
    import re
    cleaned = text.lower().strip().replace("\xa0", "").replace(" ", "")
    cleaned = cleaned.replace(",", ".")

    match_multiplier = re.match(r"^(\d+(?:\.\d+)?)(?:к|k|тыс|тысяч|т)", cleaned)
    if match_multiplier:
        try:
            val = float(match_multiplier.group(1))
            return int(val * 1000)
        except ValueError:
            pass

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
    await state.set_state(SearchWizard.waiting_for_price_alert)

    kb = ReplyKeyboardBuilder()
    for pct in PRICE_DROP_THRESHOLD_OPTIONS:
        kb.button(text=f"{pct}%")
    kb.adjust(3, 2)

    await message.answer(
        "🔔 *Шаг 5 из 11:* При каком падении цены присылать уведомление?\n"
        "Например: 5% — чаще и чувствительнее, 15-20% — только заметные скидки.",
        reply_markup=kb.as_markup(resize_keyboard=True, one_time_keyboard=True)
    )

@router.message(SearchWizard.waiting_for_price_alert)
async def process_price_alert_threshold(message: types.Message, state: FSMContext):
    threshold = parse_price_drop_threshold(message.text or "")
    if threshold is None:
        await message.answer("❌ Выберите процент из кнопок или напишите число, например 8 или 10.")
        return

    await state.update_data(price_drop_threshold_pct=threshold)
    await state.set_state(SearchWizard.waiting_for_baggage)

    kb = ReplyKeyboardBuilder()
    kb.button(text="Да")
    kb.button(text="Нет (только ручная кладь)")
    kb.adjust(2)

    await message.answer(
        "🎒 *Шаг 6 из 11:* Нужен ли багаж?\n"
        "Это влияет на риски при самостоятельных пересадках: короткие стыки не пройдут с багажом.",
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
        "✈️ *Шаг 7 из 11:* Сколько перелетов максимум вы готовы сделать?\n"
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
        "🏝️ *Шаг 8 из 11:* Стоповеры (Остановки по пути)\n"
        "Где вы хотите задержаться на 2-5 дней для прогулки?\n"
        "Напишите города через запятую (например: *Ереван, Алматы*) или отправьте *Все*, чтобы разрешить любые транзитные хабы:",
        reply_markup=types.ReplyKeyboardRemove()
    )

@router.message(SearchWizard.waiting_for_stopovers)
async def process_stopovers(message: types.Message, state: FSMContext):
    text = message.text.strip()
    stopovers = [] if text.lower() in ["все", "всё"] else [s.strip() for s in text.split(",")]

    await state.update_data(stopovers=stopovers)
    await state.set_state(SearchWizard.waiting_for_stopover_preset)

    kb = ReplyKeyboardBuilder()
    kb.button(text="⚡ Быстро")
    kb.button(text="🚶 Погулять")
    kb.button(text="🌍 Мини-путешествие")
    kb.button(text="💰 Только цена")
    kb.button(text="⚖️ Баланс")
    kb.adjust(2, 2, 1)

    await message.answer(
        "⏱️ *Шаг 9 из 11:* Режим стоповеров\n\n"
        "⚡ *Быстро* — пересадки до 1 дня, без длинных ожиданий\n"
        "🚶 *Погулять* — остановки 1-3 дня, погулять по городу\n"
        "🌍 *Мини-путешествие* — остановки 2-5 дней, полноценный визит\n"
        "💰 *Только цена* — любые пересадки, главное дешево\n"
        "⚖️ *Баланс* — умеренный режим (рекомендуем)",
        reply_markup=kb.as_markup(resize_keyboard=True, one_time_keyboard=True)
    )

@router.message(SearchWizard.waiting_for_stopover_preset)
async def process_stopover_preset(message: types.Message, state: FSMContext):
    text = message.text.strip().lower()
    preset_map = {
        "быстро": "fast", "⚡": "fast", "⚡ быстро": "fast",
        "погулять": "walk", "🚶": "walk", "🚶 погулять": "walk",
        "мини-путешествие": "mini_trip", "мини": "mini_trip", "🌍": "mini_trip", "🌍 мини-путешествие": "mini_trip",
        "только цена": "price_only", "цена": "price_only", "💰": "price_only", "💰 только цена": "price_only",
        "баланс": "balanced", "⚖️": "balanced", "⚖️ баланс": "balanced",
    }
    stopover_preset = preset_map.get(text, "balanced")
    stopover_settings = stopover_settings_for_preset(stopover_preset)
    await state.update_data(
        stopover_preset=stopover_preset,
        min_stopover_hours=stopover_settings["min_stopover_hours"],
        max_stopover_days=stopover_settings["max_stopover_days"],
        allow_awkward_layovers=stopover_settings["allow_awkward_layovers"],
    )

    await state.set_state(SearchWizard.waiting_for_visa_mode)
    kb = ReplyKeyboardBuilder()
    kb.button(text="✅ Только безвизовые")
    kb.button(text="⚠️ Предупреждать")
    kb.button(text="🙈 Не фильтровать")
    kb.adjust(1)
    await message.answer(
        "🛂 *Шаг 10 из 11:* Визовый режим для транзитных stopover-стран\n\n"
        "✅ *Только безвизовые* — исключать известные визовые хабы для РФ\n"
        "⚠️ *Предупреждать* — не исключать, но показывать риск\n"
        "🙈 *Не фильтровать* — оставить визы на ручную проверку",
        reply_markup=kb.as_markup(resize_keyboard=True, one_time_keyboard=True)
    )

@router.message(SearchWizard.waiting_for_visa_mode)
async def process_visa_mode(message: types.Message, state: FSMContext):
    text = message.text.strip().lower()
    if "предуп" in text:
        visa_mode = "warn"
    elif "не фильтр" in text or "игнор" in text:
        visa_mode = "ignore"
    else:
        visa_mode = "visa_free_only"
    await state.update_data(visa_mode=visa_mode)

    await state.set_state(SearchWizard.waiting_for_exclusions)
    await message.answer(
        "🛡️ *Шаг 11 из 11:* Исключения\n"
        "Какие страны/города транзита нужно точно исключить (например: *Стамбул, Дубай*)?\n"
        "Отправьте *Нет*, если ничего исключать не нужно:",
        reply_markup=types.ReplyKeyboardRemove()
    )

@router.message(SearchWizard.waiting_for_exclusions)
async def process_exclusions(message: types.Message, state: FSMContext):
    text = message.text.strip()
    exclusions = [] if text.lower() in ["нет", "не надо"] else [s.strip() for s in text.split(",")]

    # Finalize search wizard
    data = await state.get_data()
    await state.clear()

    # Save search preferences in DB (DOP-1 / async-safe thread)
    search_id = await asyncio.to_thread(
        save_user_search,
        user_id=message.from_user.id,
        origin_iata=data["origin_iata"],
        destination_text=data["dest_iata"],
        date_start=data["date_start"],
        date_end=data["date_end"],
        max_transfers=data["max_transfers"],
        visa_allowed=1,
        lodging_exceptions={},
        max_budget=data["max_budget"],
        stopovers=data["stopovers"],
        exclusions=exclusions,
        baggage_needed=data["baggage_needed"],
        stopover_preset=data.get("stopover_preset", "balanced"),
        min_stopover_hours=data.get("min_stopover_hours", 0),
        max_stopover_days=data.get("max_stopover_days", 5),
        allow_awkward_layovers=data.get("allow_awkward_layovers", 1),
        visa_mode=data.get("visa_mode", "visa_free_only"),
        price_drop_threshold_pct=data.get("price_drop_threshold_pct", DEFAULT_PRICE_DROP_THRESHOLD_PCT),
    )

    stopover_summary = format_stopover_settings({
        "stopover_preset": data.get("stopover_preset", "balanced"),
        "min_stopover_hours": data.get("min_stopover_hours", 0),
        "max_stopover_days": data.get("max_stopover_days", 5),
        "allow_awkward_layovers": data.get("allow_awkward_layovers", 1),
    })

    await message.answer(
        "🎉 **Мониторинг успешно настроен!**\n\n"
        f"🆔 ID поиска: `{search_id}`\n"
        f"📍 Маршрут: `{data['origin_name']} ({data['origin_iata']})` ➔ `{data['dest_name']} ({data['dest_iata']})`\n"
        f"📅 Период: {data['date_start']} — {data['date_end']}\n"
        f"💰 Бюджет: до {data['max_budget']:,} ₽\n"
        f"🔔 Алерт при падении: {format_price_drop_threshold(data.get('price_drop_threshold_pct'))}\n"
        f"🎒 Багаж: {'Да' if data['baggage_needed'] else 'Нет'}\n"
        f"✈️ Макс. перелетов: {data['max_transfers'] + 1}\n\n"
        f"⏱️ Стоповеры: {stopover_summary}\n\n"
        f"🛂 Визы: {format_visa_mode(data.get('visa_mode', 'visa_free_only'))}\n\n"
        "🤖 Я запускаю первый фоновый расчет билетов через API и нейросеть. Это займет около 30-60 секунд..."
    )

    # Trigger immediate calculation
    immediate_config = {
        "search_id": search_id,
        "origin_iata": data["origin_iata"],
        "dest_iata": data["dest_iata"],
        "date_start": data["date_start"],
        "date_end": data["date_end"],
        "max_budget": data["max_budget"],
        "max_transfers": data["max_transfers"],
        "baggage_needed": data["baggage_needed"],
        "stopovers": data["stopovers"],
        "exclusions": exclusions,
        "stopover_preset": data.get("stopover_preset", "balanced"),
        "min_stopover_hours": data.get("min_stopover_hours", 0),
        "max_stopover_days": data.get("max_stopover_days", 5),
        "allow_awkward_layovers": data.get("allow_awkward_layovers", 1),
        "visa_mode": data.get("visa_mode", "visa_free_only"),
        "price_drop_threshold_pct": data.get("price_drop_threshold_pct", DEFAULT_PRICE_DROP_THRESHOLD_PCT),
    }
    asyncio.create_task(run_search_and_update_baseline(message.from_user.id, message.chat.id, immediate_config))

@router.message(Command("my_searches", "mysearches"))
async def cmd_my_searches(message: types.Message):
    searches = await asyncio.to_thread(get_user_searches, message.from_user.id)
    if not searches:
        await message.answer("📋 У вас пока нет настроенных активных поисков. Используйте /new_search для добавления!")
        return

    text = ["📋 **Ваши активные мониторинги:**\n"]
    for s in searches:
        stopovers = json.loads(s.get("stopovers_json") or "[]")
        exclusions = json.loads(s.get("exclusions_json") or "[]")
        stopover_text = format_stopover_settings(s)
        visa_text = format_visa_mode(s.get("visa_mode", "visa_free_only"))
        stopovers_text = ", ".join(stopovers) if stopovers else "любые"
        exclusions_text = ", ".join(exclusions) if exclusions else "нет"
        last_price = float(s.get("last_checked_price") or 0)
        last_price_text = f"{last_price:,.0f} ₽".replace(",", " ") if last_price > 0 else "пока нет"
        last_checked_text = s.get("last_checked_at") or "не проверялось"
        text.append(
            f"🆔 ID: {s['id']}\n"
            f"✈️ Маршрут: `{s['origin_iata']}` ➔ `{s['destination_text']}`\n"
            f"📅 Даты: {s['date_start']} — {s['date_end']}\n"
            f"💰 Бюджет: до {s['max_budget']:,} ₽\n"
            f"🔔 Алерт: падение от {format_price_drop_threshold(s.get('price_drop_threshold_pct'))}\n"
            f"📉 Baseline: {last_price_text} ({last_checked_text})\n"
            f"🎒 Багаж: {'Да' if s.get('baggage_needed') else 'Нет'}\n"
            f"🧭 Макс. перелетов: {int(s.get('max_transfers', 2)) + 1}\n"
            f"⏱️ Стоповеры: {stopover_text}\n"
            f"🛂 Визы: {visa_text}\n"
            f"🏙️ Города стоповера: {stopovers_text}\n"
            f"🛡️ Исключения: {exclusions_text}\n"
            f"🔔 Порог: /set_price_alert {s['id']} 10\n"
            f"🔄 Обновить: /refresh_search {s['id']}\n"
            f"📊 Кэш: /cache_status {s['id']}\n"
            f"Удалить: /del_{s['id']}\n"
            "---"
        )
    await message.answer("\n".join(text), parse_mode="Markdown")

@router.message(Command("set_price_alert"))
async def cmd_set_price_alert(message: types.Message):
    args = (message.text or "").split()
    if len(args) < 3:
        options = ", ".join(f"{pct}%" for pct in PRICE_DROP_THRESHOLD_OPTIONS)
        await message.answer(
            "Использование: `/set_price_alert <ID поиска> <процент>`\n"
            f"Например: `/set_price_alert 12 8`\n"
            f"Удобные варианты: {options}"
        )
        return

    try:
        search_id = int(args[1])
    except ValueError:
        await message.answer("❌ Неверный ID поиска.")
        return

    threshold = parse_price_drop_threshold(args[2])
    if threshold is None:
        await message.answer("❌ Укажите процент числом, например 5, 8, 10, 15 или 20.")
        return

    updated = await asyncio.to_thread(update_price_drop_threshold, search_id, message.from_user.id, threshold)
    if not updated:
        await message.answer("Не нашел активный поиск с таким ID.")
        return

    await message.answer(
        f"✅ Порог алерта для поиска ID {search_id} обновлен: падение от {format_price_drop_threshold(threshold)}."
    )

@router.message(lambda msg: msg.text and msg.text.startswith("/del_"))
async def cmd_delete_search(message: types.Message):
    try:
        search_id = int(message.text.split("_")[1])
        await asyncio.to_thread(delete_user_search, search_id, message.from_user.id)
        await message.answer(f"✅ Мониторинг ID {search_id} успешно удален!")
    except Exception as e:
        await message.answer("❌ Неверный ID поиска. Попробуйте еще раз.")

@router.message(Command("refresh_search"))
async def cmd_refresh_search(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: `/refresh_search <ID поиска>`")
        return
    try:
        search_id = int(args[1])
    except ValueError:
        await message.answer("Неверный формат ID.")
        return

    # Check search ownership
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM user_searches WHERE id = ? AND user_id = ?", (search_id, message.from_user.id))
    search = cursor.fetchone()
    conn.close()
    if not search:
        await message.answer("Поиск с таким ID не найден или принадлежит не вам.")
        return

    search = dict(search)

    immediate_config = {
        "search_id": search_id,
        "origin_iata": search["origin_iata"],
        "dest_iata": search["destination_text"],
        "date_start": search["date_start"],
        "date_end": search["date_end"],
        "max_budget": search["max_budget"],
        "max_transfers": search.get("max_transfers", 2),
        "baggage_needed": search.get("baggage_needed", 0),
        "stopovers": json.loads(search.get("stopovers_json") or "[]"),
        "exclusions": json.loads(search.get("exclusions_json") or "[]"),
        "stopover_preset": search.get("stopover_preset", "balanced"),
        "min_stopover_hours": search.get("min_stopover_hours", 0),
        "max_stopover_days": search.get("max_stopover_days", 5),
        "allow_awkward_layovers": search.get("allow_awkward_layovers", 1),
        "visa_mode": search.get("visa_mode", "visa_free_only"),
        "price_drop_threshold_pct": search.get("price_drop_threshold_pct", DEFAULT_PRICE_DROP_THRESHOLD_PCT),
        "cache_mode": "fresh"  # Force bypass cache
    }

    await message.answer(f"🔄 Запущен принудительный перерасчет поиска ID {search_id} в режиме реального времени (без кэша)...")
    asyncio.create_task(run_search_and_update_baseline(message.from_user.id, message.chat.id, immediate_config))

@router.message(Command("cache_status"))
async def cmd_cache_status(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: `/cache_status <ID поиска>`")
        return
    try:
        search_id = int(args[1])
    except ValueError:
        await message.answer("Неверный формат ID.")
        return

    # Check search ownership
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM user_searches WHERE id = ? AND user_id = ?", (search_id, message.from_user.id))
    search = cursor.fetchone()
    conn.close()
    if not search:
        await message.answer("Поиск с таким ID не найден или принадлежит не вам.")
        return

    search = dict(search)
    dest_iatas = await resolve_destination_airports(search["destination_text"])
    edges, months, edge_source = await _candidate_edges_for_saved_search(message.from_user.id, search, dest_iatas)
    status = await asyncio.to_thread(get_cache_status_for_search, search_id, dest_iatas, edges, months)

    if "error" in status:
        await message.answer(f"Ошибка получения статуса кэша: {status['error']}")
        return

    msg = (
        f"📊 *Статус кэша цен для поиска ID {search_id}*\n"
        f"📍 Маршрут: `{search['origin_iata']}` ➔ `{search['destination_text']}`\n"
        f"📅 Период: {search['date_start']} — {search['date_end']}\n"
        f"🧭 Источник ребер: `{edge_source}` ({len(edges)} сегм., {len(months)} мес.)\n"
        f"📦 Всего записей в кэше: `{status['total_cached']}`\n"
        f"⏳ Из них устарело (>24 ч): `{status['stale_count']}`\n"
    )
    if status['newest_fetched_at']:
        msg += f"🆕 Последнее обновление кэша: `{status['newest_fetched_at']}`\n"
    if status['oldest_fetched_at']:
        msg += f"⏳ Старейшая запись кэша: `{status['oldest_fetched_at']}`\n"

    await message.answer(msg, parse_mode="Markdown")

@router.message(Command("clear_cache_search"))
async def cmd_clear_cache_search(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: `/clear_cache_search <ID поиска>`")
        return
    try:
        search_id = int(args[1])
    except ValueError:
        await message.answer("Неверный формат ID.")
        return

    # Check search ownership
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM user_searches WHERE id = ? AND user_id = ?", (search_id, message.from_user.id))
    search = cursor.fetchone()
    conn.close()
    if not search:
        await message.answer("Поиск с таким ID не найден или принадлежит не вам.")
        return

    search = dict(search)
    dest_iatas = await resolve_destination_airports(search["destination_text"])
    edges, months, edge_source = await _candidate_edges_for_saved_search(message.from_user.id, search, dest_iatas)

    await asyncio.to_thread(clear_price_cache_for_edges, edges, months)
    await message.answer(
        f"🧹 Кэш цен для поиска ID {search_id} очищен.\n"
        f"Источник ребер: `{edge_source}` ({len(edges)} сегм., {len(months)} мес.).\n"
        "Следующий поиск загрузит свежие цены из API."
    )

@router.message(Command("route"))
async def cmd_route_details(message: types.Message):
    route_id, search_id = _parse_route_command_args(message.text or "")
    if not route_id:
        await message.answer("Укажите route_id: `/route R-ABC123`\nМожно уточнить поиск: `/route R-ABC123 12`")
        return

    row = await asyncio.to_thread(get_route_snapshot, message.from_user.id, route_id, search_id)
    if not row:
        await message.answer("Не нашел такой маршрут в ваших последних результатах. Запустите поиск заново или проверьте route_id.")
        return

    metadata = json.loads(row.get("metadata_json") or "{}")
    text = await _render_snapshot_routes([row], metadata)
    await send_message_safely(message.chat.id, "📌 *Детали маршрута*\n\n" + text)

@router.message(Command("refresh_route"))
async def cmd_refresh_route(message: types.Message):
    route_id, search_id = _parse_route_command_args(message.text or "")
    if not route_id:
        await message.answer("Укажите route_id: `/refresh_route R-ABC123`\nМожно уточнить поиск: `/refresh_route R-ABC123 12`")
        return

    row = await asyncio.to_thread(get_route_snapshot, message.from_user.id, route_id, search_id)
    if not row:
        await message.answer("Не нашел такой маршрут в ваших последних результатах. Проверьте route_id или запустите поиск заново.")
        return

    route = json.loads(row["route_json"])
    refreshed_segments = []
    refresh_warnings = []
    status_msg = await message.answer(f"🔄 Обновляю цены по маршруту `{route_id}`...")

    for leg in route.get("segments", []):
        if leg.get("is_manual"):
            refreshed_segments.append(leg)
            continue

        date = leg.get("depart_date", leg.get("departure_at", "")[:10])
        fresh_options = await provider.get_prices(
            leg["origin"],
            leg["destination"],
            date,
            direct_only=True,
            cache_mode="fresh"
        )
        same_day_options = [item for item in fresh_options if item.get("depart_date") == date]
        options = same_day_options or fresh_options
        if not options:
            refreshed_segments.append(leg)
            refresh_warnings.append(f"{leg['origin']} -> {leg['destination']} на {date}: свежая цена не найдена, оставил старую.")
            continue

        best = sorted(options, key=lambda item: item.get("price", 0))[0]
        refreshed_segments.append(best)

    refreshed_route = dict(route)
    refreshed_route["segments"] = refreshed_segments
    refreshed_route["base_price"] = sum(float(leg.get("price", 0)) for leg in refreshed_segments)
    refreshed_route["total_price"] = refreshed_route["base_price"] + float(refreshed_route.get("lodging_price", 0))
    refreshed_route["badges"] = ["Свежая проверка"] + [b for b in route.get("badges", []) if b != "Свежая проверка"]
    if refresh_warnings:
        refreshed_route["risk_warnings"] = list(route.get("risk_warnings", [])) + refresh_warnings

    text = await analyst.analyze_routes(
        origin=row.get("origin_iata", ""),
        destination=row.get("destination_text", ""),
        date_range=f"{row.get('date_start', '')} — {row.get('date_end', '')}",
        max_budget=0,
        solved_data={
            "recommended": [refreshed_route],
            "is_fallback_active": False,
            "total_routes_after_filter": 1,
            "rendered_routes_count": 1
        },
        search_metadata=None
    )

    try:
        await status_msg.delete()
    except Exception:
        pass
    await send_message_safely(message.chat.id, "✅ *Маршрут обновлен точечно*\n\n" + text)

@router.message(Command("check_route", "check_segment"))
async def cmd_check_route(message: types.Message):
    args = (message.text or "").split()
    if len(args) < 4:
        await message.answer(
            "Использование: `/check_route UFA MOW 2026-06-10`\n"
            "Команда проверяет свежие цены прямого сегмента через API."
        )
        return

    origin = args[1].strip().upper()
    destination = args[2].strip().upper()
    depart_date = args[3].strip()
    try:
        datetime.strptime(depart_date, "%Y-%m-%d")
    except ValueError:
        await message.answer("❌ Дата должна быть в формате YYYY-MM-DD, например 2026-06-10.")
        return

    if len(origin) != 3 or len(destination) != 3 or not origin.isalpha() or not destination.isalpha():
        await message.answer("❌ Укажите IATA-коды из 3 букв, например `UFA MOW`.")
        return

    status_msg = await message.answer(f"🔎 Проверяю свежие цены `{origin}` -> `{destination}` на {depart_date}...")
    options = await provider.get_prices(origin, destination, depart_date, direct_only=True, cache_mode="fresh")
    same_day_options = [item for item in options if item.get("depart_date") == depart_date]
    options = sorted(same_day_options or options, key=lambda item: item.get("price", 0))[:5]

    try:
        await status_msg.delete()
    except Exception:
        pass

    if not options:
        await message.answer(
            f"Не нашел свежих прямых вариантов `{origin}` -> `{destination}` на {depart_date}.\n"
            "Если Aviasales показывает билет, возможно это чартер/агентский остаток, непрямой рейс или данные API еще не обновились."
        )
        return

    lines = [f"🔎 *Свежая проверка сегмента* `{origin}` -> `{destination}` на {depart_date}\n"]
    for index, option in enumerate(options, start=1):
        price = float(option.get("price", 0) or 0)
        departure_at = option.get("departure_at") or option.get("depart_date") or depart_date
        airline = option.get("airline") or "?"
        flight_number = option.get("flight_number") or ""
        duration_min = int(option.get("duration", 0) or 0)
        duration_text = f"{duration_min // 60} ч {duration_min % 60} мин" if duration_min else "время не указано"
        link = analyst.renderer.clean_text(
            f"https://www.aviasales.ru/search/{origin}{depart_date[8:10]}{depart_date[5:7]}{destination}1"
        )
        lines.append(
            f"{index}) {price:,.0f} ₽ | {departure_at} | {airline}{flight_number} | {duration_text}\n"
            f"{link}"
        )

    await send_message_safely(message.chat.id, "\n".join(lines))

@router.message(Command("more_routes", "more"))
async def cmd_more_routes(message: types.Message):
    search_id, offset, sort_mode = _parse_more_routes_args(message.text or "")
    snapshot, rows = await asyncio.to_thread(get_snapshot_routes, message.from_user.id, search_id, offset, 5, sort_mode)
    if not snapshot:
        await message.answer("Пока нет сохраненного результата. Сначала запустите /new_search.")
        return
    if not rows:
        await message.answer("Больше сохраненных маршрутов для этого поиска нет.")
        return

    metadata = json.loads(snapshot.get("metadata_json") or "{}")
    text = await _render_snapshot_routes(rows, metadata)
    next_offset = offset + len(rows)
    text += (
        f"\n\nПоказаны варианты с offset={offset}, сортировка: {sort_mode}."
        f"\nЕще: `/more_routes offset={next_offset} {sort_mode}`"
    )
    await send_message_safely(message.chat.id, text)

async def _send_sorted_routes(message: types.Message, sort_mode: str):
    snapshot, rows = await asyncio.to_thread(get_snapshot_routes, message.from_user.id, None, 0, 5, sort_mode)
    if not snapshot:
        await message.answer("Пока нет сохраненного результата. Сначала запустите /new_search.")
        return
    if not rows:
        await message.answer("В сохраненном результате нет маршрутов.")
        return
    metadata = json.loads(snapshot.get("metadata_json") or "{}")
    text = await _render_snapshot_routes(rows, metadata)
    text += f"\n\nСортировка: {sort_mode}. Еще: `/more_routes offset=5 {sort_mode}`"
    await send_message_safely(message.chat.id, text)

@router.message(Command("routes_by_price"))
async def cmd_routes_by_price(message: types.Message):
    await _send_sorted_routes(message, "price")

@router.message(Command("routes_by_duration"))
async def cmd_routes_by_duration(message: types.Message):
    await _send_sorted_routes(message, "duration")

@router.message(Command("routes_by_comfort"))
async def cmd_routes_by_comfort(message: types.Message):
    await _send_sorted_routes(message, "comfort")

@router.message(Command("routes_by_stopover"))
async def cmd_routes_by_stopover(message: types.Message):
    await _send_sorted_routes(message, "stopover")

# Logic execution & background task
async def run_single_search_and_send(user_id: int, chat_id: int, search_config: dict, is_monitor_job: bool = False):
    """Executes a single flight search cascade, path solver, LLM analyst, and sends result."""
    origin = search_config["origin_iata"]
    destination_country = search_config["dest_iata"]
    date_start = search_config["date_start"]
    date_end = search_config["date_end"]
    max_budget = search_config["max_budget"]
    max_transfers = search_config.get("max_transfers", 2)
    baggage_needed = search_config.get("baggage_needed", 0)
    stopovers_pref = search_config.get("stopovers", [])
    exclusions = search_config.get("exclusions", [])
    search_id = search_config.get("search_id")

    cache_mode = search_config.get("cache_mode", "overview")
    stopover_preset = search_config.get("stopover_preset", "balanced")
    min_stopover_hours = search_config.get("min_stopover_hours", 0)
    max_stopover_days = search_config.get("max_stopover_days", 5)
    allow_awkward_layovers = search_config.get("allow_awkward_layovers", 1)
    visa_mode = search_config.get("visa_mode", "visa_free_only")

    # Track provider failures for this run without resetting global limiter state.
    failed_requests_before = provider.rate_limiter.stats.get("failed_requests", 0)

    # Send initial status message to user (only if not a background monitoring job)
    status_msg = None
    if not is_monitor_job:
        try:
            status_msg = await bot.send_message(
                chat_id,
                "🔍 *Идет поиск авиабилетов...*\n\n"
                "1️⃣ [Шаг 1/3] Построение динамического графа пересадок (bidirectional discovery)..."
            )
        except Exception as e:
            logger.error(f"Failed to send status message: {e}")

    # Resolve country destination airport codes deterministically or via LLM (Codex B)
    destination_iatas = await resolve_destination_airports(destination_country)
    if not destination_iatas:
        logger.error(f"Could not resolve destination airports for {destination_country}")
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
        if not is_monitor_job:
            await send_message_safely(
                chat_id,
                "❌ Не удалось определить аэропорты страны назначения. "
                "Попробуйте указать конкретный IATA-код аэропорта/города или добавить страну в каталог."
            )
        return 0

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

    # Run dynamic bidirectional discovery, caching topology separately from prices.
    discovery_cache_hit = False
    candidate_edges = await asyncio.to_thread(
        get_discovery_cache,
        origin,
        destination_country,
        destination_iatas,
        months_list,
        max_transfers
    )
    if candidate_edges is not None:
        discovery_cache_hit = True
        logger.info(f"Discovery cache hit: {len(candidate_edges)} edges for {origin} -> {destination_country}")
    else:
        try:
            candidate_edges = await discovery_service.discover_candidate_edges(
                origin=origin,
                destination_country=destination_country,
                destination_iatas=destination_iatas,
                months=months_list,
                max_transfers=max_transfers
            )
            await asyncio.to_thread(
                save_discovery_cache,
                origin,
                destination_country,
                destination_iatas,
                months_list,
                max_transfers,
                candidate_edges
            )
        except Exception as e:
            logger.error(f"Error in discovery service: {e}")
            candidate_edges = set()

    # If no candidate edges were discovered, fallback to static hubs so we don't return zero flights
    if not candidate_edges:
        logger.warning("No dynamic candidates found. Falling back to default transit hubs...")
        hubs = await asyncio.to_thread(get_all_transit_hubs)
        candidate_edges = set()
        for hub in hubs:
            candidate_edges.add((origin, hub["iata"]))
            for dest in destination_iatas:
                candidate_edges.add((hub["iata"], dest))
        for dest in destination_iatas:
            candidate_edges.add((origin, dest))

    # Compile actual query tasks
    tasks = []
    for edge in candidate_edges:
        edge_from, edge_to = edge
        for m in months_list:
            # Enforce direct_only=True for clean cascade building
            tasks.append(provider.get_prices(edge_from, edge_to, m, direct_only=True, cache_mode=cache_mode))

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
    priced_flights = []

    for i in range(0, len(tasks), chunk_size):
        chunk = tasks[i:i+chunk_size]
        results = await asyncio.gather(*chunk)
        for res in results:
            if res:
                priced_flights.extend(res)

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

    # Level 2: Solve DAG & Scorer (Codex E: pass priced_flights in memory)
    solved_data = solver.solve(
        origin_iata=origin,
        destination_country_code=destination_country,
        destination_iatas=destination_iatas,
        date_start_str=date_start,
        date_end_str=date_end,
        priced_flights=priced_flights,
        max_transfers=max_transfers,
        visa_allowed=0 if visa_mode == "visa_free_only" else 1,
        lodging_exceptions=search_config.get("lodging_exceptions", {}),
        max_budget=max_budget,
        baggage_needed=baggage_needed,
        stopovers_pref=stopovers_pref,
        exclusions=exclusions,
        min_stopover_hours=min_stopover_hours,
        max_stopover_days=max_stopover_days,
        stopover_preset=stopover_preset,
        allow_awkward_layovers=allow_awkward_layovers,
        visa_mode=visa_mode
    )

    # Extract explored transit hubs from candidate edges
    explored_hubs = set()
    for edge in candidate_edges:
        u, v = edge
        if u != origin and u not in destination_iatas:
            explored_hubs.add(u)
        if v != origin and v not in destination_iatas:
            explored_hubs.add(v)

    # Build search metadata for transparency report
    metadata = {
        "hubs": list(explored_hubs),
        "segments_count": len(candidate_edges),
        "priced_segments_count": len(priced_flights),
        "total_routes_found": solved_data.get("total_routes_found_before_filter", 0),
        "total_routes_after_filter": solved_data.get("total_routes_after_filter", 0),
        "rendered_routes_count": solved_data.get("rendered_routes_count", 0),
        "discovery_cache_hit": discovery_cache_hit,
        "is_fallback_active": solved_data.get("is_fallback_active", False),
        "max_transfers": max_transfers,
        "destination_iatas": destination_iatas,
        "china_destinations": destination_iatas,
        "partial_data": provider.rate_limiter.stats.get("failed_requests", 0) > failed_requests_before,
        "visa_mode": visa_mode
    }

    try:
        snapshot_id = await asyncio.to_thread(
            save_search_snapshot,
            user_id,
            search_id,
            search_config,
            metadata,
            solved_data
        )
        metadata["snapshot_id"] = snapshot_id
        logger.info(f"Saved search snapshot {snapshot_id} for user {user_id}, search {search_id}")
    except Exception as e:
        logger.error(f"Failed to save search snapshot: {e}")

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
    if solved_data.get("recommended"):
        analysis_text += (
            "\n\n📎 *Команды по результату:*"
            "\n`/route R-XXXXXX` — детали маршрута"
            "\n`/refresh_route R-XXXXXX` — обновить цены этого маршрута"
            "\n`/check_route UFA MOW 2026-06-10` — свежая проверка сегмента"
            "\n`/more_routes` — следующие 5 вариантов"
            "\n`/routes_by_price` / `/routes_by_duration` — пересортировать сохраненный результат"
        )

    # Delete status message before sending final report
    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    # Send report
    try:
        cheapest_routes = solved_data.get("cheapest", [])
        best_price = cheapest_routes[0]["total_price"] if cheapest_routes else 0

        if is_monitor_job:
            decision = price_drop_alert_decision(
                last_price=search_config.get("last_checked_price", 0),
                current_price=best_price,
                threshold_pct=search_config.get("price_drop_threshold_pct", DEFAULT_PRICE_DROP_THRESHOLD_PCT),
                partial_data=metadata.get("partial_data", False),
            )
            if not decision["should_update_baseline"]:
                logger.info(
                    "Monitoring search ID %s skipped baseline update: %s",
                    search_config.get("search_id"),
                    decision["reason"],
                )
                return 0

            if not decision["should_alert"]:
                logger.info(
                    "Monitoring search ID %s found no alert-worthy drop: %.1f%% < %.1f%%",
                    search_config.get("search_id"),
                    decision["drop_pct"],
                    decision["threshold_pct"],
                )
                return best_price

            last_price = float(search_config.get("last_checked_price", 0) or 0)
            if last_price > 0:
                analysis_text = (
                    f"🔔 *Мониторинг цен: цена упала на {decision['drop_pct']:.1f}%*\n"
                    f"📉 Было: {last_price:,.0f} ₽\n"
                    f"🔥 Стало: {best_price:,.0f} ₽\n"
                    f"Порог уведомления: {decision['threshold_pct']:.1f}%\n\n"
                    + analysis_text
                )

        # Send safe and split message (DOP-4)
        await send_message_safely(chat_id, analysis_text)
        return best_price
    except Exception as e:
        logger.error(f"Failed to send telegram message: {e}")
        return 0

async def run_search_and_update_baseline(user_id: int, chat_id: int, search_config: dict):
    best_price = await run_single_search_and_send(user_id, chat_id, search_config, is_monitor_job=False)
    search_id = search_config.get("search_id")
    if search_id and best_price > 0:
        await asyncio.to_thread(update_last_checked_price, search_id, best_price)
    return best_price

# Scheduler job
async def run_daily_monitoring_job():
    logger.info("Starting scheduled flight monitoring job...")
    searches = await asyncio.to_thread(get_all_active_searches)
    for s in searches:
        logger.info(f"Processing monitoring search ID {s['id']} for user {s['user_id']}")
        config_data = {
            "search_id": s["id"],
            "last_checked_price": s["last_checked_price"],
            "price_drop_threshold_pct": s.get("price_drop_threshold_pct", DEFAULT_PRICE_DROP_THRESHOLD_PCT),
            "origin_iata": s["origin_iata"],
            "dest_iata": s["destination_text"],
            "date_start": s["date_start"],
            "date_end": s["date_end"],
            "max_budget": s["max_budget"],
            "max_transfers": s.get("max_transfers", 2),
            "lodging_exceptions": json.loads(s.get("lodging_exceptions_json", "{}")),
            "stopovers": json.loads(s.get("stopovers_json", "[]")),
            "exclusions": json.loads(s.get("exclusions_json", "[]")),
            "baggage_needed": s.get("baggage_needed", 0),
            "cache_mode": "monitor",
            "stopover_preset": s.get("stopover_preset", "balanced"),
            "min_stopover_hours": s.get("min_stopover_hours", 0),
            "max_stopover_days": s.get("max_stopover_days", 5),
            "allow_awkward_layovers": s.get("allow_awkward_layovers", 1),
            "visa_mode": s.get("visa_mode", "visa_free_only")
        }

        # Run search with monitor flag active (DOP-2)
        best_price = await run_single_search_and_send(s["user_id"], s["chat_id"], config_data, is_monitor_job=True)

        # Update last checked price (async-safe thread)
        if best_price > 0:
            await asyncio.to_thread(update_last_checked_price, s["id"], best_price)

        await asyncio.sleep(5.0) # Gap between users to reduce rate load

# Scheduler triggers configuration
def setup_scheduler():
    scheduler.add_job(run_daily_monitoring_job, 'cron', hour=10, minute=0)
    scheduler.start()
    logger.info("Scheduler setup complete. Daily job scheduled at 10:00.")

async def main():
    setup_scheduler()
    logger.info("Starting Telegram Bot long-polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    from db import init_db
    init_db()
    asyncio.run(main())
