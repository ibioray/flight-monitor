import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from airport_names import remember_iata_name

logger = logging.getLogger("airport_catalog")

AIRPORT_DATA_CACHE_PATH = Path(os.getenv("AIRPORT_DATA_CACHE_PATH", "travelpayouts_data_cache.json"))
AIRPORT_DATA_CACHE_TTL_DAYS = int(os.getenv("AIRPORT_DATA_CACHE_TTL_DAYS", "14"))

AIRPORTS_URL = "https://api.travelpayouts.com/data/ru/airports.json"
CITIES_URL = "https://api.travelpayouts.com/data/ru/cities.json"
ROUTES_URL = "https://api.travelpayouts.com/data/routes.json"

_CATALOG: dict | None = None


def _valid_iata(value: str | None) -> str | None:
    code = str(value or "").upper().strip()
    if len(code) == 3 and code.isascii() and code.isalpha():
        return code
    return None


def _name_for_item(item: dict) -> str:
    name = item.get("city_name") or item.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    translations = item.get("name_translations")
    if isinstance(translations, dict):
        for key in ("ru", "en"):
            value = translations.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _is_cache_fresh(payload: dict) -> bool:
    fetched_at = payload.get("fetched_at")
    if not fetched_at:
        return False
    try:
        fetched_dt = datetime.fromisoformat(str(fetched_at))
    except ValueError:
        return False
    if fetched_dt.tzinfo is None:
        fetched_dt = fetched_dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched_dt < timedelta(days=AIRPORT_DATA_CACHE_TTL_DAYS)


async def _fetch_json(client: httpx.AsyncClient, url: str) -> list:
    response = await client.get(url)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def _load_cache() -> dict | None:
    try:
        if AIRPORT_DATA_CACHE_PATH.exists():
            payload = json.loads(AIRPORT_DATA_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and _is_cache_fresh(payload):
                return payload
    except Exception as e:
        logger.warning("Failed to load airport data cache: %s", e)
    return None


def _save_cache(payload: dict):
    try:
        AIRPORT_DATA_CACHE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception as e:
        logger.warning("Failed to save airport data cache: %s", e)


async def load_catalog(force_refresh: bool = False) -> dict:
    global _CATALOG
    if _CATALOG is not None and not force_refresh:
        return _CATALOG

    if not force_refresh:
        cached = _load_cache()
        if cached:
            _CATALOG = cached
            _remember_known_names(cached.get("airports", []), cached.get("cities", []))
            return _CATALOG

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            airports = await _fetch_json(client, AIRPORTS_URL)
            cities = await _fetch_json(client, CITIES_URL)
            try:
                routes = await _fetch_json(client, ROUTES_URL)
            except Exception as e:
                logger.warning("Failed to fetch routes catalog, airport ranking will be weaker: %s", e)
                routes = []
    except Exception as e:
        logger.warning("Failed to fetch Travelpayouts airport catalog: %s", e)
        cached_any_age = _load_cache_any_age()
        if cached_any_age:
            _CATALOG = cached_any_age
            _remember_known_names(cached_any_age.get("airports", []), cached_any_age.get("cities", []))
            return _CATALOG
        _CATALOG = {"fetched_at": datetime.now(timezone.utc).isoformat(), "airports": [], "cities": [], "routes": []}
        return _CATALOG

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "airports": airports,
        "cities": cities,
        "routes": routes,
    }
    _save_cache(payload)
    _CATALOG = payload
    _remember_known_names(airports, cities)
    return _CATALOG


def _load_cache_any_age() -> dict | None:
    try:
        if AIRPORT_DATA_CACHE_PATH.exists():
            payload = json.loads(AIRPORT_DATA_CACHE_PATH.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
    except Exception:
        return None
    return None


def _remember_known_names(airports: list[dict], cities: list[dict]):
    for city in cities:
        code = _valid_iata(city.get("code"))
        if code:
            remember_iata_name(code, _name_for_item(city))
    for airport in airports:
        code = _valid_iata(airport.get("code"))
        if code:
            remember_iata_name(code, _name_for_item(airport))


def _route_degree(routes: list[dict]) -> dict[str, int]:
    degree: dict[str, int] = {}
    for route in routes:
        for key in (
            "departure_airport_iata",
            "arrival_airport_iata",
            "departure_airport",
            "arrival_airport",
            "origin",
            "destination",
        ):
            code = _valid_iata(route.get(key))
            if code:
                degree[code] = degree.get(code, 0) + 1
    return degree


async def airports_for_country(country_code: str, limit: int = 40) -> list[str]:
    country = str(country_code or "").upper().strip()
    if len(country) != 2 or not country.isalpha():
        return []
    catalog = await load_catalog()
    airports = catalog.get("airports", [])
    routes = catalog.get("routes", [])
    degree = _route_degree(routes)

    candidates = []
    for airport in airports:
        code = _valid_iata(airport.get("code"))
        if not code:
            continue
        if str(airport.get("country_code") or "").upper() != country:
            continue
        if airport.get("iata_type") != "airport":
            continue
        if not airport.get("flightable"):
            continue
        city_code = _valid_iata(airport.get("city_code"))
        name = _name_for_item(airport)
        remember_iata_name(code, name)
        candidates.append({
            "code": code,
            "city_code": city_code,
            "degree": degree.get(code, 0),
            "has_name": 1 if name else 0,
        })

    candidates.sort(key=lambda item: (
        -item["degree"],
        0 if item["code"] == item.get("city_code") else 1,
        -item["has_name"],
        item["code"],
    ))
    return [item["code"] for item in candidates[:limit]]


async def airports_for_city(city_code: str, limit: int = 8) -> list[str]:
    city = _valid_iata(city_code)
    if not city:
        return []
    catalog = await load_catalog()
    airports = catalog.get("airports", [])
    routes = catalog.get("routes", [])
    degree = _route_degree(routes)
    candidates = []
    for airport in airports:
        code = _valid_iata(airport.get("code"))
        if not code:
            continue
        if _valid_iata(airport.get("city_code")) != city:
            continue
        if airport.get("iata_type") != "airport":
            continue
        if not airport.get("flightable"):
            continue
        name = _name_for_item(airport)
        remember_iata_name(code, name)
        candidates.append({"code": code, "degree": degree.get(code, 0), "has_name": 1 if name else 0})

    candidates.sort(key=lambda item: (-item["degree"], -item["has_name"], item["code"]))
    codes = [item["code"] for item in candidates[:limit]]
    if city not in codes:
        codes.insert(0, city)
    return codes[:limit]
