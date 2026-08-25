"""
Popup s přehledem výsledků obchodního dne.

Ukazuje na jedné obrazovce, co se dnes obchodovalo, co ještě běží a s jakým
výsledkem: souhrnné dlaždice, seznam běžících a ukončených obchodů a dva grafy.
Rozvržení je navržené tak, aby se vešlo bez posuvníku - posouvají se nejvýš
samotné seznamy uvnitř svých panelů.

Přehled žije souběžně s monitorovací tabulkou: otevřený popup se obnovuje
ze stejné periodické smyčky, takže běžící pozice v něm tikají živě.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from typing import Any

from nicegui import ui

from . import report
from .config import AppConfig
from .engine import FlowEngine
from .models import Flow, FlowState

# Popisky přepínače rozsahu přehledu
ROZSAHY = {report.ROZSAH_DNES: "Dnes", report.ROZSAH_VSE: "Vše"}

# Všechny stavové třídy pohromadě - při přepisu stavu je nutné odebrat
# tu předchozí, ať už byla kterákoliv
VSECHNY_STAVY = " ".join(sorted({stav.css_class for stav in FlowState}))

# Definice souhrnných dlaždic nad přehledem: klíč a nadpis. Hodnoty do nich
# doplňuje _hodnoty_dlazdic podle spočítaného souhrnu
DLAZDICE = (
    ("celkem", "Výsledek dne"),
    ("realizovano", "Realizováno"),
    ("otevreno", "Otevřené pozice"),
    ("uspesnost", "Úspěšnost"),
    ("factor", "Profit factor"),
    ("obchody", "Obchody"),
)


def penize(hodnota: float | None, znamenko: bool = True) -> str:
    """
    Částka v USD pro přehled - tisíce oddělené mezerou.
    Se znaménkem se zobrazují výsledky (aby byl zisk na první pohled patrný),
    bez něj ceny. Chybějící hodnota je pomlčka.
    """
    if hodnota is None:
        return "-"
    text = f"{hodnota:+,.2f}" if znamenko else f"{hodnota:,.2f}"
    return text.replace(",", " ")


def trida_vysledku(hodnota: float | None) -> str:
    """CSS třída pro obarvení částky - zisk zeleně, ztráta červeně."""
    if hodnota is None or abs(hodnota) < 0.005:
        return ""
    return "zisk" if hodnota > 0 else "ztrata"


def sklonuj(pocet: int, jednotne: str, mnozne: str, genitiv: str) -> str:
    """
    Počet se správným tvarem: 1 obchod, 2-4 obchody, 5 a víc obchodů.
    Tvary se předávají celé včetně přívlastku ('otevřená pozice').
    """
    if pocet == 1:
        return f"{pocet} {jednotne}"
    if 2 <= pocet <= 4:
        return f"{pocet} {mnozne}"
    return f"{pocet} {genitiv}"


def doba_drzeni(sekundy: float | None) -> str:
    """Délka držení pozice ve zkráceném tvaru - '48 s', '12 min', '1:05 h'."""
    if sekundy is None:
        return "-"
    celkem = int(sekundy)
    if celkem < 60:
        return f"{celkem} s"
    minuty, _ = divmod(celkem, 60)
    if minuty < 60:
        return f"{minuty} min"
    hodiny, zbytek_minut = divmod(minuty, 60)
    return f"{hodiny}:{zbytek_minut:02d} h"


def mez_osy(hodnoty: list[float]) -> tuple[float, float]:
    """
    Spodní a horní mez svislé osy s rezervou na popisky nad a pod sloupci.

    Meze se zaokrouhlují na kulatý krok odvozený z řádu největší hodnoty,
    aby na ose nestála čísla jako 354. Osa vždy obsahuje nulu, jinak by
    sloupce neměly společný základ.
    """
    dolni = min(min(hodnoty), 0.0)
    horni = max(max(hodnoty), 0.0)
    rozsah = max(abs(dolni), abs(horni))
    if rozsah <= 0:
        return -1.0, 1.0

    # Krok je polovina řádu největší hodnoty - u stovek tedy 50
    krok = 10 ** math.floor(math.log10(rozsah)) / 2
    rezerva = rozsah * 0.18
    # Rezerva se přidává jen na té straně, kde nějaké sloupce skutečně jsou -
    # jinak by graf se samými zisky měl pod nulou prázdné pásmo
    return (
        math.floor((dolni - rezerva) / krok) * krok if dolni < 0 else 0.0,
        math.ceil((horni + rezerva) / krok) * krok if horni > 0 else 0.0,
    )


def kontrakt_text(flow: Flow) -> str:
    """Popis opčního kontraktu bez tickeru - ten stojí ve vlastním sloupci."""
    if not flow.expiration:
        return "-"
    return f"{flow.right_label} {flow.expiration} @ {flow.strike:g}"


class ReportDialog:
    """
    Popup s přehledem výsledků. Vzniká jednou při stavbě stránky (aby jeho
    prvky patřily danému klientovi) a otevírá se tlačítkem nad monitorovací
    tabulkou; obsah se plní až při otevření a pak průběžně obnovuje.
    """

    def __init__(
        self, cfg: AppConfig, engine: FlowEngine, je_tmavy: Callable[[], bool]
    ) -> None:
        self.cfg = cfg
        self.engine = engine
        # Vzhled grafů se řídí přepínačem světlý/tmavý režim v hlavičce
        self.je_tmavy = je_tmavy
        self.rozsah = report.ROZSAH_DNES

        # Podpisy naposledy vykreslených dat. Seznamy a grafy se překreslují
        # jen při skutečné změně, aby přehled každou vteřinu neblikal.
        # None znamená „ještě nevykresleno" - prázdná n-tice je platný podpis
        # prázdného seznamu a nesmí se s tím zaměnit
        self._podpis_bezici: tuple | None = None
        self._podpis_uzavrene: tuple | None = None
        self._podpis_krivka: tuple | None = None
        self._podpis_tickery: tuple | None = None
        # Prvky řádků běžících obchodů podle id - do nich se zapisují živé
        # hodnoty (kotace, P/L, stav), aniž by se řádek stavěl znovu
        self._radky_bezici: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Sestavení dialogu
    # ------------------------------------------------------------------

    def build(self) -> None:
        """Vykreslí prázdnou kostru dialogu; obsah doplní až otevření."""
        with ui.dialog().props("maximized").classes("dialog-report-obal") as self.dialog:
            with ui.card().classes("dialog-report"):
                self._build_hlavicka()

                # Pás souhrnných dlaždic
                self.dlazdice: dict[str, dict[str, Any]] = {}
                with ui.element("div").classes("report-dlazdice"):
                    for klic, nadpis in DLAZDICE:
                        self.dlazdice[klic] = self._build_dlazdice(klic, nadpis)

                # Hlavní plocha: vlevo seznamy obchodů, vpravo grafy
                with ui.element("div").classes("report-mrizka"):
                    with ui.element("div").classes("report-sloupec"):
                        self.telo_bezici, self.podnadpis_bezici = self._build_panel(
                            "Běží teď", "report-panel-bezici"
                        )
                        self.telo_uzavrene, self.podnadpis_uzavrene = self._build_panel(
                            "Uzavřené obchody", "report-panel-uzavrene"
                        )
                    with ui.element("div").classes("report-sloupec"):
                        telo_krivka, self.podnadpis_krivka = self._build_panel(
                            "Průběh dne", "report-panel-graf"
                        )
                        with telo_krivka:
                            self.graf_krivka = ui.echart(
                                self._prazdny_graf()
                            ).classes("report-graf")
                            self.prazdno_krivka = ui.label(
                                "Křivka se objeví s prvním uzavřeným obchodem."
                            ).classes("report-prazdno")
                        telo_tickery, self.podnadpis_tickery = self._build_panel(
                            "Výsledek podle tickeru", "report-panel-graf"
                        )
                        with telo_tickery:
                            self.graf_tickery = ui.echart(
                                self._prazdny_graf()
                            ).classes("report-graf")
                            self.prazdno_tickery = ui.label(
                                "Sloupce se objeví s prvním nakoupeným obchodem."
                            ).classes("report-prazdno")

    def _build_hlavicka(self) -> None:
        """Horní lišta dialogu - nadpis, přepínač rozsahu a zavření."""
        with ui.element("div").classes("report-hlavicka"):
            ui.label("Výsledky").classes("report-nadpis")
            self.datum_label = ui.label("").classes("report-datum")
            ui.element("div").classes("report-vypln")

            # Rozsah přehledu: dnešní obchodní den, nebo vše, co drží monitoring
            self.prepinac_rozsahu = (
                ui.toggle(ROZSAHY, value=self.rozsah, on_change=self._on_rozsah)
                .props("dense no-caps unelevated toggle-color=primary")
                .classes("report-prepinac")
                .tooltip(
                    "Dnes: obchody založené dnešního dne a všechny, které stále "
                    "běží. Vše: celý obsah monitoringu bez ohledu na datum."
                )
            )
            ui.button(icon="close", on_click=self.dialog.close).props(
                "flat round dense"
            ).classes("report-zavrit").tooltip("Zavřít přehled")

    def _build_dlazdice(self, klic: str, nadpis: str) -> dict[str, Any]:
        """Jedna souhrnná dlaždice - nadpis, velká hodnota a popisek pod ní."""
        with ui.element("div").classes(f"report-dlazdice-polozka dlazdice-{klic}"):
            ui.label(nadpis).classes("dlazdice-nadpis")
            hodnota = ui.label("-").classes("dlazdice-hodnota")
            popis = ui.label("").classes("dlazdice-popis")
        return {"hodnota": hodnota, "popis": popis}

    def _build_panel(self, nadpis: str, trida: str) -> tuple[Any, Any]:
        """
        Panel přehledu - hlavička s nadpisem a doplňujícím popiskem a prázdné
        tělo. Vrací tělo (do něj se vykresluje obsah) a popisek v hlavičce.
        """
        with ui.element("div").classes(f"report-panel {trida}"):
            with ui.element("div").classes("report-panel-hlavicka"):
                ui.label(nadpis).classes("report-panel-nadpis")
                podnadpis = ui.label("").classes("report-panel-podnadpis")
            telo = ui.element("div").classes("report-panel-telo")
        return telo, podnadpis

    # ------------------------------------------------------------------
    # Otevření a obnova
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Otevře přehled a rovnou jej naplní aktuálními daty."""
        self.dialog.open()
        self.refresh()

    def _on_rozsah(self, event: Any) -> None:
        """Přepnutí rozsahu (dnes / vše) překreslí celý přehled."""
        self.rozsah = event.value or report.ROZSAH_DNES
        # Podpisy se zahodí, aby se seznamy i grafy postavily znovu
        self._podpis_bezici = None
        self._podpis_uzavrene = None
        self._podpis_krivka = None
        self._podpis_tickery = None
        self.refresh()

    def refresh(self) -> None:
        """
        Obnoví obsah přehledu z aktuálního stavu enginu.
        Volá se z periodické smyčky rozhraní; zavřený dialog se přeskakuje.
        """
        if not self.dialog.value:
            return

        podklad = report.sestav(list(self.engine.flows.values()), self.rozsah)
        self.datum_label.set_text(f"{datetime.now():%d.%m.%Y %H:%M:%S}")
        self._vykresli_dlazdice(podklad)
        self._vykresli_bezici(podklad)
        self._vykresli_uzavrene(podklad)
        self._vykresli_krivku(podklad)
        self._vykresli_tickery(podklad)

    # ------------------------------------------------------------------
    # Souhrnné dlaždice
    # ------------------------------------------------------------------

    def _hodnoty_dlazdic(self, podklad: report.DenniReport) -> dict[str, tuple[str, str, str]]:
        """
        Text, popisek a barevná třída pro každou dlaždici.
        Vrací slovník klíč -> (hodnota, popis, třída).
        """
        s = podklad.souhrn

        # Úspěšnost a profit factor dávají smysl až u uzavřených obchodů
        if s.uspesnost is None:
            uspesnost = ("-", "zatím nic uzavřeného", "")
        else:
            uspesnost = (
                f"{s.uspesnost:.0f} %",
                f"{s.ziskovych}× zisk / {s.ztratovych}× ztráta",
                "zisk" if s.uspesnost >= 50 else "ztrata",
            )

        if s.uzavrenych_s_vysledkem == 0:
            factor = ("-", "zatím nic uzavřeného", "")
        elif s.profit_factor is None:
            # Bez jediné ztráty se poměr nedá spočítat - řekne se to natvrdo
            factor = ("∞", "žádná ztráta", "zisk")
        else:
            factor = (
                f"{s.profit_factor:.2f}".replace(".", ","),
                f"Ø {penize(s.prumerny_zisk)} / Ø {penize(-(s.prumerna_ztrata or 0))}",
                "zisk" if s.profit_factor >= 1 else "ztrata",
            )

        bez_nakupu = f" · {s.bez_obchodu} bez nákupu" if s.bez_obchodu else ""

        # Část realizovaného výsledku může pocházet z obchodů, které dosud běží
        # (prodaný runner). Bez zmínky by souhrn nesouhlasil se seznamem uzavřených
        z_bezicich_castka = s.realizovano - sum(
            flow.realized_pnl or 0.0 for flow in podklad.ukoncene
        )
        z_bezicich = (
            f" · {penize(z_bezicich_castka)} z běžících"
            if abs(z_bezicich_castka) >= 0.005
            else ""
        )
        return {
            "celkem": (
                penize(s.celkem),
                f"realizováno {penize(s.realizovano)} · v pozicích {penize(s.otevreno)}",
                trida_vysledku(s.celkem),
            ),
            "realizovano": (
                penize(s.realizovano),
                sklonuj(
                    s.uzavrenych_s_vysledkem,
                    "uzavřený obchod",
                    "uzavřené obchody",
                    "uzavřených obchodů",
                )
                + z_bezicich,
                trida_vysledku(s.realizovano),
            ),
            "otevreno": (
                penize(s.otevreno) if s.otevrenych_pozic else "-",
                sklonuj(s.otevrenych_pozic, "pozice", "pozice", "pozic")
                + f" · {s.otevrenych_kusu} ks",
                trida_vysledku(s.otevreno) if s.otevrenych_pozic else "",
            ),
            "uspesnost": uspesnost,
            "factor": factor,
            "obchody": (
                str(s.bezicich + s.ukoncenych),
                f"{s.bezicich} běží · {s.ukoncenych} ukončeno{bez_nakupu}",
                "",
            ),
        }

    def _vykresli_dlazdice(self, podklad: report.DenniReport) -> None:
        """Přepíše hodnoty souhrnných dlaždic."""
        for klic, (hodnota, popis, trida) in self._hodnoty_dlazdic(podklad).items():
            prvky = self.dlazdice[klic]
            prvky["hodnota"].set_text(hodnota)
            prvky["hodnota"].classes(remove="zisk ztrata", add=trida)
            prvky["popis"].set_text(popis)

    # ------------------------------------------------------------------
    # Seznam běžících obchodů
    # ------------------------------------------------------------------

    def _hlavicka_seznamu(self, trida: str, popisky: tuple[str, ...]) -> None:
        """Řádek s názvy sloupců seznamu."""
        with ui.element("div").classes(f"report-radek report-hlavicka-seznamu {trida}"):
            for popisek in popisky:
                ui.label(popisek).classes("bunka bunka-popisek")

    def _radek_bezici(self, flow: Flow) -> dict[str, Any]:
        """
        Postaví řádek běžícího obchodu a vrátí prvky, do kterých se při
        obnově zapisují živé hodnoty. Ticker a směr se během obchodu nemění,
        proto se vyplní rovnou.
        """
        prvky: dict[str, Any] = {}
        with ui.element("div").classes("report-radek report-radek-bezici"):
            with ui.element("div").classes("bunka bunka-ticker"):
                ui.label(flow.symbol).classes("report-ticker")
                smer = "LONG" if flow.right == "C" else "SHORT"
                ui.label(smer).classes(
                    f"odznak-smer {'smer-long' if flow.right == 'C' else 'smer-short'}"
                )
            prvky["kontrakt"] = ui.label("").classes("bunka bunka-kontrakt")
            prvky["ks"] = ui.label("").classes("bunka bunka-cislo")
            prvky["nakup"] = ui.label("").classes("bunka bunka-cislo")
            prvky["ted"] = ui.label("").classes("bunka bunka-cislo")

            # Ukazatel, kde pozice stojí mezi SL (vlevo) a PT (vpravo)
            with ui.element("div").classes("bunka bunka-postup"):
                prvky["postup"] = (
                    ui.linear_progress(value=0.0, show_value=False, size="9px")
                    .props("rounded track-color=grey-4")
                    .classes("report-postup")
                    .tooltip("Kde stojí pozice mezi SL (vlevo) a PT (vpravo)")
                )
            prvky["pnl"] = ui.label("").classes("bunka bunka-cislo bunka-pnl")
            with ui.element("div").classes("bunka bunka-stav"):
                prvky["stav"] = ui.label("").classes("odznak")
        return prvky

    def _napln_bezici(self, prvky: dict[str, Any], flow: Flow) -> None:
        """Zapíše do řádku běžícího obchodu aktuální hodnoty."""
        prvky["kontrakt"].set_text(kontrakt_text(flow))
        prvky["ks"].set_text(f"{flow.open_quantity}/{flow.quantity}")
        prvky["nakup"].set_text(penize(flow.fill_price, znamenko=False))
        prvky["ted"].set_text(penize(flow.option_bid, znamenko=False))

        pnl = flow.open_pnl
        prvky["pnl"].set_text(penize(pnl))
        prvky["pnl"].classes(remove="zisk ztrata", add=trida_vysledku(pnl))

        # Ukazatel postupu se skrývá, dokud pozice neběží nebo chybí odhad
        postup = report.postup_k_cili(flow)
        prvky["postup"].set_visibility(postup is not None)
        if postup is not None:
            prvky["postup"].set_value(postup)
            # Nad polovinou cesty k PT je pozice v zisku, pod ní ve ztrátě
            prvky["postup"].props(f"color={'green-6' if postup >= 0.5 else 'red-6'}")

        prvky["stav"].set_text(flow.state.label)
        prvky["stav"].classes(remove=VSECHNY_STAVY, add=flow.state.css_class)

    def _vykresli_bezici(self, podklad: report.DenniReport) -> None:
        """
        Vykreslí seznam běžících obchodů. Řádky se staví znovu jen tehdy,
        když se změnilo složení seznamu; jinak se do nich jen zapisují hodnoty.
        """
        podpis = tuple(flow.id for flow in podklad.bezici)
        if podpis != self._podpis_bezici:
            self._podpis_bezici = podpis
            self._radky_bezici = {}
            self.telo_bezici.clear()
            with self.telo_bezici:
                if not podklad.bezici:
                    ui.label("Právě neběží žádný obchod.").classes("report-prazdno")
                else:
                    self._hlavicka_seznamu(
                        "report-radek-bezici",
                        ("Ticker", "Kontrakt", "Ks", "Nákup", "Teď", "SL → PT", "P/L", "Stav"),
                    )
                    for flow in podklad.bezici:
                        self._radky_bezici[flow.id] = self._radek_bezici(flow)

        for flow in podklad.bezici:
            self._napln_bezici(self._radky_bezici[flow.id], flow)

        souhrn = podklad.souhrn
        self.podnadpis_bezici.set_text(
            sklonuj(
                souhrn.otevrenych_pozic,
                "otevřená pozice",
                "otevřené pozice",
                "otevřených pozic",
            )
            + f" · {penize(souhrn.otevreno)} USD"
            if souhrn.otevrenych_pozic
            else "žádná otevřená pozice"
        )

    # ------------------------------------------------------------------
    # Seznam uzavřených obchodů
    # ------------------------------------------------------------------

    def _radek_uzavreny(self, flow: Flow, meritko: float) -> None:
        """
        Řádek ukončeného obchodu. Hodnoty se už nemění, proto se zapisují
        rovnou; měřítko je největší výsledek dne v absolutní hodnotě a určuje
        délku pruhu, kterým se obchody porovnávají mezi sebou.
        """
        vysledek = flow.realized_pnl
        with ui.element("div").classes("report-radek report-radek-uzavreny"):
            ui.label(f"{flow.updated_at:%H:%M}").classes("bunka bunka-cas")
            with ui.element("div").classes("bunka bunka-ticker"):
                ui.label(flow.symbol).classes("report-ticker")
                smer = "LONG" if flow.right == "C" else "SHORT"
                ui.label(smer).classes(
                    f"odznak-smer {'smer-long' if flow.right == 'C' else 'smer-short'}"
                )
            ui.label(kontrakt_text(flow)).classes("bunka bunka-kontrakt")
            ui.label(str(flow.filled_quantity or flow.quantity)).classes(
                "bunka bunka-cislo"
            )
            # Nákupní a prodejní cena opce vedle sebe; bez nákupu jen pomlčka
            if flow.fill_price is None:
                obchod = "-"
            else:
                obchod = (
                    f"{penize(flow.fill_price, znamenko=False)} → "
                    f"{penize(flow.exit_fill_price, znamenko=False)}"
                )
            ui.label(obchod).classes("bunka bunka-obchod")
            ui.label(doba_drzeni(flow.holding_seconds)).classes("bunka bunka-cislo")
            ui.label(flow.result_reason).classes("bunka bunka-duvod")
            ui.label(penize(vysledek)).classes(
                f"bunka bunka-cislo bunka-pnl {trida_vysledku(vysledek)}"
            )

            # Rozvážený pruh: ztráta roste doleva od středu, zisk doprava
            with ui.element("div").classes("bunka bunka-pomer"):
                podil = abs(vysledek or 0.0) / meritko if meritko > 0 else 0.0
                with ui.element("div").classes("pomer-pulka"):
                    ui.linear_progress(
                        value=podil if (vysledek or 0) < 0 else 0.0,
                        show_value=False,
                        size="9px",
                        color="red-6",
                    ).props("reverse rounded track-color=transparent")
                with ui.element("div").classes("pomer-pulka"):
                    ui.linear_progress(
                        value=podil if (vysledek or 0) > 0 else 0.0,
                        show_value=False,
                        size="9px",
                        color="green-6",
                    ).props("rounded track-color=transparent")

    def _vykresli_uzavrene(self, podklad: report.DenniReport) -> None:
        """Vykreslí seznam ukončených obchodů; mění se jen s novým výsledkem."""
        podpis = tuple(
            (flow.id, flow.state.value, round(flow.realized_pnl or 0.0, 2))
            for flow in podklad.ukoncene
        )
        if podpis != self._podpis_uzavrene:
            self._podpis_uzavrene = podpis
            self.telo_uzavrene.clear()
            # Měřítko pruhů je největší výsledek dne v absolutní hodnotě
            meritko = max(
                (abs(flow.realized_pnl or 0.0) for flow in podklad.ukoncene), default=0.0
            )
            with self.telo_uzavrene:
                if not podklad.ukoncene:
                    ui.label("Zatím není uzavřený žádný obchod.").classes("report-prazdno")
                else:
                    self._hlavicka_seznamu(
                        "report-radek-uzavreny",
                        (
                            "Čas",
                            "Ticker",
                            "Kontrakt",
                            "Ks",
                            "Nákup → prodej",
                            "Drží",
                            "Důvod",
                            "Výsledek",
                            "Porovnání",
                        ),
                    )
                    for flow in podklad.ukoncene:
                        self._radek_uzavreny(flow, meritko)

        # Extrémy dne stojí v hlavičce panelu - rychlý pohled na to,
        # co den nejvíc vytáhlo nahoru a co dolů
        s = podklad.souhrn
        casti = []
        if s.nejlepsi:
            casti.append(f"nejlepší {s.nejlepsi[0]} {penize(s.nejlepsi[1])}")
        if s.nejhorsi and s.nejhorsi != s.nejlepsi:
            casti.append(f"nejhorší {s.nejhorsi[0]} {penize(s.nejhorsi[1])}")
        self.podnadpis_uzavrene.set_text(" · ".join(casti))

    # ------------------------------------------------------------------
    # Grafy
    # ------------------------------------------------------------------

    def _barvy(self) -> dict[str, str]:
        """Barvy grafů podle zvoleného vzhledu - grafy kreslí canvas, ne CSS."""
        if self.je_tmavy():
            return {
                "text": "#94a3b8",
                "mrizka": "#334155",
                "osa": "#475569",
                "zisk": "#4ade80",
                "ztrata": "#f87171",
                "zisk_slabe": "rgba(74, 222, 128, 0.25)",
                "ztrata_slabe": "rgba(248, 113, 113, 0.25)",
            }
        return {
            "text": "#475569",
            "mrizka": "#e2e8f0",
            "osa": "#cbd5e1",
            "zisk": "#16a34a",
            "ztrata": "#dc2626",
            "zisk_slabe": "rgba(22, 163, 74, 0.20)",
            "ztrata_slabe": "rgba(220, 38, 38, 0.20)",
        }

    def _prazdny_graf(self) -> dict[str, Any]:
        """Výchozí (prázdná) konfigurace grafu, než dorazí data."""
        return {"series": []}

    def _osy(self, kategorie: list[str], barvy: dict[str, str]) -> dict[str, Any]:
        """Společné nastavení os obou grafů - popisky, mřížka a formát čísel."""
        return {
            "xAxis": {
                "type": "category",
                "data": kategorie,
                "axisLabel": {"color": barvy["text"], "fontSize": 10},
                "axisLine": {"lineStyle": {"color": barvy["osa"]}},
                "axisTick": {"show": False},
            },
            "yAxis": {
                "type": "value",
                "axisLabel": {"color": barvy["text"], "fontSize": 10},
                "splitLine": {"lineStyle": {"color": barvy["mrizka"]}},
            },
        }

    def _vykresli_krivku(self, podklad: report.DenniReport) -> None:
        """
        Křivka kumulovaného realizovaného výsledku - jak se den vyvíjel.
        Začíná v nule, každý další bod patří jednomu uzavřenému obchodu.
        """
        # Prázdný graf by ukazoval jen holé osy - místo něj se zobrazí hláška
        self.graf_krivka.set_visibility(bool(podklad.krivka))
        self.prazdno_krivka.set_visibility(not podklad.krivka)

        podpis = (self.je_tmavy(),) + tuple(
            round(hodnota, 2) for _, hodnota in podklad.krivka
        )
        if podpis == self._podpis_krivka:
            return
        self._podpis_krivka = podpis

        barvy = self._barvy()
        kategorie = ["start"] + [f"{cas:%H:%M}" for cas, _ in podklad.krivka]
        hodnoty = [0.0] + [round(hodnota, 2) for _, hodnota in podklad.krivka]
        # Barva křivky se řídí tím, jak den skončil
        kladny = hodnoty[-1] >= 0
        barva = barvy["zisk"] if kladny else barvy["ztrata"]
        vypln = barvy["zisk_slabe"] if kladny else barvy["ztrata_slabe"]

        moznosti: dict[str, Any] = {
            "grid": {"left": 58, "right": 18, "top": 18, "bottom": 26},
            "tooltip": {"trigger": "axis"},
            **self._osy(kategorie, barvy),
            "series": [
                {
                    "type": "line",
                    "data": hodnoty,
                    "smooth": True,
                    # Bez monotónního prokládání by křivka mezi body přestřelovala
                    # a den by chvílemi vypadal ztrátověji, než ve skutečnosti byl
                    "smoothMonotone": "x",
                    "symbolSize": 7,
                    "lineStyle": {"width": 2.5, "color": barva},
                    "itemStyle": {"color": barva},
                    "areaStyle": {
                        "color": {
                            "type": "linear",
                            "x": 0,
                            "y": 0,
                            "x2": 0,
                            "y2": 1,
                            "colorStops": [
                                {"offset": 0, "color": vypln},
                                {"offset": 1, "color": "rgba(0, 0, 0, 0)"},
                            ],
                        }
                    },
                    # Nulová osa odděluje ziskový den od ztrátového
                    "markLine": {
                        "silent": True,
                        "symbol": "none",
                        "label": {"show": False},
                        "lineStyle": {"color": barvy["osa"], "type": "dashed"},
                        "data": [{"yAxis": 0}],
                    },
                }
            ],
        }
        self.graf_krivka.options.clear()
        self.graf_krivka.options.update(moznosti)
        self.graf_krivka.update()

        self.podnadpis_krivka.set_text(
            "kumulovaný realizovaný výsledek · "
            + sklonuj(len(podklad.krivka), "obchod", "obchody", "obchodů")
            if podklad.krivka
            else "zatím není co vykreslit"
        )

    def _vykresli_tickery(self, podklad: report.DenniReport) -> None:
        """Sloupcový graf výsledku po tickerech - realizovaný i otevřený."""
        self.graf_tickery.set_visibility(bool(podklad.podle_tickeru))
        self.prazdno_tickery.set_visibility(not podklad.podle_tickeru)

        podpis = (self.je_tmavy(),) + tuple(
            (polozka.symbol, round(polozka.celkem, 0)) for polozka in podklad.podle_tickeru
        )
        if podpis == self._podpis_tickery:
            return
        self._podpis_tickery = podpis

        barvy = self._barvy()
        kategorie = [polozka.symbol for polozka in podklad.podle_tickeru]
        # Každý sloupec si nese vlastní barvu i umístění popisku: zisk nahoru
        # od osy, ztráta dolů, popisek vždy na vnější straně sloupce
        data = []
        for polozka in podklad.podle_tickeru:
            hodnota = round(polozka.celkem, 2)
            kladny = hodnota >= 0
            data.append(
                {
                    "value": hodnota,
                    "itemStyle": {
                        "color": barvy["zisk"] if kladny else barvy["ztrata"],
                        "borderRadius": [3, 3, 0, 0] if kladny else [0, 0, 3, 3],
                    },
                    "label": {"position": "top" if kladny else "bottom"},
                }
            )

        # Popisky sloupců stojí vně sloupce, proto osa potřebuje rezervu -
        # jinak by se nejnižší hodnota překrývala s názvem tickeru pod grafem
        hodnoty = [polozka.celkem for polozka in podklad.podle_tickeru] or [0.0]
        osy = self._osy(kategorie, barvy)
        osy["yAxis"]["min"], osy["yAxis"]["max"] = mez_osy(hodnoty)

        moznosti: dict[str, Any] = {
            "grid": {"left": 58, "right": 18, "top": 24, "bottom": 26},
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
            **osy,
            "series": [
                {
                    "type": "bar",
                    "data": data,
                    "barMaxWidth": 42,
                    "label": {
                        "show": True,
                        "color": barvy["text"],
                        "fontSize": 10,
                        "fontWeight": "bold",
                    },
                }
            ],
        }
        self.graf_tickery.options.clear()
        self.graf_tickery.options.update(moznosti)
        self.graf_tickery.update()

        self.podnadpis_tickery.set_text(
            sklonuj(len(kategorie), "ticker", "tickery", "tickerů")
            + " · realizovaný i otevřený výsledek"
            if kategorie
            else "zatím není co vykreslit"
        )
