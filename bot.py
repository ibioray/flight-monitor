import os
import re
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
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, OPENROUTER_API_KEY
from db import (
    register_user, save_user_search, get_user_searches, delete_user_search,
    get_all_active_searches, update_last_checked_price, get_all_transit_hubs,
    get_all_manual_legs, save_search_snapshot, get_route_snapshot,
    get_latest_search_snapshot, get_snapshot_routes, get_discovery_cache, save_discovery_cache,
    clear_price_cache_for_edges, get_cache_status_for_search,
    get_db_connection, update_price_drop_threshold,
    subscribe_route, get_user_route_subscriptions, get_all_active_route_subscriptions,
    update_route_subscription_baseline, deactivate_route_subscription
)
from providers import TravelpayoutsProvider
from solver import GraphSolver
from analyst import LLMCognitiveAnalyst
from discovery import RouteDiscoveryService
from airport_names import annotate_iata_codes, format_iata_city
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
        # min_stopover_hours is a HARD route filter in the solver (a route without a
        # stopover >= this is dropped). For "balanced" we must NOT force a long stopover,
        # otherwise fast routes with a normal 3-5h connection get deleted. 0 = allow both
        # short connections AND multi-day stopovers (capped by max_stopover_days).
        "min_stopover_hours": 0,
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

async def resolve_destination_airports(destination_text: str) -> list[str]:
    """Resolve a stored destination into target airport IATA codes.

    destination_text may be:
      - a comma-separated list of explicit airports, e.g. "PVG,SHA,PEK" (city-level target);
      - a single country code in the catalog, e.g. "CN";
      - a single explicit airport/city IATA, e.g. "PVG";
      - any other country code (LLM fallback).
    """
    text = (destination_text or "").strip().upper()

    # City-level target: explicit comma-separated airport list (Город-цель).
    if "," in text:
        codes = [c.strip() for c in text.split(",") if len(c.strip()) == 3 and c.strip().isalpha()]
        if codes:
            logger.info(f"Resolved explicit airport target list: {codes}")
            return codes

    if text in COUNTRY_AIRPORTS:
        logger.info(f"Resolved airports for {text} deterministically: {COUNTRY_AIRPORTS[text]}")
        return COUNTRY_AIRPORTS[text]

    # Single explicit IATA airport/city code.
    if len(text) == 3 and text.isalpha():
        logger.info(f"Treating {text} as an explicit airport/city IATA code.")
        return [text]

    # LLM fallback (treat as a country name/code).
    logger.info(f"Resolving airports for {text} dynamically via LLM...")
    resolved = await get_airports_for_country_with_llm(text)
    logger.info(f"LLM resolved airports for {text}: {resolved}")
    return resolved


# Deterministic catalog for the most common city-level targets, so city resolution does
# not fully depend on the LLM. Keys are lowercase RU/EN aliases.
CITY_AIRPORTS = {
    "шанхай": ["PVG", "SHA"], "shanghai": ["PVG", "SHA"],
    "пекин": ["PEK", "PKX"], "beijing": ["PEK", "PKX"],
    "гуанчжоу": ["CAN"], "guangzhou": ["CAN"],
    "шэньчжэнь": ["SZX"], "shenzhen": ["SZX"],
    "чэнду": ["CTU", "TFU"], "chengdu": ["CTU", "TFU"],
    "урумчи": ["URC"], "urumqi": ["URC"],
    "сиань": ["XIY"], "xian": ["XIY"], "xi'an": ["XIY"],
    "ханчжоу": ["HGH"], "hangzhou": ["HGH"],
    "санья": ["SYX"], "sanya": ["SYX"],
    "гонконг": ["HKG"], "hong kong": ["HKG"], "hongkong": ["HKG"],
    "бангкок": ["BKK", "DMK"], "bangkok": ["BKK", "DMK"],
    "пхукет": ["HKT"], "phuket": ["HKT"],
    "ханой": ["HAN"], "hanoi": ["HAN"],
    "хошимин": ["SGN"], "ho chi minh": ["SGN"], "сайгон": ["SGN"],
    "стамбул": ["IST", "SAW"], "istanbul": ["IST", "SAW"],
    "дубай": ["DXB"], "dubai": ["DXB"],
    "сеул": ["ICN", "GMP"], "seoul": ["ICN", "GMP"],
    "токио": ["HND", "NRT"], "tokyo": ["HND", "NRT"],
}


async def parse_destination_with_llm(text: str) -> dict:
    """Resolve free-text destination into either a country or specific city airports.

    Returns {"kind": "country"|"city", "iata_list": [...], "resolved_name": str}.
    Deterministic city/country catalogs are tried before the LLM (Город-цель).
    """
    raw = (text or "").strip()
    low = raw.lower()

    # 1. Deterministic city aliases (single or several joined by или/and/comma).
    matched_airports: list[str] = []
    matched_names: list[str] = []
    for token in re.split(r"\s+или\s+|\s+и\s+|\s+or\s+|\s+and\s+|[,/]", low):
        key = token.strip()
        if key in CITY_AIRPORTS:
            for code in CITY_AIRPORTS[key]:
                if code not in matched_airports:
                    matched_airports.append(code)
            matched_names.append(key)
    if matched_airports:
        return {"kind": "city", "iata_list": matched_airports, "resolved_name": raw}

    # 2. Explicit IATA list typed by the user (e.g. "PVG, PEK").
    explicit = [t.strip().upper() for t in re.split(r"[,/\s]+", raw) if len(t.strip()) == 3 and t.strip().isalpha()]
    if explicit and len(explicit) == len([t for t in re.split(r"[,/\s]+", raw) if t.strip()]):
        return {"kind": "city", "iata_list": explicit, "resolved_name": raw}

    # 3. LLM structured resolution.
    prompt = f"""
Определи пункт назначения из текста: '{raw}'.
Это либо СТРАНА (например, Китай, Таиланд), либо конкретный ГОРОД/города
(например, Шанхай, Пекин, "Шанхай или Пекин").
Верни СТРОГО JSON без пояснений:
{{"kind": "country" или "city", "country_code": "ISO alpha-2 или пусто", "iata_list": ["IATA", ...], "resolved_name": "название на русском"}}
Правила:
- Страна: country_code = ISO код (CN, TH, VN...), iata_list = [].
- Город/города: iata_list = 3-буквенные IATA всех крупных аэропортов названных городов
  (Шанхай -> ["PVG","SHA"], Пекин -> ["PEK","PKX"]). Несколько городов — включи все.
"""
    resp_text = await call_llm(prompt)
    if resp_text:
        try:
            cleaned = resp_text.strip().replace("```json", "").replace("```", "")
            parsed = json.loads(cleaned)
            kind = parsed.get("kind")
            iata_list = [
                str(c).upper().strip() for c in parsed.get("iata_list", [])
                if isinstance(c, str) and len(c.strip()) == 3 and c.strip().isalpha()
            ]
            if kind == "city" and iata_list:
                return {"kind": "city", "iata_list": iata_list, "resolved_name": parsed.get("resolved_name", raw)}
            country = str(parsed.get("country_code", "")).upper().strip()
            if country:
                return {"kind": "country", "iata_list": [country], "resolved_name": parsed.get("resolved_name", raw)}
        except Exception as e:
            logger.error(f"Failed to parse destination JSON: {e}. Raw: {resp_text}")

    # 4. Fallback: legacy country-code resolver.
    legacy = await parse_location_with_llm(raw, is_country=True)
    return {"kind": "country", "iata_list": [legacy["iata"]], "resolved_name": legacy.get("resolved_name", raw)}

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

async def send_message_safely(chat_id: int, text: str, reply_markup=None):
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

    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        try:
            await bot.send_message(chat_id, chunk, parse_mode="Markdown", disable_web_page_preview=True, reply_markup=markup)
        except Exception as e:
            logger.error(f"Markdown send failed: {e}. Falling back to plain text.")
            try:
                # Fallback without parse_mode
                await bot.send_message(chat_id, chunk, disable_web_page_preview=True, reply_markup=markup)
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

def _callback_search_id(value: str | int | None) -> int | None:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return None
    return parsed or None

def result_actions_keyboard(search_id: int | None, routes: list[dict] | None = None, offset: int = 5, sort_mode: str = "balanced"):
    builder = InlineKeyboardBuilder()
    sid = int(search_id or 0)
    builder.button(text="Еще 5", callback_data=f"more:{sid}:{offset}:{sort_mode}")
    builder.button(text="По цене", callback_data=f"sort:{sid}:price")
    builder.button(text="По времени", callback_data=f"sort:{sid}:duration")
    builder.button(text="Диагностика", callback_data=f"diag:{sid}")
    for route in (routes or [])[:3]:
        route_id = route.get("route_id")
        if route_id:
            builder.button(text=f"{route_id}", callback_data=f"route:{sid}:{route_id}")
    builder.adjust(2, 2, 3)
    return builder.as_markup()

def route_actions_keyboard(search_id: int | None, route_id: str):
    builder = InlineKeyboardBuilder()
    sid = int(search_id or 0)
    builder.button(text="Подписаться на маршрут", callback_data=f"subroute:{sid}:{route_id}")
    builder.button(text="Обновить цены", callback_data=f"refresh_route:{sid}:{route_id}")
    builder.button(text="Почему так", callback_data=f"why_route:{sid}:{route_id}")
    builder.button(text="Еще 5", callback_data=f"more:{sid}:5:balanced")
    builder.adjust(1, 1, 2)
    return builder.as_markup()

def subscription_actions_keyboard(subscription_id: int, route_id: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="Обновить маршрут", callback_data=f"refresh_sub:{subscription_id}:{route_id}")
    builder.button(text="Отписаться", callback_data=f"unsubroute:{subscription_id}")
    builder.adjust(1, 1)
    return builder.as_markup()

def _route_line(route: dict) -> str:
    duration_hours = float(route.get("duration_hours") or 0)
    hours = int(duration_hours)
    minutes = int(round((duration_hours - hours) * 60))
    duration = f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    price = f"{float(route.get('total_price') or 0):,.0f}".replace(",", " ")
    return f"{route.get('route_id')} — {price} ₽, {duration}, сегментов: {route.get('segments_count', len(route.get('segments', [])))}"

def _diagnostic_reason_ru(reason: str) -> str:
    return {
        "not_in_top_5_diverse_summary": "не попал в первые 5 разнообразных вариантов",
    }.get(reason, reason or "причина не указана")

def render_search_diagnostics(snapshot: dict) -> str:
    metadata = json.loads(snapshot.get("metadata_json") or "{}")
    solved_data = json.loads(snapshot.get("solved_data_json") or "{}")
    omitted = solved_data.get("omitted_routes", [])[:10]
    raw_routes = metadata.get("total_routes_found", solved_data.get("total_routes_found_before_filter", 0))
    scored_routes = metadata.get("total_routes_scored", solved_data.get("total_routes_scored", 0))
    filtered_routes = metadata.get("total_routes_after_filter", solved_data.get("total_routes_after_filter", 0))
    rendered = metadata.get("rendered_routes_count", solved_data.get("rendered_routes_count", 0))
    priced_edges = metadata.get("priced_edges_count")
    if priced_edges is None:
        priced_edges = "нет данных"
    unpriced_edges = metadata.get("unpriced_candidate_edges_count")
    if unpriced_edges is None:
        unpriced_edges = "нет данных"

    flags = []
    if metadata.get("partial_data"):
        flags.append("данные API неполные")
    if metadata.get("route_cap_hit") or solved_data.get("route_cap_hit"):
        flags.append("сработал лимит перебора маршрутов")
    if metadata.get("is_fallback_active") or solved_data.get("is_fallback_active"):
        flags.append("в бюджете не нашлось, показаны ближайшие")
    if not flags:
        flags.append("критичных флагов нет")

    lines = [
        "🧪 *Диагностика поиска*",
        f"Поиск: `{snapshot.get('origin_iata')}` -> `{snapshot.get('destination_text')}`",
        f"Даты: {snapshot.get('date_start')} — {snapshot.get('date_end')}",
        "",
        f"Discovery cache: {'да' if metadata.get('discovery_cache_hit') else 'нет'}",
        f"Кандидатных сегментов: {metadata.get('segments_count', 0)}",
        f"Сегментов с ценами: {metadata.get('priced_segments_count', 0)}",
        f"Уникальных priced-ребер: {priced_edges}",
        f"Кандидатных ребер без цены: {unpriced_edges}",
        "",
        f"Маршруты: raw DFS {raw_routes} -> scored {scored_routes} -> after filters {filtered_routes} -> shown {rendered}",
        "Флаги: " + ", ".join(flags),
    ]

    discovery_diag = metadata.get("discovery_diagnostics") or {}
    edge_categories = discovery_diag.get("edge_categories") or {}
    if discovery_diag:
        top_hubs = ", ".join(format_iata_city(hub) for hub in discovery_diag.get("selected_first_hubs", [])[:8])
        if not top_hubs:
            top_hubs = "нет"
        lines.extend([
            "",
            f"Discovery algorithm: {discovery_diag.get('algorithm', 'unknown')}",
            f"Forward hubs из точки старта: {discovery_diag.get('forward_hubs_count', 0)}",
            f"Backward hubs в страну: {discovery_diag.get('backward_hubs_count', 0)}",
            f"Хабов для 1 пересадки: {discovery_diag.get('one_transfer_hubs_count', 0)}",
            f"Первые хабы для bridge-поиска: {discovery_diag.get('selected_first_hubs_count', 0)} ({top_hubs})",
            "Категории ребер: "
            f"direct {edge_categories.get('direct_validation', 0)}, "
            f"origin->hub {edge_categories.get('one_transfer_origin', 0)}, "
            f"hub->target {edge_categories.get('target_side', edge_categories.get('one_transfer_target', 0))}, "
            f"bridge {edge_categories.get('bridge', 0)}, "
            f"bridge->target {edge_categories.get('bridge_to_target', 0)}",
        ])

    if omitted:
        lines.append("")
        lines.append("*Не показаны в первых 5:*")
        for item in omitted:
            lines.append(f"- {_route_line(item)}: {_diagnostic_reason_ru(item.get('reason'))}")
    else:
        lines.append("")
        lines.append("Не показанных сохраненных маршрутов нет.")

    return "\n".join(lines)

def render_route_diagnostics(route_row: dict, snapshot: dict | None = None) -> str:
    route = json.loads(route_row["route_json"])
    route_id = route.get("route_id")
    badges = ", ".join(route.get("badges") or []) or "нет"
    stopovers = route.get("stopovers") or []
    risks = [annotate_iata_codes(risk) for risk in route.get("risk_warnings", [])]
    omitted_reason = None
    if snapshot:
        solved_data = json.loads(snapshot.get("solved_data_json") or "{}")
        for item in solved_data.get("omitted_routes", []):
            if item.get("route_id") == route_id:
                omitted_reason = _diagnostic_reason_ru(item.get("reason"))
                break

    lines = [
        f"🧪 *Диагностика маршрута {route_id}*",
        f"Статус: {'показан в топе' if route.get('badges') else 'сохранен в результатах'}",
        f"Бейджи: {badges}",
        f"Цена: {float(route.get('total_price') or 0):,.0f} ₽".replace(",", " "),
        f"Билеты: {float(route.get('base_price') or 0):,.0f} ₽ | жилье: {float(route.get('lodging_price') or 0):,.0f} ₽".replace(",", " "),
        f"Время: {route.get('duration_hours', 0):.1f} ч",
        f"Risk score: {route.get('risk_score', 0)}",
    ]
    if omitted_reason:
        lines.append(f"Почему не в первых 5: {omitted_reason}.")
    if stopovers:
        lines.append("Стоповеры:")
        for stop in stopovers:
            city = format_iata_city(stop.get("city", ""))
            name = stop.get("name")
            label = name if name and name != stop.get("city") else city
            lines.append(f"- {label}: {stop.get('layover_hours', 0)} ч, {stop.get('layover_type')}")
    if risks:
        lines.append("Риски:")
        for risk in risks[:5]:
            lines.append(f"- {risk}")
    if route.get("estimated_timing"):
        lines.append("Тайминг частично расчетный: перед покупкой лучше обновить маршрут.")
    return "\n".join(lines)

def render_route_subscription(sub: dict) -> str:
    route = json.loads(sub.get("route_json") or "{}")
    price = float(sub.get("last_checked_price") or route.get("total_price") or 0)
    threshold = format_price_drop_threshold(sub.get("price_drop_threshold_pct"))
    checked = sub.get("last_checked_at") or "не проверялось"
    route_line = _route_line(route) if route else sub.get("route_id", "")
    return (
        f"🔔 *Подписка #{sub['id']}*\n"
        f"{route_line}\n"
        f"Маршрут: `{format_iata_city(sub.get('origin_iata'))}` -> `{sub.get('destination_text')}`\n"
        f"Порог: падение от {threshold}\n"
        f"Baseline: {price:,.0f} ₽ ({checked})".replace(",", " ")
    )

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
    await message.answer("🏙️ **Шаг 1 из 11:** Откуда летим?\nНапишите название города (например, Уфа) или его IATA-код (UFA):")

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
        f"🌏 **Шаг 2 из 11:** Куда летим?\n"
        f"Можно указать *страну* (например, Китай) — тогда проверю все крупные аэропорты,\n"
        f"либо *конкретный город или несколько* (например, *Шанхай* или *Шанхай или Пекин*) — "
        f"тогда ищу только в выбранные города:"
    )

@router.message(SearchWizard.waiting_for_destination)
async def process_destination(message: types.Message, state: FSMContext):
    dest_text = message.text.strip()
    status_msg = await message.answer("🔍 Распознаю направление...")
    resolved = await parse_destination_with_llm(dest_text)
    await status_msg.delete()

    if resolved["kind"] == "city":
        # City-level target: store explicit airport list, e.g. "PVG,SHA,PEK".
        dest_store = ",".join(resolved["iata_list"])
        kind_label = "Города"
    else:
        # Whole-country target: store the country code (legacy behaviour).
        dest_store = resolved["iata_list"][0]
        kind_label = "Страна"

    await state.update_data(
        dest_iata=dest_store,
        dest_name=resolved.get("resolved_name", dest_text),
        dest_kind=resolved["kind"],
    )
    await state.set_state(SearchWizard.waiting_for_dates)
    await message.answer(
        f"✅ {kind_label} назначения: **{resolved.get('resolved_name', dest_text)} ({dest_store})**\n\n"
        f"📅 **Шаг 3 из 11:** Когда летим?\nНапишите дату или диапазон человеческим языком (например, *середина июня*, *ближайшие 2 недели*, *с 15 по 30 июня 2026*):"
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
        f"💰 **Шаг 4 из 11:** Максимальный бюджет?\nВведите максимальную стоимость билетов в рублях (например, 50000, 50к, 20 тысяч):"
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
        "🛡️ *Шаг 11 из 11:* Фильтр городов пересадок\n\n"
        "Можно ограничить, через какие города строить пересадки. Это работает для любого "
        "числа пересадок (1, 2 или 3) — фильтр применяется к каждому транзитному городу.\n\n"
        "• *Исключить:* напишите города через запятую (например: *Стамбул, Дубай*)\n"
        "• *Только эти:* начните со слова `только` (например: *только Ташкент, Алматы, Ереван*) — "
        "тогда маршруты строятся пересадками лишь через эти города\n"
        "• *Нет* — без ограничений",
        reply_markup=types.ReplyKeyboardRemove()
    )

@router.message(SearchWizard.waiting_for_exclusions)
async def process_exclusions(message: types.Message, state: FSMContext):
    text = message.text.strip()
    low = text.lower()

    exclusions: list = []
    allowed_hubs: list = []
    if low in ["нет", "не надо", "-", "любые", "все", "всё"]:
        pass
    elif low.startswith("только") or low.startswith("whitelist") or low.startswith("вайтлист"):
        # Whitelist mode: route transit only through these hubs (applies to every hop).
        body = text.split(":", 1)[1] if ":" in text else re.sub(r"^(только|whitelist|вайтлист)\s*", "", text, flags=re.IGNORECASE)
        allowed_hubs = [s.strip() for s in body.split(",") if s.strip()]
    else:
        # Blacklist mode (optionally prefixed with "кроме").
        body = re.sub(r"^(кроме|исключить|except)\s*:?\s*", "", text, flags=re.IGNORECASE)
        exclusions = [s.strip() for s in body.split(",") if s.strip()]

    # Finalize search wizard
    data = await state.get_data()
    data["allowed_hubs"] = allowed_hubs
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
        allowed_hubs=allowed_hubs,
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
        "🎉 **Поиск сохранен и запущен!**\n\n"
        f"🆔 ID поиска: `{search_id}`\n"
        f"📍 Маршрут: `{data['origin_name']} ({data['origin_iata']})` ➔ `{data['dest_name']} ({data['dest_iata']})`\n"
        f"📅 Период: {data['date_start']} — {data['date_end']}\n"
        f"💰 Бюджет: до {data['max_budget']:,} ₽\n"
        f"🔔 Алерт при падении: {format_price_drop_threshold(data.get('price_drop_threshold_pct'))}\n"
        f"🎒 Багаж: {'Да' if data['baggage_needed'] else 'Нет'}\n"
        f"✈️ Макс. перелетов: {data['max_transfers'] + 1}\n\n"
        f"⏱️ Стоповеры: {stopover_summary}\n\n"
        f"🛂 Визы: {format_visa_mode(data.get('visa_mode', 'visa_free_only'))}\n\n"
        "🤖 Я запускаю расчет билетов через API и нейросеть. Это займет около 30-60 секунд.\n"
        "После выдачи выберите подходящие маршруты кнопкой *Подписаться на маршрут*."
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
        "allowed_hubs": allowed_hubs,
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
        await message.answer("📋 У вас пока нет сохраненных поисков. Используйте /new_search для добавления!")
        return

    text = ["📋 **Ваши сохраненные поиски:**\n"]
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
            f"🔔 Подписки: /my_route_alerts\n"
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

@router.message(Command("subscribe_route"))
async def cmd_subscribe_route(message: types.Message):
    route_id, search_id = _parse_route_command_args(message.text or "")
    if not route_id:
        await message.answer("Укажите route_id: `/subscribe_route R-ABC123`\nМожно уточнить поиск: `/subscribe_route R-ABC123 12`")
        return

    threshold = None
    parts = (message.text or "").split()
    if len(parts) >= 4:
        parsed_threshold = parse_price_drop_threshold(parts[3])
        if parsed_threshold is not None:
            threshold = parsed_threshold

    sub = await asyncio.to_thread(
        subscribe_route,
        message.from_user.id,
        message.chat.id,
        route_id,
        search_id,
        threshold
    )
    if not sub:
        await message.answer("Не нашел такой маршрут в ваших сохраненных результатах. Сначала откройте `/route R-...` или запустите поиск.")
        return

    await send_message_safely(
        message.chat.id,
        "✅ *Подписка на маршрут включена*\n\n" + render_route_subscription(sub),
        reply_markup=subscription_actions_keyboard(sub["id"], sub["route_id"])
    )

@router.message(Command("my_route_alerts", "my_alerts", "subscriptions"))
async def cmd_my_route_alerts(message: types.Message):
    subs = await asyncio.to_thread(get_user_route_subscriptions, message.from_user.id)
    if not subs:
        await message.answer(
            "🔔 У вас пока нет подписок на конкретные маршруты.\n"
            "Откройте маршрут кнопкой `R-...` и нажмите *Подписаться на маршрут*."
        )
        return

    for sub in subs:
        await send_message_safely(
            message.chat.id,
            render_route_subscription(sub),
            reply_markup=subscription_actions_keyboard(sub["id"], sub["route_id"])
        )

@router.message(Command("unsubscribe_route"))
async def cmd_unsubscribe_route(message: types.Message):
    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("Использование: `/unsubscribe_route <ID подписки>`")
        return
    try:
        subscription_id = int(args[1])
    except ValueError:
        await message.answer("❌ Неверный ID подписки.")
        return
    changed = await asyncio.to_thread(deactivate_route_subscription, subscription_id, message.from_user.id)
    await message.answer("✅ Подписка отключена." if changed else "Не нашел активную подписку с таким ID.")

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
        "allowed_hubs": json.loads(search.get("allowed_hubs_json") or "[]"),
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
    await send_message_safely(
        message.chat.id,
        "📌 *Детали маршрута*\n\n" + text,
        reply_markup=route_actions_keyboard(row.get("search_id"), route_id)
    )

@router.message(Command("diagnose_search"))
async def cmd_diagnose_search(message: types.Message):
    args = (message.text or "").split()
    search_id = None
    if len(args) >= 2:
        try:
            search_id = int(args[1])
        except ValueError:
            await message.answer("Неверный ID поиска. Пример: `/diagnose_search 12`")
            return

    snapshot = await asyncio.to_thread(get_latest_search_snapshot, message.from_user.id, search_id)
    if not snapshot:
        await message.answer("Пока нет сохраненного результата для диагностики. Запустите поиск или `/refresh_search <id>`.")
        return

    await send_message_safely(
        message.chat.id,
        render_search_diagnostics(snapshot),
        reply_markup=result_actions_keyboard(snapshot.get("search_id"), [], 5, "balanced")
    )

@router.message(Command("why_route", "diagnose_route"))
async def cmd_why_route(message: types.Message):
    route_id, search_id = _parse_route_command_args(message.text or "")
    if not route_id:
        await message.answer("Укажите route_id: `/why_route R-ABC123`\nМожно уточнить поиск: `/why_route R-ABC123 12`")
        return

    row = await asyncio.to_thread(get_route_snapshot, message.from_user.id, route_id, search_id)
    if not row:
        await message.answer("Не нашел такой маршрут в ваших сохраненных результатах.")
        return

    snapshot = await asyncio.to_thread(get_latest_search_snapshot, message.from_user.id, row.get("search_id"))
    await send_message_safely(
        message.chat.id,
        render_route_diagnostics(row, snapshot),
        reply_markup=route_actions_keyboard(row.get("search_id"), route_id)
    )

async def refresh_route_snapshot(chat_id: int, row: dict, route_id: str):
    route = json.loads(row["route_json"])
    refreshed_segments = []
    refresh_warnings = []
    status_msg = await bot.send_message(chat_id, f"🔄 Обновляю цены по маршруту `{route_id}`...")

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
    await send_message_safely(
        chat_id,
        "✅ *Маршрут обновлен точечно*\n\n" + text,
        reply_markup=route_actions_keyboard(row.get("search_id"), route_id)
    )

async def refresh_route_data(route: dict, cache_mode: str = "fresh") -> tuple[dict, bool, list[str]]:
    refreshed_segments = []
    refresh_warnings = []
    partial_data = False

    for leg in route.get("segments", []):
        if leg.get("is_manual"):
            refreshed_segments.append(leg)
            continue

        date = leg.get("depart_date", leg.get("departure_at", "")[:10])
        failed_before = provider.rate_limiter.stats.get("failed_requests", 0)
        fresh_options = await provider.get_prices(
            leg["origin"],
            leg["destination"],
            date,
            direct_only=True,
            cache_mode=cache_mode
        )
        if provider.rate_limiter.stats.get("failed_requests", 0) > failed_before:
            partial_data = True

        same_day_options = [item for item in fresh_options if item.get("depart_date") == date]
        options = same_day_options or fresh_options
        if not options:
            refreshed_segments.append(leg)
            refresh_warnings.append(f"{leg['origin']} -> {leg['destination']} на {date}: свежая цена не найдена, оставил старую.")
            partial_data = True
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
    return refreshed_route, partial_data, refresh_warnings

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

    await refresh_route_snapshot(message.chat.id, row, route_id)
    return

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

    status_msg = await message.answer(f"🔎 Проверяю свежие цены `{format_iata_city(origin)}` -> `{format_iata_city(destination)}` на {depart_date}...")
    options = await provider.get_prices(origin, destination, depart_date, direct_only=True, cache_mode="fresh")
    same_day_options = [item for item in options if item.get("depart_date") == depart_date]
    options = sorted(same_day_options or options, key=lambda item: item.get("price", 0))[:5]

    try:
        await status_msg.delete()
    except Exception:
        pass

    if not options:
        await message.answer(
            f"Не нашел свежих прямых вариантов `{format_iata_city(origin)}` -> `{format_iata_city(destination)}` на {depart_date}.\n"
            "Если Aviasales показывает билет, возможно это чартер/агентский остаток, непрямой рейс или данные API еще не обновились."
        )
        return

    lines = [f"🔎 *Свежая проверка сегмента* `{format_iata_city(origin)}` -> `{format_iata_city(destination)}` на {depart_date}\n"]
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
    routes = [json.loads(row["route_json"]) for row in rows]
    await send_message_safely(
        message.chat.id,
        text,
        reply_markup=result_actions_keyboard(snapshot.get("search_id"), routes, next_offset, sort_mode)
    )

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
    routes = [json.loads(row["route_json"]) for row in rows]
    await send_message_safely(
        message.chat.id,
        text,
        reply_markup=result_actions_keyboard(snapshot.get("search_id"), routes, 5, sort_mode)
    )

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

@router.callback_query(lambda callback: callback.data and callback.data.startswith("more:"))
async def cb_more_routes(callback: types.CallbackQuery):
    _, sid_raw, offset_raw, sort_mode = callback.data.split(":", 3)
    search_id = _callback_search_id(sid_raw)
    try:
        offset = max(0, int(offset_raw))
    except ValueError:
        offset = 5
    snapshot, rows = await asyncio.to_thread(get_snapshot_routes, callback.from_user.id, search_id, offset, 5, sort_mode)
    if not snapshot or not rows:
        await callback.answer("Больше маршрутов нет", show_alert=False)
        return
    metadata = json.loads(snapshot.get("metadata_json") or "{}")
    text = await _render_snapshot_routes(rows, metadata)
    next_offset = offset + len(rows)
    routes = [json.loads(row["route_json"]) for row in rows]
    await callback.answer()
    await send_message_safely(
        callback.message.chat.id,
        text,
        reply_markup=result_actions_keyboard(snapshot.get("search_id"), routes, next_offset, sort_mode)
    )

@router.callback_query(lambda callback: callback.data and callback.data.startswith("sort:"))
async def cb_sort_routes(callback: types.CallbackQuery):
    _, sid_raw, sort_mode = callback.data.split(":", 2)
    search_id = _callback_search_id(sid_raw)
    snapshot, rows = await asyncio.to_thread(get_snapshot_routes, callback.from_user.id, search_id, 0, 5, sort_mode)
    if not snapshot or not rows:
        await callback.answer("Нет сохраненных маршрутов", show_alert=False)
        return
    metadata = json.loads(snapshot.get("metadata_json") or "{}")
    text = await _render_snapshot_routes(rows, metadata)
    routes = [json.loads(row["route_json"]) for row in rows]
    await callback.answer()
    await send_message_safely(
        callback.message.chat.id,
        text,
        reply_markup=result_actions_keyboard(snapshot.get("search_id"), routes, 5, sort_mode)
    )

@router.callback_query(lambda callback: callback.data and callback.data.startswith("route:"))
async def cb_route_details(callback: types.CallbackQuery):
    _, sid_raw, route_id = callback.data.split(":", 2)
    search_id = _callback_search_id(sid_raw)
    row = await asyncio.to_thread(get_route_snapshot, callback.from_user.id, route_id, search_id)
    if not row:
        await callback.answer("Маршрут не найден", show_alert=True)
        return
    metadata = json.loads(row.get("metadata_json") or "{}")
    text = await _render_snapshot_routes([row], metadata)
    await callback.answer()
    await send_message_safely(
        callback.message.chat.id,
        "📌 *Детали маршрута*\n\n" + text,
        reply_markup=route_actions_keyboard(row.get("search_id"), route_id)
    )

@router.callback_query(lambda callback: callback.data and callback.data.startswith("diag:"))
async def cb_diagnose_search(callback: types.CallbackQuery):
    _, sid_raw = callback.data.split(":", 1)
    search_id = _callback_search_id(sid_raw)
    snapshot = await asyncio.to_thread(get_latest_search_snapshot, callback.from_user.id, search_id)
    if not snapshot:
        await callback.answer("Диагностика пока недоступна", show_alert=True)
        return
    await callback.answer()
    await send_message_safely(
        callback.message.chat.id,
        render_search_diagnostics(snapshot),
        reply_markup=result_actions_keyboard(snapshot.get("search_id"), [], 5, "balanced")
    )

@router.callback_query(lambda callback: callback.data and callback.data.startswith("why_route:"))
async def cb_why_route(callback: types.CallbackQuery):
    _, sid_raw, route_id = callback.data.split(":", 2)
    search_id = _callback_search_id(sid_raw)
    row = await asyncio.to_thread(get_route_snapshot, callback.from_user.id, route_id, search_id)
    if not row:
        await callback.answer("Маршрут не найден", show_alert=True)
        return
    snapshot = await asyncio.to_thread(get_latest_search_snapshot, callback.from_user.id, row.get("search_id"))
    await callback.answer()
    await send_message_safely(
        callback.message.chat.id,
        render_route_diagnostics(row, snapshot),
        reply_markup=route_actions_keyboard(row.get("search_id"), route_id)
    )

@router.callback_query(lambda callback: callback.data and callback.data.startswith("refresh_route:"))
async def cb_refresh_route(callback: types.CallbackQuery):
    _, sid_raw, route_id = callback.data.split(":", 2)
    search_id = _callback_search_id(sid_raw)
    row = await asyncio.to_thread(get_route_snapshot, callback.from_user.id, route_id, search_id)
    if not row:
        await callback.answer("Маршрут не найден", show_alert=True)
        return
    await callback.answer("Обновляю маршрут")
    await refresh_route_snapshot(callback.message.chat.id, row, route_id)

@router.callback_query(lambda callback: callback.data and callback.data.startswith("subroute:"))
async def cb_subscribe_route(callback: types.CallbackQuery):
    _, sid_raw, route_id = callback.data.split(":", 2)
    search_id = _callback_search_id(sid_raw)
    sub = await asyncio.to_thread(
        subscribe_route,
        callback.from_user.id,
        callback.message.chat.id,
        route_id,
        search_id,
        None
    )
    if not sub:
        await callback.answer("Маршрут не найден", show_alert=True)
        return
    await callback.answer("Подписка включена", show_alert=False)
    await send_message_safely(
        callback.message.chat.id,
        "✅ *Подписка на маршрут включена*\n\n" + render_route_subscription(sub),
        reply_markup=subscription_actions_keyboard(sub["id"], sub["route_id"])
    )

@router.callback_query(lambda callback: callback.data and callback.data.startswith("unsubroute:"))
async def cb_unsubscribe_route(callback: types.CallbackQuery):
    _, sub_id_raw = callback.data.split(":", 1)
    try:
        subscription_id = int(sub_id_raw)
    except ValueError:
        await callback.answer("Неверный ID", show_alert=True)
        return
    changed = await asyncio.to_thread(deactivate_route_subscription, subscription_id, callback.from_user.id)
    await callback.answer("Подписка отключена" if changed else "Подписка не найдена", show_alert=False)

@router.callback_query(lambda callback: callback.data and callback.data.startswith("refresh_sub:"))
async def cb_refresh_subscription(callback: types.CallbackQuery):
    _, sub_id_raw, route_id = callback.data.split(":", 2)
    try:
        subscription_id = int(sub_id_raw)
    except ValueError:
        await callback.answer("Неверный ID", show_alert=True)
        return
    subs = await asyncio.to_thread(get_user_route_subscriptions, callback.from_user.id)
    sub = next((item for item in subs if int(item["id"]) == subscription_id), None)
    if not sub:
        await callback.answer("Подписка не найдена", show_alert=True)
        return
    row = {
        "route_json": sub["route_json"],
        "route_id": route_id,
        "search_id": sub.get("search_id"),
        "origin_iata": sub.get("origin_iata", ""),
        "destination_text": sub.get("destination_text", ""),
        "date_start": sub.get("date_start", ""),
        "date_end": sub.get("date_end", ""),
    }
    await callback.answer("Обновляю маршрут")
    await refresh_route_snapshot(callback.message.chat.id, row, route_id)

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
    allowed_hubs = search_config.get("allowed_hubs", [])
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
    discovery_diagnostics = {}
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
            candidate_edges, discovery_diagnostics = await discovery_service.discover_candidate_edges_with_diagnostics(
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

    # Connecting-fare lookup (Стыковочные тарифы): besides self-assembled cascades of
    # separate direct tickets, also fetch the real cheapest itineraries sold as a SINGLE
    # ticket for origin -> each target airport (this is what Aviasales shows for "1 stop").
    # These give the user a true bookable cheapest option alongside the cascades.
    connect_tasks = [
        provider.get_prices(origin, dest, m, direct_only=False, cache_mode=cache_mode)
        for dest in destination_iatas
        for m in months_list
    ]
    connecting_count = 0
    for i in range(0, len(connect_tasks), chunk_size):
        results = await asyncio.gather(*connect_tasks[i:i + chunk_size])
        for res in results:
            for f in (res or []):
                # Keep only true single-ticket connecting itineraries (>=1 transfer);
                # pure-direct ones are already covered by the direct pass above.
                if f.get("transfers_count", 0) >= 1:
                    f["is_connecting_fare"] = True
                    priced_flights.append(f)
                    connecting_count += 1
        await asyncio.sleep(1.0)
    logger.info(f"Connecting-fare lookup added {connecting_count} single-ticket itineraries.")

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
    priced_edges = {
        (flight.get("origin"), flight.get("destination"))
        for flight in priced_flights
        if flight.get("origin") and flight.get("destination")
    }
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
        allowed_hubs=allowed_hubs,
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
        "priced_edges_count": len(priced_edges),
        "unpriced_candidate_edges_count": max(len(candidate_edges) - len(priced_edges), 0),
        "total_routes_found": solved_data.get("total_routes_found_before_filter", 0),
        "total_routes_scored": solved_data.get("total_routes_scored", 0),
        "total_routes_after_filter": solved_data.get("total_routes_after_filter", 0),
        "rendered_routes_count": solved_data.get("rendered_routes_count", 0),
        "route_cap_hit": solved_data.get("route_cap_hit", False),
        "discovery_cache_hit": discovery_cache_hit,
        "discovery_diagnostics": discovery_diagnostics,
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
            "\n\n📎 *Действия доступны кнопками ниже.* "
            "Команды также есть в меню Telegram."
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

        reply_markup = None
        if solved_data.get("recommended") and not is_monitor_job:
            reply_markup = result_actions_keyboard(search_id, solved_data.get("recommended", []), 5, "balanced")

        # Send safe and split message (DOP-4)
        await send_message_safely(chat_id, analysis_text, reply_markup=reply_markup)
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
    logger.info("Starting scheduled route-subscription monitoring job...")
    subscriptions = await asyncio.to_thread(get_all_active_route_subscriptions)
    for sub in subscriptions:
        logger.info("Processing route subscription ID %s (%s)", sub["id"], sub["route_id"])
        try:
            route = json.loads(sub.get("route_json") or "{}")
            if not route:
                continue

            refreshed_route, partial_data, _ = await refresh_route_data(route, cache_mode="monitor")
            current_price = float(refreshed_route.get("total_price") or 0)
            decision = price_drop_alert_decision(
                last_price=sub.get("last_checked_price", 0),
                current_price=current_price,
                threshold_pct=sub.get("price_drop_threshold_pct", DEFAULT_PRICE_DROP_THRESHOLD_PCT),
                partial_data=partial_data,
            )

            if not decision["should_update_baseline"]:
                logger.info(
                    "Subscription %s skipped baseline update: %s",
                    sub["id"],
                    decision["reason"],
                )
                continue

            if decision["should_alert"]:
                old_price = float(sub.get("last_checked_price") or 0)
                text = await analyst.analyze_routes(
                    origin=sub.get("origin_iata", ""),
                    destination=sub.get("destination_text", ""),
                    date_range=f"{sub.get('date_start', '')} — {sub.get('date_end', '')}",
                    max_budget=0,
                    solved_data={
                        "recommended": [refreshed_route],
                        "is_fallback_active": False,
                        "total_routes_after_filter": 1,
                        "rendered_routes_count": 1
                    },
                    search_metadata=None
                )
                header = (
                    f"🔔 *Цена выбранного маршрута упала на {decision['drop_pct']:.1f}%*\n"
                    f"Маршрут: `{sub['route_id']}`\n"
                    f"Было: {old_price:,.0f} ₽\n"
                    f"Стало: {current_price:,.0f} ₽\n"
                    f"Порог: {decision['threshold_pct']:.1f}%\n\n"
                )
                await send_message_safely(
                    sub["chat_id"],
                    header + text,
                    reply_markup=subscription_actions_keyboard(sub["id"], sub["route_id"])
                )

            await asyncio.to_thread(update_route_subscription_baseline, sub["id"], current_price)
        except Exception as e:
            logger.error("Route subscription monitoring failed for ID %s: %s", sub.get("id"), e)

        await asyncio.sleep(5.0) # Gap between subscriptions to reduce rate load

# Scheduler triggers configuration
async def setup_bot_commands():
    await bot.set_my_commands([
        types.BotCommand(command="new_search", description="Новый мониторинг маршрута"),
        types.BotCommand(command="my_searches", description="Активные мониторинги"),
        types.BotCommand(command="refresh_search", description="Пересчитать поиск"),
        types.BotCommand(command="route", description="Детали маршрута"),
        types.BotCommand(command="subscribe_route", description="Подписаться на маршрут"),
        types.BotCommand(command="my_route_alerts", description="Мои подписки на маршруты"),
        types.BotCommand(command="unsubscribe_route", description="Отключить подписку"),
        types.BotCommand(command="why_route", description="Почему маршрут так оценен"),
        types.BotCommand(command="diagnose_search", description="Диагностика поиска"),
        types.BotCommand(command="check_route", description="Проверить прямой сегмент"),
        types.BotCommand(command="set_price_alert", description="Настроить порог падения цены"),
        types.BotCommand(command="cancel", description="Отменить настройку"),
    ])
    logger.info("Telegram command menu updated.")

def setup_scheduler():
    scheduler.add_job(run_daily_monitoring_job, 'cron', hour=10, minute=0)
    scheduler.start()
    logger.info("Scheduler setup complete. Daily job scheduled at 10:00.")

async def main():
    await setup_bot_commands()
    setup_scheduler()
    logger.info("Starting Telegram Bot long-polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    from db import init_db
    init_db()
    asyncio.run(main())
