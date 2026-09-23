"""
Ovládací prvky sdílené formulářem zadání obchodu a dialogem načtení pozic
ze souboru: volba runneru (tlačítka s násobky a minimum množství) a bloky
přepočtu (přepínač s polem sekund). Obě rozhraní tak drží stejný vzhled
i chování a změna se dělá na jednom místě.
"""

from __future__ import annotations

from typing import Any, Callable

from nicegui import ui

from .models import runner_volby

# Nápovědy průběžného přepočtu - {rozsah} doplní "obchod, který" (formulář)
# nebo "obchody z této dávky, které" (dialog)
NAPOVEDA_INTERVALU = (
    "Zaškrtnuto: {rozsah} za otevřené burzy čeká na vstup, se v tomto "
    "odstupu přepočítává podle živých kotací - množství (a s ním PT "
    "v procentech prémie i SL) vyjde znovu ze skutečné ceny opce a čekající "
    "příkaz se upraví na místě. Zároveň se srovná runner: pozice, která na "
    "něj dorostla (viz Runner od), ho dostane, pozice pod minimem o něj "
    "přijde. Čeká-li obchod na přepočet po otevření, průběžný přepočet začne "
    "až po něm. Nakoupený obchod se nemění."
)
NAPOVEDA_ODSTUPU = (
    "Kolik sekund uplyne mezi dvěma přepočty. Výchozí hodnota je "
    "import.refresh_interval_sec z konfigurace."
)
NAPOVEDA_PRODLEVY = (
    "Kolik sekund po otevření burzy se přepočet provede. V prvních "
    "okamžicích jsou kotace opcí nejširší, proto chvíli počkat. Výchozí "
    "hodnota je import.refresh_after_open_sec z konfigurace."
)
# Společný začátek nápovědy tlačítek runneru ve formuláři zadání i v dialogu
# načtení pozic
NAPOVEDA_RUNNER_VELIKOST = (
    "Runner je část pozice (podíl kontraktů z pole Runner [%], zaokrouhleno "
    "dolů, nejméně 1 ks) s vlastním, vzdálenějším cílem na zvoleném násobku "
    "původní vzdálenosti PT od vstupu."
)
NAPOVEDA_RUNNER_PCT = (
    "Kolik procent kontraktů pozice připadne runnerovi - zaokrouhluje se "
    "dolů, nejméně na 1 ks (z 8 ks při 25 % vyjdou 2 ks). Prázdné pole "
    "platí jako výchozí hodnota trading.runner_quantity_pct z konfigurace."
)
NAPOVEDA_RUNNER_MIN = (
    "Runner dostane pozice s alespoň tímto počtem kontraktů (včetně); menší "
    "běží bez něj, dokud na runner přepočtem nedoroste. Výchozí hodnota je "
    "import.runner_min_quantity z konfigurace."
)


def blok_prepoctu(
    popisek: str,
    popisek_pole: str,
    zapnuto: bool,
    sekund: float,
    minimum: float,
    napoveda_prepinace: str,
    napoveda_pole: str,
    trida_pole: str,
) -> tuple[Any, Any]:
    """
    Dvojice přepínač + pole sekund pro přepočet čekajícího obchodu.
    Vykresluje se do právě otevřeného kontejneru; vrací (přepínač, pole).
    """
    prepinac = (
        ui.checkbox(popisek, value=zapnuto)
        .props("dense")
        .classes("prepinac")
        .tooltip(napoveda_prepinace)
    )
    pole = (
        ui.number(
            popisek_pole,
            value=sekund,
            format="%.0f",
            step=5,
            min=minimum,
        )
        .classes(trida_pole)
        .props("outlined dense")
        .tooltip(napoveda_pole)
    )
    return prepinac, pole


def tlacitka_runneru(
    on_click: Callable[[str], None], napoveda: str, vybrane: str
) -> dict[str, Any]:
    """
    Řada tlačítek volby runneru (Nepoužít runner / 1× … 3×) v právě otevřeném
    kontejneru, hned zvýrazněná podle vybrané hodnoty. Vrací tlačítka podle
    klíče volby, aby volající mohl zvýraznění později obnovit.
    """
    tlacitka: dict[str, Any] = {}
    for hodnota, popisek in runner_volby().items():
        tlacitko = (
            ui.button(popisek, on_click=lambda _=None, h=hodnota: on_click(h))
            .props("dense no-caps size=sm")
            .classes("tlacitko-runner")
        )
        tlacitko.tooltip(napoveda)
        tlacitka[hodnota] = tlacitko
    zvyrazni_tlacitka(tlacitka, vybrane)
    return tlacitka


def zvyrazni_tlacitka(tlacitka: dict[str, Any], vybrane: str) -> None:
    """
    Vybrané tlačítko je plné a oranžové, ostatní zůstávají jen obrysové -
    stejné rozlišení jako u tlačítek runneru v řádku přehledu.
    """
    for hodnota, tlacitko in tlacitka.items():
        if hodnota == vybrane:
            tlacitko.props(add="color=orange-8", remove="outline")
        else:
            tlacitko.props(add="outline color=grey-7")


def pole_runner_pct(hodnota: float, trida: str) -> Any:
    """Pole „Runner [%]" - jak velká část pozice připadne runnerovi."""
    return (
        ui.number("Runner [%]", value=hodnota, format="%.0f", step=5, min=1, max=99)
        .classes(trida)
        .props("outlined dense")
        .tooltip(NAPOVEDA_RUNNER_PCT)
    )


def pole_runner_min(hodnota: int, trida: str) -> Any:
    """Pole „Runner od [ks]" - nejmenší množství, od kterého runner náleží."""
    return (
        ui.number("Runner od [ks]", value=hodnota, format="%.0f", step=1, min=1)
        .classes(trida)
        .props("outlined dense")
        .tooltip(NAPOVEDA_RUNNER_MIN)
    )
