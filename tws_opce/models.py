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

    # Runtime objekty z ib_async - nezobrazují se a neserializují
    option_contract: Any = field(default=None, repr=False, compare=False)
    underlying_contract: Any = field(default=None, repr=False, compare=False)
    entry_trade: Any = field(default=None, repr=False, compare=False)
    exit_trade: Any = field(default=None, repr=False, compare=False)

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
        # Počítá se s nakoupeným množstvím, aby částečné plnění výsledek nenadhodnotilo
        quantity = self.filled_quantity or self.quantity
        if self.exit_fill_price is not None:
            # Uzavřená pozice - realizovaný výsledek
            return (self.exit_fill_price - self.fill_price) * quantity * 100
        if self.option_bid is None or self.option_ask is None:
            return None
        mid = (self.option_bid + self.option_ask) / 2.0
        return (mid - self.fill_price) * quantity * 100

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

    def option_label(self) -> str:
        """Popis opčního kontraktu pro tabulku, například 'AAPL 20260828 C 230'."""
        if not self.expiration:
            return self.symbol
        return f"{self.symbol} {self.expiration} {self.right} {self.strike:g}"
