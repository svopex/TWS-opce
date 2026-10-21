"""
Vzhled rozhraní - světlý, tmavý, nebo automaticky podle nastavení systému.

Přepínač stojí v hlavičce stránky i v přehledu výsledků (ten je přes celou
obrazovku a hlavičku zakrývá). Obě tlačítka ovládají tutéž volbu, kterou
pro okno prohlížeče drží jedna instance Vzhled, a ukazují vždy týž režim.
"""

from __future__ import annotations

from typing import Callable

from nicegui import app, ui
from nicegui.events import GenericEventArguments

# Klíč obecného úložiště, pod kterým se volba pamatuje mezi spuštěními
KLIC_ULOZISTE = "dark_mode"
# Událost, kterou static/vzhled.js hlásí systémový vzhled prohlížeče
UDALOST_SYSTEMU = "systemovy-vzhled"

# Režimy v pořadí, v jakém je tlačítko střídá. Klíč je hodnota pro
# ui.dark_mode (None = podle systému), k němu ikona a popis do bubliny
REZIMY: dict[bool | None, tuple[str, str]] = {
    False: ("light_mode", "světlý vzhled"),
    True: ("dark_mode", "tmavý vzhled"),
    None: ("brightness_auto", "vzhled podle systému"),
}


def _dalsi(rezim: bool | None) -> bool | None:
    """Režim, na který tlačítko přepne z režimu rezim."""
    poradi = list(REZIMY)
    return poradi[(poradi.index(rezim) + 1) % len(poradi)]


def _popis_bubliny(rezim: bool | None) -> str:
    """Text bubliny přepínače - aktuální režim a ten, na který tlačítko přepne."""
    return f"{REZIMY[rezim][1].capitalize()} - kliknutím přepnete na {REZIMY[_dalsi(rezim)][1]}"


class Vzhled:
    """
    Volba vzhledu jednoho okna prohlížeče.

    Stránku v automatickém režimu přebarvuje Quasar sám, grafy přehledu ale
    kreslí canvas barvami ze serveru. Systémový vzhled proto hlásí prohlížeč
    (static/vzhled.js) a je_tmavy() z něj v automatickém režimu vychází.

    :param vychozi: režim z konfigurace (ui.dark), dokud obchodník přepínačem
        nic nezvolil
    """

    def __init__(self, vychozi: bool | None) -> None:
        # Poslední volba obchodníka se pamatuje mezi spuštěními a má přednost
        # před konfigurací
        self.dark_mode = ui.dark_mode(app.storage.general.get(KLIC_ULOZISTE, vychozi))
        # Do první zprávy prohlížeče se systémový vzhled bere jako světlý
        self.system_tmavy = False
        # Kdo chce vědět o změně vzhledu - přehled výsledků přebarví grafy
        self._posluchaci: list[Callable[[], None]] = []
        ui.on(UDALOST_SYSTEMU, self._on_system)

    def je_tmavy(self) -> bool:
        """Skutečně zobrazený vzhled - v automatickém režimu ten systémový."""
        if self.dark_mode.value is None:
            return self.system_tmavy
        return bool(self.dark_mode.value)

    def pri_zmene(self, posluchac: Callable[[], None]) -> None:
        """Přihlásí posluchače volaného po přepnutí režimu i po změně v systému."""
        self._posluchaci.append(posluchac)

    def tlacitko(self) -> ui.button:
        """Založí tlačítko přepínače; ikona a bublina se drží aktuálního režimu."""
        tlacitko = (
            ui.button(on_click=self._prepni)
            .props("flat round dense")
            .bind_icon_from(self.dark_mode, "value", backward=lambda rezim: REZIMY[rezim][0])
        )
        with tlacitko:
            ui.tooltip().bind_text_from(self.dark_mode, "value", backward=_popis_bubliny)
        return tlacitko

    def _prepni(self) -> None:
        """Přepne na další režim v pořadí a volbu si zapamatuje."""
        self.dark_mode.value = _dalsi(self.dark_mode.value)
        app.storage.general[KLIC_ULOZISTE] = self.dark_mode.value
        self._ohlas_zmenu()

    def _on_system(self, event: GenericEventArguments) -> None:
        """Prohlížeč nahlásil systémový vzhled - po načtení stránky a při každé změně."""
        self.system_tmavy = bool(event.args)
        self._ohlas_zmenu()

    def _ohlas_zmenu(self) -> None:
        """Dá vědět posluchačům; ti si sami poznají, zda se pro ně něco změnilo."""
        for posluchac in self._posluchaci:
            posluchac()
