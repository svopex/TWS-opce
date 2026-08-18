"""Webové rozhraní aplikace postavené na NiceGUI - formulář a monitorovací tabulka."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from nicegui import app, ui

from .config import AppConfig
from .engine import FlowEngine, Preview
from .ib_service import IBService
from .models import Flow, FlowRequest, FlowState

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Přiřazení CSS třídy jednotlivým stavům flow kvůli barevnému odlišení v tabulce
STATE_CLASSES = {
    FlowState.NEW: "stav-ceka",
    FlowState.ARMED: "stav-ceka",
    FlowState.SPREAD_BLOCKED: "stav-blokovano",
    FlowState.NO_QUOTES: "stav-blokovano",
    FlowState.FILLED: "stav-nakoupeno",
    FlowState.EXIT_ARMED: "stav-nakoupeno",
    FlowState.CLOSING: "stav-uzavira",
    FlowState.CLOSED: "stav-uzavreno",
    FlowState.MISSED: "stav-propasnuto",
    FlowState.CANCELLED: "stav-zruseno",
    FlowState.ERROR: "stav-chyba",
}

# Definice sloupců monitorovací tabulky
TABLE_COLUMNS = [
    {"name": "live", "label": "", "field": "live", "align": "center"},
    {"name": "symbol", "label": "Ticker", "field": "symbol", "align": "left", "sortable": True},
    {"name": "contract", "label": "Kontrakt", "field": "contract", "align": "left"},
    {"name": "qty", "label": "Ks", "field": "qty", "align": "right"},
    {"name": "underlying", "label": "Podklad", "field": "underlying", "align": "right"},
    {"name": "entry", "label": "Vstup", "field": "entry", "align": "right"},
    {"name": "fill", "label": "Nákup za", "field": "fill", "align": "right"},
    {"name": "pt", "label": "PT", "field": "pt", "align": "right"},
    {"name": "sl", "label": "SL", "field": "sl", "align": "right"},
    {"name": "exp_profit", "label": "Zisk na PT", "field": "exp_profit", "align": "right"},
    {"name": "exp_loss", "label": "Ztráta na SL", "field": "exp_loss", "align": "right"},
    {"name": "quote", "label": "Bid / Ask", "field": "quote", "align": "right"},
    {"name": "spread", "label": "Spread", "field": "spread", "align": "right"},
    {"name": "spread_limit", "label": "Max. spread", "field": "spread_limit", "align": "right"},
    {"name": "pnl", "label": "P/L", "field": "pnl", "align": "right"},
    {"name": "state", "label": "Stav", "field": "state", "align": "left"},
]

# Obecná šablona buňky - zvýrazní všechny buňky vybraného obchodu
CELL_SLOT = """
<q-td :props="props" :class="props.row.vybrany ? 'bunka-vybrana' : ''">
  {{ props.value }}
</q-td>
"""

# Šablona buňky s indikátorem hlídání - tepe jen u obchodů, které aplikace
# skutečně sleduje; u ukončených i při nefunkčním monitoringu zůstane prázdná
LIVE_SLOT = """
<q-td :props="props" :class="props.row.vybrany ? 'bunka-vybrana' : ''">
  <span v-if="props.row.live" class="puntik-hlidani">
    <q-tooltip>Aplikace obchod hlídá</q-tooltip>
  </span>
</q-td>
"""

# Šablona buňky se stavem - barva se řídí třídou uloženou v řádku
STATE_SLOT = """
<q-td :props="props" :class="props.row.vybrany ? 'bunka-vybrana' : ''">
  <span :class="'odznak ' + props.row.state_class">{{ props.value }}</span>
</q-td>
"""

# Šablony buněk s očekávaným výsledkem obchodu při dosažení PT a SL
PROFIT_SLOT = """
<q-td :props="props" :class="'text-right ' + (props.row.vybrany ? 'bunka-vybrana' : '')">
  <span class="zisk">{{ props.value }}</span>
</q-td>
"""

LOSS_SLOT = """
<q-td :props="props" :class="'text-right ' + (props.row.vybrany ? 'bunka-vybrana' : '')">
  <span class="ztrata">{{ props.value }}</span>
</q-td>
"""

# Šablona buňky se ziskem/ztrátou - zelená při zisku, červená při ztrátě
PNL_SLOT = """
<q-td :props="props" :class="'text-right ' + (props.row.vybrany ? 'bunka-vybrana' : '')">
  <span :class="props.row.pnl_class">{{ props.value }}</span>
</q-td>
"""


def css_href() -> str:
    """
    URL stylopisu doplněná o čas jeho poslední úpravy.
    Prohlížeč tak po změně stylů načte novou verzi místo té z keše.
    """
    css_file = STATIC_DIR / "styles.css"
    stamp = int(css_file.stat().st_mtime) if css_file.exists() else 0
    return f"/static/styles.css?v={stamp}"


def fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    """Naformátuje číslo pro tabulku, při chybějící hodnotě vrátí pomlčku."""
    if value is None:
        return "-"
    return f"{value:,.{digits}f}{suffix}".replace(",", " ")


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
        ui.add_head_html(f'<link rel="stylesheet" href="{css_href()}">')

        # Vybrané flow v monitorovací tabulce (drží se zvlášť pro každého klienta)
        self.selected_id: str | None = None
        self.preview: Preview | None = None
        # Čas poslední vypsané události - log se překresluje jen při změně
        self.last_log_stamp: datetime | None = None
        # Pořadové číslo přípravy zadání - rozlišuje souběžně běžící požadavky
        self.preview_seq: int = 0
        # Ticker, ke kterému patří hodnoty ve formuláři
        self.last_symbol: str | None = None
        # Naposledy zobrazené pozice bez dozoru; None znamená, že pruh ještě nebyl
        # vykreslen - prázdná množina je platný stav a nesmí se s tím zaměnit
        self.last_unmanaged: set[int] | None = None

        self._build_header()

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

    def _build_header(self) -> None:
        """Hlavička s názvem aplikace a stavem spojení na TWS."""
        with ui.header().classes("hlavicka"):
            ui.label("Obchodování opcí – TWS").classes("nazev")
            ui.space()
            self.status_label = ui.label().classes("stav-spojeni")
            self.connect_button = ui.button("Připojit", on_click=self._toggle_connection).props("flat")

    def _build_form(self) -> None:
        """Formulář pro zadání obchodu."""
        with ui.card().classes("karta"):
            ui.label("Zadání obchodu").classes("nadpis-sekce")

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

            with ui.row().classes("radek"):
                self.entry_input = (
                    ui.number("Vstup na podkladu", format="%.2f")
                    .classes("pole")
                    .props("outlined dense step=any")
                )
                self.pt_input = (
                    ui.number("PT na podkladu", format="%.2f")
                    .classes("pole")
                    .props("outlined dense step=any")
                )

            with ui.row().classes("radek"):
                self.sl_input = (
                    ui.number("SL (nepovinné)", format="%.2f")
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

            with ui.row().classes("radek"):
                self.qty_input = (
                    ui.number("Množství [ks]", value=None, format="%.0f", step=1, min=1)
                    .classes("pole")
                    .props("outlined dense")
                )
                ui.button("Přepočítat", on_click=lambda: self._load_preview("prepocitat")).props(
                    "outline"
                ).classes("tlacitko-vedle").tooltip(
                    "Přepíše SL a množství vypočtenými hodnotami. SL podle poměru k PT "
                    "z konfigurace, množství podle rizika a delty opce."
                )

            # Opuštění pole jen obnoví načtená data; hodnoty ve formuláři zůstávají
            for field_widget in (self.entry_input, self.pt_input, self.sl_input):
                field_widget.on("blur", lambda _: self._load_preview("auto"))

            # Přehled vypočtených parametrů obchodu
            with ui.column().classes("nahled"):
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
                ui.button("Zrušit vybraný", on_click=self._cancel_selected).props("outline dense")
                ui.button("Odstranit z přehledu", on_click=self._remove_selected).props(
                    "outline dense"
                )

            self.table = (
                ui.table(columns=TABLE_COLUMNS, rows=[], row_key="id")
                .classes("tabulka")
                .props('dense flat no-data-label="Zatím nebyl zadán žádný obchod."')
            )
            self.table.add_slot("body-cell", CELL_SLOT)
            self.table.add_slot("body-cell-live", LIVE_SLOT)
            self.table.add_slot("body-cell-state", STATE_SLOT)
            self.table.add_slot("body-cell-pnl", PNL_SLOT)
            self.table.add_slot("body-cell-exp_profit", PROFIT_SLOT)
            self.table.add_slot("body-cell-exp_loss", LOSS_SLOT)
            # Klik na řádek přepne formulář na daný obchod
            self.table.on("rowClick", self._on_row_click)

            self.detail_label = ui.label("Kliknutím na řádek přepnete na daný obchod.").classes(
                "detail-radku"
            )

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

    def _form_values(self) -> tuple[str, float | None, float | None, float | None]:
        """Přečte hodnoty z formuláře a převede je na čísla."""
        symbol = (self.symbol_input.value or "").upper().strip()
        entry = float(self.entry_input.value) if self.entry_input.value not in (None, "") else None
        pt = float(self.pt_input.value) if self.pt_input.value not in (None, "") else None
        sl = float(self.sl_input.value) if self.sl_input.value not in (None, "") else None
        return symbol, entry, pt, sl

    def _fill_from_flow(self, flow: Flow) -> None:
        """Naplní formulář parametry existujícího obchodu."""
        self.last_symbol = flow.symbol
        self.selected_id = flow.id
        self.symbol_input.set_value(flow.symbol)
        self.entry_input.set_value(round(flow.entry_price, 2))
        self.pt_input.set_value(round(flow.profit_target, 2))
        self.sl_input.set_value(round(flow.stop_loss, 2))
        self.spread_input.set_value(flow.max_spread_pct)
        self.qty_input.set_value(flow.quantity)

    def _clear_inputs(self) -> None:
        """
        Vyprázdní ceny a množství ve formuláři.
        Volá se při přechodu na jiný ticker, aby se do nového zadání
        nepřenesly hodnoty dříve načteného obchodu.
        """
        for pole in (self.entry_input, self.pt_input, self.sl_input, self.qty_input):
            pole.set_value(None)
        # Limit spreadu se vrací na hodnotu z konfigurace
        self.spread_input.set_value(self.cfg.trading.max_spread_pct)

        # Výběr v monitoringu se ruší, protože se už netýká rozepsaného zadání
        self.selected_id = None
        self.preview = None
        self.preview_detail.set_text("")
        self.preview_warning.set_text("")

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

        bezici = self.engine.active_flow_for(symbol)
        zmena_tickeru = symbol != self.last_symbol

        # Načtení se vyžaduje buď tlačítkem, nebo přechodem na jiný ticker
        if bezici is not None and (rezim == "nacist" or zmena_tickeru):
            self._fill_from_flow(bezici)
            entry, pt, sl = bezici.entry_price, bezici.profit_target, bezici.stop_loss
            ui.notify(f"Načten běžící obchod {bezici.id}.", type="info")
        elif bezici is None and zmena_tickeru:
            # Ticker bez obchodu - hodnoty předchozího se nesmí přenést
            self._clear_inputs()
            entry = pt = sl = None
        self.last_symbol = symbol

        if not self.ib.connected:
            self.preview_label.set_text("Není navázáno spojení s TWS.")
            return

        # Kliknutí na tlačítko vyvolá i opuštění právě editovaného pole, takže
        # mohou běžet dvě přípravy najednou. Zapisuje se jen výsledek té poslední.
        self.preview_seq += 1
        pozadavek = self.preview_seq

        try:
            preview = await self.engine.prepare(
                symbol, entry, pt, None if rezim == "prepocitat" else sl
            )
        except Exception as exc:
            if pozadavek != self.preview_seq:
                return
            self.preview = None
            self.preview_label.set_text(f"Chyba přípravy zadání: {exc}")
            self.preview_detail.set_text("")
            self.preview_warning.set_text("")
            return

        if pozadavek != self.preview_seq:
            return

        self.preview = preview
        self._apply_preview(preview, rezim)

    def _apply_preview(self, preview: Preview, rezim: str) -> None:
        """Promítne připravený obchod do formuláře a informačního panelu."""
        # Bez vybraného kontraktu nejsou SL ani množství k dispozici
        if preview.expiration:
            if rezim == "prepocitat":
                # Výslovný přepočet přepíše obě pole vypočtenými hodnotami
                self.sl_input.set_value(round(preview.stop_loss, 2))
                self.qty_input.set_value(preview.quantity)
            else:
                # Načtení pouze doplní dosud nevyplněná pole, ruční zadání ponechá
                if self.sl_input.value in (None, "") and preview.stop_loss:
                    self.sl_input.set_value(round(preview.stop_loss, 2))
                if not self.qty_input.value:
                    self.qty_input.set_value(preview.quantity)

        if preview.current_price is None:
            self.preview_label.set_text(f"{preview.symbol}: cena podkladu zatím není k dispozici.")
        elif not preview.expiration:
            self.preview_label.set_text(
                f"{preview.symbol}: aktuální cena {fmt(preview.current_price)} – "
                f"doplňte vstupní cenu a PT."
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
            detail_parts.append(f"delta {delta_text}")
            detail_parts.append(f"doporučeno: SL {fmt(preview.stop_loss)}, {preview.quantity} ks")
            detail_parts.append(
                f"risk {fmt(preview.risk_amount)} USD z účtu {fmt(preview.account_size)} USD"
            )
        self.preview_detail.set_text(" | ".join(detail_parts))
        self.preview_warning.set_text(" ".join(preview.warnings))

    async def _submit(self) -> None:
        """Odešle zadání obchodu do trhu."""
        symbol, entry, pt, sl = self._form_values()

        if not symbol:
            ui.notify("Zadejte ticker.", type="negative")
            return
        if entry is None or pt is None:
            ui.notify("Zadejte vstupní cenu podkladu a PT.", type="negative")
            return

        quantity = int(self.qty_input.value) if self.qty_input.value else None
        max_spread = float(self.spread_input.value) if self.spread_input.value else None

        request = FlowRequest(
            symbol=symbol,
            entry_price=entry,
            profit_target=pt,
            stop_loss=sl,
            quantity=quantity,
            max_spread_pct=max_spread,
        )

        try:
            flow = await self.engine.start_flow(request)
        except Exception as exc:
            ui.notify(f"Zadání se nezdařilo: {exc}", type="negative")
            return

        self.selected_id = flow.id
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
        symbol, _, _, _ = self._form_values()
        if not symbol:
            ui.notify("Zadejte ticker, jehož flow se má zrušit.", type="negative")
            return
        flow = self.engine.active_flow_for(symbol)
        if flow is None:
            ui.notify(f"Pro ticker {symbol} neběží žádné aktivní flow.", type="negative")
            return
        try:
            if not await self._zrus(flow):
                return
        except Exception as exc:
            ui.notify(str(exc), type="negative")
            return
        ui.notify(f"Flow {flow.id}: {flow.state.label}.", type="warning")
        self._refresh()

    async def _cancel_selected(self) -> None:
        """Zruší flow vybrané v monitorovací tabulce."""
        if not self.selected_id:
            ui.notify("Nejprve vyberte řádek v tabulce.", type="negative")
            return
        flow = self.engine.flows.get(self.selected_id)
        if flow is None:
            ui.notify("Vybraný obchod již neexistuje.", type="negative")
            return
        try:
            if not await self._zrus(flow):
                return
        except Exception as exc:
            ui.notify(str(exc), type="negative")
            return
        ui.notify(f"Flow {flow.id}: {flow.state.label}.", type="warning")
        self._refresh()

    def _remove_selected(self) -> None:
        """Odstraní ukončené flow z přehledu."""
        if not self.selected_id:
            ui.notify("Nejprve vyberte řádek v tabulce.", type="negative")
            return
        try:
            self.engine.remove_flow(self.selected_id)
        except Exception as exc:
            ui.notify(str(exc), type="negative")
            return
        self.selected_id = None
        self._refresh()

    def _on_row_click(self, event: Any) -> None:
        """
        Přepnutí na obchod klikem v tabulce.
        Quasar posílá v argumentech událost, data řádku a jeho pořadí.
        """
        args = event.args or []
        row = args[1] if len(args) > 1 else None
        if not isinstance(row, dict):
            return

        flow = self.engine.flows.get(row.get("id", ""))
        if flow is None:
            return

        # Parametry vybraného obchodu se načtou zpět do formuláře
        self._fill_from_flow(flow)
        self._refresh()

    # ------------------------------------------------------------------
    # Periodická aktualizace
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        """Aktualizuje stav spojení, tabulku obchodů, detail a log."""
        self._refresh_warning()
        self._refresh_status()
        self._refresh_table()
        self._refresh_detail()
        self._refresh_log()
        self._refresh_config()

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

    def _row(self, flow: Flow) -> dict[str, Any]:
        """Převede flow na řádek monitorovací tabulky."""
        pnl = flow.unrealized_pnl
        return {
            "id": flow.id,
            "live": flow.state.is_active and self.engine.is_monitoring,
            "symbol": flow.symbol,
            "contract": f"{flow.right_label} {flow.expiration} @ {flow.strike:g}",
            "qty": flow.quantity,
            "entry": fmt(flow.entry_price),
            "fill": fmt(flow.fill_price),
            "pt": fmt(flow.profit_target),
            "sl": fmt(flow.stop_loss),
            "underlying": fmt(flow.underlying_price),
            "quote": f"{fmt(flow.option_bid)} / {fmt(flow.option_ask)}",
            "spread": fmt(flow.option_spread_pct, 2, " %"),
            "spread_limit": fmt(flow.max_spread_pct, 2, " %"),
            "exp_profit": fmt(flow.expected_profit),
            "exp_loss": fmt(flow.expected_loss),
            "pnl": fmt(pnl) if pnl is not None else "-",
            "state": flow.state.label,
            "state_class": STATE_CLASSES.get(flow.state, ""),
            # Třída pro barevné odlišení zisku a ztráty
            "pnl_class": "zisk" if (pnl or 0) > 0 else ("ztrata" if (pnl or 0) < 0 else ""),
            # Vybraný obchod se v tabulce zvýrazňuje, aby bylo zřejmé, čeho se týkají akce
            "vybrany": flow.id == self.selected_id,
        }

    def _refresh_table(self) -> None:
        """Překreslí monitorovací tabulku podle aktuálních dat enginu."""
        self.table.rows = [self._row(flow) for flow in self.engine.sorted_flows()]
        self.table.update()

    def _refresh_detail(self) -> None:
        """Zobrazí popis stavu vybraného obchodu pod tabulkou."""
        if not self.selected_id:
            self.detail_label.set_text("Kliknutím na řádek přepnete na daný obchod.")
            return
        flow = self.engine.flows.get(self.selected_id)
        if flow is None:
            self.detail_label.set_text("Vybraný obchod již neexistuje.")
            return
        self.detail_label.set_text(f"{flow.id} | {flow.option_label()} | {flow.message}")

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

        self.config_label.set_text(
            f"Účet {ucet} | risk {self.cfg.account.risk_pct:g} % "
            f"= {fmt(self.engine.risk_amount)} USD\n"
            f"Nákup: {t.entry_order_type} (tolerance {t.ask_tolerance_pct:g} %) | "
            f"prodej: {t.exit_order_type}\n"
            f"Max. spread {t.max_spread_pct:g} % | SL:PT {t.sl_to_pt_ratio:g} | "
            f"expirace {expiration_text}"
        )


def create_ui(cfg: AppConfig, engine: FlowEngine, ib: IBService) -> None:
    """Zaregistruje statické soubory a hlavní stránku aplikace."""
    # Keš se u lokální aplikace vypíná, aby se úpravy stylů projevily ihned po obnovení stránky
    app.add_static_files("/static", str(STATIC_DIR), max_cache_age=0)

    @ui.page("/")
    def index() -> None:
        """Hlavní stránka - každý klient dostane vlastní instanci ovládacích prvků."""
        TradingUI(cfg, engine, ib).build()
