"""
Náhrada spojení s TWS pro testy.
Dědí z ostré služby, takže se testuje skutečné sestavování příkazů i podmínek
ib_async; nahrazují se pouze metody, které komunikují se sítí.
"""

from __future__ import annotations

from ib_async import Contract, ContractDetails, Option, OptionChain, OrderStatus, Stock, Trade

from tws_opce.config import AppConfig
from tws_opce.ib_service import IBService, PositionInfo

# Identifikátory kontraktů používané v testech
UNDERLYING_CONID = 265598
OPTION_CONID = 700001


class FakeIBService(IBService):
    """Služba s předepsanými cenami a pamětí zadaných příkazů."""

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__(cfg)
        self.account = "DU000000"
        # Ceny, které test nastavuje a mění mezi jednotlivými průchody smyčkou
        self.price_underlying: float | None = 230.0
        self.price_bid: float | None = 3.00
        self.price_ask: float | None = 3.10
        self.greek_delta: float | None = 0.35
        # Velikost účtu vracená místo dotazu do TWS
        self.net_liquidation_value: float | None = 12345.0
        # Záznam odeslaných a zrušených příkazů
        self.placed: list[Trade] = []
        self.cancelled: list[Trade] = []
        # Opční pozice na účtu podle conId - test je nastavuje pro scénáře obnovy
        self.held_positions: dict[int, float] = {}
        self._next_order_id = 1

    # --- spojení ---

    @property
    def connected(self) -> bool:
        """Testovací služba je vždy připojená."""
        return True

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
            expirations=["20991218", "20991224"],
            strikes=[225.0, 230.0, 232.5, 235.0, 240.0],
        )

    async def qualify_option(
        self, symbol: str, expiration: str, strike: float, right: str, trading_class: str = ""
    ) -> tuple[Contract, ContractDetails]:
        """Vrátí opční kontrakt s minimálním tikem 0,05."""
        option = Option(symbol, expiration, strike, right, "SMART", currency="USD")
        option.conId = OPTION_CONID
        option.multiplier = "100"
        details = ContractDetails(contract=option, minTick=0.05)
        return option, details

    # --- tržní data ---

    def subscribe(self, contract: Contract):
        """Odběr se v testech nezakládá."""
        return None

    def unsubscribe(self, contract: Contract | None) -> None:
        """Odběr se v testech neruší."""
        return None

    async def wait_for_quotes(self, contract: Contract, timeout: float) -> None:
        """Ceny jsou k dispozici okamžitě."""
        return None

    def underlying_price(self, contract: Contract | None) -> float | None:
        """Aktuální cena podkladu nastavená testem."""
        return self.price_underlying

    def option_quotes(self, contract: Contract | None):
        """Kotace a delta opce nastavené testem."""
        return self.price_bid, self.price_ask, self.greek_delta

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
