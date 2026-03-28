"""
Kalshi API client for placing bets on 15-minute over/under events.
Supports both production and demo endpoints.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kalshiv2.config import KalshiAPIConfig

logger = logging.getLogger(__name__)


@dataclass
class KalshiEvent:
    """A Kalshi event (market)."""
    event_ticker: str
    series_ticker: str
    title: str
    category: str
    status: str  # "open", "closed", "settled"
    markets: list[KalshiMarket] = field(default_factory=list)


@dataclass
class KalshiMarket:
    """A single Kalshi market (specific yes/no contract)."""
    ticker: str
    event_ticker: str
    title: str
    subtitle: str
    status: str
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    last_price: float
    volume: int
    open_interest: int
    close_time: str
    result: str  # "" if not settled, "yes" or "no"
    floor_strike: float  # the over/under threshold
    cap_strike: float

    @property
    def yes_mid(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def no_mid(self) -> float:
        return (self.no_bid + self.no_ask) / 2

    @property
    def spread(self) -> float:
        return self.yes_ask - self.yes_bid

    @property
    def minutes_to_close(self) -> float:
        try:
            close = datetime.fromisoformat(self.close_time.replace("Z", "+00:00"))
            return max(0, (close - datetime.now(close.tzinfo)).total_seconds() / 60)
        except Exception:
            return 0


@dataclass
class KalshiOrder:
    """Order placed on Kalshi."""
    order_id: str
    ticker: str
    side: str  # "yes" or "no"
    price: int  # in cents (1-99)
    count: int  # number of contracts
    status: str  # "resting", "executed", "canceled"
    created_time: str
    filled_count: int = 0


@dataclass
class KalshiPosition:
    """Current position in a market."""
    ticker: str
    yes_count: int
    no_count: int
    avg_price: float
    market_value: float


class KalshiClient:
    """
    Client for Kalshi's trading API v2.
    Handles authentication, event discovery, and order placement.
    """

    def __init__(self, config: KalshiAPIConfig) -> None:
        self.config = config
        self._http = None
        self._token: str | None = None
        self._token_expiry: float = 0

    def _get_http(self) -> Any:
        if self._http is None:
            import httpx
            self._http = httpx.Client(timeout=15)
        return self._http

    @property
    def base_url(self) -> str:
        return self.config.effective_url

    # -- Authentication --

    def _ensure_auth(self) -> dict[str, str]:
        """Get auth headers. Uses API key + private key signing."""
        headers: dict[str, str] = {"Content-Type": "application/json"}

        if self._token and time.time() < self._token_expiry:
            headers["Authorization"] = f"Bearer {self._token}"
            return headers

        if self.config.api_key:
            # Try login with API key
            try:
                resp = self._get_http().post(
                    f"{self.base_url}/login",
                    json={"email": self.config.api_key, "password": ""},
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    self._token = data.get("token", "")
                    self._token_expiry = time.time() + 3500  # ~1 hour
                    headers["Authorization"] = f"Bearer {self._token}"
            except Exception as e:
                logger.warning(f"Kalshi auth failed: {e}")

        return headers

    # -- Events & Markets --

    def get_events(self, series_ticker: str | None = None,
                   status: str = "open", limit: int = 50) -> list[KalshiEvent]:
        """Fetch events, optionally filtered by series."""
        try:
            params: dict[str, Any] = {"limit": limit, "status": status}
            if series_ticker:
                params["series_ticker"] = series_ticker

            headers = self._ensure_auth()
            resp = self._get_http().get(
                f"{self.base_url}/events", params=params, headers=headers,
            )
            data = resp.json()

            events = []
            for ev in data.get("events", []):
                markets = []
                for mkt in ev.get("markets", []):
                    markets.append(self._parse_market(mkt))
                events.append(KalshiEvent(
                    event_ticker=ev.get("event_ticker", ""),
                    series_ticker=ev.get("series_ticker", ""),
                    title=ev.get("title", ""),
                    category=ev.get("category", ""),
                    status=ev.get("status", ""),
                    markets=markets,
                ))
            return events
        except Exception as e:
            logger.error(f"Failed to fetch events: {e}")
            return []

    def get_markets(self, event_ticker: str | None = None,
                    series_ticker: str | None = None,
                    status: str = "open",
                    limit: int = 100) -> list[KalshiMarket]:
        """Fetch markets (individual yes/no contracts)."""
        try:
            params: dict[str, Any] = {"limit": limit, "status": status}
            if event_ticker:
                params["event_ticker"] = event_ticker
            if series_ticker:
                params["series_ticker"] = series_ticker

            headers = self._ensure_auth()
            resp = self._get_http().get(
                f"{self.base_url}/markets", params=params, headers=headers,
            )
            data = resp.json()
            return [self._parse_market(m) for m in data.get("markets", [])]
        except Exception as e:
            logger.error(f"Failed to fetch markets: {e}")
            return []

    def get_market(self, ticker: str) -> KalshiMarket | None:
        """Fetch a single market by ticker."""
        try:
            headers = self._ensure_auth()
            resp = self._get_http().get(
                f"{self.base_url}/markets/{ticker}", headers=headers,
            )
            data = resp.json()
            return self._parse_market(data.get("market", data))
        except Exception as e:
            logger.error(f"Failed to fetch market {ticker}: {e}")
            return None

    def find_15min_markets(self, series_tickers: list[str] | None = None) -> list[KalshiMarket]:
        """Find currently open 15-minute over/under markets."""
        markets = []
        tickers = series_tickers or ["INXD", "NASDAQ", "INX", "COMP"]
        for series in tickers:
            mkts = self.get_markets(series_ticker=series, status="open")
            for m in mkts:
                # Filter to ~15 min events (close within 5-20 minutes)
                if 5 <= m.minutes_to_close <= 20:
                    markets.append(m)
        return markets

    # -- Orders --

    def place_order(self, ticker: str, side: str, price_cents: int,
                    count: int = 1) -> KalshiOrder | None:
        """
        Place a limit order on Kalshi.

        Args:
            ticker: Market ticker
            side: "yes" or "no"
            price_cents: Price in cents (1-99)
            count: Number of contracts
        """
        if price_cents < 1 or price_cents > 99:
            logger.error(f"Invalid price: {price_cents}")
            return None

        try:
            headers = self._ensure_auth()
            payload = {
                "ticker": ticker,
                "action": "buy",
                "side": side,
                "type": "limit",
                "count": count,
                "yes_price" if side == "yes" else "no_price": price_cents,
            }

            resp = self._get_http().post(
                f"{self.base_url}/portfolio/orders",
                json=payload, headers=headers,
            )
            data = resp.json()
            order = data.get("order", data)
            return KalshiOrder(
                order_id=order.get("order_id", ""),
                ticker=ticker,
                side=side,
                price=price_cents,
                count=count,
                status=order.get("status", "unknown"),
                created_time=order.get("created_time", ""),
            )
        except Exception as e:
            logger.error(f"Failed to place order: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting order."""
        try:
            headers = self._ensure_auth()
            resp = self._get_http().delete(
                f"{self.base_url}/portfolio/orders/{order_id}",
                headers=headers,
            )
            return resp.status_code in (200, 204)
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    def get_positions(self) -> list[KalshiPosition]:
        """Get current portfolio positions."""
        try:
            headers = self._ensure_auth()
            resp = self._get_http().get(
                f"{self.base_url}/portfolio/positions", headers=headers,
            )
            data = resp.json()
            positions = []
            for p in data.get("market_positions", []):
                positions.append(KalshiPosition(
                    ticker=p.get("ticker", ""),
                    yes_count=p.get("market_exposure", 0),
                    no_count=p.get("rest_count", 0),
                    avg_price=p.get("average_price", 0),
                    market_value=p.get("market_value", 0),
                ))
            return positions
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            return []

    def get_balance(self) -> float:
        """Get account balance in USD."""
        try:
            headers = self._ensure_auth()
            resp = self._get_http().get(
                f"{self.base_url}/portfolio/balance", headers=headers,
            )
            data = resp.json()
            return float(data.get("balance", 0)) / 100  # cents to dollars
        except Exception as e:
            logger.error(f"Failed to fetch balance: {e}")
            return 0.0

    # -- Helpers --

    @staticmethod
    def _parse_market(m: dict[str, Any]) -> KalshiMarket:
        return KalshiMarket(
            ticker=m.get("ticker", ""),
            event_ticker=m.get("event_ticker", ""),
            title=m.get("title", ""),
            subtitle=m.get("subtitle", ""),
            status=m.get("status", ""),
            yes_bid=float(m.get("yes_bid", 0)) / 100,
            yes_ask=float(m.get("yes_ask", 0)) / 100,
            no_bid=float(m.get("no_bid", 0)) / 100,
            no_ask=float(m.get("no_ask", 0)) / 100,
            last_price=float(m.get("last_price", 0)) / 100,
            volume=int(m.get("volume", 0)),
            open_interest=int(m.get("open_interest", 0)),
            close_time=m.get("close_time", ""),
            result=m.get("result", ""),
            floor_strike=float(m.get("floor_strike", 0)),
            cap_strike=float(m.get("cap_strike", 0)),
        )
