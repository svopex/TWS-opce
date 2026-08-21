"""
Obchodní logika aplikace - příprava zadání, zadávání příkazů do TWS
a monitorovací smyčka nad všemi běžícími flow.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import calc, store
from .config import AppConfig
from .ib_service import IBService, PositionInfo, order_ref, parse_order_ref, valid_price
from .models import Flow, FlowRequest, FlowState

log = logging.getLogger(__name__)

# Stavy příkazu v TWS, které znamenají, že příkaz již není v trhu
DEAD_ORDER_STATES = ("Cancelled", "ApiCancelled", "Inactive")

# Stavy, ve kterých lze příkaz v TWS ještě upravit. Příkaz čekající na
# potvrzení zrušení ("PendingCancel") mezi ně nepatří - jeho úprava končí
# hlášením TWS "Order has been cancelled already, too late to replace".
MODIFIABLE_ORDER_STATES = ("PreSubmitted", "Submitted")

# Kolik strike cen poblíž cíle se nejvýše zkusí ověřit v TWS, než se to vzdá
MAX_STRIKE_ATTEMPTS = 8

# Hlídání vyžádaného tržního prodeje: po jaké době se nevyplněný příkaz
# zruší a zadá znovu a kolik pokusů se nejvýše provede
MARKET_SELL_RETRY_SEC = 30.0
MARKET_SELL_MAX_ATTEMPTS = 5


@dataclass
class _ReferenceOption:
    """
    Referenční opce se strike u vstupní ceny - podklad pro převody mezi
    ziskem/ztrátou v USD na opci a úrovní podkladu při přípravě zadání.
    """

    strike: float
    contract: Any
    # Podrobnosti kontraktu z TWS (minimální tik) - je-li referenční opce
    # zároveň tou vybranou, ušetří se opakovaný dotaz do TWS
    details: Any
    price: float | None
    # Odkud cena pochází (BID/ASK, ASK, BID, last, close) - ukazuje se v náhledu
    price_source: str
    delta: float | None


@dataclass
class Preview:
    """
    Výsledek přípravy zadání - podklady pro předvyplnění formuláře.
    Vzniká ještě před odesláním jakéhokoliv příkazu do trhu.
    """

    symbol: str
    current_price: float | None = None
    right: str = "C"
    expiration: str = ""
    strike: float = 0.0
    # Výsledné úrovně - zadaná, nebo dopočtená podle poměru SL:PT z konfigurace
    profit_target: float = 0.0
    stop_loss: float = 0.0
    # Režim zadání PT a SL (cena podkladu, nebo USD na kontrakt)
    pt_on_underlying: bool = True
    sl_on_underlying: bool = True
    # Úroveň podkladu, ke které se vybíral strike při PT zadaném ziskem na
    # opci, a z čeho byla odvozena (cena opce / delta / vstupní cena)
    target_level: float | None = None
    target_level_source: str = ""
    delta: float | None = None
    # True, pokud delta nepřišla z TWS a byla dopočítána z ceny opce
    delta_estimated: bool = False
    option_bid: float | None = None
    option_ask: float | None = None
    # Cena vybrané opce pro model (střed kotace, jinak last/close) a její zdroj
    option_price: float | None = None
    option_price_source: str = ""
    spread_pct: float | None = None
    quantity: int = 1
    risk_amount: float = 0.0
    account_size: float = 0.0
    warnings: list[str] = field(default_factory=list)

    # Runtime kontrakty pro následné založení flow
    underlying: Any = field(default=None, repr=False)
    option: Any = field(default=None, repr=False)
    min_tick: float = 0.01

    @property
    def right_label(self) -> str:
        """CALL / PUT pro zobrazení ve formuláři."""
        return "CALL" if self.right == "C" else "PUT"


class FlowEngine:
    """
    Správa všech obchodních flow.
    Drží jejich stav, zadává příkazy a v periodické smyčce hlídá spread,
    vyplnění nákupu a následné zadání výstupního příkazu.
    """

    def __init__(self, cfg: AppConfig, ib: IBService) -> None:
        self.cfg = cfg
        self.ib = ib
        self.flows: dict[str, Flow] = {}
        self.events: deque[tuple[datetime, str]] = deque(maxlen=cfg.ui.log_lines)
        self._ids = itertools.count(1)
        self._preview: Preview | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # Obnova a monitorovací smyčka nesmí běžet současně - obnova čeká
        # na odpovědi z TWS a smyčka by mezitím pracovala s neplatnými příkazy
        self._restore_lock = asyncio.Lock()
        self.on_change: Callable[[], None] | None = None
        # Řídí automatické navazování spojení ve smyčce; ruční odpojení jej vypíná
        self.auto_connect: bool = True
        # Zjištěná velikost účtu z TWS (používá se při account.use_live_account_size)
        self._live_account_size: float | None = None
        # Uložený stav se z disku čte jen jednou, při prvním spuštění
        self._restored: bool = False
        # Po každém (znovu)připojení je potřeba obchody spárovat s příkazy v TWS
        self._synced: bool = False
        # Opční pozice na účtu, které aplikace neřídí
        self.unmanaged: dict[int, PositionInfo] = {}
        self._unmanaged_checked: float = 0.0
        self._account_checked: float = 0.0
        # Čas posledního dokončeného průchodu smyčkou - podle něj se pozná,
        # že monitoring opravdu běží a nikde neuvázl
        self._last_tick: float = 0.0
        # Části pozice, u kterých už bylo hlášeno, že z dvojice prodejních
        # příkazů zmizel jeden - varování se nemá opakovat každý průchod
        self._lost_leg_warned: set[str] = set()

    # ------------------------------------------------------------------
    # Pomocné
    # ------------------------------------------------------------------

    def log_event(self, message: str) -> None:
        """Zaznamená událost do provozního logu zobrazovaného v UI."""
        self.events.appendleft((datetime.now(), message))
        log.info(message)
        self._notify()

    def _persist(self) -> None:
        """
        Uloží stav obchodů na disk, aby přežil restart i pád aplikace.

        Před dokončením obnovy se nezapisuje. Jinak by první událost po startu
        (například hláška o navázání spojení) přepsala uložený stav prázdným
        seznamem dřív, než se stihne načíst.
        """
        if not self.cfg.state.enabled or not self._restored:
            return
        store.save(list(self.flows.values()), self.cfg.state.file)

    def _notify(self) -> None:
        """Informuje UI o změně dat a zároveň uloží stav obchodů."""
        self._persist()
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                log.exception("Chyba při notifikaci UI.")

    @property
    def account_size(self) -> float:
        """
        Velikost účtu pro výpočet rizika.
        Kladná hodnota v konfiguraci má přednost; nula znamená převzetí z TWS.
        """
        if self.cfg.account.size > 0:
            return self.cfg.account.size
        return self._live_account_size or 0.0

    @property
    def risk_amount(self) -> float:
        """Částka v USD riskovaná na jednom obchodu."""
        return self.account_size * self.cfg.account.risk_pct / 100.0

    @property
    def is_monitoring(self) -> bool:
        """
        True, pokud aplikace obchody skutečně hlídá.

        Nestačí, že je aplikace spuštěná: smyčka musí běžet, spojení s TWS
        být navázané a poslední průchod proběhnout nedávno. Zasekne-li se
        smyčka nebo spadne spojení, hlídání fakticky neprobíhá.
        """
        if self._task is None or self._task.done():
            return False
        if not self.ib.connected:
            return False
        if not self._last_tick:
            return False

        # Tolerance několika period; delší prodleva znamená, že smyčka vázne
        limit = max(3 * self.cfg.engine.poll_interval_sec, 5.0)
        return (time.monotonic() - self._last_tick) < limit

    def active_flows_for(self, symbol: str) -> list[Flow]:
        """Aktivní flow daného tickeru - nejvýše jedno pro každý směr (CALL a PUT)."""
        symbol = symbol.upper().strip()
        return [
            flow
            for flow in self.flows.values()
            if flow.symbol == symbol and flow.state.is_active
        ]

    def active_flow_for(self, symbol: str, right: str | None = None) -> Flow | None:
        """
        Najde aktivní flow tickeru; s right jen pro daný směr obchodu.
        Na jednom tickeru smí běžet současně jeden long (CALL) a jeden short (PUT).
        """
        for flow in self.active_flows_for(symbol):
            if right is None or flow.right == right:
                return flow
        return None

    def sorted_flows(self) -> list[Flow]:
        """
        Flow seřazená pro zobrazení v tabulce: běžící obchody abecedně podle
        tickeru, zrušené a dokončené až za nimi (rovněž abecedně). Stejný
        ticker se řadí od nejnovějšího obchodu.
        """
        return sorted(
            self.flows.values(),
            key=lambda f: (not f.state.is_active, f.symbol, -f.created_at.timestamp()),
        )

    # ------------------------------------------------------------------
    # Příprava zadání
    # ------------------------------------------------------------------

    async def _qualify_nearest_option(
        self,
        symbol: str,
        expiration: str,
        strikes: list[float],
        target: float,
        right: str,
        trading_class: str = "",
    ) -> tuple[float, Any, Any]:
        """
        Ověří v TWS opční kontrakt se strike nejblíže cílové ceně.

        Opční řetězec vrací strike ceny pro všechny expirace dohromady,
        takže nejbližší strike nemusí být pro zvolenou expiraci vůbec
        obchodovatelný (např. půlbodové strike jen u týdenních expirací).
        Proto se strike zkoušejí v pořadí podle vzdálenosti od cíle,
        dokud se některý neověří. Vrací trojici (strike, kontrakt, detaily).
        """
        kandidati = sorted(strikes, key=lambda s: (abs(s - target), s))[:MAX_STRIKE_ATTEMPTS]
        if not kandidati:
            raise ValueError(f"Pro ticker {symbol} nejsou dostupné strike ceny.")

        posledni_chyba: Exception | None = None
        for strike in kandidati:
            try:
                option, details = await self.ib.qualify_option(
                    symbol, expiration, strike, right, trading_class
                )
                return strike, option, details
            except ValueError as exc:
                # Kontrakt pro tuto expiraci neexistuje - zkusí se další strike
                posledni_chyba = exc

        raise ValueError(
            f"Pro ticker {symbol} {expiration} se poblíž ceny {target:g} nepodařilo "
            f"najít obchodovatelný strike. Poslední chyba: {posledni_chyba}"
        )

    async def prepare(
        self,
        symbol: str,
        entry_price: float | None = None,
        profit_target: float | None = None,
        stop_loss: float | None = None,
        pt_on_underlying: bool = True,
        sl_on_underlying: bool = True,
    ) -> Preview:
        """
        Připraví zadání obchodu: načte cenu podkladu, určí typ opce, expiraci,
        strike podle PT, dopočítá chybějící úroveň a doporučené množství
        kontraktů. Nezadává žádný příkaz do trhu.

        PT a SL jsou buď ceny podkladu, nebo - při vypnutém přepínači
        "na podkladu" - zisk, resp. ztráta v USD na jeden kontrakt. Stačí
        zadat jednu z úrovní: chybějící se dopočítá z poměru SL:PT
        v konfiguraci (SL z PT, nebo PT ze SL).
        """
        if not self.ib.connected:
            raise RuntimeError("Není navázáno spojení s TWS.")

        symbol = symbol.upper().strip()
        if not symbol:
            raise ValueError("Zadejte ticker.")

        preview = Preview(
            symbol=symbol,
            account_size=self.account_size,
            risk_amount=self.risk_amount,
            pt_on_underlying=pt_on_underlying,
            sl_on_underlying=sl_on_underlying,
        )

        # Odběry tržních dat zakládá příprava sama; nedoběhne-li (chyba,
        # nebo zrušení kvůli novějšímu zadání), musí je zase uvolnit -
        # jinak by kontrakty zůstaly odebírané až do restartu
        referencni: _ReferenceOption | None = None
        try:
            # Podklad a jeho aktuální cena
            underlying = await self.ib.qualify_stock(symbol)
            preview.underlying = underlying
            self.ib.subscribe(underlying)
            await self.ib.wait_for_quotes(underlying, self.cfg.engine.market_data_timeout_sec)
            preview.current_price = self.ib.underlying_price(underlying)

            if preview.current_price is None:
                preview.warnings.append(
                    "Z TWS zatím nedorazila cena podkladu - zkontrolujte odběr tržních dat."
                )

            # Bez známé velikosti účtu nelze spočítat riskovanou částku ani množství
            if self.account_size <= 0:
                preview.warnings.append(
                    "Velikost účtu se přebírá z TWS (account.size = 0), ale zatím nedorazila - "
                    "množství proto nelze doporučit."
                )

            # Bez vstupní ceny a aspoň jedné úrovně nelze určit kontrakt,
            # vrací se jen cena podkladu
            if entry_price is None or (profit_target is None and stop_loss is None):
                self._replace_preview(preview)
                return preview

            reference = preview.current_price if preview.current_price is not None else entry_price
            preview.right = calc.determine_right(reference, entry_price)

            # Výběr expirace
            chain = await self.ib.option_chain(underlying)
            expiration = calc.select_expiration(
                list(chain.expirations),
                self.cfg.expiration.mode,
                self.cfg.expiration.min_dte,
                self.cfg.expiration.fixed_date,
            )
            if expiration is None:
                raise ValueError(
                    f"Pro ticker {symbol} nebyla nalezena vhodná expirace "
                    f"(režim '{self.cfg.expiration.mode}')."
                )
            preview.expiration = expiration

            # Referenční opce (strike u vstupu) slouží modelu z ceny opce - je
            # potřeba, kdykoliv se převádí mezi USD na opci a úrovní podkladu:
            # pro strike při PT na opci a pro dopočet PT ze SL ve smíšeném režimu
            if not pt_on_underlying or (profit_target is None and not sl_on_underlying):
                referencni = await self._reference_option(preview, chain, entry_price)

            # Chybí-li PT, dopočítá se ze SL podle poměru z konfigurace
            if profit_target is None:
                profit_target = self._default_profit_target(
                    preview, entry_price, stop_loss, referencni
                )
            preview.profit_target = profit_target

            # Strike se vybírá k cílové úrovni podkladu: při PT na podkladu přímo
            # k PT, při PT ziskem na opci k úrovni odvozené z ceny opce
            if pt_on_underlying:
                cil_strike = profit_target
            else:
                cil_strike = self._target_level_for_option_pt(
                    preview, entry_price, profit_target, referencni
                )

            nejblizsi = calc.nearest_strike(sorted(chain.strikes), cil_strike)
            if referencni is not None and nejblizsi == referencni.strike:
                # Cílový strike je tentýž jako referenční - kontrakt je už
                # ověřený i odebíraný, další dotaz do TWS by byl zbytečný
                strike, option, details = (
                    referencni.strike,
                    referencni.contract,
                    referencni.details,
                )
            else:
                strike, option, details = await self._qualify_nearest_option(
                    symbol, expiration, list(chain.strikes), cil_strike, preview.right, chain.tradingClass
                )
            preview.strike = strike
            preview.option = option
            preview.min_tick = details.minTick or 0.01

            # Náhradní strike se hlásí, aby bylo jasné, proč kontrakt neodpovídá cíli
            if nejblizsi is not None and strike != nejblizsi:
                preview.warnings.append(
                    f"Strike {nejblizsi:g} není pro expiraci {expiration} v TWS dostupný, "
                    f"použit nejbližší obchodovatelný {strike:g}."
                )

            # Tržní data opce kvůli deltě a spreadu
            self.ib.subscribe(option)
            await self.ib.wait_for_quotes(
                option, self.cfg.engine.market_data_timeout_sec, self.cfg.engine.quotes_grace_sec
            )
            bid, ask, delta = self.ib.option_quotes(option)
            preview.option_bid = bid
            preview.option_ask = ask
            preview.option_price, preview.option_price_source = self.ib.option_price(option)
            preview.spread_pct = calc.spread_pct(bid, ask)
            preview.delta = delta

            # TWS model greeks u opcí neposílá spolehlivě, proto se delta v takovém
            # případě dopočítá z tržní ceny opce; teprve pak se sáhne po náhradní hodnotě
            if delta is None:
                delta = self._estimate_delta(preview)
                if delta is not None:
                    preview.delta = delta
                    preview.delta_estimated = True

            if delta is None:
                preview.warnings.append(
                    f"Deltu opce se nepodařilo získat ani dopočítat - množství je spočítáno "
                    f"s náhradní hodnotou {self.cfg.trading.default_delta:g}."
                )
            used_delta = delta if delta is not None else self.cfg.trading.default_delta

            # SL buď zadaný uživatelem, nebo dopočtený podle poměru z konfigurace;
            # při smíšeném režimu PT a SL se převádí přes cenu opce, proto až teď,
            # kdy jsou k dispozici kotace vybrané opce
            preview.stop_loss = (
                stop_loss
                if stop_loss is not None
                else self._default_stop_loss(preview, entry_price, profit_target, used_delta)
            )

            # Množství z riskované částky a ztráty na kontrakt: při SL na podkladu
            # se ztráta odhaduje přes deltu, při SL na opci je zadaná přímo v USD
            if sl_on_underlying:
                preview.quantity = calc.suggest_quantity(
                    self.risk_amount,
                    entry_price,
                    preview.stop_loss,
                    used_delta,
                    self.cfg.trading.min_quantity,
                    self.cfg.trading.max_quantity,
                )
            else:
                preview.quantity = calc.suggest_quantity_for_loss(
                    self.risk_amount,
                    self._capped_option_loss(preview, entry_price, preview.stop_loss),
                    self.cfg.trading.min_quantity,
                    self.cfg.trading.max_quantity,
                )

            # Závěrečná cena je jediná dostupná mimo obchodní hodiny; pohnul-li se
            # mezitím podklad (typicky pre-market gap), vyjde z ní nesmyslná
            # implikovaná volatilita a s ní i dopočítané úrovně a množství
            if preview.option_price_source == "close":
                preview.warnings.append(
                    "Opce nemá aktuální kotace, model počítá ze závěrečné ceny - "
                    "dopočítané úrovně i doporučené množství mohou být nepřesné."
                )

            if preview.spread_pct is not None and preview.spread_pct > self.cfg.trading.max_spread_pct:
                preview.warnings.append(
                    f"Aktuální spread {preview.spread_pct:.2f} % překračuje limit "
                    f"{self.cfg.trading.max_spread_pct:g} %."
                )

            self._replace_preview(preview)
            return preview
        finally:
            # Odběr referenční opce drží jen příprava - uvolňuje se až tady,
            # po přihlášení vybrané opce, protože to bývá tentýž kontrakt
            if referencni is not None:
                self.ib.unsubscribe(referencni.contract)
            # Náhled, který se nestal aktuálním, po sobě uklidí sám
            if self._preview is not preview:
                self.ib.unsubscribe(preview.underlying)
                self.ib.unsubscribe(preview.option)

    def _model_delta(
        self,
        cena_opce: float | None,
        podklad: float | None,
        strike: float,
        expiration: str,
        right: str,
        delta: float | None,
        se_znamenkem: bool = False,
    ) -> float:
        """
        Delta pro záložní lineární odhad, když model z ceny opce selže.

        Přednost má delta z TWS, pak dopočet z aktuální ceny opce a teprve
        nakonec náhradní hodnota z konfigurace. Se se_znamenkem se náhradní
        hodnota vrací se znaménkem podle typu opce (u PUT záporná), jinak
        kladná - volající si ji stejně bere v absolutní hodnotě.
        """
        if delta is None and cena_opce and podklad:
            delta = calc.estimate_delta(
                cena_opce,
                podklad,
                strike,
                expiration,
                self.cfg.trading.risk_free_rate_pct,
                right,
            )
        if delta is None:
            delta = self.cfg.trading.default_delta
            if se_znamenkem and right == "P":
                delta = -delta
        return delta

    def _level_from_option_profit(
        self,
        cena_opce: float | None,
        podklad: float | None,
        entry_price: float,
        zisk_usd: float,
        strike: float,
        expiration: str,
        right: str,
        delta: float | None,
        zdroj_ceny: str = "",
    ) -> tuple[float, str]:
        """
        Úroveň podkladu, na které opce vydělá zadaný zisk v USD na kontrakt.

        Z aktuální ceny opce se odvodí implikovaná volatilita, spočítá se
        cena opce v okamžiku vstupu (podklad na vstupní úrovni), přičte se
        požadovaný zisk a zpětně se najde úroveň podkladu, kde opce této
        ceny dosáhne. Bez jakékoliv ceny opce se použije lineární odhad přes
        deltu, bez delty zůstává vstupní cena. Vrací dvojici (úroveň, zdroj
        odhadu); zdroj_ceny (BID/ASK, last, close…) se do popisu přidává,
        aby bylo vidět, na jak čerstvé ceně odhad stojí.
        """
        sazba = self.cfg.trading.risk_free_rate_pct
        smer = 1.0 if right == "C" else -1.0
        posun_ceny = zisk_usd / calc.OPTION_MULTIPLIER

        if cena_opce and podklad:
            cena_na_vstupu = calc.project_option_price(
                cena_opce, podklad, entry_price, strike, expiration, sazba, right
            )
            if cena_na_vstupu:
                uroven = calc.project_underlying_level(
                    cena_opce, podklad, cena_na_vstupu + posun_ceny, strike, expiration, sazba, right
                )
                if uroven is not None:
                    popis = f"z ceny opce ({zdroj_ceny})" if zdroj_ceny else "z ceny opce"
                    return uroven, popis

        # Záložní lineární odhad: pohyb podkladu = posun ceny opce / |delta|
        delta = self._model_delta(cena_opce, podklad, strike, expiration, right, delta)
        if abs(delta) > 0:
            return entry_price + smer * posun_ceny / abs(delta), "z delty"
        return entry_price, "vstupní cena"

    def _profit_from_underlying_level(
        self,
        cena_opce: float | None,
        podklad: float | None,
        entry_price: float,
        uroven: float,
        strike: float,
        expiration: str,
        right: str,
        delta: float | None,
    ) -> float:
        """
        Výsledek opce v USD na kontrakt, když podklad dojde ze vstupu na úroveň
        (kladný zisk, záporná ztráta). Protějšek _level_from_option_profit:
        model z implikované volatility, bez kotací lineárně přes deltu.
        """
        sazba = self.cfg.trading.risk_free_rate_pct
        if cena_opce and podklad:
            cena_na_vstupu = calc.project_option_price(
                cena_opce, podklad, entry_price, strike, expiration, sazba, right
            )
            cena_na_urovni = calc.project_option_price(
                cena_opce, podklad, uroven, strike, expiration, sazba, right
            )
            if cena_na_vstupu is not None and cena_na_urovni is not None:
                return (cena_na_urovni - cena_na_vstupu) * calc.OPTION_MULTIPLIER

        # Náhradní delta tady nese znaménko podle typu opce - výsledek se jím násobí
        delta = self._model_delta(
            cena_opce, podklad, strike, expiration, right, delta, se_znamenkem=True
        )
        return (uroven - entry_price) * delta * calc.OPTION_MULTIPLIER

    async def _reference_option(
        self, preview: Preview, chain: Any, entry_price: float
    ) -> _ReferenceOption | None:
        """
        Referenční opce pro převody mezi USD na opci a úrovní podkladu:
        kontrakt se strike nejblíže vstupní ceně včetně aktuální ceny a delty.
        Odběr jejích dat uvolňuje volající, až si přihlásí vybranou opci.
        Bez obchodovatelného strike vrací None.
        """
        try:
            ref_strike, ref_option, ref_details = await self._qualify_nearest_option(
                preview.symbol,
                preview.expiration,
                list(chain.strikes),
                entry_price,
                preview.right,
                chain.tradingClass,
            )
        except ValueError:
            return None

        self.ib.subscribe(ref_option)
        try:
            await self.ib.wait_for_quotes(
                ref_option,
                self.cfg.engine.market_data_timeout_sec,
                self.cfg.engine.quotes_grace_sec,
            )
            _, _, delta = self.ib.option_quotes(ref_option)
            cena, zdroj = self.ib.option_price(ref_option)
        except BaseException:
            # Volající odběr převezme až s vrácenou referenční opcí; skončí-li
            # čekání chybou nebo zrušením, musí se uvolnit tady
            self.ib.unsubscribe(ref_option)
            raise
        return _ReferenceOption(
            strike=ref_strike,
            contract=ref_option,
            details=ref_details,
            price=cena,
            price_source=zdroj,
            delta=delta,
        )

    def _target_level_for_option_pt(
        self,
        preview: Preview,
        entry_price: float,
        zisk_usd: float,
        referencni: _ReferenceOption | None,
    ) -> float:
        """
        Cílová úroveň podkladu pro výběr strike, když je PT zadané ziskem na opci.

        Z ceny referenční opce (strike u vstupu) se odvodí, kam musí podklad
        dojít, aby opce vydělala požadovaný zisk. Strike se pak vybírá k této
        úrovni - stejně jako u PT na podkladu tedy leží na cílové úrovni.
        Bez referenční opce zůstává vstupní cena.
        """
        if referencni is None:
            preview.target_level = entry_price
            preview.target_level_source = "vstupní cena"
            return entry_price

        uroven, zdroj = self._level_from_option_profit(
            referencni.price,
            preview.current_price,
            entry_price,
            zisk_usd,
            referencni.strike,
            preview.expiration,
            preview.right,
            referencni.delta,
            referencni.price_source,
        )
        preview.target_level = uroven
        preview.target_level_source = zdroj
        return uroven

    def _default_profit_target(
        self,
        preview: Preview,
        entry_price: float,
        stop_loss: float,
        referencni: _ReferenceOption | None,
    ) -> float:
        """
        PT podle poměru SL:PT z konfigurace, když obchodník zadal jen SL.

        Ve stejném režimu je to prostý podíl: na podkladu vzdálenost SL od
        vstupu dělená poměrem (na opačnou stranu), na opci ztráta v USD
        dělená poměrem. Při smíšeném režimu se převádí přes cenu referenční
        opce: buď se hledá úroveň podkladu, kde opce vydělá SL/poměr USD,
        nebo se ztráta na SL podkladu přepočte do USD a vydělí poměrem.
        """
        pomer = self.cfg.trading.sl_to_pt_ratio
        if preview.pt_on_underlying and preview.sl_on_underlying:
            return calc.default_profit_target(entry_price, stop_loss, pomer)
        if not preview.pt_on_underlying and not preview.sl_on_underlying:
            return round(stop_loss / pomer, 2)

        cena = referencni.price if referencni is not None else None
        strike = referencni.strike if referencni is not None else entry_price
        delta = referencni.delta if referencni is not None else None
        zisk_usd = stop_loss / pomer

        if preview.pt_on_underlying:
            # SL na opci, PT na podkladu: kde opce vydělá SL / poměr
            uroven, _ = self._level_from_option_profit(
                cena,
                preview.current_price,
                entry_price,
                zisk_usd,
                strike,
                preview.expiration,
                preview.right,
                delta,
            )
            return round(uroven, 2)

        # SL na podkladu, PT na opci: ztráta na SL v USD dělená poměrem
        ztrata = self._profit_from_underlying_level(
            cena,
            preview.current_price,
            entry_price,
            stop_loss,
            strike,
            preview.expiration,
            preview.right,
            delta,
        )
        return round(max(abs(ztrata) / pomer, 0.01), 2)

    def _default_stop_loss(
        self, preview: Preview, entry_price: float, profit_target: float, delta: float
    ) -> float:
        """
        SL podle poměru SL:PT z konfigurace, když jej uživatel nezadal.

        Ve stejném režimu jako PT jde o prostý násobek: na podkladu vzdálenost
        od vstupu, na opci zisk v USD. Při smíšeném režimu se zisk na PT
        převádí mezi podkladem a cenou opce stejným modelem jako sloupec
        "Zisk na PT" (implikovaná volatilita z aktuální ceny opce); bez
        kotací lineárně přes deltu.
        """
        pomer = self.cfg.trading.sl_to_pt_ratio
        if preview.pt_on_underlying and preview.sl_on_underlying:
            return calc.default_stop_loss(entry_price, profit_target, pomer)
        if not preview.pt_on_underlying and not preview.sl_on_underlying:
            return round(profit_target * pomer, 2)

        # Cena vybrané opce pro model - střed kotace, jinak last/close
        cena = preview.option_price
        podklad = preview.current_price

        if preview.pt_on_underlying:
            # PT na podkladu, SL na opci: očekávaný zisk na PT v USD krát poměr
            zisk = self._profit_from_underlying_level(
                cena,
                podklad,
                entry_price,
                profit_target,
                preview.strike,
                preview.expiration,
                preview.right,
                delta,
            )
            if zisk <= 0:
                # Model dal nesmyslný výsledek - lineárně přes deltu
                zisk = abs(profit_target - entry_price) * abs(delta) * calc.OPTION_MULTIPLIER
            return round(max(zisk * pomer, 0.01), 2)

        # PT na opci, SL na podkladu: úroveň podkladu, kde opce ztratí PT krát
        # poměr - tedy tentýž převod jako u cíle, jen se záporným "ziskem"
        uroven, _ = self._level_from_option_profit(
            cena,
            podklad,
            entry_price,
            -profit_target * pomer,
            preview.strike,
            preview.expiration,
            preview.right,
            delta,
        )
        return round(uroven, 2)

    def _capped_option_loss(self, preview: Preview, entry_price: float, stop_loss: float) -> float:
        """
        Ztráta na kontrakt omezená zaplacenou prémií.

        SL zadaný na opci může být větší než celá prémie; stop pak stojí na
        nejnižší možné ceně a obchod nemůže ztratit víc. Nákupní cena se
        odhaduje modelem pro chvíli, kdy podklad dosáhne vstupní úrovně -
        nakupuje se u ASKu, proto se k modelovému středu přičítá půl spreadu.
        Bez použitelného modelu zůstává zadaná ztráta.
        """
        if not preview.option_price or preview.current_price is None:
            return stop_loss
        cena = calc.project_option_price(
            preview.option_price,
            preview.current_price,
            entry_price,
            preview.strike,
            preview.expiration,
            self.cfg.trading.risk_free_rate_pct,
            preview.right,
        )
        if cena is None:
            return stop_loss
        if preview.option_bid and preview.option_ask:
            cena += (preview.option_ask - preview.option_bid) / 2.0
        return min(stop_loss, calc.max_option_loss(cena, preview.min_tick))

    def _estimate_delta(self, preview: Preview) -> float | None:
        """
        Dopočítá deltu z tržní ceny opce, když ji TWS nepošle.
        Používá se střed kotace, při jeho nedostupnosti poslední známá cena.
        """
        cena = preview.option_price
        if cena is None or preview.current_price is None:
            return None

        return calc.estimate_delta(
            cena,
            preview.current_price,
            preview.strike,
            preview.expiration,
            self.cfg.trading.risk_free_rate_pct,
            preview.right,
        )

    def _replace_preview(self, preview: Preview) -> None:
        """
        Nahradí držený náhled novým a uvolní odběry tržních dat toho předchozího.
        Kontrakty použité v založeném flow zůstávají odebírané díky počítadlu odběratelů.
        """
        old = self._preview
        self._preview = preview
        if old is not None:
            self.ib.unsubscribe(old.underlying)
            self.ib.unsubscribe(old.option)

    def release_preview(self) -> None:
        """Uvolní odběry držené posledním náhledem."""
        if self._preview is not None:
            self.ib.unsubscribe(self._preview.underlying)
            self.ib.unsubscribe(self._preview.option)
            self._preview = None

    # ------------------------------------------------------------------
    # Založení a zrušení flow
    # ------------------------------------------------------------------

    @staticmethod
    def _check_profit_target(
        right: str,
        entry: float,
        pt: float,
        pt_on_underlying: bool,
        popis: str = "PT",
    ) -> None:
        """
        Ověří cílovou úroveň: na podkladu musí u CALL ležet nad vstupem
        a u PUT pod ním, na opci musí být zisk kladná částka v USD
        na kontrakt. Popis se objeví v chybové hlášce ('PT', 'Cíl runneru').
        """
        if not pt_on_underlying:
            if pt <= 0:
                raise ValueError(f"{popis} na opci musí být kladná částka v USD na kontrakt.")
            return
        if right == "C" and pt <= entry:
            raise ValueError(f"U CALL opce musí {popis} ležet nad vstupní cenou podkladu.")
        if right == "P" and pt >= entry:
            raise ValueError(f"U PUT opce musí {popis} ležet pod vstupní cenou podkladu.")

    def _validate(
        self,
        right: str,
        entry: float,
        pt: float,
        sl: float,
        pt_on_underlying: bool = True,
        sl_on_underlying: bool = True,
    ) -> None:
        """
        Ověří zadané úrovně vůči typu opce.
        Na podkladu musí být u CALL PT nad vstupem a SL pod ním, u PUT opačně.
        Zisk a ztráta zadané na opci musí být kladné částky v USD na kontrakt.
        """
        self._check_profit_target(right, entry, pt, pt_on_underlying)

        if sl_on_underlying:
            if right == "C" and sl >= entry:
                raise ValueError("U CALL opce musí být SL pod vstupní cenou podkladu.")
            if right == "P" and sl <= entry:
                raise ValueError("U PUT opce musí být SL nad vstupní cenou podkladu.")
        elif sl <= 0:
            raise ValueError("Ztráta na opci (SL) musí být kladná částka v USD na kontrakt.")

    async def start_flow(self, request: FlowRequest) -> Flow:
        """
        Založí nové flow: ověří zadání, vybere kontrakt a zadá nákupní příkaz
        s cenovou podmínkou na podkladu do TWS.
        """
        async with self._lock:
            symbol = request.symbol.upper().strip()

            # Aspoň jedna úroveň musí být zadaná - druhá se dopočítá z poměru
            if request.profit_target is None and request.stop_loss is None:
                raise ValueError("Zadejte PT nebo SL - chybějící úroveň se dopočítá.")

            # Zamýšlený směr obchodu prozrazuje poloha PT (případně SL) na
            # podkladu: cíl nad vstupem = průraz nahoru (long/CALL), pod
            # vstupem průraz dolů (short/PUT). Jsou-li obě úrovně zadané
            # na opci, rozhoduje až poloha vstupu vůči aktuální ceně.
            zamer = calc.intended_right(
                request.entry_price,
                request.profit_target,
                request.stop_loss,
                request.pt_on_underlying,
                request.sl_on_underlying,
            )

            def overit_bezici(smer: str) -> Flow | None:
                """
                Na jednom tickeru smí běžet současně jeden long a jeden short.
                Nové zadání nahrazuje jen čekající obchod STEJNÉHO směru; obchod
                s otevřenou (či právě uzavíranou) pozicí se chrání.
                """
                bezici = self.active_flow_for(symbol, smer)
                if bezici is not None and not bezici.state.is_before_entry:
                    smer_popis = "long (CALL)" if smer == "C" else "short (PUT)"
                    raise ValueError(
                        f"Pro ticker {symbol} již běží {smer_popis} obchod s otevřenou "
                        f"pozicí. Nejprve jej zrušte."
                    )
                return bezici

            # Je-li směr znám předem, chráněný obchod se odhalí ještě před
            # dotazy do TWS; jinak se ověří, jakmile směr určí příprava
            bezici = overit_bezici(zamer) if zamer is not None else None

            preview = await self.prepare(
                symbol,
                request.entry_price,
                request.profit_target,
                request.stop_loss,
                request.pt_on_underlying,
                request.sl_on_underlying,
            )

            # Propásnutý vstup se hlásí dřív než ostatní kontroly, jinak by
            # uživatel dostal matoucí hlášku o poloze PT vůči vstupu.
            # Liší-li se zamýšlený směr od typu opce odvozeného z aktuální
            # ceny, cena už vstupní úroveň překonala a obchod ujel.
            if zamer is None:
                zamer = preview.right
                bezici = overit_bezici(zamer)
            elif preview.current_price is not None and zamer != preview.right:
                smer = "nad" if zamer == "C" else "pod"
                raise ValueError(
                    f"Cena podkladu {preview.current_price:g} je již {smer} vstupem "
                    f"{request.entry_price:g} - vstup je propásnutý a obchod nelze zadat."
                )

            # Zadané úrovně mají přednost, chybějící dodala příprava
            profit_target = (
                request.profit_target
                if request.profit_target is not None
                else preview.profit_target
            )
            stop_loss = request.stop_loss if request.stop_loss is not None else preview.stop_loss
            self._validate(
                preview.right,
                request.entry_price,
                profit_target,
                stop_loss,
                request.pt_on_underlying,
                request.sl_on_underlying,
            )

            quantity = int(request.quantity or preview.quantity)
            if quantity < 1:
                raise ValueError("Množství opcí musí být alespoň 1 kontrakt.")

            max_spread = (
                request.max_spread_pct
                if request.max_spread_pct is not None
                else self.cfg.trading.max_spread_pct
            )

            # Čekající obchod stejného směru se nahrazuje až teď, kdy nové
            # zadání prošlo všemi kontrolami - kdyby dřív selhalo, původní
            # obchod by byl zrušený a žádný nový by nevznikl
            runner_nasobek: float | None = None
            if bezici is not None:
                # Runner nastavený na čekajícím obchodu nesmí nahrazením tiše
                # zaniknout - jeho násobek cíle se přenese do nového zadání
                runner_nasobek = bezici.runner_multiple
                self._cancel_locked(bezici)
                # Nahrazený obchod z přehledu zmizí - nové zadání jej přepisuje
                self.flows.pop(bezici.id, None)
                self.log_event(f"{bezici.id}: nahrazeno novým zadáním obchodu.")

            flow = Flow(
                id=f"{symbol}-{next(self._ids)}",
                symbol=symbol,
                entry_price=request.entry_price,
                profit_target=profit_target,
                original_profit_target=profit_target,
                stop_loss=stop_loss,
                original_stop_loss=stop_loss,
                quantity=quantity,
                max_spread_pct=max_spread,
                right=preview.right,
                pt_on_underlying=request.pt_on_underlying,
                sl_on_underlying=request.sl_on_underlying,
                expiration=preview.expiration,
                strike=preview.strike,
                min_tick=preview.min_tick,
                option_contract=preview.option,
                underlying_contract=preview.underlying,
                option_conid=preview.option.conId,
                underlying_conid=preview.underlying.conId,
                underlying_price=preview.current_price,
                option_bid=preview.option_bid,
                option_ask=preview.option_ask,
                option_spread_pct=preview.spread_pct,
                delta=preview.delta,
            )

            # Runner z nahrazeného obchodu se přepočítá na úrovně nového zadání
            if runner_nasobek is not None:
                self._adopt_runner(flow, runner_nasobek)

            # Flow přebírá vlastní odběr tržních dat obou kontraktů
            self.ib.subscribe(flow.underlying_contract)
            self.ib.subscribe(flow.option_contract)

            self.flows[flow.id] = flow
            self.log_event(
                f"{flow.id}: založeno flow {flow.option_label()}, množství {quantity}, "
                f"vstup {request.entry_price:g}, PT {flow.level_text('pt')}, "
                f"SL {flow.level_text('sl')}."
            )

            # Zdroj ceny pro model se hlásí i do logu - v náhledu ho obchodník
            # vidět nemusel, přesto z něj vycházejí dopočítané úrovně
            if preview.option_price_source == "close":
                self.log_event(
                    f"{flow.id}: POZOR - opce nemá aktuální kotace, model počítal ze "
                    f"závěrečné ceny; zkontrolujte dopočítané úrovně i množství."
                )

            # Při příliš širokém spreadu se příkaz zatím nezadává
            if flow.option_spread_pct is not None and flow.option_spread_pct > max_spread:
                flow.set_state(
                    FlowState.SPREAD_BLOCKED,
                    f"Spread {flow.option_spread_pct:.2f} % > limit {max_spread:g} %, "
                    f"příkaz nebyl zadán.",
                )
                self.log_event(f"{flow.id}: {flow.message}")
            else:
                self._place_entry(flow)

            self._notify()
            return flow

    def _adopt_runner(self, flow: Flow, nasobek: float) -> None:
        """
        Převezme runner z nahrazeného obchodu: stejný násobek cíle se
        přepočítá na úrovně nového zadání. SL runneru začíná na SL obchodu,
        stejně jako při ručním zapnutí runneru.
        """
        runner_q = self.cfg.trading.runner_quantity
        if flow.quantity <= runner_q:
            self.log_event(
                f"{flow.id}: runner z nahrazeného obchodu nelze převzít - "
                f"množství {flow.quantity} ks na něj nestačí."
            )
            return

        cil = flow.scaled_target(nasobek)
        flow.runner_profit_target = cil
        flow.runner_quantity = runner_q
        flow.runner_stop_loss = flow.stop_loss
        self.log_event(
            f"{flow.id}: runner {runner_q} ks převzat z nahrazeného obchodu, "
            f"cíl {flow.level_text('pt', cil)} ({nasobek:g}× původní cíl)."
        )

    def _compute_expected_pnl(self, flow: Flow) -> None:
        """
        Spočítá očekávaný výsledek obchodu při dosažení PT a SL.

        Opce se přecení z aktuální implikované volatility pro cenu podkladu
        na úrovni PT, resp. SL, a rozdíl proti nákupní ceně se přepočte
        na peníze.

        Nákupní cenou je po nákupu skutečně dosažená cena. Před nákupem se
        opce přecení na vstupní úroveň, protože právě tam se bude kupovat -
        použít místo toho její dnešní cenu by výsledek zkreslilo. U obchodu
        čekajícího na pokles podkladu by dokonce vycházela ztráta na SL jako
        zisk, protože SL leží blíž k dnešní ceně než vstup.

        Předpokládá se, že podklad úrovní dosáhne brzy a volatilita zůstane
        stejná; při pozdějším pohybu bude výsledek nižší o časový rozpad.
        Prodejní ceny počítají s vyplněním u BIDu (tržní příkaz), nákupní
        s vyplněním u ASKu - od/k modelovému středu se odečítá/přičítá
        půl aktuálního spreadu.
        """
        bid, ask, _ = self.ib.option_quotes(flow.option_contract)
        # Cena opce pro model: střed kotace, bez kotací poslední/závěrečná cena
        aktualni, _ = self.ib.option_price(flow.option_contract)
        podklad = flow.underlying_price
        sazba = self.cfg.trading.risk_free_rate_pct

        def cena_pri(uroven: float) -> float | None:
            """Odhad ceny opce, až podklad dosáhne dané úrovně."""
            if not aktualni or not podklad:
                return None
            return calc.project_option_price(
                aktualni, podklad, uroven, flow.strike, flow.expiration, sazba, flow.right
            )

        # Model přeceňuje na střed trhu, prodává se ale tržním příkazem u BIDu -
        # od modelové prodejní ceny se proto odečítá půl aktuálního spreadu
        pul_spreadu = (ask - bid) / 2.0 if bid and ask else 0.0

        def prodejni_cena_pri(uroven: float) -> float | None:
            """Odhad prodejní ceny (u BIDu), až podklad dosáhne dané úrovně."""
            cena = cena_pri(uroven)
            if cena is None:
                return None
            # Cena opce nemůže být záporná ani po odečtení půl spreadu
            return max(cena - pul_spreadu, 0.0)

        if flow.fill_price:
            nakupni = flow.fill_price
        else:
            nakupni = cena_pri(flow.entry_price)
            # Model dává střed trhu, nakupuje se ale na ASK - přičte se půl spreadu
            if nakupni is not None and bid and ask:
                nakupni += pul_spreadu

        def vysledek_na_kontrakt(uroven: float, na_podkladu: bool, zisk: bool) -> float | None:
            """
            Výsledek jednoho kontraktu v USD při dosažení úrovně.
            Úroveň na opci je rovnou částka (zisk kladný, ztráta záporná);
            úroveň na podkladu se přeceňuje modelem proti nákupní ceně.
            """
            if not na_podkladu:
                if zisk:
                    return uroven
                # Ztráta na opci nemůže přesáhnout zaplacenou prémii - stop
                # stojí nejvýš o ni níž (nejnižší možná cena je jeden tik)
                if nakupni is None:
                    return -uroven
                return -min(uroven, calc.max_option_loss(nakupni, flow.min_tick))
            if nakupni is None:
                return None
            cena = prodejni_cena_pri(uroven)
            if cena is None:
                return None
            return (cena - nakupni) * calc.OPTION_MULTIPLIER

        zisk_pt = vysledek_na_kontrakt(flow.profit_target, flow.pt_on_underlying, True)
        ztrata_sl = vysledek_na_kontrakt(flow.stop_loss, flow.sl_on_underlying, False)
        if zisk_pt is None or ztrata_sl is None:
            flow.expected_profit = None
            flow.expected_loss = None
            return

        # Počítá se výhradně dosud otevřená část obchodu. Realizovaný výsledek
        # prodaných runnerů ani prodané hlavní části se nezapočítává - sloupce
        # ukazují jen to, co může otevřený zbytek pozice ještě vydělat či ztratit
        hlavni_q = flow.main_quantity if flow.exit_fill_price is None else 0

        runner_q = 0
        zisk_runner = None
        if flow.runner_active and flow.runner_quantity <= flow.held_quantity:
            zisk_runner = vysledek_na_kontrakt(
                flow.runner_profit_target, flow.pt_on_underlying, True
            )
            if zisk_runner is not None:
                runner_q = flow.runner_quantity

        # Bez otevřených kusů není co odhadovat - uzavřený obchod má pomlčku
        if hlavni_q + runner_q == 0:
            flow.expected_profit = None
            flow.expected_loss = None
            return

        flow.expected_profit = zisk_pt * hlavni_q
        if runner_q:
            flow.expected_profit += zisk_runner * runner_q

        # Ztráta hlavní části na jejím SL; runner může mít vlastní SL (třeba
        # break even), proto se jeho část oceňuje na jeho úrovni
        flow.expected_loss = ztrata_sl * hlavni_q
        if runner_q:
            ztrata_runner = vysledek_na_kontrakt(flow.runner_sl, flow.sl_on_underlying, False)
            if ztrata_runner is None:
                ztrata_runner = ztrata_sl
            flow.expected_loss += ztrata_runner * runner_q

    def _place_entry(self, flow: Flow) -> bool:
        """
        Zadá nákupní příkaz s cenovou podmínkou na dosažení vstupní ceny podkladu.
        Vrací False, pokud příkaz zatím zadat nelze - o zadání se pokusí další průchod smyčkou.
        """
        # Obchod má smysl jen dokud cena podkladu vstupní úroveň nepřekonala
        cena = self.ib.underlying_price(flow.underlying_contract)
        if cena is None:
            flow.set_state(
                FlowState.NO_QUOTES,
                "Cena podkladu není k dispozici, příkaz zatím nelze zadat.",
            )
            return False

        if not calc.entry_still_valid(flow.right, cena, flow.entry_price):
            smer = "nad" if flow.right == "C" else "pod"
            flow.set_state(
                FlowState.MISSED,
                f"Cena podkladu {cena:g} je {smer} vstupem {flow.entry_price:g} - "
                f"vstup propásnut, obchod ukončen bez zadání příkazu.",
            )
            self._release(flow)
            self.log_event(f"{flow.id}: {flow.message}")
            return False

        limit = self._entry_limit(flow)

        # Bez kotací opce nelze určit limitní cenu. Tržní příkaz by se v takové
        # situaci vyplnil za neznámou cenu, proto se čeká na data z TWS.
        if self.cfg.trading.entry_order_type != "MKT" and limit is None:
            flow.set_state(
                FlowState.NO_QUOTES,
                "Z TWS nedorazily kotace opce, limitní příkaz zatím nelze zadat "
                "(mimo obchodní hodiny nebo chybí předplatné dat).",
            )
            return False

        entry_more, _, _ = calc.condition_directions(flow.right)
        condition = self.ib.price_condition(flow.underlying_conid, entry_more, flow.entry_price)
        order = self.ib.build_entry_order(
            flow.quantity, limit, [condition], order_ref(flow.id, "entry")
        )
        flow.entry_trade = self.ib.place(flow.option_contract, order)
        flow.entry_order_id = flow.entry_trade.order.orderId
        flow.entry_limit = limit

        limit_text = f"LMT {limit:g}" if limit is not None else "MKT"
        direction = ">=" if entry_more else "<="
        flow.set_state(
            FlowState.ARMED,
            f"Příkaz v trhu ({limit_text}), podmínka: {flow.symbol} {direction} {flow.entry_price:g}.",
        )
        self.log_event(f"{flow.id}: nákupní příkaz zadán - {flow.message}")
        return True

    def _entry_limit(self, flow: Flow) -> float | None:
        """Limitní cena nákupu podle typu příkazu z konfigurace, zaokrouhlená na tik."""
        bid, ask, _ = self.ib.option_quotes(flow.option_contract)
        price = calc.entry_limit_price(
            self.cfg.trading.entry_order_type, bid, ask, self.cfg.trading.ask_tolerance_pct
        )
        if price is None:
            return None
        return calc.round_to_tick(price, flow.min_tick)

    def _ensure_fill_price(self, flow: Flow) -> None:
        """
        Doplní nákupní cenu opce, kterou TWS neposlala (tržní nákup bez limitu).

        Úrovně zadané na cenu opce se od nákupní ceny odvíjejí, bez ní by
        pozice zůstala bez zajištění. Jako náhrada se bere aktuální cena opce -
        základ pro PT/SL je pak jen přibližný, proto se to hlásí do logu.
        """
        if flow.fill_price is not None or not flow.exit_split:
            return
        nahradni, zdroj = self.ib.option_price(flow.option_contract)
        if nahradni is None:
            raise ValueError(
                "Nákupní cena opce není známa a opce nemá žádnou cenu - PT/SL "
                "zadané na cenu opce nelze zadat, zkontrolujte pozici v TWS."
            )
        flow.fill_price = nahradni
        self.log_event(
            f"{flow.id}: POZOR - TWS neposlala nákupní cenu opce, jako základ pro "
            f"PT/SL na cenu opce se bere aktuální cena {nahradni:g} ({zdroj}). "
            f"Zkontrolujte skutečnou nákupní cenu v TWS."
        )

    def _place_exit(self, flow: Flow, quantity: int) -> None:
        """
        Zadá zajišťovací prodejní příkazy pro PT i SL.

        Bez runneru se zajišťuje celá pozice najednou. S aktivním runnerem
        se pozice dělí: hlavní část prodává na PT obchodu, runner samostatně
        na vlastním cíli; SL mají oba stejný. Kolik příkazů na jednu část
        vznikne, určuje režim PT a SL (viz _build_part_orders).
        """
        self._ensure_fill_price(flow)

        # Prodaná hlavní část se znovu nezajišťuje - zbylé kusy drží runner
        if flow.exit_fill_price is not None:
            if not flow.runner_active or quantity < 1:
                flow.set_state(
                    FlowState.EXIT_ARMED,
                    "Hlavní část pozice je prodaná, zajišťovat není co.",
                )
                return
            mnozstvi = min(quantity, flow.runner_quantity)
            popis = self._place_part(flow, "runner", mnozstvi)
            flow.runner_quantity = mnozstvi
            flow.set_state(FlowState.EXIT_ARMED, f"Prodej runneru {mnozstvi} ks: {popis}")
            self.log_event(f"{flow.id}: {flow.message}")
            return

        # Runner se uplatní, jen když na něj po odečtení zbude aspoň 1 kontrakt
        runner_q = 0
        if flow.runner_active and quantity > flow.runner_quantity:
            runner_q = flow.runner_quantity
        elif flow.runner_active:
            # Runner zůstává zapamatovaný - oddělí se, pokud se nákup ještě
            # doplní; jinak jej smyčka zruší, aby nezůstal viset bez příkazu
            self.log_event(
                f"{flow.id}: nakoupené množství {quantity} ks na runner zatím "
                f"nestačí, prodává se jedním příkazem."
            )
        hlavni_q = quantity - runner_q

        popis = self._place_part(flow, "exit", hlavni_q)

        popis_runneru = ""
        if runner_q:
            popis_runneru = f" Runner {runner_q} ks: {self._place_part(flow, 'runner', runner_q)}"

        flow.set_state(
            FlowState.EXIT_ARMED,
            f"Prodej {hlavni_q} ks: {popis}{popis_runneru}",
        )
        self.log_event(f"{flow.id}: {flow.message}")

    # ------------------------------------------------------------------
    # Části pozice a jejich prodejní příkazy
    #
    # Pozice má nejvýše dvě části: hlavní ('exit') a runner ('runner').
    # Každá část se prodává buď jediným podmíněným příkazem (PT i SL na
    # podkladu, podmínky spojené OR), nebo dvojicí příkazů - jedním pro PT
    # a druhým pro SL - jakmile je aspoň jedna úroveň zadaná přímo na opci.
    # Dvojice je svázaná OCA skupinou v TWS a navíc ji hlídá smyčka: po
    # vyplnění jednoho příkazu se druhý ihned ruší, aby se opce neprodala
    # dvakrát. První slot části nese příkaz pro PT (nebo jediný společný),
    # druhý slot příkaz pro SL.
    # ------------------------------------------------------------------

    @staticmethod
    def _slot_names(part: str, which: str) -> tuple[str, str]:
        """Názvy polí flow (trade, order_id) pro daný slot části ('pt' / 'sl')."""
        if which == "pt":
            return f"{part}_trade", f"{part}_order_id"
        return f"{part}_sl_trade", f"{part}_sl_order_id"

    def _leg(self, flow: Flow, part: str, which: str) -> Any:
        """Příkaz (Trade) v daném slotu části, nebo None."""
        return getattr(flow, self._slot_names(part, which)[0])

    def _legs(self, flow: Flow, part: str) -> list[Any]:
        """Všechny existující příkazy dané části pozice."""
        return [
            trade
            for trade in (self._leg(flow, part, "pt"), self._leg(flow, part, "sl"))
            if trade is not None
        ]

    def _set_leg(self, flow: Flow, part: str, which: str, trade: Any) -> None:
        """Uloží příkaz do slotu části včetně jeho čísla v TWS."""
        trade_name, id_name = self._slot_names(part, which)
        setattr(flow, trade_name, trade)
        setattr(flow, id_name, trade.order.orderId if trade is not None else None)

    @staticmethod
    def _sold_prefix(part: str) -> str:
        """Předpona polí flow se souhrnem prodaných kusů dané části."""
        return "main" if part == "exit" else "runner"

    def _clear_part(self, flow: Flow, part: str) -> None:
        """
        Vyprázdní oba sloty části - příkazy už nejsou v trhu.

        Zároveň se nuluje počitadlo započtených vyplnění (další generace
        příkazů začíná od nuly, už zaúčtované kusy zůstávají v souhrnu)
        a zapomíná se varování o ztraceném příkazu z dvojice, aby nová
        dvojice mohla varovat znovu.
        """
        for which in ("pt", "sl"):
            self._set_leg(flow, part, which, None)
        predpona = self._sold_prefix(part)
        setattr(flow, f"{predpona}_counted_quantity", 0)
        setattr(flow, f"{predpona}_counted_value", 0.0)
        self._lost_leg_warned.discard(f"{flow.id}:{part}")

    def _cancel_part(self, flow: Flow, part: str) -> None:
        """Zruší všechny aktivní příkazy části."""
        for trade in self._legs(flow, part):
            self.ib.cancel(trade)

    def _part_modifiable(self, flow: Flow, part: str) -> bool:
        """True, pokud část má příkazy a všechny lze v TWS ještě upravit."""
        legy = self._legs(flow, part)
        return bool(legy) and all(
            trade.orderStatus.status in MODIFIABLE_ORDER_STATES for trade in legy
        )

    def _part_all_dead(self, flow: Flow, part: str) -> bool:
        """True, pokud žádný příkaz části už není v trhu (ani žádný nezbývá)."""
        return all(
            trade.orderStatus.status in DEAD_ORDER_STATES for trade in self._legs(flow, part)
        )

    def _part_out_of_market(self, flow: Flow, part: str) -> bool:
        """
        True, pokud žádný příkaz části už nemůže prodat - byl zrušen, nebo se
        celý vyplnil. Na rozdíl od _part_all_dead počítá i s vyplněným příkazem:
        ten sice není zrušený, ale v trhu po něm také nic nezůstalo.
        """
        return all(
            trade.orderStatus.status in DEAD_ORDER_STATES + ("Filled",)
            for trade in self._legs(flow, part)
        )

    def _part_covered(self, flow: Flow, part: str) -> bool:
        """
        True, pokud má část v trhu tolik živých příkazů, kolik jich režim PT
        a SL vyžaduje (dvojice při odděleném výstupu, jinak jeden).
        Vyplněný příkaz se za živý nepovažuje - prodal, co měl, a v trhu
        už není; zajištění zbytku by po něm chybělo.
        """
        zive = [
            trade
            for trade in self._legs(flow, part)
            if trade.orderStatus.status not in DEAD_ORDER_STATES + ("Filled",)
        ]
        return len(zive) >= (2 if flow.exit_split else 1)

    def _market_sell_running(self, flow: Flow, part: str) -> bool:
        """
        True, pokud je v prvním slotu části živý tržní prodej bez podmínek -
        tedy rozdělané uzavírání na pokyn obchodníka, které má doběhnout.
        """
        trade = self._leg(flow, part, "pt")
        if trade is None or trade.orderStatus.status in DEAD_ORDER_STATES + ("Filled",):
            return False
        return trade.order.orderType == "MKT" and not trade.order.conditions

    def _filled_leg(self, flow: Flow, part: str) -> Any:
        """Vyplněný příkaz části, pokud některý je; jinak None."""
        for trade in self._legs(flow, part):
            if trade.orderStatus.status == "Filled":
                return trade
        return None

    def _cancel_other_legs(self, flow: Flow, part: str, vyplneny: Any) -> None:
        """
        Po vyplnění jednoho příkazu části zruší ten druhý. TWS ho přes OCA
        skupinu ruší také, ale nečeká se na to - jde o to, aby se opce
        v žádném případě neprodala dvakrát.
        """
        for trade in self._legs(flow, part):
            if trade is not vyplneny:
                self.ib.cancel(trade)

    def _part_levels(self, flow: Flow, part: str) -> tuple[float, float]:
        """Úrovně (PT, SL) dané části - hlavní obchodu, nebo vlastní runneru."""
        if part == "runner":
            return flow.runner_profit_target, flow.runner_sl
        return flow.profit_target, flow.stop_loss

    def _exit_limit(self, flow: Flow) -> float | None:
        """Limitní cena prodeje pod BIDem pro výstupní typ LMT z konfigurace."""
        if self.cfg.trading.exit_order_type != "LMT":
            return None
        bid, _, _ = self.ib.option_quotes(flow.option_contract)
        price = calc.exit_limit_price(bid, self.cfg.trading.bid_tolerance_pct)
        return calc.round_to_tick(price, flow.min_tick) if price is not None else None

    def _oca_group(self, flow: Flow, part: str) -> str:
        """
        Název OCA skupiny pro dvojici příkazů části. Musí být v rámci účtu
        jedinečný, proto nese i čas - po restartu či opětovném zajištění
        nesmí nové příkazy spadnout do skupiny těch starých.
        """
        return f"{flow.id}-{part}-{datetime.now():%H%M%S%f}"

    def _build_part_orders(self, flow: Flow, part: str, quantity: int) -> list[tuple[str, Any]]:
        """
        Sestaví prodejní příkazy části podle režimu PT a SL:

          PT podklad, SL podklad - jeden MKT/LMT příkaz s podmínkami PT OR SL
          PT podklad, SL opce    - MKT s podmínkou PT + stop-market na cenu opce
          PT opce,    SL podklad - limit na cenu opce + MKT s podmínkou SL
          PT opce,    SL opce    - limit na cenu opce + stop-market na cenu opce

        Vrací dvojice (slot, příkaz). Dvojice příkazů sdílí OCA skupinu.
        """
        pt_level, sl_level = self._part_levels(flow, part)
        _, pt_more, sl_more = calc.condition_directions(flow.right)
        conid = flow.underlying_conid
        ref_pt = order_ref(flow.id, part)
        ref_sl = order_ref(flow.id, f"{part}sl")

        if not flow.exit_split:
            podminky = [
                self.ib.price_condition(conid, pt_more, pt_level),
                self.ib.price_condition(conid, sl_more, sl_level),
            ]
            # Limitní výstup z konfigurace se týká jen hlavní části, runner
            # se vždy prodává trhem
            limit = self._exit_limit(flow) if part == "exit" else None
            return [("pt", self.ib.build_exit_order(quantity, limit, podminky, ref_pt))]

        # Úroveň na opci se odvíjí od nákupní ceny; tu doplňuje _ensure_fill_price
        # ještě před zadáním zajištění
        if flow.fill_price is None:
            raise ValueError(
                "Nákupní cena opce není známa - PT/SL zadané na cenu opce nelze zadat, "
                "zkontrolujte pozici v TWS."
            )

        oca = self._oca_group(flow, part)
        prikazy: list[tuple[str, Any]] = []
        if flow.pt_on_underlying:
            podminka = [self.ib.price_condition(conid, pt_more, pt_level)]
            prikazy.append(("pt", self.ib.build_exit_order(quantity, None, podminka, ref_pt, oca)))
        else:
            limit = calc.option_profit_limit(flow.fill_price, pt_level, flow.min_tick)
            prikazy.append(("pt", self.ib.build_limit_sell_order(quantity, limit, ref_pt, oca)))

        if flow.sl_on_underlying:
            podminka = [self.ib.price_condition(conid, sl_more, sl_level)]
            prikazy.append(("sl", self.ib.build_exit_order(quantity, None, podminka, ref_sl, oca)))
        else:
            stop = calc.option_loss_stop(flow.fill_price, sl_level, flow.min_tick)
            prikazy.append(("sl", self.ib.build_stop_sell_order(quantity, stop, ref_sl, oca)))
        return prikazy

    def _place_part(
        self, flow: Flow, part: str, quantity: int, prikazy: list[tuple[str, Any]] | None = None
    ) -> str:
        """
        Zadá prodejní příkazy části do TWS a vrátí jejich slovní popis.
        Dřívější záznamy ve slotech části se nahrazují. Předem sestavené
        příkazy lze předat v prikazy - to využívá dělení pozice na runner,
        kde se musí sestavit dřív, než se zmenší hlavní zajištění.
        """
        if prikazy is None:
            prikazy = self._build_part_orders(flow, part, quantity)
        self._clear_part(flow, part)
        for which, order in prikazy:
            self._set_leg(flow, part, which, self.ib.place(flow.option_contract, order))
        return self._part_description(flow, part)

    def _part_description(self, flow: Flow, part: str) -> str:
        """Popis zadaných příkazů části pro stav obchodu a log."""
        pt_level, sl_level = self._part_levels(flow, part)
        if not flow.exit_split:
            trade = self._leg(flow, part, "pt")
            typ = "MKT"
            if trade is not None and trade.order.orderType == "LMT":
                typ = f"LMT {trade.order.lmtPrice:g}"
            return (
                f"příkaz {typ} s podmínkami PT {flow.level_text('pt', pt_level)} "
                f"/ SL {flow.level_text('sl', sl_level)}."
            )

        if flow.pt_on_underlying:
            popis_pt = f"PT podmínkou na podkladu {flow.level_text('pt', pt_level)}"
        else:
            popis_pt = f"PT limitem na opci {flow.level_text('pt', pt_level)}"
        if flow.sl_on_underlying:
            popis_sl = f"SL podmínkou na podkladu {flow.level_text('sl', sl_level)}"
        else:
            popis_sl = f"SL stop-marketem na opci {flow.level_text('sl', sl_level)}"
        return f"dva příkazy (OCA) - {popis_pt}, {popis_sl}."

    def _modify_part_levels(self, flow: Flow, part: str, which: str | None = None) -> bool:
        """
        Promítne aktuální úrovně části do jejích běžících příkazů.

        which = 'pt' nebo 'sl' upraví jen příslušný příkaz, None oba; jediný
        společný podmíněný příkaz se upravuje vždy celý. Vrací False, pokud
        některý příkaz části nelze v TWS upravit - pak se nemění nic.
        """
        if not self._part_modifiable(flow, part):
            return False

        pt_level, sl_level = self._part_levels(flow, part)
        _, pt_more, sl_more = calc.condition_directions(flow.right)
        conid = flow.underlying_conid

        if not flow.exit_split:
            trade = self._leg(flow, part, "pt")
            order = trade.order
            order.conditions = self.ib.prepare_conditions(
                [
                    self.ib.price_condition(conid, pt_more, pt_level),
                    self.ib.price_condition(conid, sl_more, sl_level),
                ]
            )
            # Odeslání příkazu se stejným orderId znamená jeho modifikaci
            self._set_leg(flow, part, "pt", self.ib.place(flow.option_contract, order))
            return True

        if which in (None, "pt"):
            trade = self._leg(flow, part, "pt")
            order = trade.order
            if flow.pt_on_underlying:
                order.conditions = self.ib.prepare_conditions(
                    [self.ib.price_condition(conid, pt_more, pt_level)]
                )
            else:
                order.lmtPrice = calc.option_profit_limit(flow.fill_price, pt_level, flow.min_tick)
            self._set_leg(flow, part, "pt", self.ib.place(flow.option_contract, order))

        if which in (None, "sl"):
            trade = self._leg(flow, part, "sl")
            order = trade.order
            if flow.sl_on_underlying:
                order.conditions = self.ib.prepare_conditions(
                    [self.ib.price_condition(conid, sl_more, sl_level)]
                )
            else:
                order.auxPrice = calc.option_loss_stop(flow.fill_price, sl_level, flow.min_tick)
            self._set_leg(flow, part, "sl", self.ib.place(flow.option_contract, order))
        return True

    def _resize_part(self, flow: Flow, part: str, quantity: int) -> bool:
        """
        Nastaví, kolik kusů má část ještě prodat (quantity = zbývající kusy).

        Každému příkazu se množství zvyšuje o jeho vlastní vyplnění, protože
        TWS bere totalQuantity včetně už prodaných kusů. Po částečném prodeji
        na jednom příkazu dvojice (druhý mezitím TWS přes OCA skupinu zmenšila)
        by se jinak prodalo víc kusů, než pozice drží. Vrací False, pokud
        některý příkaz upravit nelze - pak se nemění žádný.
        """
        if not self._part_modifiable(flow, part):
            return False
        for which in ("pt", "sl"):
            trade = self._leg(flow, part, which)
            if trade is None:
                continue
            order = trade.order
            order.totalQuantity = quantity + int(trade.orderStatus.filled or 0)
            self._set_leg(flow, part, which, self.ib.place(flow.option_contract, order))
        return True

    def _part_remaining(self, flow: Flow, part: str) -> int:
        """Kolik kusů mají běžící příkazy části ještě prodat (bez už vyplněných)."""
        zbyva = 0
        for trade in self._legs(flow, part):
            zbyva = max(
                zbyva, int(trade.order.totalQuantity) - int(trade.orderStatus.filled or 0)
            )
        return zbyva

    async def change_profit_target(self, flow_id: str, novy_pt: float) -> Flow:
        """
        Změní cílovou úroveň běžícího obchodu.

        Po nákupu se nová úroveň promítne do zajišťovacího příkazu; strike
        se měnit nedá, opce je už koupená. Před nákupem záleží na nastavení
        trading.pt_change_strike: buď se ponechá původní strike, nebo se
        podle nového PT vybere jiný kontrakt a příkaz se přezadá.
        """
        flow = self.flows.get(flow_id)
        if flow is None:
            raise ValueError(f"Flow '{flow_id}' neexistuje.")
        if not flow.state.is_active:
            raise ValueError("Cíl lze měnit jen u běžícího obchodu.")

        # Po prodeji hlavní části (nebo během jejího uzavírání) už cíl nemá
        # co řídit; úprava by se navíc pokusila přidat podmínky do tržního
        # prodejního příkazu
        if (
            flow.state == FlowState.CLOSING
            or flow.main_close_requested
            or flow.exit_fill_price is not None
        ):
            raise ValueError(
                "Hlavní část pozice se uzavírá nebo už je prodaná - její cíl nelze měnit."
            )

        # Základ pro násobky musí být znám, jinak by se cíl při každé změně
        # počítal z už posunuté hodnoty a rostl by bez omezení
        if not flow.original_profit_target:
            flow.original_profit_target = flow.profit_target

        # Pojistka proti zjevně chybné hodnotě: cíl nesmí být dál než
        # dvacetinásobek původní vzdálenosti od vstupu
        puvodni_vzdalenost = flow.pt_distance(flow.original_profit_target)
        if puvodni_vzdalenost > 0:
            nova_vzdalenost = flow.pt_distance(novy_pt)
            if nova_vzdalenost > puvodni_vzdalenost * 20:
                raise ValueError(
                    f"Cíl {novy_pt:g} je nesmyslně daleko od vstupu {flow.entry_price:g} "
                    f"(původní cíl {flow.original_profit_target:g})."
                )

        # Nový cíl musí zůstat na správné straně vstupu, jinak by obchod
        # ztratil smysl; zisk na opci musí zůstat kladný
        self._check_profit_target(
            flow.right, flow.entry_price, novy_pt, flow.pt_on_underlying
        )

        async with self._lock:
            puvodni = flow.profit_target
            flow.profit_target = novy_pt

            if flow.state.is_before_entry:
                await self._apply_pt_before_entry(flow)
            elif flow.exit_trade is not None:
                self._update_exit_levels(flow, "pt")

            nasobek = flow.pt_multiple
            popis = f" ({nasobek:g}× původní cíl)" if nasobek else ""
            self.log_event(
                f"{flow.id}: cíl změněn z {flow.level_text('pt', puvodni)} "
                f"na {flow.level_text('pt', novy_pt)}{popis}."
            )
            self._notify()
            return flow

    async def _apply_pt_before_entry(self, flow: Flow) -> None:
        """
        Promítne nový cíl do obchodu, který ještě nenakoupil.
        Podle konfigurace buď ponechá strike, nebo vybere nový kontrakt.
        """
        if self.cfg.trading.pt_change_strike != "recalculate":
            return

        # Cílová úroveň pro strike: PT na podkladu přímo, PT na opci se
        # přepočítá z aktuální ceny držené opce stejně jako při přípravě zadání
        if flow.pt_on_underlying:
            cil = flow.profit_target
        else:
            _, _, delta = self.ib.option_quotes(flow.option_contract)
            cena, zdroj = self.ib.option_price(flow.option_contract)
            cil, _ = self._level_from_option_profit(
                cena,
                self.ib.underlying_price(flow.underlying_contract),
                flow.entry_price,
                flow.profit_target,
                flow.strike,
                flow.expiration,
                flow.right,
                delta,
                zdroj,
            )

        chain = await self.ib.option_chain(flow.underlying_contract)
        try:
            novy_strike, option, details = await self._qualify_nearest_option(
                flow.symbol,
                flow.expiration,
                list(chain.strikes),
                cil,
                flow.right,
                chain.tradingClass,
            )
        except ValueError as exc:
            # Bez obchodovatelného strike zůstává původní kontrakt v trhu
            self.log_event(f"{flow.id}: strike nelze přepočítat - {exc}")
            return
        if novy_strike == flow.strike:
            return

        # Příkaz na původní kontrakt už neplatí, musí z trhu pryč
        self.ib.cancel(flow.entry_trade)
        self.ib.unsubscribe(flow.option_contract)

        flow.option_contract = option
        flow.option_conid = option.conId
        flow.strike = novy_strike
        flow.min_tick = details.minTick or flow.min_tick
        flow.entry_trade = None
        flow.entry_order_id = None
        self.ib.subscribe(option)

        self.log_event(f"{flow.id}: strike přepočítán na {novy_strike:g}, příkaz se zadá znovu.")
        self._place_entry(flow)

    def _part_status_text(self, flow: Flow, part: str) -> str:
        """Stavy příkazů části pro hlášky, například 'Submitted/PendingCancel'."""
        legy = self._legs(flow, part)
        if not legy:
            return "chybí"
        return "/".join(trade.orderStatus.status for trade in legy)

    def _update_exit_levels(self, flow: Flow, which: str | None = None) -> None:
        """
        Promítne aktuální PT a SL do zajišťovacích příkazů hlavní části.
        Nelze-li příkazy upravit, změna platí jen v přehledu a zaloguje se.
        """
        if not self._modify_part_levels(flow, "exit", which):
            self.log_event(
                f"{flow.id}: zajišťovací příkaz nelze upravit "
                f"({self._part_status_text(flow, 'exit')}) - změna platí jen v přehledu."
            )
            return
        flow.touch(
            f"Zajišťovací příkaz upraven na PT {flow.level_text('pt')} "
            f"/ SL {flow.level_text('sl')}."
        )

    async def set_runner(self, flow_id: str, multiple: float) -> Flow:
        """
        Zapne runner, nebo změní jeho cíl.

        Runner je část pozice (počet kusů podle trading.runner_quantity),
        která se prodává samostatným příkazem s vlastním cílem; SL sdílí
        se zbytkem pozice. Cíl runneru se zadává jako násobek původní
        vzdálenosti PT od vstupu.

        Před nákupem se volba jen zapamatuje a zajišťovací příkazy se po
        nákupu založí rozdělené. Za běhu se stávající prodejní příkaz zmenší
        a přidá se příkaz runneru; při změně cíle už běžícího runneru se
        pouze upraví jeho podmínky.
        """
        flow = self.flows.get(flow_id)
        if flow is None:
            raise ValueError(f"Flow '{flow_id}' neexistuje.")
        if not flow.state.is_active or flow.state == FlowState.CLOSING:
            raise ValueError("Runner lze měnit jen u běžícího obchodu.")
        if flow.runner_fill_price is not None:
            raise ValueError("Runner už byl prodán, jeho cíl nelze měnit.")
        if flow.runner_close_requested:
            raise ValueError("Runner se právě uzavírá trhem, jeho cíl nelze měnit.")
        if not flow.runner_active and flow.main_close_requested:
            raise ValueError("Probíhá uzavírání pozice, runner teď nelze zapnout.")

        runner_q = flow.runner_quantity if flow.runner_active else self.cfg.trading.runner_quantity
        # Rozhoduje skutečně držené množství - dříve prodané runnery se odečítají
        total = flow.held_quantity
        if total <= runner_q:
            raise ValueError(
                f"Runner ({runner_q} ks) vyžaduje obchod s větším množstvím než {runner_q} kontrakt(y)."
            )

        if not flow.original_profit_target:
            flow.original_profit_target = flow.profit_target
        novy_pt = flow.scaled_target(multiple)

        # Cíl runneru musí ležet na stejné straně vstupu jako hlavní cíl;
        # zisk na opci musí být kladný
        self._check_profit_target(
            flow.right, flow.entry_price, novy_pt, flow.pt_on_underlying, "cíl runneru"
        )

        async with self._lock:
            byl_aktivni = flow.runner_active
            # Dosavadní nastavení pro případ, že se runner nepodaří oddělit
            puvodni = (flow.runner_profit_target, flow.runner_quantity, flow.runner_stop_loss)
            flow.runner_profit_target = novy_pt
            flow.runner_quantity = runner_q
            # Nově zapnutý runner přebírá aktuální SL obchodu;
            # dál se jeho stop přepíná nezávisle na hlavní části
            if not byl_aktivni:
                flow.runner_stop_loss = flow.stop_loss

            if flow.state == FlowState.EXIT_ARMED:
                if byl_aktivni and flow.runner_trade is not None:
                    self._update_runner_levels(flow, "pt")
                else:
                    try:
                        self._split_exit_for_runner(flow)
                    except Exception:
                        # Bez příkazů v trhu by runner jen držel kusy hlavní
                        # části mimo zajištění - zadání se proto vrací zpět
                        (
                            flow.runner_profit_target,
                            flow.runner_quantity,
                            flow.runner_stop_loss,
                        ) = puvodni
                        raise

            self.log_event(
                f"{flow.id}: runner {runner_q} ks s cílem {flow.level_text('pt', novy_pt)} "
                f"({multiple:g}× původní cíl)."
            )
            self._notify()
            return flow

    async def cancel_runner(self, flow_id: str) -> Flow:
        """
        Vypne runner - jeho prodejní příkaz se zruší a hlavní příkaz se
        rozšíří zpět na celou pozici, takže platí jeden PT a SL pro všechno.
        """
        flow = self.flows.get(flow_id)
        if flow is None:
            raise ValueError(f"Flow '{flow_id}' neexistuje.")
        if not flow.runner_active:
            raise ValueError("Obchod nemá aktivní runner.")
        if flow.runner_fill_price is not None:
            raise ValueError("Runner už byl prodán, není co rušit.")
        if flow.runner_close_requested:
            raise ValueError("Runner se právě uzavírá trhem, není co rušit.")
        if flow.main_close_requested or flow.exit_fill_price is not None:
            raise ValueError(
                "Hlavní část pozice se uzavírá nebo je prodaná - runner už lze "
                "jen uzavřít trhem, nebo nechat doběhnout."
            )

        async with self._lock:
            # Nejprve se ruší příkazy runneru, teprve pak se navyšuje hlavní -
            # obráceně by na okamžik bylo v trhu více kusů, než pozice drží
            self._cancel_part(flow, "runner")
            self._clear_part(flow, "runner")
            flow.runner_profit_target = None
            flow.runner_quantity = 0
            flow.runner_stop_loss = None

            if flow.state == FlowState.EXIT_ARMED and flow.exit_trade is not None:
                # Prodává se jen dosud neprodaný zbytek - část hlavních příkazů
                # se mohla vyplnit ještě před sloučením
                self._sync_part_fills(flow, "exit")
                total = flow.held_quantity - flow.main_sold_quantity
                if self._resize_part(flow, "exit", total):
                    flow.touch(f"Runner zrušen, prodejní příkaz rozšířen na {total} ks.")
                else:
                    flow.touch(
                        "Runner zrušen, ale hlavní příkaz nelze upravit - "
                        "množství se dorovná, jakmile to TWS dovolí."
                    )

            self.log_event(f"{flow.id}: runner zrušen, platí jeden PT a SL pro celou pozici.")
            self._notify()
            return flow

    async def close_main(self, flow_id: str) -> Flow:
        """
        Prodá trhem hlavní část pozice; bez runneru celou pozici.

        Podmíněný zajišťovací příkaz se nejprve zruší a tržní prodej se zadá
        až po potvrzení zrušení - jinak by se na okamžik prodávalo více kusů,
        než pozice drží. Případný runner běží dál se svým cílem.
        """
        flow = self.flows.get(flow_id)
        if flow is None:
            raise ValueError(f"Flow '{flow_id}' neexistuje.")
        if flow.state != FlowState.EXIT_ARMED or flow.fill_price is None:
            raise ValueError("Uzavřít lze jen nakoupenou pozici se zadaným zajištěním.")
        if flow.exit_fill_price is not None:
            raise ValueError("Hlavní část pozice už je prodaná.")
        if flow.main_close_requested:
            raise ValueError("Uzavření pozice už probíhá.")

        async with self._lock:
            flow.main_close_requested = True
            self._cancel_part(flow, "exit")
            popis = "hlavní část pozice" if flow.runner_active else "pozici"
            flow.touch(f"Uzavírám {popis} trhem ({flow.main_quantity} ks).")
            self.log_event(f"{flow.id}: {flow.message}")
            self._notify()
            return flow

    async def close_runner(self, flow_id: str) -> Flow:
        """
        Prodá trhem runner; hlavní část pozice běží dál se svým PT a SL.
        Postup je stejný jako u hlavní části - nejprve zrušení podmíněného
        příkazu, tržní prodej až po jeho potvrzení.
        """
        flow = self.flows.get(flow_id)
        if flow is None:
            raise ValueError(f"Flow '{flow_id}' neexistuje.")
        if not flow.runner_active or flow.state != FlowState.EXIT_ARMED:
            raise ValueError("Obchod nemá běžící runner, který by šlo uzavřít.")
        if flow.runner_fill_price is not None:
            raise ValueError("Runner už je prodaný.")
        if flow.runner_close_requested:
            raise ValueError("Uzavření runneru už probíhá.")

        async with self._lock:
            flow.runner_close_requested = True
            self._cancel_part(flow, "runner")
            flow.touch(f"Uzavírám runner trhem ({flow.runner_quantity} ks).")
            self.log_event(f"{flow.id}: {flow.message}")
            self._notify()
            return flow

    def _resolve_sl(self, flow: Flow, rezim: str) -> float:
        """
        Převede režim tlačítka na úroveň SL.
        'puvodni' vrací SL ze zadání obchodu, 'be' vstupní cenu (break even).
        """
        if rezim == "be":
            # Break even: na podkladu vstupní cena, na opci nulová ztráta
            return flow.break_even_sl
        if rezim == "puvodni":
            # Obchod ze starší verze počáteční SL nezná - stane se jím aktuální
            if not flow.original_sl_known:
                flow.original_stop_loss = flow.stop_loss
            return flow.original_stop_loss
        raise ValueError(f"Neznámý režim SL '{rezim}'.")

    def _sl_breached(self, flow: Flow, sl: float) -> bool:
        """
        True, pokud je trh už na úrovni SL, nebo za ní.
        SL na podkladu se měří cenou podkladu, SL na opci BIDem opce proti
        stop ceně odvozené z nákupní ceny.
        """
        if not flow.sl_on_underlying:
            if flow.fill_price is None:
                return False
            # Rozhoduje tatáž cena, na které stojí stop příkaz - zaokrouhlená
            # na tik a nejvýše o celou prémii pod nákupní cenou
            stop = calc.option_loss_stop(flow.fill_price, sl, flow.min_tick)
            bid, _, _ = self.ib.option_quotes(flow.option_contract)
            if bid is None:
                bid = flow.option_bid
            if bid is None:
                return False
            return bid <= stop

        cena = self.ib.underlying_price(flow.underlying_contract)
        if cena is None:
            cena = flow.underlying_price
        if cena is None:
            return False
        # U CALL chrání stop zdola, u PUT shora
        if flow.right == "C":
            return cena <= sl
        return cena >= sl

    async def set_stop_loss(self, flow_id: str, rezim: str) -> Flow:
        """
        Přepne SL hlavní části na počáteční hodnotu ('puvodni'),
        nebo na vstupní cenu podkladu ('be', break even).

        Má smysl až u nakoupené pozice se zadaným zajištěním. Je-li cena
        podkladu už na zvolené úrovni nebo za ní, podmíněný příkaz se zruší
        a hlavní část se rovnou prodá trhem - čekat na podmínku by nemělo smysl.
        """
        flow = self.flows.get(flow_id)
        if flow is None:
            raise ValueError(f"Flow '{flow_id}' neexistuje.")
        if flow.state != FlowState.EXIT_ARMED or flow.fill_price is None:
            raise ValueError("SL lze přepínat jen u nakoupené pozice se zadaným zajištěním.")
        if flow.exit_fill_price is not None or flow.main_close_requested:
            raise ValueError(
                "Hlavní část pozice se uzavírá nebo už je prodaná - její SL nelze měnit."
            )

        novy_sl = self._resolve_sl(flow, rezim)

        async with self._lock:
            flow.stop_loss = novy_sl
            if self._sl_breached(flow, novy_sl):
                # Úroveň je už proražená - stejný postup jako Uzavřít pozici:
                # tržní prodej zadá smyčka až po potvrzení zrušení příkazů
                flow.main_close_requested = True
                self._cancel_part(flow, "exit")
                flow.touch(
                    f"SL {flow.level_text('sl')} je již dosažen, hlavní část "
                    f"({flow.main_quantity} ks) se prodává trhem."
                )
                self.log_event(f"{flow.id}: {flow.message}")
            else:
                self._update_exit_levels(flow, "sl")
                popis = "break even" if rezim == "be" else "počáteční hodnota"
                self.log_event(
                    f"{flow.id}: SL hlavní části nastaven na {flow.level_text('sl')} ({popis})."
                )
            self._notify()
            return flow

    async def set_runner_stop_loss(self, flow_id: str, rezim: str) -> Flow:
        """
        Přepne SL runneru na počáteční hodnotu ('puvodni'), nebo na vstupní
        cenu podkladu ('be'). Chová se stejně jako přepnutí SL hlavní části,
        jen se týká výhradně příkazu runneru.
        """
        flow = self.flows.get(flow_id)
        if flow is None:
            raise ValueError(f"Flow '{flow_id}' neexistuje.")
        if (
            not flow.runner_active
            or flow.state != FlowState.EXIT_ARMED
            or flow.runner_order_id is None
        ):
            raise ValueError("Obchod nemá běžící runner, jehož SL by šlo přepínat.")
        if flow.runner_fill_price is not None:
            raise ValueError("Runner už je prodaný, jeho SL nelze měnit.")
        if flow.runner_close_requested:
            raise ValueError("Runner se právě uzavírá trhem, jeho SL nelze měnit.")

        novy_sl = self._resolve_sl(flow, rezim)

        async with self._lock:
            flow.runner_stop_loss = novy_sl
            if self._sl_breached(flow, novy_sl):
                # Proražená úroveň - runner se prodá trhem, hlavní část běží dál
                flow.runner_close_requested = True
                self._cancel_part(flow, "runner")
                flow.touch(
                    f"SL runneru {flow.level_text('sl', novy_sl)} je již dosažen, "
                    f"runner ({flow.runner_quantity} ks) se prodává trhem."
                )
                self.log_event(f"{flow.id}: {flow.message}")
            else:
                self._update_runner_levels(flow, "sl")
                popis = "break even" if rezim == "be" else "počáteční hodnota"
                self.log_event(
                    f"{flow.id}: SL runneru nastaven na "
                    f"{flow.level_text('sl', novy_sl)} ({popis})."
                )
            self._notify()
            return flow

    def _split_exit_for_runner(self, flow: Flow) -> None:
        """
        Rozdělí běžící zajištění na hlavní část a runner.

        Příkazy runneru se sestaví jako první: selhalo-li by sestavení (chybí
        cena opce pro úroveň na opci), zůstane hlavní zajištění nedotčené.
        Teprve pak se hlavní příkazy zmenší a příkazy runneru se zadají -
        v opačném pořadí by v trhu na okamžik bylo víc kusů, než pozice drží.
        """
        if not self._part_modifiable(flow, "exit"):
            raise ValueError(
                "Zajišťovací příkaz nelze upravit "
                f"({self._part_status_text(flow, 'exit')}) - runner teď nelze zapnout."
            )

        # Do dělení jdou jen dosud neprodané kusy
        self._sync_part_fills(flow, "exit")
        total = flow.held_quantity - flow.main_sold_quantity
        prikazy = self._build_part_orders(flow, "runner", flow.runner_quantity)
        self._resize_part(flow, "exit", total - flow.runner_quantity)
        try:
            self._place_part(flow, "runner", flow.runner_quantity, prikazy)
        except Exception:
            # Zadání příkazů runneru selhalo - hlavní zajištění se vrací
            # na celou pozici, aby žádné kusy nezůstaly nekryté
            self._resize_part(flow, "exit", total)
            raise

    def _update_runner_levels(self, flow: Flow, which: str | None = None) -> None:
        """Promítne cíl či SL runneru do jeho běžících příkazů; jinak vyhodí chybu."""
        if not self._modify_part_levels(flow, "runner", which):
            raise ValueError(
                f"Příkaz runneru nelze upravit ({self._part_status_text(flow, 'runner')})."
            )

    async def cancel_flow(self, flow_id: str, close_position: bool = False) -> None:
        """
        Zruší flow podle identifikátoru včetně jeho příkazů v TWS.
        S close_position se navíc uzavře držená pozice tržním příkazem.
        """
        flow = self.flows.get(flow_id)
        if flow is None:
            raise ValueError(f"Flow '{flow_id}' neexistuje.")
        await self._cancel(flow, close_position)

    async def cancel_by_symbol(
        self, symbol: str, close_position: bool = False, right: str | None = None
    ) -> Flow:
        """
        Zruší aktivní flow podle tickeru, volitelně jen daného směru (C/P).
        Běží-li na tickeru long i short a směr není určen, výběr je
        nejednoznačný a rušení se odmítne.
        """
        flows = self.active_flows_for(symbol)
        if right is not None:
            flows = [flow for flow in flows if flow.right == right]
        if not flows:
            raise ValueError(f"Pro ticker {symbol.upper().strip()} neběží žádné aktivní flow.")
        if len(flows) > 1:
            raise ValueError(
                f"Na tickeru {symbol.upper().strip()} běží long i short obchod - "
                f"zrušte jej tlačítkem v jeho řádku přehledu."
            )
        flow = flows[0]
        await self._cancel(flow, close_position)
        return flow

    async def _cancel(
        self, flow: Flow, close_position: bool = False, reason: str | None = None
    ) -> None:
        """
        Zruší příkazy flow a ukončí jej.

        Drží-li obchod pozici, rozhoduje close_position: buď se pozice uzavře
        tržním příkazem, nebo zůstane otevřená a bez zajištění k ručnímu řízení.
        Volitelný reason nahradí výchozí text zprávy (např. automatické uzavření).
        """
        async with self._lock:
            self._cancel_locked(flow, close_position, reason)

    def _cancel_locked(
        self, flow: Flow, close_position: bool = False, reason: str | None = None
    ) -> None:
        """Tělo rušení flow - volá se výhradně s již drženým zámkem."""
        self.ib.cancel(flow.entry_trade)
        self._cancel_part(flow, "exit")
        self._cancel_part(flow, "runner")

        v_pozici = flow.fill_price is not None

        if v_pozici and close_position:
            # Už vyplněný prodej hlavní části nesmí zůstat jako exit_trade -
            # smyčka by jeho staré vyplnění vzala za dokončené uzavření
            # a zbylé kusy (runner) by se nikdy neprodaly
            if flow.exit_fill_price is not None:
                self._clear_part(flow, "exit")
                flow.exit_market_sent = None
            # Prodejní příkaz se zadá až po zrušení nákupního, protože TWS
            # nepovolí oba příkazy na jednom kontraktu současně
            flow.set_state(
                FlowState.CLOSING, reason or "Zrušeno obchodníkem, pozice se uzavírá trhem."
            )
        elif v_pozici:
            flow.set_state(
                FlowState.CANCELLED,
                "Flow zrušeno. POZOR: pozice zůstává otevřená v TWS bez zajištění - "
                "prodejní příkaz byl zrušen, uzavřete ji ručně.",
            )
            self._release(flow)
        else:
            flow.set_state(
                FlowState.CANCELLED,
                reason or "Flow zrušeno před nákupem, příkaz odstraněn z trhu.",
            )
            self._release(flow)

        self.log_event(f"{flow.id}: {flow.message}")
        self._notify()

    def _release(self, flow: Flow) -> None:
        """Uvolní odběry tržních dat držené ukončeným flow."""
        self.ib.unsubscribe(flow.underlying_contract)
        self.ib.unsubscribe(flow.option_contract)
        flow.underlying_contract = None
        flow.option_contract = None
        # Ukončený obchod nemá otevřené kusy - odhady na PT/SL ztrácejí smysl
        # a v přehledu by jinak visela poslední spočtená hodnota
        flow.expected_profit = None
        flow.expected_loss = None

    def remove_flow(self, flow_id: str) -> None:
        """Odstraní ukončené flow z přehledu."""
        flow = self.flows.get(flow_id)
        if flow is None:
            return
        if flow.state.is_active:
            raise ValueError("Aktivní flow nelze odstranit, nejprve jej zrušte.")
        self.flows.pop(flow_id, None)
        self._notify()

    # ------------------------------------------------------------------
    # Monitorovací smyčka
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spustí periodickou monitorovací smyčku."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Zastaví monitorovací smyčku."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        """Hlavní smyčka - periodicky prochází aktivní flow a hlídá spojení."""
        while True:
            try:
                await self._tick()
                self._last_tick = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Chyba v monitorovací smyčce.")
            await asyncio.sleep(self.cfg.engine.poll_interval_sec)

    def _exchange_now(self) -> datetime:
        """Aktuální čas v časové zóně burzy (řeší letní/zimní čas)."""
        return datetime.now(ZoneInfo(self.cfg.trading.exchange_timezone))

    def _exchange_close(self, ted: datetime) -> datetime:
        """Dnešní čas zavření burzy v její časové zóně."""
        hodina, minuta = (
            int(cast) for cast in self.cfg.trading.exchange_close_time.split(":")
        )
        return ted.replace(hour=hodina, minute=minuta, second=0, microsecond=0)

    def auto_close_seconds(self) -> float | None:
        """
        Počet sekund do začátku automatického uzavírání obchodů.

        None znamená, že se dnes už neuzavírá (funkce vypnutá, víkend, nebo
        burza už zavřela); nula znamená, že uzavírací okno právě běží.
        """
        if not self.cfg.trading.auto_close_enabled:
            return None

        ted = self._exchange_now()
        # O víkendu se neobchoduje - odpočet nemá co měřit
        if ted.weekday() >= 5:
            return None

        zavirani = self._exchange_close(ted)
        start = zavirani - timedelta(minutes=self.cfg.trading.auto_close_minutes_before)
        if ted >= zavirani:
            return None
        if ted >= start:
            return 0.0
        return (start - ted).total_seconds()

    async def _auto_close_flows(self) -> None:
        """
        Krátce před zavřením burzy uzavře všechny běžící obchody.

        Čas se počítá v časové zóně burzy, takže posun letního a zimního času
        vůči místnímu času počítače nehraje roli. Čekající obchody se ruší
        (nákupní příkaz se odstraní z trhu), obchody s pozicí se prodají trhem.
        """
        if self.auto_close_seconds() != 0:
            return

        minuty = self.cfg.trading.auto_close_minutes_before
        for flow in list(self.flows.values()):
            if not flow.state.is_active or flow.state == FlowState.CLOSING:
                continue
            if flow.state.is_before_entry:
                self.log_event(
                    f"{flow.id}: automatické zrušení čekajícího obchodu "
                    f"({minuty:g} min před zavřením burzy)."
                )
                await self._cancel(
                    flow,
                    reason="Automaticky zrušeno před koncem obchodování, "
                    "příkaz odstraněn z trhu.",
                )
            else:
                self.log_event(
                    f"{flow.id}: automatické uzavření pozice "
                    f"({minuty:g} min před zavřením burzy)."
                )
                await self._cancel(
                    flow,
                    close_position=True,
                    reason="Automaticky uzavíráno před koncem obchodování, "
                    "pozice se prodává trhem.",
                )

    async def _tick(self) -> None:
        """Jeden průchod monitoringem všech aktivních flow."""
        # Během obnovy se nemonitoruje - příkazy z minulého spojení nejsou platné
        if self._restore_lock.locked():
            return

        if not self.ib.connected:
            # Po obnovení spojení se obchody musí znovu spárovat s příkazy v TWS
            self._synced = False
            # Automatické připojení jen pokud je povoleno konfigurací i uživatelem
            if self.cfg.connection.auto_reconnect and self.auto_connect:
                await self._try_reconnect()
            return

        # Velikost účtu z TWS se obnovuje, jen když ji konfigurace přebírá (size = 0)
        await self._refresh_account_size()

        # Pozice bez dozoru aplikace se kontrolují v delším intervalu
        await self._check_unmanaged()

        # Krátce před zavřením burzy se běžící obchody automaticky uzavírají
        await self._auto_close_flows()

        changed = False
        for flow in list(self.flows.values()):
            if not flow.state.is_active:
                continue
            try:
                changed |= await self._monitor(flow)
            except Exception as exc:
                log.exception("Chyba při monitoringu flow %s.", flow.id)
                flow.set_state(FlowState.ERROR, f"Chyba monitoringu: {exc}")
                changed = True

        if changed:
            self._notify()

    async def restore(self) -> None:
        """
        Obnoví obchody z uloženého stavu a srovná je se skutečností v TWS.

        Uložený soubor říká, jaké obchody aplikace vedla; závazné jsou ale
        příkazy a pozice v TWS. Obchod, jehož příkazy v TWS nejsou, se proto
        označí jako vyžadující pozornost, a naopak stav vyplněných příkazů
        se převezme z TWS.
        """
        async with self._restore_lock:
            await self._restore_locked()

    async def _restore_locked(self) -> None:
        """Vlastní obnova; volá se pod zámkem, aby neběžela souběžně se smyčkou."""
        if self._synced:
            return
        self._synced = True

        prikazy = await self.ib.app_trades()
        pozice = await self.ib.positions()

        # Ze souboru se čte jen při prvním spuštění; při dalším připojení
        # je stav v paměti aktuálnější než ten uložený
        ulozene: list[Flow] = []
        if not self._restored:
            self._restored = True
            if self.cfg.state.enabled:
                ulozene = store.load(self.cfg.state.file)
                if ulozene:
                    self.log_event(
                        f"Obnovuji {len(ulozene)} uložených obchodů a ověřuji je v TWS."
                    )
        else:
            self.log_event("Spojení navázáno, ověřuji stav obchodů v TWS.")

        # Obchody z paměti se po obnově spojení musí znovu spárovat s příkazy
        # v TWS - objekty z minulého spojení už nejsou platné
        k_overeni = ulozene + [
            flow for flow in self.flows.values() if flow.state.is_active and flow not in ulozene
        ]

        for flow in k_overeni:
            # Ukončené obchody se jen vrátí do přehledu, nic se u nich neověřuje
            if not flow.state.is_active:
                self.flows[flow.id] = flow
                continue
            try:
                await self._restore_flow(flow, prikazy, pozice)
            except Exception as exc:
                log.exception("Obchod %s se nepodařilo obnovit.", flow.id)
                flow.set_state(FlowState.ERROR, f"Obnova obchodu selhala: {exc}")
                # Chyba musí být vidět i v průběhu, jinak obchod tiše zůstane
                # ve stavu, který neodpovídá skutečnosti v TWS
                self.log_event(f"{flow.id}: obnova selhala - {exc}")
            self.flows[flow.id] = flow

        # Příkazy se značkou aplikace, které v uloženém stavu nejsou,
        # se dohledají přímo v TWS - záchrana pro případ ztráty souboru
        await self._adopt_orphans(prikazy, pozice)

        # Pozice, ke kterým se nepodařilo přiřadit obchod, jsou bez dozoru aplikace
        self._warn_unmanaged(pozice)

        # Číslování dalších obchodů musí navázat za obnovené záznamy
        nejvyssi = 0
        for flow in self.flows.values():
            cast = flow.id.rsplit("-", 1)[-1]
            if cast.isdigit():
                nejvyssi = max(nejvyssi, int(cast))
        self._ids = itertools.count(nejvyssi + 1)

        # Uložený stav se přepisuje jen tehdy, když se skutečně něco obnovilo
        if self.flows:
            self._notify()

    def _warn_unmanaged(self, pozice: dict) -> None:
        """
        Upozorní na opční pozice na účtu, které aplikace neřídí.

        Nastává, když se ztratí uložený stav a pozice už byla nakoupena -
        vyplněné příkazy TWS vrací bez značky v orderRef, takže je nelze
        k obchodu přiřadit. Aplikace k nim proto sama nic nezadává, protože
        nezná původní PT ani SL, a nechává rozhodnutí na obchodníkovi.
        """
        rizene = {
            flow.option_conid
            for flow in self.flows.values()
            if flow.state.is_active and flow.option_conid
        }
        for conid, info in pozice.items():
            if conid in rizene:
                continue
            self.unmanaged[conid] = info
            self.log_event(f"POZOR: {self.unmanaged_text(info)}")

    async def _adopt_orphans(self, prikazy: dict, pozice: dict) -> None:
        """
        Dohledá příkazy označené značkou aplikace, ke kterým chybí uložený obchod.

        Nastává, když se soubor se stavem ztratí nebo poškodí. Obchod se sestaví
        z parametrů příkazu: vstupní cena z jeho cenové podmínky, PT a SL
        z podmínek prodejního příkazu. Není-li prodejní příkaz k dispozici,
        odvodí se PT ze strike a SL z poměru v konfiguraci - proto se takový
        obchod označí jako dopočítaný a je vhodné jej zkontrolovat.
        """
        for ref, trade in prikazy.items():
            rozklad = parse_order_ref(ref)
            if rozklad is None:
                continue
            flow_id, druh = rozklad
            # Obchod už je obnovený z uloženého stavu, nebo jde o výstupní
            # příkaz, který se dohledá spolu se svým obchodem
            if flow_id in self.flows or druh != "entry":
                continue
            # Zrušený příkaz bez pozice už není co přebírat; vyplněný ano,
            # protože k němu může být otevřená pozice bez zajištění
            if trade.orderStatus.status in DEAD_ORDER_STATES:
                continue
            if trade.orderStatus.filled <= 0 and trade.orderStatus.status == "Filled":
                continue

            try:
                flow = await self._flow_from_trade(flow_id, trade, prikazy, pozice)
            except Exception as exc:
                log.exception("Osiřelý příkaz %s se nepodařilo převzít.", ref)
                self.log_event(f"Příkaz {ref} se nepodařilo převzít: {exc}")
                continue

            self.flows[flow.id] = flow
            self.log_event(
                f"{flow.id}: převzat příkaz nalezený v TWS ({flow.option_label()}) - "
                f"{flow.state.label}. {flow.message}"
            )

    async def _flow_from_trade(
        self, flow_id: str, trade: Any, prikazy: dict, pozice: dict
    ) -> Flow:
        """Sestaví obchod z příkazu nalezeného v TWS."""
        kontrakt = trade.contract
        podminky = trade.order.conditions
        if not podminky:
            raise ValueError("příkaz nemá cenovou podmínku na podkladu")

        entry_price = float(podminky[0].price)
        right = kontrakt.right
        strike = float(kontrakt.strike)
        quantity = int(trade.order.totalQuantity)
        nakup = valid_price(trade.orderStatus.avgFillPrice)

        # PT a SL nesou prodejní příkazy: společný podmíněný příkaz má obě
        # podmínky; při odděleném výstupu nese příkaz pro PT buď podmínku
        # na podkladu, nebo limitní cenu opce, příkaz pro SL podmínku, nebo
        # stop cenu opce. Z cen opce se zisk/ztráta v USD odvodí proti
        # nákupní ceně. Co nelze zjistit, dopočítá se ze strike a konfigurace.
        vystup = prikazy.get(order_ref(flow_id, "exit"))
        vystup_sl = prikazy.get(order_ref(flow_id, "exitsl"))
        pt_on = sl_on = True
        profit_target: float | None = None
        stop_loss: float | None = None

        if vystup is not None:
            podminky_pt = vystup.order.conditions
            if len(podminky_pt) >= 2:
                # Společný podmíněný příkaz nese obě úrovně na podkladu
                profit_target = float(podminky_pt[0].price)
                stop_loss = float(podminky_pt[1].price)
            elif podminky_pt:
                profit_target = float(podminky_pt[0].price)
            elif vystup.order.orderType == "LMT" and nakup:
                zisk = round((float(vystup.order.lmtPrice) - nakup) * calc.OPTION_MULTIPLIER, 2)
                if zisk > 0:
                    pt_on = False
                    profit_target = zisk

        # Příkaz pro SL se čte samostatně - přežít mohl i sám, bez příkazu pro PT
        if stop_loss is None and vystup_sl is not None:
            podminky_sl = vystup_sl.order.conditions
            if podminky_sl:
                stop_loss = float(podminky_sl[0].price)
            elif vystup_sl.order.orderType == "STP" and nakup:
                ztrata = round(
                    (nakup - float(vystup_sl.order.auxPrice)) * calc.OPTION_MULTIPLIER, 2
                )
                # Nula je platná hodnota - stop na nákupní ceně je break even
                if ztrata >= 0:
                    sl_on = False
                    stop_loss = ztrata

        dopocteno = profit_target is None or stop_loss is None
        if profit_target is None:
            pt_on = True
            profit_target = strike
        if stop_loss is None:
            sl_on = True
            stop_loss = calc.default_stop_loss(
                entry_price, profit_target if pt_on else strike, self.cfg.trading.sl_to_pt_ratio
            )

        flow = Flow(
            id=flow_id,
            symbol=kontrakt.symbol,
            entry_price=entry_price,
            profit_target=profit_target,
            stop_loss=stop_loss,
            quantity=quantity,
            max_spread_pct=self.cfg.trading.max_spread_pct,
            right=right,
            pt_on_underlying=pt_on,
            sl_on_underlying=sl_on,
            expiration=kontrakt.lastTradeDateOrContractMonth,
            strike=strike,
        )
        flow.entry_order_id = trade.order.orderId
        flow.entry_limit = valid_price(trade.order.lmtPrice)
        # Nákupní cena je základem úrovní zadaných na cenu opce; bez ní by
        # je nešlo ani zobrazit, ani měnit. Skutečný čas nákupu TWS u převzatého
        # příkazu neposkytne, zaznamenává se tedy čas převzetí
        if nakup is not None:
            flow.fill_price = nakup
            flow.fill_time = datetime.now()
        flow.filled_quantity = int(trade.orderStatus.filled or 0)

        await self._restore_flow(flow, prikazy, pozice)

        if dopocteno:
            flow.message += (
                " PT a SL nebyly v TWS k dispozici, jsou dopočítané ze strike "
                "a konfigurace - zkontrolujte je."
            )
        return flow

    async def _restore_flow(self, flow: Flow, prikazy: dict, pozice: dict) -> None:
        """Obnoví jeden obchod - kontrakty, odběry dat a skutečný stav příkazů."""
        # Kontrakty je nutné znovu ověřit, runtime objekty se neukládají
        flow.underlying_contract = await self.ib.qualify_stock(flow.symbol)
        option, details = await self.ib.qualify_option(
            flow.symbol, flow.expiration, flow.strike, flow.right
        )
        flow.option_contract = option
        flow.option_conid = option.conId
        flow.underlying_conid = flow.underlying_contract.conId
        flow.min_tick = details.minTick or flow.min_tick

        self.ib.subscribe(flow.underlying_contract)
        self.ib.subscribe(flow.option_contract)

        if not flow.original_profit_target:
            flow.original_profit_target = flow.profit_target
        # Starší stav počáteční SL nezná - doplní se z aktuálního. Nula je
        # platná hodnota (break even u SL na opci), proto rozhoduje
        # original_sl_known, ne pravdivost čísla
        if not flow.original_sl_known:
            flow.original_stop_loss = flow.stop_loss

        # Uložený stav mohl vzniknout ještě s chybným výpočtem cíle; obchod
        # s nesmyslnými úrovněmi se do trhu vracet nesmí
        if not calc.levels_sane(
            flow.entry_price,
            flow.profit_target,
            flow.stop_loss,
            pt_on_underlying=flow.pt_on_underlying,
            sl_on_underlying=flow.sl_on_underlying,
        ):
            flow.set_state(
                FlowState.ERROR,
                f"Obnovený obchod má nesmyslné úrovně (vstup {flow.entry_price:,.2f}, "
                f"PT {flow.profit_target:,.2f}, SL {flow.stop_loss:,.2f}). "
                f"Zrušte jej a zadejte znovu.".replace(",", " "),
            )
            self.log_event(f"{flow.id}: {flow.message}")
            return

        # Stav uložený starší verzí nesl výsledek prodaného runneru v jeho
        # polích; nově se zúčtovává, aby šel nastartovat další runner
        if flow.runner_active and flow.runner_fill_price is not None:
            if flow.fill_price is not None:
                flow.runner_realized_pnl += (
                    (flow.runner_fill_price - flow.fill_price) * flow.runner_quantity * 100
                )
            flow.runner_sold_quantity += flow.runner_quantity
            flow.runner_profit_target = None
            flow.runner_quantity = 0
            flow.runner_stop_loss = None
            flow.runner_fill_price = None

        flow.entry_trade = prikazy.get(order_ref(flow.id, "entry"))
        flow.exit_trade = prikazy.get(order_ref(flow.id, "exit"))
        flow.exit_sl_trade = prikazy.get(order_ref(flow.id, "exitsl"))
        flow.runner_trade = prikazy.get(order_ref(flow.id, "runner"))
        flow.runner_sl_trade = prikazy.get(order_ref(flow.id, "runnersl"))
        info = pozice.get(option.conId)
        drzeno = int(info.quantity) if info else 0

        self._restore_state(flow, drzeno)
        self.log_event(f"{flow.id}: obnoveno - {flow.state.label}. {flow.message}")

    def _restore_state(self, flow: Flow, drzeno: int) -> None:
        """
        Určí stav obchodu podle toho, co se skutečně nachází v TWS.
        Rozhoduje existence pozice a stav nalezených příkazů, nikoliv uložený zápis.
        """
        vstup = flow.entry_trade

        # Uzavírání na pokyn obchodníka pokračuje dál, stav se nepřepisuje
        if flow.state == FlowState.CLOSING:
            flow.touch("Spojení obnoveno, pozice se dál uzavírá.")
            return

        # Pozice je otevřená - rozhoduje stav prodejních příkazů obou částí
        if drzeno > 0:
            # Závazné je držené množství v TWS; už prodané kusy se přičtou,
            # aby hlavní část, runner i otevřené množství vycházely správně
            flow.filled_quantity = drzeno + flow.main_sold_quantity + flow.runner_sold_quantity

            # Rozdělané uzavírání trhem pokračuje. Podmíněné příkazy, na jejichž
            # zrušení se čekalo, se ruší znovu - požadavek se mohl ztratit
            # s výpadkem spojení a bez zrušení by tržní prodej nešel zadat
            for cast, uzavira in (
                ("exit", flow.main_close_requested),
                ("runner", flow.runner_close_requested),
            ):
                if uzavira and not self._market_sell_running(flow, cast):
                    self._cancel_part(flow, cast)

            # Zajištění potřebuje jen část, která se ještě neprodala a zároveň
            # se neuzavírá trhem
            chybi = [
                cast
                for cast, potreba in (
                    ("exit", flow.exit_fill_price is None and not flow.main_close_requested),
                    ("runner", flow.runner_active and not flow.runner_close_requested),
                )
                if potreba and not self._part_covered(flow, cast)
            ]

            if not chybi:
                flow.set_state(
                    FlowState.EXIT_ARMED,
                    f"Obnoveno: drženo {drzeno} ks, prodejní příkazy jsou v TWS.",
                )
                return

            if flow.main_close_requested or flow.runner_close_requested:
                # Nové zajištění by se sčítalo s běžícím tržním prodejem -
                # rozhodnutí zůstává na obchodníkovi
                flow.set_state(
                    FlowState.EXIT_ARMED,
                    f"Obnoveno: drženo {drzeno} ks, ale zajištění části pozice v TWS chybí "
                    f"a zároveň probíhá uzavírání trhem - zkontrolujte pozici v TWS.",
                )
                self.log_event(f"{flow.id}: {flow.message}")
                return

            # Zajištění chybí celé, nebo jen zčásti (osamocená polovina
            # odděleného výstupu, přeživší příkaz runneru). Přeživší
            # příkazy se ruší, ale zůstávají ve svých slotech - smyčka
            # počká na potvrzení zrušení a teprve pak zajištění založí
            # znovu, aby se v trhu neprodávalo víc kusů, než pozice drží
            self._cancel_part(flow, "exit")
            self._cancel_part(flow, "runner")
            flow.set_state(
                FlowState.FILLED,
                f"Obnoveno: drženo {drzeno} ks bez úplného zajištění, zajištění se doplní.",
            )
            flow.entry_cancel_requested = True
            return

        # Pozice není a prodejní příkaz byl vyplněn - obchod se uzavřel během výpadku
        if self._filled_leg(flow, "exit") is not None:
            _, _, vyplnene = self._part_fill_summary(flow, "exit")
            # Vyžádané uzavření trhem není ani PT, ani SL
            duvod = (
                "ručně" if flow.main_close_requested
                else self._part_reason(flow, "exit", vyplnene)
            )
            self._sync_part_fills(flow, "exit")
            if flow.main_sold_quantity:
                flow.exit_fill_price = flow.main_sold_value / flow.main_sold_quantity
            flow.exit_reason = duvod
            flow.set_state(FlowState.CLOSED, "Obnoveno: pozice byla uzavřena během výpadku.")
            self._release(flow)
            return

        # Nákupní příkaz stále čeká v trhu
        if vstup is not None and vstup.orderStatus.status not in DEAD_ORDER_STATES:
            if vstup.orderStatus.filled > 0:
                # Vyplněné množství je závazné - zajišťovat se bude podle něj
                flow.filled_quantity = max(flow.filled_quantity, int(vstup.orderStatus.filled))
                flow.set_state(FlowState.FILLED, "Obnoveno: nákup vyplněn, zajištění se doplní.")
            else:
                flow.entry_limit = valid_price(vstup.order.lmtPrice) or flow.entry_limit
                flow.set_state(FlowState.ARMED, "Obnoveno: nákupní příkaz čeká v trhu.")
            return

        # Obchod byl před nákupem a příkaz v TWS není - vrátí se do trhu smyčkou
        if flow.fill_price is None:
            flow.entry_trade = None
            flow.entry_order_id = None
            flow.set_state(
                FlowState.NO_QUOTES,
                "Obnoveno: nákupní příkaz v TWS nenalezen, bude zadán znovu.",
            )
            return

        # Zbývá případ, kdy byl obchod nakoupen, ale pozice ani příkaz nejsou
        flow.set_state(
            FlowState.ERROR,
            "Obnoveno: obchod byl nakoupen, ale v TWS není pozice ani prodejní příkaz. "
            "Zkontrolujte účet ručně.",
        )

    async def _refresh_account_size(self) -> None:
        """
        Obnoví velikost účtu z TWS, je-li v konfiguraci account.size = 0.
        Hodnota se mění s otevřenými pozicemi, proto se načítá opakovaně.
        """
        if self.cfg.account.size > 0:
            return

        loop = asyncio.get_running_loop()
        if (
            self._live_account_size is not None
            and loop.time() - self._account_checked < self.cfg.engine.account_refresh_sec
        ):
            return
        self._account_checked = loop.time()

        hodnota = await self.ib.net_liquidation()
        if hodnota is None:
            return
        if self._live_account_size is None:
            self.log_event(f"Velikost účtu převzata z TWS: {hodnota:,.2f} USD.".replace(",", " "))
        self._live_account_size = hodnota

    async def _check_unmanaged(self) -> None:
        """
        Periodicky hlídá opční pozice na účtu, ke kterým aplikace nemá obchod.
        Takové pozice nemají zajištění a obchodník o nich musí vědět.
        """
        interval = self.cfg.engine.unmanaged_check_sec
        if interval <= 0:
            return

        loop = asyncio.get_running_loop()
        if loop.time() - self._unmanaged_checked < interval:
            return
        self._unmanaged_checked = loop.time()

        pozice = await self.ib.positions()
        rizene = {
            flow.option_conid
            for flow in self.flows.values()
            if flow.state.is_active and flow.option_conid
        }
        nalezene = {conid: info for conid, info in pozice.items() if conid not in rizene}

        # Do průběhu se hlásí jen změna, aby se log nezaplnil stejnou hláškou
        if nalezene.keys() != self.unmanaged.keys():
            for conid, info in nalezene.items():
                if conid not in self.unmanaged:
                    self.log_event(f"POZOR: {self.unmanaged_text(info)}")
            self.unmanaged = nalezene
            self._notify()
        else:
            self.unmanaged = nalezene

    def unmanaged_text(self, info: PositionInfo) -> str:
        """
        Popis pozice bez zajištění pro upozornění.

        Běží-li na stejném tickeru obchod, jde nutně o jiný opční kontrakt
        (jiný strike nebo expiraci) - to bývá zdrojem nedorozumění, proto se
        na to upozorní výslovně.
        """
        jine = [
            flow
            for flow in self.flows.values()
            if flow.state.is_active and flow.symbol == info.symbol
        ]
        popis = (
            f"{info.label} ({int(info.quantity)} ks) je bez zajištění "
            f"a aplikace ji neřídí."
        )
        if jine:
            flow = jine[0]
            popis += (
                f" Obchod {flow.id} v přehledu se týká jiného kontraktu "
                f"({flow.right_label} {flow.strike:g}, expirace {flow.expiration}), "
                f"tuto pozici nehlídá."
            )
        popis += " Zkontrolujte ji v TWS."
        return popis

    async def _try_reconnect(self) -> None:
        """Pokusí se obnovit spojení s TWS po jeho výpadku."""
        try:
            await self.ib.connect()
            self.log_event("Spojení s TWS obnoveno.")
            # Obnova zároveň znovu založí odběry tržních dat
            await self.restore()
        except Exception:
            await asyncio.sleep(self.cfg.connection.reconnect_delay_sec)

    async def _monitor(self, flow: Flow) -> bool:
        """
        Jeden krok stavového automatu flow.
        Vrací True, pokud došlo ke změně, která se má promítnout do UI.
        """
        changed = self._refresh_market_data(flow)

        if flow.state in (FlowState.ARMED, FlowState.SPREAD_BLOCKED, FlowState.NO_QUOTES):
            changed |= self._handle_before_entry(flow)
        elif flow.state == FlowState.FILLED:
            changed |= self._handle_filled(flow)
        elif flow.state == FlowState.EXIT_ARMED:
            changed |= self._handle_exit(flow)
        elif flow.state == FlowState.CLOSING:
            changed |= self._handle_closing(flow)

        return changed

    def _refresh_market_data(self, flow: Flow) -> bool:
        """Načte aktuální ceny podkladu i opce a přepočítá spread."""
        price = self.ib.underlying_price(flow.underlying_contract)
        bid, ask, delta = self.ib.option_quotes(flow.option_contract)

        flow.underlying_price = price if price is not None else flow.underlying_price
        flow.option_bid = bid
        flow.option_ask = ask
        flow.option_spread_pct = calc.spread_pct(bid, ask)
        if delta is not None:
            flow.delta = delta

        # Očekávaný výsledek se přepočítává s každou změnou cen na trhu
        self._compute_expected_pnl(flow)
        return True

    def _handle_before_entry(self, flow: Flow) -> bool:
        """
        Stav před nákupem: kontrola vyplnění, hlídání spreadu
        a průběžná aktualizace limitní ceny.
        """
        trade = flow.entry_trade

        # Vyplnění nákupu má přednost před vším ostatním
        if trade is not None and trade.orderStatus.filled > 0:
            return self._register_fill(flow)

        # Příkaz zrušený mimo aplikaci (například ručně v TWS)
        if trade is not None and trade.orderStatus.status in DEAD_ORDER_STATES:
            if flow.state == FlowState.ARMED:
                flow.set_state(
                    FlowState.CANCELLED,
                    f"Nákupní příkaz byl zrušen v TWS ({trade.orderStatus.status}).",
                )
                self._release(flow)
                self.log_event(f"{flow.id}: {flow.message}")
                return True

        spread = flow.option_spread_pct
        trading = self.cfg.trading

        # Spread nad limitem - nevyplněný příkaz se odstraňuje z trhu
        if flow.state == FlowState.ARMED and spread is not None and spread > flow.max_spread_pct:
            if trading.cancel_on_spread_breach:
                self.ib.cancel(flow.entry_trade)
                flow.entry_trade = None
                flow.entry_order_id = None
                flow.blocked_since = datetime.now()
                flow.set_state(
                    FlowState.SPREAD_BLOCKED,
                    f"Spread {spread:.2f} % > limit {flow.max_spread_pct:g} %, "
                    f"příkaz odstraněn z trhu.",
                )
                self.log_event(f"{flow.id}: {flow.message}")
                return True
            return False

        # Spread zpět v limitu - příkaz se vrací do trhu
        if flow.state == FlowState.SPREAD_BLOCKED:
            if trading.rearm_on_spread_ok and self._can_rearm(flow, spread):
                return self._place_entry(flow)
            return False

        # Čekání na kotace - jakmile dorazí a spread vyhovuje, příkaz se zadá
        if flow.state == FlowState.NO_QUOTES:
            if spread is not None and spread > flow.max_spread_pct:
                flow.set_state(
                    FlowState.SPREAD_BLOCKED,
                    f"Spread {spread:.2f} % > limit {flow.max_spread_pct:g} %, příkaz nebyl zadán.",
                )
                return True
            return self._place_entry(flow)

        # Průběžná aktualizace limitní ceny nevyplněného příkazu
        if flow.state == FlowState.ARMED and trading.relimit_enabled:
            return self._update_entry_limit(flow)

        return False

    def _can_rearm(self, flow: Flow, spread: float | None) -> bool:
        """
        Posoudí, zda lze příkaz vrátit do trhu po zablokování spreadem.
        Spread musí klesnout s rezervou pod limit a od odstranění příkazu
        musí uplynout nastavená prodleva - jinak by se příkaz při kolísání
        spreadu kolem limitu opakovaně zadával a rušil.
        """
        if spread is None:
            return False

        trading = self.cfg.trading
        prah = flow.max_spread_pct * (1.0 - trading.rearm_spread_margin_pct / 100.0)
        if spread > prah:
            return False

        if flow.blocked_since is not None:
            uplynulo = (datetime.now() - flow.blocked_since).total_seconds()
            if uplynulo < trading.rearm_delay_sec:
                return False

        return True

    def _update_entry_limit(self, flow: Flow) -> bool:
        """
        Přepočítá limitní cenu nákupního příkazu podle aktuálního ASK / MID.
        Příkaz se modifikuje jen při změně větší než práh z konfigurace,
        aby se TWS nezahlcovala drobnými úpravami.
        """
        if flow.entry_trade is None or self.cfg.trading.entry_order_type == "MKT":
            return False

        # Příkaz, který TWS už ruší nebo vyplňuje, se upravovat nesmí.
        # Částečně vyplněný příkaz se také nechává být - modifikace by se
        # závodila s dobíhajícím vyplněním a TWS by hlásila "too late to replace"
        if flow.entry_trade.orderStatus.status not in MODIFIABLE_ORDER_STATES:
            return False
        if flow.entry_trade.orderStatus.filled > 0:
            return False

        new_limit = self._entry_limit(flow)
        if new_limit is None or flow.entry_limit is None:
            return False

        change_pct = abs(new_limit - flow.entry_limit) / flow.entry_limit * 100.0
        if change_pct < self.cfg.trading.relimit_min_change_pct:
            return False

        order = flow.entry_trade.order
        order.lmtPrice = new_limit
        # Odeslání příkazu se stejným orderId znamená jeho modifikaci
        flow.entry_trade = self.ib.place(flow.option_contract, order)
        flow.entry_limit = new_limit
        flow.touch()
        return True

    def _register_fill(self, flow: Flow) -> bool:
        """Zaznamená nákup opce a připraví flow na zadání výstupního příkazu."""
        status = flow.entry_trade.orderStatus
        flow.fill_price = valid_price(status.avgFillPrice) or flow.entry_limit
        flow.fill_time = datetime.now()
        flow.filled_quantity = int(status.filled)
        price_text = f"{flow.fill_price:g}" if flow.fill_price is not None else "neznámou cenu"
        flow.set_state(
            FlowState.FILLED,
            f"Nakoupeno {int(status.filled)} ks za {price_text}.",
        )
        self.log_event(f"{flow.id}: {flow.message}")
        return True

    def _handle_filled(self, flow: Flow) -> bool:
        """
        Po nákupu zadá jediný prodejní příkaz s podmínkami pro PT i SL.

        TWS nepovolí mít na jednom opčním kontraktu současně nákupní i prodejní
        příkaz (chyba 201). Při částečném vyplnění se proto nejprve zruší
        nevyplněný zbytek nákupu a na zrušení se počká; teprve potom lze
        zajistit už nakoupenou pozici.
        """
        trade = flow.entry_trade
        filled = int(trade.orderStatus.filled) if trade else flow.quantity
        if filled < 1:
            return False

        if trade is not None and trade.orderStatus.status not in ("Filled",) + DEAD_ORDER_STATES:
            # Zrušení se vyžaduje jen jednou, další průchody čekají na potvrzení z TWS
            if not flow.entry_cancel_requested:
                self.ib.cancel(trade)
                flow.entry_cancel_requested = True
                flow.touch(
                    f"Nakoupeno {filled} ks z {flow.quantity}, ruší se nevyplněný zbytek "
                    f"nákupu, aby šlo zadat prodejní příkaz."
                )
                self.log_event(f"{flow.id}: {flow.message}")
                return True
            return False

        # Přeživší prodejní příkazy z dřívějška (například osamocená polovina
        # odděleného výstupu po restartu) musí z trhu pryč, než se zajištění
        # založí znovu - jinak by se prodávalo víc kusů, než pozice drží.
        # Rušení je bezpečné opakovat, ruší se jen příkaz dosud aktivní.
        for part in ("exit", "runner"):
            for leg in self._legs(flow, part):
                if leg.orderStatus.status in ("Filled",) + DEAD_ORDER_STATES:
                    continue
                self.ib.cancel(leg)
                flow.touch(
                    "Čeká se na zrušení dřívějšího prodejního příkazu, "
                    "pak se zajištění založí znovu."
                )
                return False

        # Zajišťuje se skutečně držené množství - po restartu bývá nižší než
        # vyplněný nákup, protože část pozice se už mezitím prodala
        drzeno = flow.held_quantity - flow.main_sold_quantity
        self._place_exit(flow, drzeno if drzeno > 0 else filled)
        return True

    def _handle_exit(self, flow: Flow) -> bool:
        """
        Stav po zadání výstupních příkazů.

        Hlídá doplnění částečně vyplněného nákupu, prodej hlavní části
        i runneru a uzavření obchodu, jakmile jsou prodány obě části.
        Každá část může mít jeden společný podmíněný příkaz, nebo dvojici
        příkazů (PT a SL zvlášť) - po vyplnění jednoho z dvojice se druhý
        ihned ruší, aby se opce neprodala dvakrát.
        """
        changed = False

        # Nejprve se zúčtuje, co se na běžících příkazech (byť zčásti) prodalo -
        # teprve nad skutečně drženými kusy má smysl upravovat množství příkazů
        if self._collect_part_fills(flow, "runner"):
            changed = True
        if self._collect_part_fills(flow, "exit"):
            changed = True

        # Do dorovnání hlavního příkazu se počítá jen runner s vlastním
        # příkazem v trhu - runner čekající na doplnění nákupu žádné kusy nedrží
        runner_q = (
            flow.runner_quantity
            if flow.runner_active and flow.runner_trade is not None
            else 0
        )

        # Nákup se mohl doplnit až po zadání výstupu - hlavní příkazy se dorovnají
        # (runner má pevné množství, dorovnává se vždy hlavní část)
        if (
            flow.entry_trade is not None
            and flow.exit_trade is not None
            and not flow.main_close_requested
            and self._part_modifiable(flow, "exit")
        ):
            filled = int(flow.entry_trade.orderStatus.filled)
            # Kolik kusů má hlavní část ještě prodat: nakoupené mínus prodané
            # runnery, mínus kusy běžícího runneru a mínus vlastní částečné prodeje
            cilove = max(
                filled - flow.runner_sold_quantity - runner_q - flow.main_sold_quantity, 0
            )
            exit_qty = self._part_remaining(flow, "exit")
            if filled > flow.filled_quantity or cilove > exit_qty:
                if cilove > exit_qty:
                    self._resize_part(flow, "exit", cilove)
                    self.log_event(
                        f"{flow.id}: množství prodejního příkazu upraveno na {cilove} ks "
                        f"(nákup byl doplněn)."
                    )
                flow.filled_quantity = max(filled, flow.filled_quantity)
                changed = True

            # Runner odložený při částečném nákupu se oddělí, jakmile je kusů dost
            if (
                flow.runner_active
                and flow.runner_trade is None
                and not flow.runner_close_requested
                and flow.held_quantity > flow.runner_quantity
            ):
                self._split_exit_for_runner(flow)
                self.log_event(
                    f"{flow.id}: nákup doplněn, runner {flow.runner_quantity} ks "
                    f"se oddělil s cílem {flow.level_text('pt', flow.runner_profit_target)}."
                )
                changed = True

        # Runner, na který se nákup už nedoplní, se ruší - jinak by v přehledu
        # navždy vypadal jako aktivní, přestože žádný příkaz v trhu nemá
        if (
            flow.runner_active
            and flow.runner_trade is None
            and not flow.runner_close_requested
            and flow.held_quantity <= flow.runner_quantity
            and (
                flow.entry_trade is None
                or flow.entry_trade.orderStatus.status in DEAD_ORDER_STATES + ("Filled",)
            )
        ):
            flow.runner_profit_target = None
            flow.runner_quantity = 0
            flow.runner_stop_loss = None
            self.log_event(
                f"{flow.id}: runner zrušen - nakoupené množství "
                f"{flow.held_quantity} ks na něj nestačí."
            )
            changed = True

        # --- runner ---
        # Vyžádané uzavření runneru trhem: jakmile TWS potvrdí zrušení
        # podmíněných příkazů, zadá se prodej trhem
        if (
            flow.runner_active
            and flow.runner_close_requested
            and flow.runner_fill_price is None
            and self._part_all_dead(flow, "runner")
        ):
            # Kusy prodané těsně před zrušením se zaúčtují, trhem jde jen zbytek
            self._settle_part_fills(flow, "runner")
            if flow.runner_active:
                order = self.ib.market_sell_order(
                    flow.runner_quantity, order_ref(flow.id, "runner")
                )
                self._clear_part(flow, "runner")
                self._set_leg(flow, "runner", "pt", self.ib.place(flow.option_contract, order))
                flow.runner_market_sent = datetime.now()
                flow.runner_market_attempts += 1
                self.log_event(
                    f"{flow.id}: runner se prodává trhem ({flow.runner_quantity} ks)."
                )
            changed = True

        # Tržní prodej runneru, který TWS drží nevyplněný, se zadá znovu
        if (
            flow.runner_active
            and flow.runner_close_requested
            and flow.runner_fill_price is None
        ):
            changed |= self._retry_stalled_market_sell(flow, "runner")

        legy_runneru = self._legs(flow, "runner") if flow.runner_active else []
        if (
            legy_runneru
            and self._part_out_of_market(flow, "runner")
            and flow.runner_fill_price is None
            and not flow.runner_close_requested
        ):
            # Příkazy runneru zmizely mimo aplikaci - jeho kusy se vrací
            # pod hlavní příkaz, aby pozice nezůstala částečně nezajištěná
            if flow.exit_fill_price is None and self._part_modifiable(flow, "exit"):
                self._clear_part(flow, "runner")
                flow.runner_profit_target = None
                flow.runner_quantity = 0
                flow.runner_stop_loss = None
                self._resize_part(
                    flow, "exit", flow.held_quantity - flow.main_sold_quantity
                )
                self.log_event(
                    f"{flow.id}: příkaz runneru byl zrušen v TWS - jeho kusy "
                    f"převzal hlavní prodejní příkaz."
                )
            else:
                flow.set_state(
                    FlowState.ERROR,
                    "Příkaz runneru byl zrušen v TWS a nelze jej nahradit - "
                    "část pozice je bez zajištění.",
                )
                self.log_event(f"{flow.id}: {flow.message}")
            return True
        elif legy_runneru and not flow.runner_close_requested:
            changed |= self._warn_lost_leg(flow, "runner")

        # --- hlavní část ---
        # Vyžádané uzavření hlavní části trhem - stejný postup jako u runneru
        if (
            flow.main_close_requested
            and flow.exit_fill_price is None
            and self._part_all_dead(flow, "exit")
        ):
            # Kusy prodané těsně před zrušením se zaúčtují, trhem jde jen zbytek;
            # prodala-li se celá hlavní část, tržní prodej se nezadává vůbec
            self._settle_part_fills(flow, "exit")
            zbyva = flow.main_quantity - flow.main_sold_quantity
            if flow.exit_fill_price is None and zbyva >= 1:
                order = self.ib.market_sell_order(zbyva, order_ref(flow.id, "exit"))
                self._clear_part(flow, "exit")
                self._set_leg(flow, "exit", "pt", self.ib.place(flow.option_contract, order))
                flow.exit_market_sent = datetime.now()
                flow.exit_market_attempts += 1
                self.log_event(f"{flow.id}: hlavní část se prodává trhem ({zbyva} ks).")
            changed = True

        # Tržní prodej hlavní části, který TWS drží nevyplněný, se zadá znovu
        if flow.main_close_requested and flow.exit_fill_price is None:
            changed |= self._retry_stalled_market_sell(flow, "exit")

        legy = self._legs(flow, "exit")
        if not legy:
            return changed

        # Prodejní příkazy, které už nemohou prodat - zrušené mimo aplikaci,
        # nebo vyplněné na menší množství, než pozice drží
        if (
            self._part_out_of_market(flow, "exit")
            and flow.exit_fill_price is None
            and not flow.main_close_requested
        ):
            flow.set_state(
                FlowState.ERROR,
                f"Prodejní příkaz už není v trhu ({self._part_status_text(flow, 'exit')}) - "
                f"zbytek pozice ({flow.main_quantity - flow.main_sold_quantity} ks) "
                f"je bez zajištění.",
            )
            self.log_event(f"{flow.id}: {flow.message}")
            return True

        # Z dvojice příkazů zmizel jen jeden - pozice je krytá jen z jedné strany
        if flow.exit_fill_price is None and not flow.main_close_requested:
            changed |= self._warn_lost_leg(flow, "exit")

        # --- uzavření: hlavní část prodaná a žádný runner už neběží ---
        hlavni_hotova = flow.exit_fill_price is not None
        if hlavni_hotova and not flow.runner_active:
            pnl = flow.unrealized_pnl
            pnl_text = f", výsledek {pnl:+.2f} USD" if pnl is not None else ""
            cena = f"{flow.exit_fill_price:g}" if flow.exit_fill_price else "?"
            dovetek = ""
            if flow.runner_sold_quantity:
                dovetek = (
                    f", runnery {flow.runner_sold_quantity} ks "
                    f"{flow.runner_realized_pnl:+.2f} USD"
                )
            flow.set_state(
                FlowState.CLOSED,
                f"Pozice uzavřena ({flow.exit_reason}) za {cena}{dovetek}{pnl_text}.",
            )
            self._release(flow)
            self.log_event(f"{flow.id}: {flow.message}")
            return True

        # Hlavní část je prodaná, ale runner běží dál
        if hlavni_hotova and flow.runner_active and "runner běží dál" not in flow.message:
            flow.touch(
                f"Hlavní část prodána ({flow.exit_reason}), runner "
                f"{flow.runner_quantity} ks běží dál s cílem "
                f"{flow.level_text('pt', flow.runner_profit_target)}."
            )
            changed = True

        return changed

    def _warn_lost_leg(self, flow: Flow, part: str) -> bool:
        """
        Upozorní (jednou), že z dvojice prodejních příkazů části zmizel jeden
        bez vyplnění, zatímco druhý dál běží - pozice je krytá jen z jedné strany.

        Nahrazovat ztracený příkaz naslepo nelze: TWS ruší druhý příkaz OCA
        skupiny i ve chvíli, kdy se první teprve vyplňuje, a nový příkaz by
        pak opci prodal podruhé. Rozhodnutí zůstává na obchodníkovi.
        """
        legy = self._legs(flow, part)
        if len(legy) < 2:
            return False
        mrtve = [t for t in legy if t.orderStatus.status in DEAD_ORDER_STATES]
        if not mrtve or len(mrtve) == len(legy):
            return False
        if any(t.orderStatus.filled > 0 for t in legy):
            return False

        klic = f"{flow.id}:{part}"
        if klic in self._lost_leg_warned:
            return False
        self._lost_leg_warned.add(klic)

        ztraceny = "SL" if mrtve[0] is self._leg(flow, part, "sl") else "PT"
        popis = "runneru" if part == "runner" else "hlavní části"
        self.log_event(
            f"{flow.id}: POZOR - příkaz pro {ztraceny} {popis} byl zrušen v TWS, "
            f"v trhu zůstává jen příkaz pro {'PT' if ztraceny == 'SL' else 'SL'}. "
            f"Zkontrolujte pozici v TWS."
        )
        return True

    def _handle_closing(self, flow: Flow) -> bool:
        """
        Uzavírá pozici na pokyn obchodníka.
        Čeká na zrušení dřívějších příkazů a poté zadá prodej trhem.
        """
        # Tržní prodej, který TWS drží nevyplněný, se zadá znovu. Musí se
        # ověřit před čekáním na aktivní příkazy - zaseknutý prodej je sám
        # aktivním příkazem a jinak by se k hlídači nikdy nedošlo
        if self._retry_stalled_market_sell(flow, "exit"):
            return True

        # Dokud je jakýkoliv dřívější příkaz aktivní, tržní prodej by se s ním
        # sčítal a prodalo by se více kusů, než pozice drží
        for trade in (
            flow.entry_trade,
            flow.exit_trade,
            flow.exit_sl_trade,
            flow.runner_trade,
            flow.runner_sl_trade,
        ):
            if trade is not None and trade.orderStatus.status in MODIFIABLE_ORDER_STATES:
                return False

        # Prodej se zadává jednou; v dalších průchodech se sleduje jeho vyplnění
        if flow.exit_trade is None or flow.exit_trade.orderStatus.status in DEAD_ORDER_STATES:
            # Prodeje vyplněné těsně před zrušením příkazů (i částečné) se
            # zaúčtují dřív, než se zadá tržní prodej - jinak by se prodalo
            # víc kusů, než pozice drží, a vznikla by nekrytá krátká pozice
            self._settle_part_fills(flow, "exit")
            self._settle_part_fills(flow, "runner")

            # Prodává se jen skutečně otevřený zbytek - po ručním prodeji hlavní
            # části to bývá pouze runner
            mnozstvi = flow.open_quantity

            # Bez otevřených kusů už není co prodávat - obchod se rovnou uzavře
            if mnozstvi < 1:
                pnl = flow.unrealized_pnl
                pnl_text = f", výsledek {pnl:+.2f} USD" if pnl is not None else ""
                flow.set_state(FlowState.CLOSED, f"Pozice uzavřena obchodníkem{pnl_text}.")
                self._release(flow)
                self.log_event(f"{flow.id}: {flow.message}")
                return True

            order = self.ib.market_sell_order(mnozstvi, order_ref(flow.id, "exit"))
            self._clear_part(flow, "exit")
            self._set_leg(flow, "exit", "pt", self.ib.place(flow.option_contract, order))
            flow.exit_market_sent = datetime.now()
            flow.exit_market_attempts += 1
            flow.touch(f"Uzavírám pozici trhem ({mnozstvi} ks).")
            self.log_event(f"{flow.id}: {flow.message}")
            return True

        if flow.exit_trade.orderStatus.status == "Filled":
            cena = valid_price(flow.exit_trade.orderStatus.avgFillPrice)
            prodano = int(flow.exit_trade.orderStatus.filled or 0)

            # Zbylý runner (prodaný samostatně i v rámci celé pozice)
            # se zúčtuje do realizovaného výsledku
            if flow.runner_active and flow.runner_quantity <= prodano:
                if cena is not None and flow.fill_price is not None:
                    flow.runner_realized_pnl += (
                        (cena - flow.fill_price) * flow.runner_quantity * 100
                    )
                flow.runner_sold_quantity += flow.runner_quantity
                flow.runner_profit_target = None
                flow.runner_quantity = 0
                flow.runner_stop_loss = None
                self._clear_part(flow, "runner")

            # Cena prodeje hlavní části se nepřepisuje, pokud už byla prodána
            # dříve - teď se uzavíral jen zbytek pozice. Kusy prodané před
            # zrušením příkazů se do ceny započítají váženým průměrem
            if flow.exit_fill_price is None:
                ks_ted = max(min(prodano, flow.main_quantity - flow.main_sold_quantity), 0)
                flow.exit_fill_price = self._blended_exit_price(flow, cena, ks_ted)
                flow.exit_reason = "ručně"

            pnl = flow.unrealized_pnl
            pnl_text = f", výsledek {pnl:+.2f} USD" if pnl is not None else ""
            cena_text = f"{cena:g}" if cena is not None else "?"
            flow.set_state(
                FlowState.CLOSED, f"Pozice uzavřena obchodníkem za {cena_text}{pnl_text}."
            )
            self._release(flow)
            self.log_event(f"{flow.id}: {flow.message}")
            return True

        return False

    def _retry_stalled_market_sell(self, flow: Flow, cast: str) -> bool:
        """
        Hlídá vyžádaný tržní prodej dané části ('exit' = hlavní, 'runner').

        TWS (zejména demo) občas nechá tržní příkaz viset nevyplněný ve stavu
        PreSubmitted. Takový příkaz se po prodlevě zruší a smyčka jej zadá
        znovu. Po vyčerpání pokusů zůstane poslední příkaz v trhu a obchodník
        je upozorněn, aby pozici zkontroloval v TWS.
        """
        trade = getattr(flow, f"{cast}_trade")
        if trade is None or trade.orderStatus.status not in MODIFIABLE_ORDER_STATES:
            return False
        # Částečně vyplněný příkaz se neruší, aby se prodej nezdvojil
        if trade.orderStatus.filled > 0:
            return False

        odeslano = getattr(flow, f"{cast}_market_sent")
        if odeslano is None:
            # Příkaz převzatý např. při obnově po restartu - čas běží od teď
            setattr(flow, f"{cast}_market_sent", datetime.now())
            return False
        if (datetime.now() - odeslano).total_seconds() < MARKET_SELL_RETRY_SEC:
            return False

        pokusy = getattr(flow, f"{cast}_market_attempts")
        popis = "runneru" if cast == "runner" else "hlavní části"
        if pokusy >= MARKET_SELL_MAX_ATTEMPTS:
            # Varování se vypíše jen jednou - počítadlo se posune za limit
            if pokusy == MARKET_SELL_MAX_ATTEMPTS:
                setattr(flow, f"{cast}_market_attempts", pokusy + 1)
                self.log_event(
                    f"{flow.id}: POZOR - tržní prodej {popis} se opakovaně "
                    f"nedaří vyplnit, poslední příkaz zůstává v trhu. "
                    f"Zkontrolujte pozici v TWS."
                )
                return True
            return False

        self.ib.cancel(trade)
        setattr(flow, f"{cast}_market_sent", None)
        self.log_event(
            f"{flow.id}: tržní prodej {popis} se do {MARKET_SELL_RETRY_SEC:g} s "
            f"nevyplnil, příkaz se zruší a zadá znovu."
        )
        return True

    def _exit_reason(self, flow: Flow, profit_target: float, stop_loss: float) -> str:
        """
        Určí, zda společný podmíněný příkaz (PT OR SL) prodal na PT nebo SL,
        podle ceny podkladu při uzavření.
        """
        price = flow.underlying_price
        if price is None:
            return "PT/SL"
        # Rozhoduje bližší úroveň. Podklad se mezi splněním podmínky a zápisem
        # prodeje stihne pohnout, jednostranné porovnání s PT proto umělo
        # označit ziskový výstup těsně pod cílem jako SL.
        return "PT" if abs(price - profit_target) <= abs(price - stop_loss) else "SL"

    def _part_fill_summary(self, flow: Flow, part: str) -> tuple[float | None, int, list[Any]]:
        """
        Souhrn prodeje části přes všechny její příkazy: vážená průměrná cena,
        celkový počet prodaných kusů a příkazy, které se (byť zčásti) vyplnily.

        U dvojice příkazů se může část kusů prodat na PT a zbytek - poté, co
        TWS přes OCA skupinu zmenší druhý příkaz - na SL. Cena jediného
        příkazu se stavem Filled by pak zkreslila výsledek celé části.
        """
        celkem = 0
        hodnota = 0.0
        vyplnene: list[Any] = []
        for trade in self._legs(flow, part):
            mnozstvi = int(trade.orderStatus.filled or 0)
            cena = valid_price(trade.orderStatus.avgFillPrice)
            if mnozstvi <= 0 or cena is None:
                continue
            celkem += mnozstvi
            hodnota += cena * mnozstvi
            vyplnene.append(trade)
        if celkem == 0:
            return None, 0, []
        return hodnota / celkem, celkem, vyplnene

    def _blended_exit_price(self, flow: Flow, cena: float | None, ks: int) -> float | None:
        """
        Výsledná prodejní cena hlavní části: vážený průměr kusů prodaných
        dříve (před zrušením příkazů) a kusů prodaných teď.
        """
        if cena is None:
            return None
        celkem = flow.main_sold_quantity + ks
        if celkem <= 0:
            return cena
        return (flow.main_sold_value + cena * ks) / celkem

    def _account_part_fills(self, flow: Flow, part: str) -> tuple[int, float]:
        """
        Zjistí, kolik kusů se na běžících příkazech části nově vyplnilo.

        Účtuje se přírůstkově - pamatuje se, kolik kusů a za jakou hodnotu už
        z těchto příkazů započteno bylo, takže opakované volání v každém
        průchodu smyčkou totéž vyplnění nezapočítá dvakrát. Vrací dvojici
        (nově prodané kusy, jejich hodnota = součet cena × kusy).
        """
        cena, ks, _ = self._part_fill_summary(flow, part)
        if ks <= 0 or cena is None:
            return 0, 0.0

        predpona = self._sold_prefix(part)
        drive_ks = getattr(flow, f"{predpona}_counted_quantity")
        drive_hodnota = getattr(flow, f"{predpona}_counted_value")
        if ks <= drive_ks:
            return 0, 0.0

        setattr(flow, f"{predpona}_counted_quantity", ks)
        setattr(flow, f"{predpona}_counted_value", cena * ks)
        return ks - drive_ks, cena * ks - drive_hodnota

    def _sync_part_fills(self, flow: Flow, part: str) -> tuple[int, float]:
        """
        Promítne nově vyplněné kusy části do modelu pozice.

        Hlavní část se sčítá do main_sold_quantity / main_sold_value, ze kterých
        vychází vážený průměr výsledné prodejní ceny. Runner se rovnou zúčtuje
        do realizovaného výsledku a o prodané kusy se zmenší, aby držené
        množství odpovídalo skutečnosti. Vrací (nově prodané kusy, jejich
        průměrnou cenu).
        """
        if part == "exit":
            if flow.exit_fill_price is not None:
                return 0, 0.0
            ks, hodnota = self._account_part_fills(flow, "exit")
            if ks <= 0:
                return 0, 0.0
            flow.main_sold_quantity += ks
            flow.main_sold_value += hodnota
            return ks, hodnota / ks

        if not flow.runner_active or flow.runner_fill_price is not None:
            return 0, 0.0
        ks, hodnota = self._account_part_fills(flow, "runner")
        if ks <= 0:
            return 0, 0.0

        cena = hodnota / ks
        # Pojistka: víc kusů, než runner drží, se zúčtovat nesmí
        ks = min(ks, flow.runner_quantity)
        if flow.fill_price is not None:
            flow.runner_realized_pnl += (cena - flow.fill_price) * ks * calc.OPTION_MULTIPLIER
        flow.runner_sold_quantity += ks
        flow.runner_quantity -= ks
        # Doprodaný runner uvolní svá pole, aby šlo nastartovat další
        if flow.runner_quantity <= 0:
            flow.runner_profit_target = None
            flow.runner_quantity = 0
            flow.runner_stop_loss = None
        return ks, cena

    def _settle_part_fills(self, flow: Flow, part: str) -> int:
        """
        Zaúčtuje prodeje části, které se (i zčásti) vyplnily na jejích dosavadních
        příkazech, a vrátí počet takto prodaných kusů.

        Volá se těsně před zadáním tržního prodeje na pokyn obchodníka: příkaz
        se mohl vyplnit ve stejné chvíli, kdy se rušil, a tyto kusy se už
        prodávat nesmí - jinak by vznikla nekrytá krátká pozice. Celá prodaná
        hlavní část se zapíše jako její prodej, částečný prodej se jen
        poznamená a zbytek prodá trh.
        """
        _, _, vyplnene = self._part_fill_summary(flow, part)
        ks, cena = self._sync_part_fills(flow, part)
        if ks == 0:
            return 0

        if part == "exit":
            duvod = self._part_reason(flow, "exit", vyplnene)
            if flow.main_sold_quantity >= flow.main_quantity:
                flow.exit_fill_price = flow.main_sold_value / flow.main_sold_quantity
                flow.exit_reason = duvod
                self.log_event(
                    f"{flow.id}: hlavní část ({flow.main_quantity} ks) se prodala ({duvod}) "
                    f"za {flow.exit_fill_price:g} ještě před zrušením příkazů - "
                    f"trhem se neprodává."
                )
            else:
                self.log_event(
                    f"{flow.id}: {ks} ks hlavní části se prodalo ({duvod}) za {cena:g} "
                    f"ještě před zrušením příkazů, trhem se prodá jen zbytek."
                )
            return ks

        self.log_event(
            f"{flow.id}: {ks} ks runneru se prodalo za {cena:g} ještě před zrušením příkazů."
        )
        if not flow.runner_active:
            flow.runner_close_requested = False
            self._clear_part(flow, "runner")
        return ks

    def _collect_part_fills(self, flow: Flow, part: str) -> bool:
        """
        Zúčtuje prodeje vyplněné na běžících příkazech části a popíše je v logu.

        Je-li jeden příkaz dvojice vyplněný celý, druhý se ihned ruší, aby se
        opce neprodala dvakrát (TWS jej přes OCA skupinu ruší také, ale na to
        se nečeká). Částečné vyplnění se jen zaúčtuje - příkazy dál běží
        a TWS druhý příkaz dvojice sama zmenšila. Vrací True, pokud se stav
        obchodu změnil.
        """
        legy = self._legs(flow, part)
        if not legy:
            return False
        if part == "exit":
            if flow.exit_fill_price is not None:
                return False
        elif not flow.runner_active or flow.runner_fill_price is not None:
            return False

        vyplneny = self._filled_leg(flow, part)
        if vyplneny is not None:
            self._cancel_other_legs(flow, part, vyplneny)

        _, _, vyplnene = self._part_fill_summary(flow, part)
        # Prodej na pokyn obchodníka (tržní příkaz) není ani PT, ani SL.
        # Důvod se určuje před zúčtováním - doprodaný runner uvolní svůj cíl
        # a úrovně pro porovnání by pak chyběly
        zavira = flow.main_close_requested if part == "exit" else flow.runner_close_requested
        duvod = "ručně" if zavira else self._part_reason(flow, part, vyplnene)

        ks, cena = self._sync_part_fills(flow, part)
        if ks == 0:
            return False

        if part == "exit":
            if flow.main_sold_quantity >= flow.main_quantity:
                flow.exit_fill_price = flow.main_sold_value / flow.main_sold_quantity
                flow.exit_reason = duvod
                self.log_event(
                    f"{flow.id}: hlavní část ({flow.main_quantity} ks) prodána ({duvod}) "
                    f"za {flow.exit_fill_price:g}."
                )
            else:
                self.log_event(
                    f"{flow.id}: {ks} ks hlavní části prodáno ({duvod}) za {cena:g}, "
                    f"zbývá prodat {flow.main_quantity - flow.main_sold_quantity} ks."
                )
            return True

        vysledek = ""
        if flow.fill_price is not None:
            dilci = (cena - flow.fill_price) * ks * calc.OPTION_MULTIPLIER
            vysledek = f", výsledek {dilci:+.2f} USD"
        if flow.runner_active:
            self.log_event(
                f"{flow.id}: {ks} ks runneru prodáno ({duvod}) za {cena:g}{vysledek}, "
                f"zbývá {flow.runner_quantity} ks."
            )
        else:
            # Doprodaný runner uvolní své sloty - z hlavní části lze oddělit další
            self.log_event(
                f"{flow.id}: runner ({ks} ks) prodán ({duvod}) za {cena:g}{vysledek}."
            )
            self._clear_part(flow, "runner")
            flow.runner_close_requested = False
        return True

    def _part_reason(self, flow: Flow, part: str, vyplnene: list[Any]) -> str:
        """
        Důvod prodeje části podle příkazů, které se vyplnily.
        Prodaly-li se kusy na obou příkazech dvojice, je důvod 'PT+SL'.
        """
        if not vyplnene:
            return "PT/SL"
        if flow.exit_split and len(vyplnene) > 1:
            return "PT+SL"
        return self._leg_reason(flow, part, vyplnene[0])

    def _leg_reason(self, flow: Flow, part: str, trade: Any) -> str:
        """
        Důvod prodeje části podle toho, který její příkaz se vyplnil.
        U odděleného výstupu je to jednoznačné (příkaz pro PT, nebo pro SL),
        u společného příkazu rozhoduje poloha podkladu vůči úrovním.
        """
        if flow.exit_split:
            return "SL" if trade is self._leg(flow, part, "sl") else "PT"
        pt_level, sl_level = self._part_levels(flow, part)
        return self._exit_reason(flow, pt_level, sl_level)
