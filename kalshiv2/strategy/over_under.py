"""
Over/Under strategy for Kalshi 15-minute events.
Combines signal engine output with market microstructure analysis
to decide when and how to bet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kalshiv2.api.client import KalshiMarket
from kalshiv2.risk.manager import RiskManager, SizingResult
from kalshiv2.signals.engine import SignalEngine, TradeSignal

logger = logging.getLogger(__name__)


@dataclass
class BetDecision:
    """Complete bet decision with full context."""
    market: KalshiMarket
    signal: TradeSignal
    sizing: SizingResult
    action: str            # "BET_YES", "BET_NO", "SKIP"
    reason: str
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def should_bet(self) -> bool:
        return self.action.startswith("BET_")

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_ticker": self.market.ticker,
            "market_title": self.market.title,
            "action": self.action,
            "reason": self.reason,
            "side": "yes" if self.action == "BET_YES" else ("no" if self.action == "BET_NO" else "none"),
            "contracts": self.sizing.contracts if self.should_bet else 0,
            "price_cents": self.sizing.price_cents if self.should_bet else 0,
            "size_usd": self.sizing.size_usd if self.should_bet else 0,
            "signal": self.signal.to_dict(),
            "edge_pct": round(self.sizing.edge * 100, 2) if self.should_bet else 0,
            "minutes_to_close": round(self.market.minutes_to_close, 1),
        }


class OverUnderStrategy:
    """
    Strategy for 15-minute "Will X be over/under Y" events on Kalshi.

    Decision flow:
    1. Filter markets by time-to-close and liquidity
    2. Generate signal for each eligible market
    3. Check risk limits and calculate sizing
    4. Apply microstructure filters (spread, volume)
    5. Output bet decisions
    """

    def __init__(
        self,
        signal_engine: SignalEngine,
        risk_manager: RiskManager,
        min_volume: int = 10,
        max_spread: float = 0.10,
        min_minutes_to_close: float = 3.0,
        max_minutes_to_close: float = 12.0,
        min_open_interest: int = 5,
    ) -> None:
        self.signal_engine = signal_engine
        self.risk = risk_manager
        self.min_volume = min_volume
        self.max_spread = max_spread
        self.min_minutes_to_close = min_minutes_to_close
        self.max_minutes_to_close = max_minutes_to_close
        self.min_open_interest = min_open_interest

    def evaluate_market(self, market: KalshiMarket) -> BetDecision:
        """Evaluate a single market and produce a bet decision."""

        # 1. Time filter
        ttc = market.minutes_to_close
        if ttc < self.min_minutes_to_close:
            return self._skip(market, f"Too close to expiry: {ttc:.1f}m")
        if ttc > self.max_minutes_to_close:
            return self._skip(market, f"Too far from expiry: {ttc:.1f}m")

        # 2. Liquidity filter
        if market.volume < self.min_volume:
            return self._skip(market, f"Low volume: {market.volume}")
        if market.spread > self.max_spread:
            return self._skip(market, f"Wide spread: {market.spread:.2f}")
        if market.open_interest < self.min_open_interest:
            return self._skip(market, f"Low OI: {market.open_interest}")

        # 3. Generate signal
        # Determine event type from ticker
        event_type = self._ticker_to_event_type(market.event_ticker)
        signal = self.signal_engine.generate_signal(event_type, kalshi_mid=market.yes_mid)

        if signal.direction == "HOLD":
            return self._skip(market, f"Signal says HOLD (score={signal.score:.3f}, conf={signal.confidence:.3f})",
                              signal=signal)

        # 4. Determine price for our side
        if signal.side == "YES":
            market_price = market.yes_ask  # we'd buy at the ask
        else:
            market_price = market.no_ask

        # 5. Risk check and sizing
        sizing = self.risk.check_and_size(signal, market_price)
        if not sizing.approved:
            return self._skip(market, f"Risk rejected: {sizing.reason}", signal=signal, sizing=sizing)

        # 6. Price improvement: try to get midpoint or better
        if signal.side == "YES":
            target_price = int(market.yes_mid * 100)
            # Don't pay more than ask
            target_price = min(target_price, int(market.yes_ask * 100))
        else:
            target_price = int(market.no_mid * 100)
            target_price = min(target_price, int(market.no_ask * 100))
        target_price = max(1, min(99, target_price))
        sizing.price_cents = target_price

        action = f"BET_{signal.side}"
        reason = (
            f"{signal.direction} signal (score={signal.score:.3f}, "
            f"conf={signal.confidence:.2f}, edge={sizing.edge*100:.1f}%) | "
            f"{sizing.contracts}x @ {target_price}c = ${sizing.size_usd:.2f}"
        )

        logger.info(f"[{market.ticker}] {action}: {reason}")

        return BetDecision(
            market=market,
            signal=signal,
            sizing=sizing,
            action=action,
            reason=reason,
        )

    def evaluate_markets(self, markets: list[KalshiMarket]) -> list[BetDecision]:
        """Evaluate multiple markets and return sorted decisions."""
        decisions = []
        for market in markets:
            decision = self.evaluate_market(market)
            decisions.append(decision)

        # Sort by edge (best bets first)
        decisions.sort(key=lambda d: d.sizing.edge if d.should_bet else 0, reverse=True)
        return decisions

    def get_actionable_bets(self, markets: list[KalshiMarket]) -> list[BetDecision]:
        """Return only decisions that should result in bets."""
        all_decisions = self.evaluate_markets(markets)
        return [d for d in all_decisions if d.should_bet]

    @staticmethod
    def _ticker_to_event_type(event_ticker: str) -> str:
        """Extract event type from Kalshi event ticker."""
        # Tickers like "INXD-24MAR28-T5515" → "INXD"
        parts = event_ticker.split("-")
        return parts[0] if parts else "INXD"

    @staticmethod
    def _skip(market: KalshiMarket, reason: str,
              signal: TradeSignal | None = None,
              sizing: SizingResult | None = None) -> BetDecision:
        return BetDecision(
            market=market,
            signal=signal or TradeSignal(
                score=0, confidence=0, direction="HOLD",
                side="HOLD", asset_key="",
            ),
            sizing=sizing or SizingResult(
                approved=False, contracts=0, size_usd=0,
                price_cents=0, reason=reason,
            ),
            action="SKIP",
            reason=reason,
        )
