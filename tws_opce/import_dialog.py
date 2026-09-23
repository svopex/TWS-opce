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

from . import importer, widgets
from .config import AppConfig
from .engine import FlowEngine, Preview
from .ib_service import IBService
from .importer import ImportedPosition
from .models import (
    EntryMissedError,
    MODE_PREMIUM,
    MODE_UNDERLYING,
    MODE_USD,
    MODES_ON_OPTION,
    RUNNER_VYPNUTO,
    FlowRequest,
    cislo_text,
    cislo_z_pole,
    format_countdown,
    pomer_z_rrr,
    priznaky_urovne,
    rrr_z_pomeru,
    runner_klic,
    runner_nasobek,
    runner_volby,
    sekundy_prepoctu,
    urovne_z_rezimu,
)

log = logging.getLogger(__name__)

# Režimy zadání cíle v dialogu
REZIM_PCT = "pct"
REZIM_USD = "usd"
REZIM_PREMIUM = "premium"

# Režim úrovní (viz models.MODE_*), který z každé volby cíle vyplyne. PT i SL
# sdílejí jednu volbu záměrně - u hromadného zadání se dopočítaný SL nemá jak
# přepnout zvlášť, takže by se obchod do běžného formuláře vrátil s každou
# úrovní v jiné jednotce
UROVNE_CILE = {
    REZIM_PCT: MODE_UNDERLYING,
    REZIM_USD: MODE_USD,
    REZIM_PREMIUM: MODE_PREMIUM,
}

# Režimy, ve kterých jsou PT i SL zadané na opci - jen u nich má smysl
# kompenzace SL o zaplacený spread
REZIMY_NA_OPCI = tuple(
    rezim for rezim, uroven in UROVNE_CILE.items() if uroven in MODES_ON_OPTION
)

# Jednotka, ve které tabulka dialogu ukazuje PT a SL. V obou opčních režimech
# je to USD na kontrakt i tehdy, když se cíl zadával v procentech prémie -
# procento je jen jednotka zadání, kterou si pamatuje až založený obchod
JEDNOTKY_UROVNI = {
    MODE_UNDERLYING: "podklad",
    MODE_USD: "USD/ks",
    MODE_PREMIUM: "USD/ks",
}

# Popisek tlačítka naplánovaného zadání ve vypnutém stavu; zapnutý stav
# ukazuje odpočet, proto se skládá až za běhu
POPIS_PLANU_VYPNUTO = "Zadat po otevření trhu"


def uroven_cile(rezim: str) -> str:
    """
    Režim úrovní PT i SL pro zvolený režim cíle nad tabulkou:

      cíl v % dráhy k cíli -> obojí na podkladu (cena podkladu)
      cíl v USD/ks         -> obojí na opci v USD na kontrakt
      cíl v % prémie       -> obojí na opci v procentech prémie

    Neznámý režim je chyba volajícího a padá na KeyError stejně jako
    _zadana_hodnota - tiché uhnutí k výchozí hodnotě by z úrovně na opci
    udělalo cenu podkladu a projevilo by se až špatně zadaným obchodem.
    """
    return UROVNE_CILE[rezim]


def vychozi(hodnota: Any, zaloha: Any) -> Any:
    """
    Výchozí hodnota pole dialogu: prázdná volba v sekci import konfigurace
    znamená převzít nastavení z odpovídající volby v sekci trading.
    """
    return zaloha if hodnota is None else hodnota


# Popisky sloupců tabulky načtených pozic. Hlavičky PT a SL se doplňují
# o jednotku podle zvoleného režimu cíle - viz _popis_hlavicky
SLOUPCE = (
    "", "Ticker", "Směr", "Vstup", "Cíl", "Kontrakt", "PT", "SL", "Ks", "Runner", "Stav"
)

# Sloupce s úrovněmi, jejichž hlavička nese jednotku
SLOUPCE_UROVNI = ("PT", "SL")


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
    # Runner v řádku přepnul obchodník ručně - taková volba platí bez ohledu
    # na množství, kdežto volbu přidělenou dialogem si obchod odnáší
    # s minimem. Druhý příznak kryje programový zápis, při kterém obsluha
    # comboboxu běží také
    runner_rucne: bool = False
    runner_prepis: bool = False
    stav_label: Any = None
    obnovit_button: Any = None
    # Poslední připravený náhled - drží vybraný kontrakt a určený směr
    preview: Preview | None = None
    # Odhad nákupní ceny opce, ze kterého vyšel PT zadaný procentem prémie
    premie: float | None = None
    # Režim cíle, ve kterém platí čísla v polích řádku. Prázdné znamená, že
    # řádek nemá platnou přípravu - buď selhala, nebo se od ní změnil režim
    # a úrovně by šly do trhu ve špatné jednotce. Takový řádek se nezadává
    rezim_hodnot: str = ""
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
        self.runner_value: str = runner_klic(cfg.import_.runner_multiple)
        # Pole s nejmenším množstvím, od kterého se runner nastavuje. Vzniká
        # až s vykresleným dialogem, do té doby platí hodnota z konfigurace
        self.runner_min_input: Any = None
        # Přepínač a prodleva přepočtu po otevření burzy - také vznikají až
        # s vykresleným dialogem, do té doby platí konfigurace
        self.refresh_checkbox: Any = None
        self.refresh_sec_input: Any = None
        # Průběžný přepočet čekajících obchodů - stejný režim jako výše
        self.interval_checkbox: Any = None
        self.interval_sec_input: Any = None
        # Načtené pozice v pořadí ze souboru
        self.radky: list[RadekPozice] = []
        # Jméno naposledy načteného souboru - ukazuje se nad tabulkou
        self.nazev_souboru: str = ""
        # Hlavičky sloupců s úrovněmi; jejich popisek nese jednotku, která se
        # mění s režimem cíle. Vznikají až s prvním načteným souborem
        self.hlavicky: dict[str, Any] = {}
        # Právě běží dávkové zadávání do trhu - druhý stisk tlačítka by
        # pracoval se zastaralým výběrem a poslal tytéž pozice podruhé
        self.zadavani: bool = False
        # Právě běží příprava řádků - zadávat se smí až po ní, jinak by šla
        # do trhu čísla z rozpracovaného přepočtu
        self.priprava: bool = False
        # Naplánované zadání po otevření trhu. plan_aktivni drží zapnutý
        # režim, dokud okamžik spuštění teprve nastane; plan_bezi pak kryje
        # vlastní běh (přepočet a zadání dávky), po který se plán nesmí
        # zrušit ani spustit podruhé. Prodleva se zafixuje při zapnutí, aby
        # pozdější úprava pole neposunula okamžik, na který se čeká
        self.plan_aktivni: bool = False
        self.plan_bezi: bool = False
        self.plan_prodleva: float = 0.0
        # Tlačítko plánu vzniká až s vykresleným dialogem
        self.plan_button: Any = None
        # Varování na chybějící spojení s TWS nad tlačítky; také vzniká až
        # s vykresleným dialogem
        self.spojeni_label: Any = None

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
            # Zavřený dialog ukončí naplánované zadání, ať se ven dostane
            # jakkoliv - tlačítkem, klávesou i kliknutím mimo kartu
            self.dialog.on_value_change(self._na_zavreni_dialogu)
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

            # Varování na chybějící spojení s TWS. Stojí těsně nad tlačítky,
            # aby ho obchodník viděl ve chvíli, kdy zadání do trhu spouští
            self.spojeni_label = ui.label("").classes(
                "varovani-spojeni varovani-spojeni-klid"
            )
            self.spojeni_label.set_visibility(False)

            with ui.row().classes("radek radek-tlacitka"):
                self.zadat_button = (
                    ui.button("Zadat vybrané pozice do trhu", on_click=self._zadej)
                    .props("color=green-8")
                    .classes("tlacitko-import-akce")
                )
                self.zadat_button.set_enabled(False)
                # Naplánované zadání: po otevření trhu a uplynulé prodlevě se
                # samo provede přepočet a hned po něm zadání vybraných pozic
                self.plan_button = (
                    ui.button(POPIS_PLANU_VYPNUTO, on_click=self._prepni_plan)
                    .props("outline color=orange-8")
                    .classes("tlacitko-import-akce tlacitko-plan")
                )
                self.plan_button.set_enabled(False)
                self.plan_button.tooltip(
                    "Zapne jednorázový režim: po otevření trhu a uplynutí "
                    "prodlevy vpravo nahoře se samo provede přepočet všech "
                    "nezadaných řádků a hned po něm zadání vybraných pozic "
                    "do trhu - tytéž dva kroky jako tlačítka Přepočítat "
                    "a Zadat vybrané pozice do trhu, včetně kontroly, zda "
                    "podklad od nastaveného času nepřekročil vstup (takový "
                    "řádek se označí Vstup propásnut a odškrtne). Použitelné i pro pozice, "
                    "které v trhu ještě vůbec nejsou. Proběhne jediný přepočet: "
                    "zadané obchody už nedostanou Po otevření trhu přepočítat, "
                    "takže v přehledu mají stejná čísla i runner jako v dialogu. "
                    "Režim ukončí opětovný "
                    "stisk, kterékoliv z obou tlačítek, zavření dialogu "
                    "i načtení jiného souboru. Zapnout jde jen dokud okamžik "
                    "přepočtu teprve nastane."
                )
                ui.button("Zavřít", on_click=self.dialog.close).props("flat").classes(
                    "tlacitko-import-akce"
                )

    def _build_parametry(self) -> None:
        """Společné parametry pro všechny načtené pozice - režim cíle a spread."""
        # Výchozí obsah formuláře je z konfigurace; klíče režimů se shodují
        # s hodnotami import.pt_mode, takže se přebírají přímo
        imp = self.cfg.import_
        # Přepínače režimu cíle vlevo; vpravo od nich, v jinak prázdném místě,
        # stojí blok přepočtu po otevření burzy
        with ui.row().classes("radek radek-rezim"):
            with ui.column().classes("prepinace prepinace-import"):
                with ui.column().classes("skupina-prepinacu"):
                    self.rezim = (
                        ui.radio(
                            {
                                REZIM_PCT: "PT na podkladu v % dráhy k cíli",
                                REZIM_USD: "PT na opci v USD/ks",
                                REZIM_PREMIUM: "PT na opci v % prémie",
                            },
                            value=imp.pt_mode,
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
                            value=vychozi(
                                imp.sl_spread_compensated,
                                self.cfg.trading.sl_spread_compensated,
                            ),
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

            # Přepočet po otevření burzy. Obchody zadané před otevřením vychází
            # z odhadu prémie ze závěrečné ceny, který po gapu neplatí; engine
            # je po zadané prodlevě od otevření přepočítá podle živých kotací
            # a čekající příkaz upraví na místě. Volba se zapisuje do každého
            # zakládaného obchodu, takže platí i po zavření dialogu
            with ui.row().classes("blok-obnova"):
                self.refresh_checkbox, self.refresh_sec_input = widgets.blok_prepoctu(
                    "Po otevření trhu přepočítat",
                    "Prodleva [s]",
                    imp.refresh_after_open,
                    imp.refresh_after_open_sec,
                    0,
                    "Zaškrtnuto: obchody z této dávky, které po otevření burzy "
                    "ještě čekají na vstup, se po uplynutí prodlevy vpravo jednou "
                    "přepočítají podle živých kotací - PT v procentech prémie, SL "
                    "i množství vyjdou ze skutečné ceny opce místo odhadu ze "
                    "závěrečné ceny. Čekající příkaz v trhu se upraví na místě, "
                    "neruší se. Obchod, který už nakoupil, se nemění. Spread nad "
                    "limitem přepočet nezdrží - počítá se s Max. spread. Zadat po otevření trhu "
                    "volbu nepoužije - dávku přepočítá samo až po otevření, "
                    "takže druhý přepočet není potřeba.",
                    widgets.NAPOVEDA_PRODLEVY,
                    "pole pole-obnova-sec",
                )

            # Průběžný přepočet za otevřené burzy - volba se zapisuje do obchodu
            with ui.row().classes("blok-obnova"):
                self.interval_checkbox, self.interval_sec_input = widgets.blok_prepoctu(
                    "Přepočítávat každých",
                    "Odstup [s]",
                    imp.refresh_interval,
                    imp.refresh_interval_sec,
                    1,
                    widgets.NAPOVEDA_INTERVALU.format(
                        rozsah="obchody z této dávky, které"
                    ),
                    widgets.NAPOVEDA_ODSTUPU,
                    "pole pole-obnova-sec",
                )

        with ui.row().classes("radek"):
            # Každý režim má vlastní pole s vlastní výchozí hodnotou -
            # přepínač jen mění, které z nich je vidět. Prázdná volba
            # v konfiguraci nechá pole nevyplněné
            self.pct_input = (
                ui.number("PT [%]", value=imp.pt_pct, format="%.2f", min=0)
                .classes("pole")
                .props("outlined dense step=any")
            )
            self.usd_input = (
                ui.number("PT [USD/ks]", value=imp.pt_usd, format="%.2f", min=0)
                .classes("pole")
                .props("outlined dense step=any")
            )
            self.premium_input = (
                ui.number(
                    "PT [% prémie]", value=imp.pt_premium_pct, format="%.2f", min=0
                )
                .classes("pole")
                .props("outlined dense step=any")
            )
            self.spread_input = (
                ui.number(
                    "Max. spread [%]",
                    value=vychozi(imp.max_spread_pct, self.cfg.trading.max_spread_pct),
                    format="%.2f",
                    min=0,
                )
                .classes("pole")
                .props("outlined dense step=any")
            )
            # RRR pro dopočet SL z PT u všech načtených pozic; mění se
            # zřídka, výchozí hodnota vychází z konfigurace - buď přímo
            # z importu, nebo z poměru SL:PT pro běžné zadání
            self.rrr_input = (
                ui.number(
                    "RRR (PT:SL)",
                    value=vychozi(
                        imp.rrr, rrr_z_pomeru(self.cfg.trading.sl_to_pt_ratio)
                    ),
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
            ui.label("Runner:").classes("popisek-volby-runner")
            napoveda = (
                "Výchozí nastavení runneru pro všechny načtené pozice - přepíše "
                "volbu ve sloupci Runner, kde ji lze u každé pozice doladit zvlášť. "
                + widgets.NAPOVEDA_RUNNER_VELIKOST.format(
                    procento=self.cfg.trading.runner_quantity_pct
                )
                + " Runner dostanou jen pozice s množstvím alespoň takovým, jaké je "
                "v poli vpravo; menší zůstanou na volbě Bez."
            )
            self.runner_buttons = widgets.tlacitka_runneru(
                self._nastav_runner, napoveda, self.runner_value
            )
            # Nejmenší velikost pozice, které se runner nastaví. Výchozí
            # hodnota je z konfigurace, změna přerozdělí runnery ve všech
            # řádcích podle právě spočítaného množství
            self.runner_min_input = widgets.pole_runner_min(
                imp.runner_min_quantity, "pole pole-runner-min"
            )
            self.runner_min_input.on_value_change(
                lambda _=None: self._obnov_runner_vsech()
            )

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
        Přepnutí režimu cíle: ukáže se pole odpovídající zvolenému režimu,
        kompenzace spreadu se zpřístupní jen u SL na opci a hlavičky úrovní
        dostanou novou jednotku. Načtené pozice se rovnou přepočítají,
        protože se mění význam všech úrovní.
        """
        rezim = self.rezim.value
        self.pct_input.set_visibility(rezim == REZIM_PCT)
        self.usd_input.set_visibility(rezim == REZIM_USD)
        self.premium_input.set_visibility(rezim == REZIM_PREMIUM)
        # Kompenzace spreadu patří k SL na opci - tedy k oběma opčním režimům
        self.sl_spread_compensated.set_enabled(rezim in REZIMY_NA_OPCI)
        self._popis_hlavicky()
        if not (prepocitat and self.radky):
            return

        # Úrovně z předchozího režimu mají jiný význam, proto se pole
        # vyprázdní. Zamčený řádek popisuje běžící obchod a přepsat se nedá,
        # jeho čísla ale zůstávají v jednotce původního režimu - označí se
        # tedy za neplatná a po odemčení se musí přepočítat, jinak by šla
        # do trhu jako úroveň v jiné jednotce
        # Přepočítat má smysl jen tam, kde už nějaká čísla byla. Po načtení
        # souboru se s přípravou čeká na tlačítko, takže ani přepnutí režimu
        # nesmí sáhnout do TWS samo od sebe
        pripraveno = any(radek.rezim_hodnot for radek in self.radky)
        for radek in self.radky:
            if self._zamceno(radek):
                radek.rezim_hodnot = ""
                continue
            self._vycisti_urovne(radek)
            radek.premie = None
        if pripraveno:
            self._naplanuj_pripravu()

    async def _on_upload(self, event: Any) -> None:
        """
        Zpracuje vybraný soubor: načte pozice a vykreslí tabulku.

        Nic se nepočítá - kontrakt, SL ani množství nevzniknou, dokud si
        obchodník přepočet nevyžádá tlačítkem Přepočítat. Načtení souboru
        tak nesahá do TWS a nechá čas doladit nastavení nad tabulkou.
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

        # Řádky zatím nemají čísla - ať je ve sloupci Stav vidět, na co se čeká
        for radek in self.radky:
            radek.stav(
                "Čeká na přepočet - stiskněte Přepočítat.", "stav-import-varovani"
            )

        ui.notify(
            f"Načteno {len(vysledek.positions)} pozic ze souboru {soubor.name} - "
            "zadání se připraví tlačítkem Přepočítat.",
            type="positive",
        )

    def _vykresli_tabulku(self, pozice: list[ImportedPosition]) -> None:
        """Postaví tabulku načtených pozic - hlavičku a řádek pro každou pozici."""
        self.radky = []
        self.hlavicky = {}
        self.tabulka.clear()
        self.tabulka.set_visibility(bool(pozice))
        # Probíhající dávka drží tlačítko zakázané, dokud nedoběhne
        self.zadat_button.set_enabled(bool(pozice) and not self.zadavani)
        # Nová tabulka přebíjí plán zapnutý nad předchozím souborem - jinak by
        # do trhu odešly jiné pozice, než u kterých se režim zapínal
        self._zrus_plan("Naplánované zadání zrušeno načtením jiného souboru.")
        self.plan_button.set_enabled(bool(pozice) and not self.plan_bezi)
        self.souhrn_label.set_text("")
        if not pozice:
            return

        napoveda_urovni = (
            "Úrovně se v tabulce ukazují v jednotce, se kterou počítá "
            "aplikace: v režimu na podkladu je to cena podkladu, v obou "
            "opčních režimech USD na kontrakt. Cíl zadaný v procentech "
            "prémie je do USD už přepočtený - jednotku zadání si pamatuje "
            "založený obchod, takže ji běžný formulář ukáže zase "
            "v procentech."
        )
        with self.tabulka:
            for popisek in SLOUPCE:
                label = ui.label(popisek).classes("hlavicka-import")
                # Hlavičky úrovní nesou jednotku, která se mění s režimem -
                # popisek se proto drží stranou a přepisuje se v _popis_hlavicky
                if popisek in SLOUPCE_UROVNI:
                    label.tooltip(napoveda_urovni)
                    self.hlavicky[popisek] = label

            for polozka in pozice:
                self.radky.append(self._vykresli_radek(polozka))

        self._popis_hlavicky()
        self._obnov_souhrn()

    def _popis_hlavicky(self) -> None:
        """
        Doplní do hlaviček PT a SL jednotku podle zvoleného režimu cíle -
        bez ní není v tabulce poznat, že tatáž úroveň se v běžném formuláři
        může ukázat jako procento prémie, tedy jiným číslem.
        """
        if not self.hlavicky:
            return
        jednotka = JEDNOTKY_UROVNI[uroven_cile(self.rezim.value)]
        for druh, label in self.hlavicky.items():
            label.set_text(f"{druh} [{jednotka}]")

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
        # Ručně vyplněné číslo platí v právě zvoleném režimu, takže řádek
        # zase zadatelným udělá - i tehdy, když předtím příprava selhala.
        # Obsluha běží i při programovém zápisu, proto se příznak neplatnosti
        # ve _vycisti_urovne a _zahod_dopocet nastavuje až po zápisu do polí
        for pole in (radek.pt_input, radek.sl_input, radek.qty_input):
            pole.on_value_change(lambda _=None, r=radek: self._rucni_zmena(r))
        # Množství rozhoduje, zda řádek runner dostane. Obsluha běží i při
        # programovém zápisu, takže volbu srovná i přepočet celé tabulky
        radek.qty_input.on_value_change(
            lambda _=None, r=radek: self._obnov_runner_radku(r)
        )
        # Runner se nastavuje u každé pozice zvlášť. Řádek začíná bez něj -
        # globální volbu dostane až podle spočítaného množství
        radek.runner_select = (
            ui.select(runner_volby(kratke=True), value=RUNNER_VYPNUTO)
            .classes("bunka-import pole-import vyber-runner")
            .props("outlined dense options-dense")
        )
        radek.runner_select.on_value_change(lambda _=None, r=radek: self._rucni_runner(r))

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
        return cislo_z_pole(hodnota)

    def _rucni_runner(self, radek: RadekPozice) -> None:
        """
        Obsluha comboboxu runneru v řádku: přepnutí obchodníkem si řádek
        poznamená, programový zápis z _obnov_runner_radku ne.
        """
        if not radek.runner_prepis:
            radek.runner_rucne = True

    def _rucni_zmena(self, radek: RadekPozice) -> None:
        """Čísla vyplněná v řádku platí v právě zvoleném režimu cíle."""
        radek.rezim_hodnot = self.rezim.value

    def _pripraveno(self, radek: RadekPozice) -> bool:
        """
        True, pokud čísla v řádku platí v právě zvoleném režimu cíle - tedy
        vznikla úspěšnou přípravou (nebo ruční úpravou) po poslední změně
        režimu. Jen takový řádek se smí poslat do trhu; jinak by úroveň
        z předchozího režimu odešla ve špatné jednotce.
        """
        return bool(radek.rezim_hodnot) and radek.rezim_hodnot == self.rezim.value

    def _vycisti_urovne(self, radek: RadekPozice) -> None:
        """
        Vyprázdní PT, SL i množství řádku a označí jeho čísla za neplatná.
        Volá se při změně režimu cíle, kdy úrovně z předchozí volby dostávají
        jiný význam. Zaškrtnutí zůstává - řádek se buď hned nato přepočítá,
        nebo (u dosud nepřipravených pozic) čeká na tlačítko Přepočítat.
        """
        radek.preview = None
        radek.pt_input.set_value(None)
        radek.sl_input.set_value(None)
        radek.qty_input.set_value(None)
        # Až po zápisu do polí: set_value spouští obsluhu ruční změny, která
        # by řádek zase označila za platný
        radek.rezim_hodnot = ""

    def _zahod_dopocet(self, radek: RadekPozice) -> None:
        """
        Neúspěšná příprava: zahodí náhled, dopočítaný SL i množství a označí
        čísla řádku za neplatná, takže se nedají zadat do trhu.

        Bez toho by v polích zůstala čísla z minulého, už neplatného výpočtu
        - třeba SL spočítaný k polovičnímu PT - a zaškrtnutý řádek by je
        poslal do trhu. PT se nemaže: je vidět i bez spojení s TWS a dá se
        doladit ručně, čímž se řádek zase stane zadatelným.

        Zaškrtnutí se nesundává - po obnoveném spojení stačí Přepočítat
        a výběr zůstane, jak si ho obchodník nastavil.
        """
        radek.preview = None
        radek.sl_input.set_value(None)
        radek.qty_input.set_value(None)
        # Až po zápisu do polí: set_value spouští obsluhu ruční změny, která
        # by řádek zase označila za platný
        radek.rezim_hodnot = ""

    def _zamitni_radek(self, radek: RadekPozice, text: str) -> None:
        """
        Řádek, který do trhu nesmí - propásnutý vstup podle živé ceny nebo
        svíček, či rozporný směr: zahodí dopočet, odškrtne řádek a zapíše
        chybový stav. Odškrtnutí je jediná pojistka, aby řádek neposlalo do
        trhu ani znovuotevření dialogu; _zahod_dopocet sám zaškrtnutí nechává.
        """
        self._zahod_dopocet(radek)
        radek.vybrano.set_value(False)
        radek.stav(text, "stav-import-chyba")

    def _rezimy(self) -> tuple[bool, bool]:
        """
        Režim PT a SL podle volby dialogu: True = na podkladu.
        Procento prémie je jen jiný způsob zápisu úrovně na opci, proto se
        engine v obou opčních režimech chová stejně.
        """
        na_podkladu, _ = priznaky_urovne(uroven_cile(self.rezim.value))
        return na_podkladu, na_podkladu

    def _nastav_runner(self, hodnota: str) -> None:
        """
        Zapamatuje výchozí volbu runneru a přenese ji do všech řádků, které
        lze zadat; u zamčeného obchodu s pozicí už by neměla co změnit.
        """
        self.runner_value = hodnota
        self._zvyrazni_runner()
        self._obnov_runner_vsech()

    def _runner_min(self) -> int:
        """
        Nejmenší množství, od kterého (včetně) řádek runner dostane. Prázdné nebo
        nesmyslné pole spadne zpět na hodnotu z konfigurace, ať se runner
        nerozdává podle náhodného čísla.
        """
        hodnota = self._cislo(
            self.runner_min_input.value if self.runner_min_input is not None else None
        )
        if hodnota is None or hodnota < 1:
            return self.cfg.import_.runner_min_quantity
        return int(hodnota)

    def _runner_pro_radek(self, radek: RadekPozice) -> str:
        """
        Volba runneru, která řádku podle jeho množství náleží: globální
        nastavení u pozic s množstvím alespoň rovným zadanému minimu,
        jinak "Bez". Řádek bez spočítaného množství runner nedostane.
        """
        mnozstvi = self._cislo(radek.qty_input.value) if radek.qty_input else None
        # Hranice je včetně: pozice s množstvím rovným minimu runner dostane
        if mnozstvi is None or mnozstvi < self._runner_min():
            return RUNNER_VYPNUTO
        return self.runner_value

    def _obnov_runner_radku(self, radek: RadekPozice) -> None:
        """
        Srovná volbu runneru v řádku s globálním nastavením a jeho množstvím.
        Zamčeného řádku se to netýká - jeho obchod už drží pozici a runner
        se u něj přepíná tlačítky v přehledu.
        """
        if radek.runner_select is None or self._zamceno(radek):
            return
        # Programový zápis spustí obsluhu comboboxu také - příznak ji odliší
        # od ručního přepnutí, které se přidělením volby ruší
        radek.runner_prepis = True
        try:
            radek.runner_select.set_value(self._runner_pro_radek(radek))
        finally:
            radek.runner_prepis = False
        radek.runner_rucne = False

    def _obnov_runner_vsech(self) -> None:
        """Přerozdělí runner ve všech načtených řádcích."""
        for radek in self.radky:
            self._obnov_runner_radku(radek)

    def _zvyrazni_runner(self) -> None:
        """Zvýrazní vybrané tlačítko runneru."""
        widgets.zvyrazni_tlacitka(self.runner_buttons, self.runner_value)

    def _pomer(self) -> float | None:
        """
        Poměr SL:PT pro engine, odvozený z RRR v dialogu.

        Dialog se ptá na RRR (kolikrát je PT dál než SL), engine počítá
        s obrácenou hodnotou. Prázdné i nekladné pole vrací None - engine
        pak použije hodnotu z konfigurace.
        """
        rrr = self._cislo(self.rrr_input.value)
        return pomer_z_rrr(rrr) if rrr is not None and rrr > 0 else None

    def _max_spread(self) -> float | None:
        """
        Limit spreadu z pole nad tabulkou. Prázdné pole vrací None - platí
        pak hodnota z konfigurace, stejně jako u zadání obchodu.
        """
        return self._cislo(self.spread_input.value)

    def _sl_spread(self) -> bool:
        """Kompenzace SL o spread - uplatní se jen při SL zadaném na opci."""
        return bool(self.sl_spread_compensated.value) and self.rezim.value in REZIMY_NA_OPCI

    def _refresh_after_open_sec(self) -> float | None:
        """
        Prodleva přepočtu po otevření burzy pro zakládané obchody; None
        znamená přepočet nepoužít. Bez vykreslených prvků platí konfigurace.
        """
        imp = self.cfg.import_
        return sekundy_prepoctu(
            imp.refresh_after_open if self.refresh_checkbox is None else bool(self.refresh_checkbox.value),
            self.refresh_sec_input.value if self.refresh_sec_input is not None else None,
            0,
            imp.refresh_after_open_sec,
        )

    def _refresh_interval_sec(self) -> float | None:
        """
        Odstup průběžného přepočtu pro zakládané obchody; None znamená
        nepřepočítávat. Bez vykreslených prvků platí konfigurace.
        """
        imp = self.cfg.import_
        return sekundy_prepoctu(
            imp.refresh_interval if self.interval_checkbox is None else bool(self.interval_checkbox.value),
            self.interval_sec_input.value if self.interval_sec_input is not None else None,
            1,
            imp.refresh_interval_sec,
        )

    def _runner_pro_zadani(self, radek: RadekPozice) -> tuple[float, int | None]:
        """
        Automatická volba runneru, kterou si obchod z řádku odnese: násobek
        cíle (0 = runner nepoužít) a minimum množství, od kterého runner
        náleží. Podle nich engine runner nastaví při založení i po každém
        přepočtu množství.

        Řádek s volbou přidělenou dialogem dostává globální nastavení - i řádek
        na "Bez" jen kvůli malému množství, aby runner dostal, až na něj
        přepočtem doroste. Ručně přepnutý řádek si nese svou volbu bez minima.
        """
        if radek.runner_rucne and radek.runner_select is not None:
            return runner_nasobek(radek.runner_select.value), None
        return runner_nasobek(self.runner_value), self._runner_min()

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

        Ruční přepočet ukončuje naplánované zadání - obchodník právě vzal
        čísla do vlastních rukou. Přepočet spuštěný samotným plánem se
        nevypíná, ten už běží.
        """
        self._zrus_plan("Naplánované zadání zrušeno ručním přepočtem.")
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
        self.priprava = True
        try:
            for poradi, radek in enumerate(cekajici, 1):
                self._set_loading(
                    True, f"Připravuji {radek.pozice.symbol} ({poradi}/{celkem})…"
                )
                await self._priprav_radek(radek)
        finally:
            self.priprava = False
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
        self.priprava = True
        try:
            await self._priprav_radek(radek, zachovat_pt=True)
        finally:
            self.priprava = False
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
                None,
                self._max_spread(),
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
                self._zahod_dopocet(radek)
                radek.stav("Vyplňte PT", "stav-import-chyba")
                return
            # Ručně přepsané PT už z prémie nevychází, poznámka o ní by mátla
            radek.premie = None
        else:
            pt = self._cil_pro(radek.pozice)
            # V režimu procenta z prémie se PT dopočítá až z ceny opce (níže),
            # jinak prázdná hodnota znamená nevyplněné zadání
            if pt is None and self.rezim.value != REZIM_PREMIUM:
                self._zahod_dopocet(radek)
                return

        # Známé PT patří do pole hned - nezávisí na kotacích, tak ať je vidět
        # i bez spojení s TWS
        if pt is not None:
            radek.pt_input.set_value(round(pt, 2))
        if not self.ib.connected:
            self._zahod_dopocet(radek)
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
                self._zahod_dopocet(radek)
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
                self._max_spread(),
            )
        except Exception as exc:
            self._zahod_dopocet(radek)
            radek.kontrakt_label.set_text("-")
            radek.stav(f"Chyba přípravy: {exc}", "stav-import-chyba")
            return

        radek.preview = preview

        if not preview.expiration:
            self._zahod_dopocet(radek)
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

        # Skutečný směr určuje aplikace z aktuální ceny podkladu, a není-li
        # známa, z polohy zadaných úrovní. Liší-li se od směru daného souborem,
        # buď trh vstupní úroveň už překonal, nebo si řádek v souboru odporuje;
        # tak či tak se řádek odškrtne, aby se omylem nezaložil obchod na
        # opačnou stranu
        if preview.right != radek.pozice.right:
            # Dopočet patří k opačnému kontraktu, než jaký by obchod koupil
            if preview.current_price is not None:
                self._zamitni_radek(
                    radek,
                    f"Vstup propásnut - podklad je na {fmt(preview.current_price)}, "
                    f"z ceny vychází {preview.right_label} místo "
                    f"{radek.pozice.right_label}.",
                )
            else:
                self._zamitni_radek(
                    radek,
                    f"Cena podkladu není známa a ze zadaných úrovní vychází "
                    f"{preview.right_label} místo {radek.pozice.right_label} - "
                    f"zkontrolujte řádek v souboru.",
                )
            return

        # Kontrola propásnutého vstupu podle minutových svíček
        # (import.entry_cross_check): podklad se sice může vrátit na správnou
        # stranu vstupu, takže živá cena nic neodhalí, přes úroveň ale už
        # jednou prošel. Řádek dopadne stejně jako při vstupu překonaném
        # podle živé ceny výše. Selhání dotazu přípravu nezneplatní, jen se
        # připíše do stavu
        varovani_svicek = ""
        od_kdy = self.engine.entry_cross_start()
        if od_kdy is not None:
            try:
                propasnuti = await self.engine.entry_crossed(
                    preview.underlying, preview.right, radek.pozice.entry_price, od_kdy
                )
            except Exception as exc:
                varovani_svicek = f" Kontrola svíček se nezdařila: {exc}"
            else:
                if propasnuti is not None:
                    self._zamitni_radek(radek, f"Vstup propásnut - {propasnuti}.")
                    return

        # Čísla v řádku od téhle chvíle platí ve zvoleném režimu, takže se
        # smí zadat do trhu. Nastavuje se výslovně: set_value obsluhu ruční
        # změny nespustí, když se hodnota oproti minulé přípravě nezměnila
        radek.rezim_hodnot = self.rezim.value

        # U PT odvozeného z prémie se uvede, z jaké ceny opce se počítalo
        zaklad = (
            f" · prémie ≈ {fmt(radek.premie * 100)} USD" if radek.premie else ""
        )
        if preview.warnings or varovani_svicek:
            radek.stav(
                f"Připraveno{zaklad} s výhradami: "
                + " ".join(preview.warnings)
                + varovani_svicek,
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
        # Chtěný, ale nezapnutý runner (málo kontraktů) není chyba - obchod
        # ho dostane, až na něj přepočtem doroste; důvod zná engine
        elif flow.runner_skip_reason:
            popis_runneru = f", runner nezapnut: {flow.runner_skip_reason}"
        # Přepočet po otevření burzy: čekající obchod na něj ještě čeká,
        # nebo už proběhl; obchod bez volby ani po nákupu nic nehlásí
        popis_prepoctu = ""
        if flow.refresh_after_open_sec is not None:
            if flow.refresh_after_open_done:
                popis_prepoctu = ", přepočteno po otevření"
            elif flow.state.is_before_entry:
                popis_prepoctu = f", přepočet {flow.refresh_after_open_sec:g} s po otevření"
        # Průběžný přepočet běží jen před nákupem
        if flow.refresh_interval_sec and flow.state.is_before_entry:
            popis_prepoctu += f", přepočet každých {flow.refresh_interval_sec:g} s"
        text = f"Zadáno {flow.id} – {flow.state.label}{popis_runneru}{popis_prepoctu}"
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
        Zaškrtne všechny řádky, které lze zadat. Odškrtnutý zůstává zamčený
        řádek (obchod už drží pozici) i řádek, jehož čísla neplatí v právě
        zvoleném režimu - ten by šel do trhu s úrovní ve špatné jednotce,
        nebo s hodnotami z výpočtu, který selhal.
        """
        if not self.radky:
            return
        for radek in self.radky:
            radek.vybrano.set_value(
                not self._zamceno(radek) and self._pripraveno(radek)
            )
        self._obnov_souhrn()

    def refresh(self) -> None:
        """
        Udrží otevřený dialog v souladu se skutečností - stav založených
        obchodů, zámky řádků i souhrn pod tabulkou. Volá se z periodické
        smyčky rozhraní; zavřený dialog se přeskakuje.

        Naplánované zadání je z toho vyjmuté - běží i se zavřeným dialogem,
        protože obchodník ho zapne a dialog odklidí.
        """
        self._tik_planu()
        self._obnov_varovani_spojeni()
        if not self.dialog.value or not self.radky:
            return

        for radek in self.radky:
            if radek.flow_id and radek.stav_z_obchodu:
                self._zapis_stav_obchodu(radek)
        self._obnov_zamky()
        self._obnov_souhrn()

    def _k_zadani(self) -> list[RadekPozice]:
        """
        Řádky, které jdou do trhu - vybrané a nezamčené. Z téhož výběru
        počítá souhrn, popis plánu i varování, aby se shodovaly s dávkou,
        kterou zadání skutečně pošle.
        """
        return [
            radek
            for radek in self.radky
            if radek.vybrano.value and not self._zamceno(radek)
        ]

    def _obnov_souhrn(self) -> None:
        """Souhrn pod tabulkou - kolik pozic a kontraktů se chystá do trhu."""
        vybrane = self._k_zadani()
        kontrakty = sum(int(self._cislo(radek.qty_input.value) or 0) for radek in vybrane)
        self.souhrn_label.set_text(
            f"K zadání {len(vybrane)} pozic, celkem {kontrakty} kontraktů | "
            f"risk na obchod {fmt(self.engine.risk_amount)} USD "
            f"z účtu {fmt(self.engine.account_size)} USD"
        )

    # ------------------------------------------------------------------
    # Naplánované zadání po otevření trhu
    # ------------------------------------------------------------------

    def _prodleva_planu(self) -> float:
        """
        Prodleva od otevření trhu, po které se naplánované zadání provede.

        Sdílí se s polem u přepočtu po otevření, ale čte se nezávisle na jeho
        přepínači - plán je samostatná funkce a nesmí ho vypnout odškrtnutý
        přepočet. Prázdné či záporné pole spadne na hodnotu z konfigurace,
        ať se okamžik zadání neřídí náhodným číslem.
        """
        hodnota = self._cislo(
            self.refresh_sec_input.value if self.refresh_sec_input is not None else None
        )
        if hodnota is None or hodnota < 0:
            return float(self.cfg.import_.refresh_after_open_sec)
        return float(hodnota)

    def _plan_zbyva(self) -> float | None:
        """
        Sekundy do spuštění naplánovaného zadání; None, když plán neběží.

        Okamžik je otevření trhu plus prodleva. Mimo obchodní hodiny se
        skládá z odpočtu do otevření, uvnitř seance ze zbytku prodlevy -
        záporná hodnota tedy znamená, že okamžik už minul.
        """
        if not self.plan_aktivni:
            return None
        do_otevreni = self.engine.market_open_seconds()
        if do_otevreni is not None:
            return do_otevreni + self.plan_prodleva
        uplynulo = self.engine.market_open_elapsed()
        return self.plan_prodleva - (uplynulo if uplynulo is not None else 0.0)

    def plan_popis(self) -> str | None:
        """
        Popis naplánovaného zadání pro hlavičku stránky; None, když plán
        neběží. Zapnutý režim je tak vidět i se zavřeným dialogem, odkud by
        o něm jinak nebylo ani stopy.
        """
        if self.plan_bezi:
            return "Probíhá naplánované zadání pozic"
        zbyva = self._plan_zbyva()
        if zbyva is None:
            return None
        return f"Naplánováno zadání {self._davka_planu(zbyva)}"

    def _davka_planu(self, zbyva: float) -> str:
        """Počet pozic plánu a odpočet do spuštění - pro popis i varování."""
        return f"{len(self._k_zadani())} pozic za {format_countdown(zbyva)}"

    def plan_bez_spojeni(self) -> bool:
        """
        Naplánované zadání čeká, ale TWS není připojen.

        V takovém stavu by plán po otevření trhu nedopočítal SL ani množství
        a žádnou pozici by do trhu nezadal - chyba by se ukázala až ve
        stavech řádků, kdy už je pozdě. Hlavička stránky i dialog to proto
        hlásí výrazně, dokud se spojení nenaváže.
        """
        return self.plan_aktivni and not self.ib.connected

    def varovani_spojeni(self) -> str | None:
        """
        Text varování na chybějící spojení s TWS; None, když spojení stojí.

        Se zapnutým plánem říká, že zadání neproběhne, a nese i počet pozic
        a odpočet - hlavička pak skrývá popisek plánu, aby se její řádek
        nepřeplnil. Bez plánu jen připomene, co bez spojení nejde. Rada na
        konci se řídí tím, zda se aplikace po spuštění TWS připojí sama -
        po ručním odpojení to neudělá.
        """
        if self.ib.connected:
            return None
        if self.engine.reconnects_automatically:
            rada = "Spusťte Trader Workstation, aplikace se k němu připojí sama."
        else:
            rada = "Spusťte Trader Workstation a v hlavičce klikněte na Připojit."
        if self.plan_aktivni:
            davka = self._davka_planu(self._plan_zbyva() or 0.0)
            return (
                f"POZOR: TWS není připojen - naplánované zadání {davka} "
                f"bez spojení neproběhne. {rada}"
            )
        return (
            "TWS není připojen - bez něj se nedopočítá SL ani množství "
            f"a do trhu nepůjde žádná pozice. {rada}"
        )

    def _obnov_varovani_spojeni(self) -> None:
        """
        Sladí varování nad tlačítky se stavem spojení a plánu. Se zapnutým
        plánem dostane naléhavý vzhled, protože zadání samo proběhne jen
        tehdy, když spojení do okamžiku spuštění naskočí.
        """
        if self.spojeni_label is None:
            return
        text = self.varovani_spojeni()
        self.spojeni_label.set_visibility(text is not None)
        if text is None:
            return
        self.spojeni_label.set_text(text)
        # Se zapnutým plánem má varování stejný vzhled jako poplach
        # v hlavičce stránky, bez plánu je to klidný rámeček
        stav = "poplach" if self.plan_aktivni else "varovani-spojeni-klid"
        self.spojeni_label.classes(replace=f"varovani-spojeni {stav}")

    def _obnov_plan(self) -> None:
        """
        Sladí tlačítko se stavem plánu. Vypnutý je obrysový, zapnutý plný
        oranžový s odpočtem do spuštění - zapnutý režim tak jde poznat na
        první pohled, stejně jako u zvolené volby runneru.
        """
        if self.plan_button is None:
            return
        if self.plan_bezi:
            self.plan_button.set_text("Zadávám naplánované pozice…")
            self.plan_button.props(add="color=orange-8", remove="outline")
            self.plan_button.classes(add="plan-aktivni")
            return
        zbyva = self._plan_zbyva()
        if zbyva is None:
            self.plan_button.set_text(POPIS_PLANU_VYPNUTO)
            self.plan_button.props(add="outline color=orange-8")
            self.plan_button.classes(remove="plan-aktivni")
            return
        self.plan_button.set_text(f"Zrušit plán ({format_countdown(zbyva)})")
        self.plan_button.props(add="color=orange-8", remove="outline")
        self.plan_button.classes(add="plan-aktivni")

    def _zrus_plan(self, duvod: str = "") -> None:
        """
        Ukončí naplánované zadání. Volá se ze všech ručních zásahů, které
        plán přebíjejí - Přepočítat, Zadat vybrané pozice do trhu, Zavřít
        i načtení jiného souboru.

        Právě probíhající plán se neruší: přepočet a zadání už běží a vzít
        se zpět nedají. Ticho při vypnutém plánu nechá volajícího zavolat
        rušení bez ptaní, jestli je co rušit.
        """
        if not self.plan_aktivni or self.plan_bezi:
            return
        self.plan_aktivni = False
        self._obnov_plan()
        if duvod:
            ui.notify(duvod, type="info")

    def _prepni_plan(self) -> None:
        """
        Obsluha tlačítka naplánovaného zadání - zapne, nebo vypne režim.

        Zapnout jde jen tehdy, když okamžik spuštění teprve nastane. Po
        otevření trhu s uplynulou prodlevou by plán zadal do trhu hned po
        stisku, což od tlačítka se slovem "po otevření" nikdo nečeká -
        v takové chvíli má obchodník po ruce Přepočítat a Zadat vybrané
        pozice do trhu a udělá totéž vědomě.
        """
        if self.plan_bezi:
            ui.notify("Naplánované zadání právě probíhá.", type="warning")
            return
        if self.plan_aktivni:
            self._zrus_plan("Naplánované zadání zrušeno.")
            return
        if not self.radky:
            ui.notify("Nejprve vyberte soubor s pozicemi.", type="warning")
            return
        # Bez vyplněného cíle by naplánovaný přepočet stejně jen ohlásil
        # chybu a dávka by šla do trhu s prázdnými čísly
        if self._zadana_hodnota() is None:
            ui.notify(f"Vyplňte {self._popis_hodnoty()}.", type="warning")
            return
        # Plán zadá právě to, co je zaškrtnuté teď - výběr se do spuštění
        # nemění. Prázdný by tiše skončil zadáním nula pozic, a to až po
        # otevření trhu, kdy už je na nápravu pozdě: znovu otevřený dialog
        # zaškrtnutí nepřepočtených řádků sundává
        if not self._k_zadani():
            ui.notify(
                "Není vybrána žádná pozice - plán by do trhu nezadal nic.",
                type="warning",
            )
            return

        prodleva = self._prodleva_planu()
        uplynulo = self.engine.market_open_elapsed()
        if uplynulo is not None and uplynulo >= prodleva:
            ui.notify(
                f"Okamžik přepočtu ({prodleva:g} s po otevření trhu) je pryč - "
                f"trh je otevřený už {format_countdown(uplynulo)}. Použijte "
                f"Přepočítat a Zadat vybrané pozice do trhu.",
                type="warning",
            )
            return

        self.plan_prodleva = prodleva
        self.plan_aktivni = True
        self._obnov_plan()
        # Bez spojení s TWS plán zapnout jde - TWS se dá do otevření trhu
        # ještě spustit. Místo potvrzení, že zadání proběhne, se ale ukáže
        # varování, že bez spojení neproběhne. Hláška sama nezmizí a čeká
        # na potvrzení (timeout 0); potvrzovací tlačítko je akce Quasaru,
        # ne close_button - jen u akce jde nastavit bílá barva, výchozí
        # modrá na červeném pozadí špatně čte
        if not self.ib.connected:
            ui.notify(
                self.varovani_spojeni(),
                type="negative",
                multi_line=True,
                timeout=0,
                actions=[{"label": "Rozumím", "color": "white"}],
            )
            return
        zbyva = self._plan_zbyva() or 0.0
        ui.notify(
            f"Přepočet a zadání proběhne za {format_countdown(zbyva)} "
            f"({prodleva:g} s po otevření trhu).",
            type="positive",
        )

    def _tik_planu(self) -> None:
        """
        Průchod plánem z periodické obnovy rozhraní - drží odpočet na
        tlačítku a po dosažení okamžiku spustí přepočet se zadáním.

        Běží i se zavřeným dialogem: obchodník plán zapne a dialog odklidí,
        spouštěč musí přesto nastat. Vlastní běh se pouští odloženě přes
        časovač, aby v něm fungovalo ui.notify - stejně jako u přípravy
        vyvolané přepnutím režimu.
        """
        if not self.plan_aktivni or self.plan_bezi:
            return
        zbyva = self._plan_zbyva()
        if zbyva is None:
            return
        if zbyva > 0:
            self._obnov_plan()
            return

        # Okamžik nastal - zapnutý režim se hned překlápí do běhu, ať ho
        # další průchod smyčkou nespustí podruhé
        self.plan_aktivni = False
        self.plan_bezi = True
        self._obnov_plan()
        with self.tabulka:
            ui.timer(0, self._spust_plan, once=True)

    async def _spust_plan(self) -> None:
        """
        Vlastní naplánovaný běh: přepočet všech nezadaných řádků podle
        nastavení nad tabulkou a hned po něm zadání vybraných pozic do trhu.

        Je to táž dvojice kroků jako ruční Přepočítat a Zadat vybrané pozice
        do trhu, včetně jejich kontrol - obchod tedy vznikne přesně jako
        z ruky. Selhání se ohlásí a režim se v každém případě vypne, aby
        se dávka po chybě neopakovala.
        """
        try:
            await self._priprav_vse()
            await self._zadej()
        except Exception as exc:
            log.exception("Naplánované zadání selhalo.")
            ui.notify(f"Naplánované zadání selhalo: {exc}", type="negative")
        finally:
            self.plan_bezi = False
            self._obnov_plan()

    def _na_zavreni_dialogu(self, event: Any) -> None:
        """
        Zavření dialogu ukončí naplánované zadání - odklizený dialog nemá
        nechávat běžet režim, který sám od sebe pošle pozice do trhu.

        Visí na změně hodnoty dialogu, takže platí pro všechny cesty ven:
        tlačítko Zavřít, klávesu i kliknutí mimo kartu. Otevření dialogu
        (hodnota True) se přeskakuje, stejně jako zavření po doběhlé dávce,
        kdy plán běží a rušit se nedá.
        """
        if event.value:
            return
        self._zrus_plan("Naplánované zadání zrušeno zavřením dialogu.")

    # ------------------------------------------------------------------
    # Zadání do trhu
    # ------------------------------------------------------------------

    async def _zadej(self) -> None:
        """
        Obsluha tlačítka „Zadat vybrané pozice do trhu".

        Dávka smí běžet jen jedna a až po dokončené přípravě: druhý stisk
        tlačítka by pracoval s výběrem pořízeným ještě před odškrtnutím
        řádků, takže by tytéž pozice poslal do trhu podruhé. Tlačítko se
        proto na dobu běhu zakáže.

        Ruční zadání ukončuje naplánované zadání - pozice jdou do trhu teď
        a plán by je po otevření hnal podruhé. Dávka spuštěná samotným
        plánem se nevypíná, ta je jeho druhým krokem.
        """
        self._zrus_plan("Naplánované zadání zrušeno ručním zadáním do trhu.")
        if self.zadavani:
            ui.notify("Zadávání do trhu už probíhá.", type="warning")
            return
        if self.priprava:
            ui.notify(
                "Počkejte na dokončení přípravy - do trhu by šla "
                "rozpracovaná čísla.",
                type="warning",
            )
            return

        self.zadavani = True
        self.zadat_button.set_enabled(False)
        try:
            await self._zadej_davku()
        finally:
            self.zadavani = False
            self.zadat_button.set_enabled(bool(self.radky))

    async def _zadej_davku(self) -> None:
        """
        Vlastní dávka: pro každý vybraný řádek se založí obchod stejným
        voláním jako z běžného formuláře. Chyba jedné pozice ostatní
        nezastaví, zapíše se do jejího stavu; už založený řádek se podruhé
        nezadává.
        """
        vybrane = self._k_zadani()
        if not vybrane:
            ui.notify("Není vybrána žádná pozice k zadání.", type="warning")
            return

        # Všechna společná nastavení se čtou jednou pro celou dávku - ovládací
        # prvky zůstávají během zadávání živé a jejich změna uprostřed by
        # rozešla jednotku úrovní s příznaky, které už jsou zafixované
        max_spread = self._max_spread()
        sl_spread = self._sl_spread()
        pomer = self._pomer()
        # Naplánované zadání dávku přepočítalo až po otevření a prodlevě;
        # druhý přepočet v enginu by přepsal čísla dialogu i výběr runneru
        prepocet_sec = None if self.plan_bezi else self._refresh_after_open_sec()
        # Průběžný přepočet platí i pro naplánované zadání - běží až od
        # dalšího odstupu po založení, čísla dialogu tedy nepřepisuje hned
        interval_sec = self._refresh_interval_sec()
        # Od kdy engine při zadání prochází svíčky podkladu, zda už
        # nepřekročil vstup (import.entry_cross_check); None = nekontrolovat
        svicky_od = self.engine.entry_cross_start()
        rezim_cile = self.rezim.value
        rezim_urovni = uroven_cile(rezim_cile)

        zalozeno = 0
        chyb = 0
        odmitnuto = 0
        celkem = len(vybrane)
        self._set_loading(True, "Zadávám obchody do trhu…")
        try:
            for poradi, radek in enumerate(vybrane, 1):
                self._set_loading(
                    True, f"Zadávám {radek.pozice.symbol} ({poradi}/{celkem})…"
                )
                # Čísla spočítaná v jiném režimu, nebo zbylá po neúspěšné
                # přípravě, by odešla do trhu ve špatné jednotce - takový
                # řádek se přeskočí, dokud ho obchodník nepřepočítá
                if radek.rezim_hodnot != rezim_cile:
                    radek.stav(
                        "Hodnoty neplatí ve zvoleném režimu cíle - "
                        "přepočítejte řádek.",
                        "stav-import-chyba",
                    )
                    chyb += 1
                    continue

                pt = self._cislo(radek.pt_input.value)
                sl = self._cislo(radek.sl_input.value)
                qty = self._cislo(radek.qty_input.value)
                if pt is None and sl is None:
                    radek.stav("Vyplňte PT nebo SL", "stav-import-chyba")
                    chyb += 1
                    continue

                nasobek_runneru, minimum_runneru = self._runner_pro_zadani(radek)
                request = FlowRequest(
                    symbol=radek.pozice.symbol,
                    entry_price=radek.pozice.entry_price,
                    profit_target=pt,
                    stop_loss=sl,
                    quantity=int(qty) if qty else None,
                    max_spread_pct=max_spread,
                    sl_spread_compensated=sl_spread,
                    sl_to_pt_ratio=pomer,
                    # Přepočet po otevření burzy i průběžný platí pro celou dávku
                    refresh_after_open_sec=prepocet_sec,
                    refresh_interval_sec=interval_sec,
                    # Runner zapíná engine sám - při založení a znovu po každém
                    # přepočtu množství, podle volby a minima z dialogu
                    runner_multiple=nasobek_runneru,
                    runner_min_quantity=minimum_runneru,
                    entry_cross_since=svicky_od,
                    # Jediná volba cíle určuje režim PT i SL a na příznaky
                    # zadání se rozbaluje jedním voláním, takže se úrovně
                    # nemohou rozejít. Procenta prémie se přenášejí jen
                    # s cenou opce, ze které vyšla - ručně přepsané PT ji
                    # zahazuje, takže takový řádek jde do trhu v USD/ks
                    **urovne_z_rezimu(rezim_urovni, radek.premie),
                    # Zadanou úrovní je v dialogu cíl, SL se dopočítá podle
                    # RRR. U řádku s prázdným PT je to naopak - prvotní je
                    # SL, ať si obchod nepamatuje úroveň, kterou nikdo nezadal
                    primary_level="pt" if pt is not None else "sl",
                    # Směr má soubor (cíl pod vstupem = short) a v obou
                    # režimech na opci je to jediný jeho zdroj - engine by
                    # jinak typ opce určil z okamžité polohy ceny podkladu.
                    # Mezi přípravou řádku a stiskem tlačítka se cena může
                    # přehoupnout přes vstup a ze short zadání by se stal
                    # long CALL, který by k tomu nahradil čekající long
                    # obchod téhož tickeru. Se směrem engine takové zadání
                    # odmítne jako propásnutý vstup
                    intended_right=radek.pozice.right,
                )
                try:
                    flow = await self.engine.start_flow(request)
                except EntryMissedError as exc:
                    # Vstup překonaný mezi přípravou a zadáním dopadne stejně
                    # jako při přípravě - řádek se odškrtne a dopočet zahodí
                    self._zamitni_radek(radek, f"Vstup propásnut - {exc.reason}.")
                    chyb += 1
                    continue
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

                # Obchod, který se do trhu nedostal, vzniká rovnou ukončený -
                # odmítlo ho rušicí či uzavírací okno. Do počtu založených nepatří
                if not flow.state.is_active:
                    odmitnuto += 1
                    self._zapis_stav_obchodu(radek)
                    continue

                self._zapis_stav_obchodu(radek)
                zalozeno += 1
        finally:
            self._set_loading(False)

        self._obnov_zamky()
        self._obnov_souhrn()
        if self.on_created:
            self.on_created()

        duvody = []
        if chyb:
            duvody.append(f"{chyb} se nezdařilo")
        if odmitnuto:
            duvody.append(f"{odmitnuto} nebylo zadáno do trhu")
        if duvody:
            ui.notify(
                f"Založeno {zalozeno} obchodů, {' a '.join(duvody)} - "
                f"podrobnosti jsou ve sloupci Stav.",
                type="warning",
            )
        else:
            ui.notify(f"Založeno {zalozeno} obchodů ze souboru.", type="positive")

        # Dialog zůstává otevřený jen kvůli chybám řádků - ty jsou vidět
        # pouze v jeho sloupci Stav a obchodník je má opravit. Obchod, který
        # vznikl rovnou ukončený (rušicí okno), je v monitoringu i s důvodem,
        # takže dialog nemá proč překážet
        if not chyb:
            self.dialog.close()
