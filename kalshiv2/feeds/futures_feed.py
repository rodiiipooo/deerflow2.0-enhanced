"""
Futures data feed - pulls real-time and historical futures data
for momentum, mean-reversion, and volatility breakout signals.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from kalshiv2.config import FuturesConfig

logger = logging.getLogger(__name__)


@dataclass
class FuturesSnapshot:
    """Point-in-time snapshot of a futures contract."""
    symbol: str
    price: float
    open: float
    high: float
    low: float
    volume: int
    timestamp: datetime
    bid: float = 0.0
    ask: float = 0.0
    # Derived
    change_pct: float = 0.0
    intraday_range_pct: float = 0.0


@dataclass
class FuturesBar:
    """OHLCV bar for a futures contract."""
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class FuturesFeed:
    """
    Pulls futures data from Yahoo Finance, Alpaca, or Polygon.
    Provides snapshots and intraday bars for signal generation.
    """

    def __init__(self, config: FuturesConfig) -> None:
        self.config = config
        self._cache: dict[str, list[FuturesBar]] = {}
        self._last_fetch: dict[str, float] = {}
        self._min_fetch_interval = 5.0  # seconds

    def get_snapshot(self, symbol: str) -> FuturesSnapshot | None:
        """Get current price snapshot for a futures symbol."""
        if self.config.provider == "yahoo":
            return self._yahoo_snapshot(symbol)
        elif self.config.provider == "alpaca":
            return self._alpaca_snapshot(symbol)
        elif self.config.provider == "polygon":
            return self._polygon_snapshot(symbol)
        return None

    def get_snapshots(self) -> dict[str, FuturesSnapshot]:
        """Get snapshots for all configured symbols."""
        results = {}
        for sym in self.config.symbols:
            snap = self.get_snapshot(sym)
            if snap:
                results[sym] = snap
        return results

    def get_intraday_bars(self, symbol: str, interval: str = "1m",
                          lookback_minutes: int = 120) -> list[FuturesBar]:
        """Get intraday OHLCV bars."""
        cache_key = f"{symbol}:{interval}"
        now = time.time()
        if cache_key in self._last_fetch and now - self._last_fetch[cache_key] < self._min_fetch_interval:
            return self._cache.get(cache_key, [])

        bars = []
        if self.config.provider == "yahoo":
            bars = self._yahoo_bars(symbol, interval, lookback_minutes)
        elif self.config.provider == "alpaca":
            bars = self._alpaca_bars(symbol, interval, lookback_minutes)
        elif self.config.provider == "polygon":
            bars = self._polygon_bars(symbol, interval, lookback_minutes)

        self._cache[cache_key] = bars
        self._last_fetch[cache_key] = now
        return bars

    def get_prices_array(self, symbol: str, lookback_minutes: int = 120) -> np.ndarray:
        """Get closing prices as numpy array for technical analysis."""
        bars = self.get_intraday_bars(symbol, "1m", lookback_minutes)
        if not bars:
            return np.array([])
        return np.array([b.close for b in bars])

    def get_volumes_array(self, symbol: str, lookback_minutes: int = 120) -> np.ndarray:
        """Get volumes as numpy array."""
        bars = self.get_intraday_bars(symbol, "1m", lookback_minutes)
        if not bars:
            return np.array([])
        return np.array([b.volume for b in bars], dtype=float)

    # -- Yahoo Finance --

    def _yahoo_snapshot(self, symbol: str) -> FuturesSnapshot | None:
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            info = ticker.fast_info
            hist = ticker.history(period="1d", interval="1m")
            if hist.empty:
                return None
            last = hist.iloc[-1]
            day_open = hist.iloc[0]["Open"]
            return FuturesSnapshot(
                symbol=symbol,
                price=float(last["Close"]),
                open=float(day_open),
                high=float(hist["High"].max()),
                low=float(hist["Low"].min()),
                volume=int(hist["Volume"].sum()),
                timestamp=datetime.now(),
                change_pct=((float(last["Close"]) - day_open) / day_open * 100) if day_open else 0,
                intraday_range_pct=(
                    (float(hist["High"].max()) - float(hist["Low"].min()))
                    / day_open * 100
                ) if day_open else 0,
            )
        except Exception as e:
            logger.warning(f"Yahoo snapshot failed for {symbol}: {e}")
            return None

    def _yahoo_bars(self, symbol: str, interval: str, lookback_minutes: int) -> list[FuturesBar]:
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            # Yahoo requires period for intraday
            period = "1d" if lookback_minutes <= 390 else "5d"
            hist = ticker.history(period=period, interval=interval)
            bars = []
            for ts, row in hist.iterrows():
                bars.append(FuturesBar(
                    symbol=symbol,
                    timestamp=ts.to_pydatetime(),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=int(row["Volume"]),
                ))
            # Trim to lookback
            if lookback_minutes and len(bars) > lookback_minutes:
                bars = bars[-lookback_minutes:]
            return bars
        except Exception as e:
            logger.warning(f"Yahoo bars failed for {symbol}: {e}")
            return []

    # -- Alpaca --

    def _alpaca_snapshot(self, symbol: str) -> FuturesSnapshot | None:
        try:
            import httpx
            headers = {
                "APCA-API-KEY-ID": self.config.alpaca_api_key,
                "APCA-API-SECRET-KEY": self.config.alpaca_secret,
            }
            resp = httpx.get(
                f"https://data.alpaca.markets/v2/stocks/{symbol}/snapshot",
                headers=headers, timeout=10,
            )
            data = resp.json()
            latest = data.get("latestTrade", {})
            bar = data.get("dailyBar", {})
            return FuturesSnapshot(
                symbol=symbol,
                price=float(latest.get("p", 0)),
                open=float(bar.get("o", 0)),
                high=float(bar.get("h", 0)),
                low=float(bar.get("l", 0)),
                volume=int(bar.get("v", 0)),
                timestamp=datetime.now(),
            )
        except Exception as e:
            logger.warning(f"Alpaca snapshot failed for {symbol}: {e}")
            return None

    def _alpaca_bars(self, symbol: str, interval: str, lookback_minutes: int) -> list[FuturesBar]:
        try:
            import httpx
            headers = {
                "APCA-API-KEY-ID": self.config.alpaca_api_key,
                "APCA-API-SECRET-KEY": self.config.alpaca_secret,
            }
            start = (datetime.utcnow() - timedelta(minutes=lookback_minutes)).isoformat() + "Z"
            tf_map = {"1m": "1Min", "5m": "5Min", "15m": "15Min"}
            timeframe = tf_map.get(interval, "1Min")
            resp = httpx.get(
                f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
                params={"timeframe": timeframe, "start": start, "limit": lookback_minutes},
                headers=headers, timeout=10,
            )
            data = resp.json()
            bars = []
            for b in data.get("bars", []):
                bars.append(FuturesBar(
                    symbol=symbol,
                    timestamp=datetime.fromisoformat(b["t"].replace("Z", "+00:00")),
                    open=float(b["o"]),
                    high=float(b["h"]),
                    low=float(b["l"]),
                    close=float(b["c"]),
                    volume=int(b["v"]),
                ))
            return bars
        except Exception as e:
            logger.warning(f"Alpaca bars failed for {symbol}: {e}")
            return []

    # -- Polygon --

    def _polygon_snapshot(self, symbol: str) -> FuturesSnapshot | None:
        try:
            import httpx
            resp = httpx.get(
                f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}",
                params={"apiKey": self.config.polygon_api_key},
                timeout=10,
            )
            data = resp.json().get("ticker", {})
            day = data.get("day", {})
            return FuturesSnapshot(
                symbol=symbol,
                price=float(data.get("lastTrade", {}).get("p", 0)),
                open=float(day.get("o", 0)),
                high=float(day.get("h", 0)),
                low=float(day.get("l", 0)),
                volume=int(day.get("v", 0)),
                timestamp=datetime.now(),
            )
        except Exception as e:
            logger.warning(f"Polygon snapshot failed for {symbol}: {e}")
            return None

    def _polygon_bars(self, symbol: str, interval: str, lookback_minutes: int) -> list[FuturesBar]:
        try:
            import httpx
            multiplier = int(interval.replace("m", ""))
            start_ms = int((datetime.utcnow() - timedelta(minutes=lookback_minutes)).timestamp() * 1000)
            end_ms = int(datetime.utcnow().timestamp() * 1000)
            resp = httpx.get(
                f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/{multiplier}/minute/{start_ms}/{end_ms}",
                params={"apiKey": self.config.polygon_api_key, "limit": 5000},
                timeout=10,
            )
            data = resp.json()
            bars = []
            for b in data.get("results", []):
                bars.append(FuturesBar(
                    symbol=symbol,
                    timestamp=datetime.fromtimestamp(b["t"] / 1000),
                    open=float(b["o"]),
                    high=float(b["h"]),
                    low=float(b["l"]),
                    close=float(b["c"]),
                    volume=int(b["v"]),
                ))
            return bars
        except Exception as e:
            logger.warning(f"Polygon bars failed for {symbol}: {e}")
            return []
