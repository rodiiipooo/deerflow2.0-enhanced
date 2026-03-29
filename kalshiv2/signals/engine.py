"""
Signal Engine - combines all data sources into a unified trade signal.
Weights futures momentum, options IV, Polymarket herd, and technicals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kalshiv2.config import SignalConfig
from kalshiv2.feeds.futures_feed import FuturesFeed, FuturesSnapshot
from kalshiv2.feeds.options_feed import OptionsFeed, OptionsSnapshot
from kalshiv2.feeds.polymarket_feed import HerdSignal, PolymarketFeed
from kalshiv2.signals.technical import TechnicalAnalyzer, TechnicalReadout

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    """
    Unified trade signal output.
    score: -1.0 (strong UNDER/NO) to 1.0 (strong OVER/YES)
    confidence: 0.0 to 1.0
    """
    score: float                    # -1 to 1
    confidence: float               # 0 to 1
    direction: str                  # "OVER" or "UNDER" or "HOLD"
    side: str                       # "YES" or "NO" or "HOLD"
    asset_key: str
    timestamp: datetime = field(default_factory=datetime.now)

    # Component scores for transparency
    futures_momentum_score: float = 0.0
    futures_mean_rev_score: float = 0.0
    options_ivol_score: float = 0.0
    options_skew_score: float = 0.0
    polymarket_herd_score: float = 0.0
    technical_rsi_score: float = 0.0
    technical_vwap_score: float = 0.0
    technical_bb_score: float = 0.0

    # Raw data
    futures_snapshot: FuturesSnapshot | None = None
    options_snapshot: OptionsSnapshot | None = None
    herd_signal: HerdSignal | None = None
    technical_readout: TechnicalReadout | None = None

    @property
    def edge_pct(self) -> float:
        """Perceived edge as percentage above 50/50."""
        return abs(self.score) * self.confidence * 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 4),
            "direction": self.direction,
            "side": self.side,
            "edge_pct": round(self.edge_pct, 2),
            "asset_key": self.asset_key,
            "components": {
                "futures_momentum": round(self.futures_momentum_score, 4),
                "futures_mean_rev": round(self.futures_mean_rev_score, 4),
                "options_ivol": round(self.options_ivol_score, 4),
                "options_skew": round(self.options_skew_score, 4),
                "polymarket_herd": round(self.polymarket_herd_score, 4),
                "technical_rsi": round(self.technical_rsi_score, 4),
                "technical_vwap": round(self.technical_vwap_score, 4),
                "technical_bb": round(self.technical_bb_score, 4),
            },
        }


# Maps Kalshi event types to futures symbols and asset keys
EVENT_ASSET_MAP: dict[str, dict[str, str]] = {
    "INXD": {"symbol": "ES=F", "options": "SPY", "asset_key": "SP500"},
    "INX":  {"symbol": "ES=F", "options": "SPY", "asset_key": "SP500"},
    "NASDAQ": {"symbol": "NQ=F", "options": "QQQ", "asset_key": "NASDAQ"},
    "COMP": {"symbol": "NQ=F", "options": "QQQ", "asset_key": "NASDAQ"},
}


class SignalEngine:
    """
    Combines futures, options, Polymarket, and technical signals
    into a single actionable TradeSignal for each event.
    """

    def __init__(
        self,
        config: SignalConfig,
        futures_feed: FuturesFeed,
        options_feed: OptionsFeed,
        polymarket_feed: PolymarketFeed,
    ) -> None:
        self.config = config
        self.futures = futures_feed
        self.options = options_feed
        self.polymarket = polymarket_feed
        self.tech = TechnicalAnalyzer(
            rsi_period=config.rsi_period,
            bb_period=config.bb_period,
            bb_std=config.bb_std,
            ema_fast=config.ema_fast,
            ema_slow=config.ema_slow,
            macd_signal=config.macd_signal,
            atr_period=config.atr_period,
        )

    def generate_signal(self, event_type: str, kalshi_mid: float = 0.5) -> TradeSignal:
        """
        Generate a unified trade signal for a Kalshi event type.

        Args:
            event_type: Kalshi event type (e.g. "INXD", "NASDAQ")
            kalshi_mid: Current Kalshi mid-market price for the event
        """
        mapping = EVENT_ASSET_MAP.get(event_type, {})
        fut_symbol = mapping.get("symbol", "ES=F")
        opt_symbol = mapping.get("options", "SPY")
        asset_key = mapping.get("asset_key", "SP500")

        # 1. Futures data
        fut_snap = self.futures.get_snapshot(fut_symbol)
        prices = self.futures.get_prices_array(fut_symbol)
        volumes = self.futures.get_volumes_array(fut_symbol)

        # 2. Options data
        opt_snap = self.options.get_snapshot(opt_symbol)

        # 3. Polymarket herd signal (uses cached matches)
        herd = self.polymarket.get_herd_signal(asset_key, kalshi_mid=kalshi_mid)

        # 4. Technical analysis
        tech_readout = self.tech.analyze(prices, volumes, symbol=fut_symbol) if len(prices) > 0 else None

        # -- Compute component scores --

        # Futures momentum: positive change = lean OVER
        fut_momentum = 0.0
        if fut_snap:
            change = fut_snap.change_pct
            fut_momentum = max(-1.0, min(1.0, change / 2.0))  # ±2% maps to ±1

        # Futures mean reversion: strong move = expect pullback
        fut_mean_rev = 0.0
        if fut_snap and abs(fut_snap.change_pct) > 0.5:
            fut_mean_rev = -fut_momentum * 0.5  # fade the move

        # Options IV signal: high IV rank = expect mean reversion
        opt_ivol = 0.0
        if opt_snap:
            if opt_snap.iv_rank > 70:
                opt_ivol = -0.3  # high IV = expect down/flat
            elif opt_snap.iv_rank < 30:
                opt_ivol = 0.3   # low IV = calm, trend may continue
            # VIX spike = bearish
            if opt_snap.vix_change > 5:
                opt_ivol -= 0.3
            elif opt_snap.vix_change < -5:
                opt_ivol += 0.2

        # Options skew signal: put skew = market hedging downside
        opt_skew = 0.0
        if opt_snap:
            skew = opt_snap.skew_25d
            if skew > 0.05:
                opt_skew = -0.3  # expensive puts = bearish hedge pressure
            elif skew < -0.02:
                opt_skew = 0.2   # call skew = bullish

            # Put/call ratio > 1 = bearish sentiment
            if opt_snap.put_call_volume_ratio > 1.3:
                opt_skew -= 0.2
            elif opt_snap.put_call_volume_ratio < 0.7:
                opt_skew += 0.2

        # Polymarket herd signal
        poly_herd = herd.direction * herd.confidence if herd.confidence > 0.2 else 0.0

        # Technical signals
        tech_rsi = 0.0
        tech_vwap = 0.0
        tech_bb = 0.0
        if tech_readout:
            # RSI: overbought = lean UNDER, oversold = lean OVER
            tech_rsi = -tech_readout.rsi_signal

            # VWAP: above VWAP = bullish (lean OVER)
            if abs(tech_readout.vwap_deviation_pct) > 0.1:
                tech_vwap = max(-1.0, min(1.0, tech_readout.vwap_deviation_pct / 1.0))

            # Bollinger: near upper = lean UNDER, near lower = lean OVER
            tech_bb = -(tech_readout.bb_position - 0.5) * 2.0

        # -- Weighted combination --
        w = self.config
        raw_score = (
            w.weight_futures_momentum * fut_momentum
            + w.weight_futures_mean_rev * fut_mean_rev
            + w.weight_options_ivol * opt_ivol
            + w.weight_options_skew * opt_skew
            + w.weight_polymarket_herd * poly_herd
            + w.weight_technical_rsi * tech_rsi
            + w.weight_technical_vwap * tech_vwap
            + w.weight_technical_bb * tech_bb
        )
        score = max(-1.0, min(1.0, raw_score))

        # Confidence: based on agreement between sources
        component_signs = [
            fut_momentum, fut_mean_rev, opt_ivol, opt_skew,
            poly_herd, tech_rsi, tech_vwap, tech_bb,
        ]
        nonzero = [s for s in component_signs if abs(s) > 0.05]
        if nonzero:
            agreement = sum(1 for s in nonzero if (s > 0) == (score > 0)) / len(nonzero)
        else:
            agreement = 0.0

        # Also factor in data availability
        data_score = sum([
            0.3 if fut_snap else 0,
            0.25 if opt_snap else 0,
            0.2 if herd.confidence > 0.1 else 0,
            0.25 if tech_readout else 0,
        ])

        confidence = agreement * 0.6 + data_score * 0.4

        # Direction
        if abs(score) < 0.05 or confidence < self.config.min_confidence:
            direction = "HOLD"
            side = "HOLD"
        elif score > 0:
            direction = "OVER"
            side = "YES"
        else:
            direction = "UNDER"
            side = "NO"

        return TradeSignal(
            score=score,
            confidence=confidence,
            direction=direction,
            side=side,
            asset_key=asset_key,
            futures_momentum_score=fut_momentum,
            futures_mean_rev_score=fut_mean_rev,
            options_ivol_score=opt_ivol,
            options_skew_score=opt_skew,
            polymarket_herd_score=poly_herd,
            technical_rsi_score=tech_rsi,
            technical_vwap_score=tech_vwap,
            technical_bb_score=tech_bb,
            futures_snapshot=fut_snap,
            options_snapshot=opt_snap,
            herd_signal=herd,
            technical_readout=tech_readout,
        )
