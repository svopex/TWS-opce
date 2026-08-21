"""
Náhrada spojení s TWS pro testy.
Dědí z ostré služby, takže se testuje skutečné sestavování příkazů i podmínek
ib_async; nahrazují se pouze metody, které komunikují se sítí.
"""

from __future__ import annotations

from datetime import date, timedelta

from ib_async import (
    Contract,
    ContractDetails,
    Option,
    OptionChain,
    OptionComputation,
    OrderStatus,
    Stock,
    Ticker,
    Trade,
)

from tws_opce.config import AppConfig
from tws_opce.ib_service import IBService, PositionInfo

# Identifikátory kontraktů používané v testech
UNDERLYING_CONID = 265598
OPTION_CONID = 700001

# Nevyplněná hodnota tržních dat - stejně jako ji posílá ib_async
NAN = float("nan")


class FakeIBService(IBService):
    """Služba s předepsanými cenami a pamětí zadaných příkazů."""

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__(cfg)
        self.account = "DU000000"
        # Ceny, které test nastavuje a mění mezi jednotlivými průchody smyčkou
        self.price_underlying: float | None = 230.0
        self.price_bid: float | None = 3.00
        self.price_ask: float | None = 3.10
        # Poslední obchod a závěrečná cena opce - náhrada, když kotace chybí
        self.price_last: float | None = None
        self.price_close: float | None = None
        self.greek_delta: float | None = 0.35
        # Velikost účtu vracená místo dotazu do TWS
        self.net_liquidation_value: float | None = 12345.0
        # Test může spojení shodit a znovu navázat
        self.connected_flag: bool = True
        # Záznam odeslaných a zrušených příkazů
        self.placed: list[Trade] = []
        self.cancelled: list[Trade] = []
        # Opční pozice na účtu podle conId - test je nastavuje pro scénáře obnovy
        self.held_positions: dict[int, float] = {}
        # Strike ceny, které řetězec nabízí, ale kontrakt pro ně v TWS neexistuje
        self.unavailable_strikes: set[float] = set()
        # Počet odběratelů tržních dat podle conId - testy tak odhalí odběr,
        # který se po chybě nebo zrušení přípravy neuvolnil
        self.subscribed: dict[int, int] = {}
        self._next_order_id = 1

    # --- spojení ---

    @property
    def connected(self) -> bool:
        """Stav spojení, který test přepíná pro scénáře výpadku."""
        return self.connected_flag

    async def connect(self) -> None:
        """
        Připojení bez sítě.
        Zásadní pojistka: bez tohoto předefinování by testy volaly skutečné
        připojení k TWS a mohly by zasáhnout do běžící aplikace.
        """
        self.connected_flag = True

    async def disconnect(self) -> None:
        """Odpojení bez sítě."""
        self.connected_flag = False

    async def net_liquidation(self) -> float | None:
        """Velikost účtu nastavená testem."""
        return self.net_liquidation_value

    # --- kontrakty ---

    async def qualify_stock(self, symbol: str) -> Contract:
        """Vrátí akciový kontrakt bez dotazu do TWS."""
        stock = Stock(symbol.upper(), "SMART", "USD")
        stock.conId = UNDERLYING_CONID
        return stock

    async def option_chain(self, underlying: Contract):
        """Opční řetězec s pevnou nabídkou expirací a strike cen."""
        return OptionChain(
            exchange="SMART",
            underlyingConId=UNDERLYING_CONID,
            tradingClass=underlying.symbol,
            multiplier="100",
            # Expirace se odvozují od dnešního dne, aby výpočty pracovaly
            # s reálným časovým horizontem a testy přitom nezastaraly
            expirations=[
                (date.today() + timedelta(days=dnu)).strftime("%Y%m%d") for dnu in (1, 7)
            ],
            # Strike ceny se odvozují od aktuální ceny podkladu, aby scénáře
            # s libovolnou cenovou hladinou dostaly smysluplný kontrakt
            strikes=self._strikes(),
        )

    def _strikes(self) -> list[float]:
        """Nabídka strike cen po 2,5 bodu kolem aktuální ceny podkladu."""
        stred = round((self.price_underlying or 230.0) / 2.5) * 2.5
        return [round(stred + krok * 2.5, 2) for krok in range(-8, 9)]

    async def qualify_option(
        self, symbol: str, expiration: str, strike: float, right: str, trading_class: str = ""
    ) -> tuple[Contract, ContractDetails]:
        """Vrátí opční kontrakt s minimálním tikem 0,05."""
        # Strike označený testem jako nedostupný se chová jako v ostré službě
        if strike in self.unavailable_strikes:
            raise ValueError(
                f"Opční kontrakt {symbol} {expiration} {right} {strike:g} není v TWS dostupný."
            )
        option = Option(symbol, expiration, strike, right, "SMART", currency="USD")
        option.conId = OPTION_CONID
        option.multiplier = "100"
        details = ContractDetails(contract=option, minTick=0.05)
        return option, details

    # --- tržní data ---

    def subscribe(self, contract: Contract):
        """Odběr se v testech jen počítá, žádná data se z TWS nežádají."""
        self.subscribed[contract.conId] = self.subscribed.get(contract.conId, 0) + 1
        return None

    def unsubscribe(self, contract: Contract | None) -> None:
        """Sníží počet odběratelů kontraktu; při nule záznam zmizí."""
        if contract is None:
            return
        conid = contract.conId
        if conid not in self.subscribed:
            return
        self.subscribed[conid] -= 1
        if self.subscribed[conid] <= 0:
            del self.subscribed[conid]

    async def wait_for_quotes(
        self, contract: Contract, timeout: float, quotes_grace: float = 0.0
    ) -> None:
        """Ceny jsou k dispozici okamžitě."""
        return None

    def ticker(self, contract: Contract | None) -> Ticker | None:
        """
        Sestaví tržní data kontraktu z hodnot nastavených testem.

        Překrývá se záměrně jen tato metoda: výběr ceny podkladu, kotací
        i ceny opce pro model tak prochází ostrou implementací a testy
        odhalí, kdyby se změnilo pořadí zdrojů.
        """
        if contract is None:
            return None

        # Ceny se nastavují až po vytvoření Tickeru - ib_async je v __post_init__
        # přepisuje na nevyplněné hodnoty, konstruktorem je předat nelze
        ticker = Ticker(contract=contract)
        if contract.conId == UNDERLYING_CONID:
            ticker.last = NAN if self.price_underlying is None else self.price_underlying
            return ticker

        ticker.bid = NAN if self.price_bid is None else self.price_bid
        ticker.ask = NAN if self.price_ask is None else self.price_ask
        ticker.last = NAN if self.price_last is None else self.price_last
        ticker.close = NAN if self.price_close is None else self.price_close
        if self.greek_delta is not None:
            ticker.modelGreeks = OptionComputation(tickAttrib=0, delta=self.greek_delta)
        return ticker

    # --- příkazy ---

    def place(self, contract: Contract, order) -> Trade:
        """
        Zaznamená odeslaný příkaz. Opakované odeslání se stejným orderId
        znamená modifikaci, proto se vrací původní záznam s aktualizovaným příkazem.
        """
        for trade in self.placed:
            if trade.order.orderId == order.orderId and order.orderId:
                trade.order = order
                return trade

        if not order.orderId:
            order.orderId = self._next_order_id
            self._next_order_id += 1

        trade = Trade(
            contract=contract,
            order=order,
            orderStatus=OrderStatus(orderId=order.orderId, status="PreSubmitted"),
            fills=[],
            log=[],
        )
        self.placed.append(trade)
        return trade

    def cancel(self, trade: Trade | None) -> None:
        """Zaznamená zrušení příkazu a nastaví odpovídající stav."""
        if trade is None:
            return
        # Stejné omezení jako v ostré službě - neaktivní příkaz se neruší
        if trade.orderStatus.status not in OrderStatus.ActiveStates:
            return
        trade.orderStatus.status = "Cancelled"
        self.cancelled.append(trade)

    async def app_trades(self):
        """Příkazy označené značkou aplikace, klíčované podle orderRef."""
        return {t.order.orderRef: t for t in self.placed if t.order.orderRef}

    async def positions(self) -> dict[int, PositionInfo]:
        """Držené opční pozice nastavené testem."""
        return {
            conid: PositionInfo(
                conid=conid,
                quantity=pocet,
                label=f"KONTRAKT-{conid}",
                symbol="AAPL" if conid == OPTION_CONID else "JINY",
            )
            for conid, pocet in self.held_positions.items()
        }

    # --- pomocné pro testy ---

    def fill(self, trade: Trade, quantity: int, price: float, status: str = "Filled") -> None:
        """Simuluje vyplnění příkazu v TWS."""
        trade.orderStatus.status = status
        trade.orderStatus.filled = quantity
        trade.orderStatus.remaining = max(0, int(trade.order.totalQuantity) - quantity)
        trade.orderStatus.avgFillPrice = price
