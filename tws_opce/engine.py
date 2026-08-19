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
from datetime import datetime
from typing import Any, Callable

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
    stop_loss: float = 0.0
    delta: float | None = None
    # True, pokud delta nepřišla z TWS a byla dopočítána z ceny opce
    delta_estimated: bool = False
    option_bid: float | None = None
    option_ask: float | None = None
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
    ) -> Preview:
        """
        Připraví zadání obchodu: načte cenu podkladu, určí typ opce, expiraci,
        strike podle PT, dopočítá SL a doporučené množství kontraktů.
        Nezadává žádný příkaz do trhu.
        """
        if not self.ib.connected:
            raise RuntimeError("Není navázáno spojení s TWS.")

        symbol = symbol.upper().strip()
        if not symbol:
            raise ValueError("Zadejte ticker.")

        preview = Preview(symbol=symbol, account_size=self.account_size, risk_amount=self.risk_amount)

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

        # Bez vstupní ceny a PT nelze určit kontrakt, vrací se jen cena podkladu
        if entry_price is None or profit_target is None:
            self._replace_preview(preview)
            return preview

        reference = preview.current_price if preview.current_price is not None else entry_price
        preview.right = calc.determine_right(reference, entry_price)

        # SL buď zadaný uživatelem, nebo dopočtený podle poměru z konfigurace
        preview.stop_loss = (
            stop_loss
            if stop_loss is not None
            else calc.default_stop_loss(entry_price, profit_target, self.cfg.trading.sl_to_pt_ratio)
        )

        # Výběr expirace a strike nejbližšího k PT
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

        strike, option, details = await self._qualify_nearest_option(
            symbol, expiration, list(chain.strikes), profit_target, preview.right, chain.tradingClass
        )
        preview.strike = strike
        preview.option = option
        preview.min_tick = details.minTick or 0.01

        # Náhradní strike se hlásí, aby bylo jasné, proč kontrakt neodpovídá PT
        nejblizsi = calc.nearest_strike(sorted(chain.strikes), profit_target)
        if nejblizsi is not None and strike != nejblizsi:
            preview.warnings.append(
                f"Strike {nejblizsi:g} není pro expiraci {expiration} v TWS dostupný, "
                f"použit nejbližší obchodovatelný {strike:g}."
            )

        # Tržní data opce kvůli deltě a spreadu
        self.ib.subscribe(option)
        await self.ib.wait_for_quotes(option, self.cfg.engine.market_data_timeout_sec)
        bid, ask, delta = self.ib.option_quotes(option)
        preview.option_bid = bid
        preview.option_ask = ask
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

        preview.quantity = calc.suggest_quantity(
            self.risk_amount,
            entry_price,
            preview.stop_loss,
            used_delta,
            self.cfg.trading.min_quantity,
            self.cfg.trading.max_quantity,
        )

        if preview.spread_pct is not None and preview.spread_pct > self.cfg.trading.max_spread_pct:
            preview.warnings.append(
                f"Aktuální spread {preview.spread_pct:.2f} % překračuje limit "
                f"{self.cfg.trading.max_spread_pct:g} %."
            )

        self._replace_preview(preview)
        return preview

    def _estimate_delta(self, preview: Preview) -> float | None:
        """
        Dopočítá deltu z tržní ceny opce, když ji TWS nepošle.
        Používá se střed trhu, při jeho nedostupnosti poslední známá cena.
        """
        cena = None
        if preview.option_bid is not None and preview.option_ask is not None:
            cena = (preview.option_bid + preview.option_ask) / 2.0
        elif preview.option_ask is not None:
            cena = preview.option_ask

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

    def _validate(self, right: str, entry: float, pt: float, sl: float) -> None:
        """
        Ověří vzájemnou polohu vstupní ceny, PT a SL vůči typu opce.
        U CALL musí být PT nad vstupem a SL pod ním, u PUT opačně.
        """
        if right == "C":
            if pt <= entry:
                raise ValueError("U CALL opce musí být PT nad vstupní cenou podkladu.")
            if sl >= entry:
                raise ValueError("U CALL opce musí být SL pod vstupní cenou podkladu.")
        else:
            if pt >= entry:
                raise ValueError("U PUT opce musí být PT pod vstupní cenou podkladu.")
            if sl <= entry:
                raise ValueError("U PUT opce musí být SL nad vstupní cenou podkladu.")

    async def start_flow(self, request: FlowRequest) -> Flow:
        """
        Založí nové flow: ověří zadání, vybere kontrakt a zadá nákupní příkaz
        s cenovou podmínkou na podkladu do TWS.
        """
        async with self._lock:
            symbol = request.symbol.upper().strip()

            # Zamýšlený směr obchodu prozrazuje poloha PT: je-li nad vstupem,
            # čeká se průraz nahoru (long/CALL), pod vstupem průraz dolů
            # (short/PUT). Podle směru se řídí i jedinečnost obchodů na tickeru.
            zamer = "C" if request.profit_target > request.entry_price else "P"

            # Na jednom tickeru smí běžet současně jeden long a jeden short.
            # Nové zadání nahrazuje jen čekající obchod STEJNÉHO směru; obchod
            # s otevřenou (či právě uzavíranou) pozicí se chrání.
            bezici = self.active_flow_for(symbol, zamer)
            if bezici is not None and not bezici.state.is_before_entry:
                smer_popis = "long (CALL)" if zamer == "C" else "short (PUT)"
                raise ValueError(
                    f"Pro ticker {symbol} již běží {smer_popis} obchod s otevřenou "
                    f"pozicí. Nejprve jej zrušte."
                )

            preview = await self.prepare(
                symbol, request.entry_price, request.profit_target, request.stop_loss
            )

            # Propásnutý vstup se hlásí dřív než ostatní kontroly, jinak by
            # uživatel dostal matoucí hlášku o poloze PT vůči vstupu.
            # Liší-li se zamýšlený směr od typu opce odvozeného z aktuální
            # ceny, cena už vstupní úroveň překonala a obchod ujel.
            if preview.current_price is not None and zamer != preview.right:
                smer = "nad" if zamer == "C" else "pod"
                raise ValueError(
                    f"Cena podkladu {preview.current_price:g} je již {smer} vstupem "
                    f"{request.entry_price:g} - vstup je propásnutý a obchod nelze zadat."
                )

            stop_loss = request.stop_loss if request.stop_loss is not None else preview.stop_loss
            self._validate(preview.right, request.entry_price, request.profit_target, stop_loss)

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
                profit_target=request.profit_target,
                original_profit_target=request.profit_target,
                stop_loss=stop_loss,
                original_stop_loss=stop_loss,
                quantity=quantity,
                max_spread_pct=max_spread,
                right=preview.right,
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
                f"vstup {request.entry_price:g}, PT {request.profit_target:g}, SL {stop_loss:g}."
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

        cil = round(
            flow.entry_price + (flow.original_profit_target - flow.entry_price) * nasobek, 2
        )
        flow.runner_profit_target = cil
        flow.runner_quantity = runner_q
        flow.runner_stop_loss = flow.stop_loss
        self.log_event(
            f"{flow.id}: runner {runner_q} ks převzat z nahrazeného obchodu, "
            f"cíl {cil:,.2f} ({nasobek:g}× původní cíl).".replace(",", " ")
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
        """
        bid, ask, _ = self.ib.option_quotes(flow.option_contract)
        aktualni = (bid + ask) / 2.0 if bid and ask else (ask or bid)
        podklad = flow.underlying_price

        if not aktualni or not podklad:
            flow.expected_profit = None
            flow.expected_loss = None
            return

        sazba = self.cfg.trading.risk_free_rate_pct

        def cena_pri(uroven: float) -> float | None:
            """Odhad ceny opce, až podklad dosáhne dané úrovně."""
            return calc.project_option_price(
                aktualni, podklad, uroven, flow.strike, flow.expiration, sazba, flow.right
            )

        if flow.fill_price:
            nakupni = flow.fill_price
        else:
            nakupni = cena_pri(flow.entry_price)
            # Model dává střed trhu, nakupuje se ale na ASK - přičte se půl spreadu
            if nakupni is not None and bid and ask:
                nakupni += (ask - bid) / 2.0

        cena_pt = cena_pri(flow.profit_target)
        cena_sl = cena_pri(flow.stop_loss)

        if nakupni is None or cena_pt is None or cena_sl is None:
            flow.expected_profit = None
            flow.expected_loss = None
            return

        # Počítají se jen dosud otevřené části; prodané runnery (případně
        # i prodaná hlavní část) vstupují realizovaným výsledkem
        realizovano = flow.runner_realized_pnl
        hlavni_q = flow.main_quantity
        if flow.exit_fill_price is not None:
            realizovano += (flow.exit_fill_price - nakupni) * hlavni_q * 100
            hlavni_q = 0

        runner_q = 0
        cena_runner = None
        if flow.runner_active and flow.runner_quantity <= flow.held_quantity:
            cena_runner = cena_pri(flow.runner_profit_target)
            if cena_runner is not None:
                runner_q = flow.runner_quantity

        flow.expected_profit = realizovano + (cena_pt - nakupni) * hlavni_q * 100
        if runner_q:
            flow.expected_profit += (cena_runner - nakupni) * runner_q * 100
        flow.expected_loss = realizovano + (cena_sl - nakupni) * (hlavni_q + runner_q) * 100

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

    def _place_exit(self, flow: Flow, quantity: int) -> None:
        """
        Zadá zajišťovací prodejní příkazy s podmínkami PT i SL spojenými OR.

        Bez runneru vznikne jediný příkaz na celou pozici. S aktivním runnerem
        se pozice dělí: hlavní část prodává na PT obchodu, runner samostatným
        příkazem na vlastním cíli; SL mají oba společný.
        """
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

        _, pt_more, sl_more = calc.condition_directions(flow.right)
        conditions = [
            self.ib.price_condition(flow.underlying_conid, pt_more, flow.profit_target),
            self.ib.price_condition(flow.underlying_conid, sl_more, flow.stop_loss),
        ]

        limit = None
        if self.cfg.trading.exit_order_type == "LMT":
            bid, _, _ = self.ib.option_quotes(flow.option_contract)
            price = calc.exit_limit_price(bid, self.cfg.trading.bid_tolerance_pct)
            limit = calc.round_to_tick(price, flow.min_tick) if price is not None else None

        order = self.ib.build_exit_order(
            hlavni_q, limit, conditions, order_ref(flow.id, "exit")
        )
        flow.exit_trade = self.ib.place(flow.option_contract, order)
        flow.exit_order_id = flow.exit_trade.order.orderId

        popis_runneru = ""
        if runner_q:
            runner_order = self.ib.build_exit_order(
                runner_q, None, self._runner_conditions(flow), order_ref(flow.id, "runner")
            )
            flow.runner_trade = self.ib.place(flow.option_contract, runner_order)
            flow.runner_order_id = flow.runner_trade.order.orderId
            popis_runneru = (
                f" Runner {runner_q} ks s cílem {flow.runner_profit_target:g}."
            )

        limit_text = f"LMT {limit:g}" if limit is not None else "MKT"
        flow.set_state(
            FlowState.EXIT_ARMED,
            f"Prodejní příkaz ({limit_text}) na {hlavni_q} ks, "
            f"PT {flow.profit_target:g} / SL {flow.stop_loss:g}.{popis_runneru}",
        )
        self.log_event(f"{flow.id}: {flow.message}")

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
        puvodni_vzdalenost = abs(flow.original_profit_target - flow.entry_price)
        if puvodni_vzdalenost > 0:
            nova_vzdalenost = abs(novy_pt - flow.entry_price)
            if nova_vzdalenost > puvodni_vzdalenost * 20:
                raise ValueError(
                    f"Cíl {novy_pt:g} je nesmyslně daleko od vstupu {flow.entry_price:g} "
                    f"(původní cíl {flow.original_profit_target:g})."
                )

        # Nový cíl musí zůstat na správné straně vstupu, jinak by obchod ztratil smysl
        if flow.right == "C" and novy_pt <= flow.entry_price:
            raise ValueError("U CALL opce musí PT zůstat nad vstupní cenou podkladu.")
        if flow.right == "P" and novy_pt >= flow.entry_price:
            raise ValueError("U PUT opce musí PT zůstat pod vstupní cenou podkladu.")

        async with self._lock:
            puvodni = flow.profit_target
            flow.profit_target = novy_pt

            if flow.state.is_before_entry:
                await self._apply_pt_before_entry(flow)
            elif flow.exit_trade is not None:
                self._update_exit_conditions(flow)

            nasobek = flow.pt_multiple
            popis = f" ({nasobek:g}× původní cíl)" if nasobek else ""
            self.log_event(
                f"{flow.id}: cíl změněn z {puvodni:,.2f} na {novy_pt:,.2f}{popis}.".replace(",", " ")
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

        chain = await self.ib.option_chain(flow.underlying_contract)
        try:
            novy_strike, option, details = await self._qualify_nearest_option(
                flow.symbol,
                flow.expiration,
                list(chain.strikes),
                flow.profit_target,
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

    def _update_exit_conditions(self, flow: Flow) -> None:
        """Promítne aktuální PT a SL do podmínek zajišťovacího příkazu."""
        trade = flow.exit_trade
        if trade is None or trade.orderStatus.status not in MODIFIABLE_ORDER_STATES:
            self.log_event(
                f"{flow.id}: zajišťovací příkaz nelze upravit "
                f"({trade.orderStatus.status if trade else 'chybí'}) - změna platí jen v přehledu."
            )
            return

        _, pt_more, sl_more = calc.condition_directions(flow.right)
        podminky = [
            self.ib.price_condition(flow.underlying_conid, pt_more, flow.profit_target),
            self.ib.price_condition(flow.underlying_conid, sl_more, flow.stop_loss),
        ]

        order = trade.order
        # Spojka patří k následující podmínce, poslední ji už nepoužije
        posledni = len(podminky) - 1
        for index, podminka in enumerate(podminky):
            podminka.conjunction = "a" if index == posledni else "o"
        order.conditions = podminky

        flow.exit_trade = self.ib.place(flow.option_contract, order)
        flow.touch(f"Zajišťovací příkaz upraven na PT {flow.profit_target:g} / SL {flow.stop_loss:g}.")

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
        zaklad = flow.original_profit_target
        novy_pt = round(flow.entry_price + (zaklad - flow.entry_price) * multiple, 2)

        # Cíl runneru musí ležet na stejné straně vstupu jako hlavní cíl
        if flow.right == "C" and novy_pt <= flow.entry_price:
            raise ValueError("U CALL opce musí cíl runneru zůstat nad vstupní cenou podkladu.")
        if flow.right == "P" and novy_pt >= flow.entry_price:
            raise ValueError("U PUT opce musí cíl runneru zůstat pod vstupní cenou podkladu.")

        async with self._lock:
            byl_aktivni = flow.runner_active
            flow.runner_profit_target = novy_pt
            flow.runner_quantity = runner_q
            # Nově zapnutý runner přebírá aktuální SL obchodu;
            # dál se jeho stop přepíná nezávisle na hlavní části
            if not byl_aktivni:
                flow.runner_stop_loss = flow.stop_loss

            if flow.state == FlowState.EXIT_ARMED:
                if byl_aktivni and flow.runner_trade is not None:
                    self._update_runner_conditions(flow)
                else:
                    self._split_exit_for_runner(flow)

            self.log_event(
                f"{flow.id}: runner {runner_q} ks s cílem {novy_pt:,.2f} "
                f"({multiple:g}× původní cíl).".replace(",", " ")
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
            # Nejprve se ruší příkaz runneru, teprve pak se navyšuje hlavní -
            # obráceně by na okamžik bylo v trhu více kusů, než pozice drží
            self.ib.cancel(flow.runner_trade)
            flow.runner_trade = None
            flow.runner_order_id = None
            flow.runner_profit_target = None
            flow.runner_quantity = 0
            flow.runner_stop_loss = None

            if flow.state == FlowState.EXIT_ARMED and flow.exit_trade is not None:
                trade = flow.exit_trade
                total = flow.held_quantity
                if trade.orderStatus.status in MODIFIABLE_ORDER_STATES:
                    order = trade.order
                    order.totalQuantity = total
                    flow.exit_trade = self.ib.place(flow.option_contract, order)
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
            self.ib.cancel(flow.exit_trade)
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
            self.ib.cancel(flow.runner_trade)
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
            return flow.entry_price
        if rezim == "puvodni":
            # Obchod ze starší verze počáteční SL nezná - stane se jím aktuální
            if not flow.original_stop_loss:
                flow.original_stop_loss = flow.stop_loss
            return flow.original_stop_loss
        raise ValueError(f"Neznámý režim SL '{rezim}'.")

    def _sl_breached(self, flow: Flow, sl: float) -> bool:
        """True, pokud je cena podkladu už na úrovni SL, nebo za ní."""
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
                # tržní prodej zadá smyčka až po potvrzení zrušení příkazu
                flow.main_close_requested = True
                self.ib.cancel(flow.exit_trade)
                flow.touch(
                    f"SL {novy_sl:g} je již dosažen, hlavní část "
                    f"({flow.main_quantity} ks) se prodává trhem."
                )
                self.log_event(f"{flow.id}: {flow.message}")
            else:
                self._update_exit_conditions(flow)
                popis = "break even" if rezim == "be" else "počáteční hodnota"
                self.log_event(f"{flow.id}: SL hlavní části nastaven na {novy_sl:g} ({popis}).")
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
                self.ib.cancel(flow.runner_trade)
                flow.touch(
                    f"SL runneru {novy_sl:g} je již dosažen, runner "
                    f"({flow.runner_quantity} ks) se prodává trhem."
                )
                self.log_event(f"{flow.id}: {flow.message}")
            else:
                self._update_runner_conditions(flow)
                popis = "break even" if rezim == "be" else "počáteční hodnota"
                self.log_event(f"{flow.id}: SL runneru nastaven na {novy_sl:g} ({popis}).")
            self._notify()
            return flow

    def _runner_conditions(self, flow: Flow) -> list:
        """Cenové podmínky prodeje runneru - vlastní cíl i vlastní SL."""
        _, pt_more, sl_more = calc.condition_directions(flow.right)
        return [
            self.ib.price_condition(flow.underlying_conid, pt_more, flow.runner_profit_target),
            self.ib.price_condition(flow.underlying_conid, sl_more, flow.runner_sl),
        ]

    def _split_exit_for_runner(self, flow: Flow) -> None:
        """
        Rozdělí běžící zajišťovací příkaz na hlavní část a runner.
        Hlavní příkaz se nejprve zmenší, teprve pak se zadá příkaz runneru -
        jinak by v trhu na okamžik bylo více kusů, než pozice drží.
        """
        trade = flow.exit_trade
        if trade is None or trade.orderStatus.status not in MODIFIABLE_ORDER_STATES:
            raise ValueError(
                "Zajišťovací příkaz nelze upravit "
                f"({trade.orderStatus.status if trade else 'chybí'}) - runner teď nelze zapnout."
            )

        total = flow.held_quantity
        order = trade.order
        order.totalQuantity = total - flow.runner_quantity
        flow.exit_trade = self.ib.place(flow.option_contract, order)

        runner_order = self.ib.build_exit_order(
            flow.runner_quantity, None, self._runner_conditions(flow), order_ref(flow.id, "runner")
        )
        flow.runner_trade = self.ib.place(flow.option_contract, runner_order)
        flow.runner_order_id = flow.runner_trade.order.orderId

    def _update_runner_conditions(self, flow: Flow) -> None:
        """Promítne nový cíl runneru do podmínek jeho běžícího příkazu."""
        trade = flow.runner_trade
        if trade is None or trade.orderStatus.status not in MODIFIABLE_ORDER_STATES:
            raise ValueError(
                "Příkaz runneru nelze upravit "
                f"({trade.orderStatus.status if trade else 'chybí'})."
            )

        podminky = self._runner_conditions(flow)
        posledni = len(podminky) - 1
        for index, podminka in enumerate(podminky):
            podminka.conjunction = "a" if index == posledni else "o"
        order = trade.order
        order.conditions = podminky
        flow.runner_trade = self.ib.place(flow.option_contract, order)

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

    async def _cancel(self, flow: Flow, close_position: bool = False) -> None:
        """
        Zruší příkazy flow a ukončí jej.

        Drží-li obchod pozici, rozhoduje close_position: buď se pozice uzavře
        tržním příkazem, nebo zůstane otevřená a bez zajištění k ručnímu řízení.
        """
        async with self._lock:
            self._cancel_locked(flow, close_position)

    def _cancel_locked(self, flow: Flow, close_position: bool = False) -> None:
        """Tělo rušení flow - volá se výhradně s již drženým zámkem."""
        self.ib.cancel(flow.entry_trade)
        self.ib.cancel(flow.exit_trade)
        self.ib.cancel(flow.runner_trade)

        v_pozici = flow.fill_price is not None

        if v_pozici and close_position:
            # Prodejní příkaz se zadá až po zrušení nákupního, protože TWS
            # nepovolí oba příkazy na jednom kontraktu současně
            flow.set_state(FlowState.CLOSING, "Zrušeno obchodníkem, pozice se uzavírá trhem.")
        elif v_pozici:
            flow.set_state(
                FlowState.CANCELLED,
                "Flow zrušeno. POZOR: pozice zůstává otevřená v TWS bez zajištění - "
                "prodejní příkaz byl zrušen, uzavřete ji ručně.",
            )
            self._release(flow)
        else:
            flow.set_state(
                FlowState.CANCELLED, "Flow zrušeno před nákupem, příkaz odstraněn z trhu."
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

        # PT a SL nese prodejní příkaz; bez něj se odvodí ze strike a konfigurace
        vystup = prikazy.get(order_ref(flow_id, "exit"))
        dopocteno = False
        if vystup is not None and len(vystup.order.conditions) >= 2:
            profit_target = float(vystup.order.conditions[0].price)
            stop_loss = float(vystup.order.conditions[1].price)
        else:
            dopocteno = True
            profit_target = strike
            stop_loss = calc.default_stop_loss(
                entry_price, profit_target, self.cfg.trading.sl_to_pt_ratio
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
            expiration=kontrakt.lastTradeDateOrContractMonth,
            strike=strike,
        )
        flow.entry_order_id = trade.order.orderId
        flow.entry_limit = valid_price(trade.order.lmtPrice)

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
        # Starší stav počáteční SL nezná - doplní se z aktuálního
        if not flow.original_stop_loss:
            flow.original_stop_loss = flow.stop_loss

        # Uložený stav mohl vzniknout ještě s chybným výpočtem cíle; obchod
        # s nesmyslnými úrovněmi se do trhu vracet nesmí
        if not calc.levels_sane(flow.entry_price, flow.profit_target, flow.stop_loss):
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
        flow.runner_trade = prikazy.get(order_ref(flow.id, "runner"))
        info = pozice.get(option.conId)
        drzeno = int(info.quantity) if info else 0

        self._restore_state(flow, drzeno)
        self.log_event(f"{flow.id}: obnoveno - {flow.state.label}. {flow.message}")

    def _restore_state(self, flow: Flow, drzeno: int) -> None:
        """
        Určí stav obchodu podle toho, co se skutečně nachází v TWS.
        Rozhoduje existence pozice a stav nalezených příkazů, nikoliv uložený zápis.
        """
        vystup = flow.exit_trade
        vstup = flow.entry_trade

        # Uzavírání na pokyn obchodníka pokračuje dál, stav se nepřepisuje
        if flow.state == FlowState.CLOSING:
            flow.touch("Spojení obnoveno, pozice se dál uzavírá.")
            return

        # Pozice je otevřená - rozhoduje stav prodejního příkazu
        if drzeno > 0:
            flow.filled_quantity = drzeno
            if vystup is not None and vystup.orderStatus.status not in DEAD_ORDER_STATES:
                flow.set_state(
                    FlowState.EXIT_ARMED,
                    f"Obnoveno: drženo {drzeno} ks, prodejní příkaz je v TWS.",
                )
            else:
                # Pozice bez zajištění - smyčka prodejní příkazy zadá v dalším
                # průchodu; případný přeživší příkaz runneru se ruší, aby se
                # při novém založení nezdvojil
                if (
                    flow.runner_trade is not None
                    and flow.runner_trade.orderStatus.status in MODIFIABLE_ORDER_STATES
                ):
                    self.ib.cancel(flow.runner_trade)
                    flow.runner_trade = None
                # Rozdělané uzavírání trhem se po restartu nedokončuje naslepo -
                # zajištění se založí znovu a obchodník může uzavření zopakovat
                flow.main_close_requested = False
                flow.runner_close_requested = False
                flow.set_state(
                    FlowState.FILLED,
                    f"Obnoveno: drženo {drzeno} ks bez prodejního příkazu, zajištění se doplní.",
                )
                flow.entry_cancel_requested = True
            return

        # Pozice není a prodejní příkaz byl vyplněn - obchod se uzavřel během výpadku
        if vystup is not None and vystup.orderStatus.status == "Filled":
            flow.exit_fill_price = valid_price(vystup.orderStatus.avgFillPrice)
            flow.exit_reason = self._exit_reason(flow)
            flow.set_state(FlowState.CLOSED, "Obnoveno: pozice byla uzavřena během výpadku.")
            self._release(flow)
            return

        # Nákupní příkaz stále čeká v trhu
        if vstup is not None and vstup.orderStatus.status not in DEAD_ORDER_STATES:
            if vstup.orderStatus.filled > 0:
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

        # Příkaz, který TWS už ruší nebo vyplňuje, se upravovat nesmí
        if flow.entry_trade.orderStatus.status not in MODIFIABLE_ORDER_STATES:
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

        self._place_exit(flow, filled)
        return True

    def _handle_exit(self, flow: Flow) -> bool:
        """
        Stav po zadání výstupních příkazů.

        Hlídá doplnění částečně vyplněného nákupu, prodej hlavní části
        i runneru a uzavření obchodu, jakmile jsou prodány obě části.
        """
        changed = False
        # Do dorovnání hlavního příkazu se počítá jen runner s vlastním
        # příkazem v trhu - runner čekající na doplnění nákupu žádné kusy nedrží
        runner_q = (
            flow.runner_quantity
            if flow.runner_active and flow.runner_trade is not None
            else 0
        )

        # Nákup se mohl doplnit až po zadání výstupu - hlavní příkaz se dorovná
        # (runner má pevné množství, dorovnává se vždy hlavní část)
        if (
            flow.entry_trade is not None
            and flow.exit_trade is not None
            and not flow.main_close_requested
            and flow.exit_trade.orderStatus.status in MODIFIABLE_ORDER_STATES
        ):
            filled = int(flow.entry_trade.orderStatus.filled)
            cilove = max(filled - flow.runner_sold_quantity - runner_q, 0)
            exit_qty = int(flow.exit_trade.order.totalQuantity)
            if filled > flow.filled_quantity or cilove > exit_qty:
                if cilove > exit_qty:
                    order = flow.exit_trade.order
                    order.totalQuantity = cilove
                    flow.exit_trade = self.ib.place(flow.option_contract, order)
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
                    f"se oddělil s cílem {flow.runner_profit_target:g}."
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
        # podmíněného příkazu, zadá se prodej trhem
        if (
            flow.runner_active
            and flow.runner_close_requested
            and flow.runner_fill_price is None
            and (
                flow.runner_trade is None
                or flow.runner_trade.orderStatus.status in DEAD_ORDER_STATES
            )
        ):
            order = self.ib.market_sell_order(
                flow.runner_quantity, order_ref(flow.id, "runner")
            )
            flow.runner_trade = self.ib.place(flow.option_contract, order)
            flow.runner_order_id = flow.runner_trade.order.orderId
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

        runner = flow.runner_trade
        if flow.runner_active and runner is not None:
            if runner.orderStatus.status == "Filled" and flow.runner_fill_price is None:
                # Prodaný runner se zúčtuje do realizovaného výsledku a jeho
                # pole se uvolní - z hlavní části pak lze oddělit další runner
                cena = valid_price(runner.orderStatus.avgFillPrice)
                vysledek_text = ""
                if cena is not None and flow.fill_price is not None:
                    dilci = (cena - flow.fill_price) * flow.runner_quantity * 100
                    flow.runner_realized_pnl += dilci
                    vysledek_text = f", výsledek {dilci:+.2f} USD"
                flow.runner_sold_quantity += flow.runner_quantity
                self.log_event(
                    f"{flow.id}: runner ({flow.runner_quantity} ks) prodán za "
                    f"{cena if cena is not None else '?'}{vysledek_text}."
                )
                flow.runner_profit_target = None
                flow.runner_quantity = 0
                flow.runner_stop_loss = None
                flow.runner_trade = None
                flow.runner_order_id = None
                flow.runner_close_requested = False
                changed = True
            elif (
                runner.orderStatus.status in DEAD_ORDER_STATES
                and flow.runner_fill_price is None
                and not flow.runner_close_requested
            ):
                # Příkaz runneru zmizel mimo aplikaci - jeho kusy se vrací
                # pod hlavní příkaz, aby pozice nezůstala částečně nezajištěná
                if (
                    flow.exit_trade is not None
                    and flow.exit_fill_price is None
                    and flow.exit_trade.orderStatus.status in MODIFIABLE_ORDER_STATES
                ):
                    flow.runner_trade = None
                    flow.runner_order_id = None
                    flow.runner_profit_target = None
                    flow.runner_quantity = 0
                    flow.runner_stop_loss = None
                    order = flow.exit_trade.order
                    order.totalQuantity = flow.held_quantity
                    flow.exit_trade = self.ib.place(flow.option_contract, order)
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

        # --- hlavní část ---
        # Vyžádané uzavření hlavní části trhem - stejný postup jako u runneru
        if (
            flow.main_close_requested
            and flow.exit_fill_price is None
            and (
                flow.exit_trade is None
                or flow.exit_trade.orderStatus.status in DEAD_ORDER_STATES
            )
        ):
            order = self.ib.market_sell_order(flow.main_quantity, order_ref(flow.id, "exit"))
            flow.exit_trade = self.ib.place(flow.option_contract, order)
            flow.exit_order_id = flow.exit_trade.order.orderId
            flow.exit_market_sent = datetime.now()
            flow.exit_market_attempts += 1
            self.log_event(
                f"{flow.id}: hlavní část se prodává trhem ({flow.main_quantity} ks)."
            )
            changed = True

        # Tržní prodej hlavní části, který TWS drží nevyplněný, se zadá znovu
        if flow.main_close_requested and flow.exit_fill_price is None:
            changed |= self._retry_stalled_market_sell(flow, "exit")

        trade = flow.exit_trade
        if trade is None:
            return changed

        if trade.orderStatus.status == "Filled" and flow.exit_fill_price is None:
            flow.exit_fill_price = valid_price(trade.orderStatus.avgFillPrice)
            flow.exit_reason = "ručně" if flow.main_close_requested else self._exit_reason(flow)
            cena = f"{flow.exit_fill_price:g}" if flow.exit_fill_price else "?"
            self.log_event(
                f"{flow.id}: hlavní část ({flow.main_quantity} ks) prodána "
                f"({flow.exit_reason}) za {cena}."
            )
            changed = True

        # Prodejní příkaz zrušený mimo aplikaci
        if (
            trade.orderStatus.status in DEAD_ORDER_STATES
            and flow.exit_fill_price is None
            and not flow.main_close_requested
        ):
            flow.set_state(
                FlowState.ERROR,
                f"Prodejní příkaz byl zrušen v TWS ({trade.orderStatus.status}) - "
                f"pozice je bez zajištění.",
            )
            self.log_event(f"{flow.id}: {flow.message}")
            return True

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
                f"{flow.runner_quantity} ks běží dál s cílem {flow.runner_profit_target:g}."
            )
            changed = True

        return changed

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
        for trade in (flow.entry_trade, flow.exit_trade, flow.runner_trade):
            if trade is not None and trade.orderStatus.status in MODIFIABLE_ORDER_STATES:
                return False

        mnozstvi = flow.held_quantity

        # Prodej se zadává jednou; v dalších průchodech se sleduje jeho vyplnění
        if flow.exit_trade is None or flow.exit_trade.orderStatus.status in DEAD_ORDER_STATES:
            order = self.ib.market_sell_order(mnozstvi, order_ref(flow.id, "exit"))
            flow.exit_trade = self.ib.place(flow.option_contract, order)
            flow.exit_order_id = flow.exit_trade.order.orderId
            flow.exit_market_sent = datetime.now()
            flow.exit_market_attempts += 1
            flow.touch(f"Uzavírám pozici trhem ({mnozstvi} ks).")
            self.log_event(f"{flow.id}: {flow.message}")
            return True

        if flow.exit_trade.orderStatus.status == "Filled":
            flow.exit_fill_price = valid_price(flow.exit_trade.orderStatus.avgFillPrice)
            flow.exit_reason = "ručně"
            pnl = flow.unrealized_pnl
            pnl_text = f", výsledek {pnl:+.2f} USD" if pnl is not None else ""
            cena = f"{flow.exit_fill_price:g}" if flow.exit_fill_price is not None else "?"
            flow.set_state(FlowState.CLOSED, f"Pozice uzavřena obchodníkem za {cena}{pnl_text}.")
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

    def _exit_reason(self, flow: Flow) -> str:
        """Určí, zda pozice skončila na PT nebo SL, podle ceny podkladu při uzavření."""
        price = flow.underlying_price
        if price is None:
            return "PT/SL"
        # Rozhoduje bližší úroveň. Podklad se mezi splněním podmínky a zápisem
        # prodeje stihne pohnout, jednostranné porovnání s PT proto umělo
        # označit ziskový výstup těsně pod cílem jako SL.
        return "PT" if abs(price - flow.profit_target) <= abs(price - flow.stop_loss) else "SL"
