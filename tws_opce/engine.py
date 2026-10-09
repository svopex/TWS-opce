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
from typing import Any
from zoneinfo import ZoneInfo

from . import calc, store
from .config import AppConfig
from .ib_service import IBService, PositionInfo, order_ref, parse_order_ref, valid_price
from .models import (
    RIGHT_LABELS,
    EntryMissedError,
    Flow,
    FlowRequest,
    FlowState,
    format_countdown,
)

log = logging.getLogger(__name__)

# Stavy příkazu v TWS, které znamenají, že příkaz již není v trhu
DEAD_ORDER_STATES = ("Cancelled", "ApiCancelled", "Inactive")

# Stavy, ve kterých lze příkaz v TWS ještě upravit. Příkaz čekající na
# potvrzení zrušení ("PendingCancel") mezi ně nepatří - jeho úprava končí
# hlášením TWS "Order has been cancelled already, too late to replace".
MODIFIABLE_ORDER_STATES = ("PreSubmitted", "Submitted")

# Stavy, ve kterých už příkaz nemůže obchodovat - buď byl zrušen, nebo se
# celý vyplnil. Vše ostatní je v TWS stále živé a může se vyplnit: kromě
# "PreSubmitted" a "Submitted" i "PendingCancel" (zrušení ještě není
# potvrzeno), "PendingSubmit" a "ApiPending". Právě proto se nesmí ověřovat
# jen MODIFIABLE_ORDER_STATES - tržní prodej zadaný předčasně by se s takovým
# příkazem sečetl a prodal by víc kusů, než pozice drží.
SETTLED_ORDER_STATES = DEAD_ORDER_STATES + ("Filled",)

# Kolik strike cen poblíž cíle se nejvýše zkusí ověřit v TWS, než se to vzdá
MAX_STRIKE_ATTEMPTS = 8

# Hlídání vyžádaného tržního prodeje: po jaké době se nevyplněný příkaz
# zruší a zadá znovu a kolik pokusů se nejvýše provede
MARKET_SELL_RETRY_SEC = 30.0
MARKET_SELL_MAX_ATTEMPTS = 5

# Po jaké době bez dokončeného průchodu monitorovací smyčkou se hlásí, že
# smyčka stojí. Běžný průchod trvá zlomky sekundy, delší dotazy do TWS mají
# limit REQUEST_TIMEOUT_SEC - půl minuty už znamená, že něco uvázlo
STALL_ALARM_SEC = 30.0


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
    # Poměr SL:PT použitý k dopočtu chybějící úrovně - z formuláře,
    # nebo (není-li zadán) z konfigurace
    sl_to_pt_ratio: float = 1.0
    # Kompenzace spreadu u SL na opci a její odhadovaná velikost v USD
    # na kontrakt. Skutečná hodnota se určí až ze spreadu při nákupu,
    # náhled s odhadem počítá množství a hlídá strop prémie.
    sl_spread_compensated: bool = False
    sl_spread_usd: float = 0.0
    # Úroveň podkladu, ke které se vybíral strike při PT zadaném ziskem na
    # opci, a z čeho byla odvozena (cena opce / delta / vstupní cena)
    target_level: float | None = None
    target_level_source: str = ""
    delta: float | None = None
    # True, pokud delta nepřišla z TWS a byla dopočítána z ceny opce
    delta_estimated: bool = False
    # Delta opce v okamžiku nákupu, tedy až podklad dosáhne vstupní úrovně.
    # Podle ní se počítá množství i kontrola mezí - delta výše platí pro
    # dnešní cenu podkladu, která bývá od vstupu daleko. None, chybí-li model.
    entry_delta: float | None = None
    option_bid: float | None = None
    option_ask: float | None = None
    # Cena vybrané opce pro model (střed kotace, jinak last/close) a její zdroj
    option_price: float | None = None
    option_price_source: str = ""
    # Odhad ceny, za kterou se opce nakoupí, až podklad dosáhne vstupní úrovně.
    # Slouží ke stropování ztráty zadané na opci a k převodu úrovní zadaných
    # procentem z prémie na USD na kontrakt. None, chybí-li podklad pro model.
    expected_fill_price: float | None = None
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
        # Řídí automatické navazování spojení ve smyčce; ruční odpojení jej vypíná
        self.auto_connect: bool = True
        # Runtime přepínače časovaných funkcí. Konfigurace dává jen výchozí
        # hodnotu - obchodník je za běhu vypíná a zapíná z hlavičky a po
        # restartu se obojí vrací k nastavení ze souboru, aby omylem
        # vypnutá pojistka nepřežila do dalšího dne
        self.pending_cancel_on: bool = cfg.trading.pending_cancel_enabled
        self.auto_close_on: bool = cfg.trading.auto_close_enabled
        # Zjištěná velikost účtu z TWS (používá se při account.use_live_account_size)
        self._live_account_size: float | None = None
        # Uložený stav se z disku čte jen jednou, při prvním spuštění
        self._restored: bool = False
        # Po každém (znovu)připojení je potřeba obchody spárovat s příkazy v TWS.
        # Ztrátu spojení hlásí IBService rovnou, ne až přes polling ve smyčce -
        # jinak by výpadek a obnovení uvnitř jednoho průchodu resynchronizaci
        # vůbec nespustily a obchody by dál držely příkazy z mrtvého spojení
        self._synced: bool = False
        ib.on_disconnected = self._handle_disconnect
        # Opční pozice na účtu, které aplikace neřídí
        self.unmanaged: dict[int, PositionInfo] = {}
        self._unmanaged_checked: float = 0.0
        self._account_checked: float = 0.0
        # Čas posledního dokončeného průchodu smyčkou - podle něj se pozná,
        # že monitoring opravdu běží a nikde neuvázl
        self._last_tick: float = 0.0
        # Spuštění smyčky - výchozí bod měření, když se žádný průchod ještě
        # nedokončil (například uvázla hned první obnova)
        self._started_at: float = 0.0
        # Co smyčka právě dělá, pro hlášení, na čem uvázla
        self._loop_step: str = ""
        # Hlídač smyčky a to, zda už ohlásil, že smyčka stojí
        self._watchdog: asyncio.Task | None = None
        self._stall_warned: bool = False
        # Už ohlášená varování, která se nemají opakovat každý průchod smyčkou,
        # s klíčem "flow_id:...": část pozice, z jejíž dvojice prodejních
        # příkazů zmizel jeden ("flow_id:part"), a nepovinná akce odložená
        # kvůli rozpočtu OER ("flow_id:oer:akce"). Předpona id obchodu
        # dovoluje uklidit varování odebraného obchodu najednou. Bez předpony
        # je jen "restore" - obnova, které TWS nevydal příkazy ani pozice
        self._warned: set[str] = set()
        # Překročení rozpočtu OER už bylo ohlášeno - hlásí se změna stavu
        self._oer_over_warned: bool = False

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
        # S obchody se ukládají i dnešní počítadla OER, aby je restart nevynuloval
        store.save(
            list(self.flows.values()), self.cfg.state.file, order_stats=self.ib.oer.to_dict()
        )

    def _handle_disconnect(self) -> None:
        """
        Reakce na ztrátu spojení ohlášenou IBService.

        Objekty Trade ze zaniklého spojení se už neaktualizují a odběry tržních
        dat jsou pryč, takže se obchody musí po novém spojení znovu spárovat
        se skutečností v TWS. Bez toho by hlídání jen zdánlivě běželo.
        """
        self._synced = False

    def _notify(self) -> None:
        """
        Uloží stav obchodů na disk.
        Rozhraní překresluje samo v pravidelném intervalu (ui.timer), takže
        se odsud o změně nijak neinformuje.
        """
        self._persist()

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
        stari = self._tick_age()
        if stari is None or not self.ib.connected or not self._last_tick:
            return False

        # Tolerance několika period; delší prodleva znamená, že smyčka vázne
        limit = max(3 * self.cfg.engine.poll_interval_sec, 5.0)
        return stari < limit

    def _tick_age(self) -> float | None:
        """
        Sekundy od posledního dokončeného průchodu smyčkou (před prvním od
        jejího spuštění), nebo None, pokud smyčka neběží.
        """
        if self._task is None or self._task.done():
            return None
        return time.monotonic() - (self._last_tick or self._started_at)

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
        Flow seřazená pro zobrazení v tabulce do čtyř sekcí:
        1) obchody držící pozici (Nakoupeno, výstup aktivní, Uzavírá se),
        2) ostatní běžící, tedy ty před nákupem, 3) dnešní ukončené,
        4) starší ukončené sestupně podle data (nejnovější den nahoře).

        Nakoupené patří nahoru, protože jsou to jediné obchody s penězi
        v trhu - jejich stav se hlídá nejčastěji. Uvnitř každé sekce - a u
        starších uvnitř každého dne - se řadí abecedně podle tickeru, stejný
        ticker pak od nejnovějšího obchodu.
        """
        dnes = datetime.now().date()

        def klic(flow: Flow) -> tuple[int, int, str, float]:
            den = flow.created_at.date()
            if flow.state.is_active:
                sekce = 1 if flow.state.is_before_entry else 0
            elif den == dnes:
                sekce = 2
            else:
                sekce = 3
            # Datum se uplatní jen u starších obchodů, jinde je pořadí dané
            # abecedou; záporný ordinál dá sestupné pořadí dnů
            poradi_dne = -den.toordinal() if sekce == 3 else 0
            return (sekce, poradi_dne, flow.symbol, -flow.created_at.timestamp())

        return sorted(self.flows.values(), key=klic)

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
        otm_from: float | None = None,
    ) -> tuple[float, Any, Any]:
        """
        Ověří v TWS opční kontrakt se strike nejblíže cílové ceně.

        Opční řetězec vrací strike ceny pro všechny expirace dohromady,
        takže nejbližší strike nemusí být pro zvolenou expiraci vůbec
        obchodovatelný (např. půlbodové strike jen u týdenních expirací).
        Proto se strike zkoušejí v pořadí podle vzdálenosti od cíle,
        dokud se některý neověří. Vrací trojici (strike, kontrakt, detaily).

        Parametr otm_from udává cenu, vůči které se posuzuje, zda strike leží
        v penězích. Je-li zadán, mají při stejné vzdálenosti od cíle přednost
        strike mimo peníze - rastr bývá rovnoměrný, takže náhrada za nedostupný
        strike je vždy remíza mezi oběma sousedy a bez tohoto pravidla by
        vyhrál ten nižší, u CALL tedy kontrakt v penězích.
        """

        def poradi(strike: float) -> tuple:
            """Klíč řazení kandidátů: vzdálenost od cíle, pak strana mimo peníze."""
            if otm_from is None:
                return (abs(strike - target), strike)
            v_penezich = strike < otm_from if right == "C" else strike > otm_from
            return (abs(strike - target), v_penezich, strike)

        kandidati = sorted(strikes, key=poradi)[:MAX_STRIKE_ATTEMPTS]
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
        sl_spread_compensated: bool = False,
        sl_to_pt_ratio: float | None = None,
        max_spread_pct: float | None = None,
    ) -> Preview:
        """
        Připraví zadání obchodu: načte cenu podkladu, určí typ opce, expiraci,
        strike podle PT, dopočítá chybějící úroveň a doporučené množství
        kontraktů. Nezadává žádný příkaz do trhu.

        PT a SL jsou buď ceny podkladu, nebo - při vypnutém přepínači
        "na podkladu" - zisk, resp. ztráta v USD na jeden kontrakt. Stačí
        zadat jednu z úrovní: chybějící se dopočítá z poměru SL:PT
        (SL z PT, nebo PT ze SL). Poměr přebírá sl_to_pt_ratio z formuláře;
        bez něj (None, nebo nekladná hodnota) platí hodnota z konfigurace.

        sl_spread_compensated připočte k SL na opci spread opce, aby zadaná
        hodnota odpovídala potřebnému pohybu trhu; náhled používá spread
        z aktuální kotace, skutečný obchod ten zaplacený při nákupu.

        max_spread_pct je limit spreadu, se kterým obchod poběží (z formuláře;
        bez něj platí konfigurace). Odhad kompenzace SL se jím stropuje - nad
        limitem se nenakupuje, takže širší spread obchod nezaplatí.
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
            # Kompenzace má smysl jen u SL zadaného na opci
            sl_spread_compensated=sl_spread_compensated and not sl_on_underlying,
            # Nekladný poměr by dopočet rozbil (dělení nulou, obrácené znaménko),
            # proto se v takovém případě sahá po hodnotě z konfigurace
            sl_to_pt_ratio=(
                sl_to_pt_ratio
                if sl_to_pt_ratio is not None and sl_to_pt_ratio > 0
                else self.cfg.trading.sl_to_pt_ratio
            ),
        )

        # Limit spreadu, se kterým obchod poběží. Formulář ho posílá s sebou,
        # aby se náhled počítal podle téhož čísla, jaké obchod dostane
        limit_spreadu = (
            max_spread_pct
            if max_spread_pct is not None and max_spread_pct > 0
            else self.cfg.trading.max_spread_pct
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

            # Typ opce určuje poloha vstupu vůči aktuální ceně podkladu.
            # Bez ceny (mimo obchodní hodiny, chybějící odběr dat) by dosazení
            # vstupu za referenci znamenalo determine_right(vstup, vstup) = vždy
            # CALL, takže by se každé short zadání připravilo obráceně. Směr
            # proto v takovém případě dodá poloha zadaných úrovní vůči vstupu
            if preview.current_price is not None:
                preview.right = calc.determine_right(preview.current_price, entry_price)
            else:
                smer = calc.intended_right(
                    entry_price, profit_target, stop_loss, pt_on_underlying, sl_on_underlying
                )
                if smer is None:
                    # Obě úrovně na opci a k tomu neznámá cena podkladu - z čeho
                    # směr určit, není; tichý odhad by koupil opačnou opci
                    raise ValueError(
                        f"Z TWS nedorazila cena podkladu {symbol} a obě úrovně jsou "
                        f"zadané na opci - typ opce (CALL/PUT) nelze určit. "
                        f"Zkontrolujte odběr tržních dat, nebo zadejte PT či SL na podkladu."
                    )
                preview.right = smer

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

            # Cílová úroveň podkladu: při PT na podkladu je to přímo PT, při PT
            # zadaném ziskem na opci úroveň odvozená z ceny opce. Do náhledu
            # patří vždy, jako cíl pro strike jen v režimu "target"
            if pt_on_underlying:
                cilova_uroven = profit_target
            else:
                cilova_uroven = self._target_level_for_option_pt(
                    preview, entry_price, profit_target, referencni
                )

            # Strike podle nastaveného režimu - odsazený od vstupu mimo peníze,
            # nejbližší vstupní ceně, nebo nejbližší cílové úrovni
            cil_strike = self._strike_target(
                entry_price, cilova_uroven, preview.right, list(chain.strikes)
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
                    symbol,
                    expiration,
                    list(chain.strikes),
                    cil_strike,
                    preview.right,
                    chain.tradingClass,
                    # V režimu "target" leží cíl na PT a náhrada se řídí jen
                    # vzdáleností jako dosud; jinak se drží strana mimo peníze
                    otm_from=None if self.cfg.strike.mode == "target" else entry_price,
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
            # Kotace, delta a odhad nákupní ceny; z nich pak SL a množství.
            # Stejné dva kroky používá i přepočet čekajícího obchodu po
            # otevření burzy, proto jsou vyčleněné do samostatných metod
            used_delta = self._load_option_market(preview, entry_price, limit_spreadu)
            self._derive_levels(
                preview, entry_price, profit_target, stop_loss, used_delta, limit_spreadu
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

    def _load_option_market(
        self, preview: Preview, entry_price: float, limit_spreadu: float
    ) -> float:
        """
        Načte do náhledu tržní data vybrané opce (kotace, cena pro model,
        spread, delta) a odhad nákupní ceny při vstupu; vrací deltu, se
        kterou se dál počítá množství. limit_spreadu (Max. spread v %)
        stropuje spread započtený do odhadu nákupní ceny.

        Sdílí ji příprava zadání i přepočet čekajícího obchodu po otevření
        burzy, aby obě cesty došly ke stejným číslům. Kontrakt opce musí být
        v náhledu už vybraný a odebíraný.
        """
        option = preview.option
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

        # Delta z TWS i dopočet z ceny opce platí pro dnešní cenu podkladu,
        # jenže opce se kupuje teprve na vstupní úrovni. Leží-li vstup od
        # trhu daleko, je dnešní delta výrazně nižší - rozhoduje proto ta
        # při vstupu, dnešní zbývá jen tam, kde model spočítat nelze
        preview.entry_delta = self._entry_delta(preview, entry_price)
        rozhodna_delta = preview.entry_delta if preview.entry_delta is not None else delta

        if rozhodna_delta is None:
            preview.warnings.append(
                f"Deltu opce se nepodařilo získat ani dopočítat - množství je spočítáno "
                f"s náhradní hodnotou {self.cfg.trading.default_delta:g}."
            )
        used_delta = (
            rozhodna_delta if rozhodna_delta is not None else self.cfg.trading.default_delta
        )

        # Delta vybrané opce se kontroluje proti mezím z konfigurace - není
        # to kritérium výběru, jen upozornění na kontrakt mimo obvyklé pásmo
        varovani_delta = self._delta_warning(rozhodna_delta)
        if varovani_delta is not None:
            preview.warnings.append(varovani_delta)

        # Odhad nákupní ceny opce - čistý výpočet z už načtených kotací
        preview.expected_fill_price = self._expected_fill_price(
            preview, entry_price, limit_spreadu
        )

        return used_delta

    def _derive_levels(
        self,
        preview: Preview,
        entry_price: float,
        profit_target: float,
        stop_loss: float | None,
        used_delta: float,
        limit_spreadu: float,
    ) -> None:
        """
        Dopočítá z načtených tržních dat zbytek náhledu: odhad kompenzace SL,
        chybějící SL podle poměru SL:PT a množství z riskované částky; k tomu
        varování na strop prémie, závěrečnou cenu a spread nad limitem.

        Zadaný stop_loss má přednost, None znamená dopočítat. Druhý krok
        přípravy zadání, sdílený s přepočtem obchodu po otevření burzy.
        """
        preview.profit_target = profit_target
        bid, ask = preview.option_bid, preview.option_ask

        # Odhad kompenzace SL: spread vybrané opce v USD na kontrakt, omezený
        # limitem (viz _spread_cap). Skutečně se připočte až spread zaplacený
        # při nákupu; nestropovaný by množství zbytečně zmenšil
        if preview.sl_spread_compensated:
            preview.sl_spread_usd = calc.capped_spread_usd(
                bid,
                ask,
                self._spread_cap(limit_spreadu),
                preview.expected_fill_price,
                self.cfg.trading.cheap_option_rule,
            )

        # SL buď zadaný uživatelem, nebo dopočtený podle poměru z konfigurace;
        # při smíšeném režimu PT a SL se převádí přes cenu opce, proto až teď,
        # kdy jsou k dispozici kotace vybrané opce
        preview.stop_loss = (
            stop_loss
            if stop_loss is not None
            else self._default_stop_loss(preview, entry_price, profit_target, used_delta)
        )

        # SL na opci nad zaplacenou prémií znamená stop na nejnižší možné ceně,
        # který pozici prakticky nechrání - na to se musí upozornit už
        # v náhledu, sám příkaz by později vypadal v pořádku. Kompenzovaný SL
        # zvětšuje ztrátu na kontrakt, proto se kontroluje i se spreadem
        if not preview.sl_on_underlying and preview.expected_fill_price is not None:
            varovani = self._premium_cap_text(
                preview.stop_loss + preview.sl_spread_usd,
                preview.expected_fill_price,
                preview.min_tick,
                odhad=True,
            )
            if varovani is not None:
                preview.warnings.append(varovani)

        preview.quantity = self._quantity_for_risk(
            preview, entry_price, used_delta, self.risk_amount
        )

        # Závěrečná cena je jediná dostupná mimo obchodní hodiny; pohnul-li se
        # mezitím podklad (typicky pre-market gap), vyjde z ní nesmyslná
        # implikovaná volatilita a s ní i dopočítané úrovně a množství
        if preview.option_price_source == "close":
            preview.warnings.append(
                "Opce nemá aktuální kotace, model počítá ze závěrečné ceny - "
                "dopočítané úrovně i doporučené množství mohou být nepřesné."
            )

        # Spread nad limitem se hlásí jen tehdy, když ho nepokryje ani
        # výjimka pro levné opce - jinak by náhled varoval zbytečně
        if calc.spread_over_limit(bid, ask, limit_spreadu, self.cfg.trading.cheap_option_rule):
            preview.warnings.append(
                f"Aktuální spread {preview.spread_pct:.2f} % překračuje limit "
                f"{limit_spreadu:g} %."
            )

    def _quantity_for_risk(
        self, preview: Preview, entry_price: float, used_delta: float, riziko: float
    ) -> int:
        """
        Množství kontraktů pro riskovanou částku `riziko` nad úrovněmi náhledu.

        Při SL na podkladu se ztráta na kontrakt odhaduje přes deltu, při SL
        na opci je zadaná přímo v USD (včetně kompenzace o spread) a stropuje
        se zaplacenou prémií - stop nemůže klesnout pod jeden tik. Částku
        lze předat nižší, než je riziko na obchod; průběžný přepočet tak
        zjišťuje, zda by zvýšení množství obstálo i s rezervou.
        """
        trading = self.cfg.trading
        if preview.sl_on_underlying:
            return calc.suggest_quantity(
                riziko,
                entry_price,
                preview.stop_loss,
                used_delta,
                trading.min_quantity,
                trading.max_quantity,
            )
        ztrata = preview.stop_loss + preview.sl_spread_usd
        if preview.expected_fill_price is not None:
            ztrata = min(
                ztrata, calc.max_option_loss(preview.expected_fill_price, preview.min_tick)
            )
        return calc.suggest_quantity_for_loss(
            riziko, ztrata, trading.min_quantity, trading.max_quantity
        )

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

    def _strike_target(
        self, entry_price: float, cilova_uroven: float, right: str, strikes: list[float]
    ) -> float:
        """
        Cena, ke které se vybírá strike, podle konfigurace strike.mode.

        otm_offset vrací strike odsazený od vstupní ceny na stranu mimo peníze
        (počet kroků rastru určuje strike.otm_steps), atm nejbližší strike ke
        vstupu a target cílovou úroveň - tedy tam, kam má cena dojít.
        Vrácená hodnota jde dál do kvalifikace kontraktu, která z ní vybere
        nejbližší v TWS obchodovatelný strike.
        """
        mode = self.cfg.strike.mode
        if mode == "target":
            return cilova_uroven

        # atm je odsazení o nula kroků, tedy nejbližší strike ke vstupní ceně
        steps = self.cfg.strike.otm_steps if mode == "otm_offset" else 0
        strike = calc.otm_strike(strikes, entry_price, right, steps)
        # Prázdný řetězec řeší až kvalifikace kontraktu vlastní chybou
        return strike if strike is not None else entry_price

    def _delta_warning(self, delta: float | None) -> str | None:
        """
        Upozornění, když delta vybrané opce vypadne z mezí v konfiguraci.

        Porovnává se absolutní hodnota, protože u PUT je delta záporná.
        Mez nastavená na nulu se nekontroluje, stejně jako chybějící delta -
        na tu upozorňuje samostatné varování.
        """
        if delta is None:
            return None

        velikost = abs(delta)
        mez_min = self.cfg.strike.delta_warn_min
        mez_max = self.cfg.strike.delta_warn_max

        if mez_min > 0 and velikost < mez_min:
            return (
                f"Delta vybrané opce {velikost:.2f} je pod hranicí {mez_min:g} - opce leží "
                f"daleko mimo peníze a z pohybu podkladu se zhodnotí jen málo."
            )
        if mez_max > 0 and velikost > mez_max:
            return (
                f"Delta vybrané opce {velikost:.2f} je nad hranicí {mez_max:g} - opce leží "
                f"hluboko v penězích a stojí víc, než je pro pákový efekt potřeba."
            )
        return None

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
        PT podle zvoleného poměru SL:PT, když obchodník zadal jen SL.

        Ve stejném režimu je to prostý podíl: na podkladu vzdálenost SL od
        vstupu dělená poměrem (na opačnou stranu), na opci ztráta v USD
        dělená poměrem. Při smíšeném režimu se převádí přes cenu referenční
        opce: buď se hledá úroveň podkladu, kde opce vydělá SL/poměr USD,
        nebo se ztráta na SL podkladu přepočte do USD a vydělí poměrem.
        """
        pomer = preview.sl_to_pt_ratio
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
        SL podle zvoleného poměru SL:PT, když jej uživatel nezadal.

        Ve stejném režimu jako PT jde o prostý násobek: na podkladu vzdálenost
        od vstupu, na opci zisk v USD. Při smíšeném režimu se zisk na PT
        převádí mezi podkladem a cenou opce stejným modelem jako sloupec
        "Zisk na PT" (implikovaná volatilita z aktuální ceny opce); bez
        kotací lineárně přes deltu.
        """
        pomer = preview.sl_to_pt_ratio
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

    def _expected_fill_price(
        self, preview: Preview, entry_price: float, limit_spreadu: float
    ) -> float | None:
        """
        Odhad nákupní ceny opce ve chvíli, kdy podklad dosáhne vstupní úrovně.

        Slouží ke stropování ztráty zadané na opci zaplacenou prémií: SL může
        být větší než celá prémie, stop pak stojí na nejnižší možné ceně
        a obchod nemůže ztratit víc. Nakupuje se u ASKu, proto se
        k modelovému středu přičítá půl spreadu, nejvýš do limitu (viz
        _spread_cap). Bez použitelného modelu vrací None.
        """
        if not preview.option_price or preview.current_price is None:
            return None
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
            return None
        if preview.option_bid and preview.option_ask:
            # Limit se měří proti modelovému středu při vstupu; strop vychází
            # v USD na kontrakt, na cenu opce se převádí zpět
            spread = calc.capped_spread_usd(
                preview.option_bid,
                preview.option_ask,
                self._spread_cap(limit_spreadu),
                cena,
                self.cfg.trading.cheap_option_rule,
            )
            cena += spread / calc.OPTION_MULTIPLIER / 2.0
        return cena

    def _spread_cap(self, limit_spreadu: float) -> float | None:
        """
        Limit spreadu v %, kterým se stropuje spread v odhadech nákupu, nebo
        None bez stropu. Nad limitem se nenakupuje - příkaz se do trhu nezadá
        a nevyplněný se z něj stáhne - takže širší spread obchod nezaplatí.
        Vypnuté trading.cancel_on_spread_breach strop ruší: příkaz pak v trhu
        po rozšíření spreadu zůstává a vyplnit se může za jakýkoliv.
        """
        return limit_spreadu if self.cfg.trading.cancel_on_spread_breach else None

    def _entry_delta(self, preview: Preview, entry_price: float) -> float | None:
        """
        Delta opce ve chvíli, kdy podklad dosáhne vstupní úrovně.

        Delta z TWS i dopočet z ceny opce popisují dnešní stav, jenže obchod
        nakupuje teprve na vstupní úrovni. U zadání vzdáleného od trhu je
        rozdíl zásadní: opce hluboko mimo peníze má dnes deltu pod 0,10,
        u vstupu ale klidně 0,50 - a doporučené množství i kontrola mezí by
        z té dnešní vyšly úplně mimo. Model je stejný jako u odhadu nákupní
        ceny: z ceny opce implikovaná volatilita a s ní delta pro vstupní
        úroveň. Bez použitelného modelu vrací None.
        """
        if not preview.option_price or preview.current_price is None:
            return None

        return calc.project_delta(
            preview.option_price,
            preview.current_price,
            entry_price,
            preview.strike,
            preview.expiration,
            self.cfg.trading.risk_free_rate_pct,
            preview.right,
        )

    @staticmethod
    def _premium_cap_text(
        loss_usd: float, fill_price: float, min_tick: float, odhad: bool
    ) -> str | None:
        """
        Varování pro případ, že ztráta zadaná na opci převyšuje zaplacenou
        prémii. Stop pak stojí na jednom tiku a pozici prakticky nechrání -
        spustí se až u téměř bezcenné opce. Vrací None, vejde-li se SL
        do prémie. odhad=True znamená cenu odhadnutou v náhledu, jinak jde
        o skutečnou nákupní cenu.
        """
        strop = calc.max_option_loss(fill_price, min_tick)
        if loss_usd <= strop + 0.005:
            return None
        tick = min_tick if min_tick and min_tick > 0 else 0.01
        premie = "odhadovanou prémii" if odhad else "zaplacenou prémii"
        sloveso = "bude stát" if odhad else "stojí"
        return (
            f"SL {loss_usd:g} USD na kontrakt převyšuje {premie} "
            f"{fill_price * calc.OPTION_MULTIPLIER:.0f} USD (cena opce {fill_price:.2f}) - "
            f"stop {sloveso} na nejnižší možné ceně {tick:g} a pozici prakticky nechrání, "
            f"ztratit lze až {strop:.2f} USD na kontrakt."
        )

    def _option_stop_price(self, flow: Flow, sl_level: float) -> float:
        """
        Stop cena prodejního příkazu na opci pro ztrátu v USD na kontrakt.
        Převyšuje-li ztráta zaplacenou prémii, stop skončí na jednom tiku -
        do průběhu se zapíše varování, protože samotný příkaz vypadá v pořádku.
        """
        varovani = self._premium_cap_text(sl_level, flow.fill_price, flow.min_tick, odhad=False)
        if varovani is not None:
            self.log_event(f"{flow.id}: POZOR - {varovani}")
        return calc.option_loss_stop(flow.fill_price, sl_level, flow.min_tick)

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
            #
            # Směr uvedený přímo v zadání má přednost - hromadné načtení ze
            # souboru jej zná (cíl pod vstupem = short) i tehdy, když jsou obě
            # úrovně na opci a z čísel se odvodit nedá. Bez něj by o typu opce
            # rozhodla okamžitá poloha ceny podkladu, takže by se ze short
            # obchodu po poklesu ceny pod vstup tiše stal long CALL
            zamer = request.intended_right or calc.intended_right(
                request.entry_price,
                request.profit_target,
                request.stop_loss,
                request.pt_on_underlying,
                request.sl_on_underlying,
            )
            if zamer is not None and zamer not in RIGHT_LABELS:
                raise ValueError(
                    f"Neplatný směr obchodu {zamer!r} - očekává se 'C' (long), "
                    f"nebo 'P' (short)."
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
                request.sl_spread_compensated,
                request.sl_to_pt_ratio,
                request.max_spread_pct,
            )

            # Propásnutý vstup se hlásí dřív než ostatní kontroly, jinak by
            # uživatel dostal matoucí hlášku o poloze PT vůči vstupu.
            # Liší-li se zamýšlený směr od typu opce odvozeného z aktuální
            # ceny, cena už vstupní úroveň překonala a obchod ujel.
            if zamer is None:
                zamer = preview.right
            elif zamer != preview.right:
                if preview.current_price is not None:
                    smer = "nad" if zamer == "C" else "pod"
                    raise EntryMissedError(
                        f"Cena podkladu {preview.current_price:g} je již {smer} vstupem "
                        f"{request.entry_price:g}"
                    )
                # Bez ceny podkladu vyšel typ opce z polohy zadaných úrovní.
                # Odporuje-li směru ze zadání, jde o protichůdná čísla a tichý
                # výběr jedné z možností by koupil opačnou opci
                raise ValueError(
                    f"Zadaný směr obchodu ({RIGHT_LABELS[zamer]}) neodpovídá poloze "
                    f"úrovní vůči vstupu {request.entry_price:g}, ze které vychází "
                    f"{RIGHT_LABELS[preview.right]} - zkontrolujte zadání."
                )

            # Příprava čeká na odpovědi z TWS a monitorovací smyčka mezitím
            # běží dál - obchod, který byl na začátku ještě před vstupem, se
            # během čekání mohl vyplnit. Nahrazovaný obchod se proto vyhledá
            # a ověří znovu, jinak by se rušil (a z přehledu mizel) obchod
            # s právě otevřenou pozicí, která by zůstala bez zajištění
            bezici = overit_bezici(zamer)

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

            # Kontrola propásnutého vstupu podle minutových svíček - jen když
            # zadání přineslo okamžik, od kterého se procházejí (dialog načtení
            # pozic ze souboru). Překročený vstup zadání odmítne stejně jako
            # kontrola směru výše: obchod nevznikne a v přehledu se neobjeví.
            # Jako jediná z kontrol se ptá TWS, proto se řadí až za ty levné.
            # Selhání dotazu zadání nezastaví - obchod dál hlídá živá cena
            # podkladu, jen se to zapíše do logu
            if request.entry_cross_since is not None:
                try:
                    propasnuti = await self.entry_crossed(
                        preview.underlying,
                        preview.right,
                        request.entry_price,
                        request.entry_cross_since,
                    )
                except Exception as exc:
                    log.warning("Kontrola svíček %s selhala: %s", symbol, exc)
                    self.log_event(
                        f"{symbol}: kontrola propásnutého vstupu podle svíček se "
                        f"nezdařila ({exc}) - obchod se zadává bez ní."
                    )
                else:
                    if propasnuti is not None:
                        raise EntryMissedError(propasnuti)

            # Čekající obchod stejného směru se nahrazuje až teď, kdy nové
            # zadání prošlo všemi kontrolami - kdyby dřív selhalo, původní
            # obchod by byl zrušený a žádný nový by nevznikl
            volba_nahrazeneho: tuple[float | None, int | None] | None = None
            if bezici is not None:
                # Runner nastavený na čekajícím obchodu nesmí nahrazením tiše
                # zaniknout - nové zadání bez vlastní volby převezme tu jeho
                volba_nahrazeneho = (
                    bezici.auto_runner_multiple
                    if bezici.auto_runner_multiple is not None
                    else bezici.runner_multiple,
                    bezici.auto_runner_min_quantity,
                )
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
                sl_spread_compensated=preview.sl_spread_compensated,
                sl_spread_capped=self.cfg.trading.cancel_on_spread_breach,
                # Odhad nákupní ceny, ze kterého náhled stropoval spread pro
                # množství - přehled z něj stropuje odhad ztráty na SL
                expected_fill_price=preview.expected_fill_price,
                # Výjimka z limitu spreadu pro levné opce podle konfigurace
                cheap_rule=self.cfg.trading.cheap_option_rule,
                # Jednotka zadání úrovní si jede s obchodem, aby ji formulář
                # při načtení obchodu nabídl znovu; totéž platí pro prvotní
                # úroveň a poměr, kterým se ta druhá dopočítala
                pt_in_premium=request.pt_in_premium,
                sl_in_premium=request.sl_in_premium,
                premium_base=request.premium_base,
                primary_level=request.primary_level,
                # Přepočet po otevření burzy si obchod nese s sebou - dialog,
                # který ho zadal, může být dávno zavřený
                refresh_after_open_sec=request.refresh_after_open_sec,
                refresh_interval_sec=request.refresh_interval_sec,
                # Automatický runner ze zadání - podle něj se runner nastaví
                # teď i po každém přepočtu množství
                auto_runner_multiple=request.runner_multiple,
                auto_runner_min_quantity=request.runner_min_quantity,
                runner_quantity_pct=request.runner_quantity_pct,
                # Z náhledu, ne ze zadání - ten už má doplněnou hodnotu
                # z konfigurace pro případ nevyplněného pole RRR
                sl_to_pt_ratio=preview.sl_to_pt_ratio,
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

            # Zadání bez volby runneru převezme volbu nahrazeného obchodu;
            # runner podle ní zapne _sync_runner už na úrovních nového zadání
            if (
                flow.auto_runner_multiple is None
                and volba_nahrazeneho is not None
                and volba_nahrazeneho[0] is not None
            ):
                flow.auto_runner_multiple, flow.auto_runner_min_quantity = volba_nahrazeneho
                self.log_event(
                    f"{flow.id}: volba runneru ({flow.auto_runner_multiple:g}× původní "
                    f"cíl) převzata z nahrazeného obchodu."
                )
            self._sync_runner(flow, "při založení")

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
            if flow.spread_over_limit():
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

    def _runner_pct(self, flow: Flow) -> float:
        """
        Procento pozice pro velikost runneru: přednost má volba ze zadání
        obchodu (pole Runner [%]), jinak platí trading.runner_quantity_pct.
        """
        if flow.runner_quantity_pct is not None:
            return flow.runner_quantity_pct
        return self.cfg.trading.runner_quantity_pct

    def _sync_runner(self, flow: Flow, kdy: str) -> None:
        """
        Srovná runner čekajícího obchodu s automatickou volbou a aktuálním
        množstvím - při založení i po každém přepočtu množství (kdy je slovo
        pro log). Kladný násobek runner zapíná pozici s množstvím alespoň
        auto_runner_min_quantity a víc kontraktů, než runner sám zabírá
        (procento pozice ze zadání, jinak z konfigurace); nula ho nechává
        vypnutý; obchod bez volby se nemění. Důvod nezapnutého chtěného
        runneru zůstává v runner_skip_reason pro rozhraní.
        """
        nasobek = flow.auto_runner_multiple
        if nasobek is None:
            return
        minimum = flow.auto_runner_min_quantity or 1
        # Velikost runneru je procento pozice, takže se s množstvím obchodu mění
        runner_q = calc.runner_quantity(flow.quantity, self._runner_pct(flow))

        # Proč runner nebude; None znamená zapnout
        if not nasobek:
            duvod = "volba runner nepoužívá"
        elif flow.quantity < minimum:
            duvod = f"množství {flow.quantity} ks je pod minimem {minimum} ks"
        elif flow.quantity <= runner_q:
            duvod = f"množství {flow.quantity} ks na něj nestačí"
        else:
            duvod = None

        byl = flow.runner_active
        flow.runner_skip_reason = duvod if nasobek and duvod else None
        if duvod is not None:
            if byl:
                flow.clear_runner()
                self.log_event(f"{flow.id}: runner {kdy} vypnut - {duvod}.")
            # Nezapnutý chtěný runner se hlásí jen při založení - průběžný
            # přepočet by totéž opakoval každou půlminutu
            elif nasobek and kdy == "při založení":
                self.log_event(f"{flow.id}: runner nezapnut - {duvod}.")
            return

        cil = flow.scaled_target(nasobek)
        flow.set_runner_levels(cil, runner_q)
        if not byl:
            self.log_event(
                f"{flow.id}: runner {runner_q} ks zapnut {kdy}, cíl "
                f"{flow.level_text('pt', cil)} ({nasobek:g}× původní cíl)."
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

        # Implikovaná volatilita se odvozuje jednou pro celý přepočet. Vstupy
        # (cena opce, podklad, strike, expirace, sazba) jsou u všech úrovní
        # stejné, liší se jen cílová cena podkladu - project_option_price by
        # ji hledala znovu pro každou úroveň, každý obchod a každý průchod
        # smyčky, a to padesáti půleními Black-Scholese pokaždé
        sigma: float | None = None
        roky = calc.years_to_expiry(flow.expiration)
        if aktualni and podklad and aktualni > 0 and podklad > 0:
            sigma = calc.implied_volatility(
                aktualni, podklad, flow.strike, roky, sazba / 100.0, flow.right
            )

        def cena_pri(uroven: float) -> float | None:
            """Odhad ceny opce, až podklad dosáhne dané úrovně."""
            if sigma is None:
                return None
            return calc.black_scholes_price(
                uroven, flow.strike, roky, sazba / 100.0, sigma, flow.right
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
        # Čeká-li SL na kompenzaci spreadu, počítá se ztráta i s jejím odhadem -
        # sloupec pak ukazuje riziko, které obchod ponese po nákupu
        ztrata_sl = vysledek_na_kontrakt(
            flow.sl_with_pending(flow.stop_loss), flow.sl_on_underlying, False
        )
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
            ztrata_runner = vysledek_na_kontrakt(
                flow.sl_with_pending(flow.runner_sl), flow.sl_on_underlying, False
            )
            if ztrata_runner is None:
                ztrata_runner = ztrata_sl
            flow.expected_loss += ztrata_runner * runner_q

    def _place_entry(self, flow: Flow) -> bool:
        """
        Zadá nákupní příkaz s cenovou podmínkou na dosažení vstupní ceny podkladu.
        Vrací False, pokud příkaz zatím zadat nelze - o zadání se pokusí další průchod smyčkou.
        """
        # Uvnitř rušicího či uzavíracího okna by příkaz do TWS šel jen proto,
        # aby ho následující průchod smyčky hned zase odstranil. Stráž sedí
        # tady, protože _place_entry je jediné hrdlo vedoucí do trhu - kromě
        # nového zadání jím prochází i znovuzadání po přepočtu strike
        # a návrat příkazu po uvolnění spreadu
        duvod_okna = self._entry_window_block()
        if duvod_okna is not None:
            self._end_before_entry(flow, FlowState.CANCELLED, duvod_okna)
            return False

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
            self._end_before_entry(
                flow,
                FlowState.MISSED,
                f"Cena podkladu {cena:g} je {smer} vstupem {flow.entry_price:g} - "
                f"vstup propásnut, obchod ukončen bez zadání příkazu.",
            )
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
        self._warned.discard(f"{flow.id}:{part}")

    def _cancel_part(self, flow: Flow, part: str) -> None:
        """Zruší všechny aktivní příkazy části."""
        for trade in self._legs(flow, part):
            self.ib.cancel(trade)

    @staticmethod
    def _order_live(trade: Any) -> bool:
        """
        True, pokud příkaz v TWS ještě může obchodovat.
        Živý je každý příkaz, který není zrušený ani celý vyplněný - tedy
        i ten, u kterého se teprve čeká na potvrzení zrušení či zadání.
        """
        return trade is not None and trade.orderStatus.status not in SETTLED_ORDER_STATES

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
            trade.orderStatus.status in SETTLED_ORDER_STATES
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
            if trade.orderStatus.status not in SETTLED_ORDER_STATES
        ]
        return len(zive) >= (2 if flow.exit_split else 1)

    def _market_sell_running(self, flow: Flow, part: str) -> bool:
        """
        True, pokud je v prvním slotu části živý tržní prodej bez podmínek -
        tedy rozdělané uzavírání na pokyn obchodníka, které má doběhnout.
        """
        trade = self._leg(flow, part, "pt")
        if trade is None or trade.orderStatus.status in SETTLED_ORDER_STATES:
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
            stop = self._option_stop_price(flow, sl_level)
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
                order.auxPrice = self._option_stop_price(flow, sl_level)
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
                # Přepočet strike čeká na odpovědi z TWS; nakoupí-li obchod
                # mezitím, promítne se nový cíl do zajišťovacích příkazů
                if not await self._apply_pt_before_entry(flow) and flow.exit_trade is not None:
                    self._update_exit_levels(flow, "pt")
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

    async def _apply_pt_before_entry(self, flow: Flow) -> bool:
        """
        Promítne nový cíl do obchodu, který ještě nenakoupil.
        Podle konfigurace buď ponechá strike, nebo vybere nový kontrakt.

        Vrací False, pokud se obchod během čekání na TWS vyplnil a o cíl se
        musí postarat úprava zajišťovacích příkazů; jinak True.
        """
        if self.cfg.trading.pt_change_strike != "recalculate":
            return True

        # Na cíli závisí strike jen v režimu "target". Vybírá-li se od vstupní
        # ceny (otm_offset, atm), změna PT s ním nemá co dělat a kontrakt zůstává
        if self.cfg.strike.mode != "target":
            return True

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
            return True

        # Dotazy do TWS výše trvají několik sekund a monitorovací smyčka mezitím
        # běží dál - nákup se mohl vyplnit. Opce je pak koupená, strike měnit
        # nelze a nové zadání nákupu by vytvořilo druhou, nezajištěnou pozici
        if not flow.state.is_before_entry:
            self.log_event(
                f"{flow.id}: nákup se vyplnil během přepočtu strike - "
                f"kontrakt zůstává, cíl se promítne do prodejního příkazu."
            )
            return False

        if novy_strike == flow.strike:
            return True

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
        return True

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

    def runner_size(self, flow: Flow) -> int:
        """
        Kolik kontraktů u obchodu runner zabírá: běžící si drží svůj počet,
        nový vyjde jako procento drženého množství (pole Runner [%] ze zadání,
        jinak trading.runner_quantity_pct). Podle téhož čísla rozhoduje
        rozhraní, zda sekci Runner vůbec ukázat.
        """
        if flow.runner_active:
            return flow.runner_quantity
        return calc.runner_quantity(flow.held_quantity, self._runner_pct(flow))

    async def set_runner(self, flow_id: str, multiple: float) -> Flow:
        """
        Zapne runner, nebo změní jeho cíl.

        Runner je část pozice (procento podle trading.runner_quantity_pct),
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

        # Rozhoduje skutečně držené množství - dříve prodané runnery se odečítají
        total = flow.held_quantity
        runner_q = self.runner_size(flow)
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
            # Ručně zvolený runner před nákupem platí bez ohledu na množství -
            # průběžný přepočet ho jinak podle původní volby zase vypne
            if flow.state.is_before_entry:
                flow.auto_runner_multiple = multiple
                flow.auto_runner_min_quantity = None

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
            flow.clear_runner()
            # Ručně zrušený runner před nákupem nemá průběžný přepočet
            # znovu zapínat
            if flow.state.is_before_entry:
                flow.auto_runner_multiple = 0.0

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
        # Runner odložený při částečném nákupu nemá vlastní příkaz a žádné kusy
        # nedrží - celou pozici kryje hlavní prodejní příkaz. Tržní prodej navíc
        # by proto prodal víc kusů, než pozice drží (stejná pojistka jako u SL)
        if (
            not flow.runner_active
            or flow.state != FlowState.EXIT_ARMED
            or flow.runner_order_id is None
        ):
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
        # Nákup se mohl vyplnit až po posledním průchodu monitoringu; bez
        # dobrání by se obchod s čerstvě otevřenou pozicí ukončil jako
        # "zrušeno před nákupem" a pozice by zůstala v TWS bez dozoru
        self._catch_late_fill(flow)

        self.ib.cancel(flow.entry_trade)
        self._cancel_part(flow, "exit")
        self._cancel_part(flow, "runner")

        v_pozici = flow.has_position

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

    def _still_before_entry(self, flow: Flow) -> bool:
        """
        Dobere pozdní vyplnění a řekne, zda obchod pořád čeká na nákup.

        Rozhodovat se podle samotného stavu nestačí: nákup se mohl vyplnit
        až po posledním průchodu monitoringu a obchod by se pak i s čerstvou
        pozicí ukončil jako čekající. Pořadí "nejdřív dobrat, pak se ptát"
        drží pohromadě tenhle helper, ať ho nejde omylem prohodit.
        """
        self._catch_late_fill(flow)
        return flow.state.is_before_entry

    def _catch_late_fill(self, flow: Flow) -> bool:
        """
        Dobere vyplnění nákupu, které monitorovací smyčka ještě nezaznamenala.

        Podmíněný nákupní příkaz se v TWS vyplní kdykoliv, tedy i v mezeře
        mezi dvěma průchody smyčky. Stav obchodu je do nejbližšího průchodu
        stále "před nákupem", takže by se s ním zacházelo jako s obchodem
        bez pozice. Vrací True, když se vyplnění dobralo.
        """
        trade = flow.entry_trade
        if trade is None or not flow.state.is_before_entry:
            return False
        if int(trade.orderStatus.filled or 0) < 1:
            return False
        return self._register_fill(flow)

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

    def _end_before_entry(self, flow: Flow, state: FlowState, message: str) -> None:
        """
        Ukončí obchod, který se k nákupu nedostal - propásnutý vstup nebo
        rušicí okno: nastaví konečný stav se zprávou, uvolní odběry tržních
        dat a zapíše důvod do provozního logu. Jediné místo pro všechny
        cesty, kterými obchod končí před vstupem.
        """
        flow.set_state(state, message)
        self._release(flow)
        self.log_event(f"{flow.id}: {flow.message}")

    def remove_flow(self, flow_id: str) -> None:
        """Odstraní ukončené flow z přehledu."""
        flow = self.flows.get(flow_id)
        if flow is None:
            return
        if flow.state.is_active:
            raise ValueError("Aktivní flow nelze odstranit, nejprve jej zrušte.")
        self.flows.pop(flow_id, None)
        self._notify()

    def untraded_flows(self) -> list[Flow]:
        """
        Obchody, které se nikdy nedostaly k nákupu - zrušené a propásnuté.

        Zrušený obchod, který stihl nakoupit, mezi ně nepatří: drží v TWS
        otevřenou a nezajištěnou pozici, takže z přehledu zmizet nesmí.
        """
        return [
            flow
            for flow in self.flows.values()
            if flow.state in (FlowState.CANCELLED, FlowState.MISSED)
            and not flow.has_position
        ]

    def remove_untraded(self) -> int:
        """
        Odstraní z přehledu obchody, které se nikdy nedostaly k nákupu -
        zrušené a propásnuté - a vrátí jejich počet.

        Takový obchod nemá v trhu příkaz ani pozici a nenese žádný výsledek,
        takže se jen vyřadí z přehledu a do TWS se nesahá. Čekající, otevřené
        i uzavřené obchody zůstávají a s nimi dvě výjimky, které by se z přehledu
        ztratit neměly: obchod skončený chybou vyžaduje ruční kontrolu a zrušený
        obchod, který stihl nakoupit, drží v TWS otevřenou a nezajištěnou pozici.
        """
        k_odstraneni = self.untraded_flows()
        if not k_odstraneni:
            return 0

        for flow in k_odstraneni:
            # Odběry tržních dat takový obchod zpravidla nedrží, uvolnění je
            # ale levné a pojistí se tím proti zapomenutému odběru
            self._release(flow)
            self.flows.pop(flow.id, None)
            self._warned -= {
                klic for klic in self._warned if klic.startswith(f"{flow.id}:")
            }

        self.log_event(
            f"Z přehledu odstraněno {len(k_odstraneni)} obchodů bez nákupu "
            f"(zrušené a propásnuté)."
        )
        self._notify()
        return len(k_odstraneni)

    async def remove_untraded_and_pending(self) -> tuple[int, int]:
        """
        Úklid o krok dál než remove_untraded: kromě zrušených a propásnutých
        obchodů odstraní z přehledu i ty, které teprve čekají na nákup.

        Čekající obchod se nejprve zruší, takže jeho nákupní příkaz zmizí
        i z TWS. Obchodů s otevřenou pozicí, uzavřených ani skončených chybou
        se úklid nedotkne - stejně jako u remove_untraded zůstávají v přehledu.

        Vrací dvojici (zrušeno čekajících, odstraněno položek).
        """
        async with self._lock:
            zruseno = 0
            for flow in list(self.flows.values()):
                if not self._still_before_entry(flow):
                    continue
                self._cancel_locked(
                    flow,
                    reason="Zrušeno úklidem přehledu, příkaz odstraněn z trhu.",
                )
                zruseno += 1

            if zruseno:
                self.log_event(
                    f"Úklidem přehledu zrušeno {zruseno} obchodů čekajících na nákup."
                )

        # Právě zrušené obchody teď spadají mezi neobchodované, takže je
        # z přehledu vyřadí tentýž úklid jako zrušené a propásnuté
        return zruseno, self.remove_untraded()

    async def cancel_and_clear_all(self) -> tuple[int, int]:
        """
        Zruší všechna běžící flow a vyprázdní celý přehled obchodů.

        Pozice se záměrně neuzavírají trhem: uzavírání by obchod nechalo ve
        stavu CLOSING, tedy dál v přehledu, a hromadné vyčištění by nic
        nevyčistilo. Držené pozice proto zůstávají v TWS otevřené a bez
        zajištění - obchodník je na to upozorněn už v potvrzovacím dialogu.

        Vrací dvojici (zrušeno běžících, smazáno položek).
        """
        async with self._lock:
            flows = list(self.flows.values())
            zruseno = 0
            for flow in flows:
                if flow.state.is_active:
                    self._cancel_locked(flow, close_position=False)
                    zruseno += 1
                else:
                    # Ukončené flow už odběry zpravidla nedrží, uvolnění je
                    # ale levné a pojistí se tím proti zapomenutému odběru
                    self._release(flow)

            self.flows.clear()
            self._warned.clear()
            self.log_event(
                f"Přehled obchodů vyprázdněn - zrušeno {zruseno} běžících, "
                f"smazáno {len(flows)} položek."
            )
            self._notify()
            return zruseno, len(flows)

    # ------------------------------------------------------------------
    # Monitorovací smyčka
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spustí periodickou monitorovací smyčku a jejího hlídače."""
        if self._task is None or self._task.done():
            self._started_at = time.monotonic()
            self._task = asyncio.create_task(self._run())
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watch())

    async def stop(self) -> None:
        """Zastaví monitorovací smyčku i jejího hlídače."""
        for task in (self._task, self._watchdog):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._watchdog = None

    async def _run(self) -> None:
        """Hlavní smyčka - periodicky prochází aktivní flow a hlídá spojení."""
        while True:
            try:
                # Za hlídání se počítá jen skutečně provedený průchod
                if await self._tick():
                    self._last_tick = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Chyba v monitorovací smyčce.")
            await asyncio.sleep(self.cfg.engine.poll_interval_sec)

    def monitoring_stall(self) -> tuple[float, str] | None:
        """
        Jak dlouho monitorovací smyčka nedokončila průchod a na čem stojí,
        nebo None, pokud běží normálně (či vůbec nebyla spuštěna).

        Uvázne-li smyčka na dotazu do TWS nebo na obnově, aplikace dál
        vypadá připojená, ale obchody nehlídá - to musí být vidět.
        """
        stoji = self._tick_age()
        if stoji is None or stoji < STALL_ALARM_SEC:
            return None
        return stoji, self._loop_step or "neznámý krok"

    async def _watch(self) -> None:
        """
        Hlídač monitorovací smyčky - běží jako samostatná úloha, aby ohlásil
        i smyčku uvázlou uvnitř průchodu. Do průběhu zapíše začátek i konec
        zastavení, každé jen jednou.
        """
        while True:
            await asyncio.sleep(max(self.cfg.engine.poll_interval_sec, 1.0))
            self._report_stall()

    def _report_stall(self) -> None:
        """Zapíše do průběhu změnu stavu uvázlé smyčky (začátek či konec)."""
        stall = self.monitoring_stall()
        if stall is not None and not self._stall_warned:
            stoji, krok = stall
            self.log_event(
                f"POZOR: monitorovací smyčka stojí {format_countdown(stoji)} (krok: {krok}) - "
                f"obchody nejsou hlídány."
            )
        elif stall is None and self._stall_warned:
            self.log_event("Monitorovací smyčka znovu běží, obchody jsou hlídány.")
        self._stall_warned = stall is not None

    def _exchange_now(self) -> datetime:
        """Aktuální čas v časové zóně burzy (řeší letní/zimní čas)."""
        return datetime.now(ZoneInfo(self.cfg.trading.exchange_timezone))

    def _exchange_time(self, ted: datetime, cas: str) -> datetime:
        """
        Dnešní okamžik daný časem HH:MM v časové zóně burzy.

        Sdílí jej odvození otevření, zavření i času rušení čekajících obchodů,
        aby se tvar zapsaný v konfiguraci vyhodnocoval na jediném místě.
        """
        hodina, minuta = (int(cast) for cast in cas.split(":"))
        return ted.replace(hour=hodina, minute=minuta, second=0, microsecond=0)

    def _exchange_close(self, ted: datetime) -> datetime:
        """Dnešní čas zavření burzy v její časové zóně."""
        return self._exchange_time(ted, self.cfg.trading.exchange_close_time)

    def _exchange_open(self, ted: datetime) -> datetime:
        """Dnešní čas otevření burzy v její časové zóně."""
        return self._exchange_time(ted, self.cfg.trading.exchange_open_time)

    def market_open_seconds(self) -> float | None:
        """
        Počet sekund do nejbližšího otevření burzy.

        None znamená, že burza právě obchoduje - odpočet nemá co měřit.
        Po zavření a o víkendu se míří na otevření následujícího obchodního
        dne; svátky a zkrácené obchodní dny aplikace nezná.
        """
        ted = self._exchange_now()
        otevreni = self._exchange_open(ted)

        # V obchodní den před otevřením stačí odpočet do dnešní seance
        if ted.weekday() < 5 and ted < otevreni:
            return (otevreni - ted).total_seconds()

        # Uvnitř dnešní seance se odpočet nezobrazuje
        if ted.weekday() < 5 and ted < self._exchange_close(ted):
            return None

        # Po zavření a o víkendu se hledá nejbližší další obchodní den.
        # Přičítání dnů běží v nástěnném čase burzy, takže přechod mezi
        # letním a zimním časem otevírací hodinu neposune
        cil = otevreni + timedelta(days=1)
        while cil.weekday() >= 5:
            cil += timedelta(days=1)
        # Rozdíl přes timestamp počítá skutečně uplynulé sekundy i tehdy,
        # když mezi dneškem a cílem přeskočí hodina letního času
        return cil.timestamp() - ted.timestamp()

    def market_open_elapsed(self) -> float | None:
        """
        Počet sekund od dnešního otevření burzy; None mimo obchodní hodiny.

        Opírá se o market_open_seconds, aby oba údaje vycházely z téhož času
        burzy - a aby jej testy mohly podvrhnout na jednom místě.
        """
        if self.market_open_seconds() is not None:
            return None
        ted = self._exchange_now()
        return (ted - self._exchange_open(ted)).total_seconds()

    def _window_seconds(self, ted: datetime, start: datetime) -> float | None:
        """
        Odpočet do okna, které trvá od zadaného startu do zavření burzy.

        Okno leží vždy uvnitř seance: start dřívější než otevření burzy se
        posune na otevření, aby čas zadaný před ním nerušil obchody právě
        nachystané na open. None znamená, že okno dnes nenastane - je víkend,
        burza už zavřela, nebo start vychází až za zavřením; nula znamená,
        že okno právě běží.
        """
        zavirani = self._exchange_close(ted)
        start = max(start, self._exchange_open(ted))
        # O víkendu se neobchoduje a po zavření už okno nemá co dělat
        if ted.weekday() >= 5 or ted >= zavirani or start >= zavirani:
            return None
        return max((start - ted).total_seconds(), 0.0)

    def auto_close_seconds(self) -> float | None:
        """
        Počet sekund do začátku automatického uzavírání obchodů.

        None znamená, že se dnes už neuzavírá (funkce vypnutá, víkend, nebo
        burza už zavřela); nula znamená, že uzavírací okno právě běží.

        Rozhoduje jen runtime přepínač - konfigurace je pouze jeho výchozí
        hodnotou při startu (na rozdíl od auto_connect, kde konfigurační
        klíč zůstává tvrdým zámkem). Vypnutou pojistku tak jde z hlavičky
        kdykoliv zase nasadit, i když ji soubor vypíná.
        """
        if not self.auto_close_on:
            return None

        ted = self._exchange_now()
        start = self._exchange_close(ted) - timedelta(
            minutes=self.cfg.trading.auto_close_minutes_before
        )
        return self._window_seconds(ted, start)

    @property
    def auto_close_active(self) -> bool:
        """Právě běží uzavírací okno před koncem obchodování."""
        return self.auto_close_seconds() == 0

    @property
    def pending_cancel_active(self) -> bool:
        """Právě běží okno, ve kterém se ruší obchody čekající na nákup."""
        return self.pending_cancel_seconds() == 0

    async def _auto_close_flows(self) -> None:
        """
        Krátce před zavřením burzy uzavře všechny běžící obchody.

        Čas se počítá v časové zóně burzy, takže posun letního a zimního času
        vůči místnímu času počítače nehraje roli. Čekající obchody se ruší
        (nákupní příkaz se odstraní z trhu), obchody s pozicí se prodají trhem.
        """
        if not self.auto_close_active:
            return

        minuty = self.cfg.trading.auto_close_minutes_before
        for flow in list(self.flows.values()):
            if not flow.state.is_active or flow.state == FlowState.CLOSING:
                continue
            if self._still_before_entry(flow):
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

    def pending_cancel_seconds(self) -> float | None:
        """
        Počet sekund do zrušení čekajících obchodů v pevně daný čas dne.

        None znamená, že se dnes už neruší (funkce vypnutá, víkend, nebo
        burza už zavřela); nula znamená, že rušicí okno právě běží.

        Stejně jako u auto_close_seconds rozhoduje jen runtime přepínač;
        konfigurace dává pouze jeho výchozí hodnotu při startu.
        """
        if not self.pending_cancel_on:
            return None

        ted = self._exchange_now()
        start = self._exchange_time(ted, self.cfg.trading.pending_cancel_time)
        return self._window_seconds(ted, start)

    async def _cancel_pending_flows(self) -> None:
        """
        V nastavený čas dne zruší obchody, které ještě nenakoupily.

        Na rozdíl od automatického uzavírání před koncem burzy se týká jen
        obchodů před vstupem - už nakoupené pozice běží dál se svým PT a SL.
        """
        if not self.pending_cancel_active:
            return

        cas = self.cfg.trading.pending_cancel_time
        for flow in list(self.flows.values()):
            # Stavy před vstupem jsou podmnožinou aktivních, takže tahle
            # jediná podmínka odfiltruje pozice i už uzavřené obchody
            if not self._still_before_entry(flow):
                continue
            self.log_event(
                f"{flow.id}: automatické zrušení čekajícího obchodu "
                f"(čas {cas} burzovního času)."
            )
            await self._cancel(
                flow,
                reason=f"Automaticky zrušeno v {cas} burzovního času, "
                "příkaz odstraněn z trhu.",
            )

    def _entry_window_block(self) -> str | None:
        """
        Důvod, proč nový obchod nemá vůbec jít do trhu, nebo None.

        Rušicí i uzavírací okno by příkaz odstranily hned následujícím
        průchodem monitorovací smyčky. Do té doby by ale ležel v TWS jako
        aktivní podmíněný příkaz a při dotyku vstupní úrovně by se stihl
        vyplnit - proto se v obou oknech nezadává vůbec.
        """
        if self.pending_cancel_active:
            duvod = (
                f"v době, kdy se čekající obchody ruší "
                f"(od {self.cfg.trading.pending_cancel_time} burzovního času)"
            )
        elif self.auto_close_active:
            duvod = (
                f"v uzavíracím okně před koncem obchodování "
                f"({self.cfg.trading.auto_close_minutes_before:g} min před zavřením burzy)"
            )
        else:
            return None
        return f"Zadáno {duvod} - příkaz nebyl do TWS odeslán."

    def _sync_commissions(self) -> bool:
        """
        Promítne provize účtované TWS do obchodů a řekne, zda se něco změnilo.

        Provize se vedou po jednotlivých vyplněních podle jejich execId, takže
        opakované načtení téhož vyplnění (další průchod smyčkou, obnova po
        novém spojení) hodnotu jen přepíše a nikdy ji nepřičte podruhé.
        Provize příkazů obchodu, který už v přehledu není, se zahazují.
        """
        zmena = False
        for flow_id, polozky in self.ib.commissions().items():
            flow = self.flows.get(flow_id)
            if flow is None:
                continue
            for exec_id, (druh, castka) in polozky.items():
                # Nákup má vlastní kbelík, všechny ostatní příkazy jsou prodeje
                cil = flow.entry_commissions if druh == "entry" else flow.exit_commissions
                if cil.get(exec_id) == castka:
                    continue
                cil[exec_id] = castka
                zmena = True
        return zmena

    async def _tick(self) -> bool:
        """
        Jeden průchod monitoringem všech aktivních flow.

        Vrací False, pokud se průchod vynechal kvůli obnově (běží, nebo se
        nedokončila) - takový průchod se nepočítá za hlídání (_last_tick).
        Před každým krokem, který čeká na TWS, se zapíše _loop_step, aby
        hlídač smyčky uměl říct, kde uvázla.
        """
        # Během obnovy se nemonitoruje - příkazy z minulého spojení nejsou platné
        if self._restore_lock.locked():
            return False

        if not self.ib.connected:
            # Po obnovení spojení se obchody musí znovu spárovat s příkazy v TWS
            self._synced = False
            if self.reconnects_automatically:
                self._loop_step = "obnova spojení s TWS"
                await self._try_reconnect()
            return True

        # Připojené, ale nespárované obchody (obnově TWS nevydal pozice nebo
        # příkazy) smyčka obnovuje, dokud obnova neproběhne celá. Hlídat je
        # do té doby podle neověřených příkazů by bylo nebezpečné
        if not self._synced:
            await self.restore()
            if not self._synced:
                return False

        # Velikost účtu z TWS se obnovuje, jen když ji konfigurace přebírá (size = 0)
        self._loop_step = "velikost účtu z TWS"
        await self._refresh_account_size()

        # Pozice bez dozoru aplikace se kontrolují v delším intervalu
        self._loop_step = "kontrola pozic bez dozoru"
        await self._check_unmanaged()

        # V nastavený čas dne se ruší obchody, které ještě nenakoupily
        self._loop_step = "rušení čekajících obchodů"
        await self._cancel_pending_flows()

        # Krátce před zavřením burzy se běžící obchody automaticky uzavírají
        self._loop_step = "automatické uzavírání pozic"
        await self._auto_close_flows()

        # Provize dorazí z TWS až po vyplnění příkazu, proto se dobírají průběžně
        changed = self._sync_commissions()

        for flow in list(self.flows.values()):
            if not flow.state.is_active:
                continue
            self._loop_step = f"obchod {flow.id}"
            try:
                changed |= await self._monitor(flow)
            except Exception as exc:
                log.exception("Chyba při monitoringu flow %s.", flow.id)
                flow.set_state(FlowState.ERROR, f"Chyba monitoringu: {exc}")
                changed = True

        self._warn_oer_over_limit()

        if changed:
            self._notify()
        return True

    def _oer_text(self) -> str:
        """Stav OER dne pro log - poměr, počty a rozpočet dne."""
        oer = self.ib.oer
        return (
            f"OER dne {oer.ratio:.1f} ({oer.messages} zpráv / {oer.executed} "
            f"vyplněných příkazů + 1), rozpočet dne {oer.budget:.0f} zpráv "
            f"(volný základ {oer.free_messages}, limit OER {oer.limit:g})"
        )

    def _warn_oer_over_limit(self) -> None:
        """
        Ohlásí, že zprávy dne přesáhly volný základ i limit OER. Hlásí se
        změna stavu: po vyplnění dalšího příkazu nebo s novým dnem (počítadlo
        se nuluje) stav pomine a další překročení se ohlásí znovu.
        Nepovinné zprávy se nad rozpočet nepouštějí, přetáhnout ho mohou jen
        povinné (zajištění a uzavření pozic, rušení příkazů) nebo zásahy
        obchodníka.
        """
        prekroceno = self.ib.oer.over_limit
        if prekroceno and not self._oer_over_warned:
            self.log_event(
                f"POZOR - {self._oer_text()} je překročen. Nepovinné úpravy "
                f"příkazů jsou pozastavené, dokud se nevyplní další příkaz."
            )
        self._oer_over_warned = prekroceno

    def _oer_allows(self, count: int, flow: Flow, akce: str) -> bool:
        """
        Posoudí, zda se smí odeslat `count` nepovinných zpráv do TWS (nový
        příkaz, úprava, zrušení), aniž by zprávy dne přesáhly volný základ
        trading.oer_free_messages i limit OER trading.oer_limit.

        Rezervou jsou zrušení všech čekajících nákupních příkazů v trhu -
        ta může být potřeba poslat povinně (rušení v nastavený čas, propásnutý
        vstup, spread nad limitem) a nepovinné zprávy je nesmějí vytlačit.
        Odklad se do logu hlásí jednou za obchod a akci (akce je slovo pro
        log); jakmile úprava znovu projde, další odklad se ohlásí znovu.
        """
        rezerva = sum(
            1
            for f in self.flows.values()
            if f.state.is_before_entry and f.entry_trade is not None
        )
        klic = f"{flow.id}:oer:{akce}"
        if self.ib.oer.allows(count, rezerva):
            self._warned.discard(klic)
            return True
        if klic not in self._warned:
            self._warned.add(klic)
            self.log_event(
                f"{flow.id}: {akce} odloženo - vyčerpán rozpočet zpráv: "
                f"{self._oer_text()}, rezerva na zrušení {rezerva}."
            )
        return False

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
        self._loop_step = "obnova obchodů"

        # Bez úplného seznamu příkazů a pozic z TWS se obnova odkládá - obchody
        # by se jinak vyhodnotily jako bez příkazů, resp. s pozicí uzavřenou
        # během výpadku. Zopakuje ji další průchod monitorovací smyčky.
        # Pozice se zjišťují první: bez spojení s IBKR se vrátí hned
        # a stahování všech příkazů dne se ušetří
        if (pozice := await self.ib.positions()) is None or (
            prikazy := await self.ib.app_trades()
        ) is None:
            # Hlásí se jen první neúspěch, opakování by zaplavila průběh
            if "restore" not in self._warned:
                self._warned.add("restore")
                self.log_event("TWS nevydal příkazy nebo pozice, obnova obchodů se zopakuje.")
            return
        self._warned.discard("restore")
        self._synced = True

        # Ze souboru se čte jen při prvním spuštění; při dalším připojení
        # je stav v paměti aktuálnější než ten uložený
        ulozene: list[Flow] = []
        if not self._restored:
            self._restored = True
            if self.cfg.state.enabled:
                # S obchody se obnoví i počítadlo zpráv OER z dřívějška téhož
                # dne - bez něj by restart během seance dovolil rozpočet
                # vyčerpat podruhé
                ulozene, statistiky = store.load_state(self.cfg.state.file)
                self.ib.oer.load(statistiky)
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
        # Výjimka z limitu spreadu se neukládá, platí aktuální konfigurace
        flow.cheap_rule = self.cfg.trading.cheap_option_rule
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
                    (flow.runner_fill_price - flow.fill_price)
                    * flow.runner_quantity
                    * calc.OPTION_MULTIPLIER
                )
            flow.runner_sold_quantity += flow.runner_quantity
            flow.clear_runner()
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

        # Interval platí i pro neúspěšný pokus - nezodpovězený dotaz trvá
        # až REQUEST_TIMEOUT_SEC a opakovat jej každý průchod by smyčku brzdilo
        loop = asyncio.get_running_loop()
        if loop.time() - self._account_checked < self.cfg.engine.account_refresh_sec:
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

        # Neznámé pozice (TWS bez spojení s IBKR, bez odpovědi) nic nemění -
        # dosavadní upozornění platí, dokud se pozice nepodaří načíst
        pozice = await self.ib.positions()
        if pozice is None:
            return
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

    @property
    def flows_restored(self) -> bool:
        """
        Jsou obchody z uloženého stavu načtené a ověřené v TWS?

        Do té doby engine neví o obchodech z předchozího běhu - nové zadání
        téhož tickeru by čekající obchod nenahradilo, ale založilo vedle něj
        druhý. Naplánované zadání po restartu proto čeká na tuto chvíli.
        Bez ukládání stavu není co obnovovat.
        """
        if not self.cfg.state.enabled:
            return True
        return self._restored and not self._restore_lock.locked()

    @property
    def reconnects_automatically(self) -> bool:
        """
        Obnovuje se spojení s TWS samo? Musí to povolit konfigurace
        i obchodník - ruční odpojení automatické připojování vypíná.
        """
        return self.cfg.connection.auto_reconnect and self.auto_connect

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
        """
        Načte aktuální ceny podkladu i opce a přepočítá spread.

        Vrací True jen při skutečné změně některé z hodnot. Bezpodmínečné True
        by při každém průchodu smyčky spustilo _notify() a s ním přepis celého
        stavového souboru včetně fsync - při vteřinovém intervalu desetitisíce
        zbytečných zápisů denně, i když se kotace vůbec nepohnuly.
        """
        price = self.ib.underlying_price(flow.underlying_contract)
        bid, ask, delta = self.ib.option_quotes(flow.option_contract)

        # Snímek hodnot před aktualizací - podle něj se pozná, zda se něco změnilo
        puvodni = (
            flow.underlying_price,
            flow.option_bid,
            flow.option_ask,
            flow.option_spread_pct,
            flow.delta,
            flow.expected_profit,
            flow.expected_loss,
        )

        flow.underlying_price = price if price is not None else flow.underlying_price
        flow.option_bid = bid
        flow.option_ask = ask
        flow.option_spread_pct = calc.spread_pct(bid, ask)
        if delta is not None:
            flow.delta = delta

        # Očekávaný výsledek se přepočítává s každou změnou cen na trhu
        self._compute_expected_pnl(flow)

        return puvodni != (
            flow.underlying_price,
            flow.option_bid,
            flow.option_ask,
            flow.option_spread_pct,
            flow.delta,
            flow.expected_profit,
            flow.expected_loss,
        )

    def _handle_before_entry(self, flow: Flow) -> bool:
        """
        Stav před nákupem: kontrola vyplnění, hlídání spreadu
        a průběžná aktualizace limitní ceny.
        """
        # Vyplnění nákupu má přednost před vším ostatním
        if self._catch_late_fill(flow):
            return True

        trade = flow.entry_trade

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

        # Propásnutý vstup ukončí obchod v každém ze stavů před nákupem,
        # tedy i tehdy, když příkaz zrovna v trhu není
        if self._entry_missed(flow):
            return True

        # Po otevření burzy se čekající obchod jednou přepočítá podle živých
        # kotací, se zapnutým průběžným přepočtem pak v pravidelném odstupu.
        # Příkaz nad limitem spreadu se stahuje ještě před přepočtem,
        # aby se neupravoval těsně před zrušením; zbytek obsluhy spreadu běží
        # až po něm, aby se příkaz vracel do trhu už s přepočteným množstvím
        stazeno = self._withdraw_on_spread_breach(flow)
        prepocteno = self._refresh_after_open(flow) or self._refresh_periodic(flow)
        return self._handle_spread(flow) or prepocteno or stazeno

    def _withdraw_on_spread_breach(self, flow: Flow) -> bool:
        """
        Stáhne z trhu nevyplněný nákupní příkaz, jehož spread překročil limit
        (jen při zapnutém trading.cancel_on_spread_breach). Vrací True, pokud
        se příkaz stáhl.
        """
        spread = flow.option_spread_pct
        if (
            not self.cfg.trading.cancel_on_spread_breach
            or flow.state != FlowState.ARMED
            or not flow.spread_over_limit()
        ):
            return False

        self.ib.cancel(flow.entry_trade)
        flow.entry_trade = None
        flow.entry_order_id = None
        flow.blocked_since = datetime.now()
        # Každé další odstranění prodlouží prodlevu před návratem do trhu
        flow.spread_breaches += 1
        flow.set_state(
            FlowState.SPREAD_BLOCKED,
            f"Spread {spread:.2f} % > limit {flow.max_spread_pct:g} %, "
            f"příkaz odstraněn z trhu.",
        )
        self.log_event(f"{flow.id}: {flow.message}")
        return True

    def _handle_spread(self, flow: Flow) -> bool:
        """
        Hlídání spreadu u obchodu před nákupem: po návratu spreadu pod limit
        se stažený příkaz zase zadává, čekání na kotace končí zadáním příkazu
        a nevyplněnému příkazu se průběžně upravuje limitní cena. Stažení
        příkazu nad limitem obstarává _withdraw_on_spread_breach. Vrací True
        při změně stavu obchodu.
        """
        spread = flow.option_spread_pct
        trading = self.cfg.trading

        # Spread nad limitem bez rušení příkazu - příkaz zůstává v trhu
        # a limitní cena se mu neupravuje
        if flow.state == FlowState.ARMED and flow.spread_over_limit():
            return False

        # Spread zpět v limitu - příkaz se vrací do trhu. Návrat stojí dvě
        # zprávy (zadání a případné pozdější zrušení), proto musí projít
        # limitem OER; jinak obchod zůstává zablokovaný
        if flow.state == FlowState.SPREAD_BLOCKED:
            if (
                trading.rearm_on_spread_ok
                and self._can_rearm(flow)
                and self._oer_allows(2, flow, "návrat příkazu do trhu")
            ):
                return self._place_entry(flow)
            return False

        # Čekání na kotace - jakmile dorazí a spread vyhovuje, příkaz se zadá
        if flow.state == FlowState.NO_QUOTES:
            if flow.spread_over_limit():
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

    def _preview_from_flow(self, flow: Flow, cena_podkladu: float) -> Preview:
        """
        Náhled nad kontraktem čekajícího obchodu - podklad pro přepočet úrovní
        a množství stejnými kroky, jakými vzniklo původní zadání. Kontrakt se
        nevybírá znovu: vstupní cena, od které se strike odvozuje, se nemění,
        a příkaz v trhu se stejně dá upravit jen na témže kontraktu.
        """
        return Preview(
            symbol=flow.symbol,
            current_price=cena_podkladu,
            right=flow.right,
            expiration=flow.expiration,
            strike=flow.strike,
            profit_target=flow.profit_target,
            pt_on_underlying=flow.pt_on_underlying,
            sl_on_underlying=flow.sl_on_underlying,
            # Starší uložený obchod poměr nezná - pak platí konfigurace
            sl_to_pt_ratio=flow.sl_to_pt_ratio or self.cfg.trading.sl_to_pt_ratio,
            sl_spread_compensated=flow.sl_spread_compensated,
            risk_amount=self.risk_amount,
            account_size=self.account_size,
            underlying=flow.underlying_contract,
            option=flow.option_contract,
            min_tick=flow.min_tick,
        )

    def _refresh_after_open(self, flow: Flow) -> bool:
        """
        Jednorázový přepočet čekajícího obchodu po otevření burzy. Obchod
        zadaný před otevřením má PT, SL i množství z odhadu prémie (typicky
        ze závěrečné ceny), který po gapu neplatí - po prodlevě od otevření
        se dopočítají znovu (viz _recalculate_pending). Vrací True, pokud se
        obchod změnil.
        """
        if flow.refresh_after_open_sec is None or flow.refresh_after_open_done:
            return False
        uplynulo = self.market_open_elapsed()
        if uplynulo is None or uplynulo < flow.refresh_after_open_sec:
            return False

        # Smíšený režim úrovní hromadné zadání nevytváří; přepočet by musel
        # převádět přes referenční opci, kterou obchod nedrží
        if flow.pt_on_underlying != flow.sl_on_underlying:
            flow.refresh_after_open_done = True
            self.log_event(
                f"{flow.id}: přepočet po otevření vynechán - PT a SL jsou "
                f"v různých režimech."
            )
            return True

        if not self._recalculate_pending(flow, po_otevreni=True):
            return False
        flow.refresh_after_open_done = True
        flow.last_refresh_at = datetime.now()
        flow.touch(f"{flow.message} Přepočteno po otevření burzy.".strip())
        return True

    def _refresh_periodic(self, flow: Flow) -> bool:
        """
        Průběžný přepočet čekajícího obchodu každých refresh_interval_sec
        sekund za otevřené burzy (viz _recalculate_pending). Čeká-li obchod
        ještě na přepočet po otevření, běží až po něm. Odstup se měří od
        posledního přepočtu, před prvním od založení; počítá se i pokus, který
        přepočet odložil (chybí kotace, TWS příkaz zrovna mění), ať se
        neopakuje při každém průchodu smyčkou. Vrací True, pokud se obchod
        změnil.
        """
        if not flow.refresh_interval_sec or flow.pt_on_underlying != flow.sl_on_underlying:
            return False
        # Odstup je nejlacinější a nejčastěji zamítající podmínka, čas burzy
        # se počítá až po ní
        ted = datetime.now()
        if (ted - (flow.last_refresh_at or flow.created_at)).total_seconds() < flow.refresh_interval_sec:
            return False
        if self.market_open_elapsed() is None:
            return False
        if flow.refresh_after_open_sec is not None and not flow.refresh_after_open_done:
            return False
        flow.last_refresh_at = ted
        return self._recalculate_pending(flow, po_otevreni=False)

    def _recalculate_pending(self, flow: Flow, po_otevreni: bool) -> bool:
        """
        Přepočet čekajícího obchodu podle živých kotací - jádro přepočtu po
        otevření burzy i průběžného přepočtu.

        PT zadané procentem prémie se odvodí znovu z aktuální odhadované
        nákupní ceny, PT v USD a na podkladu zůstává; SL a množství se
        dopočítají stejnými kroky jako při přípravě zadání, příkaz čekající
        v trhu se upraví na místě (stejné orderId) a runner se srovná
        s automatickou volbou. Spread nad limitem přepočet nezdržuje - stropuje
        se limitem jako při přípravě; příkaz nad limitem už předtím stáhla
        _withdraw_on_spread_breach. Dokud chybí kotace, TWS příkaz právě mění
        nebo není známa velikost účtu, přepočet počká na další pokus.
        Přepočet po otevření se zapisuje do logu vždy, průběžný jen tehdy,
        když změnil množství nebo runner. Vrací True, pokud přepočet proběhl.
        """
        # Bez známé velikosti účtu by množství vyšlo z nulového rizika
        if self.account_size <= 0:
            return False

        # Příkaz, který TWS teprve přijímá, ruší nebo už plní, se upravovat nesmí
        trade = flow.entry_trade
        if trade is not None:
            if trade.orderStatus.filled > 0:
                return False
            if trade.orderStatus.status not in MODIFIABLE_ORDER_STATES:
                return False

        # Bez ceny podkladu nebo BID i ASK opce (spread z téhož průchodu
        # smyčkou) přepočet nemá z čeho vyjít
        cena_podkladu = self.ib.underlying_price(flow.underlying_contract)
        if cena_podkladu is None or flow.option_spread_pct is None:
            return False

        preview = self._preview_from_flow(flow, cena_podkladu)
        used_delta = self._load_option_market(preview, flow.entry_price, flow.max_spread_pct)

        # PT v procentech prémie se odvíjí od ceny opce: totéž procento se
        # přepočítá z nové odhadované nákupní ceny (stejný základ jako
        # v dialogu - odhad při vstupu, jinak ASK, nakonec cena pro model).
        # Procento se vrací z uložené úrovně a ceny, ze které vyšla
        profit_target = flow.profit_target
        premie: float | None = None
        if flow.pt_in_premium and flow.premium_base:
            premie = (
                preview.expected_fill_price or preview.option_ask or preview.option_price
            )
            if not premie or premie <= 0:
                return False
            profit_target = round(flow.profit_target / flow.premium_base * premie, 2)

        self._derive_levels(
            preview, flow.entry_price, profit_target, None, used_delta, flow.max_spread_pct
        )
        if preview.quantity < 1:
            return False

        # Zvýšení množství příkazu v trhu průběžným přepočtem je nepovinná
        # úprava. Projde jen tehdy, když by vyšší množství obstálo i s riskem
        # sníženým o pásmo necitlivosti (jinak by drobný pohyb prémie množství
        # přehazoval tam a zpět a každá úprava by zvýšila OER) a když se
        # vejde do limitu OER; jinak zůstává dosavadní, menší množství.
        # Snížení se posílá vždy - chrání riziko na obchod
        if not po_otevreni and trade is not None and preview.quantity > flow.quantity:
            pasmo = self.cfg.trading.refresh_increase_margin_pct / 100.0
            s_rezervou = self._quantity_for_risk(
                preview, flow.entry_price, used_delta, self.risk_amount * (1.0 - pasmo)
            )
            if s_rezervou <= flow.quantity or not self._oer_allows(
                1, flow, "zvýšení množství přepočtem"
            ):
                preview.quantity = flow.quantity

        puvodni_pt, puvodni_sl, puvodni_ks = flow.profit_target, flow.stop_loss, flow.quantity
        byl_runner = flow.runner_active
        popis_pt, popis_sl = flow.level_text("pt"), flow.level_text("sl")

        flow.profit_target = profit_target
        flow.original_profit_target = profit_target
        flow.stop_loss = preview.stop_loss
        flow.original_stop_loss = preview.stop_loss
        flow.quantity = preview.quantity
        # Nové množství vychází z nového odhadu nákupní ceny - přehled musí
        # stropovat spread ze stejného základu. Bez odhadu zůstává původní
        if preview.expected_fill_price is not None:
            flow.expected_fill_price = preview.expected_fill_price
        if premie is not None:
            flow.premium_base = premie
        self._sync_runner(flow, "po přepočtu")

        # Příkaz v trhu se upraví na místě - odeslání se stejným orderId je
        # modifikace, příkaz se neruší a nevzniká mezera, ve které by vstup
        # utekl. Limit se srovná s aktuální kotací při téže úpravě
        if trade is not None and flow.quantity != puvodni_ks:
            order = trade.order
            order.totalQuantity = flow.quantity
            limit = self._entry_limit(flow)
            if limit is not None:
                order.lmtPrice = limit
                flow.entry_limit = limit
            flow.entry_trade = self.ib.place(flow.option_contract, order)

        zmeny = []
        if flow.profit_target != puvodni_pt:
            zmeny.append(f"PT {popis_pt} → {flow.level_text('pt')}")
        if flow.stop_loss != puvodni_sl:
            zmeny.append(f"SL {popis_sl} → {flow.level_text('sl')}")
        if flow.quantity != puvodni_ks:
            zmeny.append(f"množství {puvodni_ks} → {flow.quantity} ks")
        if flow.runner_active != byl_runner:
            zmeny.append("runner zapnut" if flow.runner_active else "runner vypnut")
        self._compute_expected_pnl(flow)

        # Průběžný přepočet se opakuje každou půlminutu a PT v procentech
        # prémie se hýbe s každou kotací - do logu jde jen změna množství
        # nebo runneru, jinak by log zaplavil
        podstatne = flow.quantity != puvodni_ks or flow.runner_active != byl_runner
        if not po_otevreni and not podstatne:
            return True
        if po_otevreni:
            popis = f"přepočteno {self.market_open_elapsed():.0f} s po otevření burzy"
        else:
            popis = "průběžně přepočteno"
        souhrn = ", ".join(zmeny) if zmeny else "hodnoty se nezměnily"
        zaklad = f", prémie ≈ {premie * calc.OPTION_MULTIPLIER:.0f} USD" if premie else ""
        vyhrady = f" Výhrady: {' '.join(preview.warnings)}" if preview.warnings else ""
        self.log_event(
            f"{flow.id}: {popis} podle živých kotací - {souhrn}{zaklad}.{vyhrady}"
        )
        return True

    def entry_cross_start(self) -> datetime | None:
        """
        Okamžik, od kterého kontrola propásnutého vstupu prochází minutové
        svíčky - dnešní čas import.entry_cross_check_from v zóně burzy.

        None, když je kontrola v konfiguraci vypnutá, nebo když nastavený
        čas dnes teprve přijde - není pak co procházet.
        """
        imp = self.cfg.import_
        if not imp.entry_cross_check:
            return None
        ted = self._exchange_now()
        zacatek = self._exchange_time(ted, imp.entry_cross_check_from)
        if zacatek >= ted:
            return None
        return zacatek

    async def entry_crossed(
        self, contract: Any, right: str, entry_price: float, since: datetime
    ) -> str | None:
        """
        Kontrola propásnutého vstupu podle minutových svíček podkladu.

        Projde svíčky od okamžiku `since` (zpravidla entry_cross_start())
        do teď a vrátí popis první, na které podklad vstupní úroveň překročil
        (u CALL high >= vstup, u PUT low <= vstup) - například „Podklad
        překročil vstup 220.99 už v 08:12 čas burzy (svíčka high 221.35)“.
        None znamená, že se vstupu žádná svíčka nedotkla.

        Používá ji jen dialog načtení pozic ze souboru (viz
        import.entry_cross_check). Chyby dotazu do TWS se nechávají
        volajícímu - ten rozhodne, zda bez kontroly pokračovat.
        """
        bars = await self.ib.minute_bars(contract, since)
        bar = calc.first_entry_cross(right, entry_price, bars)
        if bar is None:
            return None
        strana = calc.entry_cross_side(right)
        # Čas svíčky přichází z TWS v UTC, obchodníkovi se hodí čas burzy
        return (
            f"Podklad překročil vstup {entry_price:g} už v "
            f"{bar.date.astimezone(since.tzinfo):%H:%M} čas burzy "
            f"(svíčka {strana} {getattr(bar, strana):g})"
        )

    def _entry_missed(self, flow: Flow) -> bool:
        """
        Ukončí obchod před nákupem, jehož vstupní úroveň už podklad překonal.

        Kontrola běží ve všech stavech před nákupem, takže propásnutý vstup
        obchod ukončí i mimo obchodní hodiny. Dřív se odhalil až při pokusu
        o zadání příkazu, a tak obchod čekající na uvolnění spreadu (před
        otevřením trhu je spread opce zpravidla široký) visel v přehledu
        dál a po otevření trhu nakupoval na už propásnuté úrovni.

        Příkaz už zadaný do trhu se ruší jen tehdy, když jeho cenová podmínka
        spustit nemůže - tedy mimo obchodní hodiny a jen pokud podmínka
        nepracuje i mimo ně (`trading.outside_rth`). Během seance by kontrola
        závodila s dobíhajícím vyplněním a zrušila by příkaz, který se plní.

        Vrací True, pokud byl obchod ukončen.
        """
        cena = self.ib.underlying_price(flow.underlying_contract)
        if cena is None:
            return False

        # Příkaz v trhu smí kontrola sundat, jen když nehrozí souběh s vyplněním
        if flow.state == FlowState.ARMED:
            mimo_hodiny = self.market_open_seconds() is not None
            if not mimo_hodiny or self.cfg.trading.outside_rth:
                return False

        if calc.entry_still_valid(flow.right, cena, flow.entry_price):
            return False

        # Nevyplněný příkaz nesmí v TWS zůstat, jinak by po otevření trhu koupil
        v_trhu = flow.entry_trade is not None
        if v_trhu:
            self.ib.cancel(flow.entry_trade)
            flow.entry_trade = None
            flow.entry_order_id = None

        smer = "nad" if flow.right == "C" else "pod"
        zaver = (
            "příkaz odstraněn z trhu a obchod ukončen"
            if v_trhu
            else "obchod ukončen bez zadání příkazu"
        )
        self._end_before_entry(
            flow,
            FlowState.MISSED,
            f"Cena podkladu {cena:g} je {smer} vstupem {flow.entry_price:g} - "
            f"vstup propásnut, {zaver}.",
        )
        return True

    def _can_rearm(self, flow: Flow) -> bool:
        """
        Posoudí, zda lze příkaz vrátit do trhu po zablokování spreadem.
        Spread musí klesnout s rezervou pod limit a od odstranění příkazu
        musí uplynout prodleva (viz _rearm_delay) - jinak by se příkaz při
        kolísání spreadu kolem limitu opakovaně zadával a rušil. Levné opce
        s malým spreadem v USD projdou i bez rezervy - spread v celých
        centech rezervu pod hranicí nemá jak splnit, kolísání pak tlumí
        jen prodleva.
        """
        if flow.option_spread_pct is None:
            return False

        trading = self.cfg.trading
        prah = flow.max_spread_pct * (1.0 - trading.rearm_spread_margin_pct / 100.0)
        if flow.spread_over_limit(prah):
            return False

        if flow.blocked_since is not None:
            uplynulo = (datetime.now() - flow.blocked_since).total_seconds()
            if uplynulo < self._rearm_delay(flow):
                return False

        return True

    def _rearm_delay(self, flow: Flow) -> float:
        """
        Prodleva před návratem příkazu do trhu po odstranění kvůli spreadu.

        Začíná na trading.rearm_delay_sec a každé další odstranění u téhož
        obchodu ji vynásobí trading.rearm_delay_factor, nejvýš na
        trading.rearm_delay_max_sec. U levné opce, kde jediný tik posune
        spread přes limit a zpět, by jinak cyklus zrušení a nového zadání
        běžel každých pár sekund a vyčerpal limit Order Efficiency Ratio.
        """
        trading = self.cfg.trading
        zaklad = trading.rearm_delay_sec
        strop = max(trading.rearm_delay_max_sec, zaklad)
        # Prodleva se násobí po krocích a na stropu se skončí - mocnina by
        # při dlouho kolísajícím spreadu přetekla rozsah čísla
        prodleva = zaklad
        for _ in range(max(flow.spread_breaches - 1, 0)):
            if prodleva >= strop:
                break
            prodleva *= trading.rearm_delay_factor
        return min(prodleva, strop)

    def _update_entry_limit(self, flow: Flow) -> bool:
        """
        Přepočítá limitní cenu nákupního příkazu podle aktuálního ASK / MID.
        Příkaz se modifikuje jen při změně větší než práh z konfigurace,
        aby se TWS nezahlcovala drobnými úpravami.

        Každá úprava je pro IBKR zpráva, která zvyšuje Order Efficiency Ratio.
        Příkaz čekající na splnění cenové podmínky (PreSubmitted) proto
        ve výchozím nastavení upravován není (viz relimit_before_trigger)
        a úprava vůbec projde jen tehdy, když ji dovolí limit OER.
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

        # Podmíněný příkaz, jehož podmínka ještě nespustila, drží TWS u sebe
        # a na burzu ho pošle až v okamžiku spuštění. Dnešní ASK o ceně opce
        # v tu chvíli mnoho neříká a úpravy za každým tikem kotace plýtvají
        # zprávami - limit se srovná, až příkaz na burze skutečně čeká
        # (stav Submitted), případně při průběžném přepočtu množství.
        # Překonal-li podklad vstup, podmínka spouští, i když TWS stav
        # příkazu ještě nepřepsala - pak se limit upravuje hned
        if (
            flow.entry_trade.orderStatus.status == "PreSubmitted"
            and not self.cfg.trading.relimit_before_trigger
        ):
            cena = self.ib.underlying_price(flow.underlying_contract)
            if cena is None or calc.entry_still_valid(flow.right, cena, flow.entry_price):
                return False

        new_limit = self._entry_limit(flow)
        if new_limit is None or flow.entry_limit is None:
            return False

        change_pct = abs(new_limit - flow.entry_limit) / flow.entry_limit * 100.0
        if change_pct < self.cfg.trading.relimit_min_change_pct:
            return False

        # Přelimitování je nepovinné - jen dokud se vejde do limitu OER
        if not self._oer_allows(1, flow, "přelimitování nákupního příkazu"):
            return False

        order = flow.entry_trade.order
        order.lmtPrice = new_limit
        # Odeslání příkazu se stejným orderId znamená jeho modifikaci
        flow.entry_trade = self.ib.place(flow.option_contract, order)
        flow.entry_limit = new_limit
        flow.touch()
        return True

    def _apply_sl_spread(self, flow: Flow) -> None:
        """
        Připočte k SL zadanému na opci spread zaplacený při nákupu.

        Nakupuje se u ASKu, ale stop se spouští BIDem, takže SL je bez
        kompenzace blíž o celý spread. Připočtením se zadaná hodnota stane
        skutečnou vzdáleností k SL - ztráta na kontrakt o tentýž spread
        naroste (množství to už zohlednilo v náhledu).

        Uplatní se jen jednou (podruhé už je navýšení součástí uložené
        hodnoty) a nikdy u break even, který má stát na nákupní ceně.
        Neznámý BID kompenzaci ruší - odhadovat ji naslepo by posunulo
        stop mimo zadání.
        """
        if not flow.sl_spread_compensated or flow.sl_on_underlying:
            return
        if flow.sl_spread_usd or flow.stop_loss <= 0:
            return

        bid, _, _ = self.ib.option_quotes(flow.option_contract)
        if bid is None:
            bid = flow.option_bid
        spread = calc.paid_spread_usd(flow.fill_price, bid)
        if spread <= 0:
            self.log_event(
                f"{flow.id}: spread při nákupu nelze určit (chybí BID opce) - "
                f"SL zůstává na {flow.level_text('sl')} bez kompenzace."
            )
            return

        flow.sl_spread_usd = spread
        flow.stop_loss = round(flow.stop_loss + spread, 2)
        # Počáteční SL slouží tlačítku "Počáteční SL" - musí se posunout také,
        # jinak by se obchod vracel na nekompenzovanou úroveň. Porovnává se
        # s None, ne pravdivostí: nula je u SL na opci platná úroveň (break
        # even), kterou by truthiness tiše přeskočila
        if flow.original_stop_loss is not None:
            flow.original_stop_loss = round(flow.original_stop_loss + spread, 2)
        # Runner zapnutý ještě před nákupem si nese SL z doby zadání
        if flow.runner_stop_loss is not None:
            flow.runner_stop_loss = round(flow.runner_stop_loss + spread, 2)
        self.log_event(
            f"{flow.id}: SL navýšen o zaplacený spread {spread:g} USD/ks "
            f"na {flow.level_text('sl')}."
        )

    def _register_fill(self, flow: Flow) -> bool:
        """Zaznamená nákup opce a připraví flow na zadání výstupního příkazu."""
        status = flow.entry_trade.orderStatus
        flow.fill_price = valid_price(status.avgFillPrice) or flow.entry_limit
        flow.fill_time = datetime.now()
        flow.filled_quantity = int(status.filled)
        # Kompenzace SL o spread se počítá ze skutečné nákupní ceny, proto až teď
        self._apply_sl_spread(flow)
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
                or flow.entry_trade.orderStatus.status in SETTLED_ORDER_STATES
            )
        ):
            flow.clear_runner()
            self.log_event(
                f"{flow.id}: runner zrušen - nakoupené množství "
                f"{flow.held_quantity} ks na něj nestačí."
            )
            changed = True

        # --- runner ---
        # Runner bez vlastního příkazu v trhu (odložený při částečném nákupu)
        # žádné kusy nedrží - kryje je hlavní prodejní příkaz. _part_out_of_market
        # by nad prázdným seznamem vrátil True a tržní prodej níže by vytvořil
        # nekrytou krátkou pozici; vyžádané uzavření se proto zahodí a runner
        # zruší až větev níže, které tím přestane překážet
        if (
            flow.runner_active
            and flow.runner_close_requested
            and flow.runner_fill_price is None
            and not self._legs(flow, "runner")
        ):
            flow.runner_close_requested = False
            self.log_event(
                f"{flow.id}: runner nemá vlastní příkaz v trhu - "
                f"uzavření trhem se neprovádí."
            )
            changed = True

        # Vyžádané uzavření runneru trhem: jakmile TWS potvrdí zrušení
        # podmíněných příkazů, zadá se prodej trhem
        if (
            flow.runner_active
            and flow.runner_close_requested
            and flow.runner_fill_price is None
            and self._part_out_of_market(flow, "runner")
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
            # Příkazy runneru už nemohou prodat (zrušené mimo aplikaci, nebo
            # vyplněné na menší množství) - jeho zbylé kusy se vrací pod hlavní
            # příkaz, aby pozice nezůstala částečně nezajištěná
            zbylo = flow.runner_quantity
            if flow.exit_fill_price is None and self._part_modifiable(flow, "exit"):
                self._clear_part(flow, "runner")
                flow.clear_runner()
                self._resize_part(
                    flow, "exit", flow.held_quantity - flow.main_sold_quantity
                )
                self.log_event(
                    f"{flow.id}: příkaz runneru už není v trhu - jeho zbylé kusy "
                    f"({zbylo} ks) převzal hlavní prodejní příkaz."
                )
            else:
                flow.set_state(
                    FlowState.ERROR,
                    f"Příkaz runneru už není v trhu a nelze jej nahradit - "
                    f"{zbylo} ks pozice je bez zajištění.",
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
            and self._part_out_of_market(flow, "exit")
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
        if klic in self._warned:
            return False
        self._warned.add(klic)

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

        # Dokud je jakýkoliv dřívější příkaz živý, tržní prodej by se s ním
        # sčítal a prodalo by se více kusů, než pozice drží. Nestačí přitom
        # čekat jen na odchod z MODIFIABLE_ORDER_STATES - příkaz v "PendingCancel"
        # už upravit nelze, ale TWS jej stále může vyplnit
        for trade in (
            flow.entry_trade,
            flow.exit_trade,
            flow.exit_sl_trade,
            flow.runner_trade,
            flow.runner_sl_trade,
        ):
            if self._order_live(trade):
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
                        (cena - flow.fill_price)
                        * flow.runner_quantity
                        * calc.OPTION_MULTIPLIER
                    )
                flow.runner_sold_quantity += flow.runner_quantity
                flow.clear_runner()
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
            flow.clear_runner()
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
                # Částečné vyplnění před zrušením - stejný formát prodáno/celkem
                # jako u běžného částečného prodeje hlavní části
                self.log_event(
                    f"{flow.id}: částečně vyplněno {flow.main_sold_quantity}/{flow.main_quantity} ks "
                    f"hlavní části ({duvod}) za {cena:g} ještě před zrušením příkazů, "
                    f"trhem se prodá zbylých {flow.main_quantity - flow.main_sold_quantity} ks."
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
                # Částečné vyplnění - hláška ukazuje celkový stav prodeje
                # hlavní části (prodáno/celkem), aby nevypadala jako prodej celé části
                self.log_event(
                    f"{flow.id}: částečně vyplněno {flow.main_sold_quantity}/{flow.main_quantity} ks "
                    f"hlavní části ({duvod}) za {cena:g}, "
                    f"zbývá {flow.main_quantity - flow.main_sold_quantity} ks."
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
