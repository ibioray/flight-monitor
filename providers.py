import httpx
import logging
from datetime import datetime
from config import TRAVELPAYOUTS_TOKEN
from db import save_flight_cache

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
        
        try:
            logger.info(f"Querying Travelpayouts cache for {origin} -> {destination} on {depart_month_or_date} (direct_only={direct_only})")
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(self.base_url, params=params, headers=headers)
                
                if response.status_code == 429:
                    logger.error("Travelpayouts API rate limit reached (429)!")
                    return []
                    
                response.raise_for_status()
                result = response.json()
                
                if not result.get("success"):
                    logger.error(f"API returned success=false. Error: {result.get('error')}")
                    return []
                    
                data = result.get("data", [])
                logger.info(f"Found {len(data)} cached flights for {origin} -> {destination}")
                
                parsed_flights = []
                for item in data:
                    # Parse departure date (extract YYYY-MM-DD from ISO timestamp if needed)
                    dep_date_raw = item.get("departure_at", item.get("depart_date"))
                    if dep_date_raw and "T" in dep_date_raw:
                        depart_date = dep_date_raw.split("T")[0]
                    else:
                        depart_date = dep_date_raw
                        
                    price_raw = item.get("price", item.get("value"))
                    price = float(price_raw) if price_raw is not None else 0.0
                    
                    flight = {
                        "origin": item["origin"],
                        "destination": item["destination"],
                        "depart_date": depart_date,
                        "price": price,
                        "airline": item.get("airline", "Unknown"),
                        "transfers_count": int(item.get("transfers", item.get("number_of_changes", 0)))
                    }
                    parsed_flights.append(flight)
                    
                    # Save to local SQLite database cache
                    save_flight_cache(
                        origin=flight["origin"],
                        destination=flight["destination"],
                        depart_date=flight["depart_date"],
                        price=flight["price"],
                        airline=flight["airline"],
                        transfers_count=flight["transfers_count"]
                    )
                    
                return parsed_flights
                
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error querying Travelpayouts: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            logger.error(f"Unexpected error querying Travelpayouts: {e}")
            
        return []

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
        
        try:
            logger.info(f"Discovering outbound directions from {origin} for month {month}")
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(self.base_url, params=params, headers=headers)
                if response.status_code == 429:
                    logger.error("Travelpayouts API 429 rate limit!")
                    return []
                response.raise_for_status()
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
        except Exception as e:
            logger.error(f"Outbound discovery error: {e}")
        return []

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
        
        try:
            logger.info(f"Discovering inbound directions to {destination} for month {month}")
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(self.base_url, params=params, headers=headers)
                if response.status_code == 429:
                    logger.error("Travelpayouts API 429 rate limit!")
                    return []
                response.raise_for_status()
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
        except Exception as e:
            logger.error(f"Inbound discovery error: {e}")
        return []
