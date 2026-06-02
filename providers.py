import httpx
import logging
import asyncio
import time
from datetime import datetime, timezone, timedelta
from config import TRAVELPAYOUTS_TOKEN

logger = logging.getLogger("providers")


class AsyncRateLimiter:
    """Token-bucket style rate limiter for async API calls.

    target_rps: target requests per second (default 8 for Travelpayouts 600/min limit).
    burst: max concurrent requests allowed.
    """
    def __init__(self, target_rps: float = 8.0, burst: int = 5):
        self._interval = 1.0 / target_rps  # seconds between requests
        self._semaphore = asyncio.Semaphore(burst)
        self._last_request_time = 0.0
        self._lock = asyncio.Lock()
        self._partial_data = False  # True if any request failed with 429 after all retries
        self._total_requests = 0
        self._failed_requests = 0

    async def acquire(self):
        """Wait until a request slot is available."""
        await self._semaphore.acquire()
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < self._interval:
                await asyncio.sleep(self._interval - elapsed)
            self._last_request_time = time.monotonic()

    def release(self):
        self._semaphore.release()

    def record_success(self):
        self._total_requests += 1

    def record_failure(self):
        self._total_requests += 1
        self._failed_requests += 1
        self._partial_data = True

    @property
    def is_partial_data(self) -> bool:
        return self._partial_data

    @property
    def stats(self) -> dict:
        return {
            "total_requests": self._total_requests,
            "failed_requests": self._failed_requests,
            "partial_data": self._partial_data,
        }

    def reset(self):
        self._partial_data = False
        self._total_requests = 0
        self._failed_requests = 0


class FlightProvider:
    """Base class for flight ticket providers."""
    async def get_prices(self, origin: str, destination: str, depart_month_or_date: str) -> list[dict]:
        raise NotImplementedError


class TravelpayoutsProvider(FlightProvider):
    """Travelpayouts (Aviasales) API Provider (cached data)."""

    def __init__(self, token: str = TRAVELPAYOUTS_TOKEN):
        self.token = token
        self.base_url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
        self.rate_limiter = AsyncRateLimiter(target_rps=8.0, burst=5)

    async def get_prices(self, origin: str, destination: str, depart_month_or_date: str, direct_only: bool = False, cache_mode: str = "overview") -> list[dict]:
        """
        Fetch cached flight prices for a route and a specific date or month.
        depart_month_or_date format: YYYY-MM or YYYY-MM-DD
        """
        from db import check_route_query_log, log_route_query, get_cached_flights, save_flight_cache

        month = depart_month_or_date[:7] # YYYY-MM
        direct_flag = 1 if direct_only else 0

        # Check cache log first (DOP-5 / Codex D) unless in fresh/buy_now mode
        if cache_mode not in ("fresh", "buy_now"):
            ttl_hours = 12 if cache_mode == "monitor" else 24
            cache_hit = await asyncio.to_thread(check_route_query_log, origin, destination, month, direct_flag, ttl_hours)
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
        # Uses rate_limiter to enforce 8 req/s target (plan v3 §3.3)
        max_retries = 3
        backoff = 2.0
        response = None

        for attempt in range(max_retries):
            await self.rate_limiter.acquire()
            try:
                logger.info(f"Querying Travelpayouts API (attempt {attempt+1}) for {origin} -> {destination} on {depart_month_or_date} (direct={direct_only})")
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.get(self.base_url, params=params, headers=headers)
                    if response.status_code == 429:
                        logger.warning(f"Travelpayouts 429 rate limit hit. Retrying in {backoff} seconds...")
                        self.rate_limiter.release()
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue
                    response.raise_for_status()
                    self.rate_limiter.record_success()
                    self.rate_limiter.release()
                    break
            except Exception as e:
                self.rate_limiter.release()
                if attempt == max_retries - 1:
                    logger.error(f"Failed to fetch prices from Travelpayouts after {max_retries} attempts: {e}")
                    self.rate_limiter.record_failure()
                    return []
                logger.warning(f"HTTP/Connection error (attempt {attempt+1}): {e}. Retrying in {backoff} seconds...")
                await asyncio.sleep(backoff)
                backoff *= 2

        if not response or response.status_code != 200:
            self.rate_limiter.record_failure()
            return []

        result = response.json()
        if not result.get("success"):
            logger.error(f"API returned success=false. Error: {result.get('error')}")
            return []

        data = result.get("data", [])
        logger.info(f"Found {len(data)} cached flights for {origin} -> {destination}")

        fetched_dt = datetime.now(timezone.utc)
        fetched_at = fetched_dt.strftime("%Y-%m-%d %H:%M:%S")

        # Calculate expires_at
        ttl_hours = 12 if cache_mode == "monitor" else 24
        expires_dt = fetched_dt + timedelta(hours=ttl_hours)
        expires_at = expires_dt.strftime("%Y-%m-%d %H:%M:%S")

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
                "duration": int(item.get("duration", 0)),  # in minutes (Auditor connection timing improvement)
                "fetched_at": fetched_at,
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
                direct_only=direct_flag,
                fetched_at=fetched_at,
                expires_at=expires_at
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
            await self.rate_limiter.acquire()
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.get(self.base_url, params=params, headers=headers)
                    if response.status_code == 429:
                        self.rate_limiter.release()
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue
                    response.raise_for_status()
                    self.rate_limiter.release()
                    break
            except Exception as e:
                self.rate_limiter.release()
                if attempt == max_retries - 1:
                        logger.error(f"Outbound discovery API failed: {e}")
                        self.rate_limiter.record_failure()
                        return []
                await asyncio.sleep(backoff)
                backoff *= 2

        if not response or response.status_code != 200:
            self.rate_limiter.record_failure()
            return []

        data = response.json()
        if not data.get("success"):
            self.rate_limiter.record_failure()
            return []
        self.rate_limiter.record_success()

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
            await self.rate_limiter.acquire()
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.get(self.base_url, params=params, headers=headers)
                    if response.status_code == 429:
                        self.rate_limiter.release()
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue
                    response.raise_for_status()
                    self.rate_limiter.release()
                    break
            except Exception as e:
                self.rate_limiter.release()
                if attempt == max_retries - 1:
                        logger.error(f"Inbound discovery API failed: {e}")
                        self.rate_limiter.record_failure()
                        return []
                await asyncio.sleep(backoff)
                backoff *= 2

        if not response or response.status_code != 200:
            self.rate_limiter.record_failure()
            return []

        data = response.json()
        if not data.get("success"):
            self.rate_limiter.record_failure()
            return []
        self.rate_limiter.record_success()

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
