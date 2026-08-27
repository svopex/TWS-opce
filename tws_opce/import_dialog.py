"""
Dialog pro hromadné načtení vstupních pozic ze souboru se zadáním dne.

Formulář je obdobou běžného zadání obchodu, jen se místo jednoho tickeru
zpracuje celý seznam: společně se zvolí režim cíle, limit spreadu a případná
kompenzace SL o zaplacený spread. Každá pozice se pak připraví toutéž cestou
jako jednotlivé zadání (FlowEngine.prepare) a po odsouhlasení založí stejným
voláním (FlowEngine.start_flow), takže se chová přesně jako ruční zadání.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from nicegui import ui

from . import importer
from .config import AppConfig
from .engine import FlowEngine, Preview
from .ib_service import IBService
from .importer import ImportedPosition
from .models import PT_MULTIPLES, FlowRequest, cislo_text, pomer_z_rrr, rrr_z_pomeru

log = logging.getLogger(__name__)

# Režimy zadání cíle v dialogu
REZIM_PCT = "pct"
REZIM_USD = "usd"
REZIM_PREMIUM = "premium"

# Režimy, ve kterých jsou PT i SL zadané na opci - jen u nich má smysl
# kompenzace SL o zaplacený spread
REZIMY_NA_OPCI = (REZIM_USD, REZIM_PREMIUM)

# Hodnota volby runneru, která znamená "runner nezapínat"
RUNNER_VYPNUTO = "0"


def runner_volby(kratke: bool = False) -> dict[str, str]:
    """
    Nabídka nastavení runneru: vypnuto a násobky původní vzdálenosti cíle.
    Krátká varianta je pro combobox v řádku tabulky, kde není místo na
    celou větu.
    """
    vypnuto = "Bez" if kratke else "Nepoužít runner"
    return {RUNNER_VYPNUTO: vypnuto} | {
        f"{n:g}": f"{n:g}×".replace(".", ",") for n in PT_MULTIPLES
    }


def runner_nasobek(hodnota: Any) -> float | None:
    """Násobek cíle runneru z hodnoty volby; None znamená runner nezapínat."""
    text = str(hodnota or RUNNER_VYPNUTO)
    if text == RUNNER_VYPNUTO:
        return None
    try:
        return float(text)
    except ValueError:
        return None

# Popisky sloupců tabulky načtených pozic
SLOUPCE = (
    "", "Ticker", "Směr", "Vstup", "Cíl", "Kontrakt", "PT", "SL", "Ks", "Runner", "Stav"
)


def fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    """Číslo pro zobrazení v dialogu; chybějící hodnotu nahradí pomlčkou."""
    if value is None:
        return "-"
    return f"{cislo_text(value, digits)}{suffix}"


@dataclass
class RadekPozice:
    """
    Jedna načtená pozice v tabulce dialogu - data ze souboru i ovládací
    prvky jejího řádku. Hodnoty PT, SL a množství jsou editovatelné, proto
    se čtou až při zadávání do trhu, ne v okamžiku přípravy.
    """

    pozice: ImportedPosition
    vybrano: Any = None
    kontrakt_label: Any = None
    pt_input: Any = None
    sl_input: Any = None
    qty_input: Any = None
    runner_select: Any = None
    stav_label: Any = None
    obnovit_button: Any = None
    # Poslední připravený náhled - drží vybraný kontrakt a určený směr
    preview: Preview | None = None
    # Odhad nákupní ceny opce, ze kterého vyšel PT zadaný procentem prémie
    premie: float | None = None
    # Id obchodu, který z řádku vznikl; prázdné, dokud se nezadal. Podle
    # stavu tohoto obchodu se pozná, zda jde řádek zadat znovu
    flow_id: str = ""
    # Doplňující poznámka k založenému obchodu (třeba nezapnutý runner),
    # kterou obnovovaný popis stavu nesmí zahodit
    poznamka: str = ""
    # Stav řádku právě popisuje založený obchod a obnovovací smyčka jej drží
    # aktuální; jakýkoliv jiný zápis do stavu tuto značku sundá
    stav_z_obchodu: bool = False

    def stav(self, text: str, trida: str = "") -> None:
        """Zapíše stav řádku a obarví jej podle druhu sdělení."""
        self.stav_z_obchodu = False
        self.stav_label.set_text(text)
        self.stav_label.classes(
            remove="stav-import-ok stav-import-chyba stav-import-varovani",
            add=trida,
        )
        # Delší hlášky se do sloupce nevejdou, celé znění nabídne bublina
        self.stav_label.tooltip(text)


class ImportDialog:
    """
    Popup formulář pro načtení pozic ze souboru a jejich hromadné zadání.

    Drží vlastní sadu ovládacích prvků a s aplikací komunikuje jen přes
    engine; do běžného formuláře zadání nijak nezasahuje. Po založení
    obchodů zavolá on_created, aby se překreslil přehled.
    """

    def __init__(
        self,
        cfg: AppConfig,
        engine: FlowEngine,
        ib: IBService,
        on_created: Callable[[], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.engine = engine
        self.ib = ib
        self.on_created = on_created
        # Zvolené nastavení runneru pro zakládané pozice (klíč tlačítka)
        self.runner_value: str = RUNNER_VYPNUTO
        # Načtené pozice v pořadí ze souboru
        self.radky: list[RadekPozice] = []
        # Jméno naposledy načteného souboru - ukazuje se nad tabulkou
        self.nazev_souboru: str = ""

    # ------------------------------------------------------------------
    # Sestavení dialogu
    # ------------------------------------------------------------------

    def build(self) -> None:
        """
        Vykreslí dialog. Volá se jednou při stavbě stránky, aby prvky
        vznikly v kontextu klienta; otevírá se pak metodou open().
        """
        with ui.dialog().classes("dialog-import-obal") as self.dialog, ui.card().classes(
            "dialog-import"
        ):
            ui.label("Načtení pozic ze souboru").classes("dialog-nadpis")
            ui.label(
                "Čerpá se z položek s klíčem končícím plusem - ticker, vstupní "
                "a cílová cena podkladu."
            ).classes("dialog-popis")

            with ui.row().classes("radek radek-import-soubor"):
                self.upload = (
                    ui.upload(
                        label="Soubor se zadáním (YAML)",
                        on_upload=self._on_upload,
                        auto_upload=True,
                        max_files=1,
                    )
                    .props('accept=".yaml,.yml" flat dense')
                    .classes("nahrani-souboru")
                )
                self.soubor_label = ui.label("").classes("popis-souboru")

            self._build_parametry()

            # Indikace přípravy - stejná nenásilná pulzující hláška jako ve formuláři
            self.loading_label = ui.label("").classes("indikace-nacitani")
            self.loading_label.set_visibility(False)

            # Varování z načtení souboru (přeskočené položky)
            self.warning_label = ui.label("").classes("nahled-varovani")
            self.warning_label.set_visibility(False)

            # Tabulka načtených pozic; hlavička vzniká až s prvním souborem
            self.tabulka = ui.grid().classes("tabulka-import")
            self.tabulka.set_visibility(False)

            self.souhrn_label = ui.label("").classes("souhrn-import")

            with ui.row().classes("radek radek-tlacitka"):
                self.zadat_button = (
                    ui.button("Zadat vybrané pozice do trhu", on_click=self._zadej)
                    .props("color=green-8")
                    .classes("tlacitko-import-akce")
                )
                self.zadat_button.set_enabled(False)
                ui.button("Zavřít", on_click=self.dialog.close).props("flat").classes(
                    "tlacitko-import-akce"
                )

    def _build_parametry(self) -> None:
        """Společné parametry pro všechny načtené pozice - režim cíle a spread."""
        with ui.column().classes("prepinace prepinace-import"):
            with ui.column().classes("skupina-prepinacu"):
                self.rezim = (
                    ui.radio(
                        {
                            REZIM_PCT: "PT na podkladu v % dráhy k cíli",
                            REZIM_USD: "PT na opci v USD/ks",
                            REZIM_PREMIUM: "PT na opci v % prémie",
                        },
                        value=REZIM_PCT,
                    )
                    .props("dense")
                    .classes("prepinac")
                    .tooltip(
                        "% dráhy k cíli: PT je cena podkladu, 100 % je přesně cílová "
                        "cena ze souboru; SL se dopočítá také na podkladu podle poměru "
                        "SL:PT z konfigurace. USD/ks: PT je zisk na jedné opci a SL "
                        "ztráta na opci, obojí podle téhož poměru. % prémie: totéž, "
                        "ale zadané podílem z ceny opce - 30 % z opce za 3,00 je "
                        "90 USD na kontrakt, takže levná i drahá opce riskuje stejný "
                        "díl vložených peněz. V obou opčních režimech se cílová cena "
                        "ze souboru nepoužívá."
                    )
                )
                self.rezim.on_value_change(lambda _: self._on_rezim_change())

                # Kompenzace spreadu patří k SL na opci, tedy jen k režimu v USD
                self.sl_spread_compensated = (
                    ui.checkbox(
                        "SL o zaplacený spread dál",
                        value=self.cfg.trading.sl_spread_compensated,
                    )
                    .props("dense")
                    .classes("prepinac prepinac-podrizeny")
                    .tooltip(
                        "Zaškrtnuto: k SL na opci se při nákupu připočte skutečně "
                        "zaplacený spread (nákupní cena minus BID), takže zadaná "
                        "hodnota odpovídá pohybu ceny opce. Ztráta na kontrakt "
                        "o tento spread naroste a množství úměrně klesne."
                    )
                )

        with ui.row().classes("radek"):
            self.pct_input = (
                ui.number("PT [%]", value=100.0, format="%.2f", min=0)
                .classes("pole")
                .props("outlined dense step=any")
            )
            self.usd_input = (
                ui.number("PT [USD/ks]", value=None, format="%.2f", min=0)
                .classes("pole")
                .props("outlined dense step=any")
            )
            self.premium_input = (
                ui.number("PT [% prémie]", value=None, format="%.2f", min=0)
                .classes("pole")
                .props("outlined dense step=any")
            )
            self.spread_input = (
                ui.number(
                    "Max. spread [%]",
                    value=self.cfg.trading.max_spread_pct,
                    format="%.2f",
                    min=0,
                )
                .classes("pole")
                .props("outlined dense step=any")
            )
            # RRR pro dopočet SL z PT u všech načtených pozic; mění se
            # zřídka, výchozí hodnota vychází z konfigurace
            self.rrr_input = (
                ui.number(
                    "RRR (PT:SL)",
                    value=rrr_z_pomeru(self.cfg.trading.sl_to_pt_ratio),
                    format="%g",
                    min=0,
                )
                .classes("pole")
                .props("outlined dense step=any")
                .tooltip(
                    "Poměr zisku ku riziku, kterým se z PT dopočítá SL: "
                    "2 = PT je dvakrát dál než SL, 1 = obě stejně daleko. "
                    "Výchozí hodnota vychází z konfigurace (převrácené "
                    "trading.sl_to_pt_ratio), prázdné či nekladné pole "
                    "se k ní vrací."
                )
            )
            ui.button("Přepočítat", on_click=lambda: self._priprav_vse()).props(
                "outline"
            ).classes("tlacitko-vedle").tooltip(
                "Přepíše PT, SL i množství u všech načtených pozic hodnotami "
                "spočítanými podle nastavení nad tabulkou."
            )

        # Počáteční nastavení runneru, společné všem zakládaným pozicím.
        # Runner se zapíná až po založení obchodu, stejně jako tlačítky
        # v přehledu - před nákupem si volbu obchod jen zapamatuje
        with ui.row().classes("radek radek-runner"):
            ui.label("Runner:").classes("popisek-runner-import")
            napoveda = (
                "Výchozí nastavení runneru pro všechny načtené pozice - přepíše "
                "volbu ve sloupci Runner, kde ji lze u každé pozice doladit "
                "zvlášť. Runner je část pozice "
                f"({self.cfg.trading.runner_quantity} ks podle konfigurace) "
                "s vlastním, vzdálenějším cílem na zvoleném násobku původní "
                "vzdálenosti PT od vstupu. Pozice s menším množstvím runner "
                "nedostane a dá se to poznat ve sloupci Stav."
            )
            self.runner_buttons: dict[str, Any] = {}
            for hodnota, popisek in runner_volby().items():
                tlacitko = (
                    ui.button(
                        popisek,
                        on_click=lambda _=None, h=hodnota: self._nastav_runner(h),
                    )
                    .props("dense no-caps size=sm")
                    .classes("tlacitko-runner")
                )
                tlacitko.tooltip(napoveda)
                self.runner_buttons[hodnota] = tlacitko
            self._zvyrazni_runner()

        # Výchozí režim rozhoduje, které pole cíle je vidět a zda je dostupná
        # kompenzace spreadu
        self._on_rezim_change(prepocitat=False)

    # ------------------------------------------------------------------
    # Otevření a načtení souboru
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Otevře dialog a připomene stav spojení s TWS."""
        self.dialog.open()
        # Otevření formuláře nabídne k zadání vše, co zadat lze. Zaškrtnutí
        # sundané dřívějším zadáním nebo propásnutým vstupem se tím obnoví,
        # takže se dá celý soubor poslat do trhu znovu jedním tlačítkem
        self._vyber_vse()
        if not self.ib.connected:
            ui.notify(
                "Není navázáno spojení s TWS - pozice se načtou, ale SL "
                "ani množství se bez něj nedopočítají.",
                type="warning",
            )

    def _on_rezim_change(self, prepocitat: bool = True) -> None:
        """
        Přepnutí režimu cíle: ukáže se pole odpovídající zvolenému režimu
        a kompenzace spreadu se zpřístupní jen u SL na opci. Načtené pozice
        se rovnou přepočítají, protože se mění význam všech úrovní.
        """
        rezim = self.rezim.value
        self.pct_input.set_visibility(rezim == REZIM_PCT)
        self.usd_input.set_visibility(rezim == REZIM_USD)
        self.premium_input.set_visibility(rezim == REZIM_PREMIUM)
        # Kompenzace spreadu patří k SL na opci - tedy k oběma opčním režimům
        self.sl_spread_compensated.set_enabled(rezim in REZIMY_NA_OPCI)
        # Úrovně z předchozího režimu mají jiný význam, proto se pole vyprázdní
        if prepocitat and self.radky:
            for radek in self.radky:
                if not self._zamceno(radek):
                    radek.pt_input.set_value(None)
                    radek.sl_input.set_value(None)
                    radek.premie = None
            self._naplanuj_pripravu()

    async def _on_upload(self, event: Any) -> None:
        """
        Zpracuje vybraný soubor: načte pozice, vykreslí tabulku a rovnou
        připraví zadání, aby obchodník viděl kontrakty, SL i množství.
        """
        soubor = event.file
        try:
            obsah = await soubor.text("utf-8")
        except UnicodeDecodeError:
            ui.notify("Soubor není v kódování UTF-8.", type="negative")
            return
        except Exception as exc:
            ui.notify(f"Soubor se nepodařilo přečíst: {exc}", type="negative")
            return
        finally:
            # Nahrávací prvek se čistí vždy, aby šel týž soubor vybrat znovu
            self.upload.reset()

        try:
            vysledek = importer.parse_positions(obsah)
        except ValueError as exc:
            ui.notify(str(exc), type="negative")
            return

        self.nazev_souboru = soubor.name
        self.soubor_label.set_text(
            f"{soubor.name} – {len(vysledek.positions)} pozic k zadání"
        )
        self._vykresli_tabulku(vysledek.positions)

        if vysledek.warnings:
            self.warning_label.set_text(
                "Přeskočené položky: " + " | ".join(vysledek.warnings)
            )
        self.warning_label.set_visibility(bool(vysledek.warnings))

        if not vysledek.positions:
            ui.notify("V souboru není žádná použitelná položka.", type="warning")
            return

        ui.notify(
            f"Načteno {len(vysledek.positions)} pozic ze souboru {soubor.name}.",
            type="positive",
        )
        await self._priprav_vse()

    def _vykresli_tabulku(self, pozice: list[ImportedPosition]) -> None:
        """Postaví tabulku načtených pozic - hlavičku a řádek pro každou pozici."""
        self.radky = []
        self.tabulka.clear()
        self.tabulka.set_visibility(bool(pozice))
        self.zadat_button.set_enabled(bool(pozice))
        self.souhrn_label.set_text("")
        if not pozice:
            return

        with self.tabulka:
            for popisek in SLOUPCE:
                ui.label(popisek).classes("hlavicka-import")

            for polozka in pozice:
                self.radky.append(self._vykresli_radek(polozka))

        self._obnov_souhrn()

    def _vykresli_radek(self, pozice: ImportedPosition) -> RadekPozice:
        """
        Vykreslí buňky jednoho řádku tabulky. Buňky jsou přímými potomky
        mřížky, jinak by se sloupce nezarovnaly.
        """
        radek = RadekPozice(pozice=pozice)

        radek.vybrano = ui.checkbox(value=True).props("dense").classes("bunka-import")
        ui.label(pozice.symbol).classes("bunka-import bunka-ticker")
        ui.label(pozice.right_label).classes(
            "bunka-import odznak-smer "
            + ("smer-long" if pozice.right == "C" else "smer-short")
        )
        ui.label(fmt(pozice.entry_price)).classes("bunka-import bunka-cislo")
        ui.label(fmt(pozice.target_price)).classes("bunka-import bunka-cislo")
        radek.kontrakt_label = ui.label("-").classes("bunka-import bunka-kontrakt")

        radek.pt_input = (
            ui.number(value=None, format="%.2f")
            .classes("bunka-import pole-import")
            .props("outlined dense step=any")
        )
        radek.sl_input = (
            ui.number(value=None, format="%.2f")
            .classes("bunka-import pole-import")
            .props("outlined dense step=any")
        )
        radek.qty_input = (
            ui.number(value=None, format="%.0f", step=1, min=1)
            .classes("bunka-import pole-import pole-import-ks")
            .props("outlined dense")
        )
        # Runner se nastavuje u každé pozice zvlášť; výchozí je globální volba
        radek.runner_select = (
            ui.select(runner_volby(kratke=True), value=self.runner_value)
            .classes("bunka-import pole-import vyber-runner")
            .props("outlined dense options-dense")
        )

        with ui.row().classes("bunka-import bunka-stav"):
            radek.obnovit_button = (
                ui.button(
                    icon="refresh",
                    on_click=lambda _=None, r=radek: self._priprav_radek_rucne(r),
                )
                .props("flat dense round size=sm")
                .tooltip(
                    "Přepočítá SL a množství podle PT vyplněného v tomto řádku."
                )
            )
            radek.stav_label = ui.label("-").classes("stav-import")

        return radek

    # ------------------------------------------------------------------
    # Příprava zadání
    # ------------------------------------------------------------------

    def _cislo(self, hodnota: Any) -> float | None:
        """Hodnota číselného pole jako float; prázdné pole vrací None."""
        if hodnota in (None, ""):
            return None
        try:
            return float(hodnota)
        except (TypeError, ValueError):
            return None

    def _rezimy(self) -> tuple[bool, bool]:
        """
        Režim PT a SL podle volby dialogu: True = na podkladu.
        Procento prémie je jen jiný způsob zápisu úrovně na opci, proto se
        engine v obou opčních režimech chová stejně.
        """
        na_podkladu = self.rezim.value == REZIM_PCT
        return na_podkladu, na_podkladu

    def _nastav_runner(self, hodnota: str) -> None:
        """
        Zapamatuje výchozí volbu runneru a přenese ji do všech řádků, které
        lze zadat; u zamčeného obchodu s pozicí už by neměla co změnit.
        """
        self.runner_value = hodnota
        self._zvyrazni_runner()
        for radek in self.radky:
            if not self._zamceno(radek) and radek.runner_select is not None:
                radek.runner_select.set_value(hodnota)

    def _zvyrazni_runner(self) -> None:
        """
        Vybrané tlačítko je plné a oranžové, ostatní zůstávají jen obrysové -
        stejné rozlišení jako u tlačítek runneru v řádku přehledu.
        """
        for hodnota, tlacitko in self.runner_buttons.items():
            if hodnota == self.runner_value:
                tlacitko.props(add="color=orange-8", remove="outline")
            else:
                tlacitko.props(add="outline color=grey-7")

    def _pomer(self) -> float | None:
        """
        Poměr SL:PT pro engine, odvozený z RRR v dialogu.

        Dialog se ptá na RRR (kolikrát je PT dál než SL), engine počítá
        s obrácenou hodnotou. Prázdné i nekladné pole vrací None - engine
        pak použije hodnotu z konfigurace.
        """
        rrr = self._cislo(self.rrr_input.value)
        return pomer_z_rrr(rrr) if rrr is not None and rrr > 0 else None

    def _sl_spread(self) -> bool:
        """Kompenzace SL o spread - uplatní se jen při SL zadaném na opci."""
        return bool(self.sl_spread_compensated.value) and self.rezim.value in REZIMY_NA_OPCI

    def _zadana_hodnota(self) -> float | None:
        """
        Číslo vyplněné v poli aktuálního režimu. Prázdné i nekladné pole
        vrací None - zadání pak nedává smysl a příprava se neprovádí.
        """
        pole = {
            REZIM_PCT: self.pct_input,
            REZIM_USD: self.usd_input,
            REZIM_PREMIUM: self.premium_input,
        }[self.rezim.value]
        hodnota = self._cislo(pole.value)
        return hodnota if hodnota is not None and hodnota > 0 else None

    def _popis_hodnoty(self) -> str:
        """Název zadávané hodnoty pro hlášku o nevyplněném poli."""
        return {
            REZIM_PCT: "procento cíle",
            REZIM_USD: "PT na opci v USD",
            REZIM_PREMIUM: "PT v procentech prémie",
        }[self.rezim.value]

    def _cil_pro(self, pozice: ImportedPosition) -> float | None:
        """
        Cílová úroveň pro danou pozici podle zvoleného režimu: v procentech
        podíl dráhy ze vstupu k cíli ze souboru, v USD zisk na kontrakt
        společný všem pozicím.

        V režimu procenta z prémie vrací None i s vyplněným polem - PT se
        odvozuje od ceny opce, kterou zná až připravený náhled, a počítá se
        proto až v _pt_z_premie.
        """
        hodnota = self._zadana_hodnota()
        if hodnota is None:
            return None
        if self.rezim.value == REZIM_PCT:
            return importer.profit_target_from_pct(
                pozice.entry_price, pozice.target_price, hodnota
            )
        if self.rezim.value == REZIM_USD:
            return round(hodnota, 2)
        return None

    def _set_loading(self, active: bool, text: str = "") -> None:
        """Zobrazí, nebo skryje indikaci probíhající přípravy."""
        if active:
            self.loading_label.set_text(text)
        self.loading_label.set_visibility(active)

    def _naplanuj_pripravu(self) -> None:
        """
        Spustí přípravu až po doběhnutí právě probíhající obsluhy - stejným
        způsobem jako hlavní formulář, aby v ní fungovalo ui.notify.
        """
        with self.tabulka:
            ui.timer(0, lambda: self._priprav_vse(), once=True)

    async def _priprav_vse(self) -> None:
        """
        Připraví všechny dosud nezadané pozice podle nastavení nad tabulkou.
        Přepíše u nich PT, SL i množství, stejně jako tlačítko Přepočítat
        v běžném formuláři.
        """
        if not self.radky:
            ui.notify("Nejprve vyberte soubor s pozicemi.", type="warning")
            return
        if self._zadana_hodnota() is None:
            ui.notify(f"Vyplňte {self._popis_hodnoty()}.", type="warning")
            return
        # Bez spojení se PT přesto vyplní - je to čistý výpočet ze zadání.
        # Dopočet SL a množství potřebuje kontrakt a kotace z TWS
        if not self.ib.connected:
            ui.notify(
                "Není navázáno spojení s TWS - doplní se jen PT.", type="warning"
            )

        cekajici = [radek for radek in self.radky if not self._zamceno(radek)]
        celkem = len(cekajici)
        try:
            for poradi, radek in enumerate(cekajici, 1):
                self._set_loading(
                    True, f"Připravuji {radek.pozice.symbol} ({poradi}/{celkem})…"
                )
                await self._priprav_radek(radek)
        finally:
            self._set_loading(False)
        self._obnov_souhrn()

    async def _priprav_radek_rucne(self, radek: RadekPozice) -> None:
        """
        Přepočte jediný řádek a ponechá v něm ručně upravené PT.
        Slouží tlačítku v řádku po ruční změně cíle.
        """
        if self._zamceno(radek):
            return
        if not self.ib.connected:
            ui.notify("Není navázáno spojení s TWS.", type="negative")
            return
        self._set_loading(True, f"Připravuji {radek.pozice.symbol}…")
        try:
            await self._priprav_radek(radek, zachovat_pt=True)
        finally:
            self._set_loading(False)
        self._obnov_souhrn()

    async def _pt_z_premie(self, radek: RadekPozice) -> float | None:
        """
        PT v USD na kontrakt z procenta prémie zadaného nad tabulkou.

        Procento se vztahuje k ceně opce, kterou obchod nakoupí - tu ale
        aplikace vybírá až podle PT, takže se nejdřív připraví zadání s cílem
        ze souboru na podkladu. Z něj vyjde odhad nákupní ceny opce, a teprve
        z ní požadovaný podíl. Chyba i chybějící cena zapíše stav řádku
        a vrací None.
        """
        pct = self._zadana_hodnota()
        if pct is None:
            return None

        pozice = radek.pozice
        try:
            odhad = await self.engine.prepare(
                pozice.symbol,
                pozice.entry_price,
                pozice.target_price,
                None,
                True,
                True,
                False,
            )
        except Exception as exc:
            radek.premie = None
            radek.kontrakt_label.set_text("-")
            radek.stav(f"Chyba přípravy: {exc}", "stav-import-chyba")
            return None

        # Nejlepší je odhad ceny v okamžiku vstupu; bez modelu poslouží
        # aktuální ASK (nakupuje se u něj), nakonec cena pro model
        premie = odhad.expected_fill_price or odhad.option_ask or odhad.option_price
        if not premie or premie <= 0:
            radek.premie = None
            radek.kontrakt_label.set_text("-")
            radek.stav(
                "Cenu opce se nepodařilo zjistit - PT z prémie nelze spočítat.",
                "stav-import-chyba",
            )
            return None

        radek.premie = premie
        return importer.profit_target_from_premium_pct(pct, premie)

    async def _priprav_radek(self, radek: RadekPozice, zachovat_pt: bool = False) -> None:
        """
        Připraví jednu pozici: určí kontrakt, dopočítá SL podle poměru SL:PT
        z konfigurace a doporučené množství z rizika a delty opce - přesně
        jako běžný formulář, jen bez zásahu obchodníka.

        zachovat_pt bere PT z pole řádku (ruční úprava), jinak se počítá
        z nastavení nad tabulkou.
        """
        if zachovat_pt:
            pt = self._cislo(radek.pt_input.value)
            if pt is None:
                radek.stav("Vyplňte PT", "stav-import-chyba")
                return
            # Ručně přepsané PT už z prémie nevychází, poznámka o ní by mátla
            radek.premie = None
        else:
            pt = self._cil_pro(radek.pozice)
            # V režimu procenta z prémie se PT dopočítá až z ceny opce (níže),
            # jinak prázdná hodnota znamená nevyplněné zadání
            if pt is None and self.rezim.value != REZIM_PREMIUM:
                return

        # Známé PT patří do pole hned - nezávisí na kotacích, tak ať je vidět
        # i bez spojení s TWS
        if pt is not None:
            radek.pt_input.set_value(round(pt, 2))
        if not self.ib.connected:
            radek.kontrakt_label.set_text("-")
            radek.stav(
                "Bez spojení s TWS - "
                + (
                    "PT z prémie, SL ani množství nelze dopočítat."
                    if pt is None
                    else "SL ani množství nelze dopočítat."
                ),
                "stav-import-varovani",
            )
            return

        # Procento prémie se převádí na USD na kontrakt z odhadované nákupní
        # ceny opce; tu zná až připravený náhled, proto se sahá do TWS dvakrát
        if pt is None:
            pt = await self._pt_z_premie(radek)
            if pt is None:
                return
            radek.pt_input.set_value(pt)

        pt_on, sl_on = self._rezimy()
        try:
            preview = await self.engine.prepare(
                radek.pozice.symbol,
                radek.pozice.entry_price,
                pt,
                None,
                pt_on,
                sl_on,
                self._sl_spread(),
                self._pomer(),
            )
        except Exception as exc:
            radek.preview = None
            radek.kontrakt_label.set_text("-")
            radek.stav(f"Chyba přípravy: {exc}", "stav-import-chyba")
            return

        radek.preview = preview

        if not preview.expiration:
            radek.kontrakt_label.set_text("-")
            radek.stav(
                "Kontrakt se nepodařilo určit - zkontrolujte odběr tržních dat.",
                "stav-import-chyba",
            )
            return

        radek.kontrakt_label.set_text(
            f"{preview.right_label} {preview.expiration} @ {preview.strike:g}"
        )
        radek.sl_input.set_value(round(preview.stop_loss, 2))
        radek.qty_input.set_value(preview.quantity)

        # Skutečný směr určuje aplikace z aktuální ceny podkladu. Liší-li se
        # od směru daného souborem, trh už vstupní úroveň překonal - takový
        # řádek se odškrtne, aby se omylem nezaložil obchod na opačnou stranu
        if preview.current_price is not None and preview.right != radek.pozice.right:
            radek.vybrano.set_value(False)
            radek.stav(
                f"Vstup propásnut - podklad je na {fmt(preview.current_price)}, "
                f"z ceny vychází {preview.right_label} místo {radek.pozice.right_label}.",
                "stav-import-chyba",
            )
            return

        # U PT odvozeného z prémie se uvede, z jaké ceny opce se počítalo
        zaklad = (
            f" · prémie ≈ {fmt(radek.premie * 100)} USD" if radek.premie else ""
        )
        if preview.warnings:
            radek.stav(
                f"Připraveno{zaklad} s výhradami: " + " ".join(preview.warnings),
                "stav-import-varovani",
            )
        else:
            radek.stav(f"Připraveno{zaklad}", "stav-import-ok")

    def _zamceno(self, radek: RadekPozice) -> bool:
        """
        True, pokud řádek nelze zadat znovu.

        Zámek drží jedině obchod, který z řádku vznikl a už drží (nebo právě
        uzavírá) pozici - ten by nové zadání muselo zrušit i s pozicí, což
        engine zakazuje. Obchod čekající na vstup engine při novém zadání sám
        nahradí, ukončený už nepřekáží a smazaný z přehledu neexistuje.
        """
        if not radek.flow_id:
            return False
        flow = self.engine.flows.get(radek.flow_id)
        if flow is None:
            return False
        return flow.state.is_active and not flow.state.is_before_entry

    def _stav_zadaneho(self, radek: RadekPozice) -> tuple[str, str]:
        """
        Popis a barva stavu řádku, ze kterého už vznikl obchod.
        Text sleduje živý stav obchodu, aby bylo vidět, proč řádek jde
        (nebo nejde) zadat znovu.
        """
        flow = self.engine.flows.get(radek.flow_id)
        if flow is None:
            return (
                f"Obchod {radek.flow_id} už není v přehledu - lze zadat znovu.",
                "stav-import-varovani",
            )

        popis_runneru = ""
        if flow.runner_active:
            nasobek = flow.runner_multiple
            popis_runneru = f", runner {flow.runner_quantity} ks"
            if nasobek is not None:
                popis_runneru += f" na {nasobek:g}×"
        text = f"Zadáno {flow.id} – {flow.state.label}{popis_runneru}"
        if radek.poznamka:
            return f"{text}; {radek.poznamka}", "stav-import-varovani"
        if flow.state.is_active:
            return text, "stav-import-ok"
        return f"{text} - lze zadat znovu.", "stav-import-varovani"

    def _zapis_stav_obchodu(self, radek: RadekPozice) -> None:
        """
        Zapíše do stavu řádku živý popis založeného obchodu a označí stav
        jako obchodem řízený - obnova jej pak drží aktuální.
        """
        text, trida = self._stav_zadaneho(radek)
        radek.stav(text, trida)
        radek.stav_z_obchodu = True

    def _obnov_zamky(self) -> None:
        """
        Zpřístupní, nebo zamkne řádky podle stavu obchodů, které z nich
        vznikly. Zamčený řádek se zároveň odškrtne, aby nezůstal ve výběru
        k zadání - pozice se mohla nakoupit až po jeho zaškrtnutí.
        """
        for radek in self.radky:
            zamceno = self._zamceno(radek)
            radek.vybrano.set_enabled(not zamceno)
            radek.obnovit_button.set_enabled(not zamceno)
            if zamceno and radek.vybrano.value:
                radek.vybrano.set_value(False)

    def _vyber_vse(self) -> None:
        """
        Zaškrtne všechny řádky, které lze zadat. Zamčený řádek (obchod už
        drží pozici) zůstává odškrtnutý - zadat se stejně nedá.
        """
        if not self.radky:
            return
        for radek in self.radky:
            radek.vybrano.set_value(not self._zamceno(radek))
        self._obnov_souhrn()

    def refresh(self) -> None:
        """
        Udrží otevřený dialog v souladu se skutečností - stav založených
        obchodů, zámky řádků i souhrn pod tabulkou. Volá se z periodické
        smyčky rozhraní; zavřený dialog se přeskakuje.
        """
        if not self.dialog.value or not self.radky:
            return

        for radek in self.radky:
            if radek.flow_id and radek.stav_z_obchodu:
                self._zapis_stav_obchodu(radek)
        self._obnov_zamky()
        self._obnov_souhrn()

    def _obnov_souhrn(self) -> None:
        """Souhrn pod tabulkou - kolik pozic a kontraktů se chystá do trhu."""
        vybrane = [
            radek
            for radek in self.radky
            if radek.vybrano.value and not self._zamceno(radek)
        ]
        kontrakty = sum(int(self._cislo(radek.qty_input.value) or 0) for radek in vybrane)
        self.souhrn_label.set_text(
            f"K zadání {len(vybrane)} pozic, celkem {kontrakty} kontraktů | "
            f"risk na obchod {fmt(self.engine.risk_amount)} USD "
            f"z účtu {fmt(self.engine.account_size)} USD"
        )

    # ------------------------------------------------------------------
    # Zadání do trhu
    # ------------------------------------------------------------------

    async def _zadej(self) -> None:
        """
        Založí obchody pro vybrané řádky - každý stejným voláním jako běžný
        formulář. Chyba jedné pozice ostatní nezastaví, zapíše se do jejího
        stavu; už založený řádek se podruhé nezadává.
        """
        vybrane = [
            radek
            for radek in self.radky
            if radek.vybrano.value and not self._zamceno(radek)
        ]
        if not vybrane:
            ui.notify("Není vybrána žádná pozice k zadání.", type="warning")
            return

        max_spread = self._cislo(self.spread_input.value)
        pt_on, sl_on = self._rezimy()
        sl_spread = self._sl_spread()
        pomer = self._pomer()

        zalozeno = 0
        chyb = 0
        celkem = len(vybrane)
        self._set_loading(True, "Zadávám obchody do trhu…")
        try:
            for poradi, radek in enumerate(vybrane, 1):
                self._set_loading(
                    True, f"Zadávám {radek.pozice.symbol} ({poradi}/{celkem})…"
                )
                pt = self._cislo(radek.pt_input.value)
                sl = self._cislo(radek.sl_input.value)
                qty = self._cislo(radek.qty_input.value)
                if pt is None and sl is None:
                    radek.stav("Vyplňte PT nebo SL", "stav-import-chyba")
                    chyb += 1
                    continue

                # PT dopočítané z procenta prémie si nese i cenu opce, ze
                # které vyšlo - formulář pak obchod nabídne zase v procentech.
                # Ručně přepsané PT prémii zahazuje, takže se sem nedostane
                v_procentech = bool(radek.premie and not pt_on)
                request = FlowRequest(
                    symbol=radek.pozice.symbol,
                    entry_price=radek.pozice.entry_price,
                    profit_target=pt,
                    stop_loss=sl,
                    quantity=int(qty) if qty else None,
                    max_spread_pct=max_spread,
                    pt_on_underlying=pt_on,
                    sl_on_underlying=sl_on,
                    sl_spread_compensated=sl_spread,
                    sl_to_pt_ratio=pomer,
                    pt_in_premium=v_procentech,
                    premium_base=radek.premie if v_procentech else None,
                )
                try:
                    flow = await self.engine.start_flow(request)
                except Exception as exc:
                    radek.stav(f"Chyba zadání: {exc}", "stav-import-chyba")
                    chyb += 1
                    continue

                # Řádek si založený obchod zapamatuje - podle jeho stavu se
                # pozná, zda jde zadat znovu. Zaškrtnutí se sundá, aby druhý
                # stisk tlačítka tentýž řádek neposlal do trhu podruhé
                radek.flow_id = flow.id
                radek.poznamka = ""
                radek.vybrano.set_value(False)

                # Runner se zapíná až na hotovém obchodu. Nezdaří-li se
                # (typicky málo kontraktů), obchod běží dál - jen se to připíše
                # do stavu, aby to nezapadlo
                nasobek = runner_nasobek(radek.runner_select.value)
                if nasobek is not None:
                    try:
                        await self.engine.set_runner(flow.id, nasobek)
                    except Exception as exc:
                        radek.poznamka = f"runner nezapnut: {exc}"

                self._zapis_stav_obchodu(radek)
                zalozeno += 1
        finally:
            self._set_loading(False)

        self._obnov_zamky()
        self._obnov_souhrn()
        if self.on_created:
            self.on_created()

        if chyb:
            ui.notify(
                f"Založeno {zalozeno} obchodů, {chyb} se nezdařilo - "
                f"podrobnosti jsou ve sloupci Stav.",
                type="warning",
            )
            return

        ui.notify(f"Založeno {zalozeno} obchodů ze souboru.", type="positive")
        self.dialog.close()
