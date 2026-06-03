import re


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
    "IST": "Стамбул",
    "NYC": "Нью-Йорк",
    "JFK": "Нью-Йорк JFK",
    "LAX": "Лос-Анджелес",
}


def city_name_for_iata(code: str) -> str | None:
    return IATA_CITY_NAMES_RU.get(str(code or "").upper())


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
