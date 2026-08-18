"""Načítání, validace a ukládání konfiguračního souboru aplikace."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# Povolené typy vstupního příkazu (nákup opce po splnění cenové podmínky na podkladu)
ENTRY_ORDER_TYPES = ("LMT_ASK", "MKT", "LMT_MID")
# Povolené typy výstupního příkazu (jeden příkaz s podmínkami pro PT i SL)
EXIT_ORDER_TYPES = ("MKT", "LMT")
# Povolené režimy výběru expirace
EXPIRATION_MODES = ("nearest", "fixed")


@dataclass
class ConnectionConfig:
    """Parametry spojení na TWS / IB Gateway."""

    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 17
    readonly: bool = False
    # Prázdný řetězec = použije se první účet nalezený na spojení
    account: str = ""
    # 1 = live, 2 = frozen, 3 = delayed, 4 = delayed frozen
    market_data_type: int = 1
    connect_timeout: float = 8.0
    auto_reconnect: bool = True
    reconnect_delay_sec: float = 5.0


@dataclass
class AccountConfig:
    """Velikost účtu a risk management."""

    size: float = 5000.0
    risk_pct: float = 1.0
    # Pokud je True, velikost účtu se načte z TWS (NetLiquidation) místo hodnoty výše
    use_live_account_size: bool = False


@dataclass
class TradingConfig:
    """Parametry obchodní logiky - příkazy, spread, kontrakty."""

    # Výchozí poměr SL vůči PT, pokud uživatel SL nezadá (1.0 = 1:1)
    sl_to_pt_ratio: float = 1.0
    max_spread_pct: float = 7.0
    entry_order_type: str = "LMT_ASK"
    # Tolerance nad ASK v procentech pro typ příkazu LMT_ASK
    ask_tolerance_pct: float = 2.0
    exit_order_type: str = "MKT"
    # Tolerance pod BID v procentech pro výstupní LMT příkaz
    bid_tolerance_pct: float = 2.0
    # Náhradní delta pro výpočet množství, když deltu nelze získat ani dopočítat
    default_delta: float = 0.40
    # Bezriziková sazba v procentech pro dopočet delty z ceny opce (Black-Scholes).
    # Uplatní se, když TWS nepošle model greeks.
    risk_free_rate_pct: float = 4.0
    min_quantity: int = 1
    max_quantity: int = 100
    exchange: str = "SMART"
    currency: str = "USD"
    tif: str = "GTC"
    outside_rth: bool = False
    # Metoda triggeru pro PriceCondition (0 = výchozí nastavení TWS)
    trigger_method: int = 0
    # Zrušení nevyplněného nákupního příkazu při překročení maximálního spreadu
    cancel_on_spread_breach: bool = True
    # Opětovné zadání příkazu, jakmile spread klesne zpět pod limit
    rearm_on_spread_ok: bool = True
    # O kolik procent pod limitem musí spread být, aby se příkaz vrátil do trhu.
    # Brání opakovanému zadávání a rušení, když spread kolísá kolem limitu.
    rearm_spread_margin_pct: float = 10.0
    # Nejkratší prodleva mezi odstraněním příkazu z trhu a jeho novým zadáním
    rearm_delay_sec: float = 5.0
    # Průběžná aktualizace limitní ceny nákupního příkazu podle aktuálního ASK / MID
    relimit_enabled: bool = True
    # Minimální změna limitní ceny (v procentech), která vyvolá modifikaci příkazu
    relimit_min_change_pct: float = 0.5


@dataclass
class ExpirationConfig:
    """Výběr expirace opčního kontraktu."""

    # nearest = nejbližší expirace splňující min_dte, fixed = konkrétní datum
    mode: str = "nearest"
    min_dte: int = 0
    # Datum ve formátu YYYYMMDD, použije se pouze při mode = fixed
    fixed_date: str = ""


@dataclass
class EngineConfig:
    """Časování monitorovací smyčky."""

    poll_interval_sec: float = 1.0
    # Jak dlouho čekat na první ceny z TWS při zakládání flow
    market_data_timeout_sec: float = 6.0
    # Interval kontroly opčních pozic, které aplikace neřídí (0 = vypnuto)
    unmanaged_check_sec: float = 30.0


@dataclass
class StateConfig:
    """Ukládání stavu obchodů na disk."""

    # false = stav se neukládá a po restartu aplikace o obchodech neví
    enabled: bool = True
    # Soubor, do kterého se stav zapisuje
    file: str = "state.json"


@dataclass
class UiConfig:
    """Parametry webového rozhraní."""

    host: str = "127.0.0.1"
    port: int = 8080
    refresh_interval_sec: float = 1.0
    dark: bool = False
    log_lines: int = 300


@dataclass
class AppConfig:
    """Kořenová konfigurace aplikace."""

    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    account: AccountConfig = field(default_factory=AccountConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    expiration: ExpirationConfig = field(default_factory=ExpirationConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    state: StateConfig = field(default_factory=StateConfig)
    ui: UiConfig = field(default_factory=UiConfig)

    @property
    def risk_amount(self) -> float:
        """Částka v USD, kterou lze na jednom obchodu riskovat."""
        return self.account.size * self.account.risk_pct / 100.0


def _build(cls: type, data: Any, path: str = "") -> Any:
    """
    Sestaví dataclass ze slovníku načteného z YAML.
    Neznámý klíč pouze zaloguje varování, chybějící klíč ponechá výchozí hodnotu.
    """
    if not isinstance(data, dict):
        raise ValueError(
            f"Sekce '{path or 'root'}' musí být slovník, nalezeno: {type(data).__name__}"
        )

    kwargs: dict[str, Any] = {}
    known = {f.name: f for f in fields(cls)}

    for key, value in data.items():
        if key not in known:
            log.warning("Neznámý konfigurační klíč '%s%s' - ignoruji.", f"{path}." if path else "", key)
            continue
        if value is not None:
            kwargs[key] = value

    return cls(**kwargs)


# Komentovaná šablona konfigurace dodávaná s aplikací
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "config.example.yaml"


def load_config(path: str | Path) -> AppConfig:
    """
    Načte konfiguraci z YAML souboru. Pokud soubor neexistuje, vytvoří jej
    a vrátí výchozí konfiguraci.
    """
    cfg_path = Path(path)

    if not cfg_path.exists():
        log.info("Konfigurační soubor %s neexistuje - zakládám výchozí.", cfg_path)
        _create_default(cfg_path)

    with cfg_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    # Vnořené sekce se skládají ručně, aby šlo hlásit neznámé klíče po sekcích
    cfg = AppConfig(
        connection=_build(ConnectionConfig, raw.get("connection", {}), "connection"),
        account=_build(AccountConfig, raw.get("account", {}), "account"),
        trading=_build(TradingConfig, raw.get("trading", {}), "trading"),
        expiration=_build(ExpirationConfig, raw.get("expiration", {}), "expiration"),
        engine=_build(EngineConfig, raw.get("engine", {}), "engine"),
        state=_build(StateConfig, raw.get("state", {}), "state"),
        ui=_build(UiConfig, raw.get("ui", {}), "ui"),
    )
    validate_config(cfg)
    return cfg


def _create_default(cfg_path: Path) -> None:
    """
    Založí výchozí konfigurační soubor.
    Přednostně se zkopíruje komentovaná šablona config.example.yaml,
    aby si uživatel v souboru našel popis všech voleb; není-li šablona
    k dispozici, vygeneruje se soubor z výchozích hodnot bez komentářů.
    """
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if TEMPLATE_PATH.exists():
        shutil.copyfile(TEMPLATE_PATH, cfg_path)
    else:
        save_config(AppConfig(), cfg_path)


def validate_config(cfg: AppConfig) -> None:
    """Zkontroluje hodnoty konfigurace a vyhodí ValueError s popisem chyby."""
    problems: list[str] = []

    if cfg.trading.entry_order_type not in ENTRY_ORDER_TYPES:
        problems.append(
            f"trading.entry_order_type musí být jedna z {ENTRY_ORDER_TYPES}, "
            f"nalezeno '{cfg.trading.entry_order_type}'"
        )
    if cfg.trading.exit_order_type not in EXIT_ORDER_TYPES:
        problems.append(
            f"trading.exit_order_type musí být jedna z {EXIT_ORDER_TYPES}, "
            f"nalezeno '{cfg.trading.exit_order_type}'"
        )
    if cfg.expiration.mode not in EXPIRATION_MODES:
        problems.append(
            f"expiration.mode musí být jedna z {EXPIRATION_MODES}, nalezeno '{cfg.expiration.mode}'"
        )
    # Při pevné expiraci musí být zadáno datum ve správném formátu
    if cfg.expiration.mode == "fixed":
        d = cfg.expiration.fixed_date
        if not (len(d) == 8 and d.isdigit()):
            problems.append(
                "expiration.fixed_date musí být ve formátu YYYYMMDD při expiration.mode = fixed"
            )

    if cfg.account.size <= 0:
        problems.append("account.size musí být kladné číslo")
    if not 0 < cfg.account.risk_pct <= 100:
        problems.append("account.risk_pct musí být v intervalu (0, 100]")
    if cfg.trading.sl_to_pt_ratio <= 0:
        problems.append("trading.sl_to_pt_ratio musí být kladné číslo")
    if cfg.trading.max_spread_pct <= 0:
        problems.append("trading.max_spread_pct musí být kladné číslo")
    if cfg.trading.risk_free_rate_pct < 0:
        problems.append("trading.risk_free_rate_pct nesmí být záporné")
    if not 0 <= cfg.trading.rearm_spread_margin_pct < 100:
        problems.append("trading.rearm_spread_margin_pct musí být v intervalu [0, 100)")
    if cfg.trading.rearm_delay_sec < 0:
        problems.append("trading.rearm_delay_sec nesmí být záporné")
    if not 0 < abs(cfg.trading.default_delta) <= 1:
        problems.append("trading.default_delta musí být v intervalu (0, 1]")
    if cfg.trading.min_quantity < 1:
        problems.append("trading.min_quantity musí být alespoň 1")
    if cfg.trading.max_quantity < cfg.trading.min_quantity:
        problems.append("trading.max_quantity nesmí být menší než trading.min_quantity")
    if cfg.expiration.min_dte < 0:
        problems.append("expiration.min_dte nesmí být záporné")
    if cfg.connection.market_data_type not in (1, 2, 3, 4):
        problems.append("connection.market_data_type musí být 1, 2, 3 nebo 4")
    if cfg.engine.unmanaged_check_sec < 0:
        problems.append("engine.unmanaged_check_sec nesmí být záporné")
    if cfg.engine.poll_interval_sec <= 0:
        problems.append("engine.poll_interval_sec musí být kladné číslo")
    if cfg.state.enabled and not cfg.state.file:
        problems.append("state.file musí být vyplněn, pokud je ukládání stavu zapnuté")

    if problems:
        raise ValueError("Chybná konfigurace:\n - " + "\n - ".join(problems))


def _as_dict(obj: Any) -> Any:
    """Převede dataclass na slovník vhodný pro zápis do YAML."""
    if is_dataclass(obj):
        return {f.name: _as_dict(getattr(obj, f.name)) for f in fields(obj)}
    return obj


def save_config(cfg: AppConfig, path: str | Path) -> None:
    """Uloží konfiguraci do YAML souboru."""
    cfg_path = Path(path)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with cfg_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(_as_dict(cfg), fh, allow_unicode=True, sort_keys=False, default_flow_style=False)
