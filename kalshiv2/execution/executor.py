"""
Execution engine - the main event loop that ties everything together.
Scans for markets, generates signals, places bets, and tracks results.
"""

from __future__ import annotations

import json
import logging
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from kalshiv2.api.client import KalshiClient, KalshiOrder
from kalshiv2.config import BotConfig
from kalshiv2.feeds.futures_feed import FuturesFeed
from kalshiv2.feeds.options_feed import OptionsFeed
from kalshiv2.feeds.polymarket_feed import PolymarketFeed
from kalshiv2.risk.manager import BetRecord, RiskManager
from kalshiv2.signals.engine import SignalEngine
from kalshiv2.strategy.over_under import BetDecision, OverUnderStrategy

logger = logging.getLogger(__name__)


class Executor:
    """
    Main bot execution loop.

    Lifecycle:
    1. Initialize feeds, signals, strategy, risk
    2. Loop: scan markets → evaluate → place bets → monitor → settle
    3. Log everything for analysis
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self._running = False

        # Data directory
        self.data_dir = Path(config.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Initialize components
        self.kalshi = KalshiClient(config.kalshi)
        self.futures_feed = FuturesFeed(config.futures)
        self.options_feed = OptionsFeed(config.options)
        self.polymarket_feed = PolymarketFeed(config.polymarket)

        self.signal_engine = SignalEngine(
            config=config.signals,
            futures_feed=self.futures_feed,
            options_feed=self.options_feed,
            polymarket_feed=self.polymarket_feed,
        )

        self.risk = RiskManager(config.risk)

        self.strategy = OverUnderStrategy(
            signal_engine=self.signal_engine,
            risk_manager=self.risk,
        )

        # Track active orders for settlement
        self._active_orders: dict[str, tuple[KalshiOrder, BetDecision]] = {}
        self._trade_log: list[dict[str, Any]] = []

    def run(self) -> None:
        """Main event loop. Runs until interrupted."""
        self._running = True
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        logger.info("=" * 60)
        logger.info("  KalshiV2 Bot Starting")
        logger.info("=" * 60)
        logger.info(f"  Mode: {'DEMO' if self.config.kalshi.demo_mode else 'LIVE'}")
        logger.info(f"  Target events: {self.config.target_event_types}")
        logger.info(f"  Scan interval: {self.config.scan_interval_sec}s")
        logger.info(f"  Max daily loss: ${self.config.risk.max_daily_loss_usd}")
        logger.info(f"  Max single bet: ${self.config.risk.max_single_bet_usd}")
        logger.info("=" * 60)

        # Pre-warm Polymarket cache
        logger.info("Pre-warming Polymarket event cache...")
        self.polymarket_feed.fetch_events()
        for asset_key in ["SP500", "NASDAQ", "DOW", "BTC", "CRUDE", "GOLD"]:
            self.polymarket_feed.get_herd_signal(asset_key)
        logger.info("Polymarket cache ready")

        cycle = 0
        while self._running:
            cycle += 1
            try:
                self._run_cycle(cycle)
            except Exception as e:
                logger.error(f"Cycle {cycle} error: {e}", exc_info=True)

            if self._running:
                time.sleep(self.config.scan_interval_sec)

        self._shutdown()

    def _run_cycle(self, cycle: int) -> None:
        """Single scan-evaluate-execute cycle."""
        logger.debug(f"--- Cycle {cycle} ---")

        # 1. Check for settled positions
        self._check_settlements()

        # 2. Scan for 15-min markets
        markets = self.kalshi.find_15min_markets(self.config.target_event_types)
        if not markets:
            logger.debug("No eligible 15-min markets found")
            return

        logger.info(f"Found {len(markets)} eligible markets")

        # 3. Evaluate markets through strategy
        actionable = self.strategy.get_actionable_bets(markets)
        if not actionable:
            logger.debug("No actionable bets this cycle")
            return

        # 4. Execute top bets (limited by risk manager)
        for decision in actionable:
            if not self._running:
                break
            self._execute_bet(decision)

    def _execute_bet(self, decision: BetDecision) -> None:
        """Place a bet on Kalshi based on the strategy decision."""
        market = decision.market
        side = "yes" if decision.action == "BET_YES" else "no"
        price = decision.sizing.price_cents
        count = decision.sizing.contracts

        logger.info(
            f"PLACING BET: {market.ticker} | {side.upper()} {count}x @ {price}c | "
            f"Edge={decision.sizing.edge*100:.1f}% | {decision.reason}"
        )

        order = self.kalshi.place_order(
            ticker=market.ticker,
            side=side,
            price_cents=price,
            count=count,
        )

        if order:
            self._active_orders[market.ticker] = (order, decision)
            self.risk.record_bet(BetRecord(
                timestamp=datetime.now(),
                ticker=market.ticker,
                side=side,
                price=price / 100,
                size_usd=decision.sizing.size_usd,
                contracts=count,
                signal_score=decision.signal.score,
                signal_confidence=decision.signal.confidence,
            ))
            self._log_trade("BET_PLACED", decision, order)
            logger.info(f"Order placed: {order.order_id} status={order.status}")
        else:
            logger.warning(f"Failed to place order for {market.ticker}")
            self._log_trade("BET_FAILED", decision)

    def _check_settlements(self) -> None:
        """Check if any active orders have settled."""
        to_remove = []
        for ticker, (order, decision) in self._active_orders.items():
            market = self.kalshi.get_market(ticker)
            if market is None:
                continue

            if market.status == "settled" or market.result:
                result = market.result  # "yes" or "no"
                side = "yes" if decision.action == "BET_YES" else "no"
                won = result == side

                price_paid = order.price / 100
                if won:
                    pnl = (1.0 - price_paid) * order.count
                    self.risk.record_result(ticker, "win", pnl)
                else:
                    pnl = -price_paid * order.count
                    self.risk.record_result(ticker, "loss", pnl)

                logger.info(f"SETTLED: {ticker} result={result} our_side={side} "
                             f"{'WIN' if won else 'LOSS'} PnL=${pnl:.2f}")
                self._log_trade("SETTLED", decision, order,
                                extra={"result": result, "won": won, "pnl": pnl})
                to_remove.append(ticker)

        for ticker in to_remove:
            del self._active_orders[ticker]

    def _log_trade(self, event: str, decision: BetDecision,
                   order: KalshiOrder | None = None,
                   extra: dict[str, Any] | None = None) -> None:
        """Log trade to file for analysis."""
        entry = {
            "event": event,
            "timestamp": datetime.now().isoformat(),
            "decision": decision.to_dict(),
        }
        if order:
            entry["order"] = {
                "order_id": order.order_id,
                "side": order.side,
                "price": order.price,
                "count": order.count,
                "status": order.status,
            }
        if extra:
            entry.update(extra)

        self._trade_log.append(entry)

        # Append to daily log file
        log_file = self.data_dir / f"trades_{datetime.now().strftime('%Y%m%d')}.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _handle_shutdown(self, sig: int, frame: Any) -> None:
        logger.info("Shutdown signal received")
        self._running = False

    def _shutdown(self) -> None:
        """Clean shutdown - cancel resting orders, save state."""
        logger.info("Shutting down...")

        # Cancel any resting orders
        for ticker, (order, _) in self._active_orders.items():
            if order.status == "resting":
                logger.info(f"Canceling resting order: {order.order_id}")
                self.kalshi.cancel_order(order.order_id)

        # Save session summary
        stats = self.risk.get_stats()
        summary = {
            "shutdown_time": datetime.now().isoformat(),
            "stats": stats,
            "total_trades": len(self._trade_log),
            "active_orders": len(self._active_orders),
        }
        summary_file = self.data_dir / f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        summary_file.write_text(json.dumps(summary, indent=2))

        logger.info(f"Session summary saved to {summary_file}")
        logger.info(f"Final stats: {json.dumps(stats, indent=2)}")
        logger.info("Goodbye!")
