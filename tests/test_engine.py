"""Testy stavového automatu obchodního flow proti náhradě TWS."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

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

    async def test_druhe_flow_na_stejny_ticker_se_odmitne(self):
        await self.zaloz_call()
        with self.assertRaises(ValueError) as ctx:
            await self.zaloz_call()
        self.assertIn("již běží aktivní flow", str(ctx.exception))

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


class TestZruseniFlow(ZakladTestu):
    """Rušení obchodů."""

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

    async def test_prehled_je_serazen_od_nejnovejsiho(self):
        await self.zaloz_call()
        await self.engine.start_flow(
            FlowRequest(symbol="MSFT", entry_price=232.0, profit_target=235.0)
        )
        poradi = [f.symbol for f in self.engine.sorted_flows()]
        self.assertEqual(poradi[0], "MSFT")


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
