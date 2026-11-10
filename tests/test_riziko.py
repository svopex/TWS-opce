"""
Testy dodržení rizika na obchod: posun na vzdálenější (levnější) strike,
když se do rizika nevejde ani nejmenší množství, a zrušení obchodu, když
nepomůže ani to (trading.risk_overflow).
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fake_ib import UNDERLYING_CONID
from tests.zaklad import ZakladEnginu, vychozi_config
from tws_opce import calc
from tws_opce.config import validate_config
from tws_opce.models import FlowRequest, FlowState

# Kotace CALL opcí podle strike při podkladu 230 a vstupu 232. Riziko je
# 50 USD (účet 5000, 1 %), s tolerancí 10 % tedy 55 USD. Při SL 20 % prémie
# se 1 ks na strike 232,5 (prémie ≈ 400 USD) nevejde, na 235 těsně ne
# (≈ 56 USD) a na 237,5 už ano (≈ 37 USD)
KOTACE = {
    232.5: (3.00, 3.10),
    235.0: (2.00, 2.08),
    237.5: (1.25, 1.30),
    240.0: (0.70, 0.73),
    242.5: (0.35, 0.37),
    245.0: (0.15, 0.16),
}


class ZakladRizika(ZakladEnginu):
    """Engine s kotacemi podle strike a zadáním v procentech prémie."""

    def setUp(self) -> None:
        super().setUp()
        self.ib.price_underlying = 230.0
        self.ib.strike_quotes = dict(KOTACE)
        # Delta se dopočítá z ceny opce zvlášť pro každý strike
        self.ib.greek_delta = None

    def zadani(self, **zmeny) -> FlowRequest:
        """
        Zadání CALL v procentech prémie: PT 10 % (30 USD z prémie 3,00),
        SL podle poměru 1:2, tedy 20 % prémie, bez kompenzace spreadu.
        """
        pozadavek = FlowRequest(
            symbol="AAPL",
            entry_price=232.0,
            profit_target=30.0,
            pt_on_underlying=False,
            sl_on_underlying=False,
            pt_in_premium=True,
            sl_in_premium=True,
            premium_base=3.0,
            sl_to_pt_ratio=2.0,
        )
        for klic, hodnota in zmeny.items():
            setattr(pozadavek, klic, hodnota)
        return pozadavek

    async def priprav(self, **zmeny):
        """Náhled ze zadání stejně, jako ho volá formulář nebo import."""
        r = self.zadani(**zmeny)
        return await self.engine.prepare(
            r.symbol,
            r.entry_price,
            r.profit_target,
            r.stop_loss,
            r.pt_on_underlying,
            r.sl_on_underlying,
            r.sl_spread_compensated,
            r.sl_to_pt_ratio,
            r.max_spread_pct,
            premium_base=r.premium_base,
            pt_in_premium=r.pt_in_premium,
            sl_in_premium=r.sl_in_premium,
        )


class TestPosunStrike(ZakladRizika):
    """Příprava zadání hledá vzdálenější strike, když se 1 ks nevejde do rizika."""

    async def test_drahy_strike_se_posune_na_levnejsi(self):
        preview = await self.priprav()

        self.assertEqual(preview.strike, 237.5)
        self.assertEqual(preview.original_strike, 232.5)
        self.assertEqual(preview.quantity, 1)
        self.assertEqual(preview.risk_problem, "")
        self.assertTrue(calc.fits_risk(50.0, preview.loss_per_contract, 1, 10.0))
        # Procenta prémie se zachovají - PT 10 % a SL 20 % z prémie nového kontraktu
        self.assertAlmostEqual(preview.premium_base, preview.expected_fill_price)
        self.assertAlmostEqual(preview.profit_target / preview.premium_base, 10.0, delta=0.01)
        self.assertAlmostEqual(preview.stop_loss / preview.premium_base, 20.0, delta=0.01)
        self.assertTrue(any("Strike posunut z 232.5 na 237.5" in v for v in preview.warnings))

    async def test_po_posunu_zustane_odebirany_jen_novy_kontrakt(self):
        preview = await self.priprav()

        self.assertEqual(
            self.ib.subscribed,
            {UNDERLYING_CONID: 1, self.ib.option_conid(preview.strike): 1},
        )

    async def test_vejde_li_se_strike_neposouva(self):
        # Velký účet - 1 ks na původním strike riziko dodrží
        self.cfg.account.size = 50000.0

        preview = await self.priprav()

        self.assertEqual(preview.strike, 232.5)
        self.assertIsNone(preview.original_strike)
        self.assertEqual(preview.risk_problem, "")

    async def test_mez_delty_hledani_zastavi(self):
        # Už první vzdálenější strike má deltu pod mezí - dál se nehledá
        self.cfg.strike.risk_min_delta = 0.45

        preview = await self.priprav()

        self.assertEqual(preview.strike, 232.5)
        self.assertIn("strike.risk_min_delta", preview.risk_problem)
        self.assertIn(preview.risk_problem, preview.warnings)
        # Odběr zkoušeného kontraktu se uvolnil
        self.assertEqual(
            self.ib.subscribed, {UNDERLYING_CONID: 1, self.ib.option_conid(232.5): 1}
        )

    async def test_mez_kroku_hledani_omezi(self):
        # Jediný povolený krok vede na 235, který se do rizika nevejde
        self.cfg.strike.risk_max_extra_steps = 1

        preview = await self.priprav()

        self.assertEqual(preview.strike, 232.5)
        self.assertIn("235", preview.risk_problem)

    async def test_rezim_cancel_strike_nehleda(self):
        self.cfg.trading.risk_overflow = "cancel"

        preview = await self.priprav()

        self.assertEqual(preview.strike, 232.5)
        self.assertIn("nelze dodržet", preview.risk_problem)

    async def test_rezim_allow_riziko_neposuzuje(self):
        self.cfg.trading.risk_overflow = "allow"

        preview = await self.priprav()

        self.assertEqual(preview.strike, 232.5)
        self.assertEqual(preview.risk_problem, "")
        self.assertEqual(preview.quantity, 1)

    async def test_sl_pevny_v_usd_strike_neposouva(self):
        # SL 80 USD na kontrakt se se strikem nemění - hledat nemá smysl
        preview = await self.priprav(
            profit_target=40.0, stop_loss=80.0, pt_in_premium=False, sl_in_premium=False
        )

        self.assertEqual(preview.strike, 232.5)
        self.assertIn("pevná částka", preview.risk_problem)

    async def test_tolerance_pusti_mirne_prekroceni(self):
        # Na strike 235 je ztráta ≈ 56 USD - s tolerancí 15 % (57,5 USD) projde
        self.cfg.trading.risk_tolerance_pct = 15.0

        preview = await self.priprav()

        self.assertEqual(preview.strike, 235.0)


class TestZadaniARiziko(ZakladRizika):
    """Založení obchodu: import riziko vynucuje, formulář jen varuje."""

    def setUp(self) -> None:
        super().setUp()
        # Nejbližší vzdálenější strike už má deltu pod mezí - nic se nevejde
        self.cfg.strike.risk_min_delta = 0.45

    async def test_import_obchod_bez_vyhovujiciho_strike_nezada(self):
        with self.assertRaises(ValueError) as chyba:
            await self.engine.start_flow(self.zadani(enforce_risk=True))

        self.assertIn("nelze dodržet", str(chyba.exception))
        self.assertEqual(self.engine.flows, {})
        self.assertEqual(self.ib.placed, [])
        self.assertTrue(self.zaznamy("obchod nezadán"))

    async def test_formular_jen_varuje(self):
        flow = await self.engine.start_flow(self.zadani())

        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(flow.strike, 232.5)
        self.assertEqual(flow.quantity, 1)
        self.assertFalse(flow.enforce_risk)
        self.assertTrue(self.zaznamy("POZOR - Riziko na obchod"))

    async def test_import_snizi_rucne_zvysene_mnozstvi(self):
        # Velký účet: doporučené množství se vejde, ručně zvýšené ne
        self.cfg.account.size = 50000.0
        preview = await self.priprav()

        flow = await self.engine.start_flow(self.zadani(enforce_risk=True, quantity=50))

        self.assertEqual(flow.quantity, preview.quantity)
        self.assertTrue(self.zaznamy("množství sníženo z 50"))

    async def test_obchod_na_posunutem_strike_si_pamatuje_puvodni(self):
        self.cfg.strike.risk_min_delta = 0.20

        flow = await self.engine.start_flow(self.zadani(enforce_risk=True))

        self.assertEqual(flow.strike, 237.5)
        self.assertEqual(flow.original_strike, 232.5)
        self.assertTrue(flow.enforce_risk)
        self.assertEqual(flow.option_conid, self.ib.option_conid(237.5))
        self.assertAlmostEqual(flow.premium_base, flow.expected_fill_price)
        self.assertTrue(self.zaznamy("strike posunut z 232.5 na 237.5"))

    async def test_strike_z_nahledu_se_jen_prehodnoti(self):
        # Import posílá kontrakt z přípravy řádku - zadání ho přehodnotí
        # a vzdálenější strike znovu nehledá
        self.cfg.strike.risk_min_delta = 0.20
        preview = await self.priprav()
        overene: list[float] = []
        puvodni = self.ib.qualify_option

        async def qualify_option(symbol, expiration, strike, right, trading_class=""):
            """Zaznamená strike každého ověřovaného kontraktu."""
            overene.append(strike)
            return await puvodni(symbol, expiration, strike, right, trading_class)

        self.ib.qualify_option = qualify_option
        flow = await self.engine.start_flow(
            self.zadani(
                enforce_risk=True,
                strike=preview.strike,
                original_strike=preview.original_strike,
            )
        )

        self.assertEqual(flow.strike, 237.5)
        self.assertEqual(flow.original_strike, 232.5)
        self.assertNotIn(235.0, overene)
        self.assertNotIn(240.0, overene)


class TestRizikoPrepoctu(ZakladRizika):
    """
    Přepočet čekajícího obchodu: zdražená opce, která se do rizika přestala
    vejít, se přesune na vzdálenější strike, nebo se obchod zruší.
    """

    def setUp(self) -> None:
        super().setUp()
        # Zpočátku je opce na 232,5 levná a 1 ks se do rizika vejde
        self.ib.strike_quotes[232.5] = (1.60, 1.66)

    async def zaloz_a_zdraz(self, **zmeny):
        """Založí obchod, po otevření zdraží opci a nechá proběhnout přepočet."""
        flow = await self.engine.start_flow(self.zadani(refresh_after_open_sec=60, **zmeny))
        self.assertEqual(flow.strike, 232.5)
        self.assertEqual(flow.state, FlowState.ARMED)

        self.ib.strike_quotes[232.5] = KOTACE[232.5]
        self.podvrhni_sekundy_po_otevreni(61)
        await self.engine._tick()
        # Hledání strike běží na pozadí - test počká na jeho dokončení
        uloha = self.engine._risk_tasks.get(flow.id)
        if uloha is not None:
            await uloha
        return flow

    async def test_cekajici_obchod_se_presune_na_vzdalenejsi_strike(self):
        flow = await self.zaloz_a_zdraz(enforce_risk=True)
        puvodni_prikaz = self.ib.placed[0]

        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(flow.strike, 237.5)
        self.assertEqual(flow.original_strike, 232.5)
        self.assertEqual(flow.option_conid, self.ib.option_conid(237.5))
        # Příkaz na původní kontrakt se zrušil a nový míří na posunutý strike
        self.assertIn(puvodni_prikaz, self.ib.cancelled)
        self.assertEqual(flow.entry_trade.contract.strike, 237.5)
        self.assertEqual(flow.entry_trade.order.totalQuantity, flow.quantity)
        # Engine drží poslední náhled ze zadání až do další přípravy; bez něj
        # zbývá jen odběr obchodu - podklad a posunutý kontrakt
        self.engine.release_preview()
        self.assertEqual(
            self.ib.subscribed, {UNDERLYING_CONID: 1, self.ib.option_conid(237.5): 1}
        )
        self.assertTrue(self.zaznamy("strike posunut z 232.5 na 237.5"))
        self.assertEqual(self.engine._risk_tasks, {})

    async def test_bez_vyhovujiciho_strike_se_obchod_zrusi(self):
        self.cfg.strike.risk_min_delta = 0.45

        flow = await self.zaloz_a_zdraz(enforce_risk=True)

        self.assertEqual(flow.state, FlowState.CANCELLED)
        self.assertIn("Obchod zrušen před nákupem", flow.message)
        self.assertIn(self.ib.placed[0], self.ib.cancelled)
        self.engine.release_preview()
        self.assertEqual(self.ib.subscribed, {})

    async def test_rucni_obchod_jen_varuje_a_jen_jednou(self):
        self.cfg.strike.risk_min_delta = 0.45

        flow = await self.zaloz_a_zdraz()

        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(flow.strike, 232.5)
        self.assertEqual(self.zaznamy("POZOR - Riziko na obchod"), 1)

        # Další přepočty už nehledají ani nehlásí totéž znovu
        flow.refresh_interval_sec = 1.0
        flow.last_refresh_at = None
        flow.created_at = datetime.now() - timedelta(seconds=10)
        await self.engine._tick()
        self.assertNotIn(flow.id, self.engine._risk_tasks)
        self.assertEqual(self.zaznamy("POZOR - Riziko na obchod"), 1)

    async def test_rezim_cancel_zrusi_bez_hledani(self):
        self.cfg.trading.risk_overflow = "cancel"

        flow = await self.zaloz_a_zdraz(enforce_risk=True)

        self.assertEqual(flow.state, FlowState.CANCELLED)
        self.assertEqual(flow.strike, 232.5)


class TestRizikoVypoctu(unittest.TestCase):
    """Pomocné výpočty a konfigurace pro dodržení rizika."""

    def test_fits_risk_s_toleranci(self):
        self.assertTrue(calc.fits_risk(120.0, 120.0, 1))
        self.assertFalse(calc.fits_risk(120.0, 121.0, 1))
        self.assertTrue(calc.fits_risk(120.0, 132.0, 1, 10.0))
        self.assertFalse(calc.fits_risk(120.0, 70.0, 2, 10.0))

    def test_strikes_beyond_call_i_put(self):
        strikes = [225.0, 227.5, 230.0, 232.5, 235.0]
        self.assertEqual(calc.strikes_beyond(strikes, 230.0, "C", 5), [232.5, 235.0])
        self.assertEqual(calc.strikes_beyond(strikes, 230.0, "P", 1), [227.5])
        self.assertEqual(calc.strikes_beyond(strikes, 230.0, "C", 0), [])

    def test_konfigurace_odmitne_neznamy_rezim(self):
        cfg = vychozi_config()
        cfg.trading.risk_overflow = "ignore"
        cfg.strike.risk_min_delta = 1.5
        with self.assertRaises(ValueError) as chyba:
            validate_config(cfg)
        self.assertIn("trading.risk_overflow", str(chyba.exception))
        self.assertIn("strike.risk_min_delta", str(chyba.exception))


if __name__ == "__main__":
    unittest.main()
