"""
Testy režimů zadání PT a SL: na podkladu (podmíněný příkaz), nebo ziskem /
ztrátou v USD na kontrakt (příkaz přímo na cenu opce) - a jejich kombinací,
kdy vznikají dva prodejní příkazy a po vyplnění jednoho se druhý ruší.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fake_ib import OPTION_CONID, UNDERLYING_CONID, FakeIBService
from tws_opce.config import AppConfig
from tws_opce.engine import FlowEngine
from tws_opce.models import FlowRequest, FlowState


def vychozi_config() -> AppConfig:
    """Konfigurace pro testy - účet 5000 USD, risk 1 %, limit spreadu 5 %."""
    cfg = AppConfig()
    cfg.account.size = 5000.0
    cfg.account.risk_pct = 1.0
    cfg.trading.max_spread_pct = 5.0
    cfg.state.enabled = False
    cfg.trading.auto_close_enabled = False
    return cfg


class ZakladRezimu(unittest.IsolatedAsyncioTestCase):
    """Společná příprava enginu s náhradou TWS a pomocné kroky scénářů."""

    def setUp(self) -> None:
        self.cfg = vychozi_config()
        self.ib = FakeIBService(self.cfg)
        self.engine = FlowEngine(self.cfg, self.ib)

    async def zaloz(self, pt_on: bool, sl_on: bool, pt: float, sl: float | None = None, **zmeny):
        """
        Založí CALL obchod: podklad 230, vstup 232. PT a SL v zadaném režimu
        (na podkladu cena, na opci USD na kontrakt).
        """
        self.ib.price_underlying = 230.0
        pozadavek = FlowRequest(
            symbol="AAPL",
            entry_price=232.0,
            profit_target=pt,
            stop_loss=sl,
            pt_on_underlying=pt_on,
            sl_on_underlying=sl_on,
        )
        for klic, hodnota in zmeny.items():
            setattr(pozadavek, klic, hodnota)
        return await self.engine.start_flow(pozadavek)

    async def nakup(self, flow, mnozstvi: int, cena: float = 3.00) -> None:
        """Simuluje vyplnění nákupu a zadání zajišťovacích příkazů."""
        self.ib.fill(flow.entry_trade, mnozstvi, cena)
        await self.engine._tick()
        await self.engine._tick()

    def prodeje(self) -> list:
        """Všechny prodejní příkazy odeslané do náhrady TWS."""
        return [t for t in self.ib.placed if t.order.action == "SELL"]


class TestZadaniNaOpci(ZakladRezimu):
    """Příprava a založení obchodu s PT / SL zadanými na opci."""

    async def test_oba_na_podkladu_zustava_jako_dosud(self):
        flow = await self.zaloz(True, True, 235.0)
        self.assertTrue(flow.pt_on_underlying)
        self.assertTrue(flow.sl_on_underlying)
        self.assertFalse(flow.exit_split)
        self.assertEqual(flow.strike, 235.0)
        self.assertAlmostEqual(flow.stop_loss, 229.0)

    async def test_pt_na_opci_vybere_strike_na_odvozene_urovni(self):
        # Zisk 10 USD = posun ceny opce o 0,10; podklad se musí pohnout jen
        # o zlomek bodu, takže cílová úroveň leží těsně nad vstupem 232
        # a strike je nejbližší k ní (232,5), nikoliv někde u 235 jako při PT 235
        flow = await self.zaloz(False, True, 10.0)
        self.assertFalse(flow.pt_on_underlying)
        self.assertEqual(flow.strike, 232.5)
        self.assertEqual(flow.state, FlowState.ARMED)
        # Nákupní příkaz se nemění - stále jedna podmínka na vstup
        prikaz = self.ib.placed[0].order
        self.assertEqual(prikaz.action, "BUY")
        self.assertEqual(len(prikaz.conditions), 1)
        self.assertAlmostEqual(prikaz.conditions[0].price, 232.0)

    async def test_vetsi_zisk_posune_strike_dal(self):
        # Zisk 300 USD = posun ceny opce o 3 body vyžaduje výrazný pohyb
        # podkladu - strike se vybírá dál od vstupu
        flow = await self.zaloz(False, False, 300.0)
        self.assertGreater(flow.strike, 232.5)

    async def test_bez_kotaci_se_strike_odvodi_z_delty(self):
        # Bez ceny opce nelze použít model - zbývá lineární odhad přes deltu
        self.ib.price_bid = None
        self.ib.price_ask = None
        self.ib.greek_delta = 0.5
        self.cfg.trading.entry_order_type = "MKT"
        flow = await self.zaloz(False, False, 100.0)
        # Posun ceny opce 1,00 / delta 0,5 = 2 body -> cíl 234, strike 235
        self.assertEqual(flow.strike, 235.0)

    async def test_sl_na_opci_se_dopocita_z_pt_na_opci_pomerem(self):
        self.cfg.trading.sl_to_pt_ratio = 0.5
        flow = await self.zaloz(False, False, 10.0)
        self.assertFalse(flow.sl_on_underlying)
        self.assertAlmostEqual(flow.stop_loss, 5.0)

    async def test_zadany_sl_na_opci_ma_prednost(self):
        flow = await self.zaloz(False, False, 10.0, 7.0)
        self.assertAlmostEqual(flow.stop_loss, 7.0)

    async def test_mnozstvi_ze_ztraty_na_opci(self):
        # Riziko 50 USD / ztráta 10 USD na kontrakt = 5 kontraktů
        flow = await self.zaloz(False, False, 10.0, 10.0)
        self.assertEqual(flow.quantity, 5)

    async def test_smiseny_rezim_dopocita_sl_na_opci_z_pt_na_podkladu(self):
        # PT 235 na podkladu, SL na opci: očekávaný zisk na PT v USD krát poměr.
        # Výsledek musí být kladná částka úměrná pohybu 3 bodů.
        flow = await self.zaloz(True, False, 235.0)
        self.assertFalse(flow.sl_on_underlying)
        self.assertGreater(flow.stop_loss, 0)
        self.assertLess(flow.stop_loss, 300.0)

    async def test_smiseny_rezim_dopocita_sl_na_podkladu_z_pt_na_opci(self):
        # PT 10 USD na opci, SL na podkladu: úroveň pod vstupem, kde opce
        # ztratí 10 USD - těsně pod vstupem 232
        flow = await self.zaloz(False, True, 10.0)
        self.assertTrue(flow.sl_on_underlying)
        self.assertLess(flow.stop_loss, 232.0)
        self.assertGreater(flow.stop_loss, 228.0)

    async def test_zaporny_zisk_na_opci_se_odmitne(self):
        with self.assertRaises(ValueError):
            await self.zaloz(False, True, -5.0)

    async def test_zaporna_ztrata_na_opci_se_odmitne(self):
        with self.assertRaises(ValueError):
            await self.zaloz(True, False, 235.0, -5.0)

    async def test_smer_bez_urovni_na_podkladu_urci_poloha_vstupu(self):
        # Vstup nad aktuální cenou = CALL, pod ní = PUT
        call = await self.zaloz(False, False, 10.0)
        self.assertEqual(call.right, "C")
        self.ib.price_underlying = 230.0
        put = await self.engine.start_flow(
            FlowRequest(
                symbol="MSFT",
                entry_price=228.0,
                profit_target=10.0,
                pt_on_underlying=False,
                sl_on_underlying=False,
            )
        )
        self.assertEqual(put.right, "P")

    async def test_smer_urci_sl_na_podkladu_kdyz_pt_je_na_opci(self):
        # SL 229 pod vstupem 232 prozrazuje long - obchod s pozicí stejného
        # směru se chrání před nahrazením
        flow = await self.zaloz(False, True, 10.0, 229.0)
        await self.nakup(flow, 1)
        with self.assertRaises(ValueError):
            await self.zaloz(False, True, 10.0, 229.0)

    async def test_ocekavany_vysledek_na_opci_je_primo_castka(self):
        flow = await self.zaloz(False, False, 10.0, 8.0, quantity=3)
        await self.engine._tick()
        self.assertAlmostEqual(flow.expected_profit, 30.0)
        self.assertAlmostEqual(flow.expected_loss, -24.0)


class TestPrikazyPoNakupu(ZakladRezimu):
    """Podoba prodejních příkazů po nákupu podle kombinace režimů."""

    async def test_pt_podklad_sl_opce(self):
        flow = await self.zaloz(True, False, 235.0, 10.0, quantity=2)
        await self.nakup(flow, 2, 3.00)
        self.assertEqual(flow.state, FlowState.EXIT_ARMED)

        prodeje = self.prodeje()
        self.assertEqual(len(prodeje), 2)
        pt, sl = flow.exit_trade, flow.exit_sl_trade
        self.assertIn(pt, prodeje)
        self.assertIn(sl, prodeje)

        # PT: tržní příkaz s jedinou podmínkou na podkladu
        self.assertEqual(pt.order.orderType, "MKT")
        self.assertEqual(len(pt.order.conditions), 1)
        self.assertTrue(pt.order.conditions[0].isMore)
        self.assertAlmostEqual(pt.order.conditions[0].price, 235.0)
        self.assertEqual(pt.order.conditions[0].conId, UNDERLYING_CONID)
        self.assertTrue(pt.order.orderRef.endswith(":exit"))

        # SL: stop-market na ceně opce 3,00 - 0,10
        self.assertEqual(sl.order.orderType, "STP")
        self.assertAlmostEqual(sl.order.auxPrice, 2.90)
        self.assertEqual(sl.order.conditions, [])
        self.assertTrue(sl.order.orderRef.endswith(":exitsl"))

        # Oba příkazy na celé množství a ve společné OCA skupině
        self.assertEqual(int(pt.order.totalQuantity), 2)
        self.assertEqual(int(sl.order.totalQuantity), 2)
        self.assertTrue(pt.order.ocaGroup)
        self.assertEqual(pt.order.ocaGroup, sl.order.ocaGroup)
        self.assertEqual(pt.order.ocaType, 2)
        self.assertEqual(sl.order.ocaType, 2)

    async def test_pt_opce_sl_podklad(self):
        flow = await self.zaloz(False, True, 10.0, 229.0)
        await self.nakup(flow, 1, 3.00)

        pt, sl = flow.exit_trade, flow.exit_sl_trade
        # PT: limit na ceně opce 3,00 + 0,10
        self.assertEqual(pt.order.orderType, "LMT")
        self.assertAlmostEqual(pt.order.lmtPrice, 3.10)
        self.assertEqual(pt.order.conditions, [])
        # SL: tržní příkaz s jedinou podmínkou na podkladu
        self.assertEqual(sl.order.orderType, "MKT")
        self.assertEqual(len(sl.order.conditions), 1)
        self.assertFalse(sl.order.conditions[0].isMore)
        self.assertAlmostEqual(sl.order.conditions[0].price, 229.0)

    async def test_pt_opce_sl_opce(self):
        flow = await self.zaloz(False, False, 10.0, 10.0)
        await self.nakup(flow, 1, 3.00)

        pt, sl = flow.exit_trade, flow.exit_sl_trade
        self.assertEqual(pt.order.orderType, "LMT")
        self.assertAlmostEqual(pt.order.lmtPrice, 3.10)
        self.assertEqual(sl.order.orderType, "STP")
        self.assertAlmostEqual(sl.order.auxPrice, 2.90)
        self.assertEqual(pt.order.ocaGroup, sl.order.ocaGroup)

    async def test_oba_na_podkladu_jeden_prikaz_bez_oca(self):
        flow = await self.zaloz(True, True, 235.0)
        await self.nakup(flow, 1, 3.00)

        self.assertEqual(len(self.prodeje()), 1)
        self.assertIsNone(flow.exit_sl_trade)
        self.assertEqual(len(flow.exit_trade.order.conditions), 2)
        self.assertFalse(flow.exit_trade.order.ocaGroup)

    async def test_ceny_se_zaokrouhluji_na_tik(self):
        # Tik 0,05: 3,00 + 0,07 = 3,07 -> 3,05; 3,00 - 0,07 = 2,93 -> 2,95
        flow = await self.zaloz(False, False, 7.0, 7.0)
        await self.nakup(flow, 1, 3.00)
        self.assertAlmostEqual(flow.exit_trade.order.lmtPrice, 3.05)
        self.assertAlmostEqual(flow.exit_sl_trade.order.auxPrice, 2.95)

    async def test_ztrata_vetsi_nez_premie_da_stop_na_jednom_tiku(self):
        flow = await self.zaloz(True, False, 235.0, 500.0)
        await self.nakup(flow, 1, 3.00)
        self.assertAlmostEqual(flow.exit_sl_trade.order.auxPrice, 0.05)

    async def test_stav_popisuje_oba_prikazy(self):
        flow = await self.zaloz(False, False, 10.0, 10.0)
        await self.nakup(flow, 1, 3.00)
        self.assertIn("dva příkazy", flow.message)
        self.assertIn("limitem", flow.message)
        self.assertIn("stop-marketem", flow.message)


class TestVyplneniJednohoZDvojice(ZakladRezimu):
    """Po vyplnění jednoho příkazu se druhý ruší a obchod se uzavře."""

    async def test_vyplneni_pt_zrusi_sl(self):
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=2)
        await self.nakup(flow, 2, 3.00)
        sl = flow.exit_sl_trade

        self.ib.fill(flow.exit_trade, 2, 3.10)
        await self.engine._tick()

        self.assertIn(sl, self.ib.cancelled)
        self.assertEqual(flow.state, FlowState.CLOSED)
        self.assertEqual(flow.exit_reason, "PT")
        self.assertAlmostEqual(flow.exit_fill_price, 3.10)
        # Zisk 0,10 * 2 ks * 100 = 20 USD
        self.assertAlmostEqual(flow.unrealized_pnl, 20.0)

    async def test_vyplneni_sl_zrusi_pt(self):
        flow = await self.zaloz(True, False, 235.0, 10.0)
        await self.nakup(flow, 1, 3.00)
        pt = flow.exit_trade

        self.ib.fill(flow.exit_sl_trade, 1, 2.88)
        await self.engine._tick()

        self.assertIn(pt, self.ib.cancelled)
        self.assertEqual(flow.state, FlowState.CLOSED)
        self.assertEqual(flow.exit_reason, "SL")
        self.assertAlmostEqual(flow.exit_fill_price, 2.88)

    async def test_pt_leg_se_pozna_i_kdyz_jej_tws_uz_zrusila(self):
        # TWS přes OCA zruší druhý příkaz sama - aplikace to nesmí brát
        # za zrušení mimo aplikaci a hlásit chybu
        flow = await self.zaloz(False, False, 10.0, 10.0)
        await self.nakup(flow, 1, 3.00)
        flow.exit_sl_trade.orderStatus.status = "Cancelled"
        self.ib.fill(flow.exit_trade, 1, 3.10)

        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.CLOSED)

    async def test_oba_zrusene_bez_vyplneni_je_chyba(self):
        flow = await self.zaloz(False, False, 10.0, 10.0)
        await self.nakup(flow, 1, 3.00)
        flow.exit_trade.orderStatus.status = "Cancelled"
        flow.exit_sl_trade.orderStatus.status = "Cancelled"

        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.ERROR)
        self.assertIn("bez zajištění", flow.message)

    async def test_jeden_zruseny_jen_varuje(self):
        flow = await self.zaloz(False, False, 10.0, 10.0)
        await self.nakup(flow, 1, 3.00)
        flow.exit_sl_trade.orderStatus.status = "Cancelled"

        await self.engine._tick()
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.EXIT_ARMED)
        varovani = [z for _, z in self.engine.events if "POZOR" in z and "SL" in z]
        # Varování se vypíše jen jednou
        self.assertEqual(len(varovani), 1)

        # Druhý příkaz se pak ještě může vyplnit a obchod řádně uzavřít
        self.ib.fill(flow.exit_trade, 1, 3.10)
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.CLOSED)

    async def test_uzavreni_trhem_zrusi_oba_a_proda(self):
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=2)
        await self.nakup(flow, 2, 3.00)
        pt, sl = flow.exit_trade, flow.exit_sl_trade

        await self.engine.close_main(flow.id)
        self.assertIn(pt, self.ib.cancelled)
        self.assertIn(sl, self.ib.cancelled)

        await self.engine._tick()
        trzni = self.ib.placed[-1].order
        self.assertEqual(trzni.orderType, "MKT")
        self.assertEqual(trzni.conditions, [])
        self.assertEqual(int(trzni.totalQuantity), 2)
        self.assertIsNone(flow.exit_sl_trade)

        self.ib.fill(flow.exit_trade, 2, 2.95)
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.CLOSED)
        self.assertEqual(flow.exit_reason, "ručně")

    async def test_zruseni_obchodu_zrusi_oba_prikazy(self):
        flow = await self.zaloz(False, False, 10.0, 10.0)
        await self.nakup(flow, 1, 3.00)
        pt, sl = flow.exit_trade, flow.exit_sl_trade

        await self.engine.cancel_flow(flow.id, close_position=False)
        self.assertIn(pt, self.ib.cancelled)
        self.assertIn(sl, self.ib.cancelled)
        self.assertEqual(flow.state, FlowState.CANCELLED)


class TestZmenyUrovniNaOpci(ZakladRezimu):
    """Násobky cíle a přepínání SL u úrovní zadaných na opci."""

    async def test_nasobek_cile_upravi_limit(self):
        flow = await self.zaloz(False, False, 10.0, 10.0)
        await self.nakup(flow, 1, 3.00)

        await self.engine.change_profit_target(flow.id, flow.scaled_target(2.0))

        self.assertAlmostEqual(flow.profit_target, 20.0)
        self.assertAlmostEqual(flow.pt_multiple, 2.0)
        self.assertAlmostEqual(flow.exit_trade.order.lmtPrice, 3.20)
        # SL příkaz zůstává beze změny
        self.assertAlmostEqual(flow.exit_sl_trade.order.auxPrice, 2.90)

    async def test_nasobek_cile_na_podkladu_pri_sl_na_opci(self):
        flow = await self.zaloz(True, False, 235.0, 10.0)
        await self.nakup(flow, 1, 3.00)

        await self.engine.change_profit_target(flow.id, flow.scaled_target(2.0))

        self.assertAlmostEqual(flow.profit_target, 238.0)
        self.assertAlmostEqual(flow.exit_trade.order.conditions[0].price, 238.0)

    async def test_sl_be_na_opci_je_stop_na_nakupni_cene(self):
        flow = await self.zaloz(False, False, 10.0, 10.0)
        await self.nakup(flow, 1, 3.00)
        # BID nad nákupní cenou - break even není proražený
        self.ib.price_bid = 3.20
        self.ib.price_ask = 3.30

        await self.engine.set_stop_loss(flow.id, "be")

        self.assertAlmostEqual(flow.stop_loss, 0.0)
        self.assertAlmostEqual(flow.exit_sl_trade.order.auxPrice, 3.00)
        self.assertFalse(flow.main_close_requested)

        await self.engine.set_stop_loss(flow.id, "puvodni")
        self.assertAlmostEqual(flow.stop_loss, 10.0)
        self.assertAlmostEqual(flow.exit_sl_trade.order.auxPrice, 2.90)

    async def test_prorazeny_sl_na_opci_proda_trhem(self):
        flow = await self.zaloz(False, False, 10.0, 10.0)
        await self.nakup(flow, 1, 3.00)
        pt, sl = flow.exit_trade, flow.exit_sl_trade
        # BID 2,95 je pod nákupní cenou 3,00 - break even je proražený
        self.ib.price_bid = 2.95
        self.ib.price_ask = 3.05

        await self.engine.set_stop_loss(flow.id, "be")

        self.assertTrue(flow.main_close_requested)
        self.assertIn(pt, self.ib.cancelled)
        self.assertIn(sl, self.ib.cancelled)

    async def test_zmena_pred_nakupem_plati_jen_v_prehledu(self):
        flow = await self.zaloz(False, False, 10.0, 10.0)
        await self.engine.change_profit_target(flow.id, 15.0)
        self.assertAlmostEqual(flow.profit_target, 15.0)
        self.assertEqual(flow.strike, 232.5)


class TestRunnerNaOpci(ZakladRezimu):
    """Runner s PT a SL zadanými na opci - vlastní dvojice příkazů."""

    async def test_runner_ma_vlastni_dvojici_prikazu(self):
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=3)
        await self.nakup(flow, 3, 3.00)

        await self.engine.set_runner(flow.id, 2.0)

        self.assertAlmostEqual(flow.runner_profit_target, 20.0)
        self.assertEqual(int(flow.exit_trade.order.totalQuantity), 2)
        self.assertEqual(int(flow.exit_sl_trade.order.totalQuantity), 2)
        self.assertIsNotNone(flow.runner_trade)
        self.assertIsNotNone(flow.runner_sl_trade)
        self.assertEqual(flow.runner_trade.order.orderType, "LMT")
        self.assertAlmostEqual(flow.runner_trade.order.lmtPrice, 3.20)
        self.assertEqual(flow.runner_sl_trade.order.orderType, "STP")
        self.assertAlmostEqual(flow.runner_sl_trade.order.auxPrice, 2.90)
        self.assertTrue(flow.runner_trade.order.orderRef.endswith(":runner"))
        self.assertTrue(flow.runner_sl_trade.order.orderRef.endswith(":runnersl"))
        # Runner má vlastní OCA skupinu odlišnou od hlavní části
        self.assertEqual(flow.runner_trade.order.ocaGroup, flow.runner_sl_trade.order.ocaGroup)
        self.assertNotEqual(flow.runner_trade.order.ocaGroup, flow.exit_trade.order.ocaGroup)

    async def test_vyplneni_runneru_zrusi_jeho_sl_hlavni_bezi_dal(self):
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=3)
        await self.nakup(flow, 3, 3.00)
        await self.engine.set_runner(flow.id, 2.0)
        runner_sl = flow.runner_sl_trade

        self.ib.fill(flow.runner_trade, 1, 3.20)
        await self.engine._tick()

        self.assertIn(runner_sl, self.ib.cancelled)
        self.assertFalse(flow.runner_active)
        self.assertEqual(flow.runner_sold_quantity, 1)
        self.assertAlmostEqual(flow.runner_realized_pnl, 20.0)
        self.assertEqual(flow.state, FlowState.EXIT_ARMED)
        # Hlavní dvojice zůstává v trhu
        self.assertNotIn(flow.exit_trade, self.ib.cancelled)
        self.assertNotIn(flow.exit_sl_trade, self.ib.cancelled)

    async def test_zruseni_runneru_slouci_obe_dvojice(self):
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=3)
        await self.nakup(flow, 3, 3.00)
        await self.engine.set_runner(flow.id, 2.0)
        runner_pt, runner_sl = flow.runner_trade, flow.runner_sl_trade

        await self.engine.cancel_runner(flow.id)

        self.assertIn(runner_pt, self.ib.cancelled)
        self.assertIn(runner_sl, self.ib.cancelled)
        self.assertEqual(int(flow.exit_trade.order.totalQuantity), 3)
        self.assertEqual(int(flow.exit_sl_trade.order.totalQuantity), 3)

    async def test_sl_be_runneru_na_opci(self):
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=3)
        await self.nakup(flow, 3, 3.00)
        await self.engine.set_runner(flow.id, 2.0)
        self.ib.price_bid = 3.20
        self.ib.price_ask = 3.30

        await self.engine.set_runner_stop_loss(flow.id, "be")

        self.assertAlmostEqual(flow.runner_sl, 0.0)
        self.assertAlmostEqual(flow.runner_sl_trade.order.auxPrice, 3.00)
        # Hlavní část si drží původní stop
        self.assertAlmostEqual(flow.exit_sl_trade.order.auxPrice, 2.90)

    async def test_runner_pred_nakupem_v_smisenem_rezimu(self):
        flow = await self.zaloz(True, False, 235.0, 10.0, quantity=3)
        await self.engine.set_runner(flow.id, 2.0)
        await self.nakup(flow, 3, 3.00)

        self.assertEqual(len(self.prodeje()), 4)
        self.assertAlmostEqual(flow.runner_trade.order.conditions[0].price, 238.0)
        self.assertEqual(flow.runner_sl_trade.order.orderType, "STP")

    async def test_doplneny_nakup_navysi_oba_prikazy(self):
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=3)
        # Částečné vyplnění 2 ze 3
        self.ib.fill(flow.entry_trade, 2, 3.00, status="Submitted")
        await self.engine._tick()
        await self.engine._tick()
        # Zbytek nákupu se ruší, ale ještě před potvrzením se doplní
        self.ib.fill(flow.entry_trade, 3, 3.00)
        await self.engine._tick()
        await self.engine._tick()

        self.assertEqual(int(flow.exit_trade.order.totalQuantity), 3)
        self.assertEqual(int(flow.exit_sl_trade.order.totalQuantity), 3)


class TestObnovaDvojice(unittest.IsolatedAsyncioTestCase):
    """Obnova obchodu s dvojicí prodejních příkazů po restartu."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = AppConfig()
        self.cfg.state.file = str(Path(self.tmp.name) / "state.json")
        self.cfg.trading.auto_close_enabled = False
        self.ib = FakeIBService(self.cfg)
        self.engine = FlowEngine(self.cfg, self.ib)

    async def asyncSetUp(self) -> None:
        await self.engine.restore()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def zaloz_nakoupeny(self, pt_on: bool = False, sl_on: bool = False):
        """Založí obchod s oběma úrovněmi na opci, nakoupí a zajistí."""
        self.ib.price_underlying = 230.0
        flow = await self.engine.start_flow(
            FlowRequest(
                symbol="AAPL",
                entry_price=232.0,
                profit_target=10.0 if not pt_on else 235.0,
                stop_loss=10.0 if not sl_on else 229.0,
                quantity=2,
                pt_on_underlying=pt_on,
                sl_on_underlying=sl_on,
            )
        )
        self.ib.fill(flow.entry_trade, 2, 3.00)
        await self.engine._tick()
        await self.engine._tick()
        self.ib.held_positions[OPTION_CONID] = 2
        return flow

    async def test_rezimy_se_ukladaji_a_obnovi(self):
        flow = await self.zaloz_nakoupeny()
        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()

        obnovene = novy.flows[flow.id]
        self.assertFalse(obnovene.pt_on_underlying)
        self.assertFalse(obnovene.sl_on_underlying)
        self.assertAlmostEqual(obnovene.profit_target, 10.0)
        self.assertEqual(obnovene.state, FlowState.EXIT_ARMED)
        self.assertIsNotNone(obnovene.exit_trade)
        self.assertIsNotNone(obnovene.exit_sl_trade)
        self.assertEqual(obnovene.exit_sl_order_id, obnovene.exit_sl_trade.order.orderId)

    async def test_osamocena_polovina_se_zrusi_a_zajisteni_zalozi_znovu(self):
        flow = await self.zaloz_nakoupeny()
        # Stop příkaz zmizel (např. ručně zrušen), limit zůstal
        flow.exit_sl_trade.orderStatus.status = "Cancelled"
        prezivsi = flow.exit_trade

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()
        obnovene = novy.flows[flow.id]

        self.assertEqual(obnovene.state, FlowState.FILLED)
        self.assertIn(prezivsi, self.ib.cancelled)

        # Dokud TWS zrušení nepotvrdí, nic se nezadává
        prezivsi.orderStatus.status = "PendingCancel"
        pocet = len(self.ib.placed)
        await novy._tick()
        self.assertEqual(len(self.ib.placed), pocet)
        self.assertEqual(obnovene.state, FlowState.FILLED)

        # Po potvrzení vznikne nová dvojice
        prezivsi.orderStatus.status = "Cancelled"
        await novy._tick()
        self.assertEqual(obnovene.state, FlowState.EXIT_ARMED)
        self.assertEqual(len(self.ib.placed), pocet + 2)
        self.assertIsNotNone(obnovene.exit_sl_trade)

    async def test_uzavreni_behem_vypadku_pozna_pt_z_limitu(self):
        flow = await self.zaloz_nakoupeny()
        self.ib.fill(flow.exit_trade, 2, 3.10)
        flow.exit_sl_trade.orderStatus.status = "Cancelled"
        self.ib.held_positions.clear()

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()
        obnovene = novy.flows[flow.id]

        self.assertEqual(obnovene.state, FlowState.CLOSED)
        self.assertEqual(obnovene.exit_reason, "PT")
        self.assertAlmostEqual(obnovene.exit_fill_price, 3.10)

    async def test_prevzeti_bez_souboru_pozna_rezimy_z_prikazu(self):
        flow = await self.zaloz_nakoupeny()
        Path(self.cfg.state.file).unlink()

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()
        prevzaty = novy.flows[flow.id]

        # Zisk i ztráta se odvodí z cen příkazů proti nákupní ceně 3,00
        self.assertFalse(prevzaty.pt_on_underlying)
        self.assertFalse(prevzaty.sl_on_underlying)
        self.assertAlmostEqual(prevzaty.profit_target, 10.0)
        self.assertAlmostEqual(prevzaty.stop_loss, 10.0)
        self.assertEqual(prevzaty.state, FlowState.EXIT_ARMED)
        self.assertNotIn("dopočítané", prevzaty.message)

    async def test_prevzeti_smiseneho_rezimu(self):
        flow = await self.zaloz_nakoupeny(pt_on=True, sl_on=False)
        Path(self.cfg.state.file).unlink()

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()
        prevzaty = novy.flows[flow.id]

        self.assertTrue(prevzaty.pt_on_underlying)
        self.assertAlmostEqual(prevzaty.profit_target, 235.0)
        self.assertFalse(prevzaty.sl_on_underlying)
        self.assertAlmostEqual(prevzaty.stop_loss, 10.0)



class TestDopoctuPtZeSl(ZakladRezimu):
    """Prvotní je SL: PT se dopočítá podle poměru SL:PT ve všech režimech."""

    async def zaloz_se_sl(self, pt_on: bool, sl_on: bool, sl: float, **zmeny):
        """Založí CALL obchod jen se SL, PT se má dopočítat."""
        self.ib.price_underlying = 230.0
        pozadavek = FlowRequest(
            symbol="AAPL",
            entry_price=232.0,
            profit_target=None,
            stop_loss=sl,
            pt_on_underlying=pt_on,
            sl_on_underlying=sl_on,
        )
        for klic, hodnota in zmeny.items():
            setattr(pozadavek, klic, hodnota)
        return await self.engine.start_flow(pozadavek)

    async def test_oba_na_podkladu_pt_zrcadlem(self):
        # SL 229 = 3 body pod vstupem, poměr 1:1 -> PT 235, strike u PT
        flow = await self.zaloz_se_sl(True, True, 229.0)
        self.assertAlmostEqual(flow.profit_target, 235.0)
        self.assertAlmostEqual(flow.original_profit_target, 235.0)
        self.assertEqual(flow.strike, 235.0)
        self.assertEqual(flow.right, "C")

    async def test_pomer_se_uplatni(self):
        # Poměr 0,5 (SL na poloviční vzdálenosti) -> PT dvakrát dál než SL
        self.cfg.trading.sl_to_pt_ratio = 0.5
        flow = await self.zaloz_se_sl(True, True, 230.5)
        self.assertAlmostEqual(flow.profit_target, 235.0)

    async def test_put_ze_sl_nad_vstupem(self):
        self.ib.price_underlying = 230.0
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=228.0, stop_loss=231.0)
        )
        self.assertEqual(flow.right, "P")
        self.assertAlmostEqual(flow.profit_target, 225.0)

    async def test_oba_na_opci_pt_podilem(self):
        self.cfg.trading.sl_to_pt_ratio = 0.5
        flow = await self.zaloz_se_sl(False, False, 10.0)
        self.assertAlmostEqual(flow.profit_target, 20.0)
        self.assertAlmostEqual(flow.stop_loss, 10.0)

    async def test_sl_na_opci_pt_na_podkladu(self):
        # Kde opce vydělá 10 USD - těsně nad vstupem, strike k této úrovni
        flow = await self.zaloz_se_sl(True, False, 10.0)
        self.assertTrue(flow.pt_on_underlying)
        self.assertGreater(flow.profit_target, 232.0)
        self.assertLess(flow.profit_target, 233.0)
        self.assertEqual(flow.strike, 232.5)

    async def test_sl_na_podkladu_pt_na_opci(self):
        # Ztráta opce při poklesu na 229 přepočtená do USD - kladná částka
        flow = await self.zaloz_se_sl(False, True, 229.0)
        self.assertFalse(flow.pt_on_underlying)
        self.assertGreater(flow.profit_target, 0)
        self.assertLess(flow.profit_target, 300.0)

    async def test_zadane_pt_ma_prednost_pred_dopoctem(self):
        flow = await self.zaloz_se_sl(True, True, 229.0, profit_target=236.0)
        self.assertAlmostEqual(flow.profit_target, 236.0)

    async def test_bez_pt_i_sl_se_odmitne(self):
        with self.assertRaises(ValueError):
            await self.engine.start_flow(FlowRequest(symbol="AAPL", entry_price=232.0))

    async def test_nahled_nese_dopoctene_pt(self):
        self.ib.price_underlying = 230.0
        nahled = await self.engine.prepare("AAPL", 232.0, None, 229.0)
        self.assertAlmostEqual(nahled.profit_target, 235.0)
        self.assertAlmostEqual(nahled.stop_loss, 229.0)
        self.assertEqual(nahled.strike, 235.0)

    async def test_nahled_bez_urovni_vraci_jen_cenu(self):
        self.ib.price_underlying = 230.0
        nahled = await self.engine.prepare("AAPL", 232.0, None, None)
        self.assertEqual(nahled.expiration, "")
        self.assertAlmostEqual(nahled.current_price, 230.0)



class TestZdrojCenyOpce(ZakladRezimu):
    """Model z ceny opce se použije i bez kotací - z poslední či závěrečné ceny."""

    async def test_bez_kotaci_se_pouzije_zaverecna_cena(self):
        # BID/ASK chybí, ale závěrečná cena je - model běží z ní, ne z delty
        self.ib.price_bid = None
        self.ib.price_ask = None
        self.ib.price_close = 3.05
        self.cfg.trading.entry_order_type = "MKT"
        self.ib.price_underlying = 230.0

        nahled = await self.engine.prepare("AAPL", 232.0, 10.0, None, False, False)

        self.assertIn("z ceny opce", nahled.target_level_source)
        self.assertIn("close", nahled.target_level_source)
        self.assertEqual(nahled.option_price_source, "close")
        self.assertAlmostEqual(nahled.option_price, 3.05)

    async def test_posledni_obchod_ma_prednost_pred_zaverecnou(self):
        self.ib.price_bid = None
        self.ib.price_ask = None
        self.ib.price_last = 3.20
        self.ib.price_close = 2.00
        self.cfg.trading.entry_order_type = "MKT"
        self.ib.price_underlying = 230.0

        nahled = await self.engine.prepare("AAPL", 232.0, 10.0, None, False, False)

        self.assertIn("last", nahled.target_level_source)
        self.assertAlmostEqual(nahled.option_price, 3.20)

    async def test_s_kotacemi_se_uvadi_bid_ask(self):
        self.ib.price_underlying = 230.0
        nahled = await self.engine.prepare("AAPL", 232.0, 10.0, None, False, False)
        self.assertEqual(nahled.target_level_source, "z ceny opce (BID/ASK)")

    async def test_delta_jen_bez_jakekoliv_ceny(self):
        self.ib.price_bid = None
        self.ib.price_ask = None
        self.cfg.trading.entry_order_type = "MKT"
        self.ib.price_underlying = 230.0

        nahled = await self.engine.prepare("AAPL", 232.0, 10.0, None, False, False)

        self.assertEqual(nahled.target_level_source, "z delty")
        self.assertIsNone(nahled.option_price)

    async def test_ocekavany_zisk_se_pocita_i_ze_zaverecne_ceny(self):
        flow = await self.zaloz(True, True, 235.0)
        self.ib.price_bid = None
        self.ib.price_ask = None
        self.ib.price_close = 3.05
        await self.engine._tick()
        # Bez kotací by dřív sloupce zůstaly prázdné; ze závěrečné ceny se spočítají
        self.assertIsNotNone(flow.expected_profit)
        self.assertGreater(flow.expected_profit, 0)



class TestObnovaSlBeNaOpci(unittest.IsolatedAsyncioTestCase):
    """SL na break even v režimu na opci (ztráta 0 USD) musí přežít restart."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = AppConfig()
        self.cfg.state.file = str(Path(self.tmp.name) / "state.json")
        self.cfg.trading.auto_close_enabled = False
        self.ib = FakeIBService(self.cfg)
        self.engine = FlowEngine(self.cfg, self.ib)

    async def asyncSetUp(self) -> None:
        await self.engine.restore()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_sl_be_na_opci_se_obnovi(self):
        self.ib.price_underlying = 230.0
        flow = await self.engine.start_flow(
            FlowRequest(
                symbol="AAPL",
                entry_price=232.0,
                profit_target=10.0,
                stop_loss=10.0,
                quantity=2,
                pt_on_underlying=False,
                sl_on_underlying=False,
            )
        )
        self.ib.fill(flow.entry_trade, 2, 3.00)
        await self.engine._tick()
        await self.engine._tick()
        self.ib.price_bid, self.ib.price_ask = 3.20, 3.30
        await self.engine.set_stop_loss(flow.id, "be")
        self.assertAlmostEqual(flow.stop_loss, 0.0)
        self.ib.held_positions[OPTION_CONID] = 2

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()
        obnovene = novy.flows[flow.id]

        self.assertEqual(obnovene.state, FlowState.EXIT_ARMED)
        self.assertAlmostEqual(obnovene.stop_loss, 0.0)
        self.assertAlmostEqual(obnovene.original_stop_loss, 10.0)


if __name__ == "__main__":
    unittest.main()
