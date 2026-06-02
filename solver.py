import sqlite3
import json
import logging
from datetime import datetime, timedelta
from db import get_db_connection

logger = logging.getLogger("solver")

def parse_date(date_str):
    return datetime.strptime(date_str, "%Y-%m-%d")

class GraphSolver:
    def __init__(self):
        pass

    def solve(self, origin_iata: str, destination_country_code: str, destination_iatas: list[str],
              date_start_str: str, date_end_str: str, max_transfers: int = 3,
              visa_allowed: int = 1, lodging_exceptions: dict = None, max_budget: float = None):
        """
        Builds a Directed Acyclic Graph (DAG) of flight and train segments from SQLite cache
        and finds valid, scored, and categorized routes.
        """
        lodging_exceptions = lodging_exceptions or {}
        
        # Load metadata
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Load hubs
        cursor.execute("SELECT * FROM transit_hubs")
        hubs = {row["iata"]: dict(row) for row in cursor.fetchall()}
        
        # Load manual legs
        cursor.execute("SELECT * FROM manual_legs")
        manual_legs = {}
        for row in cursor.fetchall():
            manual_legs[(row["origin"], row["destination"])] = dict(row)
            
        # Get all cached flights within date window
        # Valid segments are those departing between date_start and date_end
        cursor.execute("""
        SELECT * FROM flight_cache 
        WHERE depart_date >= ? AND depart_date <= ?
        """, (date_start_str, date_end_str))
        cached_flights = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        # Build segment database for traversal
        # We group flights by origin to look up outgoing edges quickly
        adj_flights = {}
        for flight in cached_flights:
            orig = flight["origin"]
            if orig not in adj_flights:
                adj_flights[orig] = []
            adj_flights[orig].append(flight)
            
        # Add manual legs as segments for every day in the search range
        start_date = parse_date(date_start_str)
        end_date = parse_date(date_end_str)
        delta_days = (end_date - start_date).days
        
        for i in range(delta_days + 1):
            current_date_str = (start_date + timedelta(days=i)).strftime("%Y-%m-%d")
            for (orig, dest), leg in manual_legs.items():
                if orig not in adj_flights:
                    adj_flights[orig] = []
                adj_flights[orig].append({
                    "origin": orig,
                    "destination": dest,
                    "depart_date": current_date_str,
                    "price": leg["price_rub"],
                    "airline": leg["leg_type"].upper(),
                    "transfers_count": 0,
                    "is_manual": True,
                    "duration_hours": leg["duration_hours"]
                })
                
        # Depth First Search to find all routes
        all_routes = []
        
        # A route is a list of segments
        # Path validation and traversal
        def dfs(current_airport, current_date_str, path, depth):
            # Base Case: arrived in target destination in China
            if current_airport in destination_iatas:
                all_routes.append(list(path))
                return
                
            # Limit segments to max_transfers + 1
            if depth > max_transfers:
                return
                
            outgoing = adj_flights.get(current_airport, [])
            for segment in outgoing:
                dest = segment["destination"]
                dept_str = segment["depart_date"]
                
                # Prevent cycles
                if any(x["origin"] == dest for x in path) or dest == origin_iata:
                    continue
                    
                # Visa validation for intermediate hubs
                if dest not in destination_iatas: # If not final destination in China
                    hub_meta = hubs.get(dest)
                    if hub_meta:
                        if visa_allowed == 0 and hub_meta["requires_visa_for_ru"] == 1:
                            continue # Skip visa required hub
                            
                # Date sequencing & layover checks
                if path:
                    last_segment = path[-1]
                    last_dept = parse_date(last_segment["depart_date"])
                    curr_dept = parse_date(dept_str)
                    
                    # Layover length in days
                    layover_days = (curr_dept - last_dept).days
                    
                    # Connection constraints
                    if layover_days < 0:
                        continue # Cannot travel backward in time
                        
                    # Standard transit vs stopover
                    # Determine if this city is a stopover candidate
                    is_stopover_candidate = (
                        dest in hubs or last_segment["destination"] in hubs
                    )
                    
                    # If same-day self-transfer (separate tickets, delta == 0)
                    # We allow it but flag it in the scoring and risks
                    if layover_days == 0:
                        # Ensure we don't have multiple manual segments on same day that overlap or self-transfer risk
                        pass
                        
                    # Max layover constraint: do not sit in one hub for more than 5 days
                    if layover_days > 5:
                        continue
                
                # Add to path and recurse
                path.append(segment)
                dfs(dest, dept_str, path, depth + 1)
                path.pop()

        # Start search from origin
        dfs(origin_iata, date_start_str, [], 0)
        
        # Score, filter, and structure results
        scored_routes = []
        for route in all_routes:
            total_price = 0
            base_price = 0
            lodging_price = 0
            risk_score = 0
            risk_warnings = []
            stopovers = []
            
            # Duration analysis
            first_dept = parse_date(route[0]["depart_date"])
            last_dept = parse_date(route[-1]["depart_date"])
            total_duration_days = (last_dept - first_dept).days + 1
            
            for i, segment in enumerate(route):
                price = segment["price"]
                base_price += price
                
                # Check layover details
                if i > 0:
                    prev_segment = route[i-1]
                    transit_city = prev_segment["destination"]
                    
                    prev_date = parse_date(prev_segment["depart_date"])
                    curr_date = parse_date(segment["depart_date"])
                    layover_days = (curr_date - prev_date).days
                    
                    # Airport change check (e.g. SVO -> DME)
                    if prev_segment["destination"] != segment["origin"]:
                        risk_score += 100
                        risk_warnings.append(
                            f"Смена аэропорта в {transit_city}: прилет в {prev_segment['destination']}, вылет из {segment['origin']}"
                        )
                        
                    # Same-day self-transfer risk
                    if layover_days == 0 and not segment.get("is_manual") and not prev_segment.get("is_manual"):
                        risk_score += 50
                        risk_warnings.append(
                            f"Транзит {transit_city} в тот же день: отдельные билеты без гарантированной стыковки!"
                        )
                        
                    # Calculate lodging cost for layovers
                    if layover_days > 0:
                        hub_info = hubs.get(transit_city)
                        daily_lodging = 0
                        if hub_info:
                            # Apply lodging exception if set by user, otherwise DB value
                            daily_lodging = lodging_exceptions.get(transit_city, hub_info["daily_lodging_rub"])
                            
                        current_lodging_cost = layover_days * daily_lodging
                        lodging_price += current_lodging_cost
                        
                        if layover_days >= 2:
                            stopovers.append({
                                "city": transit_city,
                                "name": hub_info["city_name"] if hub_info else transit_city,
                                "days": layover_days
                            })
                            
            total_price = base_price + lodging_price
            
            scored_routes.append({
                "segments": route,
                "base_price": base_price,
                "lodging_price": lodging_price,
                "total_price": total_price,
                "duration_days": total_duration_days,
                "stopovers": stopovers,
                "risk_score": risk_score,
                "risk_warnings": risk_warnings
            })
            
        # Split into within-budget and over-budget routes
        within_budget_routes = []
        over_budget_routes = []
        for r in scored_routes:
            if max_budget and r["total_price"] > max_budget:
                over_budget_routes.append(r)
            else:
                within_budget_routes.append(r)
                
        # Determine if we need to fall back to over-budget options
        is_fallback_active = False
        target_routes = within_budget_routes
        if not within_budget_routes and over_budget_routes:
            target_routes = over_budget_routes
            is_fallback_active = True
            
        # Categorize routes from the selected target list
        # 1. Cheapest
        cheapest_routes = sorted(target_routes, key=lambda x: x["total_price"])[:5]
        
        # 2. Fastest
        fastest_routes = sorted(target_routes, key=lambda x: (x["duration_days"], x["total_price"]))[:5]
        
        # 3. Smart Stopovers (Must have at least one stopover of >= 2 days)
        stopover_routes = [r for r in target_routes if len(r["stopovers"]) > 0]
        stopover_routes = sorted(stopover_routes, key=lambda x: x["total_price"])[:5]
        
        return {
            "cheapest": cheapest_routes,
            "fastest": fastest_routes,
            "stopovers": stopover_routes,
            "is_fallback_active": is_fallback_active,
            "total_routes_found_before_filter": len(scored_routes)
        }
