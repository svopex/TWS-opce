"""
Testy souběhů mezi příkazy obchodníka a monitorovací smyčkou.

Příkazy enginu (start_flow, change_profit_target, ...) čekají na odpovědi
z TWS a monitorovací smyčka mezitím běží dál - stav obchodu se tedy může
uprostřed jejich vykonávání změnit. Testy tady simulují právě takový zásah:
náhrada TWS během čekání vyplní nákup a protočí smyčku, stejně jako by to
udělal skutečný trh.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.zaklad import ZakladEnginu
from tws_opce.models import FlowRequest, FlowState


class ZakladZavodu(ZakladEnginu):
    """Engine s náhradou TWS a vzorovým zadáním pro testy souběhů."""

    async def zaloz_call(self, **zmeny):
        """Založí vzorové CALL flow: podklad 230, vstup 232, PT 235."""
        self.ib.price_underlying = 230.0
        pozadavek = FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        for klic, hodnota in zmeny.items():
            setattr(pozadavek, klic, hodnota)
        return await self.engine.start_flow(pozadavek)

    async def nakup(self, flow, mnozstvi: int) -> None:
        """Vyplní nákup a nechá smyčku zadat zajišťovací příkazy."""
        self.ib.fill(flow.entry_trade, mnozstvi, 3.00)
        await self.engine._tick()
        await self.engine._tick()

    def trzni_prodeje(self) -> list:
        """
        Prodeje trhem zadané do náhrady TWS.
        Zajišťovací příkaz je také tržní, ale nese cenové podmínky pro PT a SL -
        bez podmínek prodává okamžitě, a právě ten se hlídá.
        """
        return [
            trade
            for trade in self.ib.placed
            if trade.order.action == "SELL" and not trade.order.conditions
        ]

    def nakupy(self) -> list:
        """Nákupní příkazy zadané do náhrady TWS."""
        return [trade for trade in self.ib.placed if trade.order.action == "BUY"]


class TestZavoduPriZalozeni(ZakladZavodu):
    """Nahrazení čekajícího obchodu novým zadáním."""

    async def test_obchod_vyplneny_behem_pripravy_se_nezrusi(self):
        # Na tickeru běží čekající CALL obchod, který se nahradí novým zadáním
        prvni = await self.zaloz_call()
        self.assertEqual(prvni.state, FlowState.ARMED)

        puvodni_qualify = self.ib.qualify_stock

        async def qualify_a_vyplnit(symbol: str):
            """Uprostřed přípravy nového zadání se původní nákup vyplní."""
            if prvni.state.is_before_entry:
                self.ib.fill(prvni.entry_trade, prvni.quantity, 3.00)
                await self.engine._tick()
                await self.engine._tick()
            return await puvodni_qualify(symbol)

        self.ib.qualify_stock = qualify_a_vyplnit

        # Nové zadání musí skončit chybou - obchod už drží pozici
        with self.assertRaises(ValueError):
            await self.zaloz_call()

        # Obchod zůstal v přehledu i se zajišťovacím příkazem v trhu
        self.assertIn(prvni.id, self.engine.flows)
        self.assertEqual(prvni.state, FlowState.EXIT_ARMED)
        self.assertIsNotNone(prvni.exit_trade)
        self.assertNotIn(prvni.exit_trade, self.ib.cancelled)


class TestZavoduPriZmeneCile(ZakladZavodu):
    """Změna cíle před nákupem s přepočtem strike."""

    def setUp(self) -> None:
        super().setUp()
        # Jen v této kombinaci vybírá změna cíle nový kontrakt
        self.cfg.trading.pt_change_strike = "recalculate"
        self.cfg.strike.mode = "target"

    async def test_vyplneni_behem_prepoctu_strike_nezada_druhy_nakup(self):
        flow = await self.zaloz_call()
        puvodni_chain = self.ib.option_chain

        async def chain_a_vyplnit(underlying):
            """Uprostřed přepočtu strike se nákup vyplní a smyčka zadá výstup."""
            if flow.state.is_before_entry:
                self.ib.fill(flow.entry_trade, flow.quantity, 3.00)
                await self.engine._tick()
                await self.engine._tick()
            return await puvodni_chain(underlying)

        self.ib.option_chain = chain_a_vyplnit

        await self.engine.change_profit_target(flow.id, 240.0)

        # Opce je koupená - strike se měnit nesmí a druhý nákup nesmí vzniknout
        self.assertEqual(len(self.nakupy()), 1)
        self.assertEqual(flow.state, FlowState.EXIT_ARMED)
        self.assertEqual(flow.profit_target, 240.0)


class TestUzavreniPriPendingCancel(ZakladZavodu):
    """Tržní prodej se nesmí sečíst s příkazem, jehož zrušení TWS nepotvrdila."""

    async def test_uzavreni_ceka_na_potvrzeni_zruseni(self):
        # Pozice rozdělená na hlavní část a runner má dva prodejní příkazy;
        # TWS je nemusí zrušit současně
        flow = await self.zaloz_call(quantity=3)
        await self.engine.set_runner(flow.id, 2.0)
        await self.nakup(flow, 3)
        self.assertEqual(flow.state, FlowState.EXIT_ARMED)
        self.assertIsNotNone(flow.runner_trade)

        await self.engine.cancel_flow(flow.id, close_position=True)
        self.assertEqual(flow.state, FlowState.CLOSING)

        # Hlavní příkaz je zrušený, u runneru TWS zrušení teprve přijala -
        # takový příkaz je stále živý a může se vyplnit
        flow.exit_trade.orderStatus.status = "Cancelled"
        flow.runner_trade.orderStatus.status = "PendingCancel"
        await self.engine._tick()
        self.assertEqual(self.trzni_prodeje(), [])

        # Teprve po potvrzení zrušení obou příkazů se zadá prodej trhem
        flow.runner_trade.orderStatus.status = "Cancelled"
        await self.engine._tick()
        trzni = self.trzni_prodeje()
        self.assertEqual(len(trzni), 1)
        self.assertEqual(trzni[0].order.totalQuantity, 3)


class TestUzavreniRunneruBezPrikazu(ZakladZavodu):
    """Runner odložený při částečném nákupu nedrží žádné kusy."""

    async def pripravit_odlozeny_runner(self):
        """
        Obchod na 3 ks s runnerem 1 ks, ze kterého se nakoupí jen 1 ks.
        Hlavní příkaz pak kryje celou pozici a runner vlastní příkaz nemá.
        """
        flow = await self.zaloz_call(quantity=3)
        await self.engine.set_runner(flow.id, 2.0)
        self.ib.fill(flow.entry_trade, 1, 3.00)
        await self.engine._tick()
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.EXIT_ARMED)
        self.assertTrue(flow.runner_active)
        self.assertIsNone(flow.runner_order_id)
        return flow

    async def test_uzavreni_runneru_bez_prikazu_se_odmitne(self):
        flow = await self.pripravit_odlozeny_runner()
        with self.assertRaises(ValueError):
            await self.engine.close_runner(flow.id)

    async def test_smycka_nezada_trzni_prodej_runneru_bez_prikazu(self):
        # Pojistka pro případ, že by se příznak nastavil jinudy než přes API
        flow = await self.pripravit_odlozeny_runner()
        flow.runner_close_requested = True

        await self.engine._tick()

        # Hlavní příkaz kryje celý 1 nakoupený kus - tržní prodej navíc by
        # vytvořil nekrytou krátkou pozici
        self.assertEqual(self.trzni_prodeje(), [])
        self.assertFalse(flow.runner_close_requested)


class TestSmeruBezCenyPodkladu(ZakladZavodu):
    """Typ opce, když z TWS nedorazila cena podkladu."""

    async def test_short_zadani_se_pripravi_jako_put(self):
        self.ib.price_underlying = None
        preview = await self.engine.prepare("AAPL", 229.0, 226.0)
        self.assertEqual(preview.right, "P")

    async def test_long_zadani_se_pripravi_jako_call(self):
        self.ib.price_underlying = None
        preview = await self.engine.prepare("AAPL", 232.0, 235.0)
        self.assertEqual(preview.right, "C")

    async def test_smer_ze_stop_lossu_kdyz_chybi_cil(self):
        self.ib.price_underlying = None
        preview = await self.engine.prepare("AAPL", 229.0, None, 232.0)
        self.assertEqual(preview.right, "P")

    async def test_obe_urovne_na_opci_bez_ceny_podkladu_selzou(self):
        # Z čeho směr určit, není - tichý odhad by koupil opačnou opci
        self.ib.price_underlying = None
        with self.assertRaises(ValueError):
            await self.engine.prepare(
                "AAPL", 232.0, 150.0, 100.0, pt_on_underlying=False, sl_on_underlying=False
            )


class TestObnovyPoVypadku(ZakladZavodu):
    """Ztráta spojení musí vynutit nové spárování obchodů s příkazy v TWS."""

    async def test_odpojeni_zrusi_priznak_sparovani(self):
        self.engine._synced = True
        self.ib._on_disconnected()
        self.assertFalse(self.engine._synced)


class TestZapisuStavu(ZakladZavodu):
    """Stav se má ukládat jen při skutečné změně, ne každý průchod smyčky."""

    async def test_nezmenene_kotace_nehlasi_zmenu(self):
        flow = await self.zaloz_call()
        # První průchod hodnoty z náhrady TWS teprve načte
        self.engine._refresh_market_data(flow)
        # Druhý průchod nad nezměněnými kotacemi už změnu hlásit nesmí
        self.assertFalse(self.engine._refresh_market_data(flow))

    async def test_zmena_kotace_se_ohlasi(self):
        flow = await self.zaloz_call()
        self.engine._refresh_market_data(flow)
        self.ib.price_bid = 3.50
        self.ib.price_ask = 3.60
        self.assertTrue(self.engine._refresh_market_data(flow))


if __name__ == "__main__":
    unittest.main()
