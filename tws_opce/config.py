"""Načítání, validace a ukládání konfiguračního souboru aplikace."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from .models import PT_MULTIPLES

log = logging.getLogger(__name__)

# Povolené typy vstupního příkazu (nákup opce po splnění cenové podmínky na podkladu)
ENTRY_ORDER_TYPES = ("LMT_ASK", "MKT", "LMT_MID")
# Co udělat se strikem, když se u obchodu před nákupem změní PT
PT_STRIKE_MODES = ("keep", "recalculate")
# Povolené typy výstupního příkazu (jeden příkaz s podmínkami pro PT i SL)
EXIT_ORDER_TYPES = ("MKT", "LMT")
# Která úroveň se zadává jako prvotní (druhá se dopočítá z poměru SL:PT)
PRIMARY_LEVELS = ("pt", "sl")
# Povolené režimy výběru expirace
EXPIRATION_MODES = ("nearest", "fixed")
# Povolené režimy výběru strike ceny opčního kontraktu
STRIKE_MODES = ("otm_offset", "atm", "target")
# Povolené režimy zadání cíle v dialogu načtení pozic ze souboru. Tytéž
# hodnoty nesou konstanty REZIM_* v import_dialog, kde se přepínač staví
IMPORT_PT_MODES = ("pct", "usd", "premium")


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

    # Velikost účtu v USD. Hodnota 0 znamená, že se převezme z TWS (NetLiquidation).
    size: float = 5000.0
    risk_pct: float = 1.0


@dataclass
class TradingConfig:
    """Parametry obchodní logiky - příkazy, spread, kontrakty."""

    # Výchozí poměr SL vůči PT, pokud uživatel SL nezadá (1.0 = 1:1)
    sl_to_pt_ratio: float = 1.0
    # Výchozí stav voleb "na podkladu" ve formuláři:
    #   true  = PT / SL se zadává jako cena podkladu (podmíněný příkaz)
    #   false = PT / SL se zadává jako zisk / ztráta v USD na jeden kontrakt
    #           a realizuje se příkazem přímo na cenu opce (LMT, resp. STP)
    pt_on_underlying: bool = True
    sl_on_underlying: bool = True
    # Výchozí stav zaškrtávátka "SL o zaplacený spread dál" - dostupné jen
    # u SL zadaného na opci (sl_on_underlying = false)
    sl_spread_compensated: bool = False
    # Která úroveň se ve formuláři zadává jako prvotní; druhá se dopočítá
    # podle sl_to_pt_ratio: "sl" = zadává se SL a PT se dopočte (výchozí),
    # "pt" = zadává se PT a SL se dopočte. Určuje výchozí stav dvojice voleb.
    primary_level: str = "sl"
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
    # Strop prodlevy před návratem do trhu. Každé další odstranění příkazu
    # kvůli spreadu u téhož obchodu prodlevu vynásobí rearm_delay_factor,
    # nejvýš na tuto hodnotu - kolísající spread tak příkaz nezadává a neruší
    # každých pár sekund. Hodnota nejvýš rearm_delay_sec prodlužování vypíná
    rearm_delay_max_sec: float = 600.0
    # Násobek prodlevy při každém dalším odstranění kvůli spreadu
    # (při 1,5 a základu 30 s: 30, 45, 67,5, 101 … s). 1 = prodleva se nemění
    rearm_delay_factor: float = 1.5
    # Velikost runneru v procentech pozice - runner je část pozice, která se
    # po aktivaci prodává samostatným příkazem s vlastním (vzdálenějším) cílem.
    # Počet kontraktů z procenta počítá runner_kusy (z 8 ks při 25 % vyjdou
    # 2 ks). Runner lze zapnout jen u obchodu, kterému po jeho odečtení zbude
    # v hlavní části aspoň jeden kontrakt.
    runner_quantity_pct: float = 25.0
    # Chování při změně PT u obchodu, který ještě nenakoupil:
    #   keep        = ponechat původní strike, mění se jen cílová úroveň
    #   recalculate = přepočítat strike podle nového PT a příkaz přezadat
    pt_change_strike: str = "keep"
    # Průběžná aktualizace limitní ceny nákupního příkazu podle aktuálního ASK / MID
    relimit_enabled: bool = True
    # Minimální změna limitní ceny (v procentech), která vyvolá modifikaci příkazu
    relimit_min_change_pct: float = 0.5
    # Upravovat limit i příkazu, který ještě čeká na splnění cenové podmínky
    # na podkladu (stav PreSubmitted). Takový příkaz na burze neleží, jeho
    # limit se uplatní až při spuštění podmínky a dnešní ASK o ceně v tom
    # okamžiku mnoho neříká - úpravy by jen zvyšovaly OER. Při false se limit
    # upravuje až po spuštění podmínky, kdy příkaz na burze skutečně čeká
    relimit_before_trigger: bool = False
    # Nejvyšší Order Efficiency Ratio dne, který smějí nepovinné zprávy do TWS
    # (přelimitování, návrat příkazu do trhu po uvolnění spreadu, zvýšení
    # množství průběžným přepočtem) vyčerpat. IBKR očekává OER nejvýš kolem
    # 20: (příkazy + úpravy + zrušení) / (vyplněné příkazy + 1). Povinné
    # zprávy (zajištění pozice, uzavření, zrušení) se posílají vždy.
    # 0 = hlídání vypnuto
    oer_limit: float = 15.0
    # Volný denní základ zpráv: do tohoto počtu projdou nepovinné zprávy bez
    # ohledu na OER, nad ním rozhoduje oer_limit. IBKR minimální hranici
    # nezveřejňuje; podle zkušeností obchodníků poměr neřeší při stovkách
    # zpráv denně, varování přicházela až při tisících. Bez základu by pět
    # čekajících obchodů bez vyplnění mělo na celý den jen pár úprav
    oer_free_messages: int = 200
    # Pásmo necitlivosti průběžného přepočtu (import.refresh_interval) pro
    # zvýšení množství čekajícího příkazu v trhu: vyšší množství se pošle,
    # jen když by vyšlo i z rizika na obchod sníženého o tolik procent.
    # Brání přehazování množství tam a zpět (3 -> 4 -> 3 ks) při drobném
    # pohybu prémie; každá taková úprava zvyšuje OER. Snížení množství se
    # posílá vždy - chrání riziko na obchod. 0 = bez pásma
    refresh_increase_margin_pct: float = 10.0
    # Automatické uzavření obchodů krátce před koncem obchodování burzy:
    # čekající obchody se zruší, otevřené pozice se prodají trhem
    auto_close_enabled: bool = True
    # Kolik minut před zavřením burzy se obchody automaticky uzavírají
    auto_close_minutes_before: float = 15.0
    # Automatické zrušení čekajících obchodů v pevně daný čas dne. Týká se
    # jen obchodů před nákupem - už nakoupené pozice běží dál
    pending_cancel_enabled: bool = True
    # Čas zrušení čekajících obchodů ve formátu HH:MM (v časové zóně burzy);
    # okno pak trvá až do zavření burzy
    pending_cancel_time: str = "12:00"
    # Časová zóna burzy - uzavírání i rušení čekajících obchodů se časuje
    # v ní, takže posuny letního a zimního času vůči místnímu času
    # počítače nehrají roli
    exchange_timezone: str = "America/New_York"
    # Čas otevření burzy ve formátu HH:MM (v časové zóně burzy). Řídí odpočet
    # v hlavičce, rozlišení obchodních hodin pro kontrolu propásnutého vstupu
    # a okamžik přepočtu čekajících obchodů po otevření (import.refresh_after_open)
    exchange_open_time: str = "09:30"
    # Čas zavření burzy ve formátu HH:MM (v časové zóně burzy).
    # Zkrácené obchodní dny (např. před svátky) aplikace nezná.
    exchange_close_time: str = "16:00"

    def runner_kusy(self, mnozstvi: int) -> int:
        """
        Počet kontraktů runneru pro pozici o daném množství.

        Procento runner_quantity_pct se zaokrouhluje dolů, nejméně však
        na jeden kontrakt: z 8 ks při 25 % vyjdou 2 ks, ze 3 ks jeden.
        Shora se výsledek neomezuje - obchod, kterému by na hlavní část
        nic nezbylo, runner prostě nedostane (rozhoduje o tom engine).
        """
        if mnozstvi < 1:
            return 0
        return max(1, int(mnozstvi * self.runner_quantity_pct / 100))


@dataclass
class ImportConfig:
    """
    Výchozí volby zadání obchodu - dialogu "Načtení pozic ze souboru"
    a u runneru a přepočtů i formuláře zadání.

    Hodnoty rozhraní jen předvyplní - před zadáním do trhu je lze přepsat.
    U voleb, které má sekce trading, znamená prázdná hodnota (null)
    "převzít nastavení ze sekce trading"; vyplněná hodnota naopak dovolí,
    aby se hromadné zadání od jednotlivého lišilo.
    """

    # Režim zadání cíle: pct = procento dráhy k cíli na podkladu,
    # usd = zisk na opci v USD na kontrakt, premium = zisk v % zaplacené prémie
    pt_mode: str = "premium"
    # Výchozí obsah tří polí PT - přepínač režimu jen mění, které z nich je
    # vidět, proto má každé vlastní hodnotu. Prázdná (null) nechá pole prázdné
    pt_pct: float | None = None
    pt_usd: float | None = None
    pt_premium_pct: float | None = None
    # Výchozí volba runneru jako násobek původní vzdálenosti PT od vstupu.
    # Povolené jsou násobky nabízené tlačítky, 0 znamená runner nepoužít.
    # Předvyplňuje tlačítka Runner v dialogu i ve formuláři zadání obchodu
    runner_multiple: float = 1.5
    # Nejmenší velikost pozice, které se runner nastaví (včetně). Pozice
    # s menším počtem kontraktů runner nedostane (v dialogu má ve sloupci
    # Runner volbu "Bez"; ručně ji tam lze přesto přepnout). Předvyplňuje
    # pole "Runner od [ks]" v dialogu i ve formuláři zadání obchodu
    runner_min_quantity: int = 3
    # Volby sdílené s formulářem zadání; null = převzít hodnotu z trading
    max_spread_pct: float | None = None
    # Poměr PT:SL tak, jak se zadává v dialogu (RRR 2 = PT je dvakrát dál
    # než SL). Prázdná hodnota se odvodí z trading.sl_to_pt_ratio
    rrr: float | None = None
    sl_spread_compensated: bool | None = None
    # Přepočet čekajících obchodů po otevření burzy podle živých kotací:
    # PT (v % prémie), SL i množství se dopočítají znovu ze skutečné prémie
    # a čekající nákupní příkaz se upraví na místě. Volba jen předvyplní
    # přepínač v dialogu, u každé dávky ji lze vypnout
    refresh_after_open: bool = True
    # Prodleva od otevření burzy v sekundách - v prvních okamžicích jsou
    # kotace opcí nejširší, přepočet proto chvíli počká
    refresh_after_open_sec: float = 60.0
    # Průběžný přepočet čekajících obchodů za otevřené burzy: každých
    # refresh_interval_sec sekund se množství (a s ním PT v % prémie i SL)
    # dopočítá znovu z živých kotací, čekající příkaz se upraví na místě
    # a runner se srovná s volbou runneru a minimem pro něj. Volba předvyplní
    # přepínač v dialogu načtení pozic i ve formuláři zadání obchodu
    refresh_interval: bool = True
    refresh_interval_sec: float = 30.0
    # Kontrola propásnutého vstupu podle minutových svíček podkladu - jen
    # v dialogu načtení pozic ze souboru (Přepočítat, Zadat vybrané pozice,
    # Zadat po otevření trhu). Překročil-li podklad od času níže vstupní
    # úroveň (stačí knot svíčky), řádek se označí "Vstup propásnut"
    # a odškrtne, stejně jako při překonaném vstupu podle živé ceny.
    # Formulář zadání ani monitorovací smyčka tuto kontrolu nepoužívají
    entry_cross_check: bool = True
    # Od kdy (HH:MM v časové zóně burzy) se svíčky procházejí. Půlnoc
    # pokrývá overnight seanci i pre-market až do okamžiku zadání
    entry_cross_check_from: str = "00:00"


@dataclass
class ExpirationConfig:
    """Výběr expirace opčního kontraktu."""

    # nearest = nejbližší expirace splňující min_dte, fixed = konkrétní datum
    mode: str = "nearest"
    min_dte: int = 0
    # Datum ve formátu YYYYMMDD, použije se pouze při mode = fixed
    fixed_date: str = ""


@dataclass
class StrikeConfig:
    """Výběr strike ceny opčního kontraktu."""

    # Podle čeho se strike vybírá:
    #   otm_offset = odsazený od vstupní ceny na stranu mimo peníze (výchozí)
    #   atm        = nejbližší strike ke vstupní ceně
    #   target     = nejbližší strike k cílové úrovni (PT)
    mode: str = "otm_offset"
    # Kolikátý strike za vstupní cenou se vybere při mode = otm_offset.
    # Počítá se v krocích rastru řetězce, takže platí pro každý ticker:
    # 1 = první strike nad vstupem u CALL, pod vstupem u PUT.
    otm_steps: int = 1
    # Meze delty vybrané opce, mimo které náhled upozorní. Nejde o kritérium
    # výběru, jen o kontrolu - příliš nízká delta znamená opci, která se
    # z pohybu podkladu skoro nezhodnotí. Nula obě kontroly vypíná.
    delta_warn_min: float = 0.25
    delta_warn_max: float = 0.60


@dataclass
class EngineConfig:
    """Časování monitorovací smyčky."""

    poll_interval_sec: float = 1.0
    # Jak dlouho čekat na první ceny z TWS při zakládání flow
    market_data_timeout_sec: float = 6.0
    # Dorazí-li u opce nejdřív jen poslední/závěrečná cena, kolik sekund se
    # ještě počká na BID/ASK, než se model spočítá z ní
    quotes_grace_sec: float = 1.5
    # Interval kontroly opčních pozic, které aplikace neřídí (0 = vypnuto)
    unmanaged_check_sec: float = 30.0
    # Jak často se obnovuje velikost účtu z TWS, používá-li se account.size = 0
    account_refresh_sec: float = 60.0


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
    # Jak často se měří odezva TWS pro ukazatel v hlavičce. Je to dotaz do
    # TWS, ne čtení z paměti jako zbytek obnovy, proto vlastní, řidší tempo;
    # 0 měření vypne a v hlavičce zůstane jen stáří tržních dat
    latency_interval_sec: float = 5.0


@dataclass
class AppConfig:
    """
    Kořenová konfigurace aplikace.

    Riskovaná částka na obchod se záměrně nepočítá tady, ale ve
    FlowEngine.risk_amount - jedině engine zná pravidlo "account.size = 0
    znamená převzít velikost účtu z TWS". Stejný výpočet nad samotnou
    konfigurací by při tomto (doporučeném) nastavení vracel nulu.
    """

    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    account: AccountConfig = field(default_factory=AccountConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    # Sekce se v YAML jmenuje "import"; atribut nese podtržítko, protože
    # "import" je v Pythonu klíčové slovo
    import_: ImportConfig = field(default_factory=ImportConfig)
    expiration: ExpirationConfig = field(default_factory=ExpirationConfig)
    strike: StrikeConfig = field(default_factory=StrikeConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    state: StateConfig = field(default_factory=StateConfig)
    ui: UiConfig = field(default_factory=UiConfig)


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
        import_=_build(ImportConfig, raw.get("import", {}), "import"),
        expiration=_build(ExpirationConfig, raw.get("expiration", {}), "expiration"),
        strike=_build(StrikeConfig, raw.get("strike", {}), "strike"),
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

    if cfg.trading.auto_close_minutes_before < 0:
        problems.append("trading.auto_close_minutes_before nesmí být záporné")

    # Časová zóna burzy musí existovat v databázi zón
    try:
        ZoneInfo(cfg.trading.exchange_timezone)
    except Exception:
        problems.append(
            f"trading.exchange_timezone '{cfg.trading.exchange_timezone}' není platná časová zóna"
        )

    # Časy otevření a zavření burzy, času zrušení čekajících obchodů
    # i začátku kontroly svíček musí mít tvar HH:MM
    for nazev, hodnota in (
        ("trading.exchange_open_time", cfg.trading.exchange_open_time),
        ("trading.exchange_close_time", cfg.trading.exchange_close_time),
        ("trading.pending_cancel_time", cfg.trading.pending_cancel_time),
        ("import.entry_cross_check_from", cfg.import_.entry_cross_check_from),
    ):
        try:
            hodina, minuta = (int(cast) for cast in hodnota.split(":"))
            if not (0 <= hodina <= 23 and 0 <= minuta <= 59):
                raise ValueError
        except (ValueError, AttributeError):
            problems.append(f"{nazev} '{hodnota}' musí mít tvar HH:MM")
    if cfg.trading.exit_order_type not in EXIT_ORDER_TYPES:
        problems.append(
            f"trading.exit_order_type musí být jedna z {EXIT_ORDER_TYPES}, "
            f"nalezeno '{cfg.trading.exit_order_type}'"
        )
    # Nula i sto procent runner fakticky vypínají (hlavní části by nezbyl
    # žádný kontrakt), záporná hodnota nedává smysl
    if not 0 < cfg.trading.runner_quantity_pct < 100:
        problems.append(
            "trading.runner_quantity_pct musí být v intervalu (0, 100) - "
            "po odečtení runneru musí hlavní části zbýt aspoň jeden kontrakt"
        )
    # Záporná prodleva ani záporný limit OER nedávají smysl; nula limitu
    # hlídání OER vypíná
    if cfg.trading.rearm_delay_max_sec < 0:
        problems.append("trading.rearm_delay_max_sec nesmí být záporné")
    # Násobek pod 1 by prodlevu s každým odstraněním zkracoval
    if cfg.trading.rearm_delay_factor < 1:
        problems.append("trading.rearm_delay_factor musí být alespoň 1")
    if cfg.trading.oer_limit < 0:
        problems.append("trading.oer_limit nesmí být záporné (0 = hlídání vypnuto)")
    if cfg.trading.oer_free_messages < 0:
        problems.append("trading.oer_free_messages nesmí být záporné")
    # Pásmo 100 % a víc by zvýšení množství zakázalo úplně
    if not 0 <= cfg.trading.refresh_increase_margin_pct < 100:
        problems.append("trading.refresh_increase_margin_pct musí být v rozsahu 0 až 99")
    if cfg.trading.pt_change_strike not in PT_STRIKE_MODES:
        problems.append(
            f"trading.pt_change_strike musí být jedna z {PT_STRIKE_MODES}, "
            f"nalezeno '{cfg.trading.pt_change_strike}'"
        )
    # Sekce importu - výchozí obsah dialogu načtení pozic ze souboru
    if cfg.import_.pt_mode not in IMPORT_PT_MODES:
        problems.append(
            f"import.pt_mode musí být jedna z {IMPORT_PT_MODES}, "
            f"nalezeno '{cfg.import_.pt_mode}'"
        )
    # Prázdné pole PT je v pořádku (obchodník si hodnotu doplní), nula ani
    # záporné číslo ale ne - z takového cíle by nevznikl obchod
    for nazev in ("pt_pct", "pt_usd", "pt_premium_pct", "max_spread_pct", "rrr"):
        hodnota = getattr(cfg.import_, nazev)
        if hodnota is not None and hodnota <= 0:
            problems.append(
                f"import.{nazev} musí být kladné číslo, nebo prázdný (null)"
            )
    if cfg.import_.runner_multiple and cfg.import_.runner_multiple not in PT_MULTIPLES:
        problems.append(
            f"import.runner_multiple musí být 0 (runner nepoužít), nebo jeden "
            f"z násobků {PT_MULTIPLES}, nalezeno {cfg.import_.runner_multiple}"
        )
    if cfg.import_.runner_min_quantity < 1:
        problems.append("import.runner_min_quantity musí být alespoň 1")
    # Nula je platná (přepočet hned po otevření), záporná prodleva nedává smysl
    if cfg.import_.refresh_after_open_sec < 0:
        problems.append("import.refresh_after_open_sec nesmí být záporné")
    # Průběžný přepočet kratší než sekunda by běžel při každém průchodu
    # smyčkou a upravoval příkaz v trhu naprázdno
    if cfg.import_.refresh_interval_sec < 1:
        problems.append("import.refresh_interval_sec musí být alespoň 1 s")
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

    if cfg.strike.mode not in STRIKE_MODES:
        problems.append(
            f"strike.mode musí být jedna z {STRIKE_MODES}, nalezeno '{cfg.strike.mode}'"
        )
    if cfg.strike.otm_steps < 0:
        problems.append("strike.otm_steps nesmí být záporné")
    # Meze delty se zadávají v absolutní hodnotě, u PUT se porovnává |delta|
    for nazev, hodnota in (
        ("delta_warn_min", cfg.strike.delta_warn_min),
        ("delta_warn_max", cfg.strike.delta_warn_max),
    ):
        if not 0.0 <= hodnota <= 1.0:
            problems.append(f"strike.{nazev} musí ležet mezi 0 a 1, nalezeno {hodnota:g}")
    if 0.0 < cfg.strike.delta_warn_max < cfg.strike.delta_warn_min:
        problems.append("strike.delta_warn_max nesmí být menší než strike.delta_warn_min")

    if cfg.account.size < 0:
        problems.append("account.size nesmí být záporná (0 = převzít z TWS)")
    if not 0 < cfg.account.risk_pct <= 100:
        problems.append("account.risk_pct musí být v intervalu (0, 100]")
    if cfg.trading.sl_to_pt_ratio <= 0:
        problems.append("trading.sl_to_pt_ratio musí být kladné číslo")
    # Přepínače režimu PT/SL musí být skutečné pravdivostní hodnoty - YAML
    # řetězec "false" by se jinak vyhodnotil jako pravda
    for nazev in ("pt_on_underlying", "sl_on_underlying", "sl_spread_compensated"):
        if not isinstance(getattr(cfg.trading, nazev), bool):
            problems.append(f"trading.{nazev} musí být true nebo false")
    if cfg.trading.primary_level not in PRIMARY_LEVELS:
        problems.append(
            f"trading.primary_level musí být jedna z {PRIMARY_LEVELS}, "
            f"nalezeno '{cfg.trading.primary_level}'"
        )
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
    if cfg.engine.account_refresh_sec <= 0:
        problems.append("engine.account_refresh_sec musí být kladné číslo")
    if cfg.engine.unmanaged_check_sec < 0:
        problems.append("engine.unmanaged_check_sec nesmí být záporné")
    if cfg.engine.poll_interval_sec <= 0:
        problems.append("engine.poll_interval_sec musí být kladné číslo")
    if cfg.engine.quotes_grace_sec < 0:
        problems.append("engine.quotes_grace_sec nesmí být záporné")
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
    # Sekce importu se v souboru jmenuje "import" - atribut ho nést nemůže,
    # je to klíčové slovo Pythonu. Přejmenování zachovává pořadí sekcí
    data = {
        ("import" if nazev == "import_" else nazev): hodnota
        for nazev, hodnota in _as_dict(cfg).items()
    }
    with cfg_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)
