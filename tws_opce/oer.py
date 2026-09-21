"""
Hlídání Order Efficiency Ratio (OER) účtu u Interactive Brokers.

IBKR hodnotí každý obchodní den poměr

    OER = (odeslané příkazy + úpravy + zrušení) / (vyplněné příkazy + 1)

a očekává hodnotu nejvýš kolem 20. Při vyšší hodnotě posílá varování
a při opakování omezuje obchodování. Třída počítá zprávy, které aplikace
do TWS odeslala, a příkazy, které se (i jen zčásti) vyplnily, a rozhoduje,
zda se do limitu vejde ještě další nepovinná zpráva - přelimitování
čekajícího příkazu, jeho návrat do trhu po uvolnění spreadu a podobně.
Den se určuje v časové zóně burzy; s novým dnem se počítadla nulují.

Poměr IBKR podle zkušeností obchodníků vymáhá až u velkého objemu zpráv
(varování přicházela při tisících zpráv denně, při stovkách ne). Proto má
rozpočet dne volný základ: do něj zprávy projdou bez ohledu na poměr,
nad ním rozhoduje limit OER.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


class OrderEfficiency:
    """
    Denní počítadlo zpráv a vyplněných příkazů pro výpočet OER.

    limit         - nejvyšší OER, který nepovinné zprávy nesmějí překročit;
                    nula nebo záporná hodnota hlídání vypíná
    timezone      - časová zóna burzy, ve které se určuje obchodní den
    free_messages - volný základ: tolik zpráv za den projde bez ohledu na OER
    fills         - zdroj vyplnění příkazů (objekty Fill z ib_async); ib_async
                    si po připojení vyžádá exekuce celého dne, takže vyplněné
                    příkazy není potřeba počítat ani ukládat zvlášť
    now           - zdroj aktuálního času (testy si jím podvrhují den)
    """

    def __init__(
        self,
        limit: float,
        timezone: ZoneInfo,
        free_messages: int = 0,
        fills: Callable[[], Iterable[Any]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.limit = limit
        self.free_messages = free_messages
        self.timezone = timezone
        self._fills = fills or tuple
        self._now = now or (lambda: datetime.now(self.timezone))
        self._day: date = self._today()
        self._messages: int = 0

    # ------------------------------------------------------------------
    # Den a počítadla
    # ------------------------------------------------------------------

    def _today(self) -> date:
        """Dnešní obchodní den v časové zóně burzy."""
        return self._now().astimezone(self.timezone).date()

    def _roll(self) -> None:
        """S novým obchodním dnem vynuluje počítadlo zpráv - IBKR hodnotí každý den zvlášť."""
        dnes = self._today()
        if dnes != self._day:
            self._day = dnes
            self._messages = 0

    @property
    def enabled(self) -> bool:
        """True, pokud se OER hlídá (kladný limit)."""
        return self.limit > 0

    @property
    def messages(self) -> int:
        """Počet dnes odeslaných zpráv (nové příkazy, úpravy, zrušení)."""
        self._roll()
        return self._messages

    @property
    def executed(self) -> int:
        """
        Počet dnes vyplněných příkazů. Částečně vyplněný příkaz má víc
        exekucí, ale do OER se počítá jednou; exekuce z jiného dne se
        přeskakují. Čas bez časové zóny se bere jako místní čas počítače.
        """
        self._roll()
        return len(
            {
                self._execution_key(fill.execution)
                for fill in self._fills()
                if fill.time.astimezone(self.timezone).date() == self._day
            }
        )

    @property
    def ratio(self) -> float:
        """Aktuální OER dne podle vzorce IBKR."""
        return self.messages / (self.executed + 1)

    @property
    def budget(self) -> float:
        """
        Kolik zpráv smí den celkem obsahovat: větší z volného základu
        a počtu, který drží OER na limitu (limit × (vyplněné + 1)).
        """
        return max(float(self.free_messages), self.limit * (self.executed + 1))

    @property
    def over_limit(self) -> bool:
        """True, pokud dnešní zprávy přesáhly rozpočet dne (viz allows)."""
        return not self.allows(0)

    def record_message(self, count: int = 1) -> None:
        """Započte odeslanou zprávu - nový příkaz, jeho úpravu nebo zrušení."""
        self._roll()
        self._messages += count

    @staticmethod
    def _execution_key(execution: Any) -> str:
        """
        Klíč vyplněného příkazu. permId je v TWS trvalý napříč spojeními,
        orderId stačí jako náhrada; bez obou se exekuce počítá samostatně.
        """
        if execution.permId:
            return f"perm:{execution.permId}"
        if execution.orderId:
            return f"order:{execution.orderId}"
        return f"exec:{execution.execId}"

    # ------------------------------------------------------------------
    # Rozhodování
    # ------------------------------------------------------------------

    def allows(self, count: int, reserve: int = 0) -> bool:
        """
        True, pokud se vejde dalších `count` nepovinných zpráv - buď do
        volného základu dne, nebo do limitu OER (viz budget).

        reserve - zprávy, které bude aplikace možná muset poslat povinně
        (zrušení čekajících nákupních příkazů); nepovinné zprávy je nesmějí
        vytlačit z rozpočtu. Při vypnutém hlídání vrací vždy True.
        """
        if not self.enabled:
            return True
        return self.messages + count + reserve <= self.budget

    # ------------------------------------------------------------------
    # Uložení a obnova
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Počítadlo zpráv k uložení na disk - přežije tak restart aplikace."""
        self._roll()
        return {"day": self._day.isoformat(), "messages": self._messages}

    def load(self, data: dict[str, Any] | None) -> None:
        """
        Obnoví počítadlo zpráv uložené dříve téhož dne. Záznam z jiného dne
        (nebo poškozený) se zahodí - IBKR počítá každý den znovu.
        """
        if not isinstance(data, dict):
            return
        try:
            den = date.fromisoformat(str(data.get("day", "")))
            zpravy = int(data.get("messages", 0))
        except (TypeError, ValueError):
            return
        self._roll()
        if den != self._day:
            return
        # Počítadlo se slučuje s tím, co se napočítalo před obnovou (zprávy
        # z obnovy spojení se odesílají dřív, než se stav načte)
        self._messages += max(zpravy, 0)
