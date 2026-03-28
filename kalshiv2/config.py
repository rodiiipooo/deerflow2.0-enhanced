"""
Central configuration for KalshiV2 bot.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class KalshiAPIConfig:
    base_url: str = "https://trading-api.kalshi.com/trade-api/v2"
    api_key: str = ""
    private_key_path: str = ""
    demo_mode: bool = True  # use demo endpoint by default

    @property
    def effective_url(self) -> str:
        if self.demo_mode:
            return "https://demo-api.kalshi.co/trade-api/v2"
        return self.base_url


@dataclass
class PolymarketConfig:
    api_url: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com"
    poll_interval_sec: float = 10.0


@dataclass
class FuturesConfig:
    provider: str = "yahoo"  # yahoo | alpaca | polygon
    symbols: list[str] = field(default_factory=lambda: [
        "ES=F",   # S&P 500 E-mini
        "NQ=F",   # Nasdaq 100 E-mini
        "YM=F",   # Dow E-mini
        "CL=F",   # Crude Oil
        "GC=F",   # Gold
        "BTC-USD", # Bitcoin
    ])
    alpaca_api_key: str = ""
    alpaca_secret: str = ""
    polygon_api_key: str = ""


@dataclass
class OptionsConfig:
    provider: str = "yahoo"  # yahoo | polygon | tradier
    vix_symbol: str = "^VIX"
    spy_symbol: str = "SPY"
    chain_expiry_days: int = 7  # look at weeklies
    tradier_token: str = ""


@dataclass
class SignalConfig:
    # Technical indicator parameters
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    bb_period: int = 20
    bb_std: float = 2.0
    vwap_lookback_minutes: int = 60
    ema_fast: int = 9
    ema_slow: int = 21
    macd_signal: int = 9
    atr_period: int = 14

    # Signal weights
    weight_futures_momentum: float = 0.20
    weight_futures_mean_rev: float = 0.10
    weight_options_ivol: float = 0.15
    weight_options_skew: float = 0.10
    weight_polymarket_herd: float = 0.20
    weight_technical_rsi: float = 0.10
    weight_technical_vwap: float = 0.08
    weight_technical_bb: float = 0.07

    # Thresholds
    min_confidence: float = 0.60  # min score to place a bet
    strong_confidence: float = 0.75  # increased sizing


@dataclass
class RiskConfig:
    max_daily_loss_usd: float = 100.0
    max_single_bet_usd: float = 25.0
    max_open_positions: int = 5
    max_daily_bets: int = 30
    default_bet_usd: float = 10.0
    strong_bet_usd: float = 20.0
    kelly_fraction: float = 0.25  # quarter-Kelly
    min_edge_pct: float = 3.0  # min perceived edge to bet
    cooldown_after_loss_sec: float = 120.0


@dataclass
class BotConfig:
    kalshi: KalshiAPIConfig = field(default_factory=KalshiAPIConfig)
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    futures: FuturesConfig = field(default_factory=FuturesConfig)
    options: OptionsConfig = field(default_factory=OptionsConfig)
    signals: SignalConfig = field(default_factory=SignalConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)

    # Event targeting
    target_event_types: list[str] = field(default_factory=lambda: [
        "INXD",   # S&P 500 above/below
        "NASDAQ", # Nasdaq above/below
        "INX",    # S&P 500 close
        "COMP",   # Nasdaq close
    ])
    event_duration_minutes: int = 15
    scan_interval_sec: float = 30.0
    log_level: str = "INFO"
    data_dir: str = "kalshiv2_data"


def load_config(path: str | Path | None = None) -> BotConfig:
    """Load config from YAML file, falling back to env vars and defaults."""
    cfg = BotConfig()

    # Try loading YAML
    if path is None:
        path = os.environ.get("KALSHIV2_CONFIG", "kalshiv2_config.yaml")
    config_path = Path(path)
    if config_path.exists():
        raw: dict[str, Any] = yaml.safe_load(config_path.read_text()) or {}
        _merge_dict_into_dataclass(cfg, raw)

    # Override from env vars
    cfg.kalshi.api_key = os.environ.get("KALSHI_API_KEY", cfg.kalshi.api_key)
    cfg.kalshi.private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", cfg.kalshi.private_key_path)
    cfg.futures.alpaca_api_key = os.environ.get("ALPACA_API_KEY", cfg.futures.alpaca_api_key)
    cfg.futures.alpaca_secret = os.environ.get("ALPACA_SECRET", cfg.futures.alpaca_secret)
    cfg.futures.polygon_api_key = os.environ.get("POLYGON_API_KEY", cfg.futures.polygon_api_key)
    cfg.options.tradier_token = os.environ.get("TRADIER_TOKEN", cfg.options.tradier_token)

    return cfg


def _merge_dict_into_dataclass(obj: Any, data: dict[str, Any]) -> None:
    """Recursively merge a dict into a nested dataclass."""
    for key, value in data.items():
        if not hasattr(obj, key):
            continue
        current = getattr(obj, key)
        if isinstance(value, dict) and hasattr(current, "__dataclass_fields__"):
            _merge_dict_into_dataclass(current, value)
        else:
            setattr(obj, key, value)
