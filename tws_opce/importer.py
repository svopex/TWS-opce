"""
Načítání vstupních pozic ze souboru se zadáním obchodního dne.

Soubor je YAML slovník, kde každý klíč popisuje jeden obchod. Aplikace
čerpá pouze z položek, jejichž klíč končí plusem (například „AMZN Long+"),
a to jen ze tří údajů: tickeru, vstupní ceny podkladu a cílové ceny
podkladu. Ostatní pole souboru patří jinému nástroji a ignorují se.

Modul je záměrně bez závislosti na TWS i na uživatelském rozhraní, takže
jej lze testovat samostatně - stejně jako výpočty v calc.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import yaml

# Klíče položek určených k načtení končí plusem. Soubor obsahuje i starší
# variantu téhož obchodu bez něj (jiný trade_type) a ta se přeskakuje.
PLUS_SUFFIX = "+"

# Údaje, které se z položky přebírají; zbytek souboru se ignoruje
FIELD_SYMBOL = "symbol"
FIELD_ENTRY = "entry_price"
FIELD_TARGET = "target_price"


@dataclass
class ImportedPosition:
    """
    Jedna pozice načtená ze souboru.

    key           - klíč položky v souboru, slouží k identifikaci v hláškách
    symbol        - ticker podkladu
    entry_price   - cena podkladu, při jejímž dosažení se nakupuje
    target_price  - cílová cena podkladu ze souboru, ze které se odvozuje PT
    """

    key: str
    symbol: str
    entry_price: float
    target_price: float

    @property
    def right(self) -> str:
        """
        Zamýšlený směr obchodu podle polohy cíle vůči vstupu: cíl nad
        vstupem znamená průraz nahoru (CALL), pod vstupem průraz dolů (PUT).
        Skutečný směr určí až aplikace z aktuální ceny podkladu; tenhle
        slouží k odhalení pozic, jejichž vstup už trh překonal.
        """
        return "C" if self.target_price > self.entry_price else "P"

    @property
    def right_label(self) -> str:
        """LONG / SHORT pro zobrazení v tabulce dialogu."""
        return "LONG" if self.right == "C" else "SHORT"


@dataclass
class ImportResult:
    """
    Výsledek načtení souboru.

    positions - použitelné pozice v pořadí ze souboru
    warnings  - popisy položek, které se načíst nepodařilo (a proč)
    """

    positions: list[ImportedPosition] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _to_price(value: Any, popis: str) -> float:
    """
    Převede hodnotu z YAML na cenu. Nečíselný, nekonečný nebo nekladný
    údaj odmítne - popis se objeví v hlášce o přeskočené položce.
    """
    try:
        cislo = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{popis} není číslo ({value!r}).") from None
    if not math.isfinite(cislo) or cislo <= 0:
        raise ValueError(f"{popis} musí být kladné číslo ({value!r}).")
    return cislo


def _position_from_item(key: str, data: Any) -> ImportedPosition:
    """
    Sestaví pozici z jedné položky souboru.
    Chybějící nebo nesmyslný povinný údaj vyvolá ValueError s popisem,
    který se pak obchodníkovi ukáže mezi přeskočenými položkami.
    """
    if not isinstance(data, dict):
        raise ValueError("položka není slovník hodnot.")

    symbol = str(data.get(FIELD_SYMBOL) or "").upper().strip()
    if not symbol:
        raise ValueError(f"chybí ticker ({FIELD_SYMBOL}).")

    entry = _to_price(data.get(FIELD_ENTRY), f"vstupní cena ({FIELD_ENTRY})")
    target = _to_price(data.get(FIELD_TARGET), f"cílová cena ({FIELD_TARGET})")

    # Shodná vstupní a cílová cena neurčuje ani směr obchodu, ani dráhu k cíli,
    # ze které se počítá PT - taková položka je k ničemu
    if abs(target - entry) < 0.005:
        raise ValueError(
            f"cílová cena {target:g} se rovná vstupní ceně - směr obchodu nelze určit."
        )

    return ImportedPosition(key=key, symbol=symbol, entry_price=entry, target_price=target)


def parse_positions(text: str) -> ImportResult:
    """
    Načte pozice z obsahu souboru se zadáním obchodního dne.

    Bere jen položky s klíčem končícím plusem. Vadná položka celé načtení
    nezastaví - přeskočí se a důvod se vrátí mezi varováními, aby zbytek
    souboru zůstal použitelný. Nečitelný soubor nebo soubor zcela bez
    „plus" položek vyvolá ValueError.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Soubor se nepodařilo přečíst jako YAML: {exc}") from exc

    if data is None:
        raise ValueError("Soubor je prázdný.")
    if not isinstance(data, dict):
        raise ValueError("Soubor musí být slovník položek ve tvaru „název: hodnoty“.")

    vysledek = ImportResult()
    nalezeno = 0
    for key, item in data.items():
        nazev = str(key).strip()
        if not nazev.endswith(PLUS_SUFFIX):
            continue
        nalezeno += 1
        try:
            vysledek.positions.append(_position_from_item(nazev, item))
        except ValueError as exc:
            vysledek.warnings.append(f"{nazev}: {exc}")

    if nalezeno == 0:
        raise ValueError(
            "Soubor neobsahuje žádnou položku s klíčem končícím plusem "
            "(například „AMZN Long+“)."
        )
    return vysledek


def profit_target_from_pct(entry_price: float, target_price: float, pct: float) -> float:
    """
    PT na podkladu jako podíl dráhy ze vstupní ceny k cílové ceně ze souboru.

    100 % je přesně cílová cena, 50 % půlka cesty k ní. Směr řeší samo
    znaménko rozdílu, takže vzorec platí pro long i short. Zaokrouhluje se
    na centy, stejně jako ostatní úrovně ve formuláři.
    """
    if pct <= 0:
        raise ValueError("Procento cíle musí být kladné číslo.")
    return round(entry_price + (target_price - entry_price) * pct / 100.0, 2)


def profit_target_from_premium_pct(pct: float, option_price: float) -> float:
    """
    PT (resp. SL) na opci v USD na kontrakt z procenta zaplacené prémie.

    Prémie jednoho kontraktu je cena opce krát multiplikátor (100), takže
    jedno procento prémie je právě cena opce v USD - 30 % z opce za 3,00
    (300 USD) je 90 USD na kontrakt. Zadaná hodnota tak znamená u levné
    i drahé opce stejný podíl vložených peněz, což fixní částka v USD
    napříč košíkem nedělá.

    Procento nad 100 nemá u SL smysl (stop by stál pod nulovou cenou opce),
    tady se ale nekontroluje - na překročení zaplacené prémie upozorňuje
    engine při přípravě zadání i po nákupu.
    """
    if pct <= 0:
        raise ValueError("Procento prémie musí být kladné číslo.")
    if option_price <= 0:
        raise ValueError("Cena opce pro výpočet z prémie musí být kladná.")
    return round(pct * option_price, 2)
