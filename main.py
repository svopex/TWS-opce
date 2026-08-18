"""
Vstupní bod aplikace pro obchodování opcí přes Interactive Brokers TWS API.
Spouští webové rozhraní (NiceGUI) a monitorovací smyčku obchodních flow.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from nicegui import app, ui

from tws_opce.config import load_config
from tws_opce.engine import FlowEngine
from tws_opce.ib_service import IBService
from tws_opce.ui import create_ui

log = logging.getLogger("tws_opce")


def setup_logging(verbose: bool) -> None:
    """Nastaví logování do konzole; s přepínačem --verbose i podrobné logy z ib_async."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Knihovna ib_async je při běhu velmi upovídaná, pokud není požadován podrobný výpis
    if not verbose:
        logging.getLogger("ib_async").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    """Zpracuje parametry příkazové řádky."""
    parser = argparse.ArgumentParser(description="Obchodování opcí přes TWS API")
    parser.add_argument(
        "-c",
        "--config",
        default=str(Path(__file__).parent / "config.yaml"),
        help="cesta ke konfiguračnímu souboru (výchozí: config.yaml)",
    )
    parser.add_argument("--verbose", action="store_true", help="podrobné logování")
    parser.add_argument(
        "--no-connect",
        action="store_true",
        help="nepřipojovat se k TWS při startu (spojení lze navázat tlačítkem v UI)",
    )
    return parser.parse_args()


def main() -> None:
    """Sestaví aplikaci a spustí webový server."""
    args = parse_args()
    setup_logging(args.verbose)

    cfg = load_config(args.config)
    ib = IBService(cfg)
    engine = FlowEngine(cfg, ib)
    create_ui(cfg, engine, ib)

    # Při --no-connect se aplikace k TWS nepřipojuje ani automaticky ve smyčce
    engine.auto_connect = not args.no_connect

    async def on_startup() -> None:
        """Po startu serveru naváže spojení s TWS a spustí monitoring."""
        if not args.no_connect:
            try:
                await ib.connect()
                engine.log_event("Aplikace spuštěna, spojení s TWS navázáno.")
                # Obchody z předchozího běhu se obnoví a ověří proti TWS
                await engine.restore()
            except Exception as exc:
                # Bez spojení aplikace běží dál, uživatel se může připojit z rozhraní
                log.error("Spojení s TWS se při startu nezdařilo: %s", exc)
                engine.log_event(f"Spojení s TWS se nezdařilo: {exc}")
        engine.start()

    async def on_shutdown() -> None:
        """Při ukončení zastaví monitoring a korektně uzavře spojení."""
        await engine.stop()
        await ib.disconnect()

    app.on_startup(on_startup)
    app.on_shutdown(on_shutdown)

    ui.run(
        host=cfg.ui.host,
        port=cfg.ui.port,
        title="Obchodování opcí – TWS",
        dark=cfg.ui.dark,
        reload=False,
        show=False,
        favicon="📈",
    )


# NiceGUI spouští modul znovu, proto se používá podmínka pro hlavní i __mp_main__ běh
if __name__ in {"__main__", "__mp_main__"}:
    main()
