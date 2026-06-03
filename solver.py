import sqlite3
import json
import logging
import hashlib
import heapq
import itertools
import math
from datetime import datetime, timezone, timedelta
from db import get_db_connection

logger = logging.getLogger("solver")

# Approximate opportunity cost of 1 hour of travel in RUB for balanced scoring.
# This weights time vs price: higher value = prefer faster routes.
HOUR_COST_RUB = 350

# RUB penalty per risk point, used to fold risk into the balanced cost function
# instead of sorting by risk first (which made "balanced" degenerate to
# "direct/known-hub only"). airport change (+100) ≈ 6000 ₽, unknown hub (+40) ≈ 2400 ₽.
RISK_COST_RUB = 60

# Conservative nightly lodging (RUB) for stopover cities that are NOT in the
# transit_hubs catalog, so multi-night routes through unknown cities are not
# scored as artificially cheap (audit M4).
DEFAULT_STOPOVER_LODGING_RUB = 4000

# Combinatorial safety limits for the DFS path search (audit C1).
MAX_TOTAL_ROUTES = 4000          # hard ceiling on enumerated (recorded) routes
EDGES_PER_DEST_DATE = 3          # keep only N cheapest flights per (dest, date) per origin
MAX_DFS_EXPANSIONS = 200000      # hard ceiling on node visits -> bounds traversal work
                                 # deterministically, independent of how many paths reach
                                 # the destination. Does NOT interfere with the over-budget
                                 # fallback (unlike budget-based pruning).

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
    if hub_info and hub_info.get("country_code", "").lower() in cleaned_exclusions:
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

def is_hub_whitelisted(city_iata, allowed_hubs, hubs):
    """Whitelist for INTERMEDIATE transit hubs.

    If allowed_hubs is empty -> no restriction (every hub allowed). Otherwise a transit
    hub is allowed only if it matches one of allowed_hubs by IATA code, city name, or
    country code. Matching mirrors is_city_excluded so include/exclude behave symmetrically.

    NOTE: this is applied per-hop in the DFS, so it constrains EVERY intermediate hub of a
    route regardless of how many transfers (1, 2 or 3) the route has.
    """
    if not allowed_hubs:
        return True
    cleaned = [x.strip().lower() for x in allowed_hubs if x and x.strip()]
    if not cleaned or "все" in cleaned or "всё" in cleaned or "любые" in cleaned:
        return True
    if city_iata.lower() in cleaned:
        return True
    hub_info = hubs.get(city_iata)
    if hub_info and hub_info["city_name"].lower() in cleaned:
        return True
    if hub_info and hub_info.get("country_code", "").lower() in cleaned:
        return True
    return False

def route_signature(route: list[dict]) -> str:
    parts = []
    for segment in route:
        parts.append(
            "|".join([
                segment.get("origin", ""),
                segment.get("destination", ""),
                segment.get("departure_at", segment.get("depart_date", "")),
                str(segment.get("price", "")),
                "manual" if segment.get("is_manual") else "flight",
            ])
        )
    return "||".join(parts)

def route_id_for(route: list[dict]) -> str:
    digest = hashlib.sha1(route_signature(route).encode("utf-8")).hexdigest()[:6].upper()
    return f"R-{digest}"

class GraphSolver:
    def __init__(self):
        pass

    def solve(self, origin_iata: str, destination_country_code: str, destination_iatas: list[str],
              date_start_str: str, date_end_str: str, priced_flights: list[dict], max_transfers: int = 3,
              visa_allowed: int = 1, lodging_exceptions: dict = None, max_budget: float = None,
              baggage_needed: int = 0, stopovers_pref: list = None, exclusions: list = None,
              min_stopover_hours: int = 0, max_stopover_days: int = 5, stopover_preset: str = "balanced",
              allow_awkward_layovers: int = 1, visa_mode: str = "visa_free_only",
              allowed_hubs: list = None):
        """
        Builds a Directed Acyclic Graph (DAG) using priced segments passed directly in memory (Codex E)
        and finds valid, scored, and categorized routes with precise time-aware buffers.

        Stopover presets (plan v3 §5):
          - 'fast': max_stopover_days=1, no awkward layovers
          - 'walk': min_stopover_hours=24, max_stopover_days=3
          - 'mini_trip': min_stopover_hours=48, max_stopover_days=5
          - 'price_only': soft stopover windows, visa rules still apply
          - 'balanced': default, moderate settings
        """
        lodging_exceptions = lodging_exceptions or {}
        stopovers_pref = stopovers_pref or []
        exclusions = exclusions or []
        allowed_hubs = allowed_hubs or []

        # Apply preset defaults if not overridden (plan v3 §5)
        if stopover_preset == "fast":
            max_stopover_days = min(max_stopover_days, 1)
            min_stopover_hours = 0
            allow_awkward_layovers = 0
        elif stopover_preset == "walk":
            min_stopover_hours = max(min_stopover_hours, 24)
            max_stopover_days = min(max_stopover_days, 3)
            allow_awkward_layovers = 0
        elif stopover_preset == "mini_trip":
            min_stopover_hours = max(min_stopover_hours, 48)
            max_stopover_days = min(max_stopover_days, 5)
            allow_awkward_layovers = 0

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

        # Collapse parallel edges to curb combinatorial blow-up (C1): keep only the
        # cheapest EDGES_PER_DEST_DATE flights per (destination, date) from each airport.
        # Without this a dense hub can hold hundreds of near-duplicate edges and the
        # DFS explodes exponentially.
        for orig in list(adj_flights.keys()):
            grouped = {}
            for f in adj_flights[orig]:
                date_key = f.get("depart_date") or f.get("departure_at", "")[:10]
                grouped.setdefault((f["destination"], date_key), []).append(f)
            collapsed = []
            for group in grouped.values():
                group.sort(key=lambda x: x.get("price", float("inf")))
                collapsed.extend(group[:EDGES_PER_DEST_DATE])
            adj_flights[orig] = collapsed

        # --- Route enumeration: k-shortest-paths via best-first search ---------------
        # Replaces the previous exhaustive DFS. We expand partial paths in increasing
        # order of generalized cost (ticket price + total elapsed travel time, including
        # layover waits). Because complete routes are POPPED in cost order, the first
        # MAX_TOTAL_ROUTES recorded are genuinely the K best — this is both complete
        # (finds the true cheapest, unlike a capped DFS) and bounded (no blow-up).
        max_legs = max_transfers + 1  # depth limit in segments (F)

        def seg_duration_hours(seg):
            """Flight/ground leg duration in hours, with a conservative default."""
            if seg.get("is_manual"):
                return float(seg.get("duration_hours", 0.0) or 0.0)
            minutes = seg.get("duration", 0) or 0
            hours = minutes / 60.0
            return hours if hours > 0 else 5.0  # conservative default flight time

        def can_extend(path, segment):
            """All per-hop feasibility checks. Applied to EVERY hop, so multi-transfer
            (2/3 connection) routes are validated at each intermediate hub."""
            dest = segment["destination"]
            current_airport = path[-1]["destination"] if path else origin_iata

            # Blacklist on both endpoints.
            if is_city_excluded(dest, exclusions, hubs) or is_city_excluded(current_airport, exclusions, hubs):
                return False
            # Whitelist: every INTERMEDIATE hub must be allowed (origin/dest exempt).
            if dest not in destination_iatas and not is_hub_whitelisted(dest, allowed_hubs, hubs):
                return False
            # No cycles.
            if any(x["origin"] == dest for x in path) or dest == origin_iata:
                return False
            # Visa: skip visa-required intermediate hubs in strict mode.
            if dest not in destination_iatas:
                hub_meta = hubs.get(dest)
                if hub_meta and visa_allowed == 0 and hub_meta["requires_visa_for_ru"] == 1:
                    return False
            # Time-aware connection checks against the previous segment.
            if path:
                last_segment = path[-1]
                last_dept = parse_departure_time(last_segment.get("departure_at", last_segment["depart_date"] + "T00:00:00Z"))
                curr_dept = parse_departure_time(segment.get("departure_at", segment["depart_date"] + "T00:00:00Z"))
                last_arrival = last_dept + timedelta(hours=seg_duration_hours(last_segment))
                buffer_hours = (curr_dept - last_arrival).total_seconds() / 3600.0

                min_buffer = 2.0  # carry-on standard transit
                if last_segment["destination"] != segment["origin"]:
                    min_buffer = 6.0  # airport change
                elif baggage_needed == 1:
                    min_buffer = 4.0  # checked baggage self-transfer

                if buffer_hours < min_buffer:
                    return False
                if buffer_hours > max_stopover_days * 24.0:
                    return False
                if buffer_hours >= 48.0:
                    if not is_stopover_allowed(last_segment["destination"], stopovers_pref, hubs):
                        return False
            return True

        all_routes = []
        route_cap_hit = [False]
        counter = itertools.count()  # stable tie-breaker so heap never compares paths
        pops = 0

        # Heap entry: (generalized_cost, tie, current_airport, path, price_sum, start_dt)
        heap = [(0.0, next(counter), origin_iata, [], 0.0, None)]

        while heap:
            if len(all_routes) >= MAX_TOTAL_ROUTES or pops >= MAX_DFS_EXPANSIONS:
                route_cap_hit[0] = True
                break

            gcost, _, current_airport, path, price_sum, start_dt = heapq.heappop(heap)
            pops += 1

            # Arrived at a target airport: record (in cost order) and stop expanding.
            if path and current_airport in destination_iatas:
                all_routes.append(path)
                continue

            if len(path) >= max_legs:
                continue

            for segment in adj_flights.get(current_airport, []):
                if not can_extend(path, segment):
                    continue
                seg_dep = parse_departure_time(segment.get("departure_at", segment["depart_date"] + "T00:00:00Z"))
                seg_arr = seg_dep + timedelta(hours=seg_duration_hours(segment))
                new_start = start_dt or seg_dep
                new_price = price_sum + (segment.get("price", 0) or 0)
                elapsed_h = max((seg_arr - new_start).total_seconds() / 3600.0, 0.0)
                new_gcost = new_price + elapsed_h * HOUR_COST_RUB
                heapq.heappush(
                    heap,
                    (new_gcost, next(counter), segment["destination"], path + [segment], new_price, new_start),
                )

        if route_cap_hit[0]:
            logger.warning(
                "Route search hit safety cap (pops=%s, routes=%s). Results may be partial.",
                pops, len(all_routes),
            )

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

            duration_hours = max((last_arrival - first_dept).total_seconds() / 3600.0, 0.0)
            total_duration_days = max(1, math.ceil(duration_hours / 24.0))

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
                    # Hotel nights = number of calendar midnights crossed between arrival and
                    # next departure (audit C2). This replaces floor(hours/24), which charged
                    # 0 nights for an overnight stay shorter than 24h (e.g. arrive 22:00,
                    # depart next day 20:00 = 22h crossed one midnight but cost 0 before).
                    calendar_nights = (curr_dept_time.date() - prev_arrival_time.date()).days
                    if layover_hours < 8.0:
                        layover_nights = 0  # short connection — no hotel, even if it crosses midnight
                    elif layover_hours >= 18.0:
                        layover_nights = max(calendar_nights, 1)  # long wait needs rest
                    else:
                        layover_nights = calendar_nights
                    layover_days = layover_nights  # kept for display / downstream compatibility

                    # Classify layover type (plan v3 §5)
                    if layover_hours < 2:
                        layover_type = "connection"
                    elif layover_hours < 18:
                        layover_type = "awkward"  # not enough to explore, too long to wait
                    elif layover_hours < 24:
                        layover_type = "walkable"  # can walk around the city
                    else:
                        layover_type = "stopover"  # real multi-day stop

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

                    # Calculate lodging cost for overnight layovers
                    if layover_nights > 0:
                        hub_info = hubs.get(transit_city)
                        if hub_info:
                            # Known catalog city (daily_lodging_rub may legitimately be 0, e.g. EVN).
                            daily_lodging = lodging_exceptions.get(transit_city, hub_info["daily_lodging_rub"])
                        else:
                            # Unknown city not in catalog: conservative default so the route is
                            # not scored as artificially cheap (audit M4).
                            daily_lodging = lodging_exceptions.get(transit_city, DEFAULT_STOPOVER_LODGING_RUB)

                        lodging_price += layover_nights * daily_lodging

                    # Add transit details
                    hub_info = hubs.get(transit_city)
                    if hub_info and hub_info.get("requires_visa_for_ru") == 1 and visa_mode == "warn":
                        risk_score += 120
                        risk_warnings.append(
                            f"Визовый риск: {hub_info['city_name']} ({hub_info.get('country_code', transit_city)}) может требовать визу для граждан РФ."
                        )
                    elif not hub_info and visa_mode in ("visa_free_only", "warn"):
                        risk_score += 40
                        risk_warnings.append(
                            f"Визовые правила для транзита через {transit_city} не проверены в локальном справочнике."
                        )
                    stopovers.append({
                        "city": transit_city,
                        "name": hub_info["city_name"] if hub_info else transit_city,
                        "country_code": hub_info.get("country_code", "") if hub_info else "",
                        "requires_visa_for_ru": int(hub_info.get("requires_visa_for_ru", 0)) if hub_info else None,
                        "days": layover_days,
                        "layover_type": layover_type,
                        "layover_hours": round(layover_hours, 1)
                    })

            # Filter: skip routes where any stopover exceeds max_stopover_days
            skip_route = False
            for stop in stopovers:
                if stop["layover_hours"] > max_stopover_days * 24.0:
                    skip_route = True
                    break
                if not allow_awkward_layovers and stop.get("layover_type") == "awkward":
                    skip_route = True
                    break
            if skip_route:
                continue

            # Filter: if min_stopover_hours > 0, at least one stopover must satisfy min_stopover_hours
            if min_stopover_hours > 0:
                has_valid_stop = False
                for stop in stopovers:
                    if stop["layover_hours"] >= min_stopover_hours:
                        has_valid_stop = True
                        break
                if not has_valid_stop:
                    continue

            total_price = base_price + lodging_price

            scored_routes.append({
                "route_id": route_id_for(route),
                "segments": route,
                "base_price": base_price,
                "lodging_price": lodging_price,
                "total_price": total_price,
                "duration_hours": duration_hours,
                "duration_days": total_duration_days,
                "stopovers": stopovers,
                "risk_score": risk_score,
                "risk_warnings": risk_warnings,
                "estimated_timing": estimated_timing,
                "badges": [],
                "stopover_preset": stopover_preset,
                "allow_awkward_layovers": allow_awkward_layovers,
                "visa_mode": visa_mode
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
        fastest_routes = sorted(target_routes, key=lambda x: (x["duration_hours"], x["total_price"]))[:5]

        # 3. Smart Stopovers (Must have at least one stopover of >= min_stopover_hours or 24 hours)
        min_hours = min_stopover_hours if min_stopover_hours > 0 else 24
        stopover_routes = []
        for r in target_routes:
            has_valid_stop = False
            for s in r["stopovers"]:
                if s["layover_hours"] >= min_hours:
                    has_valid_stop = True
                    break
            if has_valid_stop:
                stopover_routes.append(r)
        stopover_routes = sorted(stopover_routes, key=lambda x: x["total_price"])[:5]

        # Truly direct = single segment with no transfers. A single-ticket connecting fare
        # is also one segment but represents >=1 stop, so it must not get the "Прямой" badge.
        direct_routes = [
            r for r in target_routes
            if len(r["segments"]) == 1 and not r["segments"][0].get("transfers_count")
        ]
        direct_routes = sorted(direct_routes, key=lambda x: (x["total_price"], x["duration_hours"]))[:5]

        one_connection_routes = [r for r in target_routes if len(r["segments"]) == 2]
        one_connection_routes = sorted(one_connection_routes, key=lambda x: (x["total_price"], x["duration_hours"]))[:5]

        # Balanced = single scalar cost folding price, time and risk (audit H1).
        # Previously this sorted by risk_score first, which made "balanced" collapse to
        # "direct/known-hub only" because almost every discovered transit hub adds risk.
        balanced_routes = sorted(
            target_routes,
            key=lambda x: (
                x["total_price"]
                + x["duration_hours"] * HOUR_COST_RUB
                + x["risk_score"] * RISK_COST_RUB,
                len(x["segments"]),
            )
        )

        selected_by_signature = {}
        recommended_routes = []

        def add_from(routes, badge, count=1):
            added = 0
            for route in routes:
                signature = route_signature(route["segments"])
                if signature in selected_by_signature:
                    existing = selected_by_signature[signature]
                    if badge not in existing["badges"]:
                        existing["badges"].append(badge)
                    continue

                if badge not in route["badges"]:
                    route["badges"].append(badge)
                recommended_routes.append(route)
                selected_by_signature[signature] = route
                added += 1
                if added >= count:
                    break

        add_from(cheapest_routes, "Самый дешевый")
        add_from(fastest_routes, "Самый быстрый")
        add_from(direct_routes, "Прямой/короткий")
        add_from(one_connection_routes, "До 1 пересадки")
        add_from(stopover_routes, "Стоповер")
        add_from(balanced_routes, "Баланс", count=max(0, 5 - len(recommended_routes)))
        recommended_routes = recommended_routes[:5]

        ranked_routes = []
        ranked_seen = set()
        for route in recommended_routes + balanced_routes + cheapest_routes + fastest_routes:
            signature = route_signature(route["segments"])
            if signature in ranked_seen:
                continue
            ranked_seen.add(signature)
            ranked_routes.append(route)
            if len(ranked_routes) >= 50:
                break

        omitted_routes = []
        rendered_ids = {route["route_id"] for route in recommended_routes}
        for route in ranked_routes:
            if route["route_id"] in rendered_ids:
                continue
            omitted_routes.append({
                "route_id": route["route_id"],
                "reason": "not_in_top_5_diverse_summary",
                "total_price": route["total_price"],
                "duration_hours": route["duration_hours"],
                "segments_count": len(route["segments"])
            })

        return {
            "recommended": recommended_routes,
            "ranked_routes": ranked_routes,
            "cheapest": cheapest_routes,
            "fastest": fastest_routes,
            "direct": direct_routes,
            "one_connection": one_connection_routes,
            "stopovers": stopover_routes,
            "omitted_routes": omitted_routes,
            "is_fallback_active": is_fallback_active,
            # Honest funnel (audit H3): raw DFS routes -> scored (post stopover/visa filters)
            # -> within-budget target -> rendered.
            "total_routes_found_before_filter": len(all_routes),
            "total_routes_scored": len(scored_routes),
            "total_routes_after_filter": len(target_routes),
            "rendered_routes_count": len(recommended_routes),
            "route_cap_hit": route_cap_hit[0]
        }
