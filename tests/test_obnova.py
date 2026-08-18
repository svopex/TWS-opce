"""
Testy obnovy obchodů po restartu aplikace.

Ověřuje se, že se stav odvodí od skutečnosti v TWS (příkazy a pozice),
nikoliv od zápisu v uloženém souboru.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fake_ib import OPTION_CONID, FakeIBService
from tws_opce import store
from tws_opce.config import AppConfig
from tws_opce.engine import FlowEngine
from tws_opce.ib_service import PositionInfo, order_ref
from tws_opce.models import FlowRequest, FlowState


class ZakladObnovy(unittest.IsolatedAsyncioTestCase):
    """Připraví engine s dočasným souborem stavu."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = AppConfig()
        self.cfg.state.file = str(Path(self.tmp.name) / "state.json")
        self.ib = FakeIBService(self.cfg)
        self.engine = FlowEngine(self.cfg, self.ib)

    async def asyncSetUp(self) -> None:
        # Aplikace po připojení k TWS vždy nejprve obnoví stav
        await self.engine.restore()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def zaloz_a_restartuj(self, priprav=None) -> FlowEngine:
        """
        Založí obchod, volitelně nad ním provede úpravy simulující běh,
        a vrátí nový engine, který stav obnovil ze souboru.
        """
        self.ib.price_underlying = 230.0
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0, quantity=2)
        )
        if priprav:
            await priprav(flow)

        # Nový engine sdílí náhradu TWS, takže vidí stejné příkazy i pozice
        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()
        return novy


class TestUlozeniStavu(ZakladObnovy):
    """Zápis stavu na disk."""

    async def test_zalozeny_obchod_se_ulozi(self):
        await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        ulozene = store.load(self.cfg.state.file)
        self.assertEqual(len(ulozene), 1)
        self.assertEqual(ulozene[0].symbol, "AAPL")
        self.assertEqual(ulozene[0].state, FlowState.ARMED)

    async def test_vypnute_ukladani_nic_nezapisuje(self):
        self.cfg.state.enabled = False
        await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        self.assertFalse(Path(self.cfg.state.file).exists())

    async def test_prikazy_nesou_znacku_aplikace(self):
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        self.assertEqual(self.ib.placed[0].order.orderRef, order_ref(flow.id, "entry"))


class TestOchranaUlozenehoStavu(ZakladObnovy):
    """Uložený stav se nesmí ztratit dříve, než je načten."""

    async def test_udalost_pred_obnovou_stav_neprepise(self):
        # Obchod z předchozího běhu
        await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        self.assertEqual(len(store.load(self.cfg.state.file)), 1)

        # Nový běh aplikace zaloguje událost ještě před obnovou stavu
        novy = FlowEngine(self.cfg, self.ib)
        novy.log_event("Aplikace spuštěna, spojení s TWS navázáno.")

        # Uložený obchod musí zůstat na disku, jinak by se po restartu ztratil
        self.assertEqual(len(store.load(self.cfg.state.file)), 1)

        await novy.restore()
        self.assertEqual(len(novy.flows), 1)

    async def test_zmena_pred_obnovou_se_neuklada(self):
        await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        novy = FlowEngine(self.cfg, self.ib)
        # Průchod smyčkou před obnovou nesmí uložený stav přepsat
        await novy._tick()
        self.assertEqual(len(store.load(self.cfg.state.file)), 1)


class TestZnovupripojeni(ZakladObnovy):
    """Obnova při odpojení a opětovném připojení za běhu aplikace."""

    async def test_smazany_prikaz_se_po_pripojeni_zada_znovu(self):
        # Obchod běží, uživatel se odpojí a příkaz v TWS ručně smaže
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        self.assertEqual(flow.state, FlowState.ARMED)

        self.engine._synced = False          # simulace odpojení
        self.ib.cancel(flow.entry_trade)
        self.ib.placed.clear()

        # Po opětovném připojení se stav srovná se skutečností v TWS
        await self.engine.restore()
        self.assertEqual(flow.state, FlowState.NO_QUOTES)
        self.assertIn("bude zadán znovu", flow.message)

        # A smyčka příkaz vrátí do trhu
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(self.ib.placed[-1].order.action, "BUY")

    async def test_ztrata_spojeni_shodi_priznak_sparovani(self):
        # Bez automatického připojování zůstane příznak shozený až do ruční obnovy
        self.cfg.connection.auto_reconnect = False
        await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        self.assertTrue(self.engine._synced)

        self.ib.connected_flag = False
        await self.engine._tick()
        self.assertFalse(self.engine._synced)

        # Ruční připojení tlačítkem v rozhraní pak obchody znovu spáruje
        self.ib.connected_flag = True
        await self.engine.restore()
        self.assertTrue(self.engine._synced)

    async def test_automaticke_pripojeni_obchody_sparuje(self):
        # Smyčka po obnovení spojení sama zajistí nové spárování
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        self.ib.connected_flag = False
        await self.engine._tick()

        self.assertTrue(self.engine._synced)
        self.assertEqual(flow.state, FlowState.ARMED)
        udalosti = " ".join(zprava for _, zprava in self.engine.events)
        self.assertIn("ověřuji stav obchodů v TWS", udalosti)

    async def test_opakovane_pripojeni_neduplikuje_obchody(self):
        await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        pocet = len(self.engine.flows)

        for _ in range(3):
            self.engine._synced = False
            await self.engine.restore()

        self.assertEqual(len(self.engine.flows), pocet)

    async def test_uzavirani_pozice_pokracuje_po_pripojeni(self):
        # Probíhající uzavírání se nesmí přepsat stavem odvozeným z TWS
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        self.ib.fill(flow.entry_trade, 1, 3.00)
        await self.engine._tick()
        await self.engine._tick()
        self.ib.held_positions[OPTION_CONID] = 1

        await self.engine.cancel_flow(flow.id, close_position=True)
        self.assertEqual(flow.state, FlowState.CLOSING)

        self.engine._synced = False
        await self.engine.restore()
        self.assertEqual(flow.state, FlowState.CLOSING)


class TestSoubehObnovy(ZakladObnovy):
    """Obnova a monitorovací smyčka si nesmí lézt do cesty."""

    async def test_behem_obnovy_se_nemonitoruje(self):
        # Smyčka pracující s příkazy z minulého spojení by stav rozhodila
        await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        pocet_pred = len(self.ib.placed)

        await self.engine._restore_lock.acquire()
        try:
            await self.engine._tick()
        finally:
            self.engine._restore_lock.release()

        # Průchod se přeskočil, nic se nezadalo ani nezrušilo
        self.assertEqual(len(self.ib.placed), pocet_pred)
        self.assertEqual(self.ib.cancelled, [])

    async def test_selhani_obnovy_je_videt_v_prubehu(self):
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )

        # Ověření kontraktu v TWS selže
        async def selze(*args, **kwargs):
            raise RuntimeError("kontrakt se nepodařilo ověřit")

        self.ib.qualify_option = selze
        self.engine._synced = False
        await self.engine.restore()

        self.assertEqual(flow.state, FlowState.ERROR)
        udalosti = " ".join(zprava for _, zprava in self.engine.events)
        self.assertIn("obnova selhala", udalosti)
        self.assertIn(flow.id, udalosti)

    async def test_soubezne_volani_obnovy_probehne_jednou(self):
        await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        self.engine._synced = False

        # Dvě souběžná volání (tlačítko v rozhraní a smyčka) nesmí obnovu zdvojit
        await asyncio.gather(self.engine.restore(), self.engine.restore())

        obnoveni = sum(1 for _, z in self.engine.events if "obnoveno" in z)
        self.assertEqual(obnoveni, 1)


class TestPrevzetiPrikazu(ZakladObnovy):
    """Dohledání příkazů podle značky, když uložený stav chybí."""

    async def test_prikaz_bez_ulozeneho_stavu_se_prevezme(self):
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0, quantity=2)
        )
        # Soubor se stavem se ztratil, příkaz v TWS zůstal
        Path(self.cfg.state.file).unlink()

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()

        self.assertEqual(len(novy.flows), 1)
        prevzaty = list(novy.flows.values())[0]
        self.assertEqual(prevzaty.id, flow.id)
        self.assertEqual(prevzaty.symbol, "AAPL")
        self.assertEqual(prevzaty.quantity, 2)
        # Vstupní cena se odvodí z cenové podmínky příkazu
        self.assertAlmostEqual(prevzaty.entry_price, 232.0)
        self.assertEqual(prevzaty.state, FlowState.ARMED)
        self.assertIn("dopočítané", prevzaty.message)

    async def test_prevzeti_pouzije_pt_a_sl_z_prodejniho_prikazu(self):
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0, quantity=1)
        )
        # Obchod se nakoupil a má zadané zajištění
        self.ib.fill(flow.entry_trade, 1, 3.10)
        await self.engine._tick()
        await self.engine._tick()
        self.ib.held_positions[OPTION_CONID] = 1
        Path(self.cfg.state.file).unlink()

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()

        prevzaty = list(novy.flows.values())[0]
        # PT a SL se přečtou z podmínek prodejního příkazu, nedopočítávají se
        self.assertAlmostEqual(prevzaty.profit_target, 235.0)
        self.assertAlmostEqual(prevzaty.stop_loss, 229.0)
        self.assertNotIn("dopočítané", prevzaty.message)
        self.assertEqual(prevzaty.state, FlowState.EXIT_ARMED)

    async def test_pozice_bez_zajisteni_se_prevezme(self):
        # Nákup se vyplnil, aplikace spadla před zadáním zajištění a stav se ztratil
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0, quantity=2)
        )
        self.ib.fill(flow.entry_trade, 2, 3.10)
        self.ib.held_positions[OPTION_CONID] = 2
        Path(self.cfg.state.file).unlink()

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()

        self.assertEqual(len(novy.flows), 1)
        prevzaty = list(novy.flows.values())[0]
        self.assertEqual(prevzaty.state, FlowState.FILLED)
        self.assertEqual(prevzaty.filled_quantity, 2)

        # Zajištění se doplní hned v dalším průchodu smyčkou
        await novy._tick()
        self.assertEqual(prevzaty.state, FlowState.EXIT_ARMED)
        self.assertEqual(self.ib.placed[-1].order.action, "SELL")
        self.assertEqual(int(self.ib.placed[-1].order.totalQuantity), 2)

    async def test_pozice_bez_obchodu_vyvola_upozorneni(self):
        # Na účtu je opční pozice, ke které aplikace nemá obchod ani příkaz
        self.ib.held_positions[999999] = 3

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()

        udalosti = " ".join(zprava for _, zprava in novy.events)
        self.assertIn("POZOR", udalosti)
        self.assertIn("999999", udalosti)
        # Aplikace k cizí pozici sama nic nezadává
        self.assertEqual(novy.flows, {})

    async def test_rizena_pozice_upozorneni_nevyvola(self):
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        self.ib.fill(flow.entry_trade, 1, 3.10)
        await self.engine._tick()
        await self.engine._tick()
        self.ib.held_positions[OPTION_CONID] = 1

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()

        udalosti = " ".join(zprava for _, zprava in novy.events)
        self.assertNotIn("POZOR", udalosti)

    async def test_pozice_bez_dozoru_se_hlida_prubezne(self):
        # Kontrola běží i za chodu, ne jen při startu
        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()
        self.assertEqual(novy.unmanaged, {})

        # Na účtu se objeví cizí pozice
        self.ib.held_positions[555555] = 2
        await novy._tick()

        self.assertIn(555555, novy.unmanaged)
        udalosti = " ".join(zprava for _, zprava in novy.events)
        self.assertIn("POZOR", udalosti)

    async def test_upozorneni_zmizi_po_uzavreni_pozice(self):
        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()
        self.ib.held_positions[555555] = 2
        await novy._tick()
        self.assertIn(555555, novy.unmanaged)

        # Pozice byla uzavřena, upozornění se přestane hlásit
        self.ib.held_positions.clear()
        novy._unmanaged_checked = 0.0
        await novy._tick()
        self.assertEqual(novy.unmanaged, {})

    async def test_rizena_pozice_se_nehlasi(self):
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        self.ib.fill(flow.entry_trade, 1, 3.10)
        await self.engine._tick()
        await self.engine._tick()
        self.ib.held_positions[OPTION_CONID] = 1

        self.engine._unmanaged_checked = 0.0
        await self.engine._tick()
        self.assertEqual(self.engine.unmanaged, {})

    async def test_upozorneni_vysvetli_jiny_kontrakt_stejneho_tickeru(self):
        # Na tickeru běží obchod, ale pozice je na jiném strike - text to musí uvést
        await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        # Pozice na jiném kontraktu téhož tickeru (jiný conId, stejný symbol)
        self.ib.held_positions[OPTION_CONID + 1] = 1

        self.engine._unmanaged_checked = 0.0
        await self.engine._tick()

        udalosti = " ".join(zprava for _, zprava in self.engine.events)
        self.assertIn("POZOR", udalosti)
        self.assertIn("bez zajištění", udalosti)

    async def test_text_upozorneni_uvede_dotceny_obchod(self):
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        info = PositionInfo(conid=123, quantity=1, label="AAPL 260819C00240000", symbol="AAPL")

        text = self.engine.unmanaged_text(info)
        self.assertIn("bez zajištění", text)
        self.assertIn(flow.id, text)
        self.assertIn("jiného kontraktu", text)

    async def test_text_upozorneni_bez_souvisejiciho_obchodu(self):
        info = PositionInfo(conid=123, quantity=2, label="TSLA 260819P00200000", symbol="TSLA")
        text = self.engine.unmanaged_text(info)
        self.assertIn("bez zajištění", text)
        self.assertNotIn("jiného kontraktu", text)

    async def test_cizi_prikazy_se_neprebiraji(self):
        # Příkaz bez značky aplikace (zadaný ručně v TWS) se ignoruje
        await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        self.ib.placed[0].order.orderRef = "rucni prikaz obchodnika"
        Path(self.cfg.state.file).unlink()

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()
        self.assertEqual(novy.flows, {})

    async def test_zruseny_prikaz_se_neprebira(self):
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        self.ib.cancel(flow.entry_trade)
        Path(self.cfg.state.file).unlink()

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()
        self.assertEqual(novy.flows, {})


class TestObnovaStavu(ZakladObnovy):
    """Odvození stavu obchodu ze skutečnosti v TWS."""

    async def test_cekajici_nakupni_prikaz(self):
        novy = await self.zaloz_a_restartuj()

        obnovene = list(novy.flows.values())[0]
        self.assertEqual(obnovene.state, FlowState.ARMED)
        self.assertIn("čeká v trhu", obnovene.message)
        self.assertIsNotNone(obnovene.entry_trade)

    async def test_drzena_pozice_se_zajistenim(self):
        async def priprav(flow):
            # Nákup se vyplnil a prodejní příkaz byl zadán
            self.ib.fill(flow.entry_trade, 2, 3.10)
            await self.engine._tick()
            await self.engine._tick()
            self.ib.held_positions[OPTION_CONID] = 2

        novy = await self.zaloz_a_restartuj(priprav)

        obnovene = list(novy.flows.values())[0]
        self.assertEqual(obnovene.state, FlowState.EXIT_ARMED)
        self.assertEqual(obnovene.filled_quantity, 2)
        self.assertIsNotNone(obnovene.exit_trade)

    async def test_pozice_bez_zajisteni_se_doplni(self):
        async def priprav(flow):
            # Nákup se vyplnil, ale prodejní příkaz zadán nebyl (pád aplikace)
            self.ib.fill(flow.entry_trade, 2, 3.10)
            self.ib.held_positions[OPTION_CONID] = 2

        novy = await self.zaloz_a_restartuj(priprav)
        obnovene = list(novy.flows.values())[0]

        self.assertEqual(obnovene.state, FlowState.FILLED)
        self.assertIn("zajištění se doplní", obnovene.message)

        # Monitorovací smyčka zajištění skutečně doplní
        pocet_pred = len(self.ib.placed)
        await novy._tick()
        self.assertEqual(obnovene.state, FlowState.EXIT_ARMED)
        self.assertEqual(len(self.ib.placed), pocet_pred + 1)
        self.assertEqual(self.ib.placed[-1].order.action, "SELL")

    async def test_pozice_uzavrena_behem_vypadku(self):
        async def priprav(flow):
            self.ib.fill(flow.entry_trade, 2, 3.00)
            await self.engine._tick()
            await self.engine._tick()
            # Prodej proběhl, pozice na účtu už není
            self.ib.fill(flow.exit_trade, 2, 4.00)

        novy = await self.zaloz_a_restartuj(priprav)
        obnovene = list(novy.flows.values())[0]

        self.assertEqual(obnovene.state, FlowState.CLOSED)
        self.assertIn("uzavřena během výpadku", obnovene.message)
        self.assertAlmostEqual(obnovene.exit_fill_price, 4.00)

    async def test_zmizely_nakupni_prikaz_se_zada_znovu(self):
        async def priprav(flow):
            # Příkaz byl mezitím z TWS pryč (například zrušen ručně)
            self.ib.cancel(flow.entry_trade)
            self.ib.placed.clear()

        novy = await self.zaloz_a_restartuj(priprav)
        obnovene = list(novy.flows.values())[0]

        self.assertEqual(obnovene.state, FlowState.NO_QUOTES)
        self.assertIn("bude zadán znovu", obnovene.message)

        # Smyčka příkaz vrátí do trhu
        await novy._tick()
        self.assertEqual(obnovene.state, FlowState.ARMED)
        self.assertEqual(self.ib.placed[-1].order.action, "BUY")

    async def test_nakoupeno_ale_bez_pozice_i_prikazu(self):
        async def priprav(flow):
            self.ib.fill(flow.entry_trade, 2, 3.10)
            await self.engine._tick()
            await self.engine._tick()
            # Pozice ani příkazy v TWS nejsou a prodej nebyl vyplněn
            self.ib.placed.clear()

        novy = await self.zaloz_a_restartuj(priprav)
        obnovene = list(novy.flows.values())[0]

        self.assertEqual(obnovene.state, FlowState.ERROR)
        self.assertIn("Zkontrolujte účet ručně", obnovene.message)

    async def test_ukoncene_obchody_zustavaji_v_prehledu(self):
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        await self.engine.cancel_flow(flow.id)

        novy = FlowEngine(self.cfg, self.ib)
        await novy.restore()

        obnovene = list(novy.flows.values())[0]
        self.assertEqual(obnovene.state, FlowState.CANCELLED)

    async def test_cislovani_pokracuje_po_obnove(self):
        novy = await self.zaloz_a_restartuj()
        puvodni_id = list(novy.flows.keys())[0]

        dalsi = await novy.start_flow(
            FlowRequest(symbol="MSFT", entry_price=232.0, profit_target=235.0)
        )
        self.assertNotEqual(dalsi.id, puvodni_id)
        self.assertEqual(dalsi.id, "MSFT-2")

    async def test_obnova_probehne_jen_jednou(self):
        novy = await self.zaloz_a_restartuj()
        pocet = len(novy.flows)
        await novy.restore()
        self.assertEqual(len(novy.flows), pocet)

    async def test_poskozeny_soubor_neshodi_aplikaci(self):
        Path(self.cfg.state.file).write_text("{tohle není platný JSON", encoding="utf-8")
        novy = FlowEngine(self.cfg, self.ib)
        # Chyba čtení se pouze zaloguje, proto se výpis pro přehlednost potlačuje
        logging.disable(logging.CRITICAL)
        try:
            await novy.restore()
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(novy.flows, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
