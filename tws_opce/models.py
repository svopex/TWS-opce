"""Datové modely obchodního flow a jeho stavu."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

# Popisky typu opce pro zobrazení v UI
RIGHT_LABELS = {"C": "CALL", "P": "PUT"}


class FlowState(str, Enum):
    """Stavy životního cyklu jednoho obchodu."""

    NEW = "NEW"
    ARMED = "ARMED"
    SPREAD_BLOCKED = "SPREAD_BLOCKED"
    NO_QUOTES = "NO_QUOTES"
    FILLED = "FILLED"
    EXIT_ARMED = "EXIT_ARMED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    MISSED = "MISSED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"

    @property
    def label(self) -> str:
        """Český popisek stavu pro monitorovací tabulku."""
        return {
            FlowState.NEW: "Připravuje se",
            FlowState.ARMED: "Před nákupem",
            FlowState.SPREAD_BLOCKED: "Blokováno spreadem",
            FlowState.NO_QUOTES: "Čeká na kotace opce",
            FlowState.FILLED: "Nakoupeno",
            FlowState.EXIT_ARMED: "Nakoupeno – výstup aktivní",
            FlowState.CLOSING: "Uzavírá se",
            FlowState.CLOSED: "Uzavřeno",
            FlowState.MISSED: "Vstup propásnut",
            FlowState.CANCELLED: "Zrušeno",
            FlowState.ERROR: "Chyba",
        }[self]

    @property
    def is_active(self) -> bool:
        """Flow, které ještě vyžaduje pozornost monitorovací smyčky."""
        return self in (
            FlowState.NEW,
            FlowState.ARMED,
            FlowState.SPREAD_BLOCKED,
            FlowState.NO_QUOTES,
            FlowState.FILLED,
            FlowState.EXIT_ARMED,
            FlowState.CLOSING,
        )

    @property
    def is_before_entry(self) -> bool:
        """Flow, u kterého ještě nedošlo k nákupu opce."""
        return self in (
            FlowState.NEW,
            FlowState.ARMED,
            FlowState.SPREAD_BLOCKED,
            FlowState.NO_QUOTES,
        )


@dataclass
class FlowRequest:
    """Zadání obchodu z formuláře."""

    symbol: str
    entry_price: float
    profit_target: float
    stop_loss: float | None = None
    quantity: int | None = None
    max_spread_pct: float | None = None


@dataclass
class Flow:
    """
    Jeden obchod - od zadání příkazu do trhu až po uzavření pozice.
    Uchovává zadané parametry, vybraný opční kontrakt i aktuální tržní data.
    """

    id: str
    symbol: str
    entry_price: float
    profit_target: float
    stop_loss: float
    quantity: int
    max_spread_pct: float
    right: str = "C"

    # PT zadané při založení obchodu; násobky cíle se počítají z něj,
    # aby opakovaná změna nevycházela z už posunuté hodnoty
    original_profit_target: float = 0.0

    # SL zadaný při založení obchodu - tlačítko "Počáteční SL" se na něj
    # vrací poté, co byl stop posunut na break even
    original_stop_loss: float = 0.0

    # Vybraný opční kontrakt
    expiration: str = ""
    strike: float = 0.0
    option_conid: int = 0
    underlying_conid: int = 0
    min_tick: float = 0.01

    state: FlowState = FlowState.NEW
    message: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    # Aktuální tržní data (plní je monitorovací smyčka)
    underlying_price: float | None = None
    option_bid: float | None = None
    option_ask: float | None = None
    option_spread_pct: float | None = None
    delta: float | None = None

    # Stav příkazů
    entry_limit: float | None = None
    entry_order_id: int | None = None
    # Zrušení nevyplněného zbytku nákupu bylo vyžádáno (čeká se na potvrzení TWS)
    entry_cancel_requested: bool = False
    # Kdy byl příkaz naposledy odstraněn z trhu kvůli spreadu
    blocked_since: datetime | None = None
    exit_order_id: int | None = None
    fill_price: float | None = None
    fill_time: datetime | None = None
    # Skutečně nakoupené množství - při částečném plnění je nižší než zadané
    filled_quantity: int = 0
    exit_fill_price: float | None = None
    exit_reason: str = ""

    # Runner - část pozice prodávaná samostatným příkazem s vlastním cílem.
    # None v runner_profit_target znamená, že runner není aktivní.
    runner_profit_target: float | None = None
    runner_quantity: int = 0
    # Vlastní SL runneru - při zapnutí přebírá SL obchodu a dál se přepíná
    # nezávisle na hlavní části (počáteční SL / break even)
    runner_stop_loss: float | None = None
    runner_order_id: int | None = None
    runner_fill_price: float | None = None
    # Souhrn dříve prodaných runnerů - po prodeji se runner zúčtuje sem
    # a jeho pole se uvolní, takže lze nastartovat další
    runner_sold_quantity: int = 0
    runner_realized_pnl: float = 0.0
    # Vyžádané uzavření trhem - hlavní části, resp. runneru. Podmíněný příkaz
    # se nejprve ruší a tržní prodej se zadává až po potvrzení zrušení.
    main_close_requested: bool = False
    runner_close_requested: bool = False

    # Hlídání tržního prodeje: kdy byl příkaz odeslán a kolik pokusů proběhlo.
    # TWS občas nechá tržní příkaz nevyplněný; takový se po prodlevě zadá znovu.
    exit_market_sent: datetime | None = None
    exit_market_attempts: int = 0
    runner_market_sent: datetime | None = None
    runner_market_attempts: int = 0

    # Očekávaný výsledek obchodu v USD, pokud podklad dosáhne PT resp. SL.
    # Přepočítává se průběžně podle aktuální ceny opce a podkladu, takže
    # odráží měnící se podmínky na trhu.
    expected_profit: float | None = None
    expected_loss: float | None = None

    # Runtime objekty z ib_async - nezobrazují se a neserializují
    option_contract: Any = field(default=None, repr=False, compare=False)
    underlying_contract: Any = field(default=None, repr=False, compare=False)
    entry_trade: Any = field(default=None, repr=False, compare=False)
    exit_trade: Any = field(default=None, repr=False, compare=False)
    runner_trade: Any = field(default=None, repr=False, compare=False)

    @property
    def right_label(self) -> str:
        """CALL / PUT pro zobrazení."""
        return RIGHT_LABELS.get(self.right, self.right)

    @property
    def unrealized_pnl(self) -> float | None:
        """
        Nerealizovaný zisk/ztráta pozice v USD.
        Počítá se ze středu trhu opce proti nákupní ceně,
        u uzavřené pozice z dosažené prodejní ceny.
        """
        if self.fill_price is None:
            return None

        # Pozice se skládá z hlavní části, případného běžícího runneru
        # a realizovaného výsledku dříve prodaných runnerů. Prodaná část se
        # oceňuje dosaženou cenou, běžící středem trhu.
        mid = None
        if self.option_bid is not None and self.option_ask is not None:
            mid = (self.option_bid + self.option_ask) / 2.0

        casti: list[tuple[float | None, int]] = [(self.exit_fill_price, self.main_quantity)]
        if self.runner_active and self.runner_quantity <= self.held_quantity:
            casti.append((self.runner_fill_price, self.runner_quantity))

        vysledek = self.runner_realized_pnl
        for cena, mnozstvi in casti:
            if cena is None:
                cena = mid
            if cena is None:
                return None
            vysledek += (cena - self.fill_price) * mnozstvi * 100
        return vysledek

    @property
    def risk_reward(self) -> float | None:
        """Poměr očekávaného zisku k očekávané ztrátě."""
        if not self.expected_profit or not self.expected_loss:
            return None
        if self.expected_loss == 0:
            return None
        return abs(self.expected_profit / self.expected_loss)

    @property
    def spread_ok(self) -> bool:
        """True, pokud je aktuální spread v povoleném limitu."""
        if self.option_spread_pct is None:
            return False
        return self.option_spread_pct <= self.max_spread_pct

    def touch(self, message: str = "") -> None:
        """Aktualizuje čas poslední změny a volitelně poznámku ke stavu."""
        self.updated_at = datetime.now()
        if message:
            self.message = message

    def set_state(self, state: FlowState, message: str = "") -> None:
        """Změní stav flow a zaznamená čas změny."""
        self.state = state
        self.touch(message)

    @property
    def runner_active(self) -> bool:
        """True, pokud má obchod aktivní runner s vlastním cílem."""
        return self.runner_profit_target is not None and self.runner_quantity > 0

    @property
    def runner_sl(self) -> float:
        """SL runneru - vlastní hodnota; bez ní (starší stav) společný SL obchodu."""
        if self.runner_stop_loss is not None:
            return self.runner_stop_loss
        return self.stop_loss

    @property
    def held_quantity(self) -> int:
        """Počet kontraktů, které pozice ještě drží (po prodaných runnerech)."""
        total = (self.filled_quantity or self.quantity) - self.runner_sold_quantity
        return max(total, 0)

    @property
    def main_quantity(self) -> int:
        """Počet kontraktů hlavní části pozice (bez běžícího runneru)."""
        drzeno = self.held_quantity
        if self.runner_active and self.runner_quantity < drzeno:
            return drzeno - self.runner_quantity
        return drzeno

    @property
    def open_quantity(self) -> int:
        """
        Počet kontraktů právě otevřených v trhu.
        Před nákupem nula; po nákupu skutečně nakoupené množství snížené
        o prodané runnery a o prodanou hlavní část.
        """
        if self.fill_price is None:
            return 0
        drzeno = self.held_quantity
        if self.exit_fill_price is not None:
            drzeno -= self.main_quantity
        return max(drzeno, 0)

    @property
    def open_pnl(self) -> float | None:
        """
        Zisk/ztráta pouze dosud otevřené části pozice v USD.
        Otevřené kusy se oceňují středem trhu proti nákupní ceně; realizovaný
        výsledek už prodaných částí se nezapočítává. Bez otevřených kusů None.
        """
        if self.fill_price is None:
            return None
        mnozstvi = self.open_quantity
        if mnozstvi <= 0:
            return None
        if self.option_bid is None or self.option_ask is None:
            return None
        mid = (self.option_bid + self.option_ask) / 2.0
        return (mid - self.fill_price) * mnozstvi * 100

    @property
    def runner_multiple(self) -> float | None:
        """Kolikanásobek původní vzdálenosti cíle od vstupu je cíl runneru."""
        if not self.runner_active:
            return None
        zaklad = self.original_profit_target or self.profit_target
        puvodni_vzdalenost = abs(zaklad - self.entry_price)
        if puvodni_vzdalenost <= 0:
            return None
        return abs(self.runner_profit_target - self.entry_price) / puvodni_vzdalenost

    @property
    def pt_multiple(self) -> float | None:
        """Kolikanásobek původní vzdálenosti cíle od vstupu je aktuální PT."""
        zaklad = self.original_profit_target or self.profit_target
        puvodni_vzdalenost = abs(zaklad - self.entry_price)
        if puvodni_vzdalenost <= 0:
            return None
        return abs(self.profit_target - self.entry_price) / puvodni_vzdalenost

    def option_label(self) -> str:
        """Popis opčního kontraktu pro tabulku, například 'AAPL 20260828 C 230'."""
        if not self.expiration:
            return self.symbol
        return f"{self.symbol} {self.expiration} {self.right} {self.strike:g}"
