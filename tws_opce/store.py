"""
Ukládání a načítání stavu obchodů na disk.

Stav se zapisuje po každé změně, aby po restartu nebo pádu aplikace nezůstaly
obchody bez dozoru. Uložený soubor slouží pouze jako vodítko - skutečný stav
příkazů a pozic se vždy ověřuje proti TWS.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Flow, FlowState

log = logging.getLogger(__name__)

# Verze formátu souboru - při nekompatibilní změně se uložený stav ignoruje
FORMAT_VERSION = 1

# Pole flow, která se ukládají. Tržní data (kotace, delta) se neukládají,
# protože se po startu načtou znovu z TWS.
SAVED_FIELDS = (
    "id",
    "symbol",
    "entry_price",
    "profit_target",
    "original_profit_target",
    "stop_loss",
    "original_stop_loss",
    "quantity",
    "max_spread_pct",
    "right",
    "pt_on_underlying",
    "sl_on_underlying",
    "expiration",
    "strike",
    "option_conid",
    "underlying_conid",
    "min_tick",
    "message",
    "entry_limit",
    "entry_order_id",
    "exit_order_id",
    "exit_sl_order_id",
    "fill_price",
    "filled_quantity",
    "exit_fill_price",
    "exit_reason",
    "main_sold_quantity",
    "main_sold_value",
    "runner_profit_target",
    "runner_quantity",
    "runner_stop_loss",
    "runner_order_id",
    "runner_sl_order_id",
    "runner_fill_price",
    "runner_sold_quantity",
    "runner_realized_pnl",
    "main_close_requested",
    "runner_close_requested",
    "entry_cancel_requested",
)

# Pole s časovým údajem se ukládají v textovém tvaru ISO
TIME_FIELDS = ("created_at", "updated_at", "fill_time", "blocked_since")


def flow_to_dict(flow: Flow) -> dict[str, Any]:
    """Převede flow na slovník vhodný k uložení."""
    data: dict[str, Any] = {name: getattr(flow, name) for name in SAVED_FIELDS}
    data["state"] = flow.state.value
    for name in TIME_FIELDS:
        hodnota = getattr(flow, name)
        data[name] = hodnota.isoformat() if isinstance(hodnota, datetime) else None
    return data


def dict_to_flow(data: dict[str, Any]) -> Flow:
    """Sestaví flow z uloženého slovníku."""
    kwargs = {name: data.get(name) for name in SAVED_FIELDS if data.get(name) is not None}
    flow = Flow(**kwargs)

    # Stav se obnoví z uloženého zápisu, neznámý stav se považuje za chybový
    try:
        flow.state = FlowState(data.get("state", ""))
    except ValueError:
        flow.state = FlowState.ERROR
        flow.message = "Uložený stav obchodu nebylo možné rozpoznat."

    # Starší uložený stav pole nezná; bez něj by se násobky cíle počítaly
    # z už posunuté hodnoty a při každém kliknutí by se řetězily
    if not flow.original_profit_target:
        flow.original_profit_target = flow.profit_target

    # Totéž pro počáteční SL - tlačítko "Počáteční SL" se bez něj nemá kam vracet
    if not flow.original_stop_loss:
        flow.original_stop_loss = flow.stop_loss

    for name in TIME_FIELDS:
        hodnota = data.get(name)
        if hodnota:
            try:
                setattr(flow, name, datetime.fromisoformat(hodnota))
            except ValueError:
                pass

    return flow


def save(flows: list[Flow], path: str | Path) -> None:
    """
    Uloží stav obchodů do souboru.
    Zápis probíhá přes dočasný soubor a přejmenování, aby při pádu aplikace
    nezůstal soubor rozepsaný.
    """
    cesta = Path(path)
    obsah = {
        "version": FORMAT_VERSION,
        "saved_at": datetime.now().isoformat(),
        "flows": [flow_to_dict(f) for f in flows],
    }

    try:
        cesta.parent.mkdir(parents=True, exist_ok=True)
        # Dočasný soubor musí ležet ve stejném adresáři, aby šlo přejmenovat atomicky
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=cesta.parent, prefix=cesta.name, suffix=".tmp", delete=False
        ) as fh:
            json.dump(obsah, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
            docasny = fh.name
        os.replace(docasny, cesta)
    except Exception:
        log.exception("Stav obchodů se nepodařilo uložit do %s.", cesta)


def load(path: str | Path) -> list[Flow]:
    """
    Načte uložený stav obchodů.
    Chybějící, poškozený nebo neznámou verzí zapsaný soubor vrací prázdný seznam.
    """
    cesta = Path(path)
    if not cesta.exists():
        return []

    try:
        with cesta.open("r", encoding="utf-8") as fh:
            obsah = json.load(fh)
    except Exception:
        log.exception("Uložený stav v %s se nepodařilo načíst.", cesta)
        return []

    if obsah.get("version") != FORMAT_VERSION:
        log.warning("Uložený stav v %s má neznámou verzi formátu - ignoruji jej.", cesta)
        return []

    flows: list[Flow] = []
    for zaznam in obsah.get("flows", []):
        try:
            flows.append(dict_to_flow(zaznam))
        except Exception:
            log.exception("Záznam obchodu se nepodařilo obnovit: %s", zaznam)
    return flows
