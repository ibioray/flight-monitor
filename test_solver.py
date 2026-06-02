import asyncio
import json
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

from db import (
    init_db, save_flight_cache, save_search_snapshot,
    get_route_snapshot, get_snapshot_routes, save_discovery_cache,
    get_discovery_cache
)
from solver import GraphSolver
from analyst import LLMCognitiveAnalyst
from discovery import RouteDiscoveryService

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
    assert results["fastest"][0]["segments"][0]["destination"] == "PEK", "Fastest route should be the direct UFA -> PEK route by hours."
    assert any(len(r["segments"]) == 1 and r["segments"][0]["destination"] == "PEK" for r in results["recommended"]), "Direct route must be visible in recommended routes."
    assert len(results["recommended"]) >= 5, "Recommended output should show at least 5 diverse routes when available."

    # Regression: solver must ignore unrelated flights saved in the global DB cache
    save_flight_cache("UFA", "CAN", "2026-06-19", "2026-06-19T09:00:00+05:00", 100.0, "XX", "LEAK", 0, 300, 1)
    no_leak_results = solver.solve(
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
    leaked = any(
        any(leg["destination"] == "CAN" and leg.get("flight_number") == "LEAK" for leg in route["segments"])
        for route in no_leak_results["recommended"]
    )
    assert not leaked, "Solver leaked a flight from global flight_cache into current-run results."
    
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
        "total_routes_found": results.get("total_routes_found_before_filter", 0),
        "total_routes_after_filter": results.get("total_routes_after_filter", 0),
        "rendered_routes_count": results.get("rendered_routes_count", 0),
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

    for route in results["recommended"]:
        assert route["route_id"] in analysis_report, "Deterministic analysis must render every recommended route."

    snapshot_id = save_search_snapshot(
        user_id=42,
        search_id=7,
        search_config={
            "origin_iata": origin,
            "dest_iata": destination_country,
            "date_start": date_start,
            "date_end": date_end,
        },
        metadata=mock_metadata,
        solved_data=results
    )
    assert snapshot_id > 0, "Search snapshot should be persisted."

    first_route_id = results["recommended"][0]["route_id"]
    route_row = get_route_snapshot(42, first_route_id)
    assert route_row is not None, "Route snapshot should be retrievable by route_id."
    assert json.loads(route_row["route_json"])["route_id"] == first_route_id

    snapshot, price_rows = get_snapshot_routes(42, sort_mode="price")
    assert snapshot is not None and len(price_rows) >= 5, "Snapshot routes should be pageable."
    prices = [json.loads(row["route_json"])["total_price"] for row in price_rows]
    assert prices == sorted(prices), "routes_by_price should sort by total price."

    _, more_rows = get_snapshot_routes(42, offset=5, limit=5, sort_mode="balanced")
    assert len(more_rows) > 0, "more_routes should return routes beyond the first page when available."
    
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
    fallback_metadata["total_routes_found"] = fallback_results.get("total_routes_found_before_filter", 0)
    fallback_metadata["total_routes_after_filter"] = fallback_results.get("total_routes_after_filter", 0)
    fallback_metadata["rendered_routes_count"] = fallback_results.get("rendered_routes_count", 0)
    
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

    # 6. Discovery should not expand one proven backward hub to all destination airports
    class FakeProvider:
        async def get_outbound_directions(self, origin_code, month):
            if origin_code == "UFA":
                return [{"origin": "UFA", "destination": "ALA", "transfers": 0}]
            return []

        async def get_inbound_directions(self, destination_code, month):
            if destination_code == "URC":
                return [{"origin": "ALA", "destination": "URC", "transfers": 0}]
            return []

    discovery = RouteDiscoveryService(FakeProvider())
    edges = await discovery.discover_candidate_edges(
        origin="UFA",
        destination_country="CN",
        destination_iatas=["PEK", "PVG", "URC"],
        months=["2026-06"],
        max_transfers=1,
    )
    assert ("ALA", "URC") in edges, "Proven backward edge should be included."
    assert ("ALA", "PEK") not in edges and ("ALA", "PVG") not in edges, "Backward hub must not be expanded to every destination airport."

    save_discovery_cache(
        origin="UFA",
        destination_country="CN",
        destination_iatas=["PEK", "PVG", "URC"],
        months=["2026-06"],
        max_transfers=1,
        edges=edges
    )
    cached_edges = get_discovery_cache(
        origin="UFA",
        destination_country="CN",
        destination_iatas=["URC", "PEK", "PVG"],
        months=["2026-06"],
        max_transfers=1,
    )
    assert cached_edges == edges, "Discovery cache should restore the same edge set with stable destination ordering."
    assert get_discovery_cache("UFA", "CN", ["PEK", "PVG", "URC"], ["2026-06"], max_transfers=2) is None, "Discovery cache must be separated by max_transfers."
    
    # Clean up test DB after run
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
        
    logger.info("Test finished successfully!")

if __name__ == "__main__":
    asyncio.run(run_test())
