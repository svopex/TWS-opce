"""
Obálka nad ib_async - spojení s TWS, výběr kontraktů, tržní data a příkazy.
Všechny metody jsou asynchronní a počítají s během ve smyčce NiceGUI.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any, Callable

from ib_async import (
    IB,
    Contract,
    ContractDetails,
    LimitOrder,
    MarketOrder,
    Option,
    Order,
    PriceCondition,
    OrderStatus,
    Stock,
    StopOrder,
    Ticker,
    Trade,
)

from .config import AppConfig

log = logging.getLogger(__name__)

# Předpona značky, kterou aplikace označuje své příkazy v poli orderRef.
# Podle ní je po restartu pozná i bez uloženého stavu.
ORDER_REF_PREFIX = "TWSOPCE"

# Typ OCA skupiny: 2 = po (i částečném) vyplnění jednoho příkazu TWS
# ostatní příkazy ve skupině úměrně zmenší, při úplném vyplnění zruší,
# a po dobu zpracování je blokuje proti přeplnění. Oproti typu 1 (zrušit
# vše) tak po částečném prodeji na PT zůstává zbytek pozice dál krytý SL.
OCA_TYPE_REDUCE_WITH_BLOCK = 2


def order_ref(flow_id: str, druh: str) -> str:
    """Sestaví značku příkazu, například 'TWSOPCE:AAPL-1:entry'."""
    return f"{ORDER_REF_PREFIX}:{flow_id}:{druh}"


def parse_order_ref(ref: str) -> tuple[str, str] | None:
    """
    Rozloží značku příkazu na identifikátor obchodu a druh příkazu.
    Vrací None, pokud značka nepochází z této aplikace.
    """
    if not ref or not ref.startswith(f"{ORDER_REF_PREFIX}:"):
        return None
    casti = ref.split(":")
    if len(casti) != 3:
        return None
    return casti[1], casti[2]


@dataclass
class PositionInfo:
    """Držená opční pozice na účtu."""

    conid: int
    quantity: float
    # Popis kontraktu, například 'META  260819P00545000'
    label: str
    # Ticker podkladu, podle kterého lze pozici přiřadit k obchodu
    symbol: str


def valid_price(value: float | None) -> float | None:
    """
    Ověří, že cena z TWS je použitelná.
    TWS posílá u chybějících kotací NaN nebo -1, takové hodnoty se zahazují.
    """
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)


class IBService:
    """Spravuje jedno spojení na TWS a odběry tržních dat."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.ib = IB()
        self.account: str = cfg.connection.account
        # Odebírané tickery podle conId, aby se stejný kontrakt neodebíral vícekrát
        self._tickers: dict[int, Ticker] = {}
        # Počet flow, která daný kontrakt používají - odběr se ruší až při posledním
        self._subscribers: dict[int, int] = {}
        self._connect_lock = asyncio.Lock()
        self._chain_cache: dict[str, Any] = {}
        self.on_status_change: Callable[[], None] | None = None

        self.ib.disconnectedEvent += self._on_disconnected
        self.ib.errorEvent += self._on_error

    # ------------------------------------------------------------------
    # Spojení
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Stav spojení na TWS."""
        return self.ib.isConnected()

    async def connect(self) -> None:
        """
        Naváže spojení na TWS a nastaví typ tržních dat.
        Opakované volání při již navázaném spojení nic neprovede.
        """
        async with self._connect_lock:
            if self.ib.isConnected():
                return

            conn = self.cfg.connection
            log.info("Připojuji se k TWS %s:%s (clientId=%s)", conn.host, conn.port, conn.client_id)
            await self.ib.connectAsync(
                host=conn.host,
                port=conn.port,
                clientId=conn.client_id,
                timeout=conn.connect_timeout,
                readonly=conn.readonly,
                account=conn.account,
            )
            # Typ tržních dat - live / frozen / delayed podle konfigurace
            self.ib.reqMarketDataType(conn.market_data_type)

            # Účet z konfigurace má přednost, jinak se použije první dostupný
            if not self.account:
                accounts = self.ib.managedAccounts()
                self.account = accounts[0] if accounts else ""

            log.info("Spojení navázáno, účet: %s", self.account or "(neurčen)")
            self._notify_status()

    async def disconnect(self) -> None:
        """Ukončí spojení a zruší všechny odběry tržních dat."""
        for conid in list(self._tickers):
            self._cancel_ticker(conid)
        self._tickers.clear()
        self._subscribers.clear()
        if self.ib.isConnected():
            self.ib.disconnect()

    def _on_disconnected(self) -> None:
        """Reakce na ztrátu spojení - odběry už nejsou platné."""
        log.warning("Spojení s TWS bylo přerušeno.")
        self._tickers.clear()
        self._subscribers.clear()
        self._notify_status()

    def _on_error(self, reqId: int, errorCode: int, errorString: str, contract: Any) -> None:
        """
        Logování chyb z TWS. Kódy 2100-2199 jsou pouze informativní hlášení
        (například stav datového spojení), proto se logují jen jako info.
        """
        if 2100 <= errorCode < 2200:
            log.info("TWS info %s: %s", errorCode, errorString)
        else:
            local_symbol = getattr(contract, "localSymbol", "") if contract is not None else ""
            desc = f" [{local_symbol}]" if local_symbol else ""
            log.error("TWS chyba %s (reqId=%s)%s: %s", errorCode, reqId, desc, errorString)

    def _notify_status(self) -> None:
        """Informuje UI o změně stavu spojení."""
        if self.on_status_change:
            try:
                self.on_status_change()
            except Exception:
                log.exception("Chyba při notifikaci změny stavu spojení.")

    # ------------------------------------------------------------------
    # Účet
    # ------------------------------------------------------------------

    async def net_liquidation(self) -> float | None:
        """Vrátí aktuální NetLiquidation účtu z TWS, nebo None při nedostupnosti."""
        if not self.connected:
            return None
        try:
            values = await self.ib.accountSummaryAsync(self.account)
        except Exception:
            log.exception("Nepodařilo se načíst souhrn účtu.")
            return None

        for v in values:
            if v.tag == "NetLiquidation":
                try:
                    return float(v.value)
                except ValueError:
                    return None
        return None

    # ------------------------------------------------------------------
    # Kontrakty
    # ------------------------------------------------------------------

    async def qualify_stock(self, symbol: str) -> Contract:
        """Doplní identifikátory akciového kontraktu podle tickeru."""
        stock = Stock(symbol.upper().strip(), self.cfg.trading.exchange, self.cfg.trading.currency)
        qualified = await self.ib.qualifyContractsAsync(stock)
        if not qualified or qualified[0] is None:
            raise ValueError(f"Ticker '{symbol}' se nepodařilo najít v TWS.")
        return qualified[0]

    async def option_chain(self, underlying: Contract) -> Any:
        """
        Načte parametry opčního řetězce pro podklad (expirace a strike ceny).
        Výsledek se kešuje, protože se v průběhu dne nemění.
        """
        cache_key = f"{underlying.symbol}:{underlying.conId}"
        if cache_key in self._chain_cache:
            return self._chain_cache[cache_key]

        chains = await self.ib.reqSecDefOptParamsAsync(
            underlying.symbol, "", underlying.secType, underlying.conId
        )
        if not chains:
            raise ValueError(f"Pro ticker '{underlying.symbol}' nejsou dostupné opce.")

        # Preferuje se řetězec na SMART s obchodní třídou shodnou s tickerem (standardní opce)
        preferred = [c for c in chains if c.exchange == "SMART" and c.tradingClass == underlying.symbol]
        if not preferred:
            preferred = [c for c in chains if c.exchange == "SMART"]
        if not preferred:
            preferred = chains

        chain = max(preferred, key=lambda c: len(c.expirations))
        self._chain_cache[cache_key] = chain
        return chain

    async def qualify_option(
        self, symbol: str, expiration: str, strike: float, right: str, trading_class: str = ""
    ) -> tuple[Contract, ContractDetails]:
        """
        Sestaví a ověří opční kontrakt, vrátí kontrakt včetně detailů
        (kvůli minimálnímu tiku pro zaokrouhlování limitních cen).
        """
        option = Option(
            symbol=symbol,
            lastTradeDateOrContractMonth=expiration,
            strike=strike,
            right=right,
            exchange=self.cfg.trading.exchange,
            currency=self.cfg.trading.currency,
        )
        if trading_class:
            option.tradingClass = trading_class

        details = await self.ib.reqContractDetailsAsync(option)
        if not details:
            raise ValueError(
                f"Opční kontrakt {symbol} {expiration} {right} {strike:g} není v TWS dostupný."
            )
        # Při více variantách se vybírá ta s nejmenším multiplikátorem (standardní kontrakt)
        detail = min(details, key=lambda d: float(d.contract.multiplier or 100))
        return detail.contract, detail

    # ------------------------------------------------------------------
    # Tržní data
    # ------------------------------------------------------------------

    def subscribe(self, contract: Contract) -> Ticker:
        """
        Zahájí (nebo znovu použije) odběr tržních dat kontraktu.
        Počítadlo odběratelů zajišťuje, že se data zruší až po posledním flow.
        """
        conid = contract.conId
        if conid in self._tickers:
            self._subscribers[conid] = self._subscribers.get(conid, 0) + 1
            return self._tickers[conid]

        ticker = self.ib.reqMktData(contract, "", False, False)
        self._tickers[conid] = ticker
        self._subscribers[conid] = 1
        return ticker

    def unsubscribe(self, contract: Contract | None) -> None:
        """Sníží počet odběratelů kontraktu a při nule zruší odběr dat."""
        if contract is None:
            return
        conid = contract.conId
        if conid not in self._subscribers:
            return

        self._subscribers[conid] -= 1
        if self._subscribers[conid] <= 0:
            self._cancel_ticker(conid)

    def _cancel_ticker(self, conid: int) -> None:
        """Zruší odběr tržních dat daného kontraktu."""
        ticker = self._tickers.pop(conid, None)
        self._subscribers.pop(conid, None)
        if ticker is None or not self.ib.isConnected():
            return
        try:
            self.ib.cancelMktData(ticker.contract)
        except Exception:
            log.exception("Nepodařilo se zrušit odběr tržních dat conId=%s.", conid)

    def ticker(self, contract: Contract | None) -> Ticker | None:
        """Vrátí odebíranou strukturu s cenami daného kontraktu."""
        if contract is None:
            return None
        return self._tickers.get(contract.conId)

    def underlying_price(self, contract: Contract | None) -> float | None:
        """
        Aktuální cena podkladu.
        Přednost má poslední obchod, následuje střed trhu a nakonec závěrečná cena.
        """
        ticker = self.ticker(contract)
        if ticker is None:
            return None

        # midpoint() a marketPrice() jsou metody, markPrice je datové pole
        for candidate in (ticker.last, ticker.midpoint(), ticker.markPrice, ticker.close):
            price = valid_price(candidate)
            if price is not None:
                return price
        return None

    def option_quotes(self, contract: Contract | None) -> tuple[float | None, float | None, float | None]:
        """Vrátí trojici (bid, ask, delta) opčního kontraktu z odebíraných dat."""
        ticker = self.ticker(contract)
        if ticker is None:
            return None, None, None

        bid = valid_price(ticker.bid)
        ask = valid_price(ticker.ask)

        # Delta z modelu TWS, při nedostupnosti z posledního obchodu nebo kotací
        delta = None
        for greeks in (ticker.modelGreeks, ticker.lastGreeks, ticker.bidGreeks, ticker.askGreeks):
            if greeks is not None and greeks.delta is not None and math.isfinite(greeks.delta):
                delta = float(greeks.delta)
                break

        return bid, ask, delta

    async def wait_for_quotes(self, contract: Contract, timeout: float) -> None:
        """
        Počká, než TWS pošle první použitelná data kontraktu.
        Po vypršení časového limitu se pokračuje i bez nich.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            ticker = self.ticker(contract)
            if ticker is not None:
                # Stačí jakákoliv použitelná cena - kotace nebo závěrečná cena
                if any(
                    valid_price(v) is not None
                    for v in (ticker.bid, ticker.ask, ticker.last, ticker.close)
                ):
                    return
            await asyncio.sleep(0.2)

    # ------------------------------------------------------------------
    # Příkazy
    # ------------------------------------------------------------------

    def price_condition(self, conid: int, is_more: bool, price: float) -> PriceCondition:
        """
        Sestaví cenovou podmínku na podkladovém aktivu.
        is_more = True znamená 'cena podkladu je vyšší nebo rovna zadané hodnotě'.
        """
        return PriceCondition(
            conId=conid,
            exch=self.cfg.trading.exchange,
            isMore=is_more,
            price=price,
            triggerMethod=self.cfg.trading.trigger_method,
        )

    def build_entry_order(
        self,
        quantity: int,
        limit_price: float | None,
        conditions: list[PriceCondition],
        ref: str = "",
    ) -> Order:
        """
        Sestaví nákupní příkaz s cenovou podmínkou na podkladu.
        Bez limitní ceny vznikne tržní příkaz, jinak limitní.
        """
        trading = self.cfg.trading
        if limit_price is None:
            order = MarketOrder("BUY", quantity)
        else:
            order = LimitOrder("BUY", quantity, limit_price)

        order.tif = trading.tif
        order.outsideRth = trading.outside_rth
        order.conditions = list(conditions)
        # False = podmínky spouští odeslání příkazu (nikoliv jeho zrušení)
        order.conditionsCancelOrder = False
        order.conditionsIgnoreRth = trading.outside_rth
        if ref:
            order.orderRef = ref
        if self.account:
            order.account = self.account
        return order

    def _finish_sell_order(self, order: Order, ref: str, oca_group: str) -> Order:
        """
        Doplní prodejnímu příkazu společné atributy: platnost, účet, značku
        a případnou OCA skupinu. Příkazy ve stejné OCA skupině TWS sama
        váže dohromady - vyplnění jednoho zmenší či zruší ostatní, a to
        i kdyby aplikace zrovna neběžela.
        """
        trading = self.cfg.trading
        order.tif = trading.tif
        order.outsideRth = trading.outside_rth
        if ref:
            order.orderRef = ref
        if oca_group:
            order.ocaGroup = oca_group
            order.ocaType = OCA_TYPE_REDUCE_WITH_BLOCK
        if self.account:
            order.account = self.account
        return order

    def build_exit_order(
        self,
        quantity: int,
        limit_price: float | None,
        conditions: list[PriceCondition],
        ref: str = "",
        oca_group: str = "",
    ) -> Order:
        """
        Sestaví prodejní příkaz s cenovými podmínkami na podkladu.
        S oběma podmínkami (PT i SL) jde o jediný příkaz pokrývající celý
        výstup; s jedinou podmínkou o jednu ze dvou částí výstupu, kdy
        druhou hlídá příkaz přímo na cenu opce.
        Podmínky jsou spojeny logickým OR - stačí splnit jednu z nich.
        """
        trading = self.cfg.trading
        if trading.exit_order_type == "MKT" or limit_price is None:
            order = MarketOrder("SELL", quantity)
        else:
            order = LimitOrder("SELL", quantity, limit_price)

        order.conditions = self.prepare_conditions(conditions)
        order.conditionsCancelOrder = False
        order.conditionsIgnoreRth = trading.outside_rth
        return self._finish_sell_order(order, ref, oca_group)

    @staticmethod
    def prepare_conditions(conditions: list[PriceCondition]) -> list[PriceCondition]:
        """
        Nastaví spojky podmínek tak, aby stačilo splnit kteroukoliv z nich.

        Spojka uložená v podmínce ji váže k NÁSLEDUJÍCÍ podmínce, nikoliv
        k předchozí. Proto nesou všechny kromě poslední 'o' (OR); u poslední
        se spojka již neuplatní. Ověřeno proti TWS: opačné pořadí vede
        k AND a příkaz se nikdy nespustí.
        """
        prepared: list[PriceCondition] = []
        posledni = len(conditions) - 1
        for index, cond in enumerate(conditions):
            cond.conjunction = "a" if index == posledni else "o"
            prepared.append(cond)
        return prepared

    def build_limit_sell_order(
        self, quantity: int, limit_price: float, ref: str = "", oca_group: str = ""
    ) -> Order:
        """
        Limitní prodejní příkaz přímo na cenu opce - realizuje PT zadané
        ziskem v USD na kontrakt. Bez podmínek na podkladu.
        """
        order = LimitOrder("SELL", quantity, limit_price)
        return self._finish_sell_order(order, ref, oca_group)

    def build_stop_sell_order(
        self, quantity: int, stop_price: float, ref: str = "", oca_group: str = ""
    ) -> Order:
        """
        Stop-market prodejní příkaz přímo na cenu opce - realizuje SL zadaný
        ztrátou v USD na kontrakt. Po dosažení stop ceny se prodá trhem.
        """
        order = StopOrder("SELL", quantity, stop_price)
        return self._finish_sell_order(order, ref, oca_group)

    async def app_trades(self) -> dict[str, Trade]:
        """
        Vrátí příkazy založené touto aplikací, klíčované značkou z orderRef.
        Používá se po restartu k dohledání příkazů, které v TWS zůstaly.

        Načítají se i dokončené příkazy - podle vyplněného nákupu aplikace pozná,
        že jí patří otevřená pozice, ke které se má doplnit zajištění.
        """
        try:
            await self.ib.reqAllOpenOrdersAsync()
        except Exception:
            log.exception("Otevřené příkazy se nepodařilo z TWS načíst.")
        try:
            # apiOnly=False vrací i příkazy zadané ručně v TWS; filtruje se dále podle značky
            await self.ib.reqCompletedOrdersAsync(False)
        except Exception:
            log.exception("Dokončené příkazy se nepodařilo z TWS načíst.")

        nalezene: dict[str, Trade] = {}
        for trade in self.ib.trades():
            ref = trade.order.orderRef or ""
            if parse_order_ref(ref) is None:
                continue
            # Pod jednou značkou může být více záznamů; přednost má vyplněný příkaz
            drivejsi = nalezene.get(ref)
            if drivejsi is not None and drivejsi.orderStatus.filled >= trade.orderStatus.filled:
                continue
            nalezene[ref] = trade
        return nalezene

    async def positions(self) -> dict[int, PositionInfo]:
        """Vrátí držené opční pozice podle conId kontraktu."""
        try:
            await self.ib.reqPositionsAsync()
        except Exception:
            log.exception("Pozice se nepodařilo z TWS načíst.")
            return {}
        return {
            p.contract.conId: PositionInfo(
                conid=p.contract.conId,
                quantity=p.position,
                label=p.contract.localSymbol or p.contract.symbol,
                symbol=p.contract.symbol,
            )
            for p in self.ib.positions()
            if p.contract.secType == "OPT" and p.position
        }

    def market_sell_order(self, quantity: int, ref: str = "") -> Order:
        """Prodejní příkaz bez podmínek - okamžité uzavření pozice."""
        order = MarketOrder("SELL", quantity)
        # Tržní příkaz má projít okamžitě, GTC z konfigurace u něj nemá smysl.
        # TWS (přinejmenším demo) navíc GTC MKT drží ve stavu PreSubmitted
        # a nevyplní ho; s platností DAY se provádí ihned.
        order.tif = "DAY"
        order.outsideRth = self.cfg.trading.outside_rth
        if ref:
            order.orderRef = ref
        if self.account:
            order.account = self.account
        return order

    def place(self, contract: Contract, order: Order) -> Trade:
        """Odešle příkaz do TWS a vrátí objekt sledující jeho stav."""
        return self.ib.placeOrder(contract, order)

    def cancel(self, trade: Trade | None) -> None:
        """Zruší dříve zadaný příkaz, pokud je ještě aktivní."""
        if trade is None or not self.ib.isConnected():
            return
        # Ruší se jen příkaz, který je v TWS stále aktivní. Vyplněný, již zrušený
        # i rušený příkaz TWS odmítá hlášením, které vyskočí uživateli na obrazovku.
        if trade.orderStatus.status not in OrderStatus.ActiveStates:
            return
        try:
            self.ib.cancelOrder(trade.order)
        except Exception:
            log.exception("Příkaz orderId=%s se nepodařilo zrušit.", trade.order.orderId)
