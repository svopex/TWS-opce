"""
Testy hlídání Order Efficiency Ratio (OER).

IBKR očekává za den (příkazy + úpravy + zrušení) / (vyplněné příkazy + 1)
nejvýš kolem 20. Ověřuje se samotné počítadlo, započítávání zpráv
ve službě TWS, uložení počítadel přes restart a validace konfigurace.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import Execution, Fill

from tests.fake_ib import FakeIBService
from tests.zaklad import ZakladSeStavem
from tws_opce import store
from tws_opce.config import AppConfig, validate_config
from tws_opce.engine import FlowEngine
from tws_opce.models import Flow, FlowRequest
from tws_opce.oer import OrderEfficiency

BURZA = ZoneInfo("America/New_York")


def vyplneni(perm_id: int = 0, order_id: int = 0, cas: datetime | None = None) -> Fill:
    """Vyplnění příkazu s daným permId / orderId a časem (výchozí teď)."""
    return Fill(
        contract=None,
        execution=Execution(execId=f"E-{perm_id}-{order_id}", permId=perm_id, orderId=order_id),
        commissionReport=None,
        time=cas or datetime.now(timezone.utc),
    )


class TestPocitadla(unittest.TestCase):
    """Samotné počítadlo zpráv a vyplněných příkazů."""

    def setUp(self) -> None:
        # Hodiny si test posouvá sám - středa 19. 8. 2026, 10:00 čas burzy
        self.ted = datetime(2026, 8, 19, 10, 0, tzinfo=BURZA)
        self.oer = OrderEfficiency(15.0, BURZA, now=lambda: self.ted)

    def test_vypocet_podle_vzorce_ibkr(self):
        self.oer.record_message(30)
        self.oer.record_executions(
            [vyplneni(perm_id=1, cas=self.ted), vyplneni(perm_id=2, cas=self.ted)]
        )
        # 30 zpráv / (2 vyplněné + 1) = 10
        self.assertAlmostEqual(self.oer.ratio, 10.0)

    def test_castecne_vyplneni_se_pocita_jednou(self):
        # Tentýž příkaz vyplněný po částech má víc exekucí se stejným permId
        self.oer.record_executions(
            [vyplneni(perm_id=7, cas=self.ted), vyplneni(perm_id=7, cas=self.ted)]
        )
        self.oer.record_executions([vyplneni(perm_id=7, cas=self.ted)])
        self.assertEqual(self.oer.executed, 1)

    def test_bez_perm_id_rozhoduje_order_id(self):
        self.oer.record_executions(
            [
                vyplneni(order_id=3, cas=self.ted),
                vyplneni(order_id=3, cas=self.ted),
                vyplneni(order_id=4, cas=self.ted),
            ]
        )
        self.assertEqual(self.oer.executed, 2)

    def test_vyplneni_z_jineho_dne_se_nepocita(self):
        vcera = self.ted - timedelta(days=1)
        self.oer.record_executions([vyplneni(perm_id=1, cas=vcera)])
        self.assertEqual(self.oer.executed, 0)

    def test_limit_s_rezervou(self):
        # Limit 15 bez vyplnění = 15 zpráv celkem, rezerva se odečítá
        self.oer.record_message(10)
        self.assertTrue(self.oer.allows(3, reserve=2))
        self.assertFalse(self.oer.allows(3, reserve=3))
        # Vyplněný příkaz přidá prostor pro dalších 15 zpráv
        self.oer.record_executions([vyplneni(perm_id=1, cas=self.ted)])
        self.assertTrue(self.oer.allows(18, reserve=2))

    def test_nulovy_limit_hlidani_vypina(self):
        oer = OrderEfficiency(0.0, BURZA, now=lambda: self.ted)
        oer.record_message(1000)
        self.assertFalse(oer.enabled)
        self.assertTrue(oer.allows(100, reserve=100))

    def test_novy_den_pocitadla_vynuluje(self):
        self.oer.record_message(12)
        self.oer.record_executions([vyplneni(perm_id=1, cas=self.ted)])

        # Den se určuje v časové zóně burzy - 23:59 je ještě týž den
        self.ted = datetime(2026, 8, 19, 23, 59, tzinfo=BURZA)
        self.assertEqual(self.oer.messages, 12)

        self.ted = datetime(2026, 8, 20, 0, 1, tzinfo=BURZA)
        self.assertEqual(self.oer.messages, 0)
        self.assertEqual(self.oer.executed, 0)

    def test_ulozeni_a_obnova_tehoz_dne(self):
        self.oer.record_message(9)
        self.oer.record_executions([vyplneni(perm_id=5, cas=self.ted)])
        data = self.oer.to_dict()

        # Obnova se slučuje se zprávami napočítanými před ní
        obnovene = OrderEfficiency(15.0, BURZA, now=lambda: self.ted)
        obnovene.record_message(2)
        obnovene.load(data)
        self.assertEqual(obnovene.messages, 11)
        self.assertEqual(obnovene.executed, 1)

    def test_zaznam_z_jineho_dne_se_zahodi(self):
        self.oer.record_message(9)
        data = self.oer.to_dict()

        zitra = OrderEfficiency(
            15.0, BURZA, now=lambda: self.ted + timedelta(days=1)
        )
        zitra.load(data)
        self.assertEqual(zitra.messages, 0)

    def test_poskozeny_zaznam_se_ignoruje(self):
        for data in (None, "nic", {"day": "neplatne"}, {"day": "2026-08-19", "messages": "x"}):
            self.oer.load(data)
        self.assertEqual(self.oer.messages, 0)


class TestZapoctuVeSluzbe(unittest.TestCase):
    """Každé odeslání, úprava i zrušení příkazu se započte jako zpráva."""

    def setUp(self) -> None:
        self.ib = FakeIBService(AppConfig())

    def prikaz(self):
        """Nový tržní příkaz odeslaný přes službu."""
        return self.ib.place(SimpleNamespace(conId=1), self.ib.market_sell_order(1, "TEST"))

    def test_zadani_a_uprava_jsou_zpravy(self):
        trade = self.prikaz()
        # Odeslání se stejným orderId je úprava - pro IBKR další zpráva
        self.ib.place(trade.contract, trade.order)
        self.assertEqual(self.ib.oer.messages, 2)

    def test_zruseni_aktivniho_prikazu_je_zprava(self):
        trade = self.prikaz()
        self.ib.cancel(trade)
        self.assertEqual(self.ib.oer.messages, 2)

    def test_neodeslane_zruseni_se_nepocita(self):
        trade = self.prikaz()
        # Už zrušený příkaz se znovu neruší
        self.ib.cancel(trade)
        self.ib.cancel(trade)
        # Bez spojení se zrušení do TWS nedostane
        druhy = self.prikaz()
        self.ib.connected_flag = False
        self.ib.cancel(druhy)
        self.assertEqual(self.ib.oer.messages, 3)

    def test_vyplneni_prevezme_ze_seznamu_exekuci(self):
        trade = self.prikaz()
        self.ib.fill(trade, 1, 3.00)
        self.ib.refresh_executions()
        self.assertEqual(self.ib.oer.executed, 1)


class TestUlozeniPresRestart(ZakladSeStavem):
    """Počítadla OER se ukládají se stavem a restart je nevynuluje."""

    async def test_restart_pocitadla_zachova(self):
        self.ib.price_underlying = 230.0
        await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        self.assertEqual(self.ib.oer.messages, 1)
        self.assertEqual(store.load_order_stats(self.cfg.state.file)["messages"], 1)

        # Nový engine s novou službou (restart aplikace) počítadla převezme
        nova_sluzba = FakeIBService(self.cfg)
        nova_sluzba.placed = self.ib.placed
        novy = FlowEngine(self.cfg, nova_sluzba)
        await novy.restore()
        self.assertEqual(nova_sluzba.oer.messages, 1)

    async def test_pocet_odstraneni_kvuli_spreadu_se_ulozi(self):
        flow = Flow(
            id="AAPL-1",
            symbol="AAPL",
            entry_price=232.0,
            profit_target=235.0,
            stop_loss=229.0,
            quantity=1,
            max_spread_pct=7.0,
        )
        flow.spread_breaches = 4
        store.save([flow], self.cfg.state.file)
        self.assertEqual(store.load(self.cfg.state.file)[0].spread_breaches, 4)


class TestValidaceKonfigurace(unittest.TestCase):
    """Nové volby hlídání OER a prodlevy návratu do trhu."""

    def test_vychozi_hodnoty_projdou(self):
        validate_config(AppConfig())

    def test_neplatne_hodnoty_se_odmitnou(self):
        for nazev, hodnota in (
            ("oer_limit", -1.0),
            ("rearm_delay_max_sec", -5.0),
            ("refresh_increase_margin_pct", 100.0),
            ("refresh_increase_margin_pct", -1.0),
        ):
            cfg = AppConfig()
            setattr(cfg.trading, nazev, hodnota)
            with self.subTest(nazev=nazev, hodnota=hodnota):
                with self.assertRaises(ValueError):
                    validate_config(cfg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
