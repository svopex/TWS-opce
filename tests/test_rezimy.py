"""
Testy režimů zadání PT a SL: na podkladu (podmíněný příkaz), nebo ziskem /
ztrátou v USD na kontrakt (příkaz přímo na cenu opce) - a jejich kombinací,
kdy vznikají dva prodejní příkazy a po vyplnění jednoho se druhý ruší.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fake_ib import OPTION_CONID, UNDERLYING_CONID
from tests.zaklad import ZakladEnginu, ZakladSeStavem
from tws_opce.engine import FlowEngine
from tws_opce.models import FlowRequest, FlowState


class ZakladRezimu(ZakladEnginu):
    """Engine s náhradou TWS a pomocné kroky scénářů zadání na opci."""

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

    async def test_vyplneny_prikaz_na_mensi_mnozstvi_hlasi_nezajisteny_zbytek(self):
        """
        TWS ohlásí příkaz jako vyplněný, ale prodal míň kusů, než pozice drží.
        Zbytek zůstává bez zajištění - obchodník to musí vidět.
        """
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=3)
        await self.nakup(flow, 3, 3.00)
        # Limit se vyplnil jen na 1 ks a přesto skončil jako Filled,
        # stop k němu zrušila OCA skupina
        self.ib.fill(flow.exit_trade, 1, 3.10)
        flow.exit_sl_trade.orderStatus.status = "Cancelled"

        await self.engine._tick()

        self.assertEqual(flow.main_sold_quantity, 1)
        self.assertIsNone(flow.exit_fill_price)
        self.assertEqual(flow.state, FlowState.ERROR)
        self.assertIn("bez zajištění", flow.message)
        self.assertIn("2 ks", flow.message)

    async def test_nova_dvojice_varuje_znovu(self):
        """Po založení nové dvojice se varování o ztraceném příkazu smí zopakovat."""
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=3)
        await self.nakup(flow, 3, 3.00)
        await self.engine.set_runner(flow.id, 2.0)
        flow.runner_sl_trade.orderStatus.status = "Cancelled"
        await self.engine._tick()

        # Runner se zruší a hned zapne znovu - vznikne nová dvojice příkazů
        await self.engine.cancel_runner(flow.id)
        await self.engine.set_runner(flow.id, 2.0)
        flow.runner_sl_trade.orderStatus.status = "Cancelled"
        await self.engine._tick()

        varovani = [
            z for _, z in self.engine.events if "POZOR" in z and "runneru" in z
        ]
        self.assertEqual(len(varovani), 2)

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

    async def test_prorazeni_se_meri_stop_cenou_prikazu(self):
        """
        Stop příkaz stojí na ceně zaokrouhlené na tik - proražení se musí
        posuzovat proti ní, ne proti nezaokrouhlené hodnotě.
        """
        # Ztráta 12 USD = 0,12 pod nákupní cenou 3,00, po zaokrouhlení
        # na tik 0,05 stojí stop na 2,90
        flow = await self.zaloz(False, False, 20.0, 12.0)
        await self.nakup(flow, 1, 3.00)
        self.assertAlmostEqual(flow.exit_sl_trade.order.auxPrice, 2.90)
        pt, sl = flow.exit_trade, flow.exit_sl_trade
        # BID přesně na stop ceně příkazu - stop by se v TWS spustil
        self.ib.price_bid = 2.90
        self.ib.price_ask = 3.00

        await self.engine.set_stop_loss(flow.id, "puvodni")

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

    async def test_selhani_prikazu_runneru_nezmensi_hlavni_zajisteni(self):
        """
        Nelze-li příkazy runneru sestavit, hlavní zajištění musí zůstat
        na celé pozici - jinak by část kusů zůstala nekrytá.
        """
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=3)
        await self.nakup(flow, 3, 3.00)
        # Bez nákupní ceny nelze úrovně na opci spočítat
        flow.fill_price = None

        with self.assertRaises(ValueError):
            await self.engine.set_runner(flow.id, 2.0)

        self.assertEqual(int(flow.exit_trade.order.totalQuantity), 3)
        self.assertEqual(int(flow.exit_sl_trade.order.totalQuantity), 3)
        self.assertIsNone(flow.runner_trade)
        # Runner nesmí zůstat zapnutý bez příkazů v trhu
        self.assertFalse(flow.runner_active)

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


class TestObnovaDvojice(ZakladSeStavem):
    """Obnova obchodu s dvojicí prodejních příkazů po restartu."""

    async def zaloz_nakoupeny(self, pt_on: bool = False, sl_on: bool = False, quantity: int = 2):
        """Založí obchod s oběma úrovněmi na opci, nakoupí a zajistí."""
        self.ib.price_underlying = 230.0
        flow = await self.engine.start_flow(
            FlowRequest(
                symbol="AAPL",
                entry_price=232.0,
                profit_target=10.0 if not pt_on else 235.0,
                stop_loss=10.0 if not sl_on else 229.0,
                quantity=quantity,
                pt_on_underlying=pt_on,
                sl_on_underlying=sl_on,
            )
        )
        self.ib.fill(flow.entry_trade, quantity, 3.00)
        await self.engine._tick()
        await self.engine._tick()
        self.ib.held_positions[OPTION_CONID] = quantity
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

    async def test_prodana_hlavni_cast_necha_runner_bezet(self):
        """
        PT hlavní části se vyplnil a SL k němu TWS zrušila přes OCA skupinu.
        Runner běží dál - obnova se ho nesmí ani dotknout.
        """
        flow = await self.zaloz_nakoupeny(quantity=3)
        await self.engine.set_runner(flow.id, 2.0)
        # Hlavní část (2 ks) se prodala na PT, SL zrušila OCA skupina
        self.ib.fill(flow.exit_trade, 2, 3.10)
        flow.exit_sl_trade.orderStatus.status = "Cancelled"
        await self.engine._tick()
        self.assertIsNotNone(flow.exit_fill_price)
        # V TWS zbývá jen kus runneru
        self.ib.held_positions[OPTION_CONID] = 1
        runner_pt, runner_sl = flow.runner_trade, flow.runner_sl_trade
        prikazu_pred = len(self.ib.placed)

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()
        obnovene = novy.flows[flow.id]

        self.assertEqual(obnovene.state, FlowState.EXIT_ARMED)
        self.assertNotIn(runner_pt, self.ib.cancelled)
        self.assertNotIn(runner_sl, self.ib.cancelled)
        self.assertEqual(len(self.ib.placed), prikazu_pred)
        self.assertTrue(obnovene.runner_active)
        self.assertEqual(obnovene.open_quantity, 1)

    async def test_po_prodanem_runneru_se_zajisti_zbyle_kusy(self):
        """
        Runner je prodaný, hlavní části někdo zrušil polovinu dvojice.
        Nové zajištění musí krýt přesně držené kusy, ne celý nákup.
        """
        flow = await self.zaloz_nakoupeny(quantity=4)
        await self.engine.set_runner(flow.id, 2.0)
        self.ib.fill(flow.runner_trade, 1, 3.30)
        await self.engine._tick()
        self.assertEqual(flow.runner_sold_quantity, 1)
        # Zbývají 3 ks a stop hlavní části byl v TWS zrušen
        self.ib.held_positions[OPTION_CONID] = 3
        flow.exit_sl_trade.orderStatus.status = "Cancelled"

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()
        obnovene = novy.flows[flow.id]
        self.assertEqual(obnovene.state, FlowState.FILLED)

        # Po potvrzení zrušení vznikne nová dvojice na 3 ks
        for leg in (obnovene.exit_trade, obnovene.exit_sl_trade):
            if leg is not None:
                leg.orderStatus.status = "Cancelled"
        await novy._tick()

        self.assertEqual(obnovene.state, FlowState.EXIT_ARMED)
        self.assertEqual(int(obnovene.exit_trade.order.totalQuantity), 3)
        self.assertEqual(int(obnovene.exit_sl_trade.order.totalQuantity), 3)

    async def test_rozdelane_uzavirani_trhem_pokracuje(self):
        """Živý tržní prodej po restartu doběhne, nezruší se a nezakládá se zajištění."""
        flow = await self.zaloz_nakoupeny()
        await self.engine.close_main(flow.id)
        await self.engine._tick()
        trzni = flow.exit_trade
        self.assertEqual(trzni.order.orderType, "MKT")
        self.assertFalse(trzni.order.conditions)

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()
        obnovene = novy.flows[flow.id]

        self.assertEqual(obnovene.state, FlowState.EXIT_ARMED)
        self.assertTrue(obnovene.main_close_requested)
        self.assertNotIn(trzni, self.ib.cancelled)
        self.assertIs(obnovene.exit_trade, trzni)

    async def test_trzni_prodej_vyplneny_behem_vypadku_je_rucni(self):
        """Prodej na pokyn obchodníka se po obnově nesmí vydávat za PT."""
        flow = await self.zaloz_nakoupeny()
        await self.engine.close_main(flow.id)
        await self.engine._tick()
        self.ib.fill(flow.exit_trade, 2, 2.95)
        self.ib.held_positions.clear()

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()
        obnovene = novy.flows[flow.id]

        self.assertEqual(obnovene.state, FlowState.CLOSED)
        self.assertEqual(obnovene.exit_reason, "ručně")
        self.assertAlmostEqual(obnovene.exit_fill_price, 2.95)

    async def test_prevzeti_zna_nakupni_cenu_a_umi_zmenit_cil(self):
        """
        Převzatý obchod musí znát nákupní cenu opce - bez ní by úrovně
        zadané na cenu opce nešlo ani spočítat, ani měnit.
        """
        flow = await self.zaloz_nakoupeny()
        Path(self.cfg.state.file).unlink()

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()
        prevzaty = novy.flows[flow.id]

        self.assertAlmostEqual(prevzaty.fill_price, 3.00)
        self.assertEqual(prevzaty.filled_quantity, 2)

        await novy.change_profit_target(prevzaty.id, 20.0)
        self.assertAlmostEqual(prevzaty.profit_target, 20.0)
        # Zisk 20 USD na kontrakt = limit 0,20 nad nákupní cenou
        self.assertAlmostEqual(prevzaty.exit_trade.order.lmtPrice, 3.20)

    async def test_prevzeti_podle_samotneho_stop_prikazu(self):
        """Přežil-li jen příkaz pro SL, obchod se nesmí převzít jako oba na podkladu."""
        flow = await self.zaloz_nakoupeny()
        Path(self.cfg.state.file).unlink()
        # Limitní příkaz pro PT v TWS není (například byl ručně zrušen a smazán)
        self.ib.placed.remove(flow.exit_trade)

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()
        prevzaty = novy.flows[flow.id]

        self.assertFalse(prevzaty.sl_on_underlying)
        self.assertTrue(prevzaty.exit_split)
        self.assertAlmostEqual(prevzaty.stop_loss, 10.0)
        # PT se z ničeho odvodit nedá - dopočítá se ze strike a je to vidět
        self.assertTrue(prevzaty.pt_on_underlying)
        self.assertIn("dopočítané", prevzaty.message)

    async def test_prevzeti_stop_na_nakupni_cene_je_break_even(self):
        """Stop na nákupní ceně znamená nulovou ztrátu, ne chybějící údaj."""
        flow = await self.zaloz_nakoupeny()
        self.ib.price_bid, self.ib.price_ask = 3.20, 3.30
        await self.engine.set_stop_loss(flow.id, "be")
        self.assertAlmostEqual(flow.exit_sl_trade.order.auxPrice, 3.00)
        Path(self.cfg.state.file).unlink()

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()
        prevzaty = novy.flows[flow.id]

        self.assertFalse(prevzaty.sl_on_underlying)
        self.assertAlmostEqual(prevzaty.stop_loss, 0.0)

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

    async def test_zaverecna_cena_varuje_v_nahledu_i_v_logu(self):
        # Mimo obchodní hodiny je závěrečná cena jediná dostupná; pohnul-li se
        # mezitím podklad, vyjde z ní nesmyslná volatilita
        self.ib.price_bid = None
        self.ib.price_ask = None
        self.ib.price_close = 3.05
        self.cfg.trading.entry_order_type = "MKT"
        self.ib.price_underlying = 230.0

        nahled = await self.engine.prepare("AAPL", 232.0, 10.0, None, False, False)
        self.assertTrue(any("závěrečné ceny" in v for v in nahled.warnings))

        await self.zaloz(False, False, 10.0, 10.0)
        self.assertTrue(any("závěrečné ceny" in z for _, z in self.engine.events))

    async def test_s_kotacemi_se_nevaruje(self):
        self.ib.price_underlying = 230.0
        nahled = await self.engine.prepare("AAPL", 232.0, 10.0, None, False, False)
        self.assertFalse(any("závěrečné ceny" in v for v in nahled.warnings))

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



class TestObnovaSlBeNaOpci(ZakladSeStavem):
    """SL na break even v režimu na opci (ztráta 0 USD) musí přežít restart."""

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



class TestCastecnehoProdejeNaPt(ZakladRezimu):
    """Část kusů se prodá na PT, zbytek po zmenšení OCA skupinou na SL."""

    async def test_prodejni_cena_je_vazeny_prumer_obou_prikazu(self):
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=2)
        await self.nakup(flow, 2, 3.00)
        # PT limit se vyplnil jen z poloviny, zbylý kus prodal stop
        self.ib.fill(flow.exit_trade, 1, 3.10, status="Submitted")
        self.ib.fill(flow.exit_sl_trade, 1, 2.85)

        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.CLOSED)
        # (3,10 + 2,85) / 2 = 2,975 -> výsledek (2,975 - 3,00) * 2 * 100 = -5 USD
        self.assertAlmostEqual(flow.exit_fill_price, 2.975)
        self.assertAlmostEqual(flow.unrealized_pnl, -5.0)
        self.assertEqual(flow.exit_reason, "PT+SL")

    async def test_castecny_prodej_runneru(self):
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=3)
        await self.nakup(flow, 3, 3.00)
        self.cfg.trading.runner_quantity = 2
        # Runner o 2 ks lze oddělit jen z pozice 3 ks - zbude 1 ks v hlavní části
        await self.engine.set_runner(flow.id, 2.0)
        self.ib.fill(flow.runner_trade, 1, 3.20, status="Submitted")
        self.ib.fill(flow.runner_sl_trade, 1, 2.90)

        await self.engine._tick()

        # (0,20 - 0,10) * 100 = +10 USD za oba kusy dohromady
        self.assertAlmostEqual(flow.runner_realized_pnl, 10.0)
        self.assertEqual(flow.runner_sold_quantity, 2)

    async def test_obnova_po_vypadku_pocita_oba_prikazy(self):
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=2)
        await self.nakup(flow, 2, 3.00)
        self.ib.fill(flow.exit_trade, 1, 3.10, status="Cancelled")
        self.ib.fill(flow.exit_sl_trade, 1, 2.85)

        novy = FlowEngine(self.cfg, self.ib)
        novy.flows[flow.id] = flow
        await novy.restore()

        self.assertEqual(flow.state, FlowState.CLOSED)
        self.assertAlmostEqual(flow.exit_fill_price, 2.975)
        self.assertEqual(flow.exit_reason, "PT+SL")


class TestCastecnehoVyplneniZaBehu(ZakladRezimu):
    """
    Částečně vyplněný PT limit se musí promítnout do modelu ihned.
    Jinak by úpravy množství (sloučení runneru, dorovnání nákupu) zadaly
    do trhu víc kusů, než pozice drží.
    """

    async def zaloz_s_runnerem(self, mnozstvi: int = 4):
        """Obchod na opci s runnerem 1 ks - hlavní část tedy kryje zbytek."""
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=mnozstvi)
        await self.nakup(flow, mnozstvi, 3.00)
        await self.engine.set_runner(flow.id, 2.0)
        return flow

    async def test_castecny_prodej_se_promitne_do_modelu(self):
        flow = await self.zaloz_s_runnerem()
        # PT limit hlavní části prodal 1 ze 3 ks a dál běží
        self.ib.fill(flow.exit_trade, 1, 3.10, status="Submitted")

        await self.engine._tick()

        self.assertEqual(flow.main_sold_quantity, 1)
        self.assertAlmostEqual(flow.main_sold_value, 3.10)
        # Drženo 4 ks, jeden prodán -> otevřené jsou 3 ks
        self.assertEqual(flow.open_quantity, 3)
        self.assertIsNone(flow.exit_fill_price)

    async def test_opakovany_pruchod_smyckou_neprodava_dvakrat(self):
        flow = await self.zaloz_s_runnerem()
        self.ib.fill(flow.exit_trade, 1, 3.10, status="Submitted")

        for _ in range(3):
            await self.engine._tick()

        # Totéž vyplnění se smí započítat právě jednou
        self.assertEqual(flow.main_sold_quantity, 1)
        self.assertAlmostEqual(flow.main_sold_value, 3.10)
        self.assertEqual(flow.open_quantity, 3)

    async def test_zruseni_runneru_po_castecnem_prodeji_nezvetsi_prikazy(self):
        flow = await self.zaloz_s_runnerem()
        self.ib.fill(flow.exit_trade, 1, 3.10, status="Submitted")
        await self.engine._tick()

        await self.engine.cancel_runner(flow.id)

        # Zbývá prodat 3 ks: PT má v TWS celkem 4 (z toho 1 už prodaný),
        # SL zatím neprodal nic, takže celkem 3
        self.assertEqual(int(flow.exit_trade.order.totalQuantity), 4)
        self.assertEqual(int(flow.exit_sl_trade.order.totalQuantity), 3)
        self.assertEqual(flow.open_quantity, 3)

    async def test_uzavreni_po_castecnem_prodeji_zapocte_kusy_jednou(self):
        flow = await self.zaloz_s_runnerem()
        self.ib.fill(flow.exit_trade, 1, 3.10, status="Submitted")
        await self.engine._tick()

        # Ruční uzavření hlavní části: příkazy se zruší a trhem jde jen zbytek
        await self.engine.close_main(flow.id)
        await self.engine._tick()

        self.assertEqual(flow.main_sold_quantity, 1)
        trzni = [
            t for t in self.ib.placed
            if t.order.action == "SELL" and t.order.orderType == "MKT" and not t.order.conditions
        ]
        self.assertEqual(len(trzni), 1)
        # Hlavní část drží 3 ks (runner 1 ks běží dál), jeden už je prodaný
        self.assertEqual(int(trzni[0].order.totalQuantity), 2)

    async def test_doprodani_zbytku_da_vazeny_prumer(self):
        flow = await self.zaloz_s_runnerem()
        self.ib.fill(flow.exit_trade, 1, 3.10, status="Submitted")
        await self.engine._tick()
        await self.engine.cancel_runner(flow.id)

        # Zbylé 3 ks prodal stop (TWS jej po sloučení vede na 3 ks)
        self.ib.fill(flow.exit_sl_trade, 3, 2.90)
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.CLOSED)
        # (3,10 + 3 × 2,90) / 4 = 2,95
        self.assertAlmostEqual(flow.exit_fill_price, 2.95)
        self.assertEqual(flow.exit_reason, "PT+SL")
        self.assertAlmostEqual(flow.unrealized_pnl, -20.0)

    async def test_castecny_prodej_runneru_zmensi_drzene_mnozstvi(self):
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=4)
        await self.nakup(flow, 4, 3.00)
        self.cfg.trading.runner_quantity = 2
        await self.engine.set_runner(flow.id, 2.0)
        # Runner prodal 1 ze 2 ks a dál běží
        self.ib.fill(flow.runner_trade, 1, 3.30, status="Submitted")

        await self.engine._tick()
        await self.engine._tick()

        self.assertTrue(flow.runner_active)
        self.assertEqual(flow.runner_quantity, 1)
        self.assertEqual(flow.runner_sold_quantity, 1)
        self.assertAlmostEqual(flow.runner_realized_pnl, 30.0)
        self.assertEqual(flow.held_quantity, 3)


class TestStropuZtratyNaOpci(ZakladRezimu):
    """SL zadaný na opci nemůže odnést víc než zaplacenou prémii."""

    async def test_ocekavana_ztrata_nepresahne_premii(self):
        # Ztráta 400 USD na kontrakt je víc než prémie 3,00 (= 300 USD);
        # stop stojí na nejnižší možné ceně, takže ztratit lze nejvýš
        # (3,00 − 0,05) × 100 = 295 USD
        flow = await self.zaloz(False, False, 500.0, 400.0)
        await self.nakup(flow, 1, 3.00)

        await self.engine._tick()

        self.assertAlmostEqual(flow.exit_sl_trade.order.auxPrice, 0.05)
        self.assertAlmostEqual(flow.expected_loss, -295.0)

    async def test_mensi_ztrata_se_nemeni(self):
        flow = await self.zaloz(False, False, 20.0, 50.0)
        await self.nakup(flow, 1, 3.00)

        await self.engine._tick()

        self.assertAlmostEqual(flow.expected_loss, -50.0)

    async def test_mnozstvi_vychazi_ze_skutecne_ztraty(self):
        # Levná opce a velký SL: skutečná ztráta je jen zlomek zadané,
        # doporučené množství proto vyjde vyšší
        self.cfg.account.size = 100000.0
        self.ib.price_underlying = 230.0
        self.ib.price_bid, self.ib.price_ask = 0.50, 0.60

        nahled = await self.engine.prepare("AAPL", 232.0, 400.0, 200.0, False, False)

        # Bez zastropování by vyšlo 1000 / 200 = 5 kontraktů
        self.assertGreater(nahled.quantity, 5)


class TestOdberuPriPriprave(ZakladRezimu):
    """Příprava zadání si po sobě uklidí odběry, i když nedoběhne."""

    async def test_uspesna_priprava_drzi_oba_kontrakty(self):
        self.ib.price_underlying = 230.0
        await self.engine.prepare("AAPL", 232.0, 10.0, None, False, False)
        # Podklad a vybraná opce; referenční opce je tu tentýž kontrakt
        self.assertEqual(set(self.ib.subscribed), {UNDERLYING_CONID, OPTION_CONID})
        self.assertEqual(self.ib.subscribed[OPTION_CONID], 1)

    async def test_chyba_pri_vyberu_expirace_uvolni_odbery(self):
        self.cfg.expiration.mode = "fixed"
        self.cfg.expiration.fixed_date = "20200101"
        self.ib.price_underlying = 230.0

        with self.assertRaises(ValueError):
            await self.engine.prepare("AAPL", 232.0, 10.0, None, False, False)

        self.assertEqual(self.ib.subscribed, {})

    async def test_zruseni_pripravy_uvolni_odbery(self):
        self.ib.price_underlying = 230.0

        async def cekej(contract, timeout, quotes_grace=0.0):
            """Kotace opce nedorazí - příprava na nich uvázne."""
            if contract.conId == OPTION_CONID:
                await asyncio.sleep(5)

        self.ib.wait_for_quotes = cekej
        uloha = asyncio.create_task(
            self.engine.prepare("AAPL", 232.0, 10.0, None, False, False)
        )
        # Nechá se doběhnout až k čekání na kotace opce a pak se zruší
        await asyncio.sleep(0.05)
        uloha.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await uloha

        self.assertEqual(self.ib.subscribed, {})


class TestNeznameNakupniCeny(ZakladRezimu):
    """Bez nákupní ceny z TWS se jako základ pro PT/SL na opci vezme aktuální cena."""

    async def test_nahradni_zaklad_z_aktualni_ceny(self):
        self.cfg.trading.entry_order_type = "MKT"
        flow = await self.zaloz(False, False, 10.0, 10.0)
        # TWS vrátila vyplnění bez platné průměrné ceny a nákup byl tržní
        self.ib.fill(flow.entry_trade, 1, 0.0)
        await self.engine._tick()
        self.assertIsNone(flow.fill_price)

        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.EXIT_ARMED)
        # Střed kotace 3,05 je náhradní základ: limit 3,15, stop 2,95
        self.assertAlmostEqual(flow.fill_price, 3.05)
        self.assertAlmostEqual(flow.exit_trade.order.lmtPrice, 3.15)
        self.assertAlmostEqual(flow.exit_sl_trade.order.auxPrice, 2.95)
        self.assertTrue(any("neposlala nákupní cenu" in z for _, z in self.engine.events))

    async def test_bez_jakekoliv_ceny_zustava_chyba(self):
        self.cfg.trading.entry_order_type = "MKT"
        flow = await self.zaloz(False, False, 10.0, 10.0)
        self.ib.fill(flow.entry_trade, 1, 0.0)
        await self.engine._tick()
        self.ib.price_bid = None
        self.ib.price_ask = None

        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.ERROR)



class TestSoubehuPriUzavirani(ZakladRezimu):
    """Prodej vyplněný těsně před zrušením příkazů se nesmí prodat znovu trhem."""

    def trzni_prodeje(self) -> list:
        """Tržní prodejní příkazy bez podmínek (uzavírání trhem)."""
        return [
            t for t in self.ib.placed
            if t.order.action == "SELL" and t.order.orderType == "MKT" and not t.order.conditions
        ]

    async def test_sl_vyplneny_pred_zrusenim_a_uzavrenim(self):
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=2)
        await self.nakup(flow, 2, 3.00)
        # SL příkaz se v TWS vyplnil, smyčka to ještě nezaznamenala,
        # obchodník dal Zrušit + uzavřít pozici trhem
        self.ib.fill(flow.exit_sl_trade, 2, 2.90)
        await self.engine.cancel_flow(flow.id, close_position=True)

        await self.engine._tick()

        self.assertEqual(self.trzni_prodeje(), [])
        self.assertEqual(flow.state, FlowState.CLOSED)
        self.assertAlmostEqual(flow.exit_fill_price, 2.90)
        self.assertEqual(flow.exit_reason, "SL")
        self.assertAlmostEqual(flow.unrealized_pnl, -20.0)

    async def test_castecny_prodej_pred_zrusenim_proda_trhem_jen_zbytek(self):
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=3)
        await self.nakup(flow, 3, 3.00)
        # PT limit stihl prodat 1 ks, pak byl zrušen
        self.ib.fill(flow.exit_trade, 1, 3.10, status="Cancelled")
        await self.engine.cancel_flow(flow.id, close_position=True)

        await self.engine._tick()

        trzni = self.trzni_prodeje()
        self.assertEqual(len(trzni), 1)
        self.assertEqual(int(trzni[0].order.totalQuantity), 2)
        self.assertEqual(flow.main_sold_quantity, 1)

        # Zbytek se prodá trhem; výsledná cena je vážený průměr (3,10 + 2 × 2,95) / 3
        self.ib.fill(trzni[0], 2, 2.95)
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.CLOSED)
        self.assertAlmostEqual(flow.exit_fill_price, 3.0)
        self.assertAlmostEqual(flow.unrealized_pnl, 0.0)

    async def test_uzavrit_pozici_po_vyplneni_sl_neproda_znovu(self):
        flow = await self.zaloz(True, False, 235.0, 10.0, quantity=2)
        await self.nakup(flow, 2, 3.00)
        await self.engine.close_main(flow.id)
        # Stop se vyplnil dřív, než TWS potvrdila zrušení (stav Filled, ne Cancelled)
        self.ib.fill(flow.exit_sl_trade, 2, 2.90)

        await self.engine._tick()

        self.assertEqual(self.trzni_prodeje(), [])
        self.assertEqual(flow.state, FlowState.CLOSED)
        self.assertAlmostEqual(flow.exit_fill_price, 2.90)

    async def test_uzavrit_pozici_po_castecnem_prodeji_proda_zbytek(self):
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=3)
        await self.nakup(flow, 3, 3.00)
        await self.engine.close_main(flow.id)
        # Limit stihl prodat 1 ks a pak TWS potvrdila zrušení zbytku
        self.ib.fill(flow.exit_trade, 1, 3.10, status="Cancelled")

        await self.engine._tick()

        trzni = self.trzni_prodeje()
        self.assertEqual(len(trzni), 1)
        self.assertEqual(int(trzni[0].order.totalQuantity), 2)
        self.assertIsNone(flow.exit_fill_price)

        self.ib.fill(trzni[0], 2, 2.95)
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.CLOSED)
        self.assertAlmostEqual(flow.exit_fill_price, 3.0)

    async def test_uzavrit_runner_po_vyplneni_jeho_sl_neproda_znovu(self):
        flow = await self.zaloz(False, False, 10.0, 10.0, quantity=3)
        await self.nakup(flow, 3, 3.00)
        await self.engine.set_runner(flow.id, 2.0)
        await self.engine.close_runner(flow.id)
        self.ib.fill(flow.runner_sl_trade, 1, 2.90)

        await self.engine._tick()

        self.assertEqual(self.trzni_prodeje(), [])
        self.assertFalse(flow.runner_active)
        self.assertEqual(flow.runner_sold_quantity, 1)
        self.assertAlmostEqual(flow.runner_realized_pnl, -10.0)
        # Hlavní část běží dál
        self.assertEqual(flow.state, FlowState.EXIT_ARMED)


if __name__ == "__main__":
    unittest.main()
