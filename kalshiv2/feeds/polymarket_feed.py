"""
Polymarket data feed - the "herd leader" signal.
Tracks prediction market prices for correlated events to gauge
crowd consensus and detect divergences with Kalshi pricing.

Uses persistent disk-backed caching so event keyword matching
runs once and is reused across restarts without re-searching.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from kalshiv2.config import PolymarketConfig

logger = logging.getLogger(__name__)


@dataclass
class PolymarketEvent:
    """A Polymarket event/market with current pricing."""
    condition_id: str
    question: str
    description: str
    yes_price: float       # 0.0-1.0
    no_price: float        # 0.0-1.0
    volume: float          # total volume traded
    liquidity: float       # current liquidity
    end_date: str
    category: str
    outcomes: list[str] = field(default_factory=list)
    # Derived
    implied_prob: float = 0.0  # midpoint probability
    spread: float = 0.0       # bid-ask spread proxy
    volume_24h: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "question": self.question,
            "description": self.description,
            "yes_price": self.yes_price,
            "no_price": self.no_price,
            "volume": self.volume,
            "liquidity": self.liquidity,
            "end_date": self.end_date,
            "category": self.category,
            "outcomes": self.outcomes,
            "implied_prob": self.implied_prob,
            "spread": self.spread,
            "volume_24h": self.volume_24h,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PolymarketEvent:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class HerdSignal:
    """
    Aggregated herd sentiment from Polymarket.
    Positive = crowd leans bullish on the underlying.
    Negative = crowd leans bearish.
    """
    direction: float        # -1.0 (strongly bearish) to 1.0 (strongly bullish)
    confidence: float       # 0.0 to 1.0 based on volume and liquidity
    num_markets: int        # how many markets contributed to this signal
    consensus_prob: float   # weighted average probability
    divergence_vs_kalshi: float  # how far Poly price is from Kalshi mid
    timestamp: datetime = field(default_factory=datetime.now)


# Keywords that map Polymarket events to underlying assets
ASSET_KEYWORDS: dict[str, list[str]] = {
    "SP500": ["s&p", "sp500", "s&p 500", "spx", "spy"],
    "NASDAQ": ["nasdaq", "qqq", "tech stocks", "nasdaq 100"],
    "DOW": ["dow", "dow jones", "djia"],
    "CRUDE": ["oil", "crude", "wti", "brent"],
    "GOLD": ["gold", "xau"],
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth"],
    "RATES": ["fed", "interest rate", "fomc", "rate cut", "rate hike"],
}

# How long before a cached match mapping is considered stale (seconds)
_MATCH_CACHE_TTL = 3600  # 1 hour - events don't change that often


class PolymarketFeed:
    """
    Fetches Polymarket data to extract crowd wisdom signals.
    Acts as the "herd leader" - what does the smart money crowd think?

    Caching strategy:
      - Raw events are fetched from Gamma API at `poll_interval_sec` cadence
        and held in memory.
      - Keyword-to-event match mappings are persisted to disk so the expensive
        text-matching pass only runs once per TTL window.  On subsequent calls
        the feed refreshes *prices* for already-matched condition_ids instead
        of re-scanning all events.
    """

    def __init__(self, config: PolymarketConfig, cache_dir: str | Path = "kalshiv2_data") -> None:
        self.config = config

        # In-memory event cache (raw from API)
        self._events_cache: list[PolymarketEvent] = []
        self._last_fetch: float = 0
        self._http = None

        # Persistent match cache: asset_key -> list of condition_ids
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._match_cache_path = self._cache_dir / "polymarket_matches.json"
        self._match_cache: dict[str, list[str]] = {}  # asset_key -> [condition_id, ...]
        self._match_cache_time: dict[str, float] = {}  # asset_key -> timestamp
        self._load_match_cache()

        # Price cache keyed by condition_id for fast lookup without re-scanning
        self._price_cache: dict[str, PolymarketEvent] = {}

    def _get_http(self) -> Any:
        if self._http is None:
            import httpx
            self._http = httpx.Client(timeout=15)
        return self._http

    # ------------------------------------------------------------------
    # Persistent match cache
    # ------------------------------------------------------------------

    def _load_match_cache(self) -> None:
        """Load the condition_id match mappings from disk."""
        if self._match_cache_path.exists():
            try:
                raw = json.loads(self._match_cache_path.read_text())
                self._match_cache = raw.get("matches", {})
                self._match_cache_time = {
                    k: float(v) for k, v in raw.get("timestamps", {}).items()
                }
                total = sum(len(v) for v in self._match_cache.values())
                logger.info(f"Loaded {total} cached Polymarket matches for "
                            f"{len(self._match_cache)} asset keys")
            except Exception as e:
                logger.warning(f"Failed to load match cache: {e}")
                self._match_cache = {}
                self._match_cache_time = {}

    def _save_match_cache(self) -> None:
        """Persist match mappings to disk."""
        try:
            payload = {
                "matches": self._match_cache,
                "timestamps": {k: v for k, v in self._match_cache_time.items()},
                "updated": datetime.now().isoformat(),
            }
            self._match_cache_path.write_text(json.dumps(payload, indent=2))
        except Exception as e:
            logger.warning(f"Failed to save match cache: {e}")

    def _is_match_fresh(self, asset_key: str) -> bool:
        """Check if the cached matches for an asset are still fresh."""
        ts = self._match_cache_time.get(asset_key, 0)
        return (time.time() - ts) < _MATCH_CACHE_TTL and asset_key in self._match_cache

    # ------------------------------------------------------------------
    # Event fetching
    # ------------------------------------------------------------------

    def fetch_events(self, category: str | None = None,
                     active_only: bool = True) -> list[PolymarketEvent]:
        """Fetch current Polymarket events from the Gamma API."""
        now = time.time()
        if now - self._last_fetch < self.config.poll_interval_sec and self._events_cache:
            return self._events_cache

        try:
            client = self._get_http()
            params: dict[str, Any] = {
                "limit": 100,
                "active": str(active_only).lower(),
                "order": "volume",
                "ascending": "false",
            }
            if category:
                params["tag"] = category

            resp = client.get(f"{self.config.gamma_url}/markets", params=params)
            data = resp.json()

            events = []
            for mkt in data if isinstance(data, list) else data.get("data", []):
                prices_raw = mkt.get("outcomePrices", "[0.5,0.5]")
                if isinstance(prices_raw, str):
                    yes_price = float(prices_raw.strip("[]").split(",")[0])
                else:
                    yes_price = float(prices_raw[0]) if prices_raw else 0.5
                no_price = 1.0 - yes_price

                event = PolymarketEvent(
                    condition_id=str(mkt.get("conditionId", "")),
                    question=mkt.get("question", ""),
                    description=mkt.get("description", ""),
                    yes_price=yes_price,
                    no_price=no_price,
                    volume=float(mkt.get("volume", 0)),
                    liquidity=float(mkt.get("liquidity", 0)),
                    end_date=mkt.get("endDate", ""),
                    category=mkt.get("category", ""),
                    outcomes=mkt.get("outcomes", ["Yes", "No"]),
                    implied_prob=yes_price,
                    spread=abs(yes_price - no_price),
                    volume_24h=float(mkt.get("volume24hr", 0)),
                )
                events.append(event)

            self._events_cache = events
            self._last_fetch = now

            # Update the price cache so matched IDs can look up fresh prices
            for ev in events:
                self._price_cache[ev.condition_id] = ev

            return events

        except Exception as e:
            logger.warning(f"Polymarket fetch failed: {e}")
            return self._events_cache

    # ------------------------------------------------------------------
    # Cached keyword matching
    # ------------------------------------------------------------------

    def find_related_events(self, asset_key: str) -> list[PolymarketEvent]:
        """
        Find Polymarket events related to a specific asset.
        Uses a persistent disk cache so the keyword search runs only once
        per TTL window.  Subsequent calls just look up the cached
        condition_ids and return them with refreshed prices.
        """
        # Ensure we have fresh event data (prices)
        self.fetch_events()

        # If we have a fresh match cache, use it directly
        if self._is_match_fresh(asset_key):
            matched_ids = self._match_cache[asset_key]
            return [
                self._price_cache[cid]
                for cid in matched_ids
                if cid in self._price_cache
            ]

        # Cache miss or stale — run keyword matching once
        keywords = ASSET_KEYWORDS.get(asset_key.upper(), [asset_key.lower()])
        matched_ids = []
        for ev in self._events_cache:
            text = (ev.question + " " + ev.description).lower()
            if any(kw in text for kw in keywords):
                matched_ids.append(ev.condition_id)

        # Persist the mapping
        self._match_cache[asset_key] = matched_ids
        self._match_cache_time[asset_key] = time.time()
        self._save_match_cache()

        logger.info(f"Polymarket: cached {len(matched_ids)} matches for {asset_key}")

        return [
            self._price_cache[cid]
            for cid in matched_ids
            if cid in self._price_cache
        ]

    # ------------------------------------------------------------------
    # Herd signal generation
    # ------------------------------------------------------------------

    def get_herd_signal(self, asset_key: str, kalshi_mid: float = 0.5) -> HerdSignal:
        """
        Generate a herd sentiment signal for an asset.
        Compares Polymarket crowd pricing against Kalshi mid price
        to detect divergences and consensus direction.
        """
        related = self.find_related_events(asset_key)

        if not related:
            return HerdSignal(
                direction=0.0, confidence=0.0, num_markets=0,
                consensus_prob=0.5, divergence_vs_kalshi=0.0,
            )

        # Volume-weighted consensus probability
        total_vol = sum(ev.volume for ev in related) or 1.0
        weighted_prob = sum(ev.implied_prob * ev.volume for ev in related) / total_vol

        # Direction: >0.5 means crowd is bullish (YES on price going up)
        direction = (weighted_prob - 0.5) * 2.0  # scale to [-1, 1]

        # Confidence based on volume and number of markets
        vol_score = min(1.0, total_vol / 1_000_000)
        count_score = min(1.0, len(related) / 5)
        liquidity_score = min(1.0, sum(ev.liquidity for ev in related) / 500_000)
        confidence = (vol_score * 0.4 + count_score * 0.3 + liquidity_score * 0.3)

        # Divergence: how far is Polymarket from Kalshi pricing?
        divergence = weighted_prob - kalshi_mid

        return HerdSignal(
            direction=max(-1.0, min(1.0, direction)),
            confidence=confidence,
            num_markets=len(related),
            consensus_prob=weighted_prob,
            divergence_vs_kalshi=divergence,
        )

    def get_market_sentiment(self) -> dict[str, HerdSignal]:
        """Get herd signals for all tracked asset categories."""
        signals = {}
        for asset_key in ASSET_KEYWORDS:
            signals[asset_key] = self.get_herd_signal(asset_key)
        return signals

    def detect_divergence(self, asset_key: str, kalshi_prob: float,
                          threshold: float = 0.05) -> dict[str, Any]:
        """
        Detect if Polymarket and Kalshi prices diverge significantly.
        A divergence > threshold suggests a potential arbitrage or
        the crowd has information Kalshi hasn't priced in yet.
        """
        herd = self.get_herd_signal(asset_key, kalshi_mid=kalshi_prob)
        div = abs(herd.divergence_vs_kalshi)

        return {
            "asset": asset_key,
            "polymarket_prob": herd.consensus_prob,
            "kalshi_prob": kalshi_prob,
            "divergence": herd.divergence_vs_kalshi,
            "significant": div > threshold,
            "direction": "poly_higher" if herd.divergence_vs_kalshi > 0 else "kalshi_higher",
            "suggested_side": "YES" if herd.divergence_vs_kalshi > threshold else (
                "NO" if herd.divergence_vs_kalshi < -threshold else "HOLD"
            ),
            "confidence": herd.confidence,
        }

    def invalidate_cache(self, asset_key: str | None = None) -> None:
        """Force re-matching on next call.  Pass None to clear all."""
        if asset_key:
            self._match_cache.pop(asset_key, None)
            self._match_cache_time.pop(asset_key, None)
        else:
            self._match_cache.clear()
            self._match_cache_time.clear()
        self._save_match_cache()
