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
    get_discovery_cache, get_user_searches, subscribe_route,
    get_user_route_subscriptions, deactivate_route_subscription
)
from solver import GraphSolver
from analyst import LLMCognitiveAnalyst
from discovery import RouteDiscoveryService
from monitoring import price_drop_alert_decision
from providers import _absolute_aviasales_link, _parse_changes_count
from airport_names import format_iata_city

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
    assert "ALA (Алматы)" in analysis_report, "Renderer should show city names next to IATA codes."
    assert "URC (Урумчи)" in analysis_report, "Renderer should show destination city names next to IATA codes."
    assert format_iata_city("KJA") == "KJA (Красноярск)", "KJA should be labeled as Krasnoyarsk."
    assert "###" not in analysis_report and "**" not in analysis_report, "Telegram report must not use headings or double-star Markdown."
    assert _parse_changes_count({"transfers": 0}) == 0
    assert _parse_changes_count({"number_of_changes": 1}) == 1
    assert _parse_changes_count({}) is None
    assert _absolute_aviasales_link("/search/UFA0306KJA1") == "https://www.aviasales.ru/search/UFA0306KJA1"

    renderer_probe = dict(results["recommended"][0])
    renderer_probe["badges"] = ["Тест ## badge"]
    renderer_probe["risk_warnings"] = ["Риск **markdown** #tag"]
    rendered_probe = analyst.renderer.render_summary(
        {"recommended": [renderer_probe], "is_fallback_active": False},
        {"destination_iatas": china_airports, "hubs": ["MOW"], "visa_mode": "warn"}
    )
    assert "#" not in rendered_probe, "Renderer must sanitize heading/hash characters from dynamic content."
    assert "**" not in rendered_probe, "Renderer must sanitize double-star Markdown from dynamic content."
    assert renderer_probe["route_id"] in rendered_probe, "Renderer must preserve route_id."

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

    sub = subscribe_route(42, 4242, first_route_id, search_id=7, threshold_pct=8)
    assert sub is not None, "Route subscription should be created from a saved route snapshot."
    assert sub["route_id"] == first_route_id
    assert float(sub["last_checked_price"]) == results["recommended"][0]["total_price"]
    route_subs = get_user_route_subscriptions(42)
    assert len(route_subs) == 1 and route_subs[0]["route_id"] == first_route_id
    assert deactivate_route_subscription(sub["id"], 42), "Route subscription should be deactivated by owner."
    assert not get_user_route_subscriptions(42), "Inactive route subscriptions should be hidden."

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

    # Discovery bridge edges must be target-specific, not a generic all-hubs mesh.
    class TargetSpecificProvider:
        async def get_outbound_directions(self, origin_code, month):
            routes_by_origin = {
                "UFA": [
                    {"origin": "UFA", "destination": "ALA", "transfers": 0},
                    {"origin": "UFA", "destination": "SCO", "transfers": 0},
                ],
                "ALA": [
                    {"origin": "ALA", "destination": "HRB", "transfers": 0},
                    {"origin": "ALA", "destination": "HAN", "transfers": 0},
                ],
                "SCO": [
                    {"origin": "SCO", "destination": "HAN", "transfers": 0},
                ],
            }
            return routes_by_origin.get(origin_code, [])

        async def get_inbound_directions(self, destination_code, month):
            routes_by_destination = {
                "URC": [{"origin": "HRB", "destination": "URC", "transfers": 0}],
                "SGN": [{"origin": "HAN", "destination": "SGN", "transfers": 0}],
            }
            return routes_by_destination.get(destination_code, [])

    target_discovery = RouteDiscoveryService(TargetSpecificProvider())
    cn_edges, cn_diag = await target_discovery.discover_candidate_edges_with_diagnostics(
        origin="UFA",
        destination_country="CN",
        destination_iatas=["URC"],
        months=["2026-06"],
        max_transfers=2,
    )
    vn_edges, vn_diag = await target_discovery.discover_candidate_edges_with_diagnostics(
        origin="UFA",
        destination_country="VN",
        destination_iatas=["SGN"],
        months=["2026-06"],
        max_transfers=2,
    )
    assert ("ALA", "HRB") in cn_edges, "China bridge should include ALA -> HRB."
    assert ("ALA", "HAN") not in cn_edges, "China bridge should not include Vietnam-only target-side hub."
    assert ("ALA", "HAN") in vn_edges and ("SCO", "HAN") in vn_edges, "Vietnam bridges should target HAN."
    assert cn_edges != vn_edges, "Different target countries should not collapse into the same generic graph."
    assert cn_diag["edge_categories"]["bridge"] == 1, "China bridge diagnostics should be precise."
    assert vn_diag["edge_categories"]["bridge"] == 2, "Vietnam bridge diagnostics should be precise."

    # 7. Rate Limiter Test
    logger.info("Testing AsyncRateLimiter...")
    from providers import AsyncRateLimiter
    limiter = AsyncRateLimiter(target_rps=10.0, burst=2)
    start_time = asyncio.get_event_loop().time()
    for _ in range(5):
        await limiter.acquire()
        limiter.release()
    end_time = asyncio.get_event_loop().time()
    elapsed = end_time - start_time
    logger.info(f"Rate Limiter elapsed time for 5 acquires: {elapsed:.2f}s")
    assert elapsed >= 0.2, f"Rate limiter failed to delay. Elapsed: {elapsed}"

    # 8. Cache Freshness Round-trip and Mode Test
    logger.info("Testing Cache Freshness and Modes...")
    from db import get_cache_status_for_search, save_user_search, get_cached_flights

    fetched_at = "2026-06-03 00:00:00"
    expires_at = "2026-06-04 00:00:00"
    save_flight_cache(
        origin="UFA",
        destination="MOW",
        depart_date="2026-06-15",
        departure_at="2026-06-15T05:00:00+05:00",
        price=5000.0,
        airline="SU",
        flight_number="123",
        transfers_count=0,
        duration=180,
        direct_only=1,
        fetched_at=fetched_at,
        expires_at=expires_at
    )

    cached = get_cached_flights("UFA", "MOW", "2026-06", direct_only=1)
    assert len(cached) > 0
    assert cached[0]["fetched_at"] == fetched_at
    assert cached[0]["expires_at"] == expires_at

    search_id_overview = save_user_search(
        user_id=42, origin_iata="UFA", destination_text="CN", date_start="2026-06-15", date_end="2026-06-30",
        max_transfers=2, visa_allowed=1, lodging_exceptions={}, max_budget=50000,
        stopovers=["MOW"], exclusions=[], baggage_needed=0,
        cache_mode="overview",
        min_stopover_hours=24,
        max_stopover_days=3,
        stopover_preset="walk",
        allow_awkward_layovers=0,
        visa_mode="visa_free_only"
    )
    saved_search = [s for s in get_user_searches(42) if s["id"] == search_id_overview][0]
    assert saved_search["stopover_preset"] == "walk"
    assert saved_search["min_stopover_hours"] == 24
    assert saved_search["max_stopover_days"] == 3
    assert saved_search["allow_awkward_layovers"] == 0
    assert saved_search["visa_mode"] == "visa_free_only"
    assert float(saved_search["price_drop_threshold_pct"]) == 10.0

    status = get_cache_status_for_search(search_id_overview, ["PEK"])
    assert status["total_cached"] > 0
    logger.info(f"Cache status retrieved: {status}")

    # 9. Stopover Presets and Filtering Test
    logger.info("Testing Stopover Presets and Filtering...")
    fast_results = solver.solve(
        origin_iata=origin,
        destination_country_code=destination_country,
        destination_iatas=china_airports,
        date_start_str=date_start,
        date_end_str=date_end,
        priced_flights=priced_flights,
        max_transfers=3,
        visa_allowed=1,
        max_budget=max_budget,
        stopover_preset="fast"
    )
    for route in fast_results["recommended"]:
        for stop in route["stopovers"]:
            if stop["city"] == "MOW":
                assert stop["layover_hours"] <= 24.0, f"Route with >24h stopover {stop['layover_hours']} allowed in fast preset."
            assert stop["layover_type"] != "awkward", "Fast preset must filter awkward layovers."

    walk_results = solver.solve(
        origin_iata=origin,
        destination_country_code=destination_country,
        destination_iatas=china_airports,
        date_start_str=date_start,
        date_end_str=date_end,
        priced_flights=priced_flights,
        max_transfers=3,
        visa_allowed=1,
        max_budget=max_budget,
        stopover_preset="walk"
    )
    for route in walk_results["stopovers"]:
        has_valid_layover = any(stop["layover_hours"] >= 24.0 for stop in route["stopovers"])
        assert has_valid_layover, "Stopover route without >=24h layover returned under 'walk' preset."
        assert all(stop["layover_type"] != "awkward" for stop in route["stopovers"]), "Walk preset must filter awkward layovers."

    strict_results = solver.solve(
        origin_iata=origin,
        destination_country_code=destination_country,
        destination_iatas=china_airports,
        date_start_str=date_start,
        date_end_str=date_end,
        priced_flights=priced_flights,
        max_transfers=3,
        visa_allowed=1,
        max_budget=max_budget,
        allow_awkward_layovers=0
    )
    for route in strict_results["ranked_routes"]:
        assert all(stop["layover_type"] != "awkward" for stop in route["stopovers"]), "Explicit awkward layover ban must filter all awkward routes."

    # 10. Visa mode and country-code blacklist test
    logger.info("Testing Visa Mode and Country Exclusions...")
    visa_flights = list(priced_flights) + [
        {
            "origin": "UFA",
            "destination": "TYO",
            "depart_date": "2026-06-15",
            "departure_at": "2026-06-15T08:00:00+05:00",
            "price": 6000.0,
            "airline": "JL",
            "flight_number": "VISA1",
            "transfers_count": 0,
            "duration": 600
        },
        {
            "origin": "TYO",
            "destination": "PEK",
            "depart_date": "2026-06-16",
            "departure_at": "2026-06-16T18:00:00+09:00",
            "price": 6000.0,
            "airline": "JL",
            "flight_number": "VISA2",
            "transfers_count": 0,
            "duration": 240
        },
    ]
    visa_free_results = solver.solve(
        origin_iata=origin,
        destination_country_code=destination_country,
        destination_iatas=china_airports,
        date_start_str=date_start,
        date_end_str=date_end,
        priced_flights=visa_flights,
        max_transfers=2,
        visa_allowed=0,
        max_budget=100000,
        visa_mode="visa_free_only"
    )
    assert not any(
        any(leg["origin"] == "TYO" or leg["destination"] == "TYO" for leg in route["segments"])
        for route in visa_free_results["ranked_routes"]
    ), "visa_free_only must filter known visa-required transit hubs."

    warn_results = solver.solve(
        origin_iata=origin,
        destination_country_code=destination_country,
        destination_iatas=china_airports,
        date_start_str=date_start,
        date_end_str=date_end,
        priced_flights=visa_flights,
        max_transfers=2,
        visa_allowed=1,
        max_budget=100000,
        visa_mode="warn"
    )
    visa_warning_routes = [
        route for route in warn_results["ranked_routes"]
        if any("Визовый риск" in warning for warning in route["risk_warnings"])
    ]
    assert visa_warning_routes, "warn visa mode should keep visa-risk routes and add visible warnings."

    excluded_country_results = solver.solve(
        origin_iata=origin,
        destination_country_code=destination_country,
        destination_iatas=china_airports,
        date_start_str=date_start,
        date_end_str=date_end,
        priced_flights=visa_flights,
        max_transfers=2,
        visa_allowed=1,
        max_budget=100000,
        visa_mode="ignore",
        exclusions=["JP"]
    )
    assert not any(
        any(leg["origin"] == "TYO" or leg["destination"] == "TYO" for leg in route["segments"])
        for route in excluded_country_results["ranked_routes"]
    ), "Country-code exclusions should remove transit hubs in that country."

    # 10b. Hub include/exclude across MULTI-transfer (2-transfer / 3-leg) routes.
    # Critical: the filter must apply to EVERY intermediate hub, not just the first.
    logger.info("Testing hub whitelist/blacklist on 1- and 2-transfer routes...")

    def _f(o, d, date, price, dur=300):
        return {
            "origin": o, "destination": d,
            "depart_date": date, "departure_at": f"{date}T08:00:00Z",
            "price": float(price), "airline": "XX", "flight_number": f"{o}{d}",
            "transfers_count": 0, "duration": dur,
        }

    # Graph to PEK:
    #   UFA->ALA->PEK            (1 transfer, hub ALA)
    #   UFA->TAS->PEK            (1 transfer, hub TAS)
    #   UFA->IST->TAS->PEK       (2 transfers, hubs IST + TAS)
    hub_flights = [
        _f("UFA", "ALA", "2026-06-10", 9000), _f("ALA", "PEK", "2026-06-11", 12000),
        _f("UFA", "TAS", "2026-06-10", 8000), _f("TAS", "PEK", "2026-06-12", 13000),
        _f("UFA", "IST", "2026-06-10", 7000), _f("IST", "TAS", "2026-06-11", 6000),
    ]
    hub_common = dict(
        origin_iata="UFA", destination_country_code="CN", destination_iatas=["PEK"],
        date_start_str="2026-06-10", date_end_str="2026-06-20", priced_flights=hub_flights,
        max_transfers=3, visa_allowed=1, max_budget=100000, visa_mode="ignore",
        min_stopover_hours=0, max_stopover_days=6, allow_awkward_layovers=1,
    )

    def _hubs_used(result):
        used = set()
        for route in result["ranked_routes"]:
            for leg in route["segments"]:
                used.add(leg["origin"]); used.add(leg["destination"])
        return used

    # Baseline: all three routes discoverable.
    base = solver.solve(**hub_common)
    assert any(len(r["segments"]) == 3 for r in base["ranked_routes"]), "2-transfer route must be found at baseline."

    # Whitelist a single hub that only appears on the 1-transfer route -> 2-transfer route gone.
    only_ala = solver.solve(**hub_common, allowed_hubs=["ALA"])
    used = _hubs_used(only_ala)
    assert "ALA" in used, "Whitelisted hub ALA must remain."
    assert "TAS" not in used and "IST" not in used, "Whitelist must drop all non-whitelisted hubs (incl. on 2-transfer routes)."

    # Whitelist TAS only: 1-transfer UFA->TAS->PEK ok, but 2-transfer UFA->IST->TAS->PEK
    # must be dropped because IST (an intermediate hub) is not whitelisted.
    only_tas = solver.solve(**hub_common, allowed_hubs=["TAS"])
    assert not any(
        any(leg["origin"] == "IST" or leg["destination"] == "IST" for leg in r["segments"])
        for r in only_tas["ranked_routes"]
    ), "Whitelist must reject a 2-transfer route if ANY intermediate hub is not whitelisted."
    assert any(len(r["segments"]) == 2 for r in only_tas["ranked_routes"]), "1-transfer route via TAS must survive."

    # Whitelist BOTH hubs of the 2-transfer route -> it must survive.
    ist_tas = solver.solve(**hub_common, allowed_hubs=["IST", "TAS"])
    assert any(len(r["segments"]) == 3 for r in ist_tas["ranked_routes"]), "2-transfer route must survive when all its hubs are whitelisted."

    # Blacklist TAS: every route through TAS (1- and 2-transfer) must be gone.
    no_tas = solver.solve(**hub_common, exclusions=["TAS"])
    assert not any(
        any(leg["origin"] == "TAS" or leg["destination"] == "TAS" for leg in r["segments"])
        for r in no_tas["ranked_routes"]
    ), "Blacklist must remove TAS from 1- and 2-transfer routes alike."

    # 11. Price-drop monitoring decision test
    logger.info("Testing price-drop monitoring decisions...")
    alert_decision = price_drop_alert_decision(last_price=50000, current_price=45500, threshold_pct=8)
    assert alert_decision["should_alert"], "A 9% drop should alert when threshold is 8%."
    assert alert_decision["should_update_baseline"], "Complete monitoring data should update the baseline."

    # Audit H2: a sub-threshold drop must NOT ratchet the baseline down, otherwise a
    # slow decline (e.g. 5%+5%+5%) would never cross the alert threshold.
    small_drop_decision = price_drop_alert_decision(last_price=50000, current_price=47500, threshold_pct=8)
    assert not small_drop_decision["should_alert"], "A 5% drop should not alert when threshold is 8%."
    assert not small_drop_decision["should_update_baseline"], "Sub-threshold drops must keep the old baseline (no ratchet)."

    # Cumulative drop: starting from the same 50000 baseline, a later price that is
    # 11% lower must alert even though it would not relative to an intermediate low.
    cumulative_decision = price_drop_alert_decision(last_price=50000, current_price=44500, threshold_pct=8)
    assert cumulative_decision["should_alert"], "A cumulative 11% drop vs the kept baseline should alert."

    # A price increase should update the baseline up (track the peak) but not alert.
    price_up_decision = price_drop_alert_decision(last_price=50000, current_price=55000, threshold_pct=8)
    assert not price_up_decision["should_alert"], "A price increase must not alert."
    assert price_up_decision["should_update_baseline"], "A price increase should move the baseline up to the new peak."

    partial_decision = price_drop_alert_decision(last_price=50000, current_price=40000, threshold_pct=8, partial_data=True)
    assert not partial_decision["should_alert"], "Partial API data must not trigger a price alert."
    assert not partial_decision["should_update_baseline"], "Partial API data must not overwrite baseline."

    first_baseline_decision = price_drop_alert_decision(last_price=0, current_price=50000, threshold_pct=8)
    assert not first_baseline_decision["should_alert"], "First observed price should create baseline silently."
    assert first_baseline_decision["should_update_baseline"], "First valid price should be stored as baseline."

    # Clean up test DB after run
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)

    logger.info("Test finished successfully!")

if __name__ == "__main__":
    asyncio.run(run_test())
