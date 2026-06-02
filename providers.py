import httpx
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from config import TRAVELPAYOUTS_TOKEN

logger = logging.getLogger("providers")

class FlightProvider:
    """Base class for flight ticket providers."""
    async def get_prices(self, origin: str, destination: str, depart_month_or_date: str) -> list[dict]:
        raise NotImplementedError

class TravelpayoutsProvider(FlightProvider):
    """Travelpayouts (Aviasales) API Provider (cached data)."""
    
    def __init__(self, token: str = TRAVELPAYOUTS_TOKEN):
        self.token = token
        self.base_url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
        
    async def get_prices(self, origin: str, destination: str, depart_month_or_date: str, direct_only: bool = False) -> list[dict]:
        """
        Fetch cached flight prices for a route and a specific date or month.
        depart_month_or_date format: YYYY-MM or YYYY-MM-DD
        """
        from db import check_route_query_log, log_route_query, get_cached_flights, save_flight_cache
        
        month = depart_month_or_date[:7] # YYYY-MM
        direct_flag = 1 if direct_only else 0
        
        # Check cache log first (DOP-5 / Codex D)
        cache_hit = await asyncio.to_thread(check_route_query_log, origin, destination, month, direct_flag)
        if cache_hit:
            logger.info(f"Cache hit for {origin} -> {destination} on month {month} (direct={direct_only})")
            return await asyncio.to_thread(get_cached_flights, origin, destination, month, direct_flag)
            
        if not self.token or self.token == "your_travelpayouts_token_here":
            logger.error("Travelpayouts token is not configured. Skipping request.")
            return []
            
        params = {
            "origin": origin,
            "destination": destination,
            "departure_at": depart_month_or_date,
            "one_way": "true",
            "sorting": "price",
            "currency": "rub",
            "limit": 100,
            "token": self.token
        }
        if direct_only:
            params["direct"] = "true"
            
        headers = {
            "Accept-Encoding": "gzip, deflate"
        }
        
        # Implement retry with exponential backoff on 429 / network errors (DOP-5)
        max_retries = 3
        backoff = 2.0
        response = None
        
        for attempt in range(max_retries):
            try:
                logger.info(f"Querying Travelpayouts API (attempt {attempt+1}) for {origin} -> {destination} on {depart_month_or_date} (direct={direct_only})")
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.get(self.base_url, params=params, headers=headers)
                    if response.status_code == 429:
                        logger.warning(f"Travelpayouts 429 rate limit hit. Retrying in {backoff} seconds...")
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue
                    response.raise_for_status()
                    break
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(f"Failed to fetch prices from Travelpayouts after {max_retries} attempts: {e}")
                    return []
                logger.warning(f"HTTP/Connection error (attempt {attempt+1}): {e}. Retrying in {backoff} seconds...")
                await asyncio.sleep(backoff)
                backoff *= 2
                
        if not response or response.status_code != 200:
            return []
            
        result = response.json()
        if not result.get("success"):
            logger.error(f"API returned success=false. Error: {result.get('error')}")
            return []
            
        data = result.get("data", [])
        logger.info(f"Found {len(data)} cached flights for {origin} -> {destination}")
        
        parsed_flights = []
        for item in data:
            departure_at = item.get("departure_at", item.get("depart_date") + "T00:00:00Z" if item.get("depart_date") else "")
            if not departure_at:
                continue
                
            depart_date = departure_at.split("T")[0]
            price_raw = item.get("price", item.get("value"))
            price = float(price_raw) if price_raw is not None else 0.0
            
            flight = {
                "origin": item["origin"],
                "destination": item["destination"],
                "depart_date": depart_date,
                "departure_at": departure_at,
                "price": price,
                "airline": item.get("airline", "Unknown"),
                "flight_number": str(item.get("flight_number", "")),
                "transfers_count": int(item.get("transfers", item.get("number_of_changes", 0))),
                "duration": int(item.get("duration", 0)) # in minutes (Auditor connection timing improvement)
            }
            parsed_flights.append(flight)
            
            # Save to local SQLite database cache (async-safe thread)
            await asyncio.to_thread(
                save_flight_cache,
                origin=flight["origin"],
                destination=flight["destination"],
                depart_date=flight["depart_date"],
                departure_at=flight["departure_at"],
                price=flight["price"],
                airline=flight["airline"],
                flight_number=flight["flight_number"],
                transfers_count=flight["transfers_count"],
                duration=flight["duration"],
                direct_only=direct_flag
            )
            
        # Log that we queried this route, caching empty results too (Codex D)
        await asyncio.to_thread(log_route_query, origin, destination, month, direct_flag)
        return parsed_flights

    async def get_outbound_directions(self, origin: str, month: str) -> list[dict]:
        """
        Discover unique outbound routes from a city for a given month.
        """
        if not self.token or self.token == "your_travelpayouts_token_here":
            logger.error("Travelpayouts token is not configured.")
            return []
            
        params = {
            "origin": origin,
            "unique": "true",
            "sorting": "route",
            "departure_at": month,
            "one_way": "true",
            "currency": "rub",
            "limit": 100,
            "token": self.token
        }
        headers = {"Accept-Encoding": "gzip, deflate"}
        
        max_retries = 3
        backoff = 2.0
        response = None
        
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.get(self.base_url, params=params, headers=headers)
                    if response.status_code == 429:
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue
                    response.raise_for_status()
                    break
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(f"Outbound discovery API failed: {e}")
                    return []
                await asyncio.sleep(backoff)
                backoff *= 2
                
        if not response or response.status_code != 200:
            return []
            
        data = response.json()
        if not data.get("success"):
            return []
        
        results = []
        for item in data.get("data", []):
            results.append({
                "origin": item["origin"],
                "destination": item["destination"],
                "price": float(item["price"]) if item.get("price") is not None else 0.0,
                "departure_at": item.get("departure_at"),
                "airline": item.get("airline", "Unknown"),
                "transfers": int(item.get("transfers", 0))
            })
        return results

    async def get_inbound_directions(self, destination: str, month: str) -> list[dict]:
        """
        Discover unique inbound routes to a city for a given month.
        """
        if not self.token or self.token == "your_travelpayouts_token_here":
            logger.error("Travelpayouts token is not configured.")
            return []
            
        params = {
            "destination": destination,
            "unique": "true",
            "sorting": "route",
            "departure_at": month,
            "one_way": "true",
            "currency": "rub",
            "limit": 100,
            "token": self.token
        }
        headers = {"Accept-Encoding": "gzip, deflate"}
        
        max_retries = 3
        backoff = 2.0
        response = None
        
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.get(self.base_url, params=params, headers=headers)
                    if response.status_code == 429:
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue
                    response.raise_for_status()
                    break
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(f"Inbound discovery API failed: {e}")
                    return []
                await asyncio.sleep(backoff)
                backoff *= 2
                
        if not response or response.status_code != 200:
            return []
            
        data = response.json()
        if not data.get("success"):
            return []
        
        results = []
        for item in data.get("data", []):
            results.append({
                "origin": item["origin"],
                "destination": item["destination"],
                "price": float(item["price"]) if item.get("price") is not None else 0.0,
                "departure_at": item.get("departure_at"),
                "airline": item.get("airline", "Unknown"),
                "transfers": int(item.get("transfers", 0))
            })
        return results
