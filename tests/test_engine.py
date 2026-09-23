"""Testy stavového automatu obchodního flow proti náhradě TWS."""

from __future__ import annotations

import sys
import asyncio
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fake_ib import UNDERLYING_CONID
from tests.zaklad import ZakladEnginu
from tws_opce import calc, store
from tws_opce.config import AppConfig, validate_config
from tws_opce.engine import FlowEngine
from tws_opce.models import FlowRequest, FlowState


class ZakladTestu(ZakladEnginu):
    """Společná příprava enginu s náhradou TWS a vzorová zadání."""

    async def zaloz_call(self, **zmeny):
        """Založí vzorové CALL flow: podklad 230, vstup 232, PT 235."""
        self.ib.price_underlying = 230.0
        pozadavek = FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        for klic, hodnota in zmeny.items():
            setattr(pozadavek, klic, hodnota)
        return await self.engine.start_flow(pozadavek)

    async def zaloz_a_vypln(self, **zmeny):
        """
        Založí obchod a vyplní jeho nákup bez průchodu monitorovací smyčkou -
        tedy přesně tak, jak vypadá vyplnění v mezeře mezi dvěma průchody.
        """
        flow = await self.zaloz_call(quantity=1, **zmeny)
        self.ib.fill(flow.entry_trade, 1, 3.10)
        return flow

    async def zaloz_put(self, **zmeny):
        """Založí vzorové PUT flow: podklad 230, vstup 229, PT 226."""
        self.ib.price_underlying = 230.0
        pozadavek = FlowRequest(symbol="AAPL", entry_price=229.0, profit_target=226.0)
        for klic, hodnota in zmeny.items():
            setattr(pozadavek, klic, hodnota)
        return await self.engine.start_flow(pozadavek)

    def zaznamy(self, text: str) -> int:
        """Počet zápisů v provozním logu obsahujících daný text."""
        return sum(1 for _, zprava in self.engine.events if text in zprava)

    def omez_oer(self, limit: float) -> None:
        """
        Nastaví počítadlu OER limit poměru bez volného základu, aby test
        zkoušel samotný limit (počítadlo vzniká v setUp, konfigurace ho už
        nezmění).
        """
        self.ib.oer.limit = limit
        self.ib.oer.free_messages = 0


class TestZalozeniFlow(ZakladTestu):
    """Založení obchodu a podoba nákupního příkazu."""

    async def test_call_se_zada_s_podminkou_nahoru(self):
        flow = await self.zaloz_call()

        self.assertEqual(flow.right, "C")
        self.assertEqual(flow.state, FlowState.ARMED)
        # Strike je první mimo peníze za vstupem 232 (rastr 2,5), ne u PT 235
        self.assertEqual(flow.strike, 232.5)
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
        # Počítá se s deltou při vstupu (0,49), ne s dnešní z TWS (0,35):
        # riziko 50 USD, pohyb ke SL 3 USD -> 50 / 147 = 0 -> minimum 1
        flow = await self.zaloz_call()
        self.assertEqual(flow.quantity, 1)

        # Při větším účtu vyjde více kontraktů: riziko 500 / 147 = 3
        self.cfg.account.size = 50000.0
        flow2 = await self.engine.start_flow(
            FlowRequest(symbol="MSFT", entry_price=232.0, profit_target=235.0)
        )
        self.assertEqual(flow2.quantity, 3)

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


class TestStropuSpreaduVMnozstvi(ZakladTestu):
    """
    Množství i odhad nákupní ceny se počítají se spreadem omezeným limitem.
    Nad limitem se nenakupuje (nevyplněný příkaz se z trhu odstraní), takže
    širší spread obchod nezaplatí a nemá pozici zbytečně zmenšovat.
    """

    async def priprav(
        self, limit: float | None = None, bid: float = 2.60, ask: float = 3.40
    ):
        """Náhled s SL 10 USD/ks na opci, výchozí je široký spread 2,60 / 3,40."""
        # Větší účet, ať je na rozdílu v množství co poznat
        self.cfg.account.size = 50_000.0
        self.ib.price_underlying = 230.0
        self.ib.price_bid, self.ib.price_ask = bid, ask
        return await self.engine.prepare(
            "AAPL", 232.0, 10.0, 10.0, False, False, True, None, limit
        )

    async def test_spread_nad_limitem_se_v_odhadu_ustrihne(self):
        preview = await self.priprav()

        # Strop je limit (5 %) krát odhadovaná nákupní cena opce
        strop = round(self.cfg.trading.max_spread_pct * preview.expected_fill_price, 2)
        self.assertAlmostEqual(preview.sl_spread_usd, strop)
        self.assertLess(preview.sl_spread_usd, calc.spread_usd(2.60, 3.40))

    async def test_strop_zvetsi_mnozstvi(self):
        se_stropem = await self.priprav()

        # Vypnuté zrušení příkazu při překročení limitu strop ruší - příkaz
        # zůstává v trhu a vyplnit se může za jakýkoliv spread
        self.cfg.trading.cancel_on_spread_breach = False
        bez_stropu = await self.priprav()

        self.assertAlmostEqual(bez_stropu.sl_spread_usd, 80.0)
        self.assertGreater(se_stropem.quantity, bez_stropu.quantity)

    async def test_strop_se_ridi_limitem_z_formulare(self):
        preview = await self.priprav(limit=10.0)

        # Formulář posílá vlastní limit; konfigurace platí, jen když chybí
        strop = round(10.0 * preview.expected_fill_price, 2)
        self.assertAlmostEqual(preview.sl_spread_usd, min(80.0, strop))

    async def test_uzky_spread_zustava_cely(self):
        se_stropem = await self.priprav(bid=3.00, ask=3.10)
        self.assertAlmostEqual(se_stropem.sl_spread_usd, 10.0)

        # Spread v limitu se do odhadu nákupní ceny započte celou polovinou
        self.cfg.trading.cancel_on_spread_breach = False
        bez_stropu = await self.priprav(bid=3.00, ask=3.10)
        self.assertAlmostEqual(se_stropem.expected_fill_price, bez_stropu.expected_fill_price)

    async def test_odhad_nakupni_ceny_pricita_spread_nejvys_do_limitu(self):
        # Nad limitem se nenakupuje, takže k modelové ceně se přičte nejvýš
        # půl spreadu odpovídajícího limitu, ne polovina celé kotace
        se_stropem = await self.priprav()
        self.cfg.trading.cancel_on_spread_breach = False
        bez_stropu = await self.priprav()

        # Bez stropu je v odhadu celá polovina spreadu 2,60 / 3,40
        modelova = bez_stropu.expected_fill_price - 0.40
        strop = round(self.cfg.trading.max_spread_pct * modelova, 2) / 100
        self.assertAlmostEqual(se_stropem.expected_fill_price, modelova + strop / 2)
        self.assertLess(se_stropem.expected_fill_price, bez_stropu.expected_fill_price)


class TestVyberuStrike(ZakladTestu):
    """Výběr strike, když nejbližší cena z řetězce není v TWS obchodovatelná."""

    async def test_nedostupny_strike_se_nahradi_nejblizsim_obchodovatelnym(self):
        # Řetězec strike 232,5 nabízí, ale kontrakt pro něj v TWS neexistuje
        self.ib.unavailable_strikes = {232.5}
        preview = await self.engine.prepare("AAPL", 232.0, 235.0)

        # Sousedé 230 a 235 jsou od cíle stejně daleko - přednost má ten
        # mimo peníze, protože 230 by u CALL se vstupem 232 leželo v penězích
        self.assertEqual(preview.strike, 235.0)
        # SL se počítá z cen podkladu (vstup a PT), náhradní strike ho nemění
        self.assertAlmostEqual(preview.stop_loss, 229.0)
        # Náhrada se obchodníkovi hlásí varováním
        self.assertTrue(any("232.5" in varovani for varovani in preview.warnings))

    async def test_nahrada_v_rezimu_target_se_ridi_jen_vzdalenosti(self):
        # Původní režim preferenci strany nezná - rozhoduje jen vzdálenost
        # od cíle a při shodě nižší strike
        self.cfg.strike.mode = "target"
        self.ib.unavailable_strikes = {235.0}
        preview = await self.engine.prepare("AAPL", 232.0, 235.0)

        self.assertEqual(preview.strike, 232.5)
        self.assertTrue(any("235" in varovani for varovani in preview.warnings))

    async def test_bez_obchodovatelneho_strike_priprava_selze(self):
        # Žádný strike z řetězce není obchodovatelný - příprava musí skončit chybou
        self.ib.unavailable_strikes = set(self.ib._strikes())
        with self.assertRaises(ValueError):
            await self.engine.prepare("AAPL", 232.0, 235.0)


class TestRezimuVyberuStrike(ZakladTestu):
    """Režimy strike.mode - podle čeho se vybírá strike opčního kontraktu."""

    async def test_otm_offset_vybere_prvni_strike_za_vstupem(self):
        # Výchozí režim: podklad 230, vstup 232, rastr 2,5 -> první strike
        # nad vstupem je 232,5. Cíl 235 do výběru nemluví
        preview = await self.engine.prepare("AAPL", 232.0, 235.0)
        self.assertEqual(preview.strike, 232.5)

    async def test_otm_offset_u_put_vybira_pod_vstupem(self):
        # U PUT leží mimo peníze strike pod vstupní cenou
        preview = await self.engine.prepare("AAPL", 229.0, 226.0)
        self.assertEqual(preview.right, "P")
        self.assertEqual(preview.strike, 227.5)

    async def test_vice_kroku_posune_strike_dal_od_penez(self):
        self.cfg.strike.otm_steps = 2
        preview = await self.engine.prepare("AAPL", 232.0, 235.0)
        self.assertEqual(preview.strike, 235.0)

    async def test_atm_vybere_nejblizsi_strike_ke_vstupu(self):
        # Vstup 230,4 má nejblíž strike 230, i když u CALL leží v penězích
        self.cfg.strike.mode = "atm"
        preview = await self.engine.prepare("AAPL", 230.4, 235.0)
        self.assertEqual(preview.strike, 230.0)

    async def test_nula_kroku_se_chova_jako_atm(self):
        self.cfg.strike.mode = "otm_offset"
        self.cfg.strike.otm_steps = 0
        preview = await self.engine.prepare("AAPL", 230.4, 235.0)
        self.assertEqual(preview.strike, 230.0)

    async def test_rezim_target_vybira_podle_cile(self):
        # Původní chování zůstává dostupné - strike na cílové úrovni
        self.cfg.strike.mode = "target"
        preview = await self.engine.prepare("AAPL", 232.0, 235.0)
        self.assertEqual(preview.strike, 235.0)

    async def test_zmena_cile_strike_nemeni(self):
        # Strike na cíli nezávisí, takže recalculate nemá co přepočítávat
        self.cfg.trading.pt_change_strike = "recalculate"
        flow = await self.zaloz_call()
        puvodni_prikaz = flow.entry_trade

        await self.engine.change_profit_target(flow.id, 240.0)

        self.assertEqual(flow.strike, 232.5)
        self.assertIs(flow.entry_trade, puvodni_prikaz)
        self.assertNotIn(puvodni_prikaz, self.ib.cancelled)


class TestKontrolyDelty(ZakladTestu):
    """Upozornění, když delta vybrané opce vypadne z mezí v konfiguraci."""

    async def test_nizka_delta_vyvola_varovani(self):
        # Strike 250 je od vstupu 232 daleko - při vstupu vyjde delta 0,26,
        # zatímco TWS hlásí pro dnešní cenu podkladu 0,35 a mezí by prošla
        self.cfg.strike.mode = "target"
        self.cfg.strike.delta_warn_min = 0.30
        preview = await self.engine.prepare("AAPL", 232.0, 250.0)
        self.assertEqual(preview.strike, 250.0)
        self.assertTrue(any("pod hranicí" in v for v in preview.warnings))

    async def test_vysoka_delta_vyvola_varovani(self):
        # Delta při vstupu je 0,49; dnešní z TWS (0,35) by mezí prošla
        self.cfg.strike.delta_warn_max = 0.40
        preview = await self.engine.prepare("AAPL", 232.0, 235.0)
        self.assertTrue(any("nad hranicí" in v for v in preview.warnings))

    async def test_delta_v_pasmu_je_bez_varovani(self):
        self.ib.greek_delta = 0.35
        preview = await self.engine.prepare("AAPL", 232.0, 235.0)
        self.assertFalse(any("hranicí" in v for v in preview.warnings))

    async def test_u_put_se_porovnava_absolutni_hodnota(self):
        # Delta PUT je záporná, mez se přesto vyhodnotí správně
        self.ib.greek_delta = -0.35
        preview = await self.engine.prepare("AAPL", 229.0, 226.0)
        self.assertEqual(preview.right, "P")
        self.assertFalse(any("hranicí" in v for v in preview.warnings))

    async def test_nulova_mez_kontrolu_vypne(self):
        # Týž scénář jako u nízké delty, jen s vypnutou dolní mezí
        self.cfg.strike.mode = "target"
        self.cfg.strike.delta_warn_min = 0.0
        preview = await self.engine.prepare("AAPL", 232.0, 250.0)
        self.assertFalse(any("hranicí" in v for v in preview.warnings))


class TestDeltyPriVstupu(ZakladTestu):
    """
    Delta se pro množství i kontrolu mezí bere ve chvíli nákupu, ne z dnešní
    ceny podkladu - opce se kupuje teprve na vstupní úrovni.
    """

    async def test_nahled_nese_obe_delty(self):
        # Podklad 230, vstup 232: TWS hlásí deltu pro dnešek, model dopočítá
        # tu při vstupu, kde je opce blíž penězům a delta vyšší
        preview = await self.engine.prepare("AAPL", 232.0, 235.0)
        self.assertAlmostEqual(preview.delta, 0.35)
        self.assertAlmostEqual(preview.entry_delta, 0.49, places=2)

    async def test_delta_pri_vstupu_je_u_put_zaporna(self):
        self.ib.greek_delta = -0.35
        preview = await self.engine.prepare("AAPL", 229.0, 226.0)
        self.assertEqual(preview.right, "P")
        self.assertLess(preview.entry_delta, 0)

    async def test_nizka_delta_z_tws_neovlivni_kontrolu(self):
        # Zadání daleko od trhu: TWS hlásí deltu 0,07, protože opce je dnes
        # hluboko mimo peníze. Při vstupu ale bude u peněz, takže varovat
        # se nemá - právě tohle dřív hlásilo planě
        self.ib.greek_delta = 0.07
        preview = await self.engine.prepare("AAPL", 232.0, 235.0)
        self.assertFalse(any("hranicí" in v for v in preview.warnings))

    async def test_mnozstvi_vychazi_z_delty_pri_vstupu(self):
        # Množství se nesmí řídit dnešní deltou: ta je nižší, ztráta na
        # kontrakt by z ní vyšla menší a kontraktů by se koupilo víc,
        # než odpovídá riziku
        self.cfg.account.size = 50000.0
        self.ib.greek_delta = 0.07
        preview = await self.engine.prepare("AAPL", 232.0, 235.0)

        # Riziko 500 USD, pohyb ke SL 3 USD, delta při vstupu 0,49 -> 3 ks.
        # S deltou 0,07 z TWS by vyšlo 23 kontraktů
        self.assertEqual(preview.quantity, 3)

    async def test_bez_ceny_opce_zbyva_delta_z_tws(self):
        # Bez kotací nelze implikovanou volatilitu spočítat - model odpadá
        # a rozhoduje delta, kterou poslalo TWS
        self.ib.price_bid = None
        self.ib.price_ask = None
        self.ib.price_last = None
        self.ib.price_close = None
        self.ib.greek_delta = 0.35
        self.cfg.trading.entry_order_type = "MKT"
        preview = await self.engine.prepare("AAPL", 232.0, 235.0)
        self.assertIsNone(preview.entry_delta)
        self.assertAlmostEqual(preview.delta, 0.35)


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


    async def test_propasnuty_vstup_ukonci_obchod_blokovany_spreadem(self):
        # Před otevřením trhu bývá spread opce široký, takže se příkaz do trhu
        # nevrací - propásnutý vstup přesto musí obchod ukončit hned
        flow = await self.zaloz_call()
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.50
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.SPREAD_BLOCKED)

        # Spread zůstává nad limitem, jen cena překoná vstupní úroveň
        self.ib.price_underlying = 233.0
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.MISSED)
        self.assertIn("vstup propásnut", flow.message)
        # Příkaz se do trhu nevrátil
        self.assertEqual(len(self.ib.placed), 1)

    async def test_propasnuty_vstup_ukonci_obchod_cekajici_na_kotace(self):
        # Bez kotací opce se příkaz nezadá a obchod čeká; propásnutý vstup
        # jej musí ukončit i bez nich
        self.ib.price_bid, self.ib.price_ask = None, None
        flow = await self.zaloz_call()
        self.assertEqual(flow.state, FlowState.NO_QUOTES)

        self.ib.price_underlying = 233.0
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.MISSED)
        self.assertIn("vstup propásnut", flow.message)

    async def test_propasnuty_vstup_mimo_hodiny_sunda_prikaz_z_trhu(self):
        flow = await self.zaloz_call()
        self.assertEqual(flow.state, FlowState.ARMED)

        # Mimo obchodní hodiny cenová podmínka spustit nemůže; příkaz čekající
        # na už propásnuté úrovni by po otevření trhu koupil za horší cenu
        self.engine.market_open_seconds = lambda: 300.0
        self.ib.price_underlying = 233.0
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.MISSED)
        self.assertIn("odstraněn z trhu", flow.message)
        self.assertEqual(len(self.ib.cancelled), 1)

    async def test_propasnuty_vstup_behem_seance_prikaz_v_trhu_nerusi(self):
        flow = await self.zaloz_call()
        self.assertEqual(flow.state, FlowState.ARMED)

        # Během seance se příkaz na své cenové podmínce právě plní - jeho
        # zrušení by s dobíhajícím vyplněním závodilo
        self.engine.market_open_seconds = lambda: None
        self.ib.price_underlying = 233.0
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(self.ib.cancelled, [])

    async def test_propasnuty_vstup_u_put_ukonci_obchod_blokovany_spreadem(self):
        # Zrcadlově k CALL: u PUT je vstup propásnutý cenou pod vstupem
        flow = await self.zaloz_put()
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.50
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.SPREAD_BLOCKED)

        self.ib.price_underlying = 228.0
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.MISSED)
        self.assertIn("vstup propásnut", flow.message)


class TestSmeruZeZadani(ZakladTestu):
    """
    Směr uvedený v zadání (intended_right) proti odvození z ceny podkladu.

    Jsou-li PT i SL zadané na opci, z čísel se směr určit nedá a bez tohoto
    údaje o typu opce rozhodne poloha vstupu vůči aktuální ceně - short
    zadaný pod aktuální cenou by se pak založil jako CALL.
    """

    async def zaloz_na_opci(self, entry: float, **zmeny):
        """Obchod s PT i SL v USD na kontrakt: podklad 230, PT 10 USD/ks."""
        self.ib.price_underlying = 230.0
        pozadavek = FlowRequest(
            symbol="AAPL",
            entry_price=entry,
            profit_target=10.0,
            pt_on_underlying=False,
            sl_on_underlying=False,
        )
        for klic, hodnota in zmeny.items():
            setattr(pozadavek, klic, hodnota)
        return await self.engine.start_flow(pozadavek)

    async def test_short_nad_cenou_se_zada_jako_put(self):
        # Vstup 229 je pod cenou 230, průraz dolů teprve nastane
        self.ib.greek_delta = -0.35
        flow = await self.zaloz_na_opci(229.0, intended_right="P")

        self.assertEqual(flow.right, "P")
        self.assertEqual(flow.state, FlowState.ARMED)

    async def test_short_pod_cenou_se_odmitne_misto_zalozeni_callu(self):
        # Vstup 231 je nad cenou 230: short čekal průraz dolů, ten už nastat
        # nemůže. Bez směru v zadání by z ceny vyšel CALL a koupila by se
        # opačná opce
        with self.assertRaises(ValueError) as ctx:
            await self.zaloz_na_opci(231.0, intended_right="P")

        self.assertIn("propásnutý", str(ctx.exception))
        self.assertEqual(self.ib.placed, [])

    async def test_long_nad_cenou_se_odmitne(self):
        # Zrcadlově: long se vstupem 229 pod cenou 230 už také ujel
        with self.assertRaises(ValueError) as ctx:
            await self.zaloz_na_opci(229.0, intended_right="C")

        self.assertIn("propásnutý", str(ctx.exception))
        self.assertEqual(self.ib.placed, [])

    async def test_odmitnuty_short_nezrusi_cekajici_long(self):
        # Nahrazuje se jen obchod stejného směru. Kdyby se short založil jako
        # CALL, sebral by místo čekajícímu longu téhož tickeru
        long_flow = await self.zaloz_call()
        self.assertEqual(long_flow.state, FlowState.ARMED)

        with self.assertRaises(ValueError):
            await self.zaloz_na_opci(231.0, intended_right="P")

        self.assertIn(long_flow.id, self.engine.flows)
        self.assertEqual(long_flow.state, FlowState.ARMED)
        self.assertEqual(self.ib.cancelled, [])
        self.assertEqual(len(self.ib.placed), 1)

    async def test_bez_smeru_v_zadani_rozhoduje_cena(self):
        # Běžný formulář směr neuvádí - u úrovní na opci zůstává rozhodnutí
        # na poloze vstupu vůči aktuální ceně
        flow = await self.zaloz_na_opci(231.0)

        self.assertEqual(flow.right, "C")
        self.assertEqual(flow.state, FlowState.ARMED)

    async def test_smer_odporujici_urovnim_na_podkladu_se_odmitne(self):
        # Cena podkladu nedorazila, takže typ opce vyšel z polohy PT nad
        # vstupem (CALL) - zadaný short je proti němu protichůdné zadání
        self.ib.price_underlying = None
        with self.assertRaises(ValueError) as ctx:
            await self.engine.start_flow(
                FlowRequest(
                    symbol="AAPL",
                    entry_price=232.0,
                    profit_target=235.0,
                    intended_right="P",
                )
            )

        self.assertIn("neodpovídá", str(ctx.exception))
        self.assertEqual(self.ib.placed, [])

    async def test_neplatny_smer_se_odmitne(self):
        with self.assertRaises(ValueError) as ctx:
            await self.zaloz_na_opci(229.0, intended_right="X")

        self.assertIn("Neplatný směr", str(ctx.exception))


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

    async def test_levna_opce_s_uzkym_spreadem_se_zada(self):
        # 0,14 / 0,16 = 13,3 % nad limitem 5 %, ale rozdíl 0,02 USD u opce
        # pod 0,30 USD vyhoví výjimce
        self.ib.price_bid, self.ib.price_ask = 0.14, 0.16
        flow = await self.zaloz_call()

        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(len(self.ib.placed), 1)

    async def test_priznak_vyjimky_pro_sloupec_max_spread(self):
        # Nákup povolený jen výjimkou se v přehledu zvýrazní
        self.ib.price_bid, self.ib.price_ask = 0.14, 0.16
        flow = await self.zaloz_call()
        self.assertTrue(flow.cheap_spread_allows)

        # Spread v procentním limitu výjimku nepotřebuje
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.10
        await self.engine._tick()
        self.assertFalse(flow.cheap_spread_allows)

        # Po nákupu se spread nehlídá a výjimka nic nepovoluje
        self.ib.price_bid, self.ib.price_ask = 0.14, 0.16
        await self.engine._tick()
        self.assertTrue(flow.cheap_spread_allows)
        flow.set_state(FlowState.FILLED, "Vyplněno.")
        self.assertFalse(flow.cheap_spread_allows)

    async def test_levna_opce_se_sirsim_spreadem_se_nezada(self):
        # Rozdíl 0,03 USD výjimku nesplní
        self.ib.price_bid, self.ib.price_ask = 0.14, 0.17
        flow = await self.zaloz_call()

        self.assertEqual(flow.state, FlowState.SPREAD_BLOCKED)
        self.assertEqual(self.ib.placed, [])

    async def test_drazsi_opce_s_uzkym_spreadem_se_nezada(self):
        # Střed 0,35 je nad hranicí 0,30 - rozdíl 0,02 = 5,9 % > limit 5 %
        self.ib.price_bid, self.ib.price_ask = 0.34, 0.36
        flow = await self.zaloz_call()

        self.assertEqual(flow.state, FlowState.SPREAD_BLOCKED)

    async def test_vypnuta_vyjimka_levnou_opci_zablokuje(self):
        self.cfg.trading.cheap_option_max_price = 0.0
        self.ib.price_bid, self.ib.price_ask = 0.14, 0.16
        flow = await self.zaloz_call()

        self.assertEqual(flow.state, FlowState.SPREAD_BLOCKED)

    async def test_levna_opce_zustane_v_trhu_a_vrati_se_do_nej(self):
        self.ib.price_bid, self.ib.price_ask = 0.14, 0.16
        flow = await self.zaloz_call()

        # Rozšíření na 0,04 USD příkaz odstraní z trhu
        self.ib.price_bid, self.ib.price_ask = 0.14, 0.18
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.SPREAD_BLOCKED)
        self.assertEqual(len(self.ib.cancelled), 1)

        # Po zúžení na 0,02 USD a uplynutí prodlevy se vrátí, i když procenta
        # rezervu pod limitem nesplní
        self.ib.price_bid, self.ib.price_ask = 0.14, 0.16
        flow.blocked_since = datetime.now() - timedelta(seconds=30)
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(len(self.ib.placed), 2)

    async def test_vypnute_ruseni_ponecha_prikaz_v_trhu(self):
        self.cfg.trading.cancel_on_spread_breach = False
        flow = await self.zaloz_call()
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.50
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(self.ib.cancelled, [])


class TestProdlevyNavratuPoSpreadu(ZakladTestu):
    """
    Prodlužování prodlevy před návratem příkazu do trhu a limit OER.
    Kolísající spread u levné opce jinak příkaz ruší a zadává každých pár
    sekund a každý takový cyklus stojí dvě zprávy do TWS.
    """

    async def odstran_a_vrat(self, flow, sekund_od_odstraneni: float) -> None:
        """Spread nad limit (odstranění), pak zpět v limitu po dané době."""
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.50
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.SPREAD_BLOCKED)
        self.ib.price_bid, self.ib.price_ask = 3.00, 3.10
        flow.blocked_since = datetime.now() - timedelta(seconds=sekund_od_odstraneni)
        await self.engine._tick()

    async def test_prodleva_se_s_kazdym_odstranenim_prodlouzi(self):
        self.cfg.trading.rearm_delay_sec = 5.0
        self.cfg.trading.rearm_delay_factor = 1.5
        flow = await self.zaloz_call()

        # První odstranění - základní prodleva 5 s
        await self.odstran_a_vrat(flow, 6)
        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(flow.spread_breaches, 1)

        # Druhé odstranění - násobek 1,5 dává 7,5 s, 6 s je ještě brzy
        await self.odstran_a_vrat(flow, 6)
        self.assertEqual(flow.state, FlowState.SPREAD_BLOCKED)
        flow.blocked_since = datetime.now() - timedelta(seconds=8)
        await self.engine._tick()
        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(flow.spread_breaches, 2)

    async def test_pocet_odstraneni_prezije_ulozeni(self):
        # Restart nesmí vrátit prodlevu před návratem do trhu na začátek
        flow = await self.zaloz_call()
        await self.odstran_a_vrat(flow, 6)
        obnovene = store.dict_to_flow(store.flow_to_dict(flow))
        self.assertEqual(obnovene.spread_breaches, 1)

    async def test_vypocet_prodlevy(self):
        # (základ, strop, násobek, počet odstranění, očekávaná prodleva)
        tabulka = (
            # Výchozí násobek 1,5 od základu 30 s až po strop 600 s
            (30.0, 600.0, 1.5, 1, 30.0),
            (30.0, 600.0, 1.5, 2, 45.0),
            (30.0, 600.0, 1.5, 3, 67.5),
            (30.0, 600.0, 1.5, 4, 101.25),
            (30.0, 600.0, 1.5, 8, 512.578125),
            (30.0, 600.0, 1.5, 9, 600.0),
            # Zdvojování se stropem 60 s
            (5.0, 60.0, 2.0, 4, 40.0),
            (5.0, 60.0, 2.0, 5, 60.0),
            (5.0, 60.0, 2.0, 40, 60.0),
            # Násobek 1 i strop pod základem prodlužování vypínají
            (30.0, 600.0, 1.0, 500, 30.0),
            (5.0, 0.0, 2.0, 6, 5.0),
            # Velký násobek ani počet odstranění výpočet nepřetečou
            (30.0, 600.0, 10.0, 10_000, 600.0),
        )
        flow = await self.zaloz_call()
        trading = self.cfg.trading
        for zaklad, strop, nasobek, pocet, cekani in tabulka:
            trading.rearm_delay_sec = zaklad
            trading.rearm_delay_max_sec = strop
            trading.rearm_delay_factor = nasobek
            flow.spread_breaches = pocet
            with self.subTest(zaklad=zaklad, strop=strop, nasobek=nasobek, pocet=pocet):
                self.assertAlmostEqual(self.engine._rearm_delay(flow), cekani)

    async def test_navrat_do_trhu_respektuje_limit_oer(self):
        # Limit 3: zadání (1) + zrušení (1) + návrat by potřeboval další dvě
        self.omez_oer(3.0)
        flow = await self.zaloz_call()

        await self.odstran_a_vrat(flow, 30)

        self.assertEqual(flow.state, FlowState.SPREAD_BLOCKED)
        self.assertEqual(self.ib.oer.messages, 2)
        self.assertEqual(self.zaznamy("návrat příkazu do trhu odloženo"), 1)


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
        # Podmínka na podkladu spustila, příkaz čeká na burze
        flow.entry_trade.orderStatus.status = "Submitted"

        # ASK vyroste na 3,60 -> limit 3,672 -> na tik 3,65
        self.ib.price_bid, self.ib.price_ask = 3.55, 3.60
        await self.engine._tick()

        self.assertAlmostEqual(flow.entry_limit, 3.65)
        self.assertNotAlmostEqual(flow.entry_limit, puvodni)
        # Modifikace se posílá pod stejným orderId
        self.assertEqual(len(self.ib.placed), 1)
        self.assertAlmostEqual(self.ib.placed[0].order.lmtPrice, 3.65)

    async def test_prikaz_cekajici_na_podminku_se_neupravuje(self):
        # Podmínka ještě nespustila (PreSubmitted) - příkaz na burze neleží
        # a úprava za každým pohybem ASK by jen zvyšovala OER
        flow = await self.zaloz_call()
        puvodni = flow.entry_limit
        self.assertEqual(flow.entry_trade.orderStatus.status, "PreSubmitted")

        self.ib.price_bid, self.ib.price_ask = 3.55, 3.60
        await self.engine._tick()

        self.assertAlmostEqual(flow.entry_limit, puvodni)
        self.assertAlmostEqual(self.ib.placed[0].order.lmtPrice, puvodni)
        # Do TWS šlo jen samotné zadání příkazu
        self.assertEqual(self.ib.oer.messages, 1)

    async def test_prekonany_vstup_limit_upravi_i_bez_zmeny_stavu(self):
        # Podklad vstup 232 překonal - podmínka spouští, i když TWS stav
        # příkazu (PreSubmitted) ještě nepřepsala; limit se upraví hned
        self.podvrhni_cas_burzy(10, 0)
        flow = await self.zaloz_call()
        self.ib.price_underlying = 232.5

        self.ib.price_bid, self.ib.price_ask = 3.55, 3.60
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertAlmostEqual(flow.entry_limit, 3.65)

    async def test_uprava_pred_spustenim_podminky_lze_zapnout(self):
        # Volba relimit_before_trigger vrací původní chování
        self.cfg.trading.relimit_before_trigger = True
        flow = await self.zaloz_call()

        self.ib.price_bid, self.ib.price_ask = 3.55, 3.60
        await self.engine._tick()

        self.assertAlmostEqual(flow.entry_limit, 3.65)
        self.assertEqual(self.ib.oer.messages, 2)

    async def test_uprava_limitu_respektuje_limit_oer(self):
        # Limit OER 2 dovolí bez vyplnění jen dvě zprávy: zadání příkazu
        # a rezervu na jeho zrušení - na úpravu limitu už nezbude
        self.omez_oer(2.0)
        flow = await self.zaloz_call()
        puvodni = flow.entry_limit
        flow.entry_trade.orderStatus.status = "Submitted"

        self.ib.price_bid, self.ib.price_ask = 3.55, 3.60
        await self.engine._tick()
        await self.engine._tick()

        self.assertAlmostEqual(flow.entry_limit, puvodni)
        self.assertEqual(self.ib.oer.messages, 1)
        # Odklad se do logu hlásí jen jednou, ne každým průchodem smyčkou
        self.assertEqual(self.zaznamy("odloženo"), 1)
        self.assertEqual(self.zaznamy("přelimitování nákupního příkazu odloženo"), 1)

    async def test_vyplneni_uvolni_limit_oer(self):
        # Každý vyplněný příkaz přidá prostor pro další limit zpráv
        self.omez_oer(2.0)
        flow = await self.zaloz_call()
        flow.entry_trade.orderStatus.status = "Submitted"
        self.ib.price_bid, self.ib.price_ask = 3.55, 3.60
        await self.engine._tick()
        self.assertEqual(self.ib.oer.messages, 1)

        # Vyplnění jiného příkazu aplikace (jiné orderId) zvedne limit na 4
        jiny = self.ib.place(flow.option_contract, self.ib.market_sell_order(1, "JINY"))
        self.ib.fill(jiny, 1, 3.00)
        await self.engine._tick()

        self.assertEqual(self.ib.oer.executed, 1)
        self.assertAlmostEqual(flow.entry_limit, 3.65)

    async def test_drobna_zmena_prikaz_nemodifikuje(self):
        flow = await self.zaloz_call()
        puvodni = flow.entry_limit
        flow.entry_trade.orderStatus.status = "Submitted"

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
        # Nastavení recalculate vybere podle nového cíle jiný kontrakt.
        # Na cíli závisí strike jen v režimu "target", jinak se přepočet
        # neuplatní - kontrakt se vybírá od vstupní ceny
        self.cfg.strike.mode = "target"
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
        # 40 % z pěti kontraktů (zaokrouhleno dolů) dává runner o 2 ks
        self.cfg.trading.runner_quantity_pct = 40.0
        flow = await self.zaloz_call(quantity=5)
        await self.engine.set_runner(flow.id, 2.0)
        await self.nakup(flow, 5)

        hlavni = next(t for t in self.ib.placed if t.order.orderRef.endswith(":exit"))
        runner = next(t for t in self.ib.placed if t.order.orderRef.endswith(":runner"))
        self.assertEqual(int(hlavni.order.totalQuantity), 3)
        self.assertEqual(int(runner.order.totalQuantity), 2)

    async def test_velikost_runneru_je_nejmene_jeden_kontrakt(self):
        # 25 % ze tří kontraktů je po zaokrouhlení dolů nula - runner
        # přesto dostane jeden kontrakt
        flow = await self.zaloz_call(quantity=3)
        await self.engine.set_runner(flow.id, 2.0)
        self.assertEqual(flow.runner_quantity, 1)

    async def test_procento_ze_zadani_prebiji_konfiguraci(self):
        # Pole Runner [%] v zadání: 50 % z 8 ks dává runner o 4 ks,
        # ačkoli konfigurace drží výchozích 25 %
        flow = await self.zaloz_call(quantity=8, runner_quantity_pct=50.0)
        await self.engine.set_runner(flow.id, 2.0)
        self.assertEqual(flow.runner_quantity, 4)

    async def test_procento_ze_zadani_plati_i_pro_automaticky_runner(self):
        flow = await self.zaloz_call(
            quantity=8,
            runner_multiple=2.0,
            runner_min_quantity=3,
            runner_quantity_pct=50.0,
        )
        self.assertTrue(flow.runner_active)
        self.assertEqual(flow.runner_quantity, 4)
        # Volbu si obchod nese s sebou i do uloženého stavu
        obnoveny = store.dict_to_flow(store.flow_to_dict(flow))
        self.assertEqual(obnoveny.runner_quantity_pct, 50.0)

    async def test_runner_nelze_zapnout_na_jediny_kontrakt(self):
        # Runneru by nezbyl protějšek v hlavní části
        flow = await self.zaloz_call(quantity=1)
        with self.assertRaises(ValueError):
            await self.engine.set_runner(flow.id, 2.0)

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

    async def test_hromadne_vycisteni_vyprazdni_prehled(self):
        # Tři obchody v různých stavech: běžící před nákupem, běžící v pozici
        # a jeden už ukončený - po vyčištění nesmí zůstat ani jeden
        pred_nakupem = await self.zaloz_call()
        ukonceny = await self.zaloz_put()
        await self.engine.cancel_flow(ukonceny.id)

        self.ib.price_underlying = 100.0
        v_pozici = await self.engine.start_flow(
            FlowRequest(symbol="MSFT", entry_price=102.0, profit_target=105.0)
        )
        self.ib.fill(v_pozici.entry_trade, 1, 3.00)
        await self.engine._tick()
        await self.engine._tick()
        self.assertEqual(v_pozici.state, FlowState.EXIT_ARMED)

        zruseno, smazano = await self.engine.cancel_and_clear_all()

        self.assertEqual(zruseno, 2)
        self.assertEqual(smazano, 3)
        self.assertEqual(self.engine.flows, {})
        # Obchod v pozici se trhem neuzavírá, jen se zruší jeho zajištění
        self.assertEqual(v_pozici.state, FlowState.CANCELLED)
        self.assertIn("bez zajištění", v_pozici.message)
        self.assertEqual(pred_nakupem.state, FlowState.CANCELLED)

    async def test_hromadne_vycisteni_prazdneho_prehledu(self):
        zruseno, smazano = await self.engine.cancel_and_clear_all()
        self.assertEqual((zruseno, smazano), (0, 0))


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

    async def test_ukoncene_obchody_se_deli_na_dnesni_a_starsi(self):
        # Tři ukončené obchody: jeden dnešní a dva ze dvou různých starších dnů
        dnesni = await self.engine.start_flow(
            FlowRequest(symbol="ZM", entry_price=232.0, profit_target=235.0)
        )
        vcerejsi = await self.engine.start_flow(
            FlowRequest(symbol="MSFT", entry_price=232.0, profit_target=235.0)
        )
        predvcerejsi = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        # Aktivní obchod musí zůstat nahoře bez ohledu na abecedu i stáří
        aktivni = await self.engine.start_flow(
            FlowRequest(symbol="TSLA", entry_price=232.0, profit_target=235.0)
        )

        for flow in (dnesni, vcerejsi, predvcerejsi):
            await self.engine.cancel_flow(flow.id)
        vcerejsi.created_at -= timedelta(days=1)
        predvcerejsi.created_at -= timedelta(days=2)

        poradi = [f.symbol for f in self.engine.sorted_flows()]
        self.assertEqual(poradi, ["TSLA", "ZM", "MSFT", "AAPL"])

    async def test_nakoupene_obchody_stoji_nad_cekajicimi(self):
        # Nakoupený obchod má peníze v trhu, proto patří nad ty před nákupem
        # i tehdy, když je abecedně později
        pred_nakupem = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        v_pozici = await self.engine.start_flow(
            FlowRequest(symbol="ZM", entry_price=232.0, profit_target=235.0)
        )
        self.ib.fill(v_pozici.entry_trade, 1, 3.00)
        await self.engine._tick()

        self.assertFalse(v_pozici.state.is_before_entry)
        self.assertEqual(pred_nakupem.state, FlowState.ARMED)
        poradi = [f.symbol for f in self.engine.sorted_flows()]
        self.assertEqual(poradi, ["ZM", "AAPL"])

    async def test_uvnitr_nakoupenych_plati_abeceda(self):
        # Dělení na sekce nesmí zrušit abecední pořadí uvnitř sekce
        zm = await self.engine.start_flow(
            FlowRequest(symbol="ZM", entry_price=232.0, profit_target=235.0)
        )
        aapl = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=232.0, profit_target=235.0)
        )
        for flow in (zm, aapl):
            self.ib.fill(flow.entry_trade, 1, 3.00)
        await self.engine._tick()

        poradi = [f.symbol for f in self.engine.sorted_flows()]
        self.assertEqual(poradi, ["AAPL", "ZM"])


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
        """Podvrhne čas burzy se zapnutým automatickým uzavíráním."""
        self.engine.auto_close_on = True
        self.podvrhni_cas_burzy(hodina, minuta, den)

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
        self.engine.auto_close_on = False
        self.assertIsNone(self.engine.auto_close_seconds())

    async def test_odpocet_do_otevreni_trhu(self):
        # Hodinu před otevřením zbývá 3600 sekund
        self.burza(8, 30)
        self.assertAlmostEqual(self.engine.market_open_seconds(), 3600.0)

        # Během seance se odpočet nezobrazuje
        self.burza(11, 0)
        self.assertIsNone(self.engine.market_open_seconds())

        # Po zavření se míří na otevření následujícího obchodního dne
        self.burza(17, 30)
        self.assertAlmostEqual(self.engine.market_open_seconds(), 16 * 3600.0)

        # Pátek 21. 8. 2026 po zavření - nejbližší otevření je až v pondělí
        self.burza(17, 30, den=21)
        self.assertAlmostEqual(self.engine.market_open_seconds(), 64 * 3600.0)

        # Sobota 22. 8. 2026 dopoledne - stále se čeká na pondělní otevření
        self.burza(9, 0, den=22)
        self.assertAlmostEqual(self.engine.market_open_seconds(), (48 + 0.5) * 3600.0)

        # Odpočet nezávisí na automatickém uzavírání obchodů
        self.burza(8, 30)
        self.engine.auto_close_on = False
        self.assertAlmostEqual(self.engine.market_open_seconds(), 3600.0)

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


class TestZruseniCekajicich(ZakladTestu):
    """Zrušení čekajících obchodů v pevně daný čas dne."""

    def burza(self, hodina: int, minuta: int, den: int = 19) -> None:
        """Podvrhne čas burzy se zapnutým rušením čekajících obchodů."""
        self.engine.pending_cancel_on = True
        self.podvrhni_cas_burzy(hodina, minuta, den)

    async def test_odpocet_sekund_do_zruseni(self):
        # Půl hodiny před výchozím časem 12:00 zbývá 1800 sekund
        self.burza(11, 30)
        self.assertAlmostEqual(self.engine.pending_cancel_seconds(), 1800.0)

        # Uvnitř okna je odpočet nulový - okno trvá až do zavření burzy
        self.burza(14, 0)
        self.assertEqual(self.engine.pending_cancel_seconds(), 0.0)

        # Po zavření burzy se dnes už neruší
        self.burza(16, 5)
        self.assertIsNone(self.engine.pending_cancel_seconds())

        # Sobota 22. 8. 2026 - burza neobchoduje
        self.burza(14, 0, den=22)
        self.assertIsNone(self.engine.pending_cancel_seconds())

        # Čas zadaný až za zavřením burzy okno nikdy neotevře
        self.burza(11, 30)
        self.cfg.trading.pending_cancel_time = "17:00"
        self.assertIsNone(self.engine.pending_cancel_seconds())

        # Vypnutá funkce odpočet nenabízí
        self.cfg.trading.pending_cancel_time = "12:00"
        self.engine.pending_cancel_on = False
        self.assertIsNone(self.engine.pending_cancel_seconds())

    async def test_okno_nezacne_pred_otevrenim_burzy(self):
        # Čas před otevřením se posune na otevření, aby okno nerušilo
        # obchody nachystané právě na open
        self.cfg.trading.pending_cancel_time = "08:00"

        # Půl hodiny před otevřením se odpočet měří k otevření v 9:30
        self.burza(9, 0)
        self.assertAlmostEqual(self.engine.pending_cancel_seconds(), 1800.0)

        # Od otevření dál už okno běží
        self.burza(9, 30)
        self.assertEqual(self.engine.pending_cancel_seconds(), 0.0)

    async def test_cekajici_se_zrusi_a_pozice_bezi_dal(self):
        cekajici = await self.zaloz_call()
        drzeny = await self.zaloz_put()
        self.ib.fill(drzeny.entry_trade, 1, 3.10)
        await self.engine._tick()
        await self.engine._tick()

        self.burza(12, 0)
        await self.engine._tick()

        # Obchod před nákupem je zrušen a jeho příkaz odstraněn z trhu
        self.assertEqual(cekajici.state, FlowState.CANCELLED)
        self.assertIn("12:00", cekajici.message)
        self.assertIn(cekajici.entry_trade, self.ib.cancelled)

        # Nakoupená pozice běží dál se svým zajištěním
        self.assertEqual(drzeny.state, FlowState.EXIT_ARMED)
        self.assertNotIn(drzeny.exit_trade, self.ib.cancelled)

    async def test_pred_casem_se_obchody_nechavaji(self):
        flow = await self.zaloz_call()

        # Minutu před nastaveným časem se ještě nic neděje
        self.burza(11, 59)
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(self.ib.cancelled, [])

    async def test_vypnuta_funkce_nerusi(self):
        flow = await self.zaloz_call()

        self.burza(14, 0)
        self.engine.pending_cancel_on = False
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(self.ib.cancelled, [])


class TestPrepinacuOken(ZakladTestu):
    """
    Runtime zapnutí a vypnutí časovaných funkcí z hlavičky.

    Konfigurace dává jen výchozí hodnotu; obchodník ji za běhu přebíjí,
    aniž by se sahalo do souboru.
    """

    def test_vychozi_hodnota_je_z_konfigurace(self):
        # Testovací konfigurace má obě funkce vypnuté
        self.assertFalse(self.engine.pending_cancel_on)
        self.assertFalse(self.engine.auto_close_on)

        self.cfg.trading.pending_cancel_enabled = True
        self.cfg.trading.auto_close_enabled = True
        engine = FlowEngine(self.cfg, self.ib)
        self.assertTrue(engine.pending_cancel_on)
        self.assertTrue(engine.auto_close_on)

    async def test_vypnute_ruseni_pusti_zadani_i_odpoledne(self):
        # Se zapnutou funkcí by odpolední zadání skončilo rovnou zrušené
        # (viz TestZadaniVOkne); s vypnutou jde do trhu jako každé jiné
        self.engine.pending_cancel_on = False
        self.podvrhni_cas_burzy(14, 0)

        flow = await self.zaloz_call()
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertIsNone(self.engine.pending_cancel_seconds())

    async def test_vypnute_uzavirani_necha_pozici_bezet(self):
        self.engine.auto_close_on = False
        self.podvrhni_cas_burzy(15, 50)
        flow = await self.zaloz_a_vypln()
        await self.engine._tick()
        await self.engine._tick()

        # Pozice se před koncem seance neuzavírá, běží se svým zajištěním
        self.assertEqual(flow.state, FlowState.EXIT_ARMED)
        self.assertIsNone(self.engine.auto_close_seconds())

    async def test_zapnuti_za_behu_funkci_obnovi(self):
        # Vypnutá funkce v konfiguraci nebrání tomu ji za běhu zapnout
        self.podvrhni_cas_burzy(14, 0)
        flow = await self.zaloz_call()
        self.assertEqual(flow.state, FlowState.ARMED)

        self.engine.pending_cancel_on = True
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.CANCELLED)
        self.assertIn("12:00", flow.message)


class TestZadaniVOkne(ZakladTestu):
    """Zadání obchodu v době, kdy by ho okno vzápětí zrušilo."""

    def burza(self, hodina: int, minuta: int, ruseni=False, uzavirani=False) -> None:
        """Podvrhne čas burzy se zvolenými okny zapnutými."""
        self.engine.pending_cancel_on = ruseni
        self.engine.auto_close_on = uzavirani
        self.podvrhni_cas_burzy(hodina, minuta)

    async def test_zadani_v_rusicim_okne_nejde_do_trhu(self):
        self.burza(14, 0, ruseni=True)

        flow = await self.zaloz_call()

        # Obchod vzniká rovnou ukončený a do TWS se neposlalo nic
        self.assertEqual(flow.state, FlowState.CANCELLED)
        self.assertIsNone(flow.entry_trade)
        self.assertEqual(self.ib.placed, [])
        self.assertIn("12:00", flow.message)

    async def test_zadani_v_uzaviracim_okne_nejde_do_trhu(self):
        self.burza(15, 50, uzavirani=True)

        flow = await self.zaloz_call()

        self.assertEqual(flow.state, FlowState.CANCELLED)
        self.assertIsNone(flow.entry_trade)
        self.assertEqual(self.ib.placed, [])
        self.assertIn("uzavíracím okně", flow.message)

    async def test_zadani_mimo_okna_jde_do_trhu(self):
        self.burza(11, 0, ruseni=True, uzavirani=True)

        flow = await self.zaloz_call()

        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(len(self.ib.placed), 1)

    async def test_znovuzadani_prikazu_v_okne_do_trhu_nejde(self):
        # Stráž sedí v _place_entry, takže platí i pro cesty, kterými se
        # příkaz vrací do trhu později - třeba po uvolnění spreadu
        self.burza(11, 0, ruseni=True)
        flow = await self.zaloz_call()
        flow.set_state(FlowState.SPREAD_BLOCKED, "Spread nad limitem.")
        self.ib.placed.clear()

        self.podvrhni_cas_burzy(12, 0)
        self.assertFalse(self.engine._place_entry(flow))

        self.assertEqual(flow.state, FlowState.CANCELLED)
        self.assertEqual(self.ib.placed, [])

    async def test_vypnuta_okna_zadani_nebrani(self):
        # Obě funkce vypnuté - odpoledne se zadává normálně
        self.burza(14, 0)

        flow = await self.zaloz_call()

        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(len(self.ib.placed), 1)


class TestOpozdenehoVyplneni(ZakladTestu):
    """
    Nákup vyplněný v mezeře mezi dvěma průchody smyčky.

    Stav obchodu je do nejbližšího průchodu stále "před nákupem", takže bez
    dobrání vyplnění by se čerstvě otevřená pozice ukončila jako obchod bez
    pozice a zůstala v TWS bez zajištění.
    """

    async def zaloz_pred_polednem(self):
        """Založí obchod před polednem, tedy mimo obě okna, a vyplní jej."""
        self.podvrhni_cas_burzy(11, 0)
        return await self.zaloz_a_vypln()

    async def test_rusici_okno_nesahne_na_prave_nakoupeny_obchod(self):
        flow = await self.zaloz_pred_polednem()

        self.engine.pending_cancel_on = True
        self.podvrhni_cas_burzy(12, 0)
        await self.engine._tick()

        # Obchod se nezrušil, pozice se zajistila jako každá jiná
        self.assertEqual(flow.state, FlowState.EXIT_ARMED)
        self.assertEqual(flow.fill_price, 3.10)
        self.assertIsNotNone(flow.exit_trade)

    async def test_uzaviraci_okno_prave_nakoupenou_pozici_proda_trhem(self):
        flow = await self.zaloz_pred_polednem()

        self.engine.auto_close_on = True
        self.podvrhni_cas_burzy(15, 50)
        await self.engine._tick()

        # Pozice se uzavírá trhem, ne že by se obchod zrušil jako čekající
        self.assertEqual(flow.state, FlowState.CLOSING)
        self.assertEqual(flow.fill_price, 3.10)

    async def test_rucni_zruseni_pozici_neprehledne(self):
        flow = await self.zaloz_pred_polednem()

        await self.engine.cancel_flow(flow.id)

        # Pozice se rozpoznala, uživatel dostal varování místo tichého
        # "zrušeno před nákupem"
        self.assertEqual(flow.state, FlowState.CANCELLED)
        self.assertEqual(flow.fill_price, 3.10)
        self.assertEqual(flow.filled_quantity, 1)
        self.assertIn("zůstává otevřená", flow.message)

    async def test_rucni_zruseni_s_uzavrenim_pozici_proda(self):
        flow = await self.zaloz_pred_polednem()

        await self.engine.cancel_flow(flow.id, close_position=True)

        self.assertEqual(flow.state, FlowState.CLOSING)
        await self.engine._tick()
        self.assertEqual(flow.exit_trade.order.orderType, "MKT")

    async def test_nevyplneny_obchod_se_rusi_beze_zmeny(self):
        # Kontrola, že dobírání nezasáhlo do běžného rušení čekajícího obchodu
        self.podvrhni_cas_burzy(11, 0)
        flow = await self.zaloz_call()

        await self.engine.cancel_flow(flow.id)

        self.assertEqual(flow.state, FlowState.CANCELLED)
        self.assertIsNone(flow.fill_price)
        self.assertIn("před nákupem", flow.message)


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
        # Riziko 123,45 USD, pohyb 3 USD, delta při vstupu 0,49 -> 123,45 / 147 = 1
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
        self.assertEqual(druhy.quantity, 33)


class TestProvizi(ZakladTestu):
    """Přebírání skutečně účtovaných provizí z TWS do obchodů."""

    async def test_provize_z_nakupu_i_prodeje_se_rozdeli_podle_prikazu(self):
        flow = await self.zaloz_call(quantity=2)
        self.ib.fill(flow.entry_trade, 2, 3.00, commission=1.30)
        await self.engine._tick()
        await self.engine._tick()

        self.assertAlmostEqual(flow.entry_commission, 1.30)
        self.assertAlmostEqual(flow.exit_commission, 0.0)

        # Prodej na PT přinese druhou provizi - tentokrát do prodejního kbelíku
        self.ib.price_underlying = 235.5
        self.ib.fill(flow.exit_trade, 2, 4.00, commission=1.30)
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.CLOSED)
        self.assertAlmostEqual(flow.exit_commission, 1.30)
        self.assertAlmostEqual(flow.commission, 2.60)
        # Zisk 200 USD snížený o obě strany provize
        self.assertAlmostEqual(flow.realized_pnl_net, 197.40)

    async def test_opakovany_pruchod_smyckou_provizi_nezdvojnasobi(self):
        flow = await self.zaloz_call(quantity=1)
        self.ib.fill(flow.entry_trade, 1, 3.00, commission=0.65)
        for _ in range(4):
            await self.engine._tick()

        self.assertAlmostEqual(flow.entry_commission, 0.65)

    async def test_provize_ciziho_prikazu_se_ignoruje(self):
        flow = await self.zaloz_call(quantity=1)
        self.ib.fill(flow.entry_trade, 1, 3.00)
        # Vyplnění příkazu bez značky aplikace (ruční obchod v TWS)
        cizi = self.ib.placed[-1]
        cizi.order.orderRef = "RUCNI"
        self.ib.record_commission(cizi, 5.0)
        await self.engine._tick()

        self.assertAlmostEqual(flow.commission, 0.0)

    async def test_castecne_plneni_secte_provize_po_castech(self):
        flow = await self.zaloz_call(quantity=5)
        self.ib.fill(flow.entry_trade, 2, 3.00, status="Submitted", commission=1.30)
        await self.engine._tick()
        self.ib.fill(flow.entry_trade, 5, 3.00, commission=1.95)
        await self.engine._tick()

        # Dvě exekuce s vlastním execId se sečtou, nepřepíší
        self.assertAlmostEqual(flow.entry_commission, 3.25)


class ZakladPrehleduStavu(ZakladTestu):
    """Přehled se zástupcem každého stavu - sdílí jej obě varianty úklidu."""

    async def priprav_prehled(self) -> dict[str, FlowState]:
        """
        Přehled se zástupcem každého zajímavého stavu: čekající před nákupem,
        otevřená pozice, uzavřený obchod, zrušený, propásnutý a chybový.
        """
        otevreny = await self.zaloz_call(symbol="MSFT", quantity=1)
        self.ib.fill(otevreny.entry_trade, 1, 3.00)
        await self.engine._tick()
        await self.engine._tick()

        # Uzavření přes PT vyžaduje podklad nad cílem 235; cena se hned vrací
        uzavreny = await self.zaloz_call(symbol="AMZN", quantity=1)
        self.ib.fill(uzavreny.entry_trade, 1, 3.00)
        await self.engine._tick()
        await self.engine._tick()
        self.ib.price_underlying = 235.5
        self.ib.fill(uzavreny.exit_trade, 1, 4.00)
        await self.engine._tick()
        self.ib.price_underlying = 230.0

        # Čekající obchod vzniká až po výletu ceny nad 235: kontrola
        # propásnutého vstupu běží ve všech stavech před nákupem a mimo
        # obchodní hodiny by čekající příkaz se vstupem 232 při ceně 235,5
        # zrušila jako propásnutý - test by pak závisel na denní době
        cekajici = await self.zaloz_call(symbol="AAPL")

        zruseny = await self.zaloz_call(symbol="TSLA")
        await self.engine.cancel_flow(zruseny.id)

        propasnuty = await self.zaloz_call(symbol="NFLX")
        propasnuty.set_state(FlowState.MISSED, "Vstup propásnut.")

        chybovy = await self.zaloz_call(symbol="META")
        chybovy.set_state(FlowState.ERROR, "Něco se pokazilo.")

        return {
            "cekajici": cekajici.id,
            "otevreny": otevreny.id,
            "uzavreny": uzavreny.id,
            "zruseny": zruseny.id,
            "propasnuty": propasnuty.id,
            "chybovy": chybovy.id,
        }


class TestUkliduNeobchodovanych(ZakladPrehleduStavu):
    """Odstranění obchodů, které se nikdy nedostaly k nákupu."""

    async def test_odstrani_zrusene_i_propasnute(self):
        ids = await self.priprav_prehled()
        self.assertEqual(self.engine.flows[ids["zruseny"]].state, FlowState.CANCELLED)

        self.assertEqual(self.engine.remove_untraded(), 2)
        self.assertNotIn(ids["zruseny"], self.engine.flows)
        self.assertNotIn(ids["propasnuty"], self.engine.flows)

    async def test_ostatni_obchody_v_prehledu_zustavaji(self):
        ids = await self.priprav_prehled()
        self.engine.remove_untraded()

        # Čekající, otevřený i uzavřený zůstávají; chybový vyžaduje ruční
        # kontrolu a nesmí z přehledu tiše zmizet
        for klic in ("cekajici", "otevreny", "uzavreny", "chybovy"):
            self.assertIn(ids[klic], self.engine.flows, klic)

    async def test_prazdny_uklid_nic_neodstrani(self):
        await self.zaloz_call(symbol="AAPL")
        self.assertEqual(self.engine.remove_untraded(), 0)
        self.assertEqual(len(self.engine.flows), 1)

    async def test_zruseny_obchod_s_pozici_v_prehledu_zustava(self):
        # Zrušení nakoupeného obchodu bez uzavření nechá pozici otevřenou
        # a nezajištěnou v TWS - takový řádek nesmí úklid odklidit
        flow = await self.zaloz_call(symbol="TSLA", quantity=1)
        self.ib.fill(flow.entry_trade, 1, 3.00)
        await self.engine._tick()
        await self.engine._tick()
        await self.engine.cancel_flow(flow.id)

        self.assertEqual(flow.state, FlowState.CANCELLED)
        self.assertIsNotNone(flow.fill_price)
        self.assertEqual(self.engine.remove_untraded(), 0)
        self.assertIn(flow.id, self.engine.flows)

    async def test_opakovany_uklid_uz_nic_nenajde(self):
        await self.priprav_prehled()
        self.assertEqual(self.engine.remove_untraded(), 2)
        self.assertEqual(self.engine.remove_untraded(), 0)


class TestUkliduSCekajicimi(ZakladPrehleduStavu):
    """
    Úklid, který kromě neobchodovaných odklidí i obchody čekající na nákup.

    Sdílí přípravu přehledu s úklidem neobchodovaných, aby obě varianty
    pracovaly nad stejnou sadou stavů a rozdíl mezi nimi byl vidět.
    """

    async def test_odklidi_i_cekajici_a_zrusi_jejich_prikazy(self):
        ids = await self.priprav_prehled()
        cekajici = self.engine.flows[ids["cekajici"]]
        prikaz = cekajici.entry_trade

        zruseno, odstraneno = await self.engine.remove_untraded_and_pending()

        # Čekající obchod se zrušil a jeho příkaz zmizel z trhu
        self.assertEqual(zruseno, 1)
        self.assertIn(prikaz, self.ib.cancelled)
        self.assertNotIn(ids["cekajici"], self.engine.flows)

        # Zrušený a propásnutý zmizely stejně jako při prostém úklidu
        self.assertEqual(odstraneno, 3)
        self.assertNotIn(ids["zruseny"], self.engine.flows)
        self.assertNotIn(ids["propasnuty"], self.engine.flows)

    async def test_pozice_uzavrene_i_chybove_zustavaji(self):
        ids = await self.priprav_prehled()
        await self.engine.remove_untraded_and_pending()

        for klic in ("otevreny", "uzavreny", "chybovy"):
            self.assertIn(ids[klic], self.engine.flows, klic)

    async def test_obchod_blokovany_spreadem_se_take_odklidi(self):
        # Blokace spreadem je stav před nákupem, takže do úklidu patří
        flow = await self.zaloz_call()
        flow.set_state(FlowState.SPREAD_BLOCKED, "Spread nad limitem.")

        zruseno, odstraneno = await self.engine.remove_untraded_and_pending()

        self.assertEqual((zruseno, odstraneno), (1, 1))
        self.assertEqual(self.engine.flows, {})

    async def test_prave_vyplneny_obchod_uklid_neodklidi(self):
        # Nákup vyplněný mezi průchody smyčky nesmí skončit jako uklizený
        # obchod s pozicí bez zajištění
        flow = await self.zaloz_a_vypln()

        zruseno, odstraneno = await self.engine.remove_untraded_and_pending()

        self.assertEqual((zruseno, odstraneno), (0, 0))
        self.assertIn(flow.id, self.engine.flows)
        self.assertEqual(flow.fill_price, 3.10)

    async def test_prazdny_uklid_nic_nezrusi(self):
        zruseno, odstraneno = await self.engine.remove_untraded_and_pending()
        self.assertEqual((zruseno, odstraneno), (0, 0))


class ZakladPrepoctu(ZakladTestu):
    """
    Společná příprava testů přepočtu čekajícího obchodu (po otevření burzy
    i průběžného): účet 50 000 USD (riziko 500 USD), podvržený čas burzy
    a vzorový obchod v procentech prémie.
    """

    def setUp(self) -> None:
        super().setUp()
        # Větší účet, aby množství nebylo přibité na minimu 1 ks
        self.cfg.account.size = 50000.0

    def burza(self, sekund_po_otevreni: float) -> None:
        """Podvrhne čas burzy na daný počet sekund od otevření (středa 19. 8. 2026)."""
        otevreni = datetime(2026, 8, 19, 9, 30, tzinfo=ZoneInfo("America/New_York"))
        self.engine._exchange_now = lambda: otevreni + timedelta(seconds=sekund_po_otevreni)

    async def zaloz_v_premii(self, **zmeny):
        """
        Obchod v procentech prémie: PT 9 USD/ks jsou 3 % z prémie 3,00
        (300 USD na kontrakt), SL podle poměru 1:1, přepočet 60 s po otevření.
        """
        self.ib.price_underlying = 230.0
        pozadavek = FlowRequest(
            symbol="AAPL",
            entry_price=232.0,
            profit_target=9.0,
            pt_on_underlying=False,
            sl_on_underlying=False,
            pt_in_premium=True,
            sl_in_premium=True,
            premium_base=3.0,
            sl_to_pt_ratio=1.0,
            refresh_after_open_sec=60,
        )
        for klic, hodnota in zmeny.items():
            setattr(pozadavek, klic, hodnota)
        return await self.engine.start_flow(pozadavek)


class TestPrepoctuPoOtevreni(ZakladPrepoctu):
    """
    Přepočet čekajícího obchodu po otevření burzy podle živých kotací.

    Obchod zadaný před otevřením má úrovně i množství z odhadu prémie;
    po prodlevě od otevření se dopočítají znovu a příkaz v trhu se upraví
    na místě. Přepočet běží jednou a jen u obchodu, který ještě čeká na vstup.
    """

    def zmen_kotace(self) -> None:
        """Opce po otevření zlevnila - jiná implikovaná volatilita, jiná delta."""
        self.ib.price_bid = 1.50
        self.ib.price_ask = 1.55
        self.ib.greek_delta = 0.20

    async def ocekavane(self, **zmeny):
        """
        Co by z týchž kotací spočítala čerstvá příprava zadání - přepočet
        musí dojít ke stejným číslům, jinak by dialog a engine počítaly jinak.
        """
        parametry = dict(
            symbol="AAPL",
            entry_price=232.0,
            profit_target=235.0,
            stop_loss=None,
            pt_on_underlying=True,
            sl_on_underlying=True,
            sl_spread_compensated=False,
            sl_to_pt_ratio=None,
            max_spread_pct=5.0,
        )
        parametry.update(zmeny)
        return await self.engine.prepare(**parametry)

    async def test_prepocet_upravi_mnozstvi_i_prikaz_v_trhu(self):
        flow = await self.zaloz_v_premii()
        puvodni_ks = flow.quantity
        self.assertEqual(flow.state, FlowState.ARMED)

        # Opce zdražila: stejná procenta prémie jsou větší ztráta na kontrakt
        # a z rizika 500 USD vyjde méně kontraktů
        self.ib.price_bid = 4.00
        self.ib.price_ask = 4.10
        self.burza(61)
        await self.engine._tick()

        self.assertTrue(flow.refresh_after_open_done)
        self.assertEqual(
            flow.quantity, calc.suggest_quantity_for_loss(500.0, flow.stop_loss, 1, 100)
        )
        self.assertLess(flow.quantity, puvodni_ks)
        # Příkaz v trhu se upravil na místě: stejné orderId, nové množství
        self.assertEqual(len(self.ib.placed), 1)
        self.assertEqual(self.ib.placed[0].order.totalQuantity, flow.quantity)
        self.assertEqual(flow.entry_order_id, self.ib.placed[0].order.orderId)
        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertIn("Přepočteno po otevření", flow.message)
        self.assertTrue(
            any("přepočteno 61 s po otevření" in zprava for _, zprava in self.engine.events)
        )

    async def test_na_podkladu_vyjde_stejne_jako_cerstva_priprava(self):
        flow = await self.zaloz_call(refresh_after_open_sec=60)
        self.zmen_kotace()
        nahled = await self.ocekavane()

        self.burza(61)
        await self.engine._tick()

        # Přepočet a příprava zadání musí z týchž kotací dojít ke stejným číslům
        self.assertTrue(flow.refresh_after_open_done)
        self.assertEqual(flow.quantity, nahled.quantity)
        # PT i SL na podkladu nezávisí na kotacích - zůstávají
        self.assertAlmostEqual(flow.profit_target, 235.0)
        self.assertAlmostEqual(flow.stop_loss, 229.0)
        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(len(self.ib.placed), 1)
        self.assertEqual(self.ib.placed[0].order.totalQuantity, flow.quantity)

    async def test_pred_uplynutim_prodlevy_se_ceka(self):
        flow = await self.zaloz_call(refresh_after_open_sec=60)
        puvodni_ks = flow.quantity
        self.zmen_kotace()

        self.burza(30)
        await self.engine._tick()
        self.assertFalse(flow.refresh_after_open_done)
        self.assertEqual(flow.quantity, puvodni_ks)

        # Prodleva uplynula - přepočet proběhne při dalším průchodu
        self.burza(60)
        await self.engine._tick()
        self.assertTrue(flow.refresh_after_open_done)

    async def test_mimo_obchodni_hodiny_se_neprepocitava(self):
        flow = await self.zaloz_call(refresh_after_open_sec=60)
        puvodni_ks = flow.quantity
        self.zmen_kotace()

        # Hodinu před otevřením burza nemá živé kotace, přepočet nemá z čeho vyjít
        self.burza(-3600)
        await self.engine._tick()
        self.assertFalse(flow.refresh_after_open_done)
        self.assertEqual(flow.quantity, puvodni_ks)

    async def test_bez_volby_se_nic_nemeni(self):
        flow = await self.zaloz_call()
        puvodni_ks = flow.quantity
        self.zmen_kotace()

        self.burza(600)
        await self.engine._tick()
        self.assertIsNone(flow.refresh_after_open_sec)
        self.assertFalse(flow.refresh_after_open_done)
        self.assertEqual(flow.quantity, puvodni_ks)

    async def test_prepocet_probehne_jen_jednou(self):
        flow = await self.zaloz_call(refresh_after_open_sec=60)
        self.zmen_kotace()
        self.burza(61)
        await self.engine._tick()
        prepoctene_ks = flow.quantity

        # Další změna kotací už množství nehýbe
        self.ib.price_bid = 6.00
        self.ib.price_ask = 6.10
        self.ib.greek_delta = 0.60
        self.burza(120)
        await self.engine._tick()
        self.assertEqual(flow.quantity, prepoctene_ks)

    async def test_pri_spreadu_nad_limitem_prepocita_hned_s_max_spreadem(self):
        flow = await self.zaloz_v_premii(sl_spread_compensated=True)
        puvodni_ks = flow.quantity

        # Široký spread po otevření přepočet nezdrží - kompenzace SL se
        # počítá s limitem spreadu (Max. spread), stejně jako v dialogu
        self.ib.price_bid = 3.00
        self.ib.price_ask = 3.50
        self.burza(61)
        await self.engine._tick()

        self.assertTrue(flow.refresh_after_open_done)
        # Prémie vychází ze stejného odhadu nákupní ceny jako čerstvá příprava,
        # tedy i se spreadem ustřiženým na limit
        nahled = await self.ocekavane()
        self.assertAlmostEqual(flow.premium_base, nahled.expected_fill_price)
        # Přehled stropuje odhad spreadu z nového odhadu nákupní ceny
        self.assertAlmostEqual(flow.expected_fill_price, nahled.expected_fill_price)
        strop = round(flow.max_spread_pct * flow.premium_base, 2)
        self.assertLess(strop, calc.spread_usd(3.00, 3.50))
        self.assertEqual(
            flow.quantity,
            calc.suggest_quantity_for_loss(500.0, flow.stop_loss + strop, 1, 100),
        )
        self.assertNotEqual(flow.quantity, puvodni_ks)
        # Příkaz jde z trhu neupravený - modifikace těsně před stažením
        # by jen závodila se zrušením
        self.assertEqual(flow.state, FlowState.SPREAD_BLOCKED)
        self.assertEqual(len(self.ib.placed), 1)
        self.assertEqual(self.ib.placed[0].order.totalQuantity, puvodni_ks)

        # Spread se stáhl - příkaz se vrací do trhu s přepočteným množstvím
        # a přepočet se neopakuje
        prepoctene_ks = flow.quantity
        self.ib.price_ask = 3.05
        flow.blocked_since = datetime.now() - timedelta(seconds=60)
        self.burza(120)
        await self.engine._tick()

        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(flow.quantity, prepoctene_ks)
        self.assertEqual(self.ib.placed[-1].order.totalQuantity, prepoctene_ks)

    async def test_pt_v_procentech_premie_se_prepocita_z_nove_ceny(self):
        flow = await self.zaloz_v_premii()
        self.assertAlmostEqual(flow.stop_loss, 9.0)

        # Opce zdražila - stejná tři procenta znamenají větší částku
        self.ib.price_bid = 4.00
        self.ib.price_ask = 4.10
        self.burza(61)
        await self.engine._tick()

        self.assertTrue(flow.refresh_after_open_done)
        self.assertGreater(flow.premium_base, 3.0)
        self.assertAlmostEqual(flow.profit_target, round(3.0 * flow.premium_base, 2))
        self.assertAlmostEqual(flow.original_profit_target, flow.profit_target)
        self.assertAlmostEqual(flow.stop_loss, round(flow.profit_target * 1.0, 2))
        self.assertAlmostEqual(flow.original_stop_loss, flow.stop_loss)
        # Množství vychází z nové ztráty na kontrakt: 500 USD / (SL × 100)
        self.assertEqual(
            flow.quantity,
            calc.suggest_quantity_for_loss(500.0, flow.stop_loss, 1, 100),
        )

    async def test_pt_v_usd_zustava_prepocita_se_jen_zbytek(self):
        flow = await self.engine.start_flow(
            FlowRequest(
                symbol="AAPL",
                entry_price=232.0,
                profit_target=60.0,
                pt_on_underlying=False,
                sl_on_underlying=False,
                sl_spread_compensated=True,
                sl_to_pt_ratio=0.5,
                refresh_after_open_sec=60,
            )
        )
        self.assertAlmostEqual(flow.profit_target, 60.0)
        self.assertAlmostEqual(flow.stop_loss, 30.0)

        self.zmen_kotace()
        self.burza(61)
        await self.engine._tick()

        self.assertTrue(flow.refresh_after_open_done)
        # Částka v USD není odvozená od prémie, přepočet ji nemění
        self.assertAlmostEqual(flow.profit_target, 60.0)
        self.assertAlmostEqual(flow.stop_loss, 30.0)
        self.assertIsNone(flow.premium_base)

    async def test_runner_pred_nakupem_se_prepocita_na_nove_urovne(self):
        flow = await self.zaloz_v_premii()
        await self.engine.set_runner(flow.id, 2.0)
        self.assertAlmostEqual(flow.runner_profit_target, 18.0)

        self.ib.price_bid = 4.00
        self.ib.price_ask = 4.10
        self.burza(61)
        await self.engine._tick()

        # Dvojnásobek se počítá z nového PT, SL runneru sleduje nový SL
        self.assertTrue(flow.runner_active)
        self.assertAlmostEqual(flow.runner_profit_target, round(flow.profit_target * 2, 2))
        self.assertAlmostEqual(flow.runner_stop_loss, flow.stop_loss)

    async def test_runner_se_vypne_kdyz_na_nej_mnozstvi_nestaci(self):
        flow = await self.zaloz_call(refresh_after_open_sec=60)
        await self.engine.set_runner(flow.id, 2.0)
        self.assertTrue(flow.runner_active)

        # Účet se mezitím scvrkl - riziko dovolí jediný kontrakt
        self.cfg.account.size = 100.0
        self.burza(61)
        await self.engine._tick()

        self.assertEqual(flow.quantity, 1)
        self.assertFalse(flow.runner_active)
        self.assertTrue(
            any("runner po přepočtu vypnut" in zprava for _, zprava in self.engine.events)
        )

    async def test_nakoupeny_obchod_se_neprepocitava(self):
        flow = await self.zaloz_call(refresh_after_open_sec=60)
        puvodni_ks = flow.quantity
        self.ib.fill(flow.entry_trade, puvodni_ks, 3.05)
        await self.engine._tick()
        self.assertFalse(flow.state.is_before_entry)

        self.zmen_kotace()
        self.burza(61)
        await self.engine._tick()
        self.assertFalse(flow.refresh_after_open_done)
        self.assertEqual(flow.quantity, puvodni_ks)

    async def test_castecne_vyplneny_prikaz_se_neupravuje(self):
        flow = await self.zaloz_call(refresh_after_open_sec=60)
        puvodni_ks = flow.quantity
        # Příkaz se právě plní - modifikace by závodila s vyplněním
        flow.entry_trade.orderStatus.filled = 1
        flow.entry_trade.orderStatus.status = "Submitted"

        self.zmen_kotace()
        self.burza(61)
        # Vyplnění se zaregistruje dřív, než přepočet přijde na řadu
        await self.engine._tick()
        self.assertFalse(flow.refresh_after_open_done)
        self.assertEqual(self.ib.placed[0].order.totalQuantity, puvodni_ks)

    async def test_smiseny_rezim_urovni_se_vynecha(self):
        flow = await self.engine.start_flow(
            FlowRequest(
                symbol="AAPL",
                entry_price=232.0,
                profit_target=235.0,
                stop_loss=60.0,
                pt_on_underlying=True,
                sl_on_underlying=False,
                refresh_after_open_sec=60,
            )
        )
        puvodni = (flow.profit_target, flow.stop_loss, flow.quantity)
        self.zmen_kotace()
        self.burza(61)
        await self.engine._tick()

        self.assertTrue(flow.refresh_after_open_done)
        self.assertEqual((flow.profit_target, flow.stop_loss, flow.quantity), puvodni)
        self.assertTrue(
            any("přepočet po otevření vynechán" in zprava for _, zprava in self.engine.events)
        )

    async def test_cas_od_otevreni_burzy(self):
        self.burza(90)
        self.assertAlmostEqual(self.engine.market_open_elapsed(), 90.0)
        # Před otevřením i po zavření burzy se čas od otevření neměří
        self.burza(-60)
        self.assertIsNone(self.engine.market_open_elapsed())
        self.burza(7 * 3600)
        self.assertIsNone(self.engine.market_open_elapsed())

    async def test_volba_prezije_ulozeni_stavu(self):
        flow = await self.zaloz_call(refresh_after_open_sec=45)
        flow.refresh_after_open_done = True
        obnoveny = store.dict_to_flow(store.flow_to_dict(flow))
        self.assertEqual(obnoveny.refresh_after_open_sec, 45)
        self.assertTrue(obnoveny.refresh_after_open_done)

        # Obchod bez volby ji nemá ani po obnově
        bez = await self.zaloz_call(symbol="MSFT")
        self.assertIsNone(store.dict_to_flow(store.flow_to_dict(bez)).refresh_after_open_sec)


class TestAutomatickehoRunneru(ZakladTestu):
    """
    Runner podle volby zadání: engine ho zapne při založení obchodu, má-li
    obchod alespoň minimum kontraktů, a volbu si obchod nese s sebou pro
    přepočty. Ruční zásah do runneru před nákupem volbu přepisuje.
    """

    async def test_runner_podle_zadani_se_zapne_pri_zalozeni(self):
        flow = await self.zaloz_call(quantity=4, runner_multiple=2.0, runner_min_quantity=3)

        # Vstup 232, PT 235: dvojnásobek vzdálenosti dává cíl runneru 238
        self.assertTrue(flow.runner_active)
        self.assertEqual(flow.runner_quantity, 1)
        self.assertAlmostEqual(flow.runner_profit_target, 238.0)
        self.assertAlmostEqual(flow.runner_stop_loss, flow.stop_loss)
        # Volba zůstává u obchodu pro přepočty množství
        self.assertAlmostEqual(flow.auto_runner_multiple, 2.0)
        self.assertEqual(flow.auto_runner_min_quantity, 3)

    async def test_pod_minimem_se_runner_nezapne(self):
        flow = await self.zaloz_call(quantity=2, runner_multiple=2.0, runner_min_quantity=3)

        self.assertFalse(flow.runner_active)
        # Volba ale zůstává - runner přijde, až na něj množství doroste
        self.assertAlmostEqual(flow.auto_runner_multiple, 2.0)
        self.assertEqual(self.zaznamy("runner nezapnut - množství 2 ks je pod minimem 3 ks"), 1)
        # Důvod zůstává u obchodu pro rozhraní
        self.assertEqual(flow.runner_skip_reason, "množství 2 ks je pod minimem 3 ks")

    async def test_pozice_presne_na_minimu_runner_dostane(self):
        flow = await self.zaloz_call(quantity=3, runner_multiple=1.5, runner_min_quantity=3)
        self.assertTrue(flow.runner_active)

    async def test_nula_znamena_bez_runneru_i_pri_nahrazeni(self):
        # Výslovné "Nepoužít runner" má přednost před runnerem nahrazovaného
        # čekajícího obchodu; bez volby (None) se runner dál přebírá
        prvni = await self.zaloz_call(quantity=3)
        await self.engine.set_runner(prvni.id, 2.0)

        druhe = await self.zaloz_call(quantity=3, runner_multiple=0.0)

        self.assertFalse(druhe.runner_active)
        self.assertEqual(druhe.auto_runner_multiple, 0.0)

    async def test_bez_volby_se_prevezme_volba_nahrazeneho_i_s_minimem(self):
        # Nahrazený obchod nesl volbu 2× od tří kontraktů; nové zadání bez
        # volby ji převezme a runner zapne až podle svého množství
        prvni = await self.zaloz_call(quantity=4, runner_multiple=2.0, runner_min_quantity=3)
        self.assertTrue(prvni.runner_active)

        druhe = await self.zaloz_call(quantity=2, profit_target=236.0)

        self.assertAlmostEqual(druhe.auto_runner_multiple, 2.0)
        self.assertEqual(druhe.auto_runner_min_quantity, 3)
        self.assertFalse(druhe.runner_active)
        self.assertEqual(self.zaznamy("převzata z nahrazeného obchodu"), 1)

    async def test_rucni_runner_pred_nakupem_prepise_volbu(self):
        flow = await self.zaloz_call(quantity=4, runner_multiple=0.0, runner_min_quantity=3)
        self.assertFalse(flow.runner_active)

        # Ručně zapnutý runner platí bez ohledu na minimum ze zadání
        await self.engine.set_runner(flow.id, 1.5)
        self.assertAlmostEqual(flow.auto_runner_multiple, 1.5)
        self.assertIsNone(flow.auto_runner_min_quantity)

        # Ručně zrušený runner nemá přepočet zapínat znovu
        await self.engine.cancel_runner(flow.id)
        self.assertEqual(flow.auto_runner_multiple, 0.0)

    async def test_volba_prezije_ulozeni_stavu(self):
        flow = await self.zaloz_call(
            quantity=4, runner_multiple=2.0, runner_min_quantity=3, refresh_interval_sec=30.0
        )
        flow.last_refresh_at = datetime(2026, 9, 17, 15, 45, 10)

        obnovene = store.dict_to_flow(store.flow_to_dict(flow))

        self.assertAlmostEqual(obnovene.auto_runner_multiple, 2.0)
        self.assertEqual(obnovene.auto_runner_min_quantity, 3)
        self.assertAlmostEqual(obnovene.refresh_interval_sec, 30.0)
        self.assertEqual(obnovene.last_refresh_at, flow.last_refresh_at)

        # Nula (runner nepoužít) se nesmí ztratit jako prázdná hodnota
        bez = await self.zaloz_call(quantity=2, runner_multiple=0.0)
        self.assertEqual(store.dict_to_flow(store.flow_to_dict(bez)).auto_runner_multiple, 0.0)


class TestPrubeznehoPrepoctu(ZakladPrepoctu):
    """
    Průběžný přepočet čekajícího obchodu za otevřené burzy: každých tolik
    sekund se množství dopočítá znovu z živých kotací, příkaz v trhu se
    upraví na místě a runner se srovná s volbou ze zadání.
    """

    def odstup(self, flow, sekund: float) -> None:
        """Posune poslední přepočet obchodu o daný počet sekund do minulosti."""
        flow.last_refresh_at = datetime.now() - timedelta(seconds=sekund)

    def zlevni(self) -> None:
        """Opce zlevnila - stejná procenta prémie jsou menší ztráta a víc kontraktů."""
        self.ib.price_bid, self.ib.price_ask = 1.50, 1.55

    def zdrazi(self) -> None:
        """Opce zdražila - z rizika vyjde méně kontraktů."""
        self.ib.price_bid, self.ib.price_ask = 4.00, 4.10

    async def zaloz(self, **zmeny):
        """
        Vzorový obchod s PT 150 USD/ks (50 % z prémie 3,00), zadanými 2 ks,
        bez přepočtu po otevření, s průběžným přepočtem po 30 s a runnerem
        2× od tří kontraktů.
        """
        volby = dict(
            profit_target=150.0,
            quantity=2,
            refresh_after_open_sec=None,
            refresh_interval_sec=30.0,
            runner_multiple=2.0,
            runner_min_quantity=3,
        )
        volby.update(zmeny)
        return await self.zaloz_v_premii(**volby)

    async def test_prepocet_probehne_az_po_odstupu(self):
        flow = await self.zaloz()
        self.burza(600)
        self.zlevni()

        # Hned po založení se nepřepočítává - odstup se měří od založení
        await self.engine._tick()
        self.assertEqual(flow.quantity, 2)

        self.odstup(flow, 31)
        await self.engine._tick()

        self.assertGreater(flow.quantity, 2)
        self.assertEqual(
            flow.quantity, calc.suggest_quantity_for_loss(500.0, flow.stop_loss, 1, 100)
        )
        # Příkaz v trhu se upravil na místě - stejný příkaz, nové množství
        self.assertEqual(len(self.ib.placed), 1)
        self.assertEqual(self.ib.placed[0].order.totalQuantity, flow.quantity)
        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertLess((datetime.now() - flow.last_refresh_at).total_seconds(), 5)
        self.assertEqual(self.zaznamy("průběžně přepočteno"), 1)

    async def test_runner_se_zapne_az_mnozstvi_doroste_a_vypne_kdyz_klesne(self):
        flow = await self.zaloz()
        # Dva kontrakty jsou pod minimem tří - runner zatím ne
        self.assertFalse(flow.runner_active)
        self.burza(600)

        self.zlevni()
        self.odstup(flow, 31)
        await self.engine._tick()

        self.assertGreaterEqual(flow.quantity, 3)
        self.assertTrue(flow.runner_active)
        self.assertEqual(flow.runner_quantity, 1)
        self.assertAlmostEqual(flow.runner_profit_target, flow.scaled_target(2.0))
        self.assertAlmostEqual(flow.runner_stop_loss, flow.stop_loss)
        self.assertEqual(self.zaznamy("runner 1 ks zapnut po přepočtu"), 1)

        # Opce zdražila natolik, že z rizika vyjde jediný kontrakt
        self.zdrazi()
        self.odstup(flow, 31)
        await self.engine._tick()

        self.assertLess(flow.quantity, 3)
        self.assertFalse(flow.runner_active)
        self.assertEqual(self.zaznamy("runner po přepočtu vypnut"), 1)

    async def test_mimo_burzu_ani_pred_odstupem_se_neprepocitava(self):
        flow = await self.zaloz()
        self.zlevni()

        # Před otevřením burzy nejsou živé kotace
        self.burza(-600)
        self.odstup(flow, 31)
        await self.engine._tick()
        self.assertEqual(flow.quantity, 2)

        # Za otevřené burzy, ale odstup ještě neuplynul
        self.burza(600)
        self.odstup(flow, 10)
        await self.engine._tick()
        self.assertEqual(flow.quantity, 2)

        # Bez volby se nepřepočítává vůbec
        bez = await self.zaloz(refresh_interval_sec=None)
        self.odstup(bez, 31)
        await self.engine._tick()
        self.assertEqual(bez.quantity, 2)

    async def test_ceka_na_prepocet_po_otevreni(self):
        flow = await self.zaloz(refresh_after_open_sec=60)
        self.zlevni()

        # Prodleva po otevření ještě běží - průběžný přepočet ji respektuje
        self.burza(30)
        self.odstup(flow, 31)
        await self.engine._tick()
        self.assertEqual(flow.quantity, 2)
        self.assertFalse(flow.refresh_after_open_done)

        # Přepočet po otevření proběhne jako první a nastaví odstup
        self.burza(61)
        await self.engine._tick()
        self.assertTrue(flow.refresh_after_open_done)
        prepoctene_ks = flow.quantity
        self.assertGreater(prepoctene_ks, 2)
        self.assertLess((datetime.now() - flow.last_refresh_at).total_seconds(), 5)

        # Další změna kotací se projeví až po dalším odstupu
        self.zdrazi()
        self.burza(70)
        await self.engine._tick()
        self.assertEqual(flow.quantity, prepoctene_ks)
        self.odstup(flow, 31)
        await self.engine._tick()
        self.assertLess(flow.quantity, prepoctene_ks)

    async def test_beze_zmeny_mnozstvi_se_do_logu_nepise(self):
        flow = await self.zaloz()
        self.burza(600)
        self.zlevni()
        self.odstup(flow, 31)
        await self.engine._tick()
        self.assertEqual(self.zaznamy("průběžně přepočteno"), 1)

        # Druhý přepočet se stejnými kotacemi množství nemění - log mlčí
        self.odstup(flow, 31)
        await self.engine._tick()
        self.assertEqual(self.zaznamy("průběžně přepočteno"), 1)

    async def test_rucne_zapnuty_runner_prepocet_nevypne(self):
        flow = await self.zaloz(runner_multiple=0.0, quantity=4)
        await self.engine.set_runner(flow.id, 1.5)
        self.burza(600)
        self.zlevni()

        self.odstup(flow, 31)
        await self.engine._tick()

        # Runner zůstává na ručně zvoleném násobku, jen s cílem na nových úrovních
        self.assertTrue(flow.runner_active)
        self.assertAlmostEqual(flow.runner_profit_target, flow.scaled_target(1.5))

        # Ručně zrušený runner přepočet znovu nezapne
        await self.engine.cancel_runner(flow.id)
        self.odstup(flow, 31)
        await self.engine._tick()
        self.assertFalse(flow.runner_active)

    async def test_odlozeny_pokus_se_neopakuje_kazdym_pruchodem(self):
        # Bez kotací opce přepočet nemá z čeho vyjít; pokus se přesto počítá
        # do odstupu, ať se nezkouší při každém průchodu smyčkou
        flow = await self.zaloz()
        self.burza(600)
        self.ib.price_bid, self.ib.price_ask = None, None
        self.odstup(flow, 31)
        await self.engine._tick()
        self.assertEqual(flow.quantity, 2)
        self.assertLess((datetime.now() - flow.last_refresh_at).total_seconds(), 5)

        # Kotace se vrátily, odstup ale běží znovu
        self.zlevni()
        await self.engine._tick()
        self.assertEqual(flow.quantity, 2)

    async def test_zvyseni_v_pasmu_necitlivosti_se_neposila(self):
        # Pásmo 90 %: vyšší množství by muselo vyjít i z desetiny rizika -
        # to nevyjde, příkaz v trhu zůstává beze změny a do TWS nic nejde
        self.cfg.trading.refresh_increase_margin_pct = 90.0
        flow = await self.zaloz()
        self.burza(600)
        self.zlevni()
        self.odstup(flow, 31)
        await self.engine._tick()

        self.assertEqual(flow.quantity, 2)
        self.assertEqual(self.ib.placed[0].order.totalQuantity, 2)
        self.assertEqual(self.ib.oer.messages, 1)

    async def test_zvyseni_mnozstvi_respektuje_limit_oer(self):
        # Bez pásma, ale limit OER dovolí jen zadání a rezervu na zrušení
        self.cfg.trading.refresh_increase_margin_pct = 0.0
        self.omez_oer(2.0)
        flow = await self.zaloz()
        self.burza(600)
        self.zlevni()
        self.odstup(flow, 31)
        await self.engine._tick()

        self.assertEqual(flow.quantity, 2)
        self.assertEqual(self.ib.oer.messages, 1)
        self.assertEqual(self.zaznamy("zvýšení množství přepočtem odloženo"), 1)

    async def test_snizeni_mnozstvi_projde_i_nad_limitem_oer(self):
        # Snížení chrání riziko na obchod, proto se posílá vždy
        self.omez_oer(1.0)
        flow = await self.zaloz(quantity=5)
        self.burza(600)
        self.zdrazi()
        self.odstup(flow, 31)
        await self.engine._tick()

        self.assertLess(flow.quantity, 5)
        self.assertEqual(self.ib.placed[0].order.totalQuantity, flow.quantity)
        self.assertEqual(self.ib.oer.messages, 2)


class TestKonfiguraceRunneru(unittest.TestCase):
    """Meze procentuální velikosti runneru v konfiguraci."""

    def setUp(self) -> None:
        self.cfg = AppConfig()

    def test_vychozi_procento(self):
        self.assertEqual(self.cfg.trading.runner_quantity_pct, 25.0)

    def test_procento_mimo_rozsah_neprojde(self):
        # Nula i sto procent runner fakticky vypínají
        for hodnota in (0, 100, -5, 150):
            self.cfg.trading.runner_quantity_pct = hodnota
            with self.assertRaises(ValueError) as chyba:
                validate_config(self.cfg)
            self.assertIn("trading.runner_quantity_pct", str(chyba.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
