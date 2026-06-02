import asyncio
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone, timedelta

# Force isolated database for tests (Codex I)
TEST_DB_PATH = os.path.join(tempfile.gettempdir(), "test_flight_monitor.db")
os.environ["DATABASE_PATH"] = TEST_DB_PATH

# Now import the modules which use config.DATABASE_PATH
import config
config.DATABASE_PATH = TEST_DB_PATH

from db import init_db, save_flight_cache
from solver import GraphSolver
from analyst import LLMCognitiveAnalyst

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_solver")

def populate_mock_data():
    logger.info("Seeding mock flight data for testing solver...")
    
    # Format of segment in memory:
    # origin, destination, depart_date, departure_at, price, airline, flight_number, transfers_count, duration, direct_only
    flights = [
        # Route 1: UFA -> MOW -> PEK (2 days stopover in Moscow)
        # UFA -> MOW departs June 15 at 05:00, arrives at 08:00 (duration 180 min). Price: 5000
        ("UFA", "MOW", "2026-06-15", "2026-06-15T05:00:00+05:00", 5000.0, "SU", "123", 0, 180, 1),
        # MOW -> PEK departs June 17 at 12:00, arrives at 20:00 (duration 480 min). Price: 20000
        ("MOW", "PEK", "2026-06-17", "2026-06-17T12:00:00+03:00", 20000.0, "CA", "456", 0, 480, 1),
        
        # Route 2: UFA -> ALA -> PEK (4 days stopover in Almaty)
        ("UFA", "ALA", "2026-06-16", "2026-06-16T10:00:00+06:00", 10000.0, "KC", "789", 0, 240, 1),
        ("ALA", "PEK", "2026-06-20", "2026-06-20T14:00:00+06:00", 15000.0, "KC", "987", 0, 360, 1),
        
        # Route 3: UFA -> EVN -> PEK (3 days stopover in Yerevan, lodging cost = 0)
        ("UFA", "EVN", "2026-06-15", "2026-06-15T06:00:00+05:00", 8000.0, "RM", "111", 0, 240, 1),
        ("EVN", "PEK", "2026-06-18", "2026-06-18T11:00:00+04:00", 18000.0, "HU", "222", 0, 480, 1),
        
        # Route 4: Direct flight UFA -> PEK
        ("UFA", "PEK", "2026-06-22", "2026-06-22T08:00:00+05:00", 45000.0, "CA", "333", 0, 420, 1),
        
        # Leg for 3-hop transit: UFA -> MOW -> ALA -> PEK
        ("MOW", "ALA", "2026-06-16", "2026-06-16T22:00:00+03:00", 8000.0, "SU", "444", 0, 240, 1),
    ]
    
    priced_flights = []
    for origin, dest, date, dep_at, price, airline, fn, transfers, duration, direct in flights:
        # Save to database cache to simulate cache seeding
        save_flight_cache(origin, dest, date, dep_at, price, airline, fn, transfers, duration, direct)
        # Add to the priced_flights in-memory list
        priced_flights.append({
            "origin": origin,
            "destination": dest,
            "depart_date": date,
            "departure_at": dep_at,
            "price": price,
            "airline": airline,
            "flight_number": fn,
            "transfers_count": transfers,
            "duration": duration
        })
        
    logger.info("Mock flight data seeded successfully.")
    return priced_flights

async def run_test():
    # Clean old test DB if any
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
        
    # 1. Init Database tables in isolated path (Codex I)
    init_db()
    
    # 2. Populate DB and construct in-memory priced_flights
    priced_flights = populate_mock_data()
    
    # 3. Solver test
    logger.info("Initializing GraphSolver...")
    solver = GraphSolver()
    
    origin = "UFA"
    destination_country = "CN"
    china_airports = ["PEK", "PVG", "CAN", "CTU", "URC"]
    date_start = "2026-06-15"
    date_end = "2026-06-30"
    max_budget = 50000.0
    
    logger.info("Solving route graph...")
    # Solver receives priced_flights directly in memory (Codex E)
    results = solver.solve(
        origin_iata=origin,
        destination_country_code=destination_country,
        destination_iatas=china_airports,
        date_start_str=date_start,
        date_end_str=date_end,
        priced_flights=priced_flights,
        max_transfers=3,
        visa_allowed=1,
        lodging_exceptions={"EVN": 0.0},
        max_budget=max_budget,
        baggage_needed=0,
        stopovers_pref=[],
        exclusions=[]
    )
    
    # Validate result structure
    logger.info(f"Solved cheapest routes count: {len(results['cheapest'])}")
    logger.info(f"Solved fastest routes count: {len(results['fastest'])}")
    logger.info(f"Solved stopover routes count: {len(results['stopovers'])}")
    
    assert len(results["cheapest"]) > 0, "Error: Solver failed to find routes!"
    assert results["is_fallback_active"] is False, "Error: Fallback should not be active for 50,000 budget!"
    
    # Print the cheapest path found
    best_route = results["cheapest"][0]
    chain = " ➔ ".join([f"{leg['origin']} -> {leg['destination']} ({leg['departure_at']}, {leg['price']:.0f} ₽)" for leg in best_route["segments"]])
    logger.info(f"Best cheapest route (Total Cost: {best_route['total_price']:.0f} ₽, Tickets: {best_route['base_price']:.0f} ₽, Lodging: {best_route['lodging_price']:.0f} ₽):")
    logger.info(f"  {chain}")
    
    # 4. LLM Analyst test with search metadata (Force mock mode to isolate test, DOP-6)
    logger.info("Testing LLMCognitiveAnalyst in mock mode (no network requests)...")
    analyst = LLMCognitiveAnalyst(api_key="your_gemini_api_key_here", openrouter_key="")
    
    mock_metadata = {
        "hubs": ["MOW", "ALA", "EVN"],
        "segments_count": 12,
        "priced_segments_count": len(priced_flights),
        "total_routes_found": 8,
        "is_fallback_active": results.get("is_fallback_active", False),
        "max_transfers": 3,
        "destination_iatas": china_airports
    }
    
    analysis_report = await analyst.analyze_routes(
        origin=origin,
        destination=destination_country,
        date_range=f"{date_start} — {date_end}",
        max_budget=max_budget,
        solved_data=results,
        search_metadata=mock_metadata
    )
    
    print("\n" + "=" * 40 + " STANDARD ANALYSIS REPORT " + "=" * 40)
    print(analysis_report)
    print("=" * 97 + "\n")
    
    # 5. Low Budget Fallback Test
    logger.info("Testing solver fallback for low budget (5,000 RUB)...")
    fallback_results = solver.solve(
        origin_iata=origin,
        destination_country_code=destination_country,
        destination_iatas=china_airports,
        date_start_str=date_start,
        date_end_str=date_end,
        priced_flights=priced_flights,
        max_transfers=3,
        visa_allowed=1,
        lodging_exceptions={"EVN": 0.0},
        max_budget=5000.0,
        baggage_needed=0,
        stopovers_pref=[],
        exclusions=[]
    )
    
    assert fallback_results["is_fallback_active"] is True, "Error: Fallback should be active for low budget!"
    logger.info(f"Fallback active: {fallback_results['is_fallback_active']}")
    logger.info(f"Routes found in fallback: {len(fallback_results['cheapest'])}")
    
    # Analyze fallback results
    fallback_metadata = dict(mock_metadata)
    fallback_metadata["is_fallback_active"] = True
    
    fallback_report = await analyst.analyze_routes(
        origin=origin,
        destination=destination_country,
        date_range=f"{date_start} — {date_end}",
        max_budget=5000.0,
        solved_data=fallback_results,
        search_metadata=fallback_metadata
    )
    
    print("\n" + "=" * 40 + " FALLBACK ANALYSIS REPORT " + "=" * 40)
    print(fallback_report)
    print("=" * 97 + "\n")
    
    # Clean up test DB after run
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
        
    logger.info("Test finished successfully!")

if __name__ == "__main__":
    asyncio.run(run_test())
