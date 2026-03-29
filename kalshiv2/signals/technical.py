"""
Technical analysis indicators for signal generation.
All functions operate on numpy arrays for speed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class TechnicalReadout:
    """Aggregated technical indicator values for a single asset."""
    symbol: str
    rsi: float
    rsi_signal: float          # -1 (oversold/buy) to 1 (overbought/sell)
    vwap: float
    vwap_deviation_pct: float  # % above/below VWAP
    bb_upper: float
    bb_lower: float
    bb_mid: float
    bb_position: float         # 0=at lower, 1=at upper
    ema_fast: float
    ema_slow: float
    ema_crossover: float       # positive=bullish, negative=bearish
    macd: float
    macd_signal_line: float
    macd_histogram: float
    atr: float
    atr_pct: float             # ATR as % of price
    momentum_15m: float        # 15-min price change %
    momentum_5m: float         # 5-min price change %
    volume_ratio: float        # current vol vs avg vol


class TechnicalAnalyzer:
    """Compute technical indicators from price/volume arrays."""

    def __init__(
        self,
        rsi_period: int = 14,
        bb_period: int = 20,
        bb_std: float = 2.0,
        ema_fast: int = 9,
        ema_slow: int = 21,
        macd_signal: int = 9,
        atr_period: int = 14,
    ) -> None:
        self.rsi_period = rsi_period
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.ema_fast_period = ema_fast
        self.ema_slow_period = ema_slow
        self.macd_signal_period = macd_signal
        self.atr_period = atr_period

    def analyze(self, prices: np.ndarray, volumes: np.ndarray | None = None,
                highs: np.ndarray | None = None, lows: np.ndarray | None = None,
                symbol: str = "") -> TechnicalReadout | None:
        """Run all indicators on the given price series."""
        if len(prices) < max(self.bb_period, self.ema_slow_period, self.rsi_period) + 5:
            return None

        current = float(prices[-1])
        rsi = self.calc_rsi(prices)
        rsi_signal = self._rsi_to_signal(rsi)

        bb_upper, bb_mid, bb_lower = self.calc_bollinger(prices)
        bb_range = bb_upper - bb_lower
        bb_position = (current - bb_lower) / bb_range if bb_range > 0 else 0.5

        ema_f = self.calc_ema(prices, self.ema_fast_period)
        ema_s = self.calc_ema(prices, self.ema_slow_period)
        ema_crossover = (ema_f - ema_s) / ema_s * 100 if ema_s else 0

        macd_line, signal_line, histogram = self.calc_macd(prices)

        atr = self.calc_atr(prices, highs, lows)
        atr_pct = (atr / current * 100) if current else 0

        vwap = self.calc_vwap(prices, volumes) if volumes is not None and len(volumes) > 0 else current
        vwap_dev = (current - vwap) / vwap * 100 if vwap else 0

        momentum_15m = self._momentum(prices, 15)
        momentum_5m = self._momentum(prices, 5)

        vol_ratio = 1.0
        if volumes is not None and len(volumes) > 20:
            avg_vol = float(np.mean(volumes[-20:]))
            recent_vol = float(np.mean(volumes[-5:])) if len(volumes) >= 5 else float(volumes[-1])
            vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0

        return TechnicalReadout(
            symbol=symbol,
            rsi=rsi,
            rsi_signal=rsi_signal,
            vwap=vwap,
            vwap_deviation_pct=vwap_dev,
            bb_upper=bb_upper,
            bb_lower=bb_lower,
            bb_mid=bb_mid,
            bb_position=bb_position,
            ema_fast=ema_f,
            ema_slow=ema_s,
            ema_crossover=ema_crossover,
            macd=macd_line,
            macd_signal_line=signal_line,
            macd_histogram=histogram,
            atr=atr,
            atr_pct=atr_pct,
            momentum_15m=momentum_15m,
            momentum_5m=momentum_5m,
            volume_ratio=vol_ratio,
        )

    # -- Indicators --

    def calc_rsi(self, prices: np.ndarray) -> float:
        """Relative Strength Index."""
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        period = self.rsi_period
        if len(gains) < period:
            return 50.0

        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def calc_bollinger(self, prices: np.ndarray) -> tuple[float, float, float]:
        """Bollinger Bands (upper, mid, lower)."""
        period = self.bb_period
        if len(prices) < period:
            p = float(prices[-1])
            return (p, p, p)
        window = prices[-period:]
        mid = float(np.mean(window))
        std = float(np.std(window))
        return (mid + self.bb_std * std, mid, mid - self.bb_std * std)

    def calc_ema(self, prices: np.ndarray, period: int) -> float:
        """Exponential Moving Average."""
        if len(prices) < period:
            return float(prices[-1])
        multiplier = 2.0 / (period + 1)
        ema = float(np.mean(prices[:period]))
        for price in prices[period:]:
            ema = (float(price) - ema) * multiplier + ema
        return ema

    def calc_macd(self, prices: np.ndarray) -> tuple[float, float, float]:
        """MACD line, signal line, histogram."""
        fast = self.ema_fast_period
        slow = self.ema_slow_period
        sig = self.macd_signal_period

        if len(prices) < slow + sig:
            return (0.0, 0.0, 0.0)

        # Compute MACD line for each point
        macd_series = []
        for i in range(slow, len(prices) + 1):
            ema_f = self._ema_at(prices[:i], fast)
            ema_s = self._ema_at(prices[:i], slow)
            macd_series.append(ema_f - ema_s)

        macd_arr = np.array(macd_series)
        if len(macd_arr) < sig:
            return (float(macd_arr[-1]), 0.0, float(macd_arr[-1]))

        signal_line = self._ema_at(macd_arr, sig)
        macd_line = float(macd_arr[-1])
        histogram = macd_line - signal_line
        return (macd_line, signal_line, histogram)

    def calc_atr(self, prices: np.ndarray, highs: np.ndarray | None = None,
                 lows: np.ndarray | None = None) -> float:
        """Average True Range."""
        period = self.atr_period
        if highs is not None and lows is not None and len(highs) >= period:
            tr = np.maximum(
                highs[1:] - lows[1:],
                np.maximum(
                    np.abs(highs[1:] - prices[:-1]),
                    np.abs(lows[1:] - prices[:-1])
                )
            )
        else:
            # Approximate from close prices
            tr = np.abs(np.diff(prices))

        if len(tr) < period:
            return float(np.mean(tr)) if len(tr) > 0 else 0.0

        atr = float(np.mean(tr[:period]))
        for i in range(period, len(tr)):
            atr = (atr * (period - 1) + float(tr[i])) / period
        return atr

    def calc_vwap(self, prices: np.ndarray, volumes: np.ndarray) -> float:
        """Volume Weighted Average Price."""
        if len(prices) == 0 or len(volumes) == 0:
            return 0.0
        total_vol = np.sum(volumes)
        if total_vol == 0:
            return float(np.mean(prices))
        return float(np.sum(prices * volumes) / total_vol)

    # -- Helpers --

    def _ema_at(self, arr: np.ndarray, period: int) -> float:
        if len(arr) < period:
            return float(arr[-1]) if len(arr) > 0 else 0.0
        multiplier = 2.0 / (period + 1)
        ema = float(np.mean(arr[:period]))
        for val in arr[period:]:
            ema = (float(val) - ema) * multiplier + ema
        return ema

    @staticmethod
    def _rsi_to_signal(rsi: float) -> float:
        """Convert RSI to directional signal. Positive = overbought (lean NO/under)."""
        if rsi >= 70:
            return min(1.0, (rsi - 70) / 30)
        elif rsi <= 30:
            return max(-1.0, -(30 - rsi) / 30)
        return (rsi - 50) / 50  # mild signal in neutral zone

    @staticmethod
    def _momentum(prices: np.ndarray, periods: int) -> float:
        """Price change over N periods as percentage."""
        if len(prices) <= periods:
            return 0.0
        old = float(prices[-periods - 1])
        if old == 0:
            return 0.0
        return (float(prices[-1]) - old) / old * 100
