import sqlite3
import json
import logging
import hashlib
from datetime import datetime, timezone, timedelta
from config import DATABASE_PATH

logger = logging.getLogger("db")

DISCOVERY_CACHE_VERSION = "layered_mitm_v2"

QUERY_LOG_PROVIDER = "travelpayouts_prices_for_dates_v4"
QUERY_LOG_CURRENCY = "rub"
QUERY_LOG_ONE_WAY = 1
QUERY_LOG_MARKET = ""

def get_db_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _table_columns(cursor, table_name: str) -> set[str]:
    cursor.execute(f"PRAGMA table_info({table_name})")
    return {row["name"] for row in cursor.fetchall()}

def _table_primary_key(cursor, table_name: str) -> list[str]:
    cursor.execute(f"PRAGMA table_info({table_name})")
    rows = [dict(row) for row in cursor.fetchall()]
    return [row["name"] for row in sorted(rows, key=lambda row: row["pk"]) if row["pk"]]

def _add_column_if_missing(cursor, table_name: str, column_name: str, column_sql: str):
    if column_name not in _table_columns(cursor, table_name):
        logger.info("Migrating %s: adding column %s...", table_name, column_name)
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")

def _rebuild_flight_cache(cursor):
    logger.info("Rebuilding flight_cache with current schema...")
    existing_columns = _table_columns(cursor, "flight_cache")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS flight_cache_new (
        origin TEXT NOT NULL,
        destination TEXT NOT NULL,
        depart_date TEXT NOT NULL,
        departure_at TEXT NOT NULL,
        price REAL NOT NULL,
        airline TEXT,
        flight_number TEXT,
        transfers_count INTEGER DEFAULT 0,
        duration INTEGER DEFAULT 0,
        direct_only INTEGER DEFAULT 0,
        updated_at TEXT NOT NULL,
        fetched_at TEXT,
        expires_at TEXT,
        PRIMARY KEY (origin, destination, departure_at, direct_only)
    )
    """)

    departure_expr = "departure_at" if "departure_at" in existing_columns else "depart_date || 'T00:00:00Z'"
    flight_number_expr = "flight_number" if "flight_number" in existing_columns else "NULL"
    duration_expr = "duration" if "duration" in existing_columns else "0"
    direct_only_expr = "direct_only" if "direct_only" in existing_columns else "0"
    fetched_at_expr = "fetched_at" if "fetched_at" in existing_columns else "NULL"
    expires_at_expr = "expires_at" if "expires_at" in existing_columns else "NULL"

    cursor.execute(f"""
    INSERT OR REPLACE INTO flight_cache_new (
        origin, destination, depart_date, departure_at, price,
        airline, flight_number, transfers_count, duration, direct_only,
        updated_at, fetched_at, expires_at
    )
    SELECT
        origin,
        destination,
        depart_date,
        {departure_expr},
        price,
        airline,
        {flight_number_expr},
        transfers_count,
        {duration_expr},
        {direct_only_expr},
        updated_at,
        {fetched_at_expr},
        {expires_at_expr}
    FROM flight_cache
    """)
    cursor.execute("DROP TABLE flight_cache")
    cursor.execute("ALTER TABLE flight_cache_new RENAME TO flight_cache")

def _rebuild_route_query_log(cursor):
    logger.info("Rebuilding route_query_log with current cache key schema...")
    existing_columns = _table_columns(cursor, "route_query_log")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS route_query_log_new (
        provider TEXT NOT NULL DEFAULT 'travelpayouts_prices_for_dates_v3',
        origin TEXT NOT NULL,
        destination TEXT NOT NULL,
        month TEXT NOT NULL,
        direct_only INTEGER DEFAULT 0,
        one_way INTEGER DEFAULT 1,
        currency TEXT DEFAULT 'rub',
        market TEXT DEFAULT '',
        queried_at TEXT NOT NULL,
        PRIMARY KEY (provider, origin, destination, month, direct_only, one_way, currency, market)
    )
    """)

    provider_expr = "provider" if "provider" in existing_columns else f"'{QUERY_LOG_PROVIDER}'"
    one_way_expr = "one_way" if "one_way" in existing_columns else str(QUERY_LOG_ONE_WAY)
    currency_expr = "currency" if "currency" in existing_columns else f"'{QUERY_LOG_CURRENCY}'"
    market_expr = "market" if "market" in existing_columns else "''"

    cursor.execute(f"""
    INSERT OR REPLACE INTO route_query_log_new (
        provider, origin, destination, month, direct_only, one_way, currency, market, queried_at
    )
    SELECT
        {provider_expr},
        origin,
        destination,
        month,
        direct_only,
        {one_way_expr},
        {currency_expr},
        {market_expr},
        queried_at
    FROM route_query_log
    """)
    cursor.execute("DROP TABLE route_query_log")
    cursor.execute("ALTER TABLE route_query_log_new RENAME TO route_query_log")

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Users Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL,
        status TEXT DEFAULT 'active',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # 2. User Searches Table (Scalable, multi-user, multi-search config)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        origin_iata TEXT NOT NULL,
        destination_text TEXT NOT NULL,
        date_start TEXT NOT NULL,
        date_end TEXT NOT NULL,
        max_transfers INTEGER DEFAULT 3,
        visa_allowed INTEGER DEFAULT 1,
        lodging_exceptions_json TEXT DEFAULT '{}',
        max_budget INTEGER,
        is_active INTEGER DEFAULT 1,
        last_checked_price REAL DEFAULT 0,
        last_checked_at TEXT,
        price_drop_threshold_pct REAL DEFAULT 10,
        stopovers_json TEXT DEFAULT '[]',
        exclusions_json TEXT DEFAULT '[]',
        baggage_needed INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(user_id)
    )
    """)
    
    # 2.1 Migrate user_searches column-by-column. This survives partially migrated DBs.
    _add_column_if_missing(cursor, "user_searches", "stopovers_json", "stopovers_json TEXT DEFAULT '[]'")
    _add_column_if_missing(cursor, "user_searches", "exclusions_json", "exclusions_json TEXT DEFAULT '[]'")
    _add_column_if_missing(cursor, "user_searches", "baggage_needed", "baggage_needed INTEGER DEFAULT 0")
    _add_column_if_missing(cursor, "user_searches", "last_checked_at", "last_checked_at TEXT")
    _add_column_if_missing(cursor, "user_searches", "price_drop_threshold_pct", "price_drop_threshold_pct REAL DEFAULT 10")
    _add_column_if_missing(cursor, "user_searches", "cache_mode", "cache_mode TEXT DEFAULT 'overview'")
    _add_column_if_missing(cursor, "user_searches", "min_stopover_hours", "min_stopover_hours INTEGER DEFAULT 0")
    _add_column_if_missing(cursor, "user_searches", "max_stopover_days", "max_stopover_days INTEGER DEFAULT 5")
    _add_column_if_missing(cursor, "user_searches", "stopover_preset", "stopover_preset TEXT DEFAULT 'balanced'")
    _add_column_if_missing(cursor, "user_searches", "allow_awkward_layovers", "allow_awkward_layovers INTEGER DEFAULT 1")
    _add_column_if_missing(cursor, "user_searches", "visa_mode", "visa_mode TEXT DEFAULT 'visa_free_only'")
    
    # 3. Flight Cache Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS flight_cache (
        origin TEXT NOT NULL,
        destination TEXT NOT NULL,
        depart_date TEXT NOT NULL,
        departure_at TEXT NOT NULL,
        price REAL NOT NULL,
        airline TEXT,
        flight_number TEXT,
        transfers_count INTEGER DEFAULT 0,
        duration INTEGER DEFAULT 0,
        direct_only INTEGER DEFAULT 0,
        updated_at TEXT NOT NULL,
        fetched_at TEXT,
        expires_at TEXT,
        PRIMARY KEY (origin, destination, departure_at, direct_only)
    )
    """)
    
    # 3.1 Rebuild if the existing table is legacy or partially migrated.
    flight_cache_columns = _table_columns(cursor, "flight_cache")
    flight_cache_pk = _table_primary_key(cursor, "flight_cache")
    required_flight_cache_columns = {
        "origin", "destination", "depart_date", "departure_at", "price",
        "airline", "flight_number", "transfers_count", "duration",
        "direct_only", "updated_at"
    }
    if (
        not required_flight_cache_columns.issubset(flight_cache_columns)
        or flight_cache_pk != ["origin", "destination", "departure_at", "direct_only"]
    ):
        _rebuild_flight_cache(cursor)

    # 3.1.1 Non-destructive column migrations for flight_cache
    _add_column_if_missing(cursor, "flight_cache", "fetched_at", "fetched_at TEXT")
    _add_column_if_missing(cursor, "flight_cache", "expires_at", "expires_at TEXT")
        
    # 3.2 Route Query Log Table (Codex D)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS route_query_log (
        provider TEXT NOT NULL DEFAULT 'travelpayouts_prices_for_dates_v3',
        origin TEXT NOT NULL,
        destination TEXT NOT NULL,
        month TEXT NOT NULL,
        direct_only INTEGER DEFAULT 0,
        one_way INTEGER DEFAULT 1,
        currency TEXT DEFAULT 'rub',
        market TEXT DEFAULT '',
        queried_at TEXT NOT NULL,
        PRIMARY KEY (provider, origin, destination, month, direct_only, one_way, currency, market)
    )
    """)
    route_query_columns = _table_columns(cursor, "route_query_log")
    route_query_pk = _table_primary_key(cursor, "route_query_log")
    required_route_query_columns = {
        "provider", "origin", "destination", "month", "direct_only",
        "one_way", "currency", "market", "queried_at"
    }
    if (
        not required_route_query_columns.issubset(route_query_columns)
        or route_query_pk != ["provider", "origin", "destination", "month", "direct_only", "one_way", "currency", "market"]
    ):
        _rebuild_route_query_log(cursor)
    
    # 4. Transit Hubs Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS transit_hubs (
        iata TEXT PRIMARY KEY,
        city_name TEXT NOT NULL,
        country_code TEXT DEFAULT '',
        daily_lodging_rub REAL DEFAULT 0,
        requires_visa_for_ru INTEGER DEFAULT 0,
        passport_type_required TEXT DEFAULT 'internal' -- 'internal' or 'foreign'
    )
    """)
    _add_column_if_missing(cursor, "transit_hubs", "country_code", "country_code TEXT DEFAULT ''")
    
    # 5. Manual Legs Table (trains, buses, etc.)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS manual_legs (
        origin TEXT NOT NULL,
        destination TEXT NOT NULL,
        price_rub REAL NOT NULL,
        duration_hours REAL NOT NULL,
        leg_type TEXT DEFAULT 'train', -- 'train', 'bus', 'ferry'
        PRIMARY KEY (origin, destination)
    )
    """)

    # 6. Search snapshots keep structured route results for /route and /more_routes.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS search_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        search_id INTEGER,
        origin_iata TEXT NOT NULL,
        destination_text TEXT NOT NULL,
        date_start TEXT NOT NULL,
        date_end TEXT NOT NULL,
        metadata_json TEXT DEFAULT '{}',
        solved_data_json TEXT DEFAULT '{}',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(user_id),
        FOREIGN KEY(search_id) REFERENCES user_searches(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS route_snapshots (
        snapshot_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        search_id INTEGER,
        route_id TEXT NOT NULL,
        route_rank INTEGER NOT NULL,
        route_price REAL DEFAULT 0,
        duration_hours REAL DEFAULT 0,
        legs_count INTEGER DEFAULT 0,
        risk_score REAL DEFAULT 0,
        has_stopover INTEGER DEFAULT 0,
        route_json TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (snapshot_id, route_id),
        FOREIGN KEY(snapshot_id) REFERENCES search_snapshots(id)
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_route_snapshots_user_route ON route_snapshots(user_id, route_id, snapshot_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_route_snapshots_user_search ON route_snapshots(user_id, search_id, snapshot_id)")

    # 7. Discovery/topology cache. This caches candidate graph edges, not prices.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS route_discovery_cache (
        origin TEXT NOT NULL,
        destination_country TEXT NOT NULL,
        destination_iatas_hash TEXT NOT NULL,
        months_hash TEXT NOT NULL,
        max_transfers INTEGER NOT NULL,
        edges_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (origin, destination_country, destination_iatas_hash, months_hash, max_transfers)
    )
    """)

    # 8. Route-level price subscriptions. A search can be one-off; monitoring is attached
    # only to selected route snapshots.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS route_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        search_id INTEGER,
        snapshot_id INTEGER NOT NULL,
        route_id TEXT NOT NULL,
        route_json TEXT NOT NULL,
        origin_iata TEXT NOT NULL,
        destination_text TEXT NOT NULL,
        date_start TEXT NOT NULL,
        date_end TEXT NOT NULL,
        price_drop_threshold_pct REAL DEFAULT 10,
        last_checked_price REAL DEFAULT 0,
        last_checked_at TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, search_id, route_id),
        FOREIGN KEY(user_id) REFERENCES users(user_id),
        FOREIGN KEY(search_id) REFERENCES user_searches(id),
        FOREIGN KEY(snapshot_id) REFERENCES search_snapshots(id)
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_route_subscriptions_active ON route_subscriptions(is_active, user_id, route_id)")
    
    conn.commit()
    
    # Seed default transit hubs
    default_hubs = [
        # Internal passport, visa-free
        ("MOW", "Москва", "RU", 4000, 0, "internal"),
        ("EVN", "Ереван", "AM", 0, 0, "internal"), # Custom request: 0 lodging cost for EVN
        ("ALA", "Алматы", "KZ", 3500, 0, "internal"),
        ("NQZ", "Астана", "KZ", 3500, 0, "internal"),
        ("FRU", "Бишкек", "KG", 2500, 0, "internal"),
        
        # Russian hubs (internal passport)
        ("SVX", "Екатеринбург", "RU", 3000, 0, "internal"),
        ("KZN", "Казань", "RU", 3500, 0, "internal"),
        ("OVB", "Новосибирск", "RU", 3000, 0, "internal"),
        
        # Foreign passport, visa-free
        ("TAS", "Ташкент", "UZ", 3000, 0, "foreign"),
        ("BAK", "Баку", "AZ", 4000, 0, "foreign"),
        ("IST", "Стамбул", "TR", 7000, 0, "foreign"),
        ("DXB", "Дубай", "AE", 10000, 0, "foreign"),
        ("AUH", "Абу-Даби", "AE", 8000, 0, "foreign"),
        ("DOH", "Доха", "QA", 9000, 0, "foreign"),

        # Known visa-risk examples for RU stopovers.
        ("TYO", "Токио", "JP", 12000, 1, "foreign"),
        ("HND", "Токио Ханэда", "JP", 12000, 1, "foreign"),
        ("NRT", "Токио Нарита", "JP", 12000, 1, "foreign"),
        ("LON", "Лондон", "GB", 14000, 1, "foreign"),
        ("LHR", "Лондон Хитроу", "GB", 14000, 1, "foreign"),
        ("JFK", "Нью-Йорк", "US", 16000, 1, "foreign"),
    ]
    
    for hub in default_hubs:
        cursor.execute("""
        INSERT INTO transit_hubs (iata, city_name, country_code, daily_lodging_rub, requires_visa_for_ru, passport_type_required)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(iata) DO UPDATE SET
            city_name = excluded.city_name,
            country_code = excluded.country_code,
            daily_lodging_rub = excluded.daily_lodging_rub,
            requires_visa_for_ru = excluded.requires_visa_for_ru,
            passport_type_required = excluded.passport_type_required
        """, hub)
        
    # Seed default manual legs (popular cross-border segments)
    default_manual_legs = [
        ("ALA", "URC", 7500, 30.0, "train"),  # Almaty to Urumqi train approx price & duration
        ("NQZ", "URC", 8500, 34.0, "train"),  # Astana to Urumqi train
    ]
    
    for leg in default_manual_legs:
        cursor.execute("""
        INSERT OR IGNORE INTO manual_legs (origin, destination, price_rub, duration_hours, leg_type)
        VALUES (?, ?, ?, ?, ?)
        """, leg)
        
    conn.commit()
    conn.close()
    logger.info("Database initialized and default transit hubs / manual legs seeded.")

# CRUD operations
def register_user(user_id: int, chat_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT OR IGNORE INTO users (user_id, chat_id) VALUES (?, ?)
    """, (user_id, chat_id))
    conn.commit()
    conn.close()

def save_user_search(user_id: int, origin_iata: str, destination_text: str, date_start: str, date_end: str, 
                     max_transfers: int, visa_allowed: int, lodging_exceptions: dict, max_budget: int,
                     stopovers: list = None, exclusions: list = None, baggage_needed: int = 0,
                     cache_mode: str = "overview", min_stopover_hours: int = 0,
                     max_stopover_days: int = 5, stopover_preset: str = "balanced",
                     allow_awkward_layovers: int = 1, visa_mode: str = "visa_free_only",
                     price_drop_threshold_pct: float = 10.0):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO user_searches (
        user_id, origin_iata, destination_text, date_start, date_end, 
        max_transfers, visa_allowed, lodging_exceptions_json, max_budget,
        stopovers_json, exclusions_json, baggage_needed,
        price_drop_threshold_pct,
        cache_mode, min_stopover_hours, max_stopover_days, stopover_preset,
        allow_awkward_layovers, visa_mode
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id, origin_iata, destination_text, date_start, date_end, 
        max_transfers, visa_allowed, json.dumps(lodging_exceptions), max_budget,
        json.dumps(stopovers or []), json.dumps(exclusions or []), baggage_needed,
        price_drop_threshold_pct,
        cache_mode, min_stopover_hours, max_stopover_days, stopover_preset,
        allow_awkward_layovers, visa_mode
    ))
    search_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return search_id

def get_user_searches(user_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM user_searches WHERE user_id = ? AND is_active = 1", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def delete_user_search(search_id: int, user_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE user_searches SET is_active = 0 WHERE id = ? AND user_id = ?", (search_id, user_id))
    conn.commit()
    conn.close()

def get_all_active_searches():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT s.*, u.chat_id FROM user_searches s
    JOIN users u ON s.user_id = u.user_id
    WHERE s.is_active = 1
    """)
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def update_last_checked_price(search_id: int, price: float):
    conn = get_db_connection()
    cursor = conn.cursor()
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "UPDATE user_searches SET last_checked_price = ?, last_checked_at = ? WHERE id = ?",
        (price, checked_at, search_id)
    )
    conn.commit()
    conn.close()

def update_price_drop_threshold(search_id: int, user_id: int, threshold_pct: float) -> bool:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE user_searches
    SET price_drop_threshold_pct = ?
    WHERE id = ? AND user_id = ? AND is_active = 1
    """, (threshold_pct, search_id, user_id))
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return changed

def subscribe_route(user_id: int, chat_id: int, route_id: str, search_id: int | None = None,
                    threshold_pct: float | None = None) -> dict | None:
    route_row = get_route_snapshot(user_id, route_id, search_id)
    if not route_row:
        return None

    route = json.loads(route_row["route_json"])
    baseline = float(route.get("total_price") or route_row.get("route_price") or 0)
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sub_search_id = route_row.get("search_id")

    conn = get_db_connection()
    cursor = conn.cursor()
    threshold = threshold_pct
    if threshold is None and sub_search_id:
        cursor.execute("SELECT price_drop_threshold_pct FROM user_searches WHERE id = ? AND user_id = ?", (sub_search_id, user_id))
        search_row = cursor.fetchone()
        if search_row:
            threshold = search_row["price_drop_threshold_pct"]
    threshold = float(threshold or 10.0)
    cursor.execute("""
    INSERT INTO route_subscriptions (
        user_id, chat_id, search_id, snapshot_id, route_id, route_json,
        origin_iata, destination_text, date_start, date_end,
        price_drop_threshold_pct, last_checked_price, last_checked_at, is_active
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
    ON CONFLICT(user_id, search_id, route_id) DO UPDATE SET
        chat_id = excluded.chat_id,
        snapshot_id = excluded.snapshot_id,
        route_json = excluded.route_json,
        origin_iata = excluded.origin_iata,
        destination_text = excluded.destination_text,
        date_start = excluded.date_start,
        date_end = excluded.date_end,
        price_drop_threshold_pct = excluded.price_drop_threshold_pct,
        last_checked_price = excluded.last_checked_price,
        last_checked_at = excluded.last_checked_at,
        is_active = 1
    """, (
        user_id,
        chat_id,
        sub_search_id,
        route_row["snapshot_id"],
        route_row["route_id"],
        route_row["route_json"],
        route_row.get("origin_iata", ""),
        route_row.get("destination_text", ""),
        route_row.get("date_start", ""),
        route_row.get("date_end", ""),
        threshold,
        baseline,
        checked_at,
    ))
    conn.commit()
    if sub_search_id is None:
        cursor.execute("""
        SELECT * FROM route_subscriptions
        WHERE user_id = ? AND search_id IS NULL AND route_id = ?
        """, (user_id, route_row["route_id"]))
    else:
        cursor.execute("""
        SELECT * FROM route_subscriptions
        WHERE user_id = ? AND search_id = ? AND route_id = ?
        """, (user_id, sub_search_id, route_row["route_id"]))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_user_route_subscriptions(user_id: int) -> list[dict]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT * FROM route_subscriptions
    WHERE user_id = ? AND is_active = 1
    ORDER BY created_at DESC, id DESC
    """, (user_id,))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def get_all_active_route_subscriptions() -> list[dict]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT * FROM route_subscriptions
    WHERE is_active = 1
    ORDER BY id ASC
    """)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def update_route_subscription_baseline(subscription_id: int, price: float):
    conn = get_db_connection()
    cursor = conn.cursor()
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
    UPDATE route_subscriptions
    SET last_checked_price = ?, last_checked_at = ?
    WHERE id = ?
    """, (price, checked_at, subscription_id))
    conn.commit()
    conn.close()

def deactivate_route_subscription(subscription_id: int, user_id: int) -> bool:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE route_subscriptions
    SET is_active = 0
    WHERE id = ? AND user_id = ?
    """, (subscription_id, user_id))
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return changed

def _unique_routes_for_snapshot(solved_data: dict) -> list[dict]:
    routes = []
    seen = set()
    for key in ("ranked_routes", "recommended", "cheapest", "fastest", "direct", "one_connection", "stopovers"):
        for route in solved_data.get(key, []) or []:
            route_id = route.get("route_id")
            if not route_id or route_id in seen:
                continue
            seen.add(route_id)
            routes.append(route)
    return routes

def save_search_snapshot(user_id: int, search_id: int | None, search_config: dict, metadata: dict, solved_data: dict) -> int:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO search_snapshots (
        user_id, search_id, origin_iata, destination_text, date_start, date_end,
        metadata_json, solved_data_json
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        search_id,
        search_config.get("origin_iata", ""),
        search_config.get("dest_iata", search_config.get("destination_text", "")),
        search_config.get("date_start", ""),
        search_config.get("date_end", ""),
        json.dumps(metadata, ensure_ascii=False),
        json.dumps(solved_data, ensure_ascii=False),
    ))
    snapshot_id = cursor.lastrowid

    for rank, route in enumerate(_unique_routes_for_snapshot(solved_data), start=1):
        segments = route.get("segments", [])
        cursor.execute("""
        INSERT OR REPLACE INTO route_snapshots (
            snapshot_id, user_id, search_id, route_id, route_rank, route_price,
            duration_hours, legs_count, risk_score, has_stopover, route_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            snapshot_id,
            user_id,
            search_id,
            route.get("route_id"),
            rank,
            route.get("total_price", 0),
            route.get("duration_hours", 0),
            len(segments),
            route.get("risk_score", 0),
            1 if route.get("stopovers") else 0,
            json.dumps(route, ensure_ascii=False),
        ))

    conn.commit()
    conn.close()
    return snapshot_id

def get_latest_search_snapshot(user_id: int, search_id: int | None = None):
    conn = get_db_connection()
    cursor = conn.cursor()
    if search_id:
        cursor.execute("""
        SELECT * FROM search_snapshots
        WHERE user_id = ? AND search_id = ?
        ORDER BY id DESC LIMIT 1
        """, (user_id, search_id))
    else:
        cursor.execute("""
        SELECT * FROM search_snapshots
        WHERE user_id = ?
        ORDER BY id DESC LIMIT 1
        """, (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_route_snapshot(user_id: int, route_id: str, search_id: int | None = None):
    conn = get_db_connection()
    cursor = conn.cursor()
    if search_id:
        cursor.execute("""
        SELECT rs.*, ss.metadata_json, ss.origin_iata, ss.destination_text, ss.date_start, ss.date_end
        FROM route_snapshots rs
        JOIN search_snapshots ss ON ss.id = rs.snapshot_id
        WHERE rs.user_id = ? AND rs.search_id = ? AND upper(rs.route_id) = upper(?)
        ORDER BY rs.snapshot_id DESC LIMIT 1
        """, (user_id, search_id, route_id))
    else:
        cursor.execute("""
        SELECT rs.*, ss.metadata_json, ss.origin_iata, ss.destination_text, ss.date_start, ss.date_end
        FROM route_snapshots rs
        JOIN search_snapshots ss ON ss.id = rs.snapshot_id
        WHERE rs.user_id = ? AND upper(rs.route_id) = upper(?)
        ORDER BY rs.snapshot_id DESC LIMIT 1
        """, (user_id, route_id))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_snapshot_routes(user_id: int, search_id: int | None = None, offset: int = 0, limit: int = 5, sort_mode: str = "balanced"):
    snapshot = get_latest_search_snapshot(user_id, search_id)
    if not snapshot:
        return None, []

    order_by = {
        "price": "route_price ASC, duration_hours ASC, route_rank ASC",
        "duration": "duration_hours ASC, route_price ASC, route_rank ASC",
        "comfort": "risk_score ASC, legs_count ASC, duration_hours ASC, route_price ASC",
        "stopover": "has_stopover DESC, route_price ASC, duration_hours ASC, route_rank ASC",
        "balanced": "route_rank ASC",
    }.get(sort_mode, "route_rank ASC")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(f"""
    SELECT * FROM route_snapshots
    WHERE snapshot_id = ?
    ORDER BY {order_by}
    LIMIT ? OFFSET ?
    """, (snapshot["id"], limit, offset))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return snapshot, rows

def _stable_hash(values: list[str]) -> str:
    payload = json.dumps(sorted(values), ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()

def _versioned_discovery_destination(destination_country: str) -> str:
    return f"{destination_country}::{DISCOVERY_CACHE_VERSION}"

def get_discovery_cache(origin: str, destination_country: str, destination_iatas: list[str], months: list[str],
                        max_transfers: int, ttl_hours: int = 24) -> set[tuple[str, str]] | None:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT edges_json FROM route_discovery_cache
    WHERE origin = ? AND destination_country = ?
    AND destination_iatas_hash = ? AND months_hash = ?
    AND max_transfers = ?
    AND datetime(created_at) >= datetime('now', ?)
    """, (
        origin,
        _versioned_discovery_destination(destination_country),
        _stable_hash(destination_iatas),
        _stable_hash(months),
        max_transfers,
        f"-{ttl_hours} hours",
    ))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    try:
        edges = json.loads(row["edges_json"])
        return {(str(origin), str(destination)) for origin, destination in edges}
    except Exception:
        return None

def save_discovery_cache(origin: str, destination_country: str, destination_iatas: list[str], months: list[str],
                         max_transfers: int, edges: set[tuple[str, str]]):
    conn = get_db_connection()
    cursor = conn.cursor()
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    edges_json = json.dumps(sorted([list(edge) for edge in edges]), ensure_ascii=False)
    cursor.execute("""
    INSERT INTO route_discovery_cache (
        origin, destination_country, destination_iatas_hash, months_hash,
        max_transfers, edges_json, created_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(origin, destination_country, destination_iatas_hash, months_hash, max_transfers) DO UPDATE SET
        edges_json = excluded.edges_json,
        created_at = excluded.created_at
    """, (
        origin,
        _versioned_discovery_destination(destination_country),
        _stable_hash(destination_iatas),
        _stable_hash(months),
        max_transfers,
        edges_json,
        created_at,
    ))
    conn.commit()
    conn.close()

# Flight Cache Operations
def get_cached_flight(origin: str, destination: str, depart_date: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT * FROM flight_cache 
    WHERE origin = ? AND destination = ? AND depart_date = ?
    AND datetime(updated_at) >= datetime('now', '-24 hours')
    """, (origin, destination, depart_date))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

# Route Query Log Helpers (Codex D)
def check_route_query_log(
    origin: str,
    destination: str,
    month: str,
    direct_only: int,
    ttl_hours: int = 24,
    one_way: int = QUERY_LOG_ONE_WAY,
    currency: str = QUERY_LOG_CURRENCY,
    market: str = QUERY_LOG_MARKET,
    provider: str = QUERY_LOG_PROVIDER,
) -> bool:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(f"""
    SELECT 1 FROM route_query_log
    WHERE provider = ?
    AND origin = ? AND destination = ? AND month = ? AND direct_only = ?
    AND one_way = ? AND currency = ? AND market = ?
    AND datetime(queried_at) >= datetime('now', '-{int(ttl_hours)} hours')
    """, (provider, origin, destination, month, direct_only, one_way, currency, market))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def log_route_query(
    origin: str,
    destination: str,
    month: str,
    direct_only: int,
    one_way: int = QUERY_LOG_ONE_WAY,
    currency: str = QUERY_LOG_CURRENCY,
    market: str = QUERY_LOG_MARKET,
    provider: str = QUERY_LOG_PROVIDER,
):
    conn = get_db_connection()
    cursor = conn.cursor()
    # Use timezone-aware UTC datetime (Audit mitigation)
    queried_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
    INSERT INTO route_query_log (
        provider, origin, destination, month, direct_only, one_way, currency, market, queried_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(provider, origin, destination, month, direct_only, one_way, currency, market) DO UPDATE SET
        queried_at = excluded.queried_at
    """, (provider, origin, destination, month, direct_only, one_way, currency, market, queried_at))
    conn.commit()
    conn.close()

def get_cached_flights(origin: str, destination: str, month: str, direct_only: int) -> list[dict]:
    conn = get_db_connection()
    cursor = conn.cursor()
    if direct_only:
        cursor.execute("""
        SELECT * FROM flight_cache
        WHERE origin = ? AND destination = ? AND depart_date LIKE ?
        AND direct_only = 1 AND transfers_count = 0
        """, (origin, destination, f"{month}%"))
    else:
        cursor.execute("""
        SELECT * FROM flight_cache
        WHERE origin = ? AND destination = ? AND depart_date LIKE ?
        AND direct_only = 0
        """, (origin, destination, f"{month}%"))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def clear_flight_cache_for_route_month(origin: str, destination: str, month: str, direct_only: int | None = None):
    conn = get_db_connection()
    cursor = conn.cursor()
    if direct_only is None:
        cursor.execute("""
        DELETE FROM flight_cache
        WHERE origin = ? AND destination = ? AND depart_date LIKE ?
        """, (origin, destination, f"{month}%"))
    else:
        cursor.execute("""
        DELETE FROM flight_cache
        WHERE origin = ? AND destination = ? AND depart_date LIKE ? AND direct_only = ?
        """, (origin, destination, f"{month}%", direct_only))
    conn.commit()
    conn.close()

def save_flight_cache(origin: str, destination: str, depart_date: str, departure_at: str, price: float, 
                      airline: str, flight_number: str, transfers_count: int, duration: int, direct_only: int,
                      fetched_at: str = None, expires_at: str = None):
    conn = get_db_connection()
    cursor = conn.cursor()
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if fetched_at is None:
        fetched_at = updated_at
    cursor.execute("""
    INSERT INTO flight_cache (
        origin, destination, depart_date, departure_at, price, 
        airline, flight_number, transfers_count, duration, direct_only,
        updated_at, fetched_at, expires_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(origin, destination, departure_at, direct_only) DO UPDATE SET
        price = excluded.price,
        airline = excluded.airline,
        flight_number = excluded.flight_number,
        transfers_count = excluded.transfers_count,
        duration = excluded.duration,
        direct_only = excluded.direct_only,
        updated_at = excluded.updated_at,
        fetched_at = excluded.fetched_at,
        expires_at = excluded.expires_at
    """, (origin, destination, depart_date, departure_at, price, airline, flight_number,
          transfers_count, duration, direct_only, updated_at, fetched_at, expires_at))
    conn.commit()
    conn.close()

# Transit Hubs & Manual Legs Getter
def get_all_transit_hubs():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM transit_hubs")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_all_manual_legs():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM manual_legs")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def clear_price_cache_for_edges(edges: list[tuple[str, str]], months: list[str]):
    """Удаляет записи flight_cache и route_query_log для указанных рёбер и месяцев."""
    if not edges or not months:
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        for origin, dest in edges:
            for month in months:
                cursor.execute("""
                DELETE FROM flight_cache
                WHERE origin = ? AND destination = ? AND depart_date LIKE ?
                """, (origin, dest, f"{month}%"))
                cursor.execute("""
                DELETE FROM route_query_log
                WHERE origin = ? AND destination = ? AND month = ?
                """, (origin, dest, month))
        conn.commit()
        logger.info("Очищен кэш цен: %d рёбер × %d месяцев", len(edges), len(months))
    finally:
        conn.close()

def get_cache_status_for_search(search_id: int, destination_iatas: list[str] = None,
                                edges: list[tuple[str, str]] = None, months: list[str] = None,
                                ttl_hours: int = 24) -> dict:
    """Возвращает информацию о свежести кэша для поиска: общее число сегментов, устаревшие, старейший/новейший fetched_at."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM user_searches WHERE id = ?", (search_id,))
        search = cursor.fetchone()
        if not search:
            return {"error": "search_not_found", "search_id": search_id}
        search = dict(search)

        origin = search["origin_iata"]
        stopovers = json.loads(search.get("stopovers_json") or "[]")
        dest = search["destination_text"]

        if not destination_iatas:
            dest_upper = dest.upper()
            if len(dest_upper) == 3 and dest_upper.isalpha():
                destination_iatas = [dest_upper]
            else:
                fallback_catalog = {
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
                }
                destination_iatas = fallback_catalog.get(dest_upper, [dest_upper])

        # Fallback edge reconstruction. Prefer passing real discovery/snapshot edges from bot.py.
        if edges is None:
            edges = []
            if stopovers:
                edges.append((origin, stopovers[0]))
                for i in range(len(stopovers) - 1):
                    edges.append((stopovers[i], stopovers[i + 1]))
                for d_iata in destination_iatas:
                    edges.append((stopovers[-1], d_iata))
            else:
                for d_iata in destination_iatas:
                    edges.append((origin, d_iata))

        # Определяем месяцы из диапазона дат
        date_start = search["date_start"]
        date_end = search["date_end"]
        if months is None:
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
            except ValueError:
                months = set()
        else:
            months = set(months)

        if not edges or not months:
            return {
                "search_id": search_id,
                "total_cached": 0,
                "stale_count": 0,
                "oldest_fetched_at": None,
                "newest_fetched_at": None,
            }

        total_cached = 0
        stale_count = 0
        oldest = None
        newest = None
        stale_threshold = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).strftime("%Y-%m-%d %H:%M:%S")

        for o, d in edges:
            for month in months:
                cursor.execute("""
                SELECT fetched_at FROM flight_cache
                WHERE origin = ? AND destination = ? AND depart_date LIKE ?
                """, (o, d, f"{month}%"))
                for row in cursor.fetchall():
                    total_cached += 1
                    fa = row["fetched_at"]
                    if fa is None or fa < stale_threshold:
                        stale_count += 1
                    if fa is not None:
                        if oldest is None or fa < oldest:
                            oldest = fa
                        if newest is None or fa > newest:
                            newest = fa

        return {
            "search_id": search_id,
            "total_cached": total_cached,
            "stale_count": stale_count,
            "oldest_fetched_at": oldest,
            "newest_fetched_at": newest,
        }
    finally:
        conn.close()

if __name__ == "__main__":
    init_db()
