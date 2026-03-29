"""
KalshiV2 Bot - CLI Entry Point
===============================

Usage:
    python -m kalshiv2                      # Run the bot with default config
    python -m kalshiv2 --config my.yaml     # Use custom config
    python -m kalshiv2 --demo               # Force demo mode
    python -m kalshiv2 --dry-run            # Evaluate but don't place bets
    python -m kalshiv2 status               # Show current signal snapshot
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kalshiv2",
        description="KalshiV2 - Prediction market bet bot for 15-min over/under events",
    )
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "status", "backtest", "config"],
                        help="Command to execute (default: run)")
    parser.add_argument("--config", "-c", type=str, default=None,
                        help="Path to config YAML file")
    parser.add_argument("--demo", action="store_true",
                        help="Force demo mode (no real money)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Evaluate signals but don't place bets")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Log level (default: INFO)")
    parser.add_argument("--data-dir", default="kalshiv2_data",
                        help="Data directory for logs and cache")

    args = parser.parse_args()

    # Setup logging
    from kalshiv2.utils.logger import setup_logging
    setup_logging(level=args.log_level, log_dir=args.data_dir)

    # Load config
    from kalshiv2.config import load_config
    config = load_config(args.config)
    config.data_dir = args.data_dir
    config.log_level = args.log_level

    if args.demo:
        config.kalshi.demo_mode = True

    if args.command == "config":
        _show_config(config)
    elif args.command == "status":
        _show_status(config)
    elif args.command == "run":
        _run_bot(config, dry_run=args.dry_run)


def _show_config(config: object) -> None:
    """Print current configuration."""
    import dataclasses
    print(json.dumps(dataclasses.asdict(config), indent=2, default=str))


def _show_status(config: object) -> None:
    """Show a one-shot signal snapshot without trading."""
    from kalshiv2.config import BotConfig
    assert isinstance(config, BotConfig)

    from kalshiv2.feeds.futures_feed import FuturesFeed
    from kalshiv2.feeds.options_feed import OptionsFeed
    from kalshiv2.feeds.polymarket_feed import PolymarketFeed
    from kalshiv2.signals.engine import SignalEngine

    print("\n=== KalshiV2 Signal Snapshot ===\n")

    futures = FuturesFeed(config.futures)
    options = OptionsFeed(config.options)
    polymarket = PolymarketFeed(config.polymarket, cache_dir=config.data_dir)
    engine = SignalEngine(config.signals, futures, options, polymarket)

    # Show futures
    print("--- Futures ---")
    for sym in config.futures.symbols[:4]:
        snap = futures.get_snapshot(sym)
        if snap:
            print(f"  {sym}: ${snap.price:.2f} ({snap.change_pct:+.2f}%)")
        else:
            print(f"  {sym}: no data")

    # Show VIX
    print("\n--- Options / VIX ---")
    vix, vix_chg = options.get_vix()
    print(f"  VIX: {vix:.2f} ({vix_chg:+.2f}%)")
    opt_snap = options.get_snapshot("SPY")
    if opt_snap:
        print(f"  SPY IV: {opt_snap.implied_vol:.3f} | IV Rank: {opt_snap.iv_rank:.0f}")
        print(f"  P/C Ratio: {opt_snap.put_call_ratio:.2f} | Skew: {opt_snap.skew_25d:.4f}")

    # Show Polymarket herd
    print("\n--- Polymarket Herd ---")
    for asset in ["SP500", "NASDAQ", "BTC"]:
        herd = polymarket.get_herd_signal(asset)
        dir_str = "BULL" if herd.direction > 0.1 else ("BEAR" if herd.direction < -0.1 else "NEUTRAL")
        print(f"  {asset}: {dir_str} (dir={herd.direction:+.2f}, "
              f"conf={herd.confidence:.2f}, markets={herd.num_markets})")

    # Show signals
    print("\n--- Signals ---")
    for event_type in config.target_event_types:
        sig = engine.generate_signal(event_type)
        print(f"  {event_type}: {sig.direction} "
              f"(score={sig.score:+.3f}, conf={sig.confidence:.2f}, "
              f"edge={sig.edge_pct:.1f}%)")

    print()


def _run_bot(config: object, dry_run: bool = False) -> None:
    """Run the main trading bot."""
    from kalshiv2.config import BotConfig
    assert isinstance(config, BotConfig)

    if dry_run:
        print("\n*** DRY RUN MODE - No bets will be placed ***\n")
        # In dry-run, just show status repeatedly
        import time
        try:
            while True:
                _show_status(config)
                print(f"[Dry run] Sleeping {config.scan_interval_sec}s...\n")
                time.sleep(config.scan_interval_sec)
        except KeyboardInterrupt:
            print("\nDry run stopped.")
            return

    from kalshiv2.execution.executor import Executor
    executor = Executor(config)
    executor.run()


if __name__ == "__main__":
    main()
