"""
Options data feed - pulls implied volatility, put/call ratios,
and options skew data for predictive signals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from kalshiv2.config import OptionsConfig

logger = logging.getLogger(__name__)


@dataclass
class OptionsSnapshot:
    """Aggregated options metrics for a single underlying."""
    symbol: str
    timestamp: datetime
    implied_vol: float           # ATM implied volatility
    iv_rank: float               # IV rank (0-100, current IV vs 52w range)
    put_call_ratio: float        # put OI / call OI
    put_call_volume_ratio: float # put vol / call vol
    skew_25d: float              # 25-delta put IV - 25-delta call IV
    vix: float                   # VIX level
    vix_change: float            # VIX % change from prior close
    gamma_exposure: float        # Net GEX estimate (simplified)
    max_pain: float              # Max pain strike price


@dataclass
class OptionChainEntry:
    """Single option contract data."""
    symbol: str
    strike: float
    expiry: str
    option_type: str  # "call" | "put"
    bid: float
    ask: float
    last: float
    volume: int
    open_interest: int
    implied_vol: float
    delta: float
    gamma: float
    theta: float
    vega: float


class OptionsFeed:
    """
    Pulls options data to generate implied volatility, skew,
    put/call ratio, and gamma exposure signals.
    """

    def __init__(self, config: OptionsConfig) -> None:
        self.config = config
        self._vix_cache: tuple[float, float] | None = None
        self._last_vix_time: float = 0

    def get_snapshot(self, underlying: str = "SPY") -> OptionsSnapshot | None:
        """Get aggregated options metrics for an underlying."""
        if self.config.provider == "yahoo":
            return self._yahoo_snapshot(underlying)
        elif self.config.provider == "tradier":
            return self._tradier_snapshot(underlying)
        return None

    def get_vix(self) -> tuple[float, float]:
        """Returns (vix_level, vix_change_pct)."""
        try:
            import yfinance as yf
            import time
            now = time.time()
            if self._vix_cache and now - self._last_vix_time < 30:
                return self._vix_cache
            vix = yf.Ticker(self.config.vix_symbol)
            hist = vix.history(period="2d")
            if len(hist) >= 2:
                current = float(hist.iloc[-1]["Close"])
                prior = float(hist.iloc[-2]["Close"])
                change = (current - prior) / prior * 100
                self._vix_cache = (current, change)
                self._last_vix_time = now
                return (current, change)
            elif len(hist) == 1:
                current = float(hist.iloc[-1]["Close"])
                self._vix_cache = (current, 0.0)
                self._last_vix_time = now
                return (current, 0.0)
        except Exception as e:
            logger.warning(f"VIX fetch failed: {e}")
        return (0.0, 0.0)

    def get_chain(self, underlying: str, expiry: str | None = None) -> list[OptionChainEntry]:
        """Get full options chain for analysis."""
        if self.config.provider == "yahoo":
            return self._yahoo_chain(underlying, expiry)
        return []

    # -- Yahoo --

    def _yahoo_snapshot(self, underlying: str) -> OptionsSnapshot | None:
        try:
            import yfinance as yf
            ticker = yf.Ticker(underlying)
            expirations = ticker.options
            if not expirations:
                return None

            # Pick nearest expiry within chain_expiry_days
            target_date = datetime.now() + timedelta(days=self.config.chain_expiry_days)
            nearest_exp = min(expirations, key=lambda x: abs(
                datetime.strptime(x, "%Y-%m-%d") - target_date
            ))

            chain = ticker.option_chain(nearest_exp)
            calls = chain.calls
            puts = chain.puts

            # Current price
            hist = ticker.history(period="1d")
            if hist.empty:
                return None
            spot = float(hist.iloc[-1]["Close"])

            # ATM implied vol (nearest strike to spot)
            atm_calls = calls.iloc[(calls["strike"] - spot).abs().argsort()[:3]]
            atm_puts = puts.iloc[(puts["strike"] - spot).abs().argsort()[:3]]
            atm_iv = float(atm_calls["impliedVolatility"].mean())

            # Put/call ratios
            total_call_oi = int(calls["openInterest"].sum()) if "openInterest" in calls else 1
            total_put_oi = int(puts["openInterest"].sum()) if "openInterest" in puts else 0
            pc_ratio = total_put_oi / max(total_call_oi, 1)

            total_call_vol = int(calls["volume"].fillna(0).sum())
            total_put_vol = int(puts["volume"].fillna(0).sum())
            pc_vol_ratio = total_put_vol / max(total_call_vol, 1)

            # 25-delta skew approximation
            # Find ~25-delta put and call strikes
            otm_puts = puts[puts["strike"] < spot * 0.97]
            otm_calls = calls[calls["strike"] > spot * 1.03]
            put_iv_25d = float(otm_puts["impliedVolatility"].mean()) if not otm_puts.empty else atm_iv
            call_iv_25d = float(otm_calls["impliedVolatility"].mean()) if not otm_calls.empty else atm_iv
            skew = put_iv_25d - call_iv_25d

            # Max pain calculation
            max_pain = self._calc_max_pain(calls, puts)

            # Gamma exposure (simplified)
            gex = self._calc_gamma_exposure(calls, puts, spot)

            # VIX
            vix_level, vix_change = self.get_vix()

            # IV Rank (simplified - compare current to recent range)
            iv_rank = self._calc_iv_rank(underlying, atm_iv)

            return OptionsSnapshot(
                symbol=underlying,
                timestamp=datetime.now(),
                implied_vol=atm_iv,
                iv_rank=iv_rank,
                put_call_ratio=pc_ratio,
                put_call_volume_ratio=pc_vol_ratio,
                skew_25d=skew,
                vix=vix_level,
                vix_change=vix_change,
                gamma_exposure=gex,
                max_pain=max_pain,
            )
        except Exception as e:
            logger.warning(f"Yahoo options snapshot failed for {underlying}: {e}")
            return None

    def _yahoo_chain(self, underlying: str, expiry: str | None) -> list[OptionChainEntry]:
        try:
            import yfinance as yf
            ticker = yf.Ticker(underlying)
            expirations = ticker.options
            if not expirations:
                return []
            if expiry is None:
                expiry = expirations[0]
            chain = ticker.option_chain(expiry)
            entries = []
            for _, row in chain.calls.iterrows():
                entries.append(OptionChainEntry(
                    symbol=underlying, strike=float(row["strike"]),
                    expiry=expiry, option_type="call",
                    bid=float(row.get("bid", 0)), ask=float(row.get("ask", 0)),
                    last=float(row.get("lastPrice", 0)),
                    volume=int(row.get("volume", 0) or 0),
                    open_interest=int(row.get("openInterest", 0) or 0),
                    implied_vol=float(row.get("impliedVolatility", 0)),
                    delta=0, gamma=0, theta=0, vega=0,  # Yahoo doesn't provide greeks
                ))
            for _, row in chain.puts.iterrows():
                entries.append(OptionChainEntry(
                    symbol=underlying, strike=float(row["strike"]),
                    expiry=expiry, option_type="put",
                    bid=float(row.get("bid", 0)), ask=float(row.get("ask", 0)),
                    last=float(row.get("lastPrice", 0)),
                    volume=int(row.get("volume", 0) or 0),
                    open_interest=int(row.get("openInterest", 0) or 0),
                    implied_vol=float(row.get("impliedVolatility", 0)),
                    delta=0, gamma=0, theta=0, vega=0,
                ))
            return entries
        except Exception as e:
            logger.warning(f"Yahoo chain failed: {e}")
            return []

    # -- Tradier --

    def _tradier_snapshot(self, underlying: str) -> OptionsSnapshot | None:
        try:
            import httpx
            headers = {
                "Authorization": f"Bearer {self.config.tradier_token}",
                "Accept": "application/json",
            }
            # Get chains
            resp = httpx.get(
                "https://api.tradier.com/v1/markets/options/chains",
                params={"symbol": underlying, "greeks": "true"},
                headers=headers, timeout=10,
            )
            data = resp.json()
            options = data.get("options", {}).get("option", [])
            if not options:
                return None

            calls = [o for o in options if o["option_type"] == "call"]
            puts = [o for o in options if o["option_type"] == "put"]

            # Compute basic metrics
            avg_call_iv = sum(c.get("greeks", {}).get("mid_iv", 0) for c in calls) / max(len(calls), 1)
            call_oi = sum(c.get("open_interest", 0) for c in calls)
            put_oi = sum(p.get("open_interest", 0) for p in puts)

            vix_level, vix_change = self.get_vix()

            return OptionsSnapshot(
                symbol=underlying,
                timestamp=datetime.now(),
                implied_vol=avg_call_iv,
                iv_rank=50.0,  # simplified
                put_call_ratio=put_oi / max(call_oi, 1),
                put_call_volume_ratio=0,
                skew_25d=0,
                vix=vix_level,
                vix_change=vix_change,
                gamma_exposure=0,
                max_pain=0,
            )
        except Exception as e:
            logger.warning(f"Tradier snapshot failed: {e}")
            return None

    # -- Calculations --

    @staticmethod
    def _calc_max_pain(calls: Any, puts: Any) -> float:
        """Calculate max pain strike (strike where options expire worthless)."""
        try:
            strikes = sorted(set(calls["strike"].tolist() + puts["strike"].tolist()))
            min_pain = float("inf")
            max_pain_strike = 0.0
            for strike in strikes:
                call_pain = ((calls["strike"] - strike).clip(lower=0) * calls["openInterest"].fillna(0)).sum()
                put_pain = ((strike - puts["strike"]).clip(lower=0) * puts["openInterest"].fillna(0)).sum()
                total = call_pain + put_pain
                if total < min_pain:
                    min_pain = total
                    max_pain_strike = strike
            return float(max_pain_strike)
        except Exception:
            return 0.0

    @staticmethod
    def _calc_gamma_exposure(calls: Any, puts: Any, spot: float) -> float:
        """Simplified net gamma exposure estimate."""
        try:
            call_gex = (calls["openInterest"].fillna(0) * calls["impliedVolatility"].fillna(0)).sum()
            put_gex = (puts["openInterest"].fillna(0) * puts["impliedVolatility"].fillna(0)).sum()
            return float(call_gex - put_gex)
        except Exception:
            return 0.0

    @staticmethod
    def _calc_iv_rank(symbol: str, current_iv: float) -> float:
        """IV Rank: where current IV sits in 52-week range. 0=lowest, 100=highest."""
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1y")
            if len(hist) < 20:
                return 50.0
            # Approximate historical vol using 20-day rolling
            returns = hist["Close"].pct_change().dropna()
            rolling_vol = returns.rolling(20).std() * (252 ** 0.5)
            rolling_vol = rolling_vol.dropna()
            if rolling_vol.empty:
                return 50.0
            low = float(rolling_vol.min())
            high = float(rolling_vol.max())
            if high == low:
                return 50.0
            return min(100.0, max(0.0, (current_iv - low) / (high - low) * 100))
        except Exception:
            return 50.0
