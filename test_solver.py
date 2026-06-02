import asyncio
import logging
from datetime import datetime, timedelta
from db import init_db, save_flight_cache
from solver import GraphSolver
from analyst import LLMCognitiveAnalyst

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_solver")

def populate_mock_data():
    logger.info("Seeding mock flight data for testing solver...")
    
    # Define some mock flights
    flights = [
        # Route 1: UFA -> MOW -> PEK (2 days stopover in Moscow)
        ("UFA", "MOW", "2026-06-15", 5000.0, "SU", 0),
        ("MOW", "PEK", "2026-06-17", 20000.0, "CA", 0),
        
        # Route 2: UFA -> ALA -> PEK (4 days stopover in Almaty)
        ("UFA", "ALA", "2026-06-16", 10000.0, "KC", 0),
        ("ALA", "PEK", "2026-06-20", 15000.0, "KC", 0),
        
        # Route 3: UFA -> EVN -> PEK (3 days stopover in Yerevan, lodging cost = 0)
        ("UFA", "EVN", "2026-06-15", 8000.0, "RM", 0),
        ("EVN", "PEK", "2026-06-18", 18000.0, "HU", 0),
        
        # Route 4: Direct flight UFA -> PEK
        ("UFA", "PEK", "2026-06-22", 45000.0, "CA", 0),
        
        # Leg for 3-hop transit: UFA -> MOW -> ALA -> PEK
        ("MOW", "ALA", "2026-06-16", 8000.0, "SU", 0),
    ]
    
    for origin, dest, date, price, airline, changes in flights:
        save_flight_cache(origin, dest, date, price, airline, changes)
        
    logger.info("Mock flight data seeded successfully.")

async def run_test():
    # 1. Init Database tables
    init_db()
    
    # 2. Populate DB with mock flight data
    populate_mock_data()
    
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
    results = solver.solve(
        origin_iata=origin,
        destination_country_code=destination_country,
        destination_iatas=china_airports,
        date_start_str=date_start,
        date_end_str=date_end,
        max_transfers=3,
        visa_allowed=1,
        lodging_exceptions={"EVN": 0.0},
        max_budget=max_budget
    )
    
    # Validate result structure
    logger.info(f"Solved cheapest routes count: {len(results['cheapest'])}")
    logger.info(f"Solved fastest routes count: {len(results['fastest'])}")
    logger.info(f"Solved stopover routes count: {len(results['stopovers'])}")
    
    assert len(results["cheapest"]) > 0, "Error: Solver failed to find routes!"
    
    # Print the cheapest path found
    best_route = results["cheapest"][0]
    chain = " ➔ ".join([f"{leg['origin']} -> {leg['destination']} ({leg['depart_date']}, {leg['price']:.0f} ₽)" for leg in best_route["segments"]])
    logger.info(f"Best cheapest route (Total Cost: {best_route['total_price']:.0f} ₽, Tickets: {best_route['base_price']:.0f} ₽, Lodging: {best_route['lodging_price']:.0f} ₽):")
    logger.info(f"  {chain}")
    
    # 4. LLM Analyst test
    logger.info("Testing LLMCognitiveAnalyst...")
    analyst = LLMCognitiveAnalyst()
    analysis_report = await analyst.analyze_routes(
        origin=origin,
        destination=destination_country,
        date_range=f"{date_start} — {date_end}",
        max_budget=max_budget,
        solved_data=results
    )
    
    print("\n" + "=" * 40 + " ANALYSIS REPORT " + "=" * 40)
    print(analysis_report)
    print("=" * 97 + "\n")
    logger.info("Test finished successfully!")

if __name__ == "__main__":
    asyncio.run(run_test())
