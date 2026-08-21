"""
Společné základy testů - konfigurace a příprava enginu s náhradou TWS.

Sdílí je test_engine, test_rezimy i test_obnova, aby se stejná příprava
nepsala v každém souboru znovu a případná změna platila všude naráz.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fake_ib import FakeIBService
from tws_opce.config import AppConfig
from tws_opce.engine import FlowEngine


def vychozi_config() -> AppConfig:
    """
    Konfigurace pro testy - účet 5000 USD, risk 1 %, limit spreadu 5 %.
    Stav se nezapisuje na disk a automatické uzavírání před koncem burzy
    je vypnuté; jinak by sada spuštěná těsně před zavřením obchody uzavírala.
    """
    cfg = AppConfig()
    cfg.account.size = 5000.0
    cfg.account.risk_pct = 1.0
    cfg.trading.max_spread_pct = 5.0
    cfg.trading.entry_order_type = "LMT_ASK"
    cfg.trading.ask_tolerance_pct = 2.0
    cfg.state.enabled = False
    cfg.trading.auto_close_enabled = False
    return cfg


class ZakladEnginu(unittest.IsolatedAsyncioTestCase):
    """Engine s náhradou TWS a bez zápisu stavu na disk."""

    def setUp(self) -> None:
        self.cfg = vychozi_config()
        self.ib = FakeIBService(self.cfg)
        self.engine = FlowEngine(self.cfg, self.ib)


class ZakladSeStavem(unittest.IsolatedAsyncioTestCase):
    """
    Engine se zapnutým ukládáním stavu do dočasného souboru.
    Používají jej testy obnovy po restartu - druhý engine sdílí náhradu TWS,
    takže vidí stejné příkazy i pozice jako ten původní.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = AppConfig()
        self.cfg.state.file = str(Path(self.tmp.name) / "state.json")
        # Testy si čas burzy řídí samy
        self.cfg.trading.auto_close_enabled = False
        self.ib = FakeIBService(self.cfg)
        self.engine = FlowEngine(self.cfg, self.ib)

    async def asyncSetUp(self) -> None:
        # Aplikace po připojení k TWS vždy nejprve obnoví stav
        await self.engine.restore()

    def tearDown(self) -> None:
        self.tmp.cleanup()
