"""
Sdílený stav ovládacích prvků pro dialogy, které mají přežít zavření okna
prohlížeče.

Prvek NiceGUI patří vždy jednomu klientovi (oknu prohlížeče) a se zavřením
okna zaniká i s hodnotami, které v něm byly. Dialog načtení pozic ze souboru
ale drží naplánované zadání, které musí doběhnout i bez otevřeného okna,
a po znovuotevření stránky se má ukázat přesně tak, jak byl opuštěn.

Logika dialogu proto pracuje se SdilenyPrvek - serverovým stavem prvku se
stejným rozhraním, jaké používá u prvků NiceGUI (value, set_value, set_text,
classes, ...). Každé okno prohlížeče si k němu vykreslí vlastní skutečný prvek
a připojí ho metodou pripoj: prvek převezme aktuální stav a každá další změna
se rozešle do všech připojených oken. Změna provedená obchodníkem v jednom
okně se stejnou cestou propíše do stavu i do ostatních oken.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable


def zive(prvky: list[Any]) -> list[Any]:
    """Prvky (či pohledy) oken prohlížeče, které ještě nezanikly."""
    return [prvek for prvek in prvky if not prvek.is_deleted]


def _zmen_mnozinu(
    mnozina: dict[str, Any], add: str | None, remove: str | None, klic: Callable[[str], str]
) -> bool:
    """
    Zapíše do uloženého stavu tříd či vlastností přidané (True) a odebrané
    (False) položky. Vrací True, pokud se tím stav změnil.

    klic - jméno položky, pod kterým se ukládá (u vlastností bez hodnoty)
    """
    puvodni = dict(mnozina)
    for token in (remove or "").split():
        mnozina[klic(token)] = (False, token)
    for token in (add or "").split():
        mnozina[klic(token)] = (True, token)
    return mnozina != puvodni


def _zapis_mnoziny(mnozina: dict[str, Any]) -> tuple[str | None, str | None]:
    """Uložený stav tříd či vlastností jako dvojice (přidat, odebrat)."""
    pridat = " ".join(t for pridano, t in mnozina.values() if pridano)
    odebrat = " ".join(t for pridano, t in mnozina.values() if not pridano)
    return pridat or None, odebrat or None


class SdilenyPrvek:
    """
    Serverový stav jednoho ovládacího prvku sdílený všemi okny prohlížeče.

    value - počáteční hodnota (u polí, přepínačů, zaškrtávátek a výběrů)
    text - počáteční text (u popisků, tlačítek a bublin)

    Drží hodnotu, text, viditelnost, dostupnost a přidané či odebrané třídy
    a vlastnosti. Připojené skutečné prvky se mu přizpůsobí hned při
    připojení a pak při každé změně; prvky zavřených oken se průběžně
    zapomínají.
    """

    def __init__(self, value: Any = None, text: str = "") -> None:
        self.value = value
        self.text = text
        self.visible = True
        self.enabled = True
        # Třídy a vlastnosti se ukládají jako výsledek posloupnosti volání:
        # položka -> (přidána?, zápis). Celou sadu tříd záměrně nikdy
        # nenahrazuje - zahodila by třídy, které si prvek drží sám (NiceGUI
        # třeba skrývá prvek třídou "hidden")
        self._tridy: dict[str, tuple[bool, str]] = {}
        self._vlastnosti: dict[str, tuple[bool, str]] = {}
        # Připojené skutečné prvky oken prohlížeče
        self._odrazy: list[Any] = []
        # Obsluhy změny hodnoty registrované logikou dialogu
        self._obsluhy: list[Callable[[Any], Any]] = []

    # ------------------------------------------------------------------
    # Připojení skutečných prvků
    # ------------------------------------------------------------------

    def pripoj(self, prvek: Any) -> Any:
        """
        Připojí skutečný prvek okna prohlížeče: převezme aktuální stav
        a od této chvíle se mu rozesílá každá změna. U prvků s hodnotou se
        zároveň změna provedená obchodníkem propisuje zpět do stavu.
        Vrací prvek, aby šlo připojení zapsat přímo při jeho stavbě.
        """
        # Prvky zavřených oken se zapomínají i tady - u prvků, které se
        # skoro nemění, by se jinak hromadily s každým načtením stránky
        self._odrazy = zive(self._odrazy)
        self._odrazy.append(prvek)
        if hasattr(prvek, "set_value"):
            prvek.set_value(self.value)
            # Zpětné hlášení změny rozeslané do okna zastaví set_value
            # samo - hodnota už je stejná
            prvek.on_value_change(lambda e: self.set_value(e.value))
        elif self.text and hasattr(prvek, "set_text"):
            prvek.set_text(self.text)
        prvek.set_visibility(self.visible)
        if hasattr(prvek, "set_enabled"):
            prvek.set_enabled(self.enabled)
        pridat, odebrat = _zapis_mnoziny(self._tridy)
        if pridat or odebrat:
            prvek.classes(add=pridat, remove=odebrat)
        pridat, odebrat = _zapis_mnoziny(self._vlastnosti)
        if pridat or odebrat:
            prvek.props(add=pridat, remove=odebrat)
        return prvek

    def _zive(self) -> list[Any]:
        """Připojené prvky oken, která ještě existují; ostatní zapomene."""
        self._odrazy = zive(self._odrazy)
        return self._odrazy

    # ------------------------------------------------------------------
    # Rozhraní prvku NiceGUI, které používá logika dialogu
    # ------------------------------------------------------------------

    def on_value_change(self, obsluha: Callable[[Any], Any]) -> "SdilenyPrvek":
        """Zaregistruje obsluhu změny hodnoty (ručně v okně i programově)."""
        self._obsluhy.append(obsluha)
        return self

    def set_value(self, hodnota: Any) -> None:
        """
        Nastaví hodnotu, rozešle ji do všech oken a zavolá obsluhy změny.
        Stejně jako u prvků NiceGUI se obsluhy volají jen při skutečné změně.
        """
        if hodnota == self.value:
            return
        self.value = hodnota
        for prvek in self._zive():
            prvek.set_value(hodnota)
        udalost = SimpleNamespace(value=hodnota, sender=self)
        for obsluha in list(self._obsluhy):
            obsluha(udalost)

    def set_text(self, text: str) -> None:
        """Nastaví text popisku, tlačítka či bubliny ve všech oknech."""
        # Obnova dialogu zapisuje stavy řádků každou sekundu - beze změny
        # se do oken nic neposílá
        if text == self.text:
            return
        self.text = text
        for prvek in self._zive():
            prvek.set_text(text)

    def set_visibility(self, viditelny: bool) -> None:
        """Ukáže, nebo skryje prvek ve všech oknech."""
        if viditelny == self.visible:
            return
        self.visible = viditelny
        for prvek in self._zive():
            prvek.set_visibility(viditelny)

    def set_enabled(self, dostupny: bool) -> None:
        """Zpřístupní, nebo zakáže prvek ve všech oknech."""
        if dostupny == self.enabled:
            return
        self.enabled = dostupny
        for prvek in self._zive():
            if hasattr(prvek, "set_enabled"):
                prvek.set_enabled(dostupny)

    def classes(self, add: str | None = None, *, remove: str | None = None) -> "SdilenyPrvek":
        """Přidá či odebere třídy CSS ve všech oknech a zapamatuje si je."""
        if _zmen_mnozinu(self._tridy, add, remove, lambda token: token):
            for prvek in self._zive():
                prvek.classes(add=add, remove=remove)
        return self

    def props(self, add: str | None = None, *, remove: str | None = None) -> "SdilenyPrvek":
        """Přidá či odebere vlastnosti Quasaru ve všech oknech a zapamatuje si je."""
        # Vlastnost se ukládá podle jména bez hodnoty - z "color=orange-8"
        # je "color", takže pozdější jiná barva tu předchozí nahradí
        if _zmen_mnozinu(self._vlastnosti, add, remove, lambda token: token.split("=", 1)[0]):
            for prvek in self._zive():
                prvek.props(add=add, remove=remove)
        return self


class SdileneOkno:
    """
    Okna dialogu ve všech oknech prohlížeče. Otevírá je každé okno samo,
    zavřít je ale logika dialogu umí naráz - třeba po úspěšně zadané dávce.
    """

    def __init__(self) -> None:
        self._okna: list[Any] = []

    def pripoj(self, okno: Any) -> Any:
        """Připojí ui.dialog jednoho okna prohlížeče."""
        self._okna = zive(self._okna)
        self._okna.append(okno)
        return okno

    @property
    def value(self) -> bool:
        """True, je-li dialog otevřený alespoň v jednom okně."""
        self._okna = zive(self._okna)
        return any(okno.value for okno in self._okna)

    def close(self) -> None:
        """Zavře dialog ve všech oknech."""
        self._okna = zive(self._okna)
        for okno in self._okna:
            okno.close()
