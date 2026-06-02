import sqlite3
import json
import logging
from datetime import datetime
from config import DATABASE_PATH

logger = logging.getLogger("db")

def get_db_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

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
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(user_id)
    )
    """)
    
    # 3. Flight Cache Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS flight_cache (
        origin TEXT NOT NULL,
        destination TEXT NOT NULL,
        depart_date TEXT NOT NULL,
        price REAL NOT NULL,
        airline TEXT,
        transfers_count INTEGER DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (origin, destination, depart_date)
    )
    """)
    
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
                     max_transfers: int, visa_allowed: int, lodging_exceptions: dict, max_budget: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO user_searches (user_id, origin_iata, destination_text, date_start, date_end, max_transfers, visa_allowed, lodging_exceptions_json, max_budget)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, origin_iata, destination_text, date_start, date_end, max_transfers, visa_allowed, json.dumps(lodging_exceptions), max_budget))
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
    # Expire cache after 24 hours
    cursor.execute("""
    SELECT * FROM flight_cache 
    WHERE origin = ? AND destination = ? AND depart_date = ?
    AND datetime(updated_at) >= datetime('now', '-24 hours')
    """, (origin, destination, depart_date))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def save_flight_cache(origin: str, destination: str, depart_date: str, price: float, airline: str, transfers_count: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    updated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
    INSERT INTO flight_cache (origin, destination, depart_date, price, airline, transfers_count, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(origin, destination, depart_date) DO UPDATE SET
        price = excluded.price,
        airline = excluded.airline,
        transfers_count = excluded.transfers_count,
        updated_at = excluded.updated_at
    """, (origin, destination, depart_date, price, airline, transfers_count, updated_at))
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
    # Test initialization
    init_db()
