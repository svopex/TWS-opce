"""Webové rozhraní aplikace postavené na NiceGUI - formulář a monitorovací tabulka."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from nicegui import app, ui

from . import calc
from .config import AppConfig
from .engine import FlowEngine, Preview
from .ib_service import IBService
from .import_dialog import ImportDialog
from .models import (
    MODE_PREMIUM,
    MODE_UNDERLYING,
    MODE_USD,
    MODES_ON_OPTION,
    PT_MULTIPLES,
    Flow,
    FlowRequest,
    FlowState,
    format_countdown,
    level_text,
    pomer_z_rrr,
    rezim_urovne,
    rrr_z_pomeru,
)
from .report_dialog import ReportDialog

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Definice sloupců monitorovací tabulky
TABLE_COLUMNS = [
    {"name": "live", "label": "", "field": "live", "align": "center"},
    {"name": "symbol", "label": "Ticker", "field": "symbol", "align": "left"},
    {"name": "contract", "label": "Kontrakt", "field": "contract", "align": "left"},
    {"name": "qty", "label": "Ks", "field": "qty", "align": "right"},
    {"name": "underlying", "label": "Podklad", "field": "underlying", "align": "right"},
    {"name": "entry", "label": "Vstup", "field": "entry", "align": "right"},
    {"name": "pt", "label": "PT", "field": "pt", "align": "right"},
    {"name": "sl", "label": "SL", "field": "sl", "align": "right"},
    {"name": "exp_profit", "label": "Zisk na PT", "field": "exp_profit", "align": "right"},
    {"name": "exp_loss", "label": "Ztráta na SL", "field": "exp_loss", "align": "right"},
    {"name": "fill", "label": "Nákup za", "field": "fill", "align": "right"},
    {"name": "quote", "label": "Bid / Ask", "field": "quote", "align": "right"},
    {"name": "spread", "label": "Spread", "field": "spread", "align": "right"},
    {"name": "spread_limit", "label": "Max. spread", "field": "spread_limit", "align": "right"},
    {"name": "pnl", "label": "P/L", "field": "pnl", "align": "right"},
    {"name": "state", "label": "Stav", "field": "state", "align": "left"},
]

# Šablona řádku tabulky. Vykresluje se ručně, protože pod každý obchod patří
# druhý řádek s tlačítky; Quasar při vlastním vykreslení řádku přestává hlásit
# události, proto se emitují přímo ze šablony.
BODY_SLOT = """
  <q-tr :props="props" class="radek-s-nasobky"
        @click="() => $parent.$emit('vybratFlow', {id: props.row.id})">
    <q-td v-for="col in props.cols" :key="col.name" :props="props">
      <span v-if="col.name === 'live'">
        <span v-if="props.row.live" class="puntik-hlidani"></span>
      </span>
      <span v-else-if="col.name === 'state'" :class="'odznak ' + props.row.state_class">
        {{ col.value }}
      </span>
      <span v-else-if="col.name === 'pnl'" :class="props.row.pnl_class">{{ col.value }}</span>
      <span v-else-if="col.name === 'exp_profit'" class="zisk">{{ col.value }}</span>
      <span v-else-if="col.name === 'exp_loss'" class="ztrata">{{ col.value }}</span>
      <span v-else-if="col.name === 'contract'">
        {{ col.value }}
        <span :class="'odznak-smer ' + props.row.smer_class">{{ props.row.smer }}</span>
      </span>
      <span v-else>{{ col.value }}</span>
    </q-td>
  </q-tr>
  <q-tr :props="props" class="radek-nasobky"
        @click="() => $parent.$emit('vybratFlow', {id: props.row.id})">
    <q-td :colspan="props.cols.length - 1" class="bunka-nasobku">
      <template v-if="props.row.sl_mozny">
        <span class="popisek-nasobky">SL:</span>
        <q-btn dense size="sm" class="q-ml-xs tlacitko-nasobek"
               :outline="props.row.aktivni_sl !== 'puvodni'"
               :color="props.row.aktivni_sl === 'puvodni' ? 'primary' : 'grey-7'"
               label="Počáteční SL"
               @click.stop="() => $parent.$emit('nastavSl', {id: props.row.id, rezim: 'puvodni'})" />
        <q-btn dense size="sm" class="q-ml-xs tlacitko-nasobek"
               :outline="props.row.aktivni_sl !== 'be'"
               :color="props.row.aktivni_sl === 'be' ? 'primary' : 'grey-7'"
               label="SL BE"
               @click.stop="() => $parent.$emit('nastavSl', {id: props.row.id, rezim: 'be'})" />
      </template>
      <template v-if="props.row.cil_mozny">
        <span class="popisek-nasobky" :class="props.row.sl_mozny ? 'popisek-oddeleny' : ''">Cíl:</span>
        <q-btn v-for="n in props.row.nasobky" :key="n" dense size="sm"
               class="q-ml-xs tlacitko-nasobek"
               :outline="props.row.aktivni_nasobek !== n"
               :color="props.row.aktivni_nasobek === n ? 'primary' : 'grey-7'"
               :label="String(n).replace('.', ',') + '×'"
               @click.stop="() => $parent.$emit('nasobek', {id: props.row.id, nasobek: n})" />
      </template>
      <q-btn v-if="props.row.lze_uzavrit" dense size="sm" outline color="red-8"
             class="q-ml-sm tlacitko-nasobek"
             label="Uzavřít pozici"
             @click.stop="() => $parent.$emit('uzavritPozici', {id: props.row.id})" />
      <template v-if="props.row.runner_mozny">
        <span class="popisek-nasobky popisek-runner">Runner:</span>
        <template v-if="props.row.runner_sl_mozny">
          <q-btn dense size="sm" class="q-ml-xs tlacitko-nasobek"
                 :outline="props.row.aktivni_runner_sl !== 'puvodni'"
                 :color="props.row.aktivni_runner_sl === 'puvodni' ? 'orange-8' : 'grey-7'"
                 label="Počáteční SL"
                 @click.stop="() => $parent.$emit('nastavRunnerSl', {id: props.row.id, rezim: 'puvodni'})" />
          <q-btn dense size="sm" class="q-ml-xs tlacitko-nasobek"
                 :outline="props.row.aktivni_runner_sl !== 'be'"
                 :color="props.row.aktivni_runner_sl === 'be' ? 'orange-8' : 'grey-7'"
                 label="SL BE"
                 @click.stop="() => $parent.$emit('nastavRunnerSl', {id: props.row.id, rezim: 'be'})" />
          <span class="popisek-nasobky popisek-oddeleny">Cíl:</span>
        </template>
        <q-btn v-for="n in props.row.nasobky" :key="'r' + n" dense size="sm"
               class="q-ml-xs tlacitko-nasobek"
               :outline="props.row.aktivni_runner_nasobek !== n"
               :color="props.row.aktivni_runner_nasobek === n ? 'orange-8' : 'grey-7'"
               :label="String(n).replace('.', ',') + '×'"
               @click.stop="() => $parent.$emit('runnerNasobek', {id: props.row.id, nasobek: n})" />
        <q-btn v-if="props.row.runner_lze_zrusit" dense size="sm" outline color="red-8"
               class="q-ml-sm tlacitko-nasobek"
               label="Zrušit runner"
               @click.stop="() => $parent.$emit('runnerZrusit', {id: props.row.id})" />
        <q-btn v-if="props.row.lze_uzavrit_runner" dense size="sm" outline color="red-8"
               class="q-ml-xs tlacitko-nasobek"
               label="Uzavřít runner"
               @click.stop="() => $parent.$emit('uzavritRunner', {id: props.row.id})" />
      </template>
    </q-td>
    <q-td class="bunka-akce">
      <q-btn v-if="props.row.lze_zrusit" dense size="sm" outline color="red-8"
             class="tlacitko-nasobek"
             label="Zrušit"
             @click.stop="() => $parent.$emit('zrusitFlow', {id: props.row.id})" />
      <q-btn v-if="props.row.lze_odstranit" dense size="sm" outline color="grey-7"
             class="tlacitko-nasobek"
             label="Odstranit z přehledu"
             @click.stop="() => $parent.$emit('odstranitFlow', {id: props.row.id})" />
    </q-td>
  </q-tr>
"""


def staticky_soubor(nazev: str) -> str:
    """
    URL statického souboru doplněná o čas jeho poslední úpravy.
    Prohlížeč tak po změně načte novou verzi místo té z keše.
    """
    soubor = STATIC_DIR / nazev
    stamp = int(soubor.stat().st_mtime) if soubor.exists() else 0
    return f"/static/{nazev}?v={stamp}"


def fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    """Naformátuje číslo pro tabulku, při chybějící hodnotě vrátí pomlčku."""
    if value is None:
        return "-"
    return f"{value:,.{digits}f}{suffix}".replace(",", " ")


def pnl_text(cisty: float | None, hruby: float | None) -> str:
    """
    Výsledek pozice pro tabulku - hodnota po provizích a v závorce tatáž
    bez nich, například '-92.00 (-85.00)'. Bez zaplacené provize (obě hodnoty
    stejné) se závorka vynechává, ať sloupec zbytečně nebobtná.
    """
    if cisty is None:
        return "-"
    if hruby is None or abs(hruby - cisty) < 0.005:
        return fmt(cisty)
    return f"{fmt(cisty)} ({fmt(hruby)})"


# Meze, nad kterými se ukazatel kvality spojení v hlavičce zbarví do oranžova.
# Odezva: TWS na tomtéž stroji odpovídá v jednotkách milisekund, přes síť
# v desítkách - půl sekundy už znamená, že aplikace nestíhá. Stáří dat:
# během seance chodí kotace nepřetržitě, takže delší ticho není normální
RTT_VAROVANI_MS = 500.0
STARI_KOTACI_VAROVANI_SEC = 15.0


def stari_text(sekundy: float | None) -> str:
    """
    Stáří tržních dat pro hlavičku. Do deseti sekund se hodí desetina
    sekundy (je vidět, že data tečou), výš už jen celé sekundy a od minuty
    se přechází na minuty - přesnost tam nic neřekne.
    """
    if sekundy is None:
        return "-"
    if sekundy < 10:
        return f"{sekundy:.1f} s".replace(".", ",")
    if sekundy < 60:
        return f"{sekundy:.0f} s"
    return f"{sekundy / 60:.0f} min"


def stav_linky_text(rtt_ms: float | None, stari_sec: float | None) -> str:
    """
    Popis kvality spojení do hlavičky: odezva TWS a stáří tržních dat,
    například 'TWS 0,8 ms · data 0,4 s'.

    Chybějící hodnota se píše pomlčkou - u odezvy znamená neúspěšné nebo
    vypnuté měření, u dat to, že se zatím nic neodebírá.
    """
    odezva = f"{rtt_ms:.1f} ms".replace(".", ",") if rtt_ms is not None else "-"
    return f"TWS {odezva} · data {stari_text(stari_sec)}"


def linka_varuje(
    rtt_ms: float | None, stari_sec: float | None, trh_otevren: bool
) -> bool:
    """
    Má se ukazatel kvality spojení zvýraznit?

    Pomalá odezva TWS platí vždy - je to známka nestíhající aplikace bez
    ohledu na denní dobu. Stojící kotace se hlásí jen během seance: mimo
    obchodní hodiny trh nic neposílá, takže by varování svítilo pořád
    a přestalo cokoliv znamenat.
    """
    if rtt_ms is not None and rtt_ms > RTT_VAROVANI_MS:
        return True
    return (
        trh_otevren
        and stari_sec is not None
        and stari_sec > STARI_KOTACI_VAROVANI_SEC
    )


# Popisky polí PT a SL podle režimu zadání (režimy samotné žijí v models.py,
# sdílí je i hromadné načtení pozic ze souboru)
PT_LABELS = {
    MODE_UNDERLYING: "PT na podkladu",
    MODE_USD: "PT na opci [USD/ks]",
    MODE_PREMIUM: "PT na opci [% prémie]",
}
SL_LABELS = {
    MODE_UNDERLYING: "SL na podkladu",
    MODE_USD: "SL na opci [USD/ks]",
    MODE_PREMIUM: "SL na opci [% prémie]",
}


def popisek_urovne(druh: str, rezim: str) -> str:
    """
    Popisek pole PT ('pt') nebo SL ('sl') podle režimu. Která z úrovní se
    dopočítává (a je tedy nepovinná), říká přepínač prvotní úrovně pod poli
    a napovídá i umístění pole - zadávaná úroveň stojí vždy vedle vstupu.
    """
    return (PT_LABELS if druh == "pt" else SL_LABELS)[rezim]


class TradingUI:
    """Sestavuje a obsluhuje uživatelské rozhraní nad obchodním enginem."""

    def __init__(self, cfg: AppConfig, engine: FlowEngine, ib: IBService) -> None:
        self.cfg = cfg
        self.engine = engine
        self.ib = ib

    # ------------------------------------------------------------------
    # Sestavení stránky
    # ------------------------------------------------------------------

    def build(self) -> None:
        """Vykreslí celou stránku - hlavičku, formulář, přehled a log."""
        ui.add_head_html(f'<link rel="stylesheet" href="{staticky_soubor("styles.css")}">')

        # Obchod aktuálně načtený ve formuláři - jeho změny z tabulky
        # (posun cíle, SL) se promítají zpět do polí formuláře
        self.form_flow_id: str | None = None
        self.preview: Preview | None = None
        # Čas poslední vypsané události - log se překresluje jen při změně
        self.last_log_stamp: datetime | None = None
        # Pořadové číslo přípravy zadání - rozlišuje souběžně běžící požadavky
        self.preview_seq: int = 0
        # Právě běžící příprava zadání - novější zadání tu předchozí ruší
        self._preview_task: asyncio.Task | None = None
        # Ticker, ke kterému patří hodnoty ve formuláři
        self.last_symbol: str | None = None
        # Odhad nákupní ceny opce z posledního náhledu a ticker, ke kterému
        # patří. Z něj se převádějí úrovně zadané procentem prémie na USD
        # na kontrakt; None znamená, že převod zatím nelze provést
        self.premium_estimate: float | None = None
        self.premium_symbol: str | None = None
        # Prémie, kterou poslední příprava k převodu skutečně použila. Zpětný
        # převod do procent i odeslání musí vycházet z téže hodnoty - jinak by
        # se PT a SL rozešly, kdykoliv příprava vybere jiný strike (a s ním
        # jinou cenu opce), než z jakého se procenta převáděla
        self.premium_used: float | None = None
        # Naposledy zobrazené pozice bez dozoru; None znamená, že pruh ještě nebyl
        # vykreslen - prázdná množina je platný stav a nesmí se s tím zaměnit
        self.last_unmanaged: set[int] | None = None
        # Zámek proti mazání polí při programovém nastavení režimu PT/SL
        self._modes_locked: bool = False

        self._build_header()

        # Popup formulář pro hromadné načtení pozic ze souboru. Vzniká už teď,
        # aby jeho prvky patřily tomuto klientovi; otevírá jej tlačítko ve formuláři
        self.import_dialog = ImportDialog(self.cfg, self.engine, self.ib, self._refresh)
        self.import_dialog.build()

        # Popup s přehledem výsledků dne; grafy v něm se řídí zvoleným
        # vzhledem a přepnout se dá i zevnitř - přehled je přes celou
        # obrazovku, takže tlačítko v hlavičce stránky pod ním zmizí
        self.report_dialog = ReportDialog(
            self.cfg,
            self.engine,
            lambda: bool(self.dark_mode.value),
            self._toggle_dark,
        )
        self.report_dialog.build()

        # Pruh s upozorněním na pozice, které aplikace neřídí
        self.warning_bar = ui.row().classes("pruh-varovani")
        self.warning_bar.set_visibility(False)

        with ui.row().classes("obsah"):
            with ui.column().classes("panel-formular"):
                self._build_form()
            with ui.column().classes("panel-prehled"):
                self._build_table()
                self._build_log()

        # Periodická aktualizace zobrazovaných dat
        ui.timer(self.cfg.ui.refresh_interval_sec, self._refresh)

        # Odezva TWS se měří vlastním, řidším tempem - je to dotaz do TWS,
        # ne čtení z paměti jako zbytek obnovy. Nulový interval měření vypne
        if self.cfg.ui.latency_interval_sec > 0:
            ui.timer(self.cfg.ui.latency_interval_sec, self._measure_link)

    def _build_header(self) -> None:
        """Hlavička s názvem aplikace, přepínačem vzhledu a stavem spojení na TWS."""
        # Tmavý režim: výchozí hodnota z konfigurace, poslední volba
        # obchodníka se pamatuje mezi spuštěními
        self.dark_mode = ui.dark_mode(
            bool(app.storage.general.get("dark_mode", self.cfg.ui.dark))
        )
        with ui.header().classes("hlavicka"):
            ui.label("Obchodování opcí – TWS").classes("nazev")
            ui.space()
            # Odpočet do otevření burzy - mimo obchodní hodiny
            self.market_open_label = ui.label().classes("odpocet-otevreni")
            self.market_open_label.set_visibility(False)
            # Odpočet do zrušení čekajících obchodů v nastavený čas dne
            self.pending_cancel_label = ui.label().classes("odpocet-cekajici")
            self.pending_cancel_label.set_visibility(False)
            # Odpočet do automatického uzavření obchodů před koncem burzy
            self.auto_close_label = ui.label().classes("odpocet-uzavreni")
            self.auto_close_label.set_visibility(False)
            # Naplánované zadání pozic ze souboru. Režim běží i se zavřeným
            # dialogem, takže jinak než tady by nebyl vidět
            self.plan_label = ui.label().classes("odpocet-plan")
            self.plan_label.set_visibility(False)
            self.dark_button = ui.button(on_click=self._toggle_dark).props("flat round dense")
            with self.dark_button:
                ui.tooltip("Přepnout světlý/tmavý vzhled")
            self._refresh_dark_button()
            self.status_label = ui.label().classes("stav-spojeni")
            # Kvalita spojení: odezva TWS a stáří tržních dat. Ukazuje, že
            # spojení nejen stojí, ale i žije - odpojené se skrývá
            self.link_label = ui.label().classes("stav-linky")
            self.link_label.tooltip(
                "Odezva TWS je doba, za kterou odpoví na dotaz na čas - měří "
                "tedy samotnou aplikaci, ne síť k IB. Stáří dat je doba od "
                "nejčerstvější kotace ze všech odebíraných kontraktů; mimo "
                "obchodní hodiny přirozeně roste, protože trh nic neposílá."
            )
            self.link_label.set_visibility(False)
            self.connect_button = ui.button("Připojit", on_click=self._toggle_connection).props("flat")

    def _toggle_dark(self) -> None:
        """Přepne světlý/tmavý vzhled a volbu si zapamatuje."""
        self.dark_mode.value = not self.dark_mode.value
        app.storage.general["dark_mode"] = self.dark_mode.value
        self._refresh_dark_button()

    def _refresh_dark_button(self) -> None:
        """Ikona přepínače ukazuje režim, do kterého se lze přepnout."""
        ikona = "light_mode" if self.dark_mode.value else "dark_mode"
        self.dark_button.props(f"icon={ikona}")

    def _build_form(self) -> None:
        """Formulář pro zadání obchodu."""
        with ui.card().classes("karta"):
            # Vedle nadpisu je odznak LONG/SHORT - jen indikace směru, který
            # aplikace určuje z polohy vstupu vůči aktuální ceně podkladu hned
            # po zadání tickeru a vstupu (viz _refresh_direction)
            with ui.row().classes("radek-nadpis"):
                ui.label("Zadání obchodu").classes("nadpis-sekce")
                self.direction_badge = ui.label("").classes("odznak-smer odznak-smer-formular")
                self.direction_badge.tooltip(
                    "Směr obchodu podle polohy vstupu vůči aktuální ceně podkladu: "
                    "vstup nad trhem = LONG (CALL), vstup pod trhem = SHORT (PUT). "
                    "Dokud cena podkladu není známá, určuje se z polohy PT/SL "
                    "na podkladu vůči vstupu."
                )
                self.direction_badge.set_visibility(False)
                ui.space()
                # Hromadné zadání ze souboru se zadáním obchodního dne
                ui.button(
                    "Načíst ze souboru", on_click=self.import_dialog.open
                ).props("outline dense").classes("tlacitko-import").tooltip(
                    "Načte vstupní pozice ze souboru se zadáním dne a nabídne "
                    "jejich hromadné zadání do trhu."
                )

            with ui.row().classes("radek"):
                self.symbol_input = (
                    ui.input("Ticker", placeholder="AAPL")
                    .classes("pole-ticker")
                    .props("outlined dense")
                )
                # Po opuštění pole se načte cena podkladu a připraví se kontrakt
                self.symbol_input.on("blur", lambda _: self._load_preview("auto"))
                ui.button("Načíst", on_click=lambda: self._load_preview("nacist")).props(
                    "outline"
                ).classes("tlacitko-vedle").tooltip(
                    "Načte cenu podkladu, typ opce, expiraci, strike, kotace a deltu. "
                    "Vyplněná pole formuláře nemění."
                )

            # Řádek 1: vstup a prvotní úroveň (PT, nebo SL); řádek 2:
            # dopočítávaná úroveň a limit spreadu. Pole PT a SL se mezi řádky
            # přesouvají podle přepínače prvotní úrovně - zadávaná úroveň stojí
            # vždy hned vedle vstupu
            self.radek_prvotni = ui.row().classes("radek")
            with self.radek_prvotni:
                self.entry_input = (
                    ui.number("Vstup na podkladu", format="%.2f")
                    .classes("pole")
                    .props("outlined dense step=any")
                )
                self.pt_input = (
                    ui.number(
                        popisek_urovne("pt", rezim_urovne(self.cfg.trading.pt_on_underlying)),
                        format="%.2f",
                    )
                    .classes("pole")
                    .props("outlined dense step=any")
                )
                self.sl_input = (
                    ui.number(
                        popisek_urovne("sl", rezim_urovne(self.cfg.trading.sl_on_underlying)),
                        format="%.2f",
                    )
                    .classes("pole")
                    .props("outlined dense step=any")
                )

            self.radek_dopoctene = ui.row().classes("radek")
            with self.radek_dopoctene:
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

            # Množství, RRR a přepočet stojí v jednom řádku, aby formulář
            # nerostl do výšky; obě pole jsou proto užší
            with ui.row().classes("radek"):
                self.qty_input = (
                    ui.number("Množství [ks]", value=None, format="%.0f", step=1, min=1)
                    .classes("pole-uzke")
                    .props("outlined dense")
                )
                # RRR pro dopočet druhé úrovně; výchozí hodnota vychází
                # z konfigurace a přepsáním platí pro přepočet i pro zadání obchodu
                self.rrr_input = (
                    ui.number(
                        "RRR (PT:SL)",
                        value=rrr_z_pomeru(self.cfg.trading.sl_to_pt_ratio),
                        format="%g",
                        min=0,
                    )
                    .classes("pole-uzke")
                    .props("outlined dense step=any")
                    .tooltip(
                        "Poměr zisku ku riziku, kterým se z prvotní úrovně "
                        "dopočítá ta druhá: 2 = PT je dvakrát dál než SL, "
                        "1 = obě stejně daleko. Výchozí hodnota vychází "
                        "z konfigurace (převrácené trading.sl_to_pt_ratio), "
                        "prázdné či nekladné pole se k ní vrací."
                    )
                )
                # Změna RRR rovnou přepočítá dopočítávanou úroveň i množství
                self.rrr_input.on("blur", lambda _: self._load_preview("prepocitat"))
                ui.button("Přepočítat", on_click=lambda: self._load_preview("prepocitat")).props(
                    "outline"
                ).classes("tlacitko-vedle").tooltip(
                    "Přepíše dopočítávanou úroveň (SL, nebo PT podle volby) "
                    "a množství vypočtenými hodnotami. Úroveň podle zadaného "
                    "RRR, množství podle rizika a delty opce."
                )

            # Přepínače zadání stojí pod řádkem s množstvím ve třech oddělených
            # blocích: prvotní úroveň, režim SL a režim PT. Každý blok je dvojice
            # voleb (radio), jejichž hodnotou je pravdivostní hodnota jako dřív;
            # podrobnosti říká tooltip
            with ui.column().classes("prepinace"):
                # Která úroveň je prvotní: True = zadává se SL a PT se dopočítá
                # podle RRR, False = zadává se PT a dopočítá se SL.
                # Oranžová barva blok odlišuje od přepínačů režimu
                with ui.column().classes("skupina-prepinacu"):
                    self.sl_primary = (
                        ui.radio(
                            {
                                True: "Zadává se SL, PT se dopočítá podle RRR",
                                False: "Zadává se PT, SL se dopočítá podle RRR",
                            },
                            value=self.cfg.trading.primary_level == "sl",
                        )
                        .props("dense color=orange-8")
                        .classes("prepinac")
                        .tooltip(
                            "Zadávaná úroveň stojí vedle vstupu, dopočítávaná v dalším "
                            "řádku. Druhá úroveň se dopočítá podle RRR z pole vedle "
                            "množství a lze ji vždy přepsat ručně."
                        )
                    )
                    self.sl_primary.on_value_change(lambda e: self._on_primary_change())

                # Režim SL: na opci jde o ztrátu v USD na kontrakt, kterou
                # realizuje stop-market příkaz přímo na cenu opce
                with ui.column().classes("skupina-prepinacu"):
                    self.sl_mode = (
                        ui.radio(
                            {
                                MODE_UNDERLYING: "SL na podkladu (cena podkladu)",
                                MODE_USD: "SL na opci (ztráta v USD/ks)",
                                MODE_PREMIUM: "SL na opci (% prémie)",
                            },
                            value=rezim_urovne(self.cfg.trading.sl_on_underlying),
                        )
                        .props("dense")
                        .classes("prepinac")
                        .tooltip(
                            "Na podkladu: SL je cena podkladu a hlídá ji podmíněný "
                            "příkaz. Na opci: SL je ztráta na jedné opci v USD, "
                            "prodá se stop-market příkazem na cenu opce. "
                            "% prémie: tatáž ztráta zadaná podílem z ceny opce - "
                            "30 % z opce za 3,00 je 90 USD na kontrakt. Přepočet "
                            "na USD ukazuje náhled a provede se před odesláním."
                        )
                    )
                    self.sl_mode.on_value_change(
                        lambda e: self._on_mode_change("sl", str(e.value))
                    )

                    # Kompenzace spreadu patří k SL na opci: opce se kupuje u ASKu,
                    # ale stop se spouští BIDem, takže bez ní je SL blíž o celý
                    # spread. Pro SL na podkladu nemá smysl, proto se s ním zamyká
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
                    # Volba je dostupná jen při SL na opci (v USD i v procentech
                    # prémie); výchozí stav z konfigurace
                    self.sl_spread_compensated.set_enabled(
                        not self.cfg.trading.sl_on_underlying
                    )
                    self.sl_spread_compensated.on_value_change(
                        lambda _: self._on_sl_spread_change()
                    )

                # Režim PT: na opci jde o zisk v USD na kontrakt realizovaný
                # limitním příkazem přímo na cenu opce
                with ui.column().classes("skupina-prepinacu"):
                    self.pt_mode = (
                        ui.radio(
                            {
                                MODE_UNDERLYING: "PT na podkladu (cena podkladu)",
                                MODE_USD: "PT na opci (zisk v USD/ks)",
                                MODE_PREMIUM: "PT na opci (% prémie)",
                            },
                            value=rezim_urovne(self.cfg.trading.pt_on_underlying),
                        )
                        .props("dense")
                        .classes("prepinac")
                        .tooltip(
                            "Na podkladu: PT je cena podkladu a hlídá ji podmíněný "
                            "příkaz. Na opci: PT je zisk na jedné opci v USD, prodá "
                            "se limitním příkazem na cenu opce. % prémie: tentýž zisk "
                            "zadaný podílem z ceny opce - 30 % z opce za 3,00 je "
                            "90 USD na kontrakt. Přepočet na USD ukazuje náhled "
                            "a provede se před odesláním."
                        )
                    )
                    self.pt_mode.on_value_change(
                        lambda e: self._on_mode_change("pt", str(e.value))
                    )

                # Přepočet po otevření burzy - tatáž volba, jakou nabízí hromadné
                # zadání ze souboru. Obchod zadaný před otevřením má úrovně
                # i množství z odhadu prémie (typicky ze závěrečné ceny), který po
                # gapu neplatí; engine mu po zadané prodlevě od otevření dopočítá
                # PT, SL a množství z živých kotací a čekající příkaz upraví na
                # místě. Volba se zapisuje do zakládaného obchodu, takže platí
                # i po zavření stránky. Jako poslední blok přepínačů dědí linku
                # i svislý rytmus ostatních bloků, jen stojí ve dvou sloupcích
                with ui.row().classes("skupina-prepinacu blok-prepoctu"):
                    # Výchozí stav je vypnuto, protože formulář slouží hlavně
                    # k ručnímu zadání během seance - tam by se přepočet spustil
                    # hned při nejbližším průchodu monitorovací smyčkou. Sama se
                    # volba zapíná jen při načtení obchodu, který přepočet čeká
                    self.refresh_checkbox = (
                        ui.checkbox("Po otevření trhu přepočítat", value=False)
                        .props("dense")
                        .classes("prepinac")
                        .tooltip(
                            "Zaškrtnuto: obchod, který po otevření burzy ještě čeká "
                            "na vstup, se po uplynutí prodlevy vpravo jednou přepočítá "
                            "podle živých kotací - PT v procentech prémie, SL i množství "
                            "vyjdou ze skutečné ceny opce místo odhadu ze závěrečné ceny. "
                            "Čekající příkaz v trhu se upraví na místě, neruší se. Obchod, "
                            "který už nakoupil, se nemění. Zadává-li se obchod až za "
                            "otevřeného trhu, přepočet proběhne hned - volba patří "
                            "k obchodům chystaným před otevřením."
                        )
                    )
                    self.refresh_sec_input = (
                        ui.number(
                            "Prodleva [s]",
                            value=self.cfg.import_.refresh_after_open_sec,
                            format="%.0f",
                            step=5,
                            min=0,
                        )
                        .classes("pole-prepocet-sec")
                        .props("outlined dense")
                        .tooltip(
                            "Kolik sekund po otevření burzy se přepočet provede. "
                            "V prvních okamžicích jsou kotace opcí nejširší, proto "
                            "chvíli počkat. Výchozí hodnota je "
                            "import.refresh_after_open_sec z konfigurace."
                        )
                    )

            # Pole úrovní se rozmístí podle výchozí prvotní úrovně
            self._arrange_level_groups()

            # Opuštění pole jen obnoví načtená data; hodnoty ve formuláři
            # zůstávají. Změna hodnoty ihned přepočítá odznak směru obchodu
            for field_widget in (self.entry_input, self.pt_input, self.sl_input):
                field_widget.on("blur", lambda _: self._load_preview("auto"))
                field_widget.on_value_change(lambda _: self._refresh_direction())

            # Přehled vypočtených parametrů obchodu
            with ui.column().classes("nahled"):
                # Nenásilná indikace probíhajícího načítání dat z TWS
                self.loading_label = ui.label("Načítám data z TWS…").classes(
                    "indikace-nacitani"
                )
                self.loading_label.set_visibility(False)
                self.preview_label = ui.label(
                    "Zadejte ticker a ceny pro přípravu obchodu."
                ).classes("nahled-hlavni")
                self.preview_detail = ui.label("").classes("nahled-detail")
                self.preview_warning = ui.label("").classes("nahled-varovani")

            with ui.row().classes("radek radek-tlacitka"):
                # Barva se nastavuje přes props Quasaru, vlastní CSS třída řeší jen šířku
                ui.button("Potvrdit a zadat do trhu", on_click=self._submit).props(
                    "color=green-8"
                ).classes("tlacitko-potvrdit")
                ui.button("Zrušit flow", on_click=self._cancel_by_symbol).props(
                    "color=red-8"
                ).classes("tlacitko-zrusit")

        # Souhrn nastavení z konfiguračního souboru
        with ui.card().classes("karta karta-config"):
            ui.label("Konfigurace").classes("nadpis-sekce")
            self.config_label = ui.label("").classes("config-text")

    def _build_table(self) -> None:
        """Monitorovací tabulka běžících i ukončených obchodů."""
        with ui.card().classes("karta karta-tabulka"):
            with ui.row().classes("radek radek-nadpis"):
                ui.label("Monitoring obchodů").classes("nadpis-sekce")
                ui.space()
                # Přehled výsledků dne - co se obchodovalo, co běží a jak dopadlo
                ui.button(
                    "Výsledky",
                    icon="insights",
                    on_click=self.report_dialog.open,
                ).props("outline dense color=primary").classes(
                    "tlacitko-vysledky"
                ).tooltip(
                    "Přehled obchodního dne na celou obrazovku - souhrn výsledků, "
                    "běžící i uzavřené obchody a průběh dne v grafu."
                )
                # Úklid řádků bez výsledku - zrušené a propásnuté obchody
                ui.button(
                    "Uklidit neobchodované",
                    icon="playlist_remove",
                    on_click=self._on_remove_untraded,
                ).props("outline dense color=primary").classes(
                    "tlacitko-uklidit"
                ).tooltip(
                    "Odstraní z přehledu zrušené a propásnuté obchody - ty, které "
                    "se nikdy nedostaly k nákupu. Čekající, otevřené i uzavřené "
                    "obchody zůstávají, stejně jako obchod skončený chybou nebo "
                    "zrušený s otevřenou pozicí. Do TWS se nesahá."
                )
                # Hromadné zrušení běžících obchodů a vyprázdnění přehledu
                ui.button(
                    "Zrušit a smazat vše",
                    icon="delete_sweep",
                    on_click=self._on_clear_all,
                ).props("outline dense color=red-8").classes(
                    "tlacitko-smazat-vse"
                ).tooltip(
                    "Zruší všechny běžící obchody i jejich příkazy v TWS "
                    "a vymaže všechny položky z přehledu."
                )

            self.table = (
                ui.table(columns=TABLE_COLUMNS, rows=[], row_key="id")
                .classes("tabulka")
                .props('dense flat no-data-label="Zatím nebyl zadán žádný obchod."')
            )
            self.table.add_slot("body", BODY_SLOT)
            # Kliknutí na datový řádek přenese obchod do formuláře zadání
            self.table.on("vybratFlow", self._on_select_flow)
            # Tlačítko v řádku posune cíl obchodu na zvolený násobek
            self.table.on("nasobek", self._on_pt_multiple)
            # Tlačítka runneru - vlastní cíl pro část pozice
            self.table.on("runnerNasobek", self._on_runner_multiple)
            self.table.on("runnerZrusit", self._on_runner_cancel)
            # Přepínání SL - počáteční hodnota ze zadání, nebo break even
            self.table.on("nastavSl", self._on_set_sl)
            self.table.on("nastavRunnerSl", self._on_set_runner_sl)
            # Okamžité uzavření části pozice tržním příkazem
            self.table.on("uzavritPozici", self._on_close_main)
            self.table.on("uzavritRunner", self._on_close_runner)
            # Akce celého obchodu - zrušení, resp. odstranění z přehledu
            self.table.on("zrusitFlow", self._on_cancel_flow)
            self.table.on("odstranitFlow", self._on_remove_flow)

    def _build_log(self) -> None:
        """Panel s provozním logem aplikace."""
        with ui.card().classes("karta karta-log"):
            ui.label("Průběh").classes("nadpis-sekce")
            self.log_area = ui.column().classes("log-obsah")

    # ------------------------------------------------------------------
    # Obsluha akcí
    # ------------------------------------------------------------------

    async def _toggle_connection(self) -> None:
        """Připojí nebo odpojí aplikaci od TWS."""
        try:
            if self.ib.connected:
                # Ruční odpojení vypne i automatické obnovování spojení ve smyčce
                self.engine.auto_connect = False
                await self.ib.disconnect()
                ui.notify("Spojení s TWS ukončeno.", type="warning")
            else:
                await self.ib.connect()
                self.engine.auto_connect = True
                # Po ručním připojení se dohledají obchody z předchozího běhu
                await self.engine.restore()
                ui.notify("Spojení s TWS navázáno.", type="positive")
        except Exception as exc:
            ui.notify(f"Spojení se nezdařilo: {exc}", type="negative")

    def _set_loading(self, active: bool, text: str = "Načítám data z TWS…") -> None:
        """Zobrazí, nebo skryje pulzující indikaci probíhajícího načítání."""
        if active:
            self.loading_label.set_text(text)
        self.loading_label.set_visibility(active)

    def _set_direction(self, right: str | None) -> None:
        """
        Ukáže odznak směru obchodu ('C' = LONG, 'P' = SHORT) vedle nadpisu
        formuláře; bez známého směru (None) odznak skryje.
        """
        if right not in ("C", "P"):
            self.direction_badge.set_visibility(False)
            return
        self.direction_badge.set_text("LONG (CALL)" if right == "C" else "SHORT (PUT)")
        self.direction_badge.classes(
            remove="smer-long smer-short", add="smer-long" if right == "C" else "smer-short"
        )
        self.direction_badge.set_visibility(True)

    def _refresh_direction(self) -> None:
        """Obnoví odznak směru podle aktuálního stavu formuláře."""
        self._set_direction(self._direction_from_form())

    def _direction_from_form(self) -> str | None:
        """
        Směr obchodu, jak jej lze určit z formuláře ('C' / 'P', jinak None).

        Načtený běžící obchod má směr daný, ale jen dokud jej formulář
        skutečně popisuje: přepsaná vstupní cena nebo úrovně na opačné
        straně znamenají nové zadání, u kterého směr načteného obchodu
        neplatí. Jinak rozhoduje poloha vstupu vůči aktuální ceně podkladu
        z posledního náhledu (ta se načítá už po zadání tickeru a vstupu),
        stejně jako v enginu. Dokud cena není známá (např. bez spojení
        s TWS), napoví aspoň poloha PT/SL na podkladu vůči vstupu.

        Právě proto se vstup porovnává: u nakoupeného obchodu cena podkladu
        jeho vstupní úroveň běžně překoná, takže přepočet ze samotné ceny
        by u něj směr otočil.
        """
        symbol, entry, pt, sl = self._form_values()
        pt_on, sl_on = self._form_modes()
        # Zamýšlený směr ze zadaných úrovní; None = z formuláře jej určit nelze
        # (obě úrovně na opci jsou jen částky v USD)
        zamer = calc.intended_right(entry, pt, sl, pt_on, sl_on) if entry is not None else None
        if self.form_flow_id:
            flow = self.engine.flows.get(self.form_flow_id)
            # Směr načteného obchodu platí, dokud mu neodporují zadané úrovně
            # ani přepsaný vstup; prázdné pole vstupu se ještě za změnu nepovažuje
            if (
                flow is not None
                and zamer in (None, flow.right)
                and (entry is None or abs(entry - flow.entry_price) < 0.005)
            ):
                return flow.right
        if not symbol or entry is None:
            return None
        # Cena z náhledu platí jen pro stejný ticker
        preview = self.preview
        if (
            preview is not None
            and preview.symbol == symbol
            and preview.current_price is not None
        ):
            return calc.determine_right(preview.current_price, entry)
        return zamer

    def _form_values(self) -> tuple[str, float | None, float | None, float | None]:
        """Přečte hodnoty z formuláře a převede je na čísla."""
        symbol = (self.symbol_input.value or "").upper().strip()
        entry = float(self.entry_input.value) if self.entry_input.value not in (None, "") else None
        pt = float(self.pt_input.value) if self.pt_input.value not in (None, "") else None
        sl = float(self.sl_input.value) if self.sl_input.value not in (None, "") else None
        return symbol, entry, pt, sl

    def _form_level_modes(self) -> tuple[str, str]:
        """Zvolené režimy úrovní PT a SL (podklad / USD na opci / % prémie)."""
        return str(self.pt_mode.value), str(self.sl_mode.value)

    def _form_modes(self) -> tuple[bool, bool]:
        """
        Režimy PT a SL pro engine: True = na podkladu, False = na opci.
        Procento prémie je jen jednotka formuláře, pro engine je to úroveň
        na opci stejně jako zápis přímo v USD.
        """
        pt_mode, sl_mode = self._form_level_modes()
        return pt_mode == MODE_UNDERLYING, sl_mode == MODE_UNDERLYING

    def _form_ratio(self) -> float | None:
        """
        Poměr SL:PT pro engine, odvozený z RRR ve formuláři.

        Formulář se ptá na RRR (kolikrát je PT dál než SL), engine počítá
        s obrácenou hodnotou - proto převrácená hodnota. Prázdné i nekladné
        pole vrací None a engine pak sáhne do konfigurace.
        """
        if self.rrr_input.value in (None, ""):
            return None
        rrr = float(self.rrr_input.value)
        return pomer_z_rrr(rrr) if rrr > 0 else None

    def _form_sl_spread(self) -> bool:
        """
        Zaškrtávátko kompenzace SL o spread. U SL na podkladu se neuplatní,
        i kdyby zůstalo zaškrtnuté z dřívějšího zadání.
        """
        _, sl_mode = self._form_level_modes()
        return bool(self.sl_spread_compensated.value) and sl_mode in MODES_ON_OPTION

    def _form_refresh_sec(self) -> float | None:
        """
        Prodleva přepočtu po otevření burzy pro zakládaný obchod; None znamená
        přepočet nepoužít (nezaškrtnutá volba). Prázdné či záporné pole spadne
        zpět na hodnotu z konfigurace, ať se přepočet neřídí náhodným číslem.
        """
        if not self.refresh_checkbox.value:
            return None
        hodnota = self.refresh_sec_input.value
        if hodnota in (None, "") or float(hodnota) < 0:
            return float(self.cfg.import_.refresh_after_open_sec)
        return float(hodnota)

    def _set_modes(
        self,
        pt_on_underlying: bool,
        sl_on_underlying: bool,
        sl_spread_compensated: bool | None = None,
        pt_in_premium: bool = False,
        sl_in_premium: bool = False,
    ) -> None:
        """
        Nastaví volby režimu bez vedlejších účinků jejich obsluhy -
        při programovém nastavení se hodnoty polí nesmí mazat.
        Bez zadané kompenzace (None) se její přepínač nechává být.

        Příznaky pt_in_premium a sl_in_premium říkají, že úroveň na opci
        byla zadaná procentem prémie; konfigurace je nezná, proto výchozí
        nastavení formuláře vždy sáhne po zápisu v USD.
        """
        self._modes_locked = True
        try:
            self.pt_mode.set_value(rezim_urovne(pt_on_underlying, pt_in_premium))
            self.sl_mode.set_value(rezim_urovne(sl_on_underlying, sl_in_premium))
            if sl_spread_compensated is not None:
                self.sl_spread_compensated.set_value(sl_spread_compensated)
        finally:
            self._modes_locked = False
        self._refresh_mode_labels()

    def _form_primary(self) -> str:
        """Prvotní úroveň z volby: 'sl' (PT se dopočítá), nebo 'pt'."""
        return "sl" if self.sl_primary.value else "pt"

    def _computed_input(self) -> ui.number:
        """Pole dopočítávané úrovně - PT při prvotním SL, jinak SL."""
        return self.pt_input if self._form_primary() == "sl" else self.sl_input

    def _set_primary(self, primary: str) -> None:
        """Nastaví volbu prvotní úrovně bez mazání polí (pod zámkem)."""
        self._modes_locked = True
        try:
            self.sl_primary.set_value(primary == "sl")
        finally:
            self._modes_locked = False
        self._refresh_mode_labels()

    def _refresh_mode_labels(self) -> None:
        """Popisky polí PT a SL odpovídají zvolenému režimu; rozmístění prvotní úrovni."""
        pt_mode, sl_mode = self._form_level_modes()
        self.pt_input.props(f'label="{popisek_urovne("pt", pt_mode)}"')
        self.sl_input.props(f'label="{popisek_urovne("sl", sl_mode)}"')
        # Kompenzace spreadu se týká jen SL zadaného na opci - v USD i v procentech
        self.sl_spread_compensated.set_enabled(sl_mode in MODES_ON_OPTION)
        self._arrange_level_groups()

    def _arrange_level_groups(self) -> None:
        """
        Rozmístí pole PT a SL podle prvotní úrovně: zadávaná úroveň stojí
        v prvním řádku hned vedle vstupu, dopočítávaná v druhém řádku před
        limitem spreadu.
        """
        if self._form_primary() == "sl":
            prvotni, dopoctena = self.sl_input, self.pt_input
        else:
            prvotni, dopoctena = self.pt_input, self.sl_input
        # Přesun do stejného místa je neškodný - NiceGUI prvek jen přeřadí
        prvotni.move(self.radek_prvotni, target_index=1)
        dopoctena.move(self.radek_dopoctene, target_index=0)

    def _on_mode_change(self, druh: str, rezim: str) -> None:
        """
        Přepnutí režimu PT nebo SL obchodníkem.
        Hodnota v poli má v novém režimu jiný význam (cena podkladu vs. USD),
        proto se pole vyprázdní; dopočítávaná úroveň se navíc spočítá znovu.

        Obsluha je synchronní záměrně: programové nastavení zaškrtávátek
        (_set_modes) ji volá pod zámkem, a asynchronní obsluha by se spustila
        až po jeho uvolnění a právě naplněná pole by smazala.
        """
        if self._modes_locked:
            return
        self._refresh_mode_labels()
        prepnute = self.pt_input if druh == "pt" else self.sl_input
        prepnute.set_value(None)
        self._computed_input().set_value(None)
        self._naplanuj_nahled()

    def _on_sl_spread_change(self) -> None:
        """
        Přepnutí kompenzace SL o spread obchodníkem. Zadané úrovně zůstávají,
        mění se jen doporučené množství (kompenzovaná ztráta je větší), proto
        stačí přepočítat náhled. Synchronní ze stejného důvodu jako
        _on_mode_change - programové nastavení běží pod zámkem.
        """
        if self._modes_locked:
            return
        self._naplanuj_nahled()

    def _on_primary_change(self) -> None:
        """
        Přepnutí prvotní úrovně obchodníkem: dosud dopočítávaná úroveň se
        stává zadávanou a naopak. Nově dopočítávané pole se vyprázdní a
        spočítá znovu z toho zadaného. Synchronní ze stejného důvodu jako
        _on_mode_change.
        """
        if self._modes_locked:
            return
        self._refresh_mode_labels()
        self._computed_input().set_value(None)
        self._naplanuj_nahled()

    def _naplanuj_nahled(self, rezim: str = "auto") -> None:
        """
        Spustí přípravu náhledu až po doběhnutí právě probíhající obsluhy.

        Úloha založená přes background_tasks běží bez kontextu prvku a
        ui.notify v ní končí chybou "current slot cannot be determined";
        časovač vytvořený v kontextu formuláře jej naopak má.
        """
        with self.radek_prvotni:
            ui.timer(0, lambda: self._load_preview(rezim), once=True)

    def _bezici_pro_formular(
        self, symbol: str, entry: float | None, pt: float | None, sl: float | None = None
    ) -> Flow | None:
        """
        Najde běžící obchod, ke kterému se vztahuje formulář.

        Na tickeru může běžet long i short zároveň; směr určují vyplněné ceny
        na podkladu (PT nad vstupem = long/CALL, pod vstupem = short/PUT).
        Bez nich se vrací jediný běžící obchod tickeru - při dvou je výběr
        nejednoznačný.
        """
        flows = self.engine.active_flows_for(symbol)
        pt_on, sl_on = self._form_modes()
        zamer = calc.intended_right(entry, pt, sl, pt_on, sl_on) if entry is not None else None
        if zamer is not None:
            return next((flow for flow in flows if flow.right == zamer), None)
        if len(flows) == 1:
            return flows[0]
        return None

    def _fill_from_flow(self, flow: Flow) -> None:
        """Naplní formulář parametry existujícího obchodu."""
        self.last_symbol = flow.symbol
        self.form_flow_id = flow.id
        self.symbol_input.set_value(flow.symbol)
        self._set_direction(flow.right)
        # Cena opce, ze které obchod procenta počítal, musí platit dřív než
        # se úrovně zapíšou - podle ní se vrací zpět do procent
        pt_pct, sl_pct = self._prevezmi_premii(flow)
        # Režimy se nastavují před hodnotami - jejich změna pole maže
        self._set_modes(
            flow.pt_on_underlying,
            flow.sl_on_underlying,
            flow.sl_spread_compensated,
            pt_pct,
            sl_pct,
        )
        # Prvotní úroveň přeskládá pole PT a SL mezi řádky, proto ještě
        # před zápisem hodnot; obchod ze starší verze ji nezná a volba
        # tedy zůstane, jak je
        if flow.primary_level in ("sl", "pt"):
            self._set_primary(flow.primary_level)
        # RRR se ukládá jako poměr SL:PT, formulář se ptá na převrácenou hodnotu
        if flow.sl_to_pt_ratio and flow.sl_to_pt_ratio > 0:
            self.rrr_input.set_value(rrr_z_pomeru(flow.sl_to_pt_ratio))
        self.entry_input.set_value(round(flow.entry_price, 2))
        self.pt_input.set_value(self._uroven_do_pole(flow.profit_target, pt_pct))
        self._zapis_sl(flow)
        self.spread_input.set_value(flow.max_spread_pct)
        self.qty_input.set_value(flow.quantity)
        # Přepočet po otevření se přenáší, aby ho nové zadání téhož obchodu
        # tiše neztratilo - zadání do trhu obchod nahrazuje novým a ten by
        # jinak zůstal bez přepočtu. Už provedený přepočet se ale neopakuje:
        # hodnoty ve formuláři z něj vyšly a druhý běh by je přepsal znovu
        ceka_prepocet = (
            flow.refresh_after_open_sec is not None and not flow.refresh_after_open_done
        )
        self.refresh_checkbox.set_value(ceka_prepocet)
        if ceka_prepocet:
            self.refresh_sec_input.set_value(flow.refresh_after_open_sec)

    def _prevezmi_premii(self, flow: Flow) -> tuple[bool, bool]:
        """
        Převezme z obchodu cenu opce, ze které se počítala procenta prémie,
        a vrátí, zda se PT a SL do formuláře vrací v procentech.

        Zpětný převod i případné nové zadání musí vyjít z téže ceny jako
        původní zadání - jinak by se zapsaná procenta pokaždé posunula podle
        aktuální kotace. Obchod bez uložené ceny (starší stav nebo zadání
        v USD) procento nabídnout nemůže, takže zůstane u USD na kontrakt.
        """
        if not flow.premium_base or flow.premium_base <= 0:
            return False, False

        self.premium_estimate = flow.premium_base
        self.premium_symbol = flow.symbol
        self.premium_used = flow.premium_base
        # Úroveň na podkladu je cena, ne podíl z prémie - procento se na ni nevztahuje
        return (
            flow.pt_in_premium and not flow.pt_on_underlying,
            flow.sl_in_premium and not flow.sl_on_underlying,
        )

    def _uroven_do_pole(self, hodnota: float, v_procentech: bool) -> float:
        """
        Úroveň obchodu (cena podkladu, nebo USD na kontrakt) v jednotce pole.
        V režimu procenta prémie se přepočte cenou opce; bez ní se zapíše
        původní hodnota v USD, aby pole nezůstalo prázdné.
        """
        if v_procentech:
            pct = self._usd_na_pct(hodnota, self.premium_used)
            if pct is not None:
                return pct
        return round(hodnota, 2)

    def _zapis_sl(self, flow: Flow) -> None:
        """
        Zapíše SL obchodu do formuláře.

        Break even na opci je nulová ztráta; ve formuláři by nula znamenala
        "nezadáno" a přepočet ani nové zadání by s ní nešly provést, proto
        se pole nechává prázdné. Skutečnou úroveň ukazuje přehled obchodů.

        Spread připočtený při nákupu se zase odečítá - do formuláře patří
        hodnota, kterou obchodník zadal. Jinak by se při dalším zadání
        kompenzace navršila podruhé.
        """
        if not flow.sl_on_underlying and flow.stop_loss <= 0:
            self.sl_input.set_value(None)
            return
        _, sl_mode = self._form_level_modes()
        self.sl_input.set_value(
            self._uroven_do_pole(
                flow.stop_loss - flow.sl_spread_usd, sl_mode == MODE_PREMIUM
            )
        )

    def _clear_inputs(self) -> None:
        """
        Vyprázdní ceny a množství ve formuláři.
        Volá se při přechodu na jiný ticker, aby se do nového zadání
        nepřenesly hodnoty dříve načteného obchodu.
        """
        for pole in (self.entry_input, self.pt_input, self.sl_input, self.qty_input):
            pole.set_value(None)
        # Přepočet po otevření patřil předchozímu obchodu - na jiný ticker
        # se nepřenáší, prodleva v poli zůstává pro případné další zapnutí
        self.refresh_checkbox.set_value(False)
        # Limit spreadu, režimy PT/SL i prvotní úroveň se vrací na konfiguraci
        self.spread_input.set_value(self.cfg.trading.max_spread_pct)
        self._set_modes(
            self.cfg.trading.pt_on_underlying,
            self.cfg.trading.sl_on_underlying,
            self.cfg.trading.sl_spread_compensated,
        )
        self._set_primary(self.cfg.trading.primary_level)

        # Formulář už nedrží žádný načtený obchod ani odhad prémie
        self.form_flow_id = None
        self.preview = None
        self.premium_estimate = None
        self.premium_symbol = None
        self.premium_used = None
        self.preview_detail.set_text("")
        self.preview_warning.set_text("")
        self._set_direction(None)

    def _premie_pro(self, symbol: str) -> float | None:
        """Odhad nákupní ceny opce pro daný ticker, je-li z náhledu k dispozici."""
        if self.premium_symbol != symbol:
            return None
        return self.premium_estimate

    def _zapamatuj_premii(self, preview: Preview) -> None:
        """Uloží odhad nákupní ceny opce z náhledu pro převody procent prémie."""
        if preview.expected_fill_price and preview.expected_fill_price > 0:
            self.premium_estimate = preview.expected_fill_price
            self.premium_symbol = preview.symbol

    @staticmethod
    def _pct_na_usd(pct: float | None, premie: float | None) -> float | None:
        """
        Úroveň zadaná procentem prémie převedená na USD na kontrakt.
        Prémie kontraktu je cena opce krát 100, takže jedno procento je
        právě cena opce v USD. Bez známé prémie vrací None.
        """
        if pct is None or premie is None or premie <= 0:
            return None
        return round(pct * premie, 2)

    @staticmethod
    def _usd_na_pct(usd: float | None, premie: float | None) -> float | None:
        """Zpětný převod z USD na kontrakt na procento prémie."""
        if usd is None or premie is None or premie <= 0:
            return None
        return round(usd / premie, 2)

    def _max_spread(self) -> float | None:
        """
        Limit spreadu z formuláře. Prázdné pole vrací None - platí pak
        hodnota z konfigurace. Náhled i zadání čtou limit odsud, aby se
        množství počítalo podle téhož čísla, jaké obchod dostane.
        """
        return float(self.spread_input.value) if self.spread_input.value else None

    def _urovne_v_usd(
        self, pt: float | None, sl: float | None, premie: float | None
    ) -> tuple[float | None, float | None]:
        """
        Úrovně z formuláře převedené do jednotek, kterým rozumí engine.
        Režim procenta prémie se přepočte přes odhad nákupní ceny opce,
        ostatní režimy se předávají beze změny.
        """
        pt_mode, sl_mode = self._form_level_modes()
        if pt_mode == MODE_PREMIUM:
            pt = self._pct_na_usd(pt, premie)
        if sl_mode == MODE_PREMIUM:
            sl = self._pct_na_usd(sl, premie)
        return pt, sl

    async def _priprav_s_rezimy(
        self,
        symbol: str,
        entry: float,
        pt: float | None,
        sl: float | None,
        pt_on: bool,
        sl_on: bool,
        sl_spread: bool,
        pomer: float | None,
    ) -> Preview:
        """
        Připraví zadání a přitom vyřeší úrovně zadané procentem prémie.

        Procento se vztahuje k ceně opce, kterou ale aplikace vybírá teprve
        podle zadaných úrovní. Není-li odhad nákupní ceny z dřívějšího náhledu
        po ruce, připraví se zadání nejdřív s procentem dosazeným místo USD -
        slouží jen k výběru kontraktu a odhadu jeho ceny. Z ní pak vyjdou
        skutečné úrovně v USD a zadání se připraví znovu.
        """
        pt_mode, sl_mode = self._form_level_modes()
        if MODE_PREMIUM not in (pt_mode, sl_mode):
            self.premium_used = None
            return await self.engine.prepare(
                symbol, entry, pt, sl, pt_on, sl_on, sl_spread, pomer, self._max_spread()
            )

        premie = self._premie_pro(symbol)
        if premie is None:
            # Hrubý první průchod jen kvůli ceně opce; jeho úrovně se zahodí
            odhad = await self.engine.prepare(
                symbol, entry, pt, sl, pt_on, sl_on, sl_spread, pomer, self._max_spread()
            )
            self._zapamatuj_premii(odhad)
            premie = self._premie_pro(symbol)
            if premie is None:
                self.premium_used = None
                return odhad

        # Od téhle chvíle platí pro celý přepočet jediná prémie - do USD i zpět
        # do procent. Příprava sice může vybrat jiný strike (a s ním jinak
        # drahou opci), ale úrovně se podle něj už nepřepočítávají; jinak by PT
        # vycházelo z jedné ceny opce a SL se do procent vracelo podle druhé
        self.premium_used = premie
        pt_usd, sl_usd = self._urovne_v_usd(pt, sl, premie)
        preview = await self.engine.prepare(
            symbol, entry, pt_usd, sl_usd, pt_on, sl_on, sl_spread, pomer, self._max_spread()
        )
        self._zapamatuj_premii(preview)
        return preview

    async def _load_preview(self, rezim: str = "nacist") -> None:
        """
        Připraví obchod podle vyplněných polí - určí typ opce, expiraci, strike,
        načte kotace a deltu a spočítá doporučený SL i množství kontraktů.

        Režimy:
          'auto'        - vyvolá opuštění pole; hodnoty ve formuláři nechává být
                          a mění je jen při přechodu na jiný ticker
          'nacist'      - tlačítko Načíst; běží-li na tickeru obchod, přepíše
                          formulář jeho parametry i přes ručně zadané hodnoty
          'prepocitat'  - tlačítko Přepočítat; přepíše SL a množství vypočtenými
                          hodnotami, zadaný SL se ignoruje a počítá se znovu
        """
        symbol, entry, pt, sl = self._form_values()
        if not symbol:
            return

        bezici = self._bezici_pro_formular(symbol, entry, pt, sl)
        zmena_tickeru = symbol != self.last_symbol

        # Načtení se vyžaduje buď tlačítkem, nebo přechodem na jiný ticker
        if bezici is not None and (rezim == "nacist" or zmena_tickeru):
            self._fill_from_flow(bezici)
            # Hodnoty se přebírají z formuláře, ne znovu z obchodu: _fill_from_flow
            # je už převedlo do jednotek polí (obchod drží úrovně na opci v USD
            # na kontrakt, pole je může vést v procentech prémie) a u SL navíc
            # odečetlo spread připočtený při nákupu. Počítat je znovu z obchodu
            # by kompenzaci spreadu započetlo podruhé
            _, entry, pt, sl = self._form_values()
            ui.notify(f"Načten běžící obchod {bezici.id}.", type="info")
        elif bezici is None and zmena_tickeru:
            # Ticker bez jednoznačného obchodu - hodnoty se nesmí přenést
            self._clear_inputs()
            entry = pt = sl = None

        # Běží-li na tickeru long i short a ceny směr neurčují, Načíst samo
        # nevybere - obchodník musí obchod zvolit kliknutím, nebo vyplnit ceny
        if (
            bezici is None
            and rezim == "nacist"
            and len(self.engine.active_flows_for(symbol)) > 1
        ):
            ui.notify(
                f"Na tickeru {symbol} běží long i short obchod - vyberte jej "
                f"kliknutím v přehledu, nebo vyplňte vstup a PT.",
                type="info",
            )
        self.last_symbol = symbol
        self._refresh_direction()

        if not self.ib.connected:
            self.preview_label.set_text("Není navázáno spojení s TWS.")
            return

        # Kliknutí na tlačítko vyvolá i opuštění právě editovaného pole, takže
        # mohou běžet dvě přípravy najednou. Zapisuje se jen výsledek té poslední.
        self.preview_seq += 1
        pozadavek = self.preview_seq
        self._set_loading(True)

        pt_on, sl_on = self._form_modes()
        sl_spread = self._form_sl_spread()
        pomer = self._form_ratio()
        # Přepočet zahazuje dopočítávanou úroveň (podle prvotní), aby se spočítala
        # znovu. Prázdná prvotní úroveň by ale nechala formulář bez zadání,
        # proto se v takovém případě počítá z té vyplněné
        if rezim == "prepocitat":
            if self._form_primary() == "sl":
                if sl is not None:
                    pt = None
            elif pt is not None:
                sl = None
        # Rozběhnutá dřívější příprava už není potřeba - její výsledek by se
        # stejně zahodil a do té doby by zbytečně držela odběry a odpovědi z TWS
        if self._preview_task is not None and not self._preview_task.done():
            self._preview_task.cancel()
        self._preview_task = asyncio.create_task(
            self._priprav_s_rezimy(symbol, entry, pt, sl, pt_on, sl_on, sl_spread, pomer)
        )
        try:
            preview = await self._preview_task
        except asyncio.CancelledError:
            # Nahradila ji novější příprava, ta indikaci i výsledek dořeší.
            # Zrušení celé obsluhy zvenčí se naopak musí propagovat dál
            if pozadavek != self.preview_seq:
                return
            raise
        except Exception as exc:
            # Indikaci zhasíná až poslední rozběhnutý požadavek
            if pozadavek != self.preview_seq:
                return
            self._set_loading(False)
            self.preview = None
            self.preview_label.set_text(f"Chyba přípravy zadání: {exc}")
            self.preview_detail.set_text("")
            self.preview_warning.set_text("")
            self._refresh_direction()
            return

        if pozadavek != self.preview_seq:
            return

        self._set_loading(False)
        self.preview = preview
        self._apply_preview(preview, rezim)

    def _apply_preview(self, preview: Preview, rezim: str) -> None:
        """Promítne připravený obchod do formuláře a informačního panelu."""
        # Odhad nákupní ceny opce slouží převodům procent prémie
        self._zapamatuj_premii(preview)

        # Bez vybraného kontraktu nejsou dopočtené úrovně ani množství k dispozici
        if preview.expiration:
            # Dopočítává se úroveň, která není prvotní (PT při prvotním SL, jinak SL)
            pole = self._computed_input()
            hodnota = preview.profit_target if pole is self.pt_input else preview.stop_loss
            # Engine vrací úroveň na opci v USD; do pole v režimu procenta
            # prémie patří zpět procento
            pt_mode, sl_mode = self._form_level_modes()
            rezim_pole = pt_mode if pole is self.pt_input else sl_mode
            if rezim_pole == MODE_PREMIUM:
                hodnota = self._usd_na_pct(hodnota, self.premium_used)
            if hodnota is None:
                hodnota = 0.0
            if rezim == "prepocitat":
                # Výslovný přepočet přepíše obě pole vypočtenými hodnotami
                pole.set_value(round(hodnota, 2))
                self.qty_input.set_value(preview.quantity)
            else:
                # Načtení pouze doplní dosud nevyplněná pole, ruční zadání ponechá
                if pole.value in (None, "") and hodnota:
                    pole.set_value(round(hodnota, 2))
                if not self.qty_input.value:
                    self.qty_input.set_value(preview.quantity)

        if preview.current_price is None:
            self.preview_label.set_text(f"{preview.symbol}: cena podkladu zatím není k dispozici.")
        elif not preview.expiration:
            # Vyjmenují se jen pole, která skutečně chybí - bez vstupní ceny
            # nelze dopočítat ani druhou úroveň, i když je ta prvotní vyplněná
            _, entry, pt, sl = self._form_values()
            chybi = []
            if entry is None:
                chybi.append("vstupní cenu")
            if pt is None and sl is None:
                chybi.append("SL" if self._form_primary() == "sl" else "PT")
            self.preview_label.set_text(
                f"{preview.symbol}: aktuální cena {fmt(preview.current_price)} – "
                f"doplňte {' a '.join(chybi) or 'zadání'}."
            )
        else:
            self.preview_label.set_text(
                f"{preview.right_label} {preview.symbol} {preview.expiration} "
                f"strike {preview.strike:g} | podklad {fmt(preview.current_price)}"
            )

        detail_parts = []
        if preview.expiration:
            detail_parts.append(f"Bid/Ask {fmt(preview.option_bid)} / {fmt(preview.option_ask)}")
            detail_parts.append(f"spread {fmt(preview.spread_pct, 2, ' %')}")
            # U dopočítané delty se to uvede, aby bylo zřejmé, že nejde o údaj z TWS
            delta_text = fmt(preview.delta, 3)
            if preview.delta_estimated:
                delta_text += " (dopočet)"
            # Delta výše popisuje dnešní cenu podkladu, počítá se ale s tou
            # při vstupu - u zadání daleko od trhu se obě liší i násobně,
            # takže se vedle sebe ukážou obě
            if preview.entry_delta is not None and preview.delta is not None:
                if abs(preview.entry_delta - preview.delta) >= 0.005:
                    delta_text += f" → při vstupu {fmt(preview.entry_delta, 3)}"
            detail_parts.append(f"delta {delta_text}")
            # Při PT na opci se uvede, ke které úrovni podkladu se strike vybíral
            if preview.target_level is not None:
                detail_parts.append(
                    f"cíl na podkladu ≈ {fmt(preview.target_level)} "
                    f"({preview.target_level_source})"
                )
            pt_text = level_text("pt", preview.profit_target, preview.pt_on_underlying)
            sl_text = level_text("sl", preview.stop_loss, preview.sl_on_underlying)
            # V režimu procenta prémie se uvede, z jaké ceny opce se počítalo -
            # jinak není poznat, odkud se doporučené USD vzalo
            if MODE_PREMIUM in self._form_level_modes() and self.premium_used:
                detail_parts.append(
                    f"základ procent: prémie ≈ {fmt(self.premium_used * 100)} USD/ks"
                )
            # Kompenzace se uplatní až skutečným spreadem při nákupu, náhled
            # proto uvádí jen odhad z aktuální kotace
            if preview.sl_spread_compensated:
                sl_text += f" + spread ≈ {fmt(preview.sl_spread_usd)} USD"
            detail_parts.append(
                f"doporučeno: PT {pt_text}, SL {sl_text}, {preview.quantity} ks"
            )
            detail_parts.append(
                f"risk {fmt(preview.risk_amount)} USD z účtu {fmt(preview.account_size)} USD"
            )
        self.preview_detail.set_text(" | ".join(detail_parts))
        self.preview_warning.set_text(" ".join(preview.warnings))
        # Směr je známý už z ceny podkladu a vstupu - ještě před výběrem kontraktu
        self._refresh_direction()

    async def _on_select_flow(self, event: Any) -> None:
        """
        Kliknutí na řádek monitorovací tabulky přenese všechna data obchodu
        do formuláře Zadání obchodu, aby se s nimi dalo dál pracovat.
        """
        data = event.args or {}
        flow = self.engine.flows.get(data.get("id", ""))
        if flow is None:
            return

        self._fill_from_flow(flow)
        ui.notify(f"{flow.id}: data obchodu přenesena do formuláře.", type="info")
        # Obnoví se i informační panel - kotace, delta a doporučené hodnoty
        await self._load_preview("auto")

    async def _on_pt_multiple(self, event: Any) -> None:
        """
        Posune cíl obchodu na zvolený násobek původní vzdálenosti od vstupu.
        Obsluhuje tlačítka v druhém řádku monitorovací tabulky.
        """
        data = event.args or {}
        flow = self.engine.flows.get(data.get("id", ""))
        nasobek = data.get("nasobek")
        if flow is None or nasobek is None:
            return

        # Základem je cíl ze zadání, aby opakované klikání násobky neřetězilo.
        # Chybí-li (obchod z dřívější verze), stane se jím aktuální cíl.
        if not flow.original_profit_target:
            flow.original_profit_target = flow.profit_target
        novy_pt = flow.scaled_target(float(nasobek))

        try:
            await self.engine.change_profit_target(flow.id, novy_pt)
        except Exception as exc:
            ui.notify(str(exc), type="negative")
            return

        ui.notify(
            f"{flow.id}: cíl {flow.level_text('pt', novy_pt)} ({nasobek:g}× původní).",
            type="positive",
        )
        # Formulář může ukazovat tento obchod, hodnotu je třeba srovnat
        if self.form_flow_id == flow.id:
            pt_mode, _ = self._form_level_modes()
            self.pt_input.set_value(self._uroven_do_pole(novy_pt, pt_mode == MODE_PREMIUM))
        self._refresh()

    async def _on_set_sl(self, event: Any) -> None:
        """
        Přepne SL hlavní části na počáteční hodnotu, nebo na break even.
        Je-li úroveň už proražená, engine pozici rovnou prodá trhem.
        """
        data = event.args or {}
        flow = self.engine.flows.get(data.get("id", ""))
        rezim = data.get("rezim")
        if flow is None or rezim not in ("puvodni", "be"):
            return

        try:
            await self.engine.set_stop_loss(flow.id, rezim)
        except Exception as exc:
            ui.notify(str(exc), type="negative")
            return

        # Proražená úroveň znamená okamžitý prodej - hlásí se jako varování
        if flow.main_close_requested:
            ui.notify(f"{flow.id}: {flow.message}", type="warning")
        else:
            popis = "break even" if rezim == "be" else "počáteční"
            ui.notify(f"{flow.id}: SL {flow.level_text('sl')} ({popis}).", type="positive")
        # Formulář může ukazovat tento obchod, hodnotu je třeba srovnat
        if self.form_flow_id == flow.id:
            self._zapis_sl(flow)
        self._refresh()

    async def _on_set_runner_sl(self, event: Any) -> None:
        """Přepne SL runneru; hlavní části pozice se nedotýká."""
        data = event.args or {}
        flow = self.engine.flows.get(data.get("id", ""))
        rezim = data.get("rezim")
        if flow is None or rezim not in ("puvodni", "be"):
            return

        try:
            await self.engine.set_runner_stop_loss(flow.id, rezim)
        except Exception as exc:
            ui.notify(str(exc), type="negative")
            return

        if flow.runner_close_requested:
            ui.notify(f"{flow.id}: {flow.message}", type="warning")
        else:
            popis = "break even" if rezim == "be" else "počáteční"
            ui.notify(
                f"{flow.id}: SL runneru {flow.level_text('sl', flow.runner_sl)} ({popis}).",
                type="positive",
            )
        self._refresh()

    async def _on_runner_multiple(self, event: Any) -> None:
        """Zapne runner, nebo změní jeho cíl na zvolený násobek."""
        data = event.args or {}
        flow = self.engine.flows.get(data.get("id", ""))
        nasobek = data.get("nasobek")
        if flow is None or nasobek is None:
            return

        try:
            await self.engine.set_runner(flow.id, float(nasobek))
        except Exception as exc:
            ui.notify(str(exc), type="negative")
            return

        ui.notify(
            f"{flow.id}: runner {flow.runner_quantity} ks s cílem "
            f"{flow.level_text('pt', flow.runner_profit_target)} ({nasobek:g}×).",
            type="positive",
        )
        self._refresh()

    async def _on_runner_cancel(self, event: Any) -> None:
        """Vypne runner - zbude jeden PT a SL pro celou pozici."""
        data = event.args or {}
        flow = self.engine.flows.get(data.get("id", ""))
        if flow is None:
            return

        try:
            await self.engine.cancel_runner(flow.id)
        except Exception as exc:
            ui.notify(str(exc), type="negative")
            return

        ui.notify(f"{flow.id}: runner zrušen.", type="warning")
        self._refresh()

    async def _on_close_main(self, event: Any) -> None:
        """Prodá trhem hlavní část pozice; bez runneru celou pozici."""
        data = event.args or {}
        flow = self.engine.flows.get(data.get("id", ""))
        if flow is None:
            return

        try:
            await self.engine.close_main(flow.id)
        except Exception as exc:
            ui.notify(str(exc), type="negative")
            return

        ui.notify(f"{flow.id}: {flow.message}", type="warning")
        self._refresh()

    async def _on_close_runner(self, event: Any) -> None:
        """Prodá trhem runner; hlavní část pozice běží dál."""
        data = event.args or {}
        flow = self.engine.flows.get(data.get("id", ""))
        if flow is None:
            return

        try:
            await self.engine.close_runner(flow.id)
        except Exception as exc:
            ui.notify(str(exc), type="negative")
            return

        ui.notify(f"{flow.id}: {flow.message}", type="warning")
        self._refresh()

    async def _submit(self) -> None:
        """Odešle zadání obchodu do trhu."""
        symbol, entry, pt, sl = self._form_values()

        if not symbol:
            ui.notify("Zadejte ticker.", type="negative")
            return
        # Povinná je vstupní cena a aspoň jedna z úrovní - druhou dopočítá
        # engine podle poměru SL:PT, ať už je prvotní kterákoliv
        if entry is None or (pt is None and sl is None):
            ui.notify(
                "Zadejte vstupní cenu podkladu a alespoň jednu z úrovní PT / SL.",
                type="negative",
            )
            return

        # Úroveň zadaná procentem prémie se odesílá převedená na USD na
        # kontrakt - engine procento nezná. Cena opce, ze které se převádělo,
        # putuje s obchodem dál, aby se úroveň dala vrátit zpět do procent.
        # Převod potřebuje odhad z náhledu; bez něj nelze zadání sestavit
        pt_mode, sl_mode = self._form_level_modes()
        premie: float | None = None
        if MODE_PREMIUM in (pt_mode, sl_mode):
            premie = self.premium_used or self._premie_pro(symbol)
            if premie is None:
                ui.notify(
                    "Pro přepočet z procent prémie chybí cena opce - "
                    "načtěte nejdřív data z TWS tlačítkem Načíst.",
                    type="negative",
                )
                return
            pt, sl = self._urovne_v_usd(pt, sl, premie)

        quantity = int(self.qty_input.value) if self.qty_input.value else None
        max_spread = self._max_spread()
        pt_on, sl_on = self._form_modes()

        request = FlowRequest(
            symbol=symbol,
            entry_price=entry,
            profit_target=pt,
            stop_loss=sl,
            quantity=quantity,
            max_spread_pct=max_spread,
            pt_on_underlying=pt_on,
            sl_on_underlying=sl_on,
            sl_spread_compensated=self._form_sl_spread(),
            sl_to_pt_ratio=self._form_ratio(),
            pt_in_premium=pt_mode == MODE_PREMIUM,
            sl_in_premium=sl_mode == MODE_PREMIUM,
            premium_base=premie,
            primary_level=self._form_primary(),
            # Přepočet po otevření si obchod nese s sebou - formulář může být
            # mezitím přepsaný jiným zadáním
            refresh_after_open_sec=self._form_refresh_sec(),
        )

        # Založení obchodu si znovu načítá data z TWS, indikace platí i zde
        self._set_loading(True, "Zadávám obchod do trhu…")
        try:
            flow = await self.engine.start_flow(request)
        except Exception as exc:
            ui.notify(f"Zadání se nezdařilo: {exc}", type="negative")
            return
        finally:
            self._set_loading(False)

        self.form_flow_id = flow.id
        ui.notify(f"Flow {flow.id} založeno – {flow.state.label}.", type="positive")
        self._refresh()

    async def _volba_pro_pozici(self, flow: Flow) -> str | None:
        """
        Zeptá se, co udělat s otevřenou pozicí při rušení obchodu.
        Vrací 'zavrit', 'ponechat', nebo None při odmítnutí.
        """
        with ui.dialog() as dialog, ui.card().classes("dialog-pozice"):
            ui.label("Obchod drží otevřenou pozici").classes("dialog-nadpis")
            ui.label(
                f"{flow.option_label()} – {flow.filled_quantity or flow.quantity} ks "
                f"nakoupeno za {fmt(flow.fill_price)}."
            ).classes("dialog-text")
            ui.label(
                "Zrušením obchodu se odstraní i zajišťovací příkaz pro PT a SL. "
                "Rozhodněte, co se má stát s pozicí:"
            ).classes("dialog-text")

            with ui.column().classes("dialog-tlacitka"):
                ui.button(
                    "Uzavřít pozici trhem a ukončit obchod",
                    on_click=lambda: dialog.submit("zavrit"),
                ).props("color=red-8").classes("dialog-tlacitko")
                ui.button(
                    "Ponechat pozici otevřenou (zůstane bez zajištění)",
                    on_click=lambda: dialog.submit("ponechat"),
                ).props("outline color=orange-9").classes("dialog-tlacitko")
                ui.button("Nedělat nic", on_click=lambda: dialog.submit(None)).props(
                    "flat"
                ).classes("dialog-tlacitko")

        return await dialog

    async def _zrus(self, flow: Flow) -> bool:
        """
        Zruší obchod. Drží-li pozici, nejprve se zeptá, co s ní.
        Vrací False, pokud obchodník rušení odmítl.
        """
        zavrit = False
        if flow.fill_price is not None and flow.state.is_active:
            volba = await self._volba_pro_pozici(flow)
            if volba is None:
                return False
            zavrit = volba == "zavrit"

        await self.engine.cancel_flow(flow.id, close_position=zavrit)
        return True

    async def _cancel_by_symbol(self) -> None:
        """Zruší aktivní flow podle tickeru vyplněného ve formuláři."""
        symbol, entry, pt, _ = self._form_values()
        if not symbol:
            ui.notify("Zadejte ticker, jehož flow se má zrušit.", type="negative")
            return
        if not self.engine.active_flows_for(symbol):
            ui.notify(f"Pro ticker {symbol} neběží žádné aktivní flow.", type="negative")
            return
        # Směr rušeného obchodu určují ceny ve formuláři; long i short zároveň
        # bez vyplněných cen je nejednoznačný výběr
        _, _, _, sl = self._form_values()
        flow = self._bezici_pro_formular(symbol, entry, pt, sl)
        if flow is None:
            ui.notify(
                f"Na tickeru {symbol} běží long i short obchod - zrušte jej "
                f"tlačítkem v jeho řádku, nebo vyplňte vstup a PT.",
                type="negative",
            )
            return
        try:
            if not await self._zrus(flow):
                return
        except Exception as exc:
            ui.notify(str(exc), type="negative")
            return
        ui.notify(f"Flow {flow.id}: {flow.state.label}.", type="warning")
        self._refresh()

    async def _on_cancel_flow(self, event: Any) -> None:
        """Zruší obchod tlačítkem přímo v jeho řádku tabulky."""
        data = event.args or {}
        flow = self.engine.flows.get(data.get("id", ""))
        if flow is None:
            return
        try:
            if not await self._zrus(flow):
                return
        except Exception as exc:
            ui.notify(str(exc), type="negative")
            return
        ui.notify(f"Flow {flow.id}: {flow.state.label}.", type="warning")
        self._refresh()

    def _on_remove_flow(self, event: Any) -> None:
        """Odstraní ukončený obchod z přehledu tlačítkem v jeho řádku."""
        data = event.args or {}
        flow = self.engine.flows.get(data.get("id", ""))
        if flow is None:
            return
        try:
            self.engine.remove_flow(flow.id)
        except Exception as exc:
            ui.notify(str(exc), type="negative")
            return
        # Formulář už nemá na co odkazovat, pokud ukazoval právě tento obchod
        if self.form_flow_id == flow.id:
            self.form_flow_id = None
        self._refresh()

    def _on_remove_untraded(self) -> None:
        """
        Odstraní z přehledu obchody bez nákupu - zrušené a propásnuté.
        Ostatní řádky (čekající, otevřené, uzavřené) zůstávají, proto se
        na akci neptáme: nic s výsledkem se ztratit nemůže.
        """
        try:
            odstraneno = self.engine.remove_untraded()
        except Exception as exc:
            ui.notify(str(exc), type="negative")
            return

        if not odstraneno:
            ui.notify("Žádný zrušený ani propásnutý obchod v přehledu není.", type="info")
            return

        # Formulář už nemá na co odkazovat, pokud ukazoval odstraněný obchod
        if self.form_flow_id and self.form_flow_id not in self.engine.flows:
            self.form_flow_id = None
        ui.notify(f"Z přehledu odstraněno {odstraneno} obchodů bez nákupu.", type="warning")
        self._refresh()

    async def _potvrd_vycisteni(self, bezici: int, s_pozici: int, celkem: int) -> bool:
        """
        Vyžádá si potvrzení hromadného zrušení a vymazání přehledu.
        Vrací True, pokud obchodník akci potvrdil.
        """
        with ui.dialog() as dialog, ui.card().classes("dialog-pozice"):
            ui.label("Zrušit a smazat všechny obchody").classes("dialog-nadpis")
            ui.label(
                f"Zruší se {bezici} běžících obchodů včetně jejich příkazů v TWS "
                f"a z přehledu zmizí všech {celkem} položek."
            ).classes("dialog-text")

            # Otevřená pozice hromadné vyčištění přežije - na to je potřeba
            # upozornit dřív, než se obchod z přehledu ztratí
            if s_pozici:
                ui.label(
                    f"POZOR: {s_pozici} obchodů drží otevřenou pozici. Zajišťovací "
                    f"příkazy pro PT a SL se zruší, ale pozice zůstanou v TWS "
                    f"otevřené a bez zajištění - uzavřete je ručně."
                ).classes("dialog-text dialog-varovani")

            with ui.column().classes("dialog-tlacitka"):
                ui.button(
                    "Zrušit a smazat vše",
                    on_click=lambda: dialog.submit(True),
                ).props("color=red-8").classes("dialog-tlacitko")
                ui.button("Zpět", on_click=lambda: dialog.submit(False)).props(
                    "flat"
                ).classes("dialog-tlacitko")

        return bool(await dialog)

    async def _on_clear_all(self) -> None:
        """Zruší všechny obchody a vyprázdní monitorovací přehled."""
        flows = list(self.engine.flows.values())
        if not flows:
            ui.notify("Monitoring obchodů je prázdný.", type="info")
            return

        bezici = [flow for flow in flows if flow.state.is_active]
        s_pozici = [flow for flow in bezici if flow.fill_price is not None]
        if not await self._potvrd_vycisteni(len(bezici), len(s_pozici), len(flows)):
            return

        try:
            zruseno, smazano = await self.engine.cancel_and_clear_all()
        except Exception as exc:
            ui.notify(str(exc), type="negative")
            return

        # Formulář už nemá na co odkazovat, přehled je prázdný
        self.form_flow_id = None
        ui.notify(
            f"Zrušeno {zruseno} běžících obchodů, smazáno {smazano} položek.",
            type="warning",
        )
        self._refresh()

    # ------------------------------------------------------------------
    # Periodická aktualizace
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        """Aktualizuje stav spojení, tabulku obchodů a log."""
        self._refresh_warning()
        self._refresh_status()
        self._refresh_market_open()
        self._refresh_pending_cancel()
        self._refresh_auto_close()
        self._refresh_table()
        self._refresh_log()
        self._refresh_config()
        # Otevřené popupy tikají živě spolu s tabulkou: přehled výsledků
        # i importní dialog, kterému se tím obnovují zámky zadaných řádků
        self.report_dialog.refresh()
        self.import_dialog.refresh()
        self._refresh_plan()

    def _refresh_market_open(self) -> None:
        """Odpočet do otevření burzy v hlavičce - během seance se skrývá."""
        sekundy = self.engine.market_open_seconds()
        if sekundy is None:
            self.market_open_label.set_visibility(False)
            return

        self.market_open_label.set_visibility(True)
        self.market_open_label.set_text(f"Otevření trhu za {format_countdown(sekundy)}")

    def _refresh_plan(self) -> None:
        """
        Signalizace naplánovaného zadání pozic ze souboru v hlavičce.

        Dialog si popis skládá sám - zná odpočet i počet vybraných pozic;
        prázdný popis znamená, že režim neběží a pruh se skryje.
        """
        popis = self.import_dialog.plan_popis()
        if popis is None:
            self.plan_label.set_visibility(False)
            return

        self.plan_label.set_visibility(True)
        self.plan_label.set_text(popis)

    def _refresh_window_label(
        self, label: ui.label, sekundy: float | None, cekani: str, behem: str
    ) -> None:
        """
        Vykreslí odpočet do okna, které engine hlásí počtem sekund.

        None okno skryje, nula a méně znamená, že okno běží - pak se ukáže
        zvýrazněný text "behem", jinak text "cekani" doplněný o odpočet.
        """
        if sekundy is None:
            label.set_visibility(False)
            return

        label.set_visibility(True)
        if sekundy <= 0:
            label.set_text(behem)
            label.classes(add="odpocet-aktivni")
            return

        label.set_text(f"{cekani} {format_countdown(sekundy)}")
        label.classes(remove="odpocet-aktivni")

    def _refresh_pending_cancel(self) -> None:
        """Odpočet do zrušení čekajících obchodů v hlavičce."""
        # Rušicí okno trvá až do zavření burzy - zvýrazněný text připomíná,
        # že se zruší i obchod zadaný teprve teď
        self._refresh_window_label(
            self.pending_cancel_label,
            self.engine.pending_cancel_seconds(),
            "Zrušení čekajících obchodů za",
            f"Čekající obchody se ruší (od {self.cfg.trading.pending_cancel_time})",
        )

    def _refresh_auto_close(self) -> None:
        """Odpočet do automatického uzavření obchodů v hlavičce."""
        self._refresh_window_label(
            self.auto_close_label,
            self.engine.auto_close_seconds(),
            "Automatické uzavření všech pozic za",
            "Probíhá automatické uzavírání obchodů",
        )

    def _refresh_warning(self) -> None:
        """Zobrazí upozornění na opční pozice, které aplikace neřídí."""
        pozice = self.engine.unmanaged
        # Pruh se překresluje jen při změně, aby se obsah zbytečně nezahazoval
        if set(pozice) == self.last_unmanaged:
            return
        self.last_unmanaged = set(pozice)

        self.warning_bar.clear()
        self.warning_bar.set_visibility(bool(pozice))
        if not pozice:
            return

        with self.warning_bar:
            for info in pozice.values():
                ui.label(f"POZOR: {self.engine.unmanaged_text(info)}").classes("pruh-text")

    def _refresh_status(self) -> None:
        """Zobrazí aktuální stav spojení s TWS."""
        conn = self.cfg.connection
        if self.ib.connected:
            self.status_label.set_text(
                f"Připojeno {conn.host}:{conn.port} | účet {self.ib.account or '-'}"
            )
            self.status_label.classes(add="spojeni-ok", remove="spojeni-chyba")
            self.connect_button.set_text("Odpojit")
        else:
            self.status_label.set_text(f"Odpojeno ({conn.host}:{conn.port})")
            self.status_label.classes(add="spojeni-chyba", remove="spojeni-ok")
            self.connect_button.set_text("Připojit")
        self._refresh_link()

    def _refresh_link(self) -> None:
        """
        Ukazatel kvality spojení v hlavičce - odezva TWS a stáří tržních dat.
        Bez spojení nemá co ukazovat, proto se skrývá.
        """
        if not self.ib.connected:
            self.link_label.set_visibility(False)
            return

        stari = self.ib.quotes_age()
        self.link_label.set_visibility(True)
        self.link_label.set_text(stav_linky_text(self.ib.rtt_ms, stari))

        # Trh je otevřený, když odpočet do jeho otevření nemá co ukazovat
        trh_otevren = self.engine.market_open_seconds() is None
        if linka_varuje(self.ib.rtt_ms, stari, trh_otevren):
            self.link_label.classes(add="linka-varovani")
        else:
            self.link_label.classes(remove="linka-varovani")

    async def _measure_link(self) -> None:
        """
        Změří odezvu TWS. Běží vlastním, řidším tempem než obnova hlavičky -
        na rozdíl od ní jde o skutečný dotaz do TWS.
        """
        await self.ib.measure_rtt()

    def _row(self, flow: Flow) -> dict[str, Any]:
        """Převede flow na řádek monitorovací tabulky."""
        # Sloupec P/L ukazuje jen dosud otevřenou část pozice; celkový
        # výsledek obchodu zůstává v závěrečné hlášce po uzavření.
        # Hlavní hodnota je po odečtení provize zaplacené za nákup těchto
        # kusů, v závorce tatáž částka bez ní (prodejní provize vznikne
        # až prodejem, takže v otevřené pozici ještě není)
        pnl = flow.open_pnl_net
        pnl_hruby = flow.open_pnl

        # Zvýrazní se tlačítko odpovídající aktuálnímu násobku cíle
        aktualni = flow.pt_multiple
        aktivni_nasobek = None
        if aktualni is not None:
            for nabidnuty in PT_MULTIPLES:
                if abs(aktualni - nabidnuty) < 0.01:
                    aktivni_nasobek = nabidnuty
                    break

        # Totéž pro runner; jeho sekce se zobrazuje jen tehdy, když je pozice
        # větší než velikost runneru - jinak není co dělit
        runner_nasobek = None
        runner_aktualni = flow.runner_multiple
        if runner_aktualni is not None:
            for nabidnuty in PT_MULTIPLES:
                if abs(runner_aktualni - nabidnuty) < 0.01:
                    runner_nasobek = nabidnuty
                    break
        # Sekce Cíl mizí, jakmile hlavní část přestane běžet - po jejím prodeji
        # nebo během uzavírání trhem už cíl nemá co řídit
        cil_mozny = (
            flow.state.is_active
            and flow.state != FlowState.CLOSING
            and flow.exit_fill_price is None
            and not flow.main_close_requested
        )
        lze_uzavrit = (
            flow.state == FlowState.EXIT_ARMED
            and flow.fill_price is not None
            and flow.exit_fill_price is None
            and not flow.main_close_requested
        )
        lze_uzavrit_runner = (
            flow.state == FlowState.EXIT_ARMED
            and flow.runner_active
            and flow.runner_order_id is not None
            and flow.runner_fill_price is None
            and not flow.runner_close_requested
        )
        # Zvýraznění tlačítek SL: 'be' při stopu na break even (na podkladu
        # vstupní cena, na opci nulová ztráta), 'puvodni' při stopu ze zadání;
        # jiná (ruční) hodnota nezvýrazní žádné
        zaklad_sl = flow.original_stop_loss if flow.original_sl_known else flow.stop_loss
        be_sl = flow.break_even_sl
        aktivni_sl = None
        if abs(flow.stop_loss - be_sl) < 0.005:
            aktivni_sl = "be"
        elif abs(flow.stop_loss - zaklad_sl) < 0.005:
            aktivni_sl = "puvodni"
        aktivni_runner_sl = None
        if flow.runner_active:
            if abs(flow.runner_sl - be_sl) < 0.005:
                aktivni_runner_sl = "be"
            elif abs(flow.runner_sl - zaklad_sl) < 0.005:
                aktivni_runner_sl = "puvodni"
        runner_velikost = (
            flow.runner_quantity if flow.runner_active else self.cfg.trading.runner_quantity
        )
        # Sekce Runner mizí, jakmile přestane dávat smysl: runner je prodaný,
        # právě se uzavírá, nebo se uzavírá pozice a runner ještě nebyl zapnut
        runner_mozny = (
            flow.state.is_active
            and flow.state != FlowState.CLOSING
            and flow.held_quantity > runner_velikost
            and flow.runner_fill_price is None
            and not flow.runner_close_requested
            and (flow.runner_active or not flow.main_close_requested)
        )
        return {
            "id": flow.id,
            "live": flow.state.is_active and self.engine.is_monitoring,
            # Akce celého obchodu vpravo: běžící lze zrušit, ukončený odstranit
            "lze_zrusit": flow.state.is_active,
            "lze_odstranit": not flow.state.is_active,
            "cil_mozny": cil_mozny,
            "runner_mozny": runner_mozny,
            "runner_aktivni": flow.runner_active,
            "aktivni_runner_nasobek": runner_nasobek,
            # Tlačítka okamžitého uzavření - jen u částí, které skutečně běží
            "lze_uzavrit": lze_uzavrit,
            "lze_uzavrit_runner": lze_uzavrit_runner,
            # Přepínání SL má smysl až u nakoupené pozice, resp. běžícího runneru -
            # proto sdílí podmínky s tlačítky okamžitého uzavření
            "sl_mozny": lze_uzavrit,
            "runner_sl_mozny": lze_uzavrit_runner,
            "aktivni_sl": aktivni_sl,
            "aktivni_runner_sl": aktivni_runner_sl,
            "runner_lze_zrusit": (
                flow.runner_active
                and flow.runner_fill_price is None
                and not flow.runner_close_requested
                and not flow.main_close_requested
                and flow.exit_fill_price is None
            ),
            "nasobky": list(PT_MULTIPLES),
            "aktivni_nasobek": aktivni_nasobek,
            "symbol": flow.symbol,
            "contract": f"{flow.right_label} {flow.expiration} @ {flow.strike:g}",
            # Směr obchodu: CALL čeká růst podkladu (long), PUT pokles (short)
            "smer": "LONG" if flow.right == "C" else "SHORT",
            "smer_class": "smer-long" if flow.right == "C" else "smer-short",
            # Zadané množství / kontrakty právě otevřené v trhu (např. 4/3)
            "qty": f"{flow.quantity}/{flow.open_quantity}",
            "entry": fmt(flow.entry_price),
            "fill": fmt(flow.fill_price),
            # Liší-li se cíl runneru od hlavního, ukazují se oba (stejně jako u SL).
            # Úroveň na opci se ukazuje jako zisk/ztráta v USD, po nákupu i cena opce
            "pt": flow.level_text("pt")
            + (
                f" · R {flow.level_text('pt', flow.runner_profit_target)}"
                if flow.runner_active
                and flow.runner_fill_price is None
                and abs(flow.runner_profit_target - flow.profit_target) >= 0.005
                else ""
            ),
            # Liší-li se SL runneru od hlavního, ukazují se oba
            "sl": flow.level_text("sl")
            + (
                f" · R {flow.level_text('sl', flow.runner_sl)}"
                if flow.runner_active
                and flow.runner_fill_price is None
                and abs(flow.runner_sl - flow.stop_loss) >= 0.005
                else ""
            ),
            "underlying": fmt(flow.underlying_price),
            "quote": f"{fmt(flow.option_bid)} / {fmt(flow.option_ask)}",
            "spread": fmt(flow.option_spread_pct, 2, " %"),
            "spread_limit": fmt(flow.max_spread_pct, 2, " %"),
            "exp_profit": fmt(flow.expected_profit),
            "exp_loss": fmt(flow.expected_loss),
            "pnl": pnl_text(pnl, pnl_hruby),
            "state": flow.state.label,
            "state_class": flow.state.css_class,
            # Třída pro barevné odlišení zisku a ztráty
            "pnl_class": "zisk" if (pnl or 0) > 0 else ("ztrata" if (pnl or 0) < 0 else ""),
        }

    def _refresh_table(self) -> None:
        """Překreslí monitorovací tabulku podle aktuálních dat enginu."""
        self.table.rows = [self._row(flow) for flow in self.engine.sorted_flows()]
        self.table.update()

    def _refresh_log(self) -> None:
        """
        Vypíše posledních několik událostí aplikace.
        Překresluje se pouze při nové události, aby seznam zbytečně neblikal.
        """
        events = list(self.engine.events)[:40]
        newest = events[0][0] if events else None
        if newest == self.last_log_stamp:
            return
        self.last_log_stamp = newest

        self.log_area.clear()
        with self.log_area:
            for timestamp, message in events:
                ui.label(f"{timestamp:%H:%M:%S}  {message}").classes("log-radek")

    def _refresh_config(self) -> None:
        """Zobrazí podstatná nastavení z konfiguračního souboru."""
        t = self.cfg.trading
        e = self.cfg.expiration
        expiration_text = e.fixed_date if e.mode == "fixed" else f"nejbližší (min. {e.min_dte} dní)"
        # U velikosti účtu se uvádí, zda pochází z konfigurace, nebo z TWS
        if self.cfg.account.size > 0:
            ucet = f"{fmt(self.engine.account_size)} USD (config)"
        elif self.engine.account_size > 0:
            ucet = f"{fmt(self.engine.account_size)} USD (z TWS)"
        else:
            ucet = "čeká se na hodnotu z TWS"

        # Výchozí režim PT a SL ze zaškrtávátek formuláře a prvotní úroveň
        rezim_pt = "podklad" if t.pt_on_underlying else "opce (USD/ks)"
        rezim_sl = "podklad" if t.sl_on_underlying else "opce (USD/ks)"
        prvotni = "SL (PT se dopočítá)" if t.primary_level == "sl" else "PT (SL se dopočítá)"
        self.config_label.set_text(
            f"Účet {ucet} | risk {self.cfg.account.risk_pct:g} % "
            f"= {fmt(self.engine.risk_amount)} USD\n"
            f"Nákup: {t.entry_order_type} (tolerance {t.ask_tolerance_pct:g} %) | "
            f"prodej: {t.exit_order_type}\n"
            f"Max. spread {t.max_spread_pct:g} % | "
            f"SL:PT {t.sl_to_pt_ratio:g} (RRR {rrr_z_pomeru(t.sl_to_pt_ratio):g}) | "
            f"expirace {expiration_text}\n"
            f"Výchozí PT: {rezim_pt} | výchozí SL: {rezim_sl} | prvotní: {prvotni}"
        )


def create_ui(cfg: AppConfig, engine: FlowEngine, ib: IBService) -> None:
    """Zaregistruje statické soubory a hlavní stránku aplikace."""
    # Keš se u lokální aplikace vypíná, aby se úpravy stylů projevily ihned po obnovení stránky
    app.add_static_files("/static", str(STATIC_DIR), max_cache_age=0)

    # Bubliny s nápovědou vyskakují nad prvkem, ne pod ním - pod poli formuláře
    # by zakrývaly další pole a v tabulce řádek s tlačítky. Šířku omezuje CSS,
    # takže se delší text zalomí do více řádků místo jednoho dlouhého pruhu
    ui.tooltip.default_props('anchor="top middle" self="bottom middle"')

    def uvolni_nahled(_: Any = None) -> None:
        """
        Po zavření okna prohlížeče uvolní odběry tržních dat, které drží
        poslední připravený náhled zadání.

        Bez toho by kontrakty zůstaly odebírané až do konce běhu aplikace:
        náhled se jinak uvolňuje jedině tím, že jej nahradí novější, a ten
        už po odchodu obchodníka nemá kdo vyžádat. Nové otevření stránky si
        náhled připraví (a odběry založí) znovu.
        """
        engine.release_preview()

    app.on_disconnect(uvolni_nahled)

    @ui.page("/")
    def index() -> None:
        """Hlavní stránka - každý klient dostane vlastní instanci ovládacích prvků."""
        TradingUI(cfg, engine, ib).build()
