import sqlite3
import json
import logging
from datetime import datetime, timezone, timedelta
from config import DATABASE_PATH

logger = logging.getLogger("db")

QUERY_LOG_PROVIDER = "travelpayouts_prices_for_dates_v3"
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
        PRIMARY KEY (origin, destination, departure_at, direct_only)
    )
    """)

    departure_expr = "departure_at" if "departure_at" in existing_columns else "depart_date || 'T00:00:00Z'"
    flight_number_expr = "flight_number" if "flight_number" in existing_columns else "NULL"
    duration_expr = "duration" if "duration" in existing_columns else "0"
    direct_only_expr = "direct_only" if "direct_only" in existing_columns else "0"

    cursor.execute(f"""
    INSERT OR REPLACE INTO flight_cache_new (
        origin, destination, depart_date, departure_at, price,
        airline, flight_number, transfers_count, duration, direct_only, updated_at
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
        updated_at
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
        daily_lodging_rub REAL DEFAULT 0,
        requires_visa_for_ru INTEGER DEFAULT 0,
        passport_type_required TEXT DEFAULT 'internal' -- 'internal' or 'foreign'
    )
    """)
    
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
    
    conn.commit()
    
    # Seed default transit hubs
    default_hubs = [
        # Internal passport, visa-free
        ("MOW", "Москва", 4000, 0, "internal"),
        ("EVN", "Ереван", 0, 0, "internal"), # Custom request: 0 lodging cost for EVN
        ("ALA", "Алматы", 3500, 0, "internal"),
        ("NQZ", "Астана", 3500, 0, "internal"),
        ("FRU", "Бишкек", 2500, 0, "internal"),
        
        # Russian hubs (internal passport)
        ("SVX", "Екатеринбург", 3000, 0, "internal"),
        ("KZN", "Казань", 3500, 0, "internal"),
        ("OVB", "Новосибирск", 3000, 0, "internal"),
        
        # Foreign passport, visa-free
        ("TAS", "Ташкент", 3000, 0, "foreign"),
        ("BAK", "Баку", 4000, 0, "foreign"),
        ("IST", "Стамбул", 7000, 0, "foreign"),
        ("DXB", "Дубай", 10000, 0, "foreign"),
        ("AUH", "Абу-Даби", 8000, 0, "foreign"),
        ("DOH", "Доха", 9000, 0, "foreign"),
    ]
    
    for hub in default_hubs:
        cursor.execute("""
        INSERT OR IGNORE INTO transit_hubs (iata, city_name, daily_lodging_rub, requires_visa_for_ru, passport_type_required)
        VALUES (?, ?, ?, ?, ?)
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
                     stopovers: list = None, exclusions: list = None, baggage_needed: int = 0):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO user_searches (
        user_id, origin_iata, destination_text, date_start, date_end, 
        max_transfers, visa_allowed, lodging_exceptions_json, max_budget,
        stopovers_json, exclusions_json, baggage_needed
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id, origin_iata, destination_text, date_start, date_end, 
        max_transfers, visa_allowed, json.dumps(lodging_exceptions), max_budget,
        json.dumps(stopovers or []), json.dumps(exclusions or []), baggage_needed
    ))
    conn.commit()
    conn.close()

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
    cursor.execute("UPDATE user_searches SET last_checked_price = ? WHERE id = ?", (price, search_id))
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
    one_way: int = QUERY_LOG_ONE_WAY,
    currency: str = QUERY_LOG_CURRENCY,
    market: str = QUERY_LOG_MARKET,
    provider: str = QUERY_LOG_PROVIDER,
) -> bool:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT 1 FROM route_query_log
    WHERE provider = ?
    AND origin = ? AND destination = ? AND month = ? AND direct_only = ?
    AND one_way = ? AND currency = ? AND market = ?
    AND datetime(queried_at) >= datetime('now', '-24 hours')
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

def save_flight_cache(origin: str, destination: str, depart_date: str, departure_at: str, price: float, 
                      airline: str, flight_number: str, transfers_count: int, duration: int, direct_only: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
    INSERT INTO flight_cache (
        origin, destination, depart_date, departure_at, price, 
        airline, flight_number, transfers_count, duration, direct_only, updated_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(origin, destination, departure_at, direct_only) DO UPDATE SET
        price = excluded.price,
        airline = excluded.airline,
        flight_number = excluded.flight_number,
        transfers_count = excluded.transfers_count,
        duration = excluded.duration,
        direct_only = excluded.direct_only,
        updated_at = excluded.updated_at
    """, (origin, destination, depart_date, departure_at, price, airline, flight_number, transfers_count, duration, direct_only, updated_at))
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

if __name__ == "__main__":
    init_db()
