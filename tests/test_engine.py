"""Testy stavového automatu obchodního flow proti náhradě TWS."""

from __future__ import annotations

import sys
import asyncio
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fake_ib import UNDERLYING_CONID, FakeIBService
from tws_opce.config import AppConfig
from tws_opce.engine import FlowEngine
from tws_opce.models import FlowRequest, FlowState


def vychozi_config() -> AppConfig:
    """Konfigurace pro testy - účet 5000 USD, risk 1 %, limit spreadu 5 %."""
    cfg = AppConfig()
    cfg.account.size = 5000.0
    cfg.account.risk_pct = 1.0
    cfg.trading.max_spread_pct = 5.0
    cfg.trading.entry_order_type = "LMT_ASK"
    cfg.trading.ask_tolerance_pct = 2.0
    # Testy stavového automatu nezapisují stav na disk
    cfg.state.enabled = False
    # Automatické uzavírání se v testech zapíná cíleně s podvrženým časem -
    # jinak by sada spuštěná těsně před zavřením burzy obchody uzavírala
    cfg.trading.auto_close_enabled = False
    return cfg


class ZakladTestu(unittest.IsolatedAsyncioTestCase):
    """Společná příprava enginu s náhradou TWS."""

    def setUp(self) -> None:
        self.cfg = vychozi_config()
        self.ib = FakeIBService(self.cfg)
        self.engine = FlowEngine(self.cfg, self.ib)

    async def zaloz_call(self, **zmeny):
        """Založí vzorové CALL flow: podklad 230, vstup 232, PT 235."""
        self.ib.price_underlying = 230.0
        pozadavek = FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        for klic, hodnota in zmeny.items():
            setattr(pozadavek, klic, hodnota)
        return await self.engine.start_flow(pozadavek)

    async def zaloz_put(self, **zmeny):
        """Založí vzorové PUT flow: podklad 230, vstup 229, PT 226."""
        self.ib.price_underlying = 230.0
        pozadavek = FlowRequest(symbol="AAPL", entry_price=229.0, profit_target=226.0)
        for klic, hodnota in zmeny.items():
            setattr(pozadavek, klic, hodnota)
        return await self.engine.start_flow(pozadavek)


class TestZalozeniFlow(ZakladTestu):
    """Založení obchodu a podoba nákupního příkazu."""

    async def test_call_se_zada_s_podminkou_nahoru(self):
        flow = await self.zaloz_call()

        self.assertEqual(flow.right, "C")
        self.assertEqual(flow.state, FlowState.ARMED)
        # Strike odpovídá nejbližší dostupné ceně k PT (235)
        self.assertEqual(flow.strike, 235.0)
        # SL se dopočítal 1:1 vůči PT, tedy 232 − 3
        self.assertAlmostEqual(flow.stop_loss, 229.0)

        prikaz = self.ib.placed[0].order
        self.assertEqual(prikaz.action, "BUY")
        self.assertEqual(prikaz.orderType, "LMT")
        # LMT na ASK 3,10 + 2 % = 3,162, zaokrouhleno na tik 0,05
        self.assertAlmostEqual(prikaz.lmtPrice, 3.15)
        self.assertEqual(len(prikaz.conditions), 1)

        podminka = prikaz.conditions[0]
        self.assertEqual(podminka.conId, UNDERLYING_CONID)
        self.assertTrue(podminka.isMore)
        self.assertAlmostEqual(podminka.price, 232.0)
        # Podmínka spouští odeslání příkazu, nikoliv jeho zrušení
        self.assertFalse(prikaz.conditionsCancelOrder)

    async def test_put_se_zada_s_podminkou_dolu(self):
        self.ib.price_underlying = 230.0
        self.ib.greek_delta = -0.35
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=228.0, profit_target=225.0)
        )

        self.assertEqual(flow.right, "P")
        self.assertAlmostEqual(flow.stop_loss, 231.0)
        podminka = self.ib.placed[0].order.conditions[0]
        self.assertFalse(podminka.isMore)
        self.assertAlmostEqual(podminka.price, 228.0)

    async def test_mnozstvi_se_spocita_z_rizika_a_delty(self):
        # Riziko 50 USD, pohyb ke SL 3 USD, delta 0,35 -> 50 / 105 = 0 -> minimum 1
        flow = await self.zaloz_call()
        self.assertEqual(flow.quantity, 1)

        # Při větším účtu vyjde více kontraktů: riziko 500 / 105 = 4
        self.cfg.account.size = 50000.0
        flow2 = await self.engine.start_flow(
            FlowRequest(symbol="MSFT", entry_price=232.0, profit_target=235.0)
        )
        self.assertEqual(flow2.quantity, 4)

    async def test_zadane_mnozstvi_ma_prednost(self):
        flow = await self.zaloz_call(quantity=7)
        self.assertEqual(flow.quantity, 7)
        self.assertEqual(int(self.ib.placed[0].order.totalQuantity), 7)

    async def test_zadany_sl_ma_prednost_pred_dopoctem(self):
        flow = await self.zaloz_call(stop_loss=230.5)
        self.assertAlmostEqual(flow.stop_loss, 230.5)

    async def test_flow_pred_nakupem_se_novym_zadanim_nahradi(self):
        prvni = await self.zaloz_call()
        druhe = await self.zaloz_call(profit_target=237.5)

        # Původní čekající příkaz je pryč z trhu a obchod zmizel z přehledu
        self.assertIn(prvni.entry_trade, self.ib.cancelled)
        self.assertNotIn(prvni.id, self.engine.flows)
        # Nové zadání běží se svými parametry
        self.assertIn(druhe.id, self.engine.flows)
        self.assertAlmostEqual(druhe.profit_target, 237.5)

    async def test_runner_se_prenese_pri_nahrazeni_flow(self):
        # Runner zapnutý na čekajícím obchodu nesmí nahrazením tiše zaniknout
        prvni = await self.zaloz_call(quantity=3)
        await self.engine.set_runner(prvni.id, 2.0)

        druhe = await self.zaloz_call(quantity=3, profit_target=236.0)

        # Nové flow převzalo runner: stejný násobek (2×) na nových úrovních
        self.assertTrue(druhe.runner_active)
        self.assertEqual(druhe.runner_quantity, 1)
        self.assertAlmostEqual(druhe.runner_profit_target, 240.0)
        self.assertAlmostEqual(druhe.runner_stop_loss, druhe.stop_loss)

    async def test_runner_se_pri_nahrazeni_neprevezme_bez_dostatku_kusu(self):
        prvni = await self.zaloz_call(quantity=3)
        await self.engine.set_runner(prvni.id, 2.0)

        # Nové zadání má jen 1 kontrakt - runner na něm nemá co dělit
        druhe = await self.zaloz_call(quantity=1)

        self.assertFalse(druhe.runner_active)

    async def test_flow_s_pozici_se_novym_zadanim_neprepise(self):
        flow = await self.zaloz_call()
        # Nákup se vyplní - obchod už drží pozici a přepsat se nesmí
        self.ib.fill(flow.entry_trade, 1, 3.10)
        await self.engine._tick()

        with self.assertRaises(ValueError) as ctx:
            await self.zaloz_call()
        self.assertIn("otevřenou pozicí", str(ctx.exception))

    async def test_chybne_zadani_pt_se_odmitne(self):
        # U CALL musí PT ležet nad vstupem
        with self.assertRaises(ValueError) as ctx:
            await self.engine.start_flow(
                FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0, stop_loss=236.0)
            )
        self.assertIn("SL pod vstupní cenou", str(ctx.exception))

    async def test_trzni_prikaz_nema_limitni_cenu(self):
        self.cfg.trading.entry_order_type = "MKT"
        await self.zaloz_call()
        self.assertEqual(self.ib.placed[0].order.orderType, "MKT")

    async def test_limit_za_mid(self):
        self.cfg.trading.entry_order_type = "LMT_MID"
        await self.zaloz_call()
        # Střed trhu 3,05 leží přesně na tiku
        self.assertAlmostEqual(self.ib.placed[0].order.lmtPrice, 3.05)


class TestLongShortSoucasne(ZakladTestu):
    """Souběh long (CALL) a short (PUT) obchodu na jednom tickeru."""

    async def test_long_a_short_bezi_soucasne(self):
        long = await self.zaloz_call()
        short = await self.zaloz_put()

        # Oba obchody běží vedle sebe, žádný nebyl zrušen
        self.assertIn(long.id, self.engine.flows)
        self.assertIn(short.id, self.engine.flows)
        self.assertNotIn(long.entry_trade, self.ib.cancelled)
        self.assertEqual({long.right, short.right}, {"C", "P"})

    async def test_nove_zadani_nahradi_jen_stejny_smer(self):
        long = await self.zaloz_call()
        short = await self.zaloz_put()

        novy_long = await self.zaloz_call(profit_target=236.0)

        # Nahradil se pouze původní long; short běží dál beze změny
        self.assertNotIn(long.id, self.engine.flows)
        self.assertIn(long.entry_trade, self.ib.cancelled)
        self.assertIn(short.id, self.engine.flows)
        self.assertNotIn(short.entry_trade, self.ib.cancelled)
        self.assertIn(novy_long.id, self.engine.flows)

    async def test_short_s_pozici_neblokuje_novy_long(self):
        short = await self.zaloz_put()
        self.ib.fill(short.entry_trade, 1, 3.10)
        await self.engine._tick()

        # Long na stejném tickeru jde založit i vedle nakoupeného shortu
        long = await self.zaloz_call()
        self.assertIn(long.id, self.engine.flows)

        # Nový short se ale odmítne - short s pozicí se chrání
        with self.assertRaises(ValueError) as ctx:
            await self.zaloz_put(profit_target=225.0)
        self.assertIn("short (PUT)", str(ctx.exception))

    async def test_selhane_zadani_nenahradi_cekajici_obchod(self):
        long = await self.zaloz_call()

        # Zadání stejného směru s chybným SL selže na validaci
        with self.assertRaises(ValueError):
            await self.zaloz_call(stop_loss=236.0)

        # Původní obchod přežil - nezrušil se a zůstal v přehledu
        self.assertIn(long.id, self.engine.flows)
        self.assertNotIn(long.entry_trade, self.ib.cancelled)

    async def test_zruseni_podle_tickeru_vyzaduje_jednoznacny_smer(self):
        await self.zaloz_call()
        short = await self.zaloz_put()

        # Bez určení směru je výběr nejednoznačný
        with self.assertRaises(ValueError) as ctx:
            await self.engine.cancel_by_symbol("AAPL")
        self.assertIn("long i short", str(ctx.exception))

        # S určeným směrem se zruší jen odpovídající obchod
        zruseny = await self.engine.cancel_by_symbol("AAPL", right="P")
        self.assertIs(zruseny, short)


class TestVyberuStrike(ZakladTestu):
    """Výběr strike, když nejbližší cena z řetězce není v TWS obchodovatelná."""

    async def test_nedostupny_strike_se_nahradi_nejblizsim_obchodovatelnym(self):
        # Řetězec strike 235 nabízí, ale kontrakt pro něj v TWS neexistuje
        self.ib.unavailable_strikes = {235.0}
        preview = await self.engine.prepare("AAPL", 232.0, 235.0)

        # Vybral se další strike v pořadí podle vzdálenosti od PT
        self.assertEqual(preview.strike, 232.5)
        # SL se počítá z cen podkladu (vstup a PT), náhradní strike ho nemění
        self.assertAlmostEqual(preview.stop_loss, 229.0)
        # Náhrada se obchodníkovi hlásí varováním
        self.assertTrue(any("235" in varovani for varovani in preview.warnings))

    async def test_bez_obchodovatelneho_strike_priprava_selze(self):
        # Žádný strike z řetězce není obchodovatelný - příprava musí skončit chybou
        self.ib.unavailable_strikes = set(self.ib._strikes())
        with self.assertRaises(ValueError):
            await self.engine.prepare("AAPL", 232.0, 235.0)


class TestSmeruVstupu(ZakladTestu):
    """Obchod se zadává jen dokud cena vstupní úroveň nepřekonala."""

    async def test_call_pod_vstupem_se_zada(self):
        # Cena 230 je pod vstupem 232, průraz nahoru teprve nastane
        flow = await self.zaloz_call()
        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(len(self.ib.placed), 1)

    async def test_call_nad_vstupem_se_odmitne(self):
        # Cena už vstupní úroveň překonala - obchod ujel
        self.ib.price_underlying = 233.0
        with self.assertRaises(ValueError) as ctx:
            await self.engine.start_flow(
                FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
            )
        self.assertIn("propásnutý", str(ctx.exception))
        self.assertEqual(self.ib.placed, [])

    async def test_put_nad_vstupem_se_zada(self):
        self.ib.price_underlying = 230.0
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=228.0, profit_target=225.0)
        )
        self.assertEqual(flow.right, "P")
        self.assertEqual(flow.state, FlowState.ARMED)

    async def test_put_pod_vstupem_se_odmitne(self):
        # U PUT je to zrcadlově - cena pod vstupem znamená propásnutý průraz dolů
        self.ib.price_underlying = 230.0
        self.ib.greek_delta = -0.35
        with self.assertRaises(ValueError) as ctx:
            await self.engine.start_flow(
                FlowRequest(symbol="AAPL", entry_price=231.0, profit_target=228.0)
            )
        self.assertIn("propásnutý", str(ctx.exception))

    async def test_pri_navratu_po_spreadu_se_overi_smer(self):
        flow = await self.zaloz_call()

        # Spread vyskočí a příkaz se odstraní z trhu
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.50
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.SPREAD_BLOCKED)

        # Než se spread vrátí, cena mezitím vstupní úroveň překoná
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.10
        self.ib.price_underlying = 233.0
        flow.blocked_since = datetime.now() - timedelta(seconds=30)
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.MISSED)
        self.assertIn("vstup propásnut", flow.message)
        # Příkaz se do trhu nevrátil
        self.assertEqual(len(self.ib.placed), 1)

    async def test_propasnuty_vstup_uvolni_ticker(self):
        # Ukončený obchod nesmí blokovat nové zadání na stejném tickeru
        flow = await self.zaloz_call()
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.50
        await self.engine._tick()
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.10
        self.ib.price_underlying = 233.0
        flow.blocked_since = datetime.now() - timedelta(seconds=30)
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.MISSED)
        self.assertFalse(flow.state.is_active)

        # Nový obchod s vyšším vstupem projde
        novy = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=235.0, profit_target=238.0)
        )
        self.assertEqual(novy.state, FlowState.ARMED)

    async def test_bez_ceny_podkladu_se_prikaz_nezada(self):
        flow = await self.zaloz_call()
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.50
        await self.engine._tick()

        # Cena podkladu přestane chodit - není podle čeho rozhodnout
        self.ib.price_underlying = None
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.10
        flow.blocked_since = datetime.now() - timedelta(seconds=30)
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.NO_QUOTES)
        self.assertEqual(len(self.ib.placed), 1)


class TestSpread(ZakladTestu):
    """Hlídání spreadu před nákupem."""

    async def test_siroky_spread_zabrani_zadani_prikazu(self):
        # BID 3,00 / ASK 3,50 = 15,4 % > limit 5 %
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.50
        flow = await self.zaloz_call()

        self.assertEqual(flow.state, FlowState.SPREAD_BLOCKED)
        self.assertEqual(self.ib.placed, [])

    async def test_rozsireny_spread_odstrani_prikaz_z_trhu(self):
        flow = await self.zaloz_call()
        self.assertEqual(flow.state, FlowState.ARMED)

        # Spread se rozšíří nad limit
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.50
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.SPREAD_BLOCKED)
        self.assertEqual(len(self.ib.cancelled), 1)
        self.assertIsNone(flow.entry_trade)

    async def test_zuzeny_spread_vrati_prikaz_do_trhu(self):
        flow = await self.zaloz_call()
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.50
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.SPREAD_BLOCKED)

        # Spread se vrátí do limitu, ale prodleva po odstranění ještě neuplynula
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.10
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.SPREAD_BLOCKED)

        # Po uplynutí prodlevy se příkaz vrátí do trhu
        flow.blocked_since = datetime.now() - timedelta(seconds=30)
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(len(self.ib.placed), 2)

    async def test_spread_tesne_pod_limitem_prikaz_nevrati(self):
        # Rezerva brání opakovanému zadávání a rušení při kolísání kolem limitu
        flow = await self.zaloz_call()
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.50
        await self.engine._tick()
        flow.blocked_since = datetime.now() - timedelta(seconds=30)

        # Spread 4,88 % je pod limitem 5 %, ale nad prahem 4,5 % (rezerva 10 %)
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.15
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.SPREAD_BLOCKED)

        # Po dalším zúžení pod práh se příkaz vrátí
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.10
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.ARMED)

    async def test_vlastni_limit_spreadu_ze_zadani(self):
        # Spread 3,3 % překračuje zadaný limit 2 %
        flow = await self.zaloz_call(max_spread_pct=2.0)
        self.assertEqual(flow.state, FlowState.SPREAD_BLOCKED)

    async def test_vypnute_ruseni_ponecha_prikaz_v_trhu(self):
        self.cfg.trading.cancel_on_spread_breach = False
        flow = await self.zaloz_call()
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.50
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(self.ib.cancelled, [])


class TestChybejiciKotace(ZakladTestu):
    """Chování, když z TWS nedorazily kotace opce (mimo obchodní hodiny)."""

    async def test_limitni_prikaz_se_nezmeni_na_trzni(self):
        # Bez ASK nelze určit limitní cenu - příkaz se nesmí zadat jako tržní
        self.ib.price_bid, self.ib.price_ask = None, None
        flow = await self.zaloz_call()

        self.assertEqual(flow.state, FlowState.NO_QUOTES)
        self.assertEqual(self.ib.placed, [])
        self.assertIn("limitní příkaz zatím nelze zadat", flow.message)

    async def test_po_prichodu_kotaci_se_prikaz_zada(self):
        self.ib.price_bid, self.ib.price_ask = None, None
        flow = await self.zaloz_call()
        self.assertEqual(flow.state, FlowState.NO_QUOTES)

        # TWS začne posílat kotace
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.10
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(len(self.ib.placed), 1)
        self.assertAlmostEqual(self.ib.placed[0].order.lmtPrice, 3.15)

    async def test_kotace_se_sirokym_spreadem_prikaz_nezadaji(self):
        self.ib.price_bid, self.ib.price_ask = None, None
        flow = await self.zaloz_call()

        # Kotace dorazí, ale spread je nad limitem
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.50
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.SPREAD_BLOCKED)
        self.assertEqual(self.ib.placed, [])

    async def test_trzni_prikaz_se_zada_i_bez_kotaci(self):
        # U nastavení MKT je zadání bez kotací v pořádku
        self.cfg.trading.entry_order_type = "MKT"
        self.ib.price_bid, self.ib.price_ask = None, None
        flow = await self.zaloz_call()

        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(self.ib.placed[0].order.orderType, "MKT")


class TestPrubehnaAktualizaceLimitu(ZakladTestu):
    """Průběžná úprava limitní ceny nevyplněného nákupního příkazu."""

    async def test_limit_se_upravi_pri_vetsi_zmene_ask(self):
        flow = await self.zaloz_call()
        puvodni = flow.entry_limit

        # ASK vyroste na 3,60 -> limit 3,672 -> na tik 3,65
        self.ib.price_bid, self.ib.price_ask = 3.55, 3.60
        await self.engine._tick()

        self.assertAlmostEqual(flow.entry_limit, 3.65)
        self.assertNotAlmostEqual(flow.entry_limit, puvodni)
        # Modifikace se posílá pod stejným orderId
        self.assertEqual(len(self.ib.placed), 1)
        self.assertAlmostEqual(self.ib.placed[0].order.lmtPrice, 3.65)

    async def test_drobna_zmena_prikaz_nemodifikuje(self):
        flow = await self.zaloz_call()
        puvodni = flow.entry_limit

        # Změna pod prahem 0,5 % se ignoruje
        self.ib.price_ask = 3.11
        await self.engine._tick()

        self.assertAlmostEqual(flow.entry_limit, puvodni)

    async def test_castecne_vyplneny_prikaz_se_nemodifikuje(self):
        # Modifikace vyplňovaného příkazu by závodila s TWS a končila
        # hlášením "too late to replace" - příkaz se nechává být
        flow = await self.zaloz_call()
        puvodni = flow.entry_limit

        flow.entry_trade.orderStatus.filled = 1
        self.ib.price_bid, self.ib.price_ask = 3.55, 3.60
        self.assertFalse(self.engine._update_entry_limit(flow))

        self.assertAlmostEqual(flow.entry_limit, puvodni)

    async def test_ruseny_prikaz_se_nemodifikuje(self):
        # Příkaz čekající na potvrzení zrušení nelze upravit - TWS to odmítne
        # hláškou "Order has been cancelled already, too late to replace"
        flow = await self.zaloz_call()
        puvodni = flow.entry_limit
        flow.entry_trade.orderStatus.status = "PendingCancel"

        self.ib.price_bid, self.ib.price_ask = 3.55, 3.60
        await self.engine._tick()

        self.assertAlmostEqual(flow.entry_limit, puvodni)
        self.assertAlmostEqual(self.ib.placed[0].order.lmtPrice, puvodni)

    async def test_ruseny_prikaz_se_nerusi_znovu(self):
        flow = await self.zaloz_call()
        flow.entry_trade.orderStatus.status = "PendingCancel"

        # Rozšíření spreadu by jinak vyvolalo zrušení příkazu
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.50
        await self.engine._tick()
        self.assertEqual(self.ib.cancelled, [])

    async def test_vypnuta_aktualizace_limit_nemeni(self):
        self.cfg.trading.relimit_enabled = False
        flow = await self.zaloz_call()
        puvodni = flow.entry_limit

        self.ib.price_bid, self.ib.price_ask = 3.55, 3.60
        await self.engine._tick()

        self.assertAlmostEqual(flow.entry_limit, puvodni)


class TestNakupAVystup(ZakladTestu):
    """Vyplnění nákupu a zadání prodejního příkazu."""

    async def test_po_nakupu_se_zada_jeden_prodejni_prikaz(self):
        flow = await self.zaloz_call(quantity=2)
        self.ib.fill(flow.entry_trade, 2, 3.10)

        # První průchod zaznamená nákup
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.FILLED)
        self.assertAlmostEqual(flow.fill_price, 3.10)

        # Druhý průchod zadá prodejní příkaz
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.EXIT_ARMED)

        prodej = self.ib.placed[-1].order
        self.assertEqual(prodej.action, "SELL")
        self.assertEqual(prodej.orderType, "MKT")
        self.assertEqual(int(prodej.totalQuantity), 2)

        # Jediný příkaz nese obě podmínky spojené logickým OR
        self.assertEqual(len(prodej.conditions), 2)
        pt, sl = prodej.conditions
        self.assertTrue(pt.isMore)
        self.assertAlmostEqual(pt.price, 235.0)
        self.assertFalse(sl.isMore)
        self.assertAlmostEqual(sl.price, 229.0)
        # Spojka váže podmínku k následující, proto 'o' (OR) nese první z nich.
        # Opačné pořadí znamená v TWS AND a příkaz by se nikdy nespustil.
        self.assertEqual(pt.conjunction, "o")
        self.assertEqual(sl.conjunction, "a")

    async def test_prodejni_podminky_putu_maji_opacne_smery(self):
        self.ib.price_underlying = 230.0
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=228.0, profit_target=225.0)
        )
        self.ib.fill(flow.entry_trade, 1, 3.10)
        await self.engine._tick()
        await self.engine._tick()

        pt, sl = self.ib.placed[-1].order.conditions
        # PUT: PT je pod vstupem, SL nad ním
        self.assertFalse(pt.isMore)
        self.assertAlmostEqual(pt.price, 225.0)
        self.assertTrue(sl.isMore)
        self.assertAlmostEqual(sl.price, 231.0)

    async def test_vystupni_limitni_prikaz(self):
        self.cfg.trading.exit_order_type = "LMT"
        flow = await self.zaloz_call()
        self.ib.fill(flow.entry_trade, 1, 3.10)
        await self.engine._tick()
        await self.engine._tick()

        prodej = self.ib.placed[-1].order
        self.assertEqual(prodej.orderType, "LMT")
        # BID 3,00 − 2 % = 2,94, zaokrouhleno na tik 0,05
        self.assertAlmostEqual(prodej.lmtPrice, 2.95)

    async def test_castecne_vyplneni_zrusi_zbytek_nakupu(self):
        # TWS nepovolí nákupní a prodejní příkaz současně na stejném kontraktu,
        # proto se nevyplněný zbytek nákupu ruší a zajistí se koupené množství
        flow = await self.zaloz_call(quantity=5)
        nakup = flow.entry_trade
        self.ib.fill(nakup, 2, 3.10, status="Submitted")

        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.FILLED)

        # Druhý průchod zruší zbytek nákupu, prodej se ještě nezadává
        await self.engine._tick()
        self.assertIn(nakup, self.ib.cancelled)
        self.assertEqual(len(self.ib.placed), 1)
        self.assertIn("ruší se nevyplněný zbytek", flow.message)

        # Až po potvrzení zrušení se zadá prodejní příkaz na nakoupené množství
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.EXIT_ARMED)
        self.assertEqual(int(self.ib.placed[-1].order.totalQuantity), 2)
        self.assertEqual(flow.filled_quantity, 2)

    async def test_uplne_vyplneni_zbytek_nerusi(self):
        # Při úplném vyplnění není co rušit, prodej se zadá rovnou
        flow = await self.zaloz_call(quantity=2)
        self.ib.fill(flow.entry_trade, 2, 3.10)

        await self.engine._tick()
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.EXIT_ARMED)
        self.assertEqual(self.ib.cancelled, [])

    async def test_ruseny_prodejni_prikaz_se_nemodifikuje(self):
        # Ani množství se neupravuje u příkazu, který TWS už ruší
        flow = await self.zaloz_call(quantity=5)
        self.ib.fill(flow.entry_trade, 2, 3.10, status="Submitted")
        await self.engine._tick()
        await self.engine._tick()
        await self.engine._tick()
        self.assertEqual(int(self.ib.placed[-1].order.totalQuantity), 2)

        flow.exit_trade.orderStatus.status = "PendingCancel"
        self.ib.fill(flow.entry_trade, 5, 3.12)
        await self.engine._tick()
        self.assertEqual(int(self.ib.placed[-1].order.totalQuantity), 2)

    async def test_dodatecne_doplneny_nakup_navysi_prodej(self):
        # Pojistka pro případ, že se nákup doplní ještě před potvrzením zrušení
        flow = await self.zaloz_call(quantity=5)
        self.ib.fill(flow.entry_trade, 2, 3.10, status="Submitted")
        await self.engine._tick()
        await self.engine._tick()
        await self.engine._tick()
        self.assertEqual(int(self.ib.placed[-1].order.totalQuantity), 2)

        self.ib.fill(flow.entry_trade, 5, 3.12)
        await self.engine._tick()
        self.assertEqual(int(self.ib.placed[-1].order.totalQuantity), 5)
        self.assertEqual(flow.filled_quantity, 5)

    async def test_pl_pocita_se_skutecne_nakoupenym_mnozstvim(self):
        # Zadáno 5 kontraktů, vyplněny jen 2 - výsledek nesmí počítat s pěti
        flow = await self.zaloz_call(quantity=5)
        self.ib.fill(flow.entry_trade, 2, 3.00, status="Submitted")
        await self.engine._tick()
        await self.engine._tick()
        await self.engine._tick()

        self.assertEqual(flow.filled_quantity, 2)
        self.ib.price_bid, self.ib.price_ask = 3.90, 4.10
        await self.engine._tick()
        # (4,00 − 3,00) * 2 kontrakty * 100
        self.assertAlmostEqual(flow.unrealized_pnl, 200.0)

    async def test_uzavreni_pozice_na_pt(self):
        flow = await self.zaloz_call(quantity=2)
        self.ib.fill(flow.entry_trade, 2, 3.00)
        await self.engine._tick()
        await self.engine._tick()

        # Podklad dosáhl PT a prodej se vyplnil za 4,00
        self.ib.price_underlying = 235.5
        self.ib.fill(flow.exit_trade, 2, 4.00)
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.CLOSED)
        self.assertEqual(flow.exit_reason, "PT")
        # Zisk = (4,00 − 3,00) * 2 kontrakty * 100
        self.assertAlmostEqual(flow.unrealized_pnl, 200.0)

    async def test_uzavreni_pozice_na_sl(self):
        flow = await self.zaloz_call(quantity=1)
        self.ib.fill(flow.entry_trade, 1, 3.00)
        await self.engine._tick()
        await self.engine._tick()

        self.ib.price_underlying = 228.5
        self.ib.fill(flow.exit_trade, 1, 1.80)
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.CLOSED)
        self.assertEqual(flow.exit_reason, "SL")
        self.assertAlmostEqual(flow.unrealized_pnl, -120.0)

    async def test_duvod_vystupu_urci_blizsi_uroven(self):
        # Podmínka PT (>= 235) se splnila, ale podklad do zápisu prodeje couvl
        # těsně pod cíl - důvodem výstupu je stále PT, ne SL
        flow = await self.zaloz_call(quantity=1)
        self.ib.fill(flow.entry_trade, 1, 3.00)
        await self.engine._tick()
        await self.engine._tick()

        self.ib.price_underlying = 234.6
        self.ib.fill(flow.exit_trade, 1, 3.90)
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.CLOSED)
        self.assertEqual(flow.exit_reason, "PT")

    async def test_zruseni_prodejniho_prikazu_v_tws_hlasi_chybu(self):
        flow = await self.zaloz_call()
        self.ib.fill(flow.entry_trade, 1, 3.00)
        await self.engine._tick()
        await self.engine._tick()

        # Uživatel zrušil prodejní příkaz přímo v TWS
        flow.exit_trade.orderStatus.status = "Cancelled"
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.ERROR)
        self.assertIn("bez zajištění", flow.message)


class TestZmenyCile(ZakladTestu):
    """Posunutí cílové úrovně u běžícího obchodu."""

    async def test_zmena_pred_nakupem_ponecha_strike(self):
        flow = await self.zaloz_call()
        puvodni_strike = flow.strike
        pocet_prikazu = len(self.ib.placed)

        await self.engine.change_profit_target(flow.id, 238.0)

        self.assertAlmostEqual(flow.profit_target, 238.0)
        self.assertEqual(flow.strike, puvodni_strike)
        # Nákupní příkaz se nijak nedotkne
        self.assertEqual(len(self.ib.placed), pocet_prikazu)
        self.assertEqual(self.ib.cancelled, [])

    async def test_zmena_po_nakupu_upravi_zajistovaci_prikaz(self):
        flow = await self.zaloz_call(quantity=2)
        self.ib.fill(flow.entry_trade, 2, 3.00)
        await self.engine._tick()
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.EXIT_ARMED)

        await self.engine.change_profit_target(flow.id, 240.0)

        podminky = self.ib.placed[-1].order.conditions
        self.assertAlmostEqual(podminky[0].price, 240.0)
        # SL zůstává beze změny a spojka dál znamená OR
        self.assertAlmostEqual(podminky[1].price, 229.0)
        self.assertEqual(podminky[0].conjunction, "o")
        self.assertEqual(podminky[1].conjunction, "a")

    async def test_nasobek_se_pocita_z_puvodniho_cile(self):
        # Vstup 232, původní PT 235 -> vzdálenost 3 body
        flow = await self.zaloz_call()
        self.assertAlmostEqual(flow.original_profit_target, 235.0)

        await self.engine.change_profit_target(flow.id, 238.0)
        self.assertAlmostEqual(flow.pt_multiple, 2.0)

        # Další změna vychází stále z původních 3 bodů, ne z posunutých 6
        await self.engine.change_profit_target(flow.id, 241.0)
        self.assertAlmostEqual(flow.pt_multiple, 3.0)

    async def test_cil_na_spatne_strane_se_odmitne(self):
        flow = await self.zaloz_call()
        with self.assertRaises(ValueError) as ctx:
            await self.engine.change_profit_target(flow.id, 230.0)
        self.assertIn("nad vstupní cenou", str(ctx.exception))

    async def test_cil_u_ukonceneho_obchodu_nelze_menit(self):
        flow = await self.zaloz_call()
        await self.engine.cancel_flow(flow.id)
        with self.assertRaises(ValueError) as ctx:
            await self.engine.change_profit_target(flow.id, 238.0)
        self.assertIn("běžícího obchodu", str(ctx.exception))

    async def test_prepocet_strike_zada_prikaz_znovu(self):
        # Nastavení recalculate vybere podle nového cíle jiný kontrakt
        self.cfg.trading.pt_change_strike = "recalculate"
        flow = await self.zaloz_call()
        puvodni_strike = flow.strike
        puvodni_prikaz = flow.entry_trade

        await self.engine.change_profit_target(flow.id, 240.0)

        self.assertNotEqual(flow.strike, puvodni_strike)
        self.assertIn(puvodni_prikaz, self.ib.cancelled)
        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertIsNotNone(flow.entry_trade)


class TestRunner(ZakladTestu):
    """Runner - část pozice prodávaná samostatným příkazem s vlastním cílem."""

    async def nakup(self, flow, mnozstvi):
        """Simuluje vyplnění nákupu a zadání zajišťovacích příkazů."""
        self.ib.fill(flow.entry_trade, mnozstvi, 3.00)
        await self.engine._tick()
        await self.engine._tick()

    async def test_runner_pred_nakupem_rozdeli_prodej(self):
        # Runner zapnutý před nákupem: po vyplnění vzniknou dva prodejní příkazy
        flow = await self.zaloz_call(quantity=3)
        await self.engine.set_runner(flow.id, 2.0)
        self.assertTrue(flow.runner_active)

        await self.nakup(flow, 3)
        self.assertEqual(flow.state, FlowState.EXIT_ARMED)

        prodeje = [t for t in self.ib.placed if t.order.action == "SELL"]
        self.assertEqual(len(prodeje), 2)

        hlavni = next(t for t in prodeje if t.order.orderRef.endswith(":exit"))
        runner = next(t for t in prodeje if t.order.orderRef.endswith(":runner"))
        self.assertEqual(int(hlavni.order.totalQuantity), 2)
        self.assertEqual(int(runner.order.totalQuantity), 1)
        # Hlavní část prodává na PT 235, runner na dvojnásobku (238); SL sdílí
        self.assertAlmostEqual(hlavni.order.conditions[0].price, 235.0)
        self.assertAlmostEqual(runner.order.conditions[0].price, 238.0)
        self.assertAlmostEqual(runner.order.conditions[1].price, 229.0)

    async def test_runner_za_behu_zmensi_hlavni_prikaz(self):
        flow = await self.zaloz_call(quantity=3)
        await self.nakup(flow, 3)
        self.assertEqual(int(flow.exit_trade.order.totalQuantity), 3)

        await self.engine.set_runner(flow.id, 1.5)

        self.assertEqual(int(flow.exit_trade.order.totalQuantity), 2)
        self.assertIsNotNone(flow.runner_trade)
        self.assertEqual(int(flow.runner_trade.order.totalQuantity), 1)
        self.assertAlmostEqual(flow.runner_trade.order.conditions[0].price, 236.5)

    async def test_zruseni_runneru_slouci_prodej(self):
        flow = await self.zaloz_call(quantity=3)
        await self.nakup(flow, 3)
        await self.engine.set_runner(flow.id, 2.0)
        runner_trade = flow.runner_trade

        await self.engine.cancel_runner(flow.id)

        self.assertFalse(flow.runner_active)
        self.assertIn(runner_trade, self.ib.cancelled)
        self.assertEqual(int(flow.exit_trade.order.totalQuantity), 3)

    async def test_runner_vyzaduje_vetsi_mnozstvi(self):
        flow = await self.zaloz_call(quantity=1)
        with self.assertRaises(ValueError) as ctx:
            await self.engine.set_runner(flow.id, 2.0)
        self.assertIn("větším množstvím", str(ctx.exception))

    async def test_zmena_cile_bezicicho_runneru(self):
        flow = await self.zaloz_call(quantity=3)
        await self.nakup(flow, 3)
        await self.engine.set_runner(flow.id, 2.0)
        prodeju_pred = len([t for t in self.ib.placed if t.order.action == "SELL"])

        await self.engine.set_runner(flow.id, 3.0)

        # Žádný nový příkaz - jen upravené podmínky stávajícího
        prodeju_po = len([t for t in self.ib.placed if t.order.action == "SELL"])
        self.assertEqual(prodeju_po, prodeju_pred)
        self.assertAlmostEqual(flow.runner_trade.order.conditions[0].price, 241.0)

    async def test_hlavni_cast_prodana_runner_bezi_dal(self):
        flow = await self.zaloz_call(quantity=3)
        await self.engine.set_runner(flow.id, 2.0)
        await self.nakup(flow, 3)

        # Hlavní část dosáhne PT
        self.ib.price_underlying = 236.0
        self.ib.fill(flow.exit_trade, 2, 4.00)
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.EXIT_ARMED)
        self.assertIn("runner", flow.message)
        self.assertIsNotNone(flow.exit_fill_price)
        self.assertIsNone(flow.runner_fill_price)

        # Runner dosáhne svého cíle - obchod se uzavře s kombinovaným výsledkem
        self.ib.price_underlying = 238.5
        self.ib.fill(flow.runner_trade, 1, 5.50)
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.CLOSED)
        # (4,00 − 3,00) × 2 ks + (5,50 − 3,00) × 1 ks, vše × 100
        self.assertAlmostEqual(flow.unrealized_pnl, 450.0)

    async def test_sl_proda_obe_casti(self):
        flow = await self.zaloz_call(quantity=3)
        await self.engine.set_runner(flow.id, 2.0)
        await self.nakup(flow, 3)

        self.ib.price_underlying = 228.5
        self.ib.fill(flow.exit_trade, 2, 1.80)
        self.ib.fill(flow.runner_trade, 1, 1.80)
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.CLOSED)
        self.assertEqual(flow.exit_reason, "SL")
        self.assertAlmostEqual(flow.unrealized_pnl, -360.0)

    async def test_velikost_runneru_z_konfigurace(self):
        self.cfg.trading.runner_quantity = 2
        flow = await self.zaloz_call(quantity=5)
        await self.engine.set_runner(flow.id, 2.0)
        await self.nakup(flow, 5)

        hlavni = next(t for t in self.ib.placed if t.order.orderRef.endswith(":exit"))
        runner = next(t for t in self.ib.placed if t.order.orderRef.endswith(":runner"))
        self.assertEqual(int(hlavni.order.totalQuantity), 3)
        self.assertEqual(int(runner.order.totalQuantity), 2)

    async def test_zruseni_flow_rusi_i_runner(self):
        flow = await self.zaloz_call(quantity=3)
        await self.nakup(flow, 3)
        await self.engine.set_runner(flow.id, 2.0)
        runner_trade = flow.runner_trade

        await self.engine.cancel_flow(flow.id)
        self.assertIn(runner_trade, self.ib.cancelled)

    async def test_uzavreni_trhem_zrusi_runner_a_proda_vse(self):
        flow = await self.zaloz_call(quantity=3)
        await self.nakup(flow, 3)
        await self.engine.set_runner(flow.id, 2.0)

        await self.engine.cancel_flow(flow.id, close_position=True)
        self.assertEqual(flow.state, FlowState.CLOSING)
        await self.engine._tick()

        trzni = self.ib.placed[-1].order
        self.assertEqual(trzni.orderType, "MKT")
        self.assertEqual(int(trzni.totalQuantity), 3)
        self.assertEqual(trzni.conditions, [])

    async def test_runner_zruseny_v_tws_prevezme_hlavni_prikaz(self):
        # Ruční zrušení příkazu runneru v TWS nesmí nechat kusy bez zajištění
        flow = await self.zaloz_call(quantity=3)
        await self.nakup(flow, 3)
        await self.engine.set_runner(flow.id, 2.0)

        flow.runner_trade.orderStatus.status = "Cancelled"
        await self.engine._tick()

        self.assertFalse(flow.runner_active)
        self.assertEqual(int(flow.exit_trade.order.totalQuantity), 3)
        self.assertEqual(flow.state, FlowState.EXIT_ARMED)

    async def test_ocekavany_zisk_kombinuje_obe_casti(self):
        flow = await self.zaloz_call(quantity=3)
        await self.engine._tick()
        bez_runneru = flow.expected_profit

        await self.engine.set_runner(flow.id, 3.0)
        await self.engine._tick()

        # Runner míří na vzdálenější cíl, takže očekávaný zisk musí vzrůst
        self.assertIsNotNone(flow.expected_profit)
        self.assertGreater(flow.expected_profit, bez_runneru)


class TestOdlozenehoRunneru(ZakladTestu):
    """Runner při částečném vyplnění nákupu - odložení, doplnění a úklid."""

    async def priprav_castecny_nakup(self):
        """Runner před nákupem, vyplní se ale jen 1 ks ze 3 - runner se odloží."""
        flow = await self.zaloz_call(quantity=3)
        await self.engine.set_runner(flow.id, 2.0)

        # Částečné vyplnění: příkaz zůstává aktivní, smyčka ruší zbytek nákupu
        self.ib.fill(flow.entry_trade, 1, 3.00, status="Submitted")
        await self.engine._tick()  # registrace nákupu
        await self.engine._tick()  # žádost o zrušení nevyplněného zbytku
        await self.engine._tick()  # prodej 1 ks jedním příkazem, runner odložen
        return flow

    async def test_castecny_nakup_prodava_bez_runneru(self):
        flow = await self.priprav_castecny_nakup()

        self.assertEqual(flow.state, FlowState.EXIT_ARMED)
        self.assertIsNone(flow.runner_trade)
        self.assertEqual(int(flow.exit_trade.order.totalQuantity), 1)
        # Runner zůstává zapamatovaný pro případ doplnění nákupu
        self.assertTrue(flow.runner_active)

    async def test_odlozeny_runner_se_oddeli_po_doplneni_nakupu(self):
        flow = await self.priprav_castecny_nakup()

        # Vyplnění předběhlo zrušení - nákup se dodatečně doplnil na 3 ks
        flow.entry_trade.orderStatus.filled = 3
        await self.engine._tick()

        # Pozice je celá zajištěná a runner má vlastní příkaz
        self.assertIsNotNone(flow.runner_trade)
        self.assertEqual(int(flow.exit_trade.order.totalQuantity), 2)
        self.assertEqual(int(flow.runner_trade.order.totalQuantity), 1)

    async def test_runner_se_zrusi_kdyz_na_nej_nakup_nestaci(self):
        flow = await self.priprav_castecny_nakup()

        # Nákup se už nedoplní - odložený runner se ruší, aby nevisel bez příkazu
        await self.engine._tick()

        self.assertFalse(flow.runner_active)
        self.assertEqual(int(flow.exit_trade.order.totalQuantity), 1)


class TestZaseknutehoProdeje(ZakladTestu):
    """Tržní prodej, který TWS drží nevyplněný, se po prodlevě zadá znovu."""

    async def test_zaseknuty_trzni_prodej_runneru_se_zada_znovu(self):
        flow = await self.zaloz_call(quantity=3)
        self.ib.fill(flow.entry_trade, 3, 3.00)
        await self.engine._tick()
        await self.engine._tick()
        await self.engine.set_runner(flow.id, 2.0)

        await self.engine.close_runner(flow.id)
        await self.engine._tick()  # po zrušení podmíněného příkazu se zadá MKT
        prvni = flow.runner_trade
        self.assertEqual(prvni.order.orderType, "MKT")
        # Tržní příkaz má platnost DAY - GTC drží TWS bez vyplnění
        self.assertEqual(prvni.order.tif, "DAY")
        self.assertEqual(flow.runner_market_attempts, 1)

        # TWS příkaz drží nevyplněný déle, než hlídač dovoluje
        flow.runner_market_sent = datetime.now() - timedelta(seconds=60)
        await self.engine._tick()  # hlídač příkaz zruší
        self.assertIn(prvni, self.ib.cancelled)

        await self.engine._tick()  # smyčka zadá nový tržní prodej
        self.assertIsNot(flow.runner_trade, prvni)
        self.assertEqual(flow.runner_market_attempts, 2)

    async def test_po_vycerpani_pokusu_zustava_prikaz_a_varovani(self):
        flow = await self.zaloz_call(quantity=3)
        self.ib.fill(flow.entry_trade, 3, 3.00)
        await self.engine._tick()
        await self.engine._tick()
        await self.engine.set_runner(flow.id, 2.0)
        await self.engine.close_runner(flow.id)
        await self.engine._tick()

        # Pokusy jsou vyčerpané - hlídač už příkaz neruší a jednou varuje
        flow.runner_market_attempts = 5
        flow.runner_market_sent = datetime.now() - timedelta(seconds=60)
        posledni = flow.runner_trade
        await self.engine._tick()

        self.assertNotIn(posledni, self.ib.cancelled)
        self.assertIs(flow.runner_trade, posledni)
        zpravy = [text for _, text in self.engine.events]
        self.assertTrue(any("POZOR" in z and "runneru" in z for z in zpravy))


class TestUzavreniCastiPozice(ZakladTestu):
    """Okamžité uzavření hlavní části nebo runneru tržním příkazem."""

    async def nakup(self, flow, mnozstvi):
        """Simuluje vyplnění nákupu a zadání zajišťovacích příkazů."""
        self.ib.fill(flow.entry_trade, mnozstvi, 3.00)
        await self.engine._tick()
        await self.engine._tick()

    async def test_uzavreni_cele_pozice_bez_runneru(self):
        flow = await self.zaloz_call(quantity=2)
        await self.nakup(flow, 2)
        podmineny = flow.exit_trade

        await self.engine.close_main(flow.id)
        # Podmíněný příkaz se ruší; tržní prodej až po potvrzení
        self.assertIn(podmineny, self.ib.cancelled)

        await self.engine._tick()
        trzni = flow.exit_trade
        self.assertEqual(trzni.order.orderType, "MKT")
        self.assertEqual(int(trzni.order.totalQuantity), 2)
        self.assertEqual(trzni.order.conditions, [])

        self.ib.fill(trzni, 2, 3.40)
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.CLOSED)
        self.assertEqual(flow.exit_reason, "ručně")
        self.assertAlmostEqual(flow.unrealized_pnl, 80.0)

    async def test_uzavreni_hlavni_casti_runner_bezi_dal(self):
        flow = await self.zaloz_call(quantity=3)
        await self.engine.set_runner(flow.id, 2.0)
        await self.nakup(flow, 3)
        runner_trade = flow.runner_trade

        await self.engine.close_main(flow.id)
        await self.engine._tick()

        trzni = flow.exit_trade
        self.assertEqual(trzni.order.orderType, "MKT")
        self.assertEqual(int(trzni.order.totalQuantity), 2)
        # Runner zůstává nedotčený
        self.assertIs(flow.runner_trade, runner_trade)
        self.assertNotIn(runner_trade, self.ib.cancelled)

        self.ib.fill(trzni, 2, 3.40)
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.EXIT_ARMED)
        self.assertIn("runner", flow.message)

        # Runner později dosáhne cíle a obchod se uzavře s kombinovaným výsledkem
        self.ib.price_underlying = 238.5
        self.ib.fill(flow.runner_trade, 1, 5.00)
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.CLOSED)
        self.assertAlmostEqual(flow.unrealized_pnl, 280.0)

    async def test_uzavreni_runneru_hlavni_bezi_dal(self):
        flow = await self.zaloz_call(quantity=3)
        await self.engine.set_runner(flow.id, 2.0)
        await self.nakup(flow, 3)
        hlavni = flow.exit_trade
        podmineny_runner = flow.runner_trade

        await self.engine.close_runner(flow.id)
        self.assertIn(podmineny_runner, self.ib.cancelled)

        await self.engine._tick()
        trzni = flow.runner_trade
        self.assertEqual(trzni.order.orderType, "MKT")
        self.assertEqual(int(trzni.order.totalQuantity), 1)
        # Hlavní příkaz zůstává v původním množství - žádné převzetí kusů
        self.assertIs(flow.exit_trade, hlavni)
        self.assertEqual(int(hlavni.order.totalQuantity), 2)

        self.ib.fill(trzni, 1, 3.20)
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.EXIT_ARMED)
        # Prodaný runner se zúčtoval a jeho pole se uvolnila pro další runner
        self.assertFalse(flow.runner_active)
        self.assertEqual(flow.runner_sold_quantity, 1)
        self.assertAlmostEqual(flow.runner_realized_pnl, 20.0)

        # Hlavní část dosáhne PT a obchod se uzavře
        self.ib.price_underlying = 236.0
        self.ib.fill(hlavni, 2, 4.00)
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.CLOSED)
        # (4,00 − 3,00) × 2 + (3,20 − 3,00) × 1, vše × 100
        self.assertAlmostEqual(flow.unrealized_pnl, 220.0)

    async def test_uzavrit_nelze_pred_nakupem(self):
        flow = await self.zaloz_call(quantity=2)
        with self.assertRaises(ValueError) as ctx:
            await self.engine.close_main(flow.id)
        self.assertIn("nakoupenou pozici", str(ctx.exception))

    async def test_uzavrit_runner_bez_runneru_nelze(self):
        flow = await self.zaloz_call(quantity=2)
        await self.nakup(flow, 2)
        with self.assertRaises(ValueError):
            await self.engine.close_runner(flow.id)

    async def test_opakovane_uzavreni_se_odmitne(self):
        flow = await self.zaloz_call(quantity=2)
        await self.nakup(flow, 2)
        await self.engine.close_main(flow.id)
        with self.assertRaises(ValueError) as ctx:
            await self.engine.close_main(flow.id)
        self.assertIn("už probíhá", str(ctx.exception))

    async def test_cil_runneru_lze_menit_i_pri_uzavirani_hlavni_casti(self):
        # Uzavření hlavní části se runneru netýká - jeho cíl musí jít dál posouvat
        flow = await self.zaloz_call(quantity=3)
        await self.engine.set_runner(flow.id, 2.0)
        await self.nakup(flow, 3)
        await self.engine.close_main(flow.id)

        await self.engine.set_runner(flow.id, 3.0)
        self.assertAlmostEqual(flow.runner_trade.order.conditions[0].price, 241.0)

    async def test_po_prodeji_hlavni_casti_nelze_runner_zrusit(self):
        # Sloučení zpět není kam provést - runner lze jen uzavřít, nebo nechat běžet
        flow = await self.zaloz_call(quantity=3)
        await self.engine.set_runner(flow.id, 2.0)
        await self.nakup(flow, 3)
        self.ib.price_underlying = 236.0
        self.ib.fill(flow.exit_trade, 2, 4.00)
        await self.engine._tick()

        with self.assertRaises(ValueError) as ctx:
            await self.engine.cancel_runner(flow.id)
        self.assertIn("jen uzavřít trhem", str(ctx.exception))

        # Uzavření runneru trhem naopak projít musí
        await self.engine.close_runner(flow.id)
        await self.engine._tick()
        self.assertEqual(flow.runner_trade.order.orderType, "MKT")

    async def test_cil_nelze_menit_po_prodeji_hlavni_casti(self):
        # Hlavní část je prodaná, runner běží - její cíl už nemá co řídit
        flow = await self.zaloz_call(quantity=3)
        await self.engine.set_runner(flow.id, 2.0)
        await self.nakup(flow, 3)
        self.ib.price_underlying = 236.0
        self.ib.fill(flow.exit_trade, 2, 4.00)
        await self.engine._tick()

        with self.assertRaises(ValueError) as ctx:
            await self.engine.change_profit_target(flow.id, 240.0)
        self.assertIn("nelze měnit", str(ctx.exception))

    async def test_cil_nelze_menit_behem_uzavirani(self):
        # Během uzavírání trhem by úprava přidala podmínky do tržního příkazu
        flow = await self.zaloz_call(quantity=2)
        await self.nakup(flow, 2)
        await self.engine.close_main(flow.id)
        await self.engine._tick()
        self.assertEqual(flow.exit_trade.order.orderType, "MKT")

        with self.assertRaises(ValueError):
            await self.engine.change_profit_target(flow.id, 240.0)
        # Tržní příkaz zůstal bez podmínek
        self.assertEqual(flow.exit_trade.order.conditions, [])

    async def test_behem_uzavirani_nelze_menit_runner(self):
        flow = await self.zaloz_call(quantity=3)
        await self.nakup(flow, 3)
        await self.engine.close_main(flow.id)
        with self.assertRaises(ValueError) as ctx:
            await self.engine.set_runner(flow.id, 2.0)
        self.assertIn("uzavírání", str(ctx.exception))


class TestPrepinaniSL(ZakladTestu):
    """Tlačítka Počáteční SL a SL BE - přepínání stopu u nakoupené pozice."""

    async def nakup(self, flow, mnozstvi):
        """Simuluje vyplnění nákupu a zadání zajišťovacích příkazů."""
        self.ib.fill(flow.entry_trade, mnozstvi, 3.00)
        await self.engine._tick()
        await self.engine._tick()

    async def test_sl_be_upravi_zajistovaci_prikaz(self):
        flow = await self.zaloz_call(quantity=2)
        await self.nakup(flow, 2)
        # Cena je nad vstupem, break even není proražený
        self.ib.price_underlying = 233.0

        await self.engine.set_stop_loss(flow.id, "be")

        self.assertAlmostEqual(flow.stop_loss, 232.0)
        podminky = flow.exit_trade.order.conditions
        # PT zůstává beze změny, SL se posunul na vstup
        self.assertAlmostEqual(podminky[0].price, 235.0)
        self.assertAlmostEqual(podminky[1].price, 232.0)
        self.assertFalse(flow.main_close_requested)

    async def test_navrat_na_pocatecni_sl(self):
        flow = await self.zaloz_call(quantity=2)
        await self.nakup(flow, 2)
        self.ib.price_underlying = 233.0
        await self.engine.set_stop_loss(flow.id, "be")

        await self.engine.set_stop_loss(flow.id, "puvodni")

        self.assertAlmostEqual(flow.stop_loss, 229.0)
        self.assertAlmostEqual(flow.exit_trade.order.conditions[1].price, 229.0)

    async def test_prorazeny_sl_proda_hlavni_cast_trhem(self):
        flow = await self.zaloz_call(quantity=2)
        await self.nakup(flow, 2)
        podmineny = flow.exit_trade

        # Cena 230 je pod vstupem 232 - break even je proražený a čekat
        # na podmínku by nemělo smysl, pozice se rovnou prodává
        await self.engine.set_stop_loss(flow.id, "be")

        self.assertTrue(flow.main_close_requested)
        self.assertIn(podmineny, self.ib.cancelled)
        await self.engine._tick()
        self.assertEqual(flow.exit_trade.order.orderType, "MKT")
        self.assertEqual(int(flow.exit_trade.order.totalQuantity), 2)

    async def test_cena_presne_na_sl_take_prodava(self):
        flow = await self.zaloz_call(quantity=2)
        await self.nakup(flow, 2)

        # "Pod nebo na SL" - rovnost úrovni stačí k okamžitému prodeji
        self.ib.price_underlying = 232.0
        await self.engine.set_stop_loss(flow.id, "be")
        self.assertTrue(flow.main_close_requested)

    async def test_put_prodava_pri_cene_nad_sl(self):
        # U PUT chrání stop shora - proražení znamená cenu nad úrovní SL
        self.ib.price_underlying = 230.0
        self.ib.greek_delta = -0.35
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=228.0, profit_target=225.0, quantity=2)
        )
        await self.nakup(flow, 2)

        # Cena 230 je nad vstupem 228 - break even je proražený
        await self.engine.set_stop_loss(flow.id, "be")
        self.assertTrue(flow.main_close_requested)

    async def test_runner_ma_vlastni_sl(self):
        flow = await self.zaloz_call(quantity=3)
        await self.nakup(flow, 3)
        await self.engine.set_runner(flow.id, 2.0)
        self.ib.price_underlying = 233.0

        await self.engine.set_stop_loss(flow.id, "be")

        # Hlavní část stojí na vstupu, runner zůstává na počátečním SL
        self.assertAlmostEqual(flow.exit_trade.order.conditions[1].price, 232.0)
        self.assertAlmostEqual(flow.runner_trade.order.conditions[1].price, 229.0)

        # Runner se přepíná samostatně
        await self.engine.set_runner_stop_loss(flow.id, "be")
        self.assertAlmostEqual(flow.runner_trade.order.conditions[1].price, 232.0)

    async def test_prorazeny_sl_runneru_proda_jen_runner(self):
        flow = await self.zaloz_call(quantity=3)
        await self.nakup(flow, 3)
        await self.engine.set_runner(flow.id, 2.0)
        podmineny = flow.runner_trade
        hlavni = flow.exit_trade

        # Cena 230 je pod vstupem - break even runneru je proražený
        await self.engine.set_runner_stop_loss(flow.id, "be")

        self.assertTrue(flow.runner_close_requested)
        self.assertFalse(flow.main_close_requested)
        self.assertIn(podmineny, self.ib.cancelled)
        await self.engine._tick()
        self.assertEqual(flow.runner_trade.order.orderType, "MKT")
        self.assertEqual(flow.runner_trade.order.conditions, [])
        # Hlavní část běží dál se svým podmíněným příkazem
        self.assertIs(flow.exit_trade, hlavni)
        self.assertNotIn(hlavni, self.ib.cancelled)

    async def test_novy_runner_prebira_aktualni_sl(self):
        flow = await self.zaloz_call(quantity=3)
        await self.nakup(flow, 3)
        self.ib.price_underlying = 233.0
        await self.engine.set_stop_loss(flow.id, "be")

        await self.engine.set_runner(flow.id, 2.0)

        # Runner zapnutý po přepnutí na break even startuje také na něm
        self.assertAlmostEqual(flow.runner_sl, 232.0)
        self.assertAlmostEqual(flow.runner_trade.order.conditions[1].price, 232.0)

    async def test_sl_nelze_prepinat_pred_nakupem(self):
        # Před nákupem tlačítka nemají smysl - SL řídí zadání ve formuláři
        flow = await self.zaloz_call(quantity=2)
        with self.assertRaises(ValueError) as ctx:
            await self.engine.set_stop_loss(flow.id, "be")
        self.assertIn("nakoupené pozice", str(ctx.exception))

    async def test_sl_runneru_vyzaduje_bezici_runner(self):
        flow = await self.zaloz_call(quantity=3)
        await self.nakup(flow, 3)
        with self.assertRaises(ValueError) as ctx:
            await self.engine.set_runner_stop_loss(flow.id, "be")
        self.assertIn("běžící runner", str(ctx.exception))


class TestOtevreneMnozstvi(ZakladTestu):
    """Počet kontraktů právě otevřených v trhu (druhá část sloupce Ks)."""

    async def test_prubeh_od_zadani_po_uzavreni(self):
        # Zadané 4 kontrakty; před nákupem není v trhu nic (4/0)
        flow = await self.zaloz_call(quantity=4)
        self.assertEqual(flow.open_quantity, 0)

        # Nákup se vyplní jen ze tří čtvrtin (4/3)
        self.ib.fill(flow.entry_trade, 3, 3.00)
        await self.engine._tick()
        await self.engine._tick()
        self.assertEqual(flow.open_quantity, 3)

        # Prodaný runner otevřené množství zmenší (4/2)
        await self.engine.set_runner(flow.id, 2.0)
        self.ib.fill(flow.runner_trade, 1, 4.00)
        await self.engine._tick()
        self.assertEqual(flow.open_quantity, 2)

        # Prodej zbytku pozice vrátí otevřené množství na nulu (4/0)
        self.ib.fill(flow.exit_trade, 2, 4.00)
        await self.engine._tick()
        self.assertEqual(flow.open_quantity, 0)
        self.assertEqual(flow.state, FlowState.CLOSED)


class TestDalsihoRunneru(ZakladTestu):
    """Po prodeji runneru lze z hlavní části oddělit další."""

    async def nakup(self, flow, mnozstvi):
        """Simuluje vyplnění nákupu a zadání zajišťovacích příkazů."""
        self.ib.fill(flow.entry_trade, mnozstvi, 3.00)
        await self.engine._tick()
        await self.engine._tick()

    async def test_po_dosazeni_cile_runneru_lze_zapnout_dalsi(self):
        # 3 ks: runner 1 ks dosáhne cíle, ze zbylých 2 ks lze oddělit další
        flow = await self.zaloz_call(quantity=3)
        await self.engine.set_runner(flow.id, 2.0)
        await self.nakup(flow, 3)

        self.ib.price_underlying = 238.5
        self.ib.fill(flow.runner_trade, 1, 5.00)
        await self.engine._tick()

        self.assertFalse(flow.runner_active)
        self.assertEqual(flow.held_quantity, 2)
        self.assertAlmostEqual(flow.runner_realized_pnl, 200.0)

        # Druhý runner se oddělí ze zbývající hlavní části
        await self.engine.set_runner(flow.id, 3.0)
        self.assertTrue(flow.runner_active)
        self.assertEqual(int(flow.exit_trade.order.totalQuantity), 1)
        self.assertEqual(int(flow.runner_trade.order.totalQuantity), 1)
        self.assertAlmostEqual(flow.runner_trade.order.conditions[0].price, 241.0)

    async def test_dalsi_runner_po_uzavreni_trhem(self):
        flow = await self.zaloz_call(quantity=3)
        await self.engine.set_runner(flow.id, 2.0)
        await self.nakup(flow, 3)

        await self.engine.close_runner(flow.id)
        await self.engine._tick()
        self.ib.fill(flow.runner_trade, 1, 3.20)
        await self.engine._tick()
        self.assertFalse(flow.runner_active)

        await self.engine.set_runner(flow.id, 2.5)
        self.assertTrue(flow.runner_active)
        self.assertAlmostEqual(flow.runner_trade.order.conditions[0].price, 239.5)

    async def test_dalsi_runner_vyzaduje_zbyvajici_mnozstvi(self):
        # Po prodeji runneru zbývá 1 ks - další runner už oddělit nejde
        flow = await self.zaloz_call(quantity=2)
        await self.engine.set_runner(flow.id, 2.0)
        await self.nakup(flow, 2)
        self.ib.fill(flow.runner_trade, 1, 5.00)
        await self.engine._tick()

        with self.assertRaises(ValueError) as ctx:
            await self.engine.set_runner(flow.id, 3.0)
        self.assertIn("větším množstvím", str(ctx.exception))

    async def test_vysledek_scita_vsechny_casti(self):
        # Dva runnery prodané postupně + hlavní část na PT
        flow = await self.zaloz_call(quantity=3)
        await self.engine.set_runner(flow.id, 2.0)
        await self.nakup(flow, 3)

        self.ib.fill(flow.runner_trade, 1, 5.00)     # první runner +200
        await self.engine._tick()
        await self.engine.set_runner(flow.id, 3.0)
        self.ib.fill(flow.runner_trade, 1, 6.00)     # druhý runner +300
        await self.engine._tick()

        self.assertEqual(flow.runner_sold_quantity, 2)
        self.assertAlmostEqual(flow.runner_realized_pnl, 500.0)
        self.assertEqual(int(flow.exit_trade.order.totalQuantity), 1)

        # Hlavní část (1 ks) dosáhne PT
        self.ib.price_underlying = 236.0
        self.ib.fill(flow.exit_trade, 1, 4.00)
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.CLOSED)
        # +200 + 300 + (4,00 − 3,00) × 1 × 100
        self.assertAlmostEqual(flow.unrealized_pnl, 600.0)
        self.assertIn("runnery 2 ks", flow.message)


class TestZruseniFlow(ZakladTestu):
    """Rušení obchodů."""

    async def test_zruseni_s_uzavrenim_proda_zbyly_runner(self):
        # Hlavní část je prodaná ručně, runner běží dál - zrušení s uzavřením
        # nesmí staré vyplnění hlavní části vzít za hotové uzavření pozice
        flow = await self.zaloz_call(quantity=3)
        self.ib.fill(flow.entry_trade, 3, 3.00)
        await self.engine._tick()
        await self.engine._tick()
        await self.engine.set_runner(flow.id, 2.0)

        await self.engine.close_main(flow.id)
        await self.engine._tick()
        self.ib.fill(flow.exit_trade, 2, 3.50)
        await self.engine._tick()
        self.assertAlmostEqual(flow.exit_fill_price, 3.50)

        # Zrušení s uzavřením pozice musí prodat zbývající 1 ks runneru
        await self.engine.cancel_flow(flow.id, close_position=True)
        await self.engine._tick()

        prodej = flow.exit_trade
        self.assertIsNotNone(prodej)
        self.assertEqual(int(prodej.order.totalQuantity), 1)

        self.ib.fill(prodej, 1, 3.20)
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.CLOSED)
        self.assertEqual(flow.runner_sold_quantity, 1)
        self.assertAlmostEqual(flow.runner_realized_pnl, 20.0)
        # Cena dřívějšího prodeje hlavní části zůstává zachovaná
        self.assertAlmostEqual(flow.exit_fill_price, 3.50)
        # Celkový výsledek: hlavní 2 ks +100 USD, runner 1 ks +20 USD
        self.assertAlmostEqual(flow.unrealized_pnl, 120.0)

    async def test_zruseni_podle_tickeru_pred_nakupem(self):
        flow = await self.zaloz_call()
        zruseno = await self.engine.cancel_by_symbol("aapl")

        self.assertIs(zruseno, flow)
        self.assertEqual(flow.state, FlowState.CANCELLED)
        self.assertEqual(len(self.ib.cancelled), 1)
        self.assertIn("před nákupem", flow.message)

    async def test_zruseni_po_nakupu_upozorni_na_otevrenou_pozici(self):
        flow = await self.zaloz_call()
        self.ib.fill(flow.entry_trade, 1, 3.00)
        await self.engine._tick()
        await self.engine._tick()

        await self.engine.cancel_flow(flow.id)
        self.assertEqual(flow.state, FlowState.CANCELLED)
        self.assertIn("uzavřete ji ručně", flow.message)

    async def test_zruseni_s_uzavrenim_pozice(self):
        # Obchodník zvolil uzavření pozice - zadá se prodej trhem bez podmínek
        flow = await self.zaloz_call(quantity=2)
        self.ib.fill(flow.entry_trade, 2, 3.00)
        await self.engine._tick()
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.EXIT_ARMED)

        await self.engine.cancel_flow(flow.id, close_position=True)
        self.assertEqual(flow.state, FlowState.CLOSING)

        # Smyčka zadá prodejní příkaz trhem
        await self.engine._tick()
        prodej = self.ib.placed[-1].order
        self.assertEqual(prodej.action, "SELL")
        self.assertEqual(prodej.orderType, "MKT")
        self.assertEqual(int(prodej.totalQuantity), 2)
        self.assertEqual(prodej.conditions, [])

        # Po vyplnění je obchod uzavřen včetně výsledku
        self.ib.fill(self.ib.placed[-1], 2, 4.00)
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.CLOSED)
        self.assertEqual(flow.exit_reason, "ručně")
        self.assertAlmostEqual(flow.unrealized_pnl, 200.0)

    async def test_zruseni_bez_uzavreni_varuje(self):
        flow = await self.zaloz_call()
        self.ib.fill(flow.entry_trade, 1, 3.00)
        await self.engine._tick()
        await self.engine._tick()

        await self.engine.cancel_flow(flow.id, close_position=False)
        self.assertEqual(flow.state, FlowState.CANCELLED)
        self.assertIn("POZOR", flow.message)
        self.assertIn("bez zajištění", flow.message)
        # Žádný prodejní příkaz se nezadává
        self.assertEqual(self.ib.placed[-1].order.action, "SELL")
        self.assertEqual(len([t for t in self.ib.placed if t.order.orderType == "MKT"]), 1)

    async def test_zruseni_neexistujiciho_tickeru(self):
        with self.assertRaises(ValueError):
            await self.engine.cancel_by_symbol("TSLA")

    async def test_po_zruseni_lze_zalozit_nove_flow(self):
        await self.zaloz_call()
        await self.engine.cancel_by_symbol("AAPL")
        nove = await self.zaloz_call()
        self.assertEqual(nove.state, FlowState.ARMED)

    async def test_zruseni_nakupu_v_tws_ukonci_flow(self):
        flow = await self.zaloz_call()
        # Uživatel zrušil nákupní příkaz přímo v TWS
        flow.entry_trade.orderStatus.status = "Cancelled"
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.CANCELLED)
        self.assertIn("zrušen v TWS", flow.message)

    async def test_aktivni_flow_nelze_odstranit_z_prehledu(self):
        flow = await self.zaloz_call()
        with self.assertRaises(ValueError):
            self.engine.remove_flow(flow.id)

    async def test_ukoncene_flow_lze_odstranit(self):
        flow = await self.zaloz_call()
        await self.engine.cancel_flow(flow.id)
        self.engine.remove_flow(flow.id)
        self.assertNotIn(flow.id, self.engine.flows)


class TestVicenasobneFlow(ZakladTestu):
    """Souběžné sledování více obchodů."""

    async def test_vice_tickeru_soubezne(self):
        prvni = await self.zaloz_call()
        druhy = await self.engine.start_flow(
            FlowRequest(symbol="MSFT", entry_price=232.0, profit_target=235.0)
        )

        self.assertEqual(len(self.engine.flows), 2)
        self.assertEqual(prvni.state, FlowState.ARMED)
        self.assertEqual(druhy.state, FlowState.ARMED)

        # Vyplní se jen první obchod, druhý zůstává čekat
        self.ib.fill(prvni.entry_trade, 1, 3.00)
        await self.engine._tick()
        self.assertEqual(prvni.state, FlowState.FILLED)
        self.assertEqual(druhy.state, FlowState.ARMED)

    async def test_prehled_je_serazen_abecedne_a_ukoncene_na_konci(self):
        # Zakládá se v opačném abecedním pořadí, aby se řazení skutečně ověřilo
        await self.engine.start_flow(
            FlowRequest(symbol="MSFT", entry_price=232.0, profit_target=235.0)
        )
        aapl = await self.zaloz_call()
        poradi = [f.symbol for f in self.engine.sorted_flows()]
        self.assertEqual(poradi, ["AAPL", "MSFT"])

        # Zrušený obchod putuje na konec přehledu bez ohledu na abecedu
        await self.engine.cancel_flow(aapl.id)
        poradi = [f.symbol for f in self.engine.sorted_flows()]
        self.assertEqual(poradi, ["MSFT", "AAPL"])


class TestOcekavanehoVysledku(ZakladTestu):
    """Očekávaný zisk na PT a ztráta na SL."""

    async def test_hodnoty_se_spocitaji_pri_zadani(self):
        flow = await self.zaloz_call(quantity=2)
        await self.engine._tick()

        self.assertIsNotNone(flow.expected_profit)
        self.assertIsNotNone(flow.expected_loss)
        # Na PT se vydělá, na SL prodělá
        self.assertGreater(flow.expected_profit, 0)
        self.assertLess(flow.expected_loss, 0)

    async def test_hodnoty_rostou_s_mnozstvim(self):
        jeden = await self.zaloz_call(quantity=1)
        await self.engine._tick()
        zisk_jeden = jeden.expected_profit

        self.ib.price_underlying = 230.0
        vice = await self.engine.start_flow(
            FlowRequest(symbol="MSFT", entry_price=232.0, profit_target=235.0, quantity=3)
        )
        await self.engine._tick()

        self.assertAlmostEqual(vice.expected_profit, zisk_jeden * 3, places=4)

    async def test_prepocet_reaguje_na_zmenu_trhu(self):
        flow = await self.zaloz_call()
        await self.engine._tick()
        puvodni = flow.expected_profit

        # Opce zdraží, očekávaný zisk se změní
        self.ib.price_bid, self.ib.price_ask = 4.00, 4.10
        await self.engine._tick()

        self.assertNotAlmostEqual(flow.expected_profit, puvodni, places=2)

    async def test_po_nakupu_se_pocita_ze_skutecne_ceny(self):
        flow = await self.zaloz_call(quantity=1)
        self.ib.fill(flow.entry_trade, 1, 2.00)
        await self.engine._tick()
        await self.engine._tick()

        # Levnější nákup než trh znamená vyšší očekávaný zisk
        self.assertIsNotNone(flow.expected_profit)
        self.assertGreater(flow.expected_profit, 0)

    async def test_pomer_zisku_a_ztraty(self):
        flow = await self.zaloz_call()
        await self.engine._tick()
        self.assertIsNotNone(flow.risk_reward)
        self.assertGreater(flow.risk_reward, 0)

    async def test_ztrata_na_sl_je_vzdy_zaporna_u_call(self):
        # SL leží pod aktuální cenou podkladu, přesto musí jít o ztrátu
        self.ib.price_underlying = 309.87
        self.ib.price_bid, self.ib.price_ask = 0.66, 0.69
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=311.5, profit_target=313.5, stop_loss=309.5)
        )
        await self.engine._tick()

        self.assertLess(flow.expected_loss, 0)
        self.assertGreater(flow.expected_profit, 0)

    async def test_ztrata_na_sl_je_vzdy_zaporna_u_put(self):
        # U PUT čekajícího na pokles leží SL blíž k dnešní ceně než vstup.
        # Počítat z dnešní ceny opce by udělalo ze ztráty zisk.
        self.ib.price_underlying = 548.25
        self.ib.price_bid, self.ib.price_ask = 2.34, 2.46
        flow = await self.engine.start_flow(
            FlowRequest(symbol="META", entry_price=545.0, profit_target=543.0, stop_loss=547.0)
        )
        self.assertEqual(flow.right, "P")
        await self.engine._tick()

        self.assertLess(flow.expected_loss, 0)
        self.assertGreater(flow.expected_profit, 0)

    async def test_nakupni_cena_vychazi_ze_vstupni_urovne(self):
        # Před nákupem se opce přeceňuje na vstup, ne na dnešní cenu podkladu
        self.ib.price_underlying = 548.25
        self.ib.price_bid, self.ib.price_ask = 2.34, 2.46
        flow = await self.engine.start_flow(
            FlowRequest(symbol="META", entry_price=545.0, profit_target=543.0, stop_loss=547.0)
        )
        await self.engine._tick()

        # Nákup na 545 je pro PUT dražší než dnešních 2,40, takže zisk na PT
        # musí být nižší, než kdyby se počítal z dnešní ceny
        self.assertIsNotNone(flow.expected_profit)
        self.assertLess(flow.expected_profit, 190.0)

    async def test_bez_kotaci_zustavaji_hodnoty_prazdne(self):
        self.ib.price_bid, self.ib.price_ask = None, None
        flow = await self.zaloz_call()
        await self.engine._tick()

        self.assertIsNone(flow.expected_profit)
        self.assertIsNone(flow.expected_loss)

    async def test_prodany_runner_do_odhadu_nevstupuje(self):
        # Sloupce ukazují jen otevřený zbytek - realizovaný zisk runneru
        # očekávané hodnoty nezvyšuje
        flow = await self.zaloz_call(quantity=3)
        self.ib.fill(flow.entry_trade, 3, 3.00)
        await self.engine._tick()
        await self.engine._tick()

        # Odhad pro 3 otevřené kusy se přepočte na hodnotu za jeden kus
        na_kus = flow.expected_profit / 3

        # Runner se prodá se ziskem; otevřené zůstávají 2 kusy
        await self.engine.set_runner(flow.id, 2.0)
        self.ib.fill(flow.runner_trade, 1, 5.50)
        await self.engine._tick()
        await self.engine._tick()

        self.assertAlmostEqual(flow.runner_realized_pnl, 250.0)
        # Očekávaný zisk odpovídá dvěma otevřeným kusům bez realizovaných 250
        self.assertIsNotNone(flow.expected_profit)
        self.assertAlmostEqual(flow.expected_profit, na_kus * 2, places=4)

    async def test_uzavreny_obchod_ma_odhady_i_pl_prazdne(self):
        flow = await self.zaloz_call(quantity=1)
        self.ib.fill(flow.entry_trade, 1, 3.00)
        await self.engine._tick()
        await self.engine._tick()
        self.ib.fill(flow.exit_trade, 1, 4.00)
        await self.engine._tick()
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.CLOSED)
        # Bez otevřených kusů není co ukazovat - celkový výsledek nese hláška
        self.assertIsNone(flow.expected_profit)
        self.assertIsNone(flow.expected_loss)
        self.assertIsNone(flow.open_pnl)
        self.assertAlmostEqual(flow.unrealized_pnl, 100.0)

    async def test_pl_sloupec_pocita_jen_otevrene_kusy(self):
        # Po prodeji runneru se ziskem ukazuje open_pnl jen otevřené 2 kusy
        flow = await self.zaloz_call(quantity=3)
        self.ib.fill(flow.entry_trade, 3, 3.00)
        await self.engine._tick()
        await self.engine._tick()
        await self.engine.set_runner(flow.id, 2.0)
        self.ib.fill(flow.runner_trade, 1, 5.50)
        # Trh se posune: BID 3,20 / ASK 3,30
        self.ib.price_bid, self.ib.price_ask = 3.20, 3.30
        await self.engine._tick()

        # Otevřené kusy se oceňují BIDem: (3,20 - 3,00) * 2 ks * 100
        self.assertAlmostEqual(flow.open_pnl, 40.0)
        # Celkový výsledek obchodu realizovaný runner obsahuje (střed trhu 3,25)
        self.assertAlmostEqual(flow.unrealized_pnl, 300.0)

    async def test_odhady_pocitaji_prodej_u_bidu(self):
        # Stejný střed trhu, ale širší spread -> nižší odhadovaný zisk,
        # protože tržní prodej se vyplní u BIDu, ne na středu
        flow = await self.zaloz_call(quantity=1)
        self.ib.fill(flow.entry_trade, 1, 3.00)
        await self.engine._tick()
        await self.engine._tick()
        uzky_spread = flow.expected_profit

        self.ib.price_bid, self.ib.price_ask = 2.85, 3.25
        await self.engine._tick()

        self.assertIsNotNone(uzky_spread)
        self.assertLess(flow.expected_profit, uzky_spread)


class TestAutomatickehoUzavreni(ZakladTestu):
    """Automatické uzavření obchodů před koncem obchodování burzy."""

    def burza(self, hodina: int, minuta: int, den: int = 19) -> None:
        """Podvrhne čas burzy - srpen 2026, výchozí den je středa 19. 8."""
        self.cfg.trading.auto_close_enabled = True
        self.engine._exchange_now = lambda: datetime(
            2026, 8, den, hodina, minuta, tzinfo=ZoneInfo("America/New_York")
        )

    async def test_odpocet_sekund_do_uzavirani(self):
        # Čtvrt hodiny před oknem zbývá 900 sekund
        self.burza(15, 30)
        self.assertAlmostEqual(self.engine.auto_close_seconds(), 900.0)

        # V uzavíracím okně je odpočet nulový
        self.burza(15, 50)
        self.assertEqual(self.engine.auto_close_seconds(), 0.0)

        # Po zavření burzy už se dnes neuzavírá
        self.burza(16, 5)
        self.assertIsNone(self.engine.auto_close_seconds())

        # Sobota 22. 8. 2026 - burza neobchoduje
        self.burza(15, 50, den=22)
        self.assertIsNone(self.engine.auto_close_seconds())

        # Vypnutá funkce odpočet nenabízí
        self.cfg.trading.auto_close_enabled = False
        self.assertIsNone(self.engine.auto_close_seconds())

    async def test_obchody_se_pred_zavrenim_uzavrou(self):
        cekajici = await self.zaloz_call()
        drzeny = await self.zaloz_put()
        self.ib.fill(drzeny.entry_trade, 1, 3.10)
        await self.engine._tick()
        await self.engine._tick()

        self.burza(15, 50)
        await self.engine._tick()

        # Čekající obchod je zrušen a jeho příkaz odstraněn z trhu
        self.assertEqual(cekajici.state, FlowState.CANCELLED)
        self.assertIn("Automaticky", cekajici.message)
        self.assertIn(cekajici.entry_trade, self.ib.cancelled)

        # Držený obchod se prodává trhem
        self.assertEqual(drzeny.state, FlowState.CLOSING)
        await self.engine._tick()
        prodej = drzeny.exit_trade
        self.assertEqual(prodej.order.orderType, "MKT")
        self.assertEqual(int(prodej.order.totalQuantity), 1)

        self.ib.fill(prodej, 1, 3.40)
        await self.engine._tick()
        self.assertEqual(drzeny.state, FlowState.CLOSED)

    async def test_mimo_okno_se_obchody_nechavaji(self):
        flow = await self.zaloz_call()

        # Pět minut před začátkem okna se ještě nic neděje
        self.burza(15, 40)
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(self.ib.cancelled, [])


class TestIndikatoruHlidani(ZakladTestu):
    """Příznak, že aplikace obchody skutečně hlídá."""

    async def test_bez_spustene_smycky_nehlida(self):
        # Smyčka neběží, i když je spojení navázané
        self.assertFalse(self.engine.is_monitoring)

    async def test_po_spusteni_smycky_hlida(self):
        self.engine.start()
        try:
            # Počká se na dokončení prvního průchodu
            for _ in range(20):
                await asyncio.sleep(0.05)
                if self.engine.is_monitoring:
                    break
            self.assertTrue(self.engine.is_monitoring)
        finally:
            await self.engine.stop()

    async def test_zastavena_smycka_nehlida(self):
        self.engine.start()
        for _ in range(20):
            await asyncio.sleep(0.05)
            if self.engine.is_monitoring:
                break
        await self.engine.stop()
        self.assertFalse(self.engine.is_monitoring)

    async def test_bez_spojeni_nehlida(self):
        self.engine.start()
        try:
            for _ in range(20):
                await asyncio.sleep(0.05)
                if self.engine.is_monitoring:
                    break
            self.ib.connected_flag = False
            self.assertFalse(self.engine.is_monitoring)
        finally:
            await self.engine.stop()

    async def test_zaseknuta_smycka_nehlida(self):
        # Smyčka běží, ale poslední průchod je dávno - hlídání fakticky nefunguje
        self.engine.start()
        try:
            for _ in range(20):
                await asyncio.sleep(0.05)
                if self.engine.is_monitoring:
                    break
            self.engine._last_tick -= 3600
            self.assertFalse(self.engine.is_monitoring)
        finally:
            await self.engine.stop()


class TestVelikostUctu(ZakladTestu):
    """Zdroj velikosti účtu pro výpočet rizika."""

    async def test_kladna_hodnota_z_konfigurace(self):
        self.assertAlmostEqual(self.engine.account_size, 5000.0)
        self.assertAlmostEqual(self.engine.risk_amount, 50.0)

    async def test_nula_prebira_velikost_z_tws(self):
        # account.size = 0 znamená převzetí hodnoty z platformy
        self.cfg.account.size = 0
        await self.engine._tick()
        self.assertAlmostEqual(self.engine.account_size, 12345.0)
        self.assertAlmostEqual(self.engine.risk_amount, 123.45)

    async def test_kladna_hodnota_ma_prednost_pred_tws(self):
        # Při vyplněné velikosti se z TWS nic nepřebírá
        await self.engine._tick()
        self.assertAlmostEqual(self.engine.account_size, 5000.0)

    async def test_bez_hodnoty_z_tws_je_velikost_nulova(self):
        # Dokud TWS hodnotu nepošle, není z čeho počítat riziko
        self.cfg.account.size = 0
        self.assertAlmostEqual(self.engine.account_size, 0.0)
        self.assertAlmostEqual(self.engine.risk_amount, 0.0)

    async def test_velikost_se_obnovuje(self):
        self.cfg.account.size = 0
        await self.engine._tick()
        self.assertAlmostEqual(self.engine.account_size, 12345.0)

        # Stav účtu se změní; po uplynutí intervalu se převezme nová hodnota
        self.ib.net_liquidation_value = 20000.0
        self.engine._account_checked = 0.0
        await self.engine._tick()
        self.assertAlmostEqual(self.engine.account_size, 20000.0)

    async def test_mnozstvi_se_pocita_z_velikosti_prevzate_z_tws(self):
        # Riziko 123,45 USD, pohyb 3 USD, delta 0,35 -> 123,45 / 105 = 1 kontrakt
        self.cfg.account.size = 0
        await self.engine._tick()
        flow = await self.zaloz_call()
        self.assertEqual(flow.quantity, 1)

        # Při větším účtu vyjde kontraktů více
        self.ib.net_liquidation_value = 500000.0
        self.engine._account_checked = 0.0
        await self.engine._tick()
        druhy = await self.engine.start_flow(
            FlowRequest(symbol="MSFT", entry_price=232.0, profit_target=235.0)
        )
        self.assertEqual(druhy.quantity, 47)


if __name__ == "__main__":
    unittest.main(verbosity=2)
