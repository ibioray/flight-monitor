import sqlite3
import json
import logging
from datetime import datetime, timezone, timedelta
from db import get_db_connection

logger = logging.getLogger("solver")

def parse_departure_time(dep_at_str):
    """Parses departure_at ISO timestamps to UTC naive datetime for timezone-safe comparisons."""
    try:
        dt = datetime.fromisoformat(dep_at_str.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        try:
            return datetime.strptime(dep_at_str[:10], "%Y-%m-%d")
        except Exception:
            return datetime.now(timezone.utc).replace(tzinfo=None)

def is_city_excluded(city_iata, exclusions, hubs):
    """Checks if a city/IATA is in the user-specified exclusions list."""
    if not exclusions:
        return False
    cleaned_exclusions = [x.strip().lower() for x in exclusions]
    if city_iata.lower() in cleaned_exclusions:
        return True
    hub_info = hubs.get(city_iata)
    if hub_info and hub_info["city_name"].lower() in cleaned_exclusions:
        return True
    return False

def is_stopover_allowed(city_iata, stopovers_pref, hubs):
    """Checks if a stopover (layover >= 2 days) is allowed according to user preferences."""
    if not stopovers_pref:
        return True
    cleaned_prefs = [x.strip().lower() for x in stopovers_pref]
    if "все" in cleaned_prefs or "всё" in cleaned_prefs:
        return True
    if city_iata.lower() in cleaned_prefs:
        return True
    hub_info = hubs.get(city_iata)
    if hub_info and hub_info["city_name"].lower() in cleaned_prefs:
        return True
    return False

class GraphSolver:
    def __init__(self):
        pass

    def solve(self, origin_iata: str, destination_country_code: str, destination_iatas: list[str],
              date_start_str: str, date_end_str: str, priced_flights: list[dict], max_transfers: int = 3,
              visa_allowed: int = 1, lodging_exceptions: dict = None, max_budget: float = None,
              baggage_needed: int = 0, stopovers_pref: list = None, exclusions: list = None):
        """
        Builds a Directed Acyclic Graph (DAG) using priced segments passed directly in memory (Codex E)
        and finds valid, scored, and categorized routes with precise time-aware buffers.
        """
        lodging_exceptions = lodging_exceptions or {}
        stopovers_pref = stopovers_pref or []
        exclusions = exclusions or []
        
        # Load hubs from database metadata (static data)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM transit_hubs")
        hubs = {row["iata"]: dict(row) for row in cursor.fetchall()}
        
        # Load manual legs
        cursor.execute("SELECT * FROM manual_legs")
        manual_legs = {}
        for row in cursor.fetchall():
            manual_legs[(row["origin"], row["destination"])] = dict(row)
        conn.close()
        
        # Build segment database using priced_flights in memory (Codex E)
        adj_flights = {}
        for flight in priced_flights:
            orig = flight["origin"]
            if orig not in adj_flights:
                adj_flights[orig] = []
            # Normalize key names
            adj_flights[orig].append(flight)
            
        # Add manual legs as segments for every day in the search range
        start_date = datetime.strptime(date_start_str, "%Y-%m-%d")
        end_date = datetime.strptime(date_end_str, "%Y-%m-%d")
        delta_days = (end_date - start_date).days
        
        for i in range(delta_days + 1):
            current_date_str = (start_date + timedelta(days=i)).strftime("%Y-%m-%d")
            for (orig, dest), leg in manual_legs.items():
                if orig not in adj_flights:
                    adj_flights[orig] = []
                # Make manual leg look like a flight segment with duration_hours
                adj_flights[orig].append({
                    "origin": orig,
                    "destination": dest,
                    "depart_date": current_date_str,
                    "departure_at": f"{current_date_str}T08:00:00Z", # assumed default train time
                    "price": leg["price_rub"],
                    "airline": leg["leg_type"].upper(),
                    "transfers_count": 0,
                    "is_manual": True,
                    "duration_hours": leg["duration_hours"],
                    "duration": int(leg["duration_hours"] * 60) # duration in minutes
                })
                
        # Depth First Search to find all routes
        all_routes = []
        
        def dfs(current_airport, current_date_str, path, depth):
            # Base Case: arrived in target destination IATA
            if current_airport in destination_iatas:
                all_routes.append(list(path))
                return
                
            # Limit segments to max_transfers + 1 (F: depth represents segment count)
            if depth > max_transfers:
                return
                
            outgoing = adj_flights.get(current_airport, [])
            for segment in outgoing:
                dest = segment["destination"]
                dept_at_str = segment.get("departure_at", segment["depart_date"] + "T00:00:00Z")
                
                # Filter out excluded transit hubs (DOP-1)
                if is_city_excluded(dest, exclusions, hubs) or is_city_excluded(current_airport, exclusions, hubs):
                    continue
                    
                # Prevent cycles
                if any(x["origin"] == dest for x in path) or dest == origin_iata:
                    continue
                    
                # Visa validation for intermediate hubs
                if dest not in destination_iatas:
                    hub_meta = hubs.get(dest)
                    if hub_meta:
                        if visa_allowed == 0 and hub_meta["requires_visa_for_ru"] == 1:
                            continue # skip visa required hub
                            
                # Date and timezone-aware connection checks (Codex C / Auditor refinements)
                if path:
                    last_segment = path[-1]
                    last_dept = parse_departure_time(last_segment.get("departure_at", last_segment["depart_date"] + "T00:00:00Z"))
                    curr_dept = parse_departure_time(dept_at_str)
                    
                    # Compute previous arrival time based on actual duration or conservative default (Codex C)
                    if last_segment.get("is_manual"):
                        last_duration_hours = last_segment.get("duration_hours", 0.0)
                    else:
                        last_duration_minutes = last_segment.get("duration", 0)
                        last_duration_hours = last_duration_minutes / 60.0
                        if last_duration_hours == 0:
                            last_duration_hours = 5.0 # conservative default flight time
                            
                    last_arrival = last_dept + timedelta(hours=last_duration_hours)
                    
                    # Connection buffer in hours
                    buffer_hours = (curr_dept - last_arrival).total_seconds() / 3600.0
                    
                    # Buffer check (Codex C)
                    min_buffer = 2.0  # carry-on only standard transit
                    
                    airport_changed = last_segment["destination"] != segment["origin"]
                    if airport_changed:
                        min_buffer = 6.0  # airport transfer minimum
                    elif baggage_needed == 1:
                        min_buffer = 4.0  # checked baggage self-transfer minimum
                        
                    if buffer_hours < min_buffer:
                        continue  # impossible or unsafe connection
                        
                    # Max layover constraint: do not sit in one hub for more than 5 days (120 hours)
                    if buffer_hours > 120.0:
                        continue
                        
                    # Stopover preference checks: if layover >= 48 hours, must be in stopovers_pref if specified (DOP-1)
                    if buffer_hours >= 48.0:
                        transit_city = last_segment["destination"]
                        if not is_stopover_allowed(transit_city, stopovers_pref, hubs):
                            continue
                
                # Recurse
                path.append(segment)
                dfs(dest, dept_at_str, path, depth + 1)
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
            first_dept = parse_departure_time(route[0].get("departure_at", route[0]["depart_date"] + "T00:00:00Z"))
            # Last segment arrival time
            last_segment = route[-1]
            last_dept = parse_departure_time(last_segment.get("departure_at", last_segment["depart_date"] + "T00:00:00Z"))
            if last_segment.get("is_manual"):
                last_duration = last_segment.get("duration_hours", 0.0)
            else:
                last_duration = last_segment.get("duration", 0) / 60.0
                if last_duration == 0:
                    last_duration = 5.0
            last_arrival = last_dept + timedelta(hours=last_duration)
            
            total_duration_days = (last_arrival - first_dept).days + 1
            
            # Check if timing is estimated due to missing duration (Codex C)
            estimated_timing = any((not s.get("is_manual") and s.get("duration", 0) == 0) for s in route)
            
            for i, segment in enumerate(route):
                price = segment["price"]
                base_price += price
                
                # Check layover details
                if i > 0:
                    prev_segment = route[i-1]
                    transit_city = prev_segment["destination"]
                    
                    prev_dept_time = parse_departure_time(prev_segment.get("departure_at", prev_segment["depart_date"] + "T00:00:00Z"))
                    curr_dept_time = parse_departure_time(segment.get("departure_at", segment["depart_date"] + "T00:00:00Z"))
                    
                    if prev_segment.get("is_manual"):
                        prev_duration = prev_segment.get("duration_hours", 0.0)
                    else:
                        prev_duration = prev_segment.get("duration", 0) / 60.0
                        if prev_duration == 0:
                            prev_duration = 5.0
                    prev_arrival_time = prev_dept_time + timedelta(hours=prev_duration)
                    
                    layover_hours = (curr_dept_time - prev_arrival_time).total_seconds() / 3600.0
                    layover_days = int(layover_hours / 24.0)
                    
                    # Airport change check (e.g. SVO -> DME)
                    if prev_segment["destination"] != segment["origin"]:
                        risk_score += 100
                        risk_warnings.append(
                            f"Смена аэропорта в {transit_city}: прилет в {prev_segment['destination']}, вылет из {segment['origin']}"
                        )
                        
                    # Same-day self-transfer risk if layover is tight (Codex C / Auditor refinements)
                    if layover_hours < 5.0 and not segment.get("is_manual") and not prev_segment.get("is_manual"):
                        risk_score += 50
                        risk_warnings.append(
                            f"Короткий транзит в {transit_city} ({layover_hours:.1f} ч): отдельные билеты, высокий риск при задержке первого рейса!"
                        )
                        
                    # Calculate lodging cost for layovers
                    if layover_days > 0:
                        hub_info = hubs.get(transit_city)
                        daily_lodging = 0
                        if hub_info:
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
                "risk_warnings": risk_warnings,
                "estimated_timing": estimated_timing
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
