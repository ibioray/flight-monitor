import json
import os
import re
from pathlib import Path


IATA_CITY_NAMES_RU = {
    "UFA": "Уфа",
    "MOW": "Москва",
    "SVO": "Москва Шереметьево",
    "DME": "Москва Домодедово",
    "VKO": "Москва Внуково",
    "ZIA": "Москва Жуковский",
    "LED": "Санкт-Петербург",
    "SVX": "Екатеринбург",
    "KZN": "Казань",
    "OVB": "Новосибирск",
    "KJA": "Красноярск",
    "KRR": "Краснодар",
    "MRV": "Минеральные Воды",
    "MCX": "Махачкала",
    "GOJ": "Нижний Новгород",
    "NUX": "Новый Уренгой",
    "BQS": "Благовещенск",
    "HTA": "Чита",
    "YKS": "Якутск",
    "UUS": "Южно-Сахалинск",
    "AER": "Сочи",
    "IKT": "Иркутск",
    "KHV": "Хабаровск",
    "VVO": "Владивосток",
    "TJM": "Тюмень",
    "ALA": "Алматы",
    "NQZ": "Астана",
    "SCO": "Актау",
    "CIT": "Шымкент",
    "FRU": "Бишкек",
    "OSS": "Ош",
    "TAS": "Ташкент",
    "SKD": "Самарканд",
    "BHK": "Бухара",
    "UGC": "Ургенч",
    "EVN": "Ереван",
    "GYD": "Баку",
    "BAK": "Баку",
    "TBS": "Тбилиси",
    "BUS": "Батуми",
    "KUT": "Кутаиси",
    "IST": "Стамбул",
    "SAW": "Стамбул Сабиха",
    "AYT": "Анталья",
    "ESB": "Анкара",
    "ADB": "Измир",
    "DLM": "Даламан",
    "BJV": "Бодрум",
    "TZX": "Трабзон",
    "ADA": "Адана",
    "GZT": "Газиантеп",
    "ASR": "Кайсери",
    "KYA": "Конья",
    "DIY": "Диярбакыр",
    "ERZ": "Эрзурум",
    "DXB": "Дубай",
    "AUH": "Абу-Даби",
    "DOH": "Доха",
    "PEK": "Пекин",
    "PKX": "Пекин Дасин",
    "BJS": "Пекин",
    "PVG": "Шанхай Пудун",
    "SHA": "Шанхай Хунцяо",
    "CAN": "Гуанчжоу",
    "SZX": "Шэньчжэнь",
    "CTU": "Чэнду",
    "TFU": "Чэнду Тяньфу",
    "URC": "Урумчи",
    "XIY": "Сиань",
    "HGH": "Ханчжоу",
    "HRB": "Харбин",
    "KHN": "Наньчан",
    "KWL": "Гуйлинь",
    "KMG": "Куньмин",
    "WUH": "Ухань",
    "CGO": "Чжэнчжоу",
    "CGQ": "Чанчунь",
    "SHE": "Шэньян",
    "DLC": "Далянь",
    "TSN": "Тяньцзинь",
    "TNA": "Цзинань",
    "NKG": "Нанкин",
    "XMN": "Сямынь",
    "FOC": "Фучжоу",
    "WEN": "Вэньчжоу",
    "WNZ": "Вэньчжоу",
    "LHW": "Ланьчжоу",
    "LXA": "Лхаса",
    "NNG": "Наньнин",
    "NGB": "Нинбо",
    "NMA": "Наманган",
    "SYX": "Санья",
    "HAK": "Хайкоу",
    "TAO": "Циндао",
    "TPE": "Тайбэй",
    "HKG": "Гонконг",
    "MFM": "Макао",
    "BKK": "Бангкок",
    "DMK": "Бангкок Донмыанг",
    "HKT": "Пхукет",
    "CNX": "Чиангмай",
    "USM": "Самуи",
    "KBV": "Краби",
    "HAN": "Ханой",
    "SGN": "Хошимин",
    "DAD": "Дананг",
    "CXR": "Нячанг",
    "PQC": "Фукуок",
    "NHA": "Нячанг",
    "SIN": "Сингапур",
    "KUL": "Куала-Лумпур",
    "PEN": "Пенанг",
    "DPS": "Бали",
    "CGK": "Джакарта",
    "MNL": "Манила",
    "CEB": "Себу",
    "SEL": "Сеул",
    "ICN": "Сеул Инчхон",
    "GMP": "Сеул Гимпо",
    "TYO": "Токио",
    "HND": "Токио Ханэда",
    "NRT": "Токио Нарита",
    "KIX": "Осака Кансай",
    "DEL": "Дели",
    "BOM": "Мумбаи",
    "CMB": "Коломбо",
    "MLE": "Мале",
    "LON": "Лондон",
    "LHR": "Лондон Хитроу",
    "LGW": "Лондон Гатвик",
    "PAR": "Париж",
    "CDG": "Париж Шарль-де-Голль",
    "FRA": "Франкфурт",
    "BER": "Берлин",
    "ROM": "Рим",
    "FCO": "Рим Фьюмичино",
    "CIA": "Рим Чампино",
    "MIL": "Милан",
    "MXP": "Милан Мальпенса",
    "LIN": "Милан Линате",
    "VCE": "Венеция",
    "NAP": "Неаполь",
    "IST": "Стамбул",
    "NYC": "Нью-Йорк",
    "JFK": "Нью-Йорк JFK",
    "LAX": "Лос-Анджелес",
}

AIRPORT_NAMES_CACHE_PATH = Path(os.getenv("AIRPORT_NAMES_CACHE_PATH", "airport_names_cache.json"))
_DYNAMIC_IATA_NAMES_RU: dict[str, str] | None = None


def _load_dynamic_names() -> dict[str, str]:
    global _DYNAMIC_IATA_NAMES_RU
    if _DYNAMIC_IATA_NAMES_RU is not None:
        return _DYNAMIC_IATA_NAMES_RU
    try:
        if AIRPORT_NAMES_CACHE_PATH.exists():
            data = json.loads(AIRPORT_NAMES_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _DYNAMIC_IATA_NAMES_RU = {
                    str(code).upper(): str(name)
                    for code, name in data.items()
                    if len(str(code).strip()) == 3 and str(name).strip()
                }
                return _DYNAMIC_IATA_NAMES_RU
    except Exception:
        pass
    _DYNAMIC_IATA_NAMES_RU = {}
    return _DYNAMIC_IATA_NAMES_RU


def remember_iata_name(code: str, name: str | None):
    normalized = str(code or "").upper().strip()
    clean_name = str(name or "").strip()
    if len(normalized) != 3 or not normalized.isascii() or not normalized.isalpha() or not clean_name:
        return
    if IATA_CITY_NAMES_RU.get(normalized) == clean_name:
        return
    dynamic = _load_dynamic_names()
    if dynamic.get(normalized) == clean_name:
        return
    dynamic[normalized] = clean_name
    try:
        AIRPORT_NAMES_CACHE_PATH.write_text(
            json.dumps(dynamic, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8"
        )
    except Exception:
        pass


def remember_iata_names(entries):
    dynamic = _load_dynamic_names()
    changed = False
    for code, name in entries or []:
        normalized = str(code or "").upper().strip()
        clean_name = str(name or "").strip()
        if len(normalized) != 3 or not normalized.isascii() or not normalized.isalpha() or not clean_name:
            continue
        if IATA_CITY_NAMES_RU.get(normalized) == clean_name:
            continue
        if dynamic.get(normalized) == clean_name:
            continue
        dynamic[normalized] = clean_name
        changed = True
    if not changed:
        return
    try:
        AIRPORT_NAMES_CACHE_PATH.write_text(
            json.dumps(dynamic, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8"
        )
    except Exception:
        pass


def city_name_for_iata(code: str) -> str | None:
    normalized = str(code or "").upper()
    return IATA_CITY_NAMES_RU.get(normalized) or _load_dynamic_names().get(normalized)


def format_iata_city(code: str) -> str:
    normalized = str(code or "").upper()
    name = city_name_for_iata(normalized)
    if not normalized:
        return ""
    if not name:
        return normalized
    return f"{normalized} ({name})"


def annotate_iata_codes(text: str) -> str:
    def replace(match):
        code = match.group(0)
        name = city_name_for_iata(code)
        if not name:
            return code
        tail = text[match.end():match.end() + 2]
        if tail.startswith(" ("):
            return code
        return f"{code} ({name})"

    return re.sub(r"\b[A-Z]{3}\b", replace, str(text or ""))
