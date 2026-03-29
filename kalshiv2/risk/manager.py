"""
Risk management - position sizing, loss limits, and bet validation.
Uses fractional Kelly criterion for sizing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any

from kalshiv2.config import RiskConfig
from kalshiv2.signals.engine import TradeSignal

logger = logging.getLogger(__name__)


@dataclass
class BetRecord:
    """Record of a placed bet for tracking."""
    timestamp: datetime
    ticker: str
    side: str
    price: float
    size_usd: float
    contracts: int
    signal_score: float
    signal_confidence: float
    result: str = ""  # "win", "loss", or "" if pending
    pnl_usd: float = 0.0


@dataclass
class RiskState:
    """Current risk state for the session."""
    daily_pnl: float = 0.0
    daily_bets: int = 0
    open_positions: int = 0
    consecutive_losses: int = 0
    last_loss_time: float = 0.0
    today: str = field(default_factory=lambda: date.today().isoformat())
    bets: list[BetRecord] = field(default_factory=list)

    def reset_if_new_day(self) -> None:
        today = date.today().isoformat()
        if self.today != today:
            self.daily_pnl = 0.0
            self.daily_bets = 0
            self.consecutive_losses = 0
            self.today = today
            # Keep historical bets but clear pending state
            logger.info("New trading day - risk counters reset")


@dataclass
class SizingResult:
    """Output from position sizing calculation."""
    approved: bool
    contracts: int
    size_usd: float
    price_cents: int
    reason: str = ""
    kelly_fraction: float = 0.0
    edge: float = 0.0


class RiskManager:
    """
    Validates bets against risk limits and calculates position sizes
    using fractional Kelly criterion.
    """

    def __init__(self, config: RiskConfig) -> None:
        self.config = config
        self.state = RiskState()

    def check_and_size(self, signal: TradeSignal, market_price: float) -> SizingResult:
        """
        Check if a bet is allowed and calculate position size.

        Args:
            signal: Trade signal from the signal engine
            market_price: Current market price (0-1) for our side
        """
        self.state.reset_if_new_day()

        # Gate checks
        if rejected := self._gate_checks(signal):
            return rejected

        # Calculate edge and Kelly size
        edge = self._calc_edge(signal, market_price)
        if edge < self.config.min_edge_pct / 100:
            return SizingResult(
                approved=False, contracts=0, size_usd=0, price_cents=0,
                reason=f"Edge too small: {edge*100:.1f}% < {self.config.min_edge_pct}% minimum",
                edge=edge,
            )

        # Kelly criterion sizing
        kelly_f = self._kelly_size(edge, market_price)
        bet_usd = self._apply_size_limits(signal, kelly_f)

        # Convert to contracts
        price_cents = max(1, min(99, int(market_price * 100)))
        contracts = max(1, int(bet_usd / (price_cents / 100)))

        return SizingResult(
            approved=True,
            contracts=contracts,
            size_usd=bet_usd,
            price_cents=price_cents,
            kelly_fraction=kelly_f,
            edge=edge,
        )

    def record_bet(self, record: BetRecord) -> None:
        """Record a bet that was placed."""
        self.state.bets.append(record)
        self.state.daily_bets += 1
        self.state.open_positions += 1

    def record_result(self, ticker: str, result: str, pnl: float) -> None:
        """Record the result of a settled bet."""
        self.state.daily_pnl += pnl
        self.state.open_positions = max(0, self.state.open_positions - 1)

        if result == "loss":
            self.state.consecutive_losses += 1
            self.state.last_loss_time = time.time()
        else:
            self.state.consecutive_losses = 0

        # Update bet record
        for bet in reversed(self.state.bets):
            if bet.ticker == ticker and bet.result == "":
                bet.result = result
                bet.pnl_usd = pnl
                break

        logger.info(f"Result: {ticker} {result} PnL=${pnl:.2f} | "
                     f"Daily PnL=${self.state.daily_pnl:.2f}")

    def get_stats(self) -> dict[str, Any]:
        """Get current risk statistics."""
        total_bets = len(self.state.bets)
        wins = sum(1 for b in self.state.bets if b.result == "win")
        losses = sum(1 for b in self.state.bets if b.result == "loss")
        pending = sum(1 for b in self.state.bets if b.result == "")
        total_pnl = sum(b.pnl_usd for b in self.state.bets)
        return {
            "daily_pnl": round(self.state.daily_pnl, 2),
            "daily_bets": self.state.daily_bets,
            "open_positions": self.state.open_positions,
            "consecutive_losses": self.state.consecutive_losses,
            "total_bets": total_bets,
            "wins": wins,
            "losses": losses,
            "pending": pending,
            "win_rate": round(wins / max(wins + losses, 1) * 100, 1),
            "total_pnl": round(total_pnl, 2),
        }

    # -- Internal --

    def _gate_checks(self, signal: TradeSignal) -> SizingResult | None:
        """Run risk gate checks. Returns SizingResult if rejected."""
        # Daily loss limit
        if self.state.daily_pnl <= -self.config.max_daily_loss_usd:
            return SizingResult(
                approved=False, contracts=0, size_usd=0, price_cents=0,
                reason=f"Daily loss limit hit: ${self.state.daily_pnl:.2f}",
            )

        # Daily bet count
        if self.state.daily_bets >= self.config.max_daily_bets:
            return SizingResult(
                approved=False, contracts=0, size_usd=0, price_cents=0,
                reason=f"Daily bet limit hit: {self.state.daily_bets}",
            )

        # Open position limit
        if self.state.open_positions >= self.config.max_open_positions:
            return SizingResult(
                approved=False, contracts=0, size_usd=0, price_cents=0,
                reason=f"Max open positions: {self.state.open_positions}",
            )

        # Cooldown after consecutive losses
        if self.state.consecutive_losses >= 3:
            elapsed = time.time() - self.state.last_loss_time
            if elapsed < self.config.cooldown_after_loss_sec:
                remaining = self.config.cooldown_after_loss_sec - elapsed
                return SizingResult(
                    approved=False, contracts=0, size_usd=0, price_cents=0,
                    reason=f"Loss cooldown: {remaining:.0f}s remaining after {self.state.consecutive_losses} consecutive losses",
                )

        # Minimum confidence
        if signal.confidence < 0.4:
            return SizingResult(
                approved=False, contracts=0, size_usd=0, price_cents=0,
                reason=f"Confidence too low: {signal.confidence:.2f}",
            )

        return None

    def _calc_edge(self, signal: TradeSignal, market_price: float) -> float:
        """
        Calculate perceived edge.
        Edge = our estimated probability - market implied probability.
        """
        # Convert signal score to probability estimate
        # score of +1 = 100% OVER, -1 = 0% OVER (100% UNDER)
        our_prob = 0.5 + signal.score * signal.confidence * 0.5

        if signal.side == "YES":
            edge = our_prob - market_price
        elif signal.side == "NO":
            edge = (1 - our_prob) - (1 - market_price)
        else:
            edge = 0.0

        return max(0, edge)

    def _kelly_size(self, edge: float, market_price: float) -> float:
        """
        Fractional Kelly criterion.
        f* = (bp - q) / b
        where b = odds, p = win prob, q = 1-p
        """
        if market_price <= 0 or market_price >= 1:
            return 0.0

        b = (1 - market_price) / market_price  # decimal odds
        p = market_price + edge  # our estimated win probability
        q = 1 - p

        kelly = (b * p - q) / b if b > 0 else 0
        kelly = max(0, kelly)

        return kelly * self.config.kelly_fraction

    def _apply_size_limits(self, signal: TradeSignal, kelly_f: float) -> float:
        """Apply position size limits."""
        # Base size from Kelly
        # Use a notional bankroll of daily limit * 10 as reference
        bankroll = self.config.max_daily_loss_usd * 10
        kelly_usd = bankroll * kelly_f

        # Choose between default and strong bet size
        if signal.confidence >= 0.75:
            base = self.config.strong_bet_usd
        else:
            base = self.config.default_bet_usd

        # Use the smaller of Kelly and base
        size = min(kelly_usd, base) if kelly_usd > 0 else base

        # Hard cap
        size = min(size, self.config.max_single_bet_usd)

        # Reduce if near daily limit
        remaining = self.config.max_daily_loss_usd + self.state.daily_pnl
        size = min(size, remaining)

        return max(1.0, round(size, 2))
