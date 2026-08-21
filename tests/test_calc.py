"""Testy výpočetních funkcí obchodní logiky."""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tws_opce import calc


class TestSmerObchodu(unittest.TestCase):
    """Určení typu opce a dopočet SL."""

    def test_vstup_nad_aktualni_cenou_je_call(self):
        # Čeká se průraz nahoru, kupuje se CALL
        self.assertEqual(calc.determine_right(100.0, 105.0), "C")

    def test_vstup_pod_aktualni_cenou_je_put(self):
        # Čeká se průraz dolů, kupuje se PUT
        self.assertEqual(calc.determine_right(100.0, 95.0), "P")

    def test_sl_pro_call_lezi_pod_vstupem(self):
        self.assertAlmostEqual(calc.default_stop_loss(105.0, 110.0, 1.0), 100.0)

    def test_sl_pro_put_lezi_nad_vstupem(self):
        self.assertAlmostEqual(calc.default_stop_loss(95.0, 90.0, 1.0), 100.0)

    def test_pomer_sl_vuci_pt_se_uplatni(self):
        # Poměr 0,5 znamená poloviční vzdálenost SL oproti PT
        self.assertAlmostEqual(calc.default_stop_loss(100.0, 110.0, 0.5), 95.0)

    def test_smery_podminek(self):
        # CALL: vstup nahoru, PT nahoru, SL dolů
        self.assertEqual(calc.condition_directions("C"), (True, True, False))
        # PUT: vstup dolů, PT dolů, SL nahoru
        self.assertEqual(calc.condition_directions("P"), (False, False, True))


class TestSpread(unittest.TestCase):
    """Výpočet spreadu ze středu trhu."""

    def test_spread_ze_stredu_trhu(self):
        # (8,85 − 7,75) / 8,30 * 100
        self.assertAlmostEqual(calc.spread_pct(7.75, 8.85), 13.253012, places=5)

    def test_uzky_spread(self):
        self.assertAlmostEqual(calc.spread_pct(3.00, 3.10), 3.278688, places=5)

    def test_chybejici_kotace_vraci_none(self):
        self.assertIsNone(calc.spread_pct(None, 3.10))
        self.assertIsNone(calc.spread_pct(0.0, 3.10))
        self.assertIsNone(calc.spread_pct(float("nan"), 3.10))

    def test_prohozene_kotace_vraci_none(self):
        # ASK nižší než BID je nesmyslná kotace
        self.assertIsNone(calc.spread_pct(3.10, 3.00))


class TestMnozstvi(unittest.TestCase):
    """Doporučené množství kontraktů podle rizika."""

    def test_vypocet_podle_delty(self):
        # Riziko 500 USD, pohyb 5 USD, delta 0,5 -> ztráta 250 USD na kontrakt -> 2 ks
        self.assertEqual(calc.suggest_quantity(500.0, 105.0, 100.0, 0.5), 2)

    def test_zaokrouhluje_se_dolu(self):
        # 50 / (1 * 0,3 * 100) = 1,67 -> 1 ks
        self.assertEqual(calc.suggest_quantity(50.0, 105.0, 104.0, 0.3), 1)

    def test_zaporna_delta_putu_se_bere_v_absolutni_hodnote(self):
        self.assertEqual(calc.suggest_quantity(500.0, 105.0, 100.0, -0.5), 2)

    def test_horni_mez_se_dodrzi(self):
        self.assertEqual(calc.suggest_quantity(1_000_000.0, 105.0, 100.0, 0.5, 1, 10), 10)

    def test_nulovy_pohyb_vraci_minimum(self):
        self.assertEqual(calc.suggest_quantity(500.0, 100.0, 100.0, 0.5), 1)


class TestLimitniCeny(unittest.TestCase):
    """Limitní ceny podle typu příkazu."""

    def test_limit_na_ask_s_toleranci(self):
        self.assertAlmostEqual(calc.entry_limit_price("LMT_ASK", 3.00, 3.10, 2.0), 3.162)

    def test_limit_na_mid(self):
        self.assertAlmostEqual(calc.entry_limit_price("LMT_MID", 3.00, 3.10, 2.0), 3.05)

    def test_trzni_prikaz_nema_limit(self):
        self.assertIsNone(calc.entry_limit_price("MKT", 3.00, 3.10, 2.0))

    def test_vystupni_limit_pod_bid(self):
        self.assertAlmostEqual(calc.exit_limit_price(3.00, 2.0), 2.94)

    def test_zaokrouhleni_na_tik(self):
        self.assertAlmostEqual(calc.round_to_tick(3.162, 0.05), 3.15)
        self.assertAlmostEqual(calc.round_to_tick(8.834, 0.05), 8.85)
        self.assertAlmostEqual(calc.round_to_tick(1.237, 0.01), 1.24)


class TestVyberKontraktu(unittest.TestCase):
    """Výběr strike ceny a expirace."""

    def test_nejblizsi_strike(self):
        self.assertEqual(calc.nearest_strike([225.0, 230.0, 235.0], 232.0), 230.0)
        self.assertEqual(calc.nearest_strike([225.0, 230.0, 235.0], 233.0), 235.0)

    def test_prazdny_seznam_strike(self):
        self.assertIsNone(calc.nearest_strike([], 100.0))

    def test_nejblizsi_expirace(self):
        dnes = date(2026, 8, 18)
        vyber = calc.select_expiration(["20260820", "20260828", "20260918"], "nearest", 0, today=dnes)
        self.assertEqual(vyber, "20260820")

    def test_minimalni_pocet_dni_preskoci_blizkou_expiraci(self):
        dnes = date(2026, 8, 18)
        vyber = calc.select_expiration(["20260820", "20260828", "20260918"], "nearest", 5, today=dnes)
        self.assertEqual(vyber, "20260828")

    def test_pri_nesplneni_min_dte_se_vezme_nejvzdalenejsi(self):
        dnes = date(2026, 8, 18)
        vyber = calc.select_expiration(["20260820", "20260821"], "nearest", 30, today=dnes)
        self.assertEqual(vyber, "20260821")

    def test_probehla_expirace_se_preskoci(self):
        dnes = date(2026, 8, 18)
        vyber = calc.select_expiration(["20260101", "20260828"], "nearest", 0, today=dnes)
        self.assertEqual(vyber, "20260828")

    def test_pevne_datum(self):
        vyber = calc.select_expiration(["20260820", "20260828"], "fixed", 0, "20260828")
        self.assertEqual(vyber, "20260828")

    def test_pevne_datum_mimo_nabidku(self):
        self.assertIsNone(calc.select_expiration(["20260820"], "fixed", 0, "20991231"))


class TestOdhadDelty(unittest.TestCase):
    """Dopočet delty z tržní ceny opce (náhrada za chybějící model greeks z TWS)."""

    DNES = date(2026, 8, 18)

    def test_delta_odpovida_hodnote_z_tws(self):
        # Skutečná data: AAPL 306,6 / strike 312,5 / cena 0,34 / expirace za den.
        # TWS pro tento kontrakt hlásila deltu 0,123.
        delta = calc.estimate_delta(0.34, 306.6, 312.5, "20260819", 4.0, "C", self.DNES)
        self.assertIsNotNone(delta)
        self.assertAlmostEqual(delta, 0.123, delta=0.03)

    def test_delta_call_roste_se_zanorenim_do_penez(self):
        # Cena 6,90 u strike 300 znamená vnitřní hodnotu 6,6 a malou časovou složku
        mimo = calc.estimate_delta(0.34, 306.6, 312.5, "20260819", 4.0, "C", self.DNES)
        v_penezich = calc.estimate_delta(6.90, 306.6, 300.0, "20260819", 4.0, "C", self.DNES)
        self.assertLess(mimo, v_penezich)
        # Opce v penězích krátce před expirací má deltu blízko jedné
        self.assertGreater(v_penezich, 0.8)

    def test_vyssi_cena_opce_znamena_vyssi_volatilitu(self):
        # Model musí zůstat konzistentní: dražší opce = vyšší implikovaná volatilita
        roky = calc.years_to_expiry("20260819", self.DNES)
        levna = calc.implied_volatility(6.75, 306.6, 300.0, roky, 0.04, "C")
        draha = calc.implied_volatility(8.50, 306.6, 300.0, roky, 0.04, "C")
        self.assertLess(levna, draha)

    def test_delta_putu_je_zaporna(self):
        delta = calc.estimate_delta(2.50, 306.82, 303.0, "20260819", 4.0, "P", self.DNES)
        self.assertIsNotNone(delta)
        self.assertLess(delta, 0)
        self.assertGreater(delta, -1)

    def test_delta_je_vzdy_v_platnem_rozsahu(self):
        for strike in (280.0, 300.0, 306.0, 312.5, 340.0):
            for right in ("C", "P"):
                cena = calc.black_scholes_price(306.6, strike, 0.05, 0.04, 0.35, right)
                delta = calc.estimate_delta(cena, 306.6, strike, "20260918", 4.0, right, self.DNES)
                self.assertIsNotNone(delta, f"strike {strike} {right}")
                self.assertTrue(-1.0 <= delta <= 1.0, f"strike {strike} {right}: {delta}")

    def test_zpetny_prevod_ceny_a_volatility(self):
        # Z ceny spočítaná volatilita musí vést zpět na stejnou cenu
        cena = calc.black_scholes_price(306.6, 310.0, 0.1, 0.04, 0.28, "C")
        sigma = calc.implied_volatility(cena, 306.6, 310.0, 0.1, 0.04, "C")
        self.assertAlmostEqual(sigma, 0.28, places=4)

    def test_cena_pod_vnitrni_hodnotou_nelze_resit(self):
        # Cena nižší než vnitřní hodnota je pro model neřešitelná
        self.assertIsNone(calc.estimate_delta(1.0, 320.0, 300.0, "20260918", 4.0, "C", self.DNES))

    def test_chybejici_vstupy_vraci_none(self):
        self.assertIsNone(calc.estimate_delta(None, 306.6, 310.0, "20260819", 4.0, "C", self.DNES))
        self.assertIsNone(calc.estimate_delta(0.34, None, 310.0, "20260819", 4.0, "C", self.DNES))
        self.assertIsNone(calc.estimate_delta(0.0, 306.6, 310.0, "20260819", 4.0, "C", self.DNES))

    def test_dnesni_expirace_nedeli_nulou(self):
        delta = calc.estimate_delta(0.20, 306.6, 308.0, "20260818", 4.0, "C", self.DNES)
        self.assertIsNotNone(delta)
        self.assertTrue(0.0 <= delta <= 1.0)


class TestPreceneniOpce(unittest.TestCase):
    """Odhad ceny opce při dosažení cílové úrovně podkladu."""

    DNES = date(2026, 8, 18)

    def test_call_pri_rustu_podkladu_zdrazi(self):
        # CALL za 1,01 při podkladu 310, strike 312,5
        na_pt = calc.project_option_price(1.01, 310.0, 313.0, 312.5, "20260819", 4.0, "C", self.DNES)
        self.assertIsNotNone(na_pt)
        self.assertGreater(na_pt, 1.01)

    def test_call_pri_poklesu_podkladu_zlevni(self):
        na_sl = calc.project_option_price(1.01, 310.0, 309.0, 312.5, "20260819", 4.0, "C", self.DNES)
        self.assertIsNotNone(na_sl)
        self.assertLess(na_sl, 1.01)
        self.assertGreaterEqual(na_sl, 0.0)

    def test_put_se_chova_zrcadlove(self):
        pri_poklesu = calc.project_option_price(
            2.50, 545.0, 540.0, 542.5, "20260819", 4.0, "P", self.DNES
        )
        pri_rustu = calc.project_option_price(
            2.50, 545.0, 550.0, 542.5, "20260819", 4.0, "P", self.DNES
        )
        self.assertGreater(pri_poklesu, 2.50)
        self.assertLess(pri_rustu, 2.50)

    def test_cil_na_aktualni_cene_vraci_stejnou_cenu(self):
        # Bez pohybu podkladu musí přecenění vrátit původní cenu
        cena = calc.project_option_price(1.01, 310.0, 310.0, 312.5, "20260819", 4.0, "C", self.DNES)
        self.assertAlmostEqual(cena, 1.01, places=2)

    def test_bez_pouzitelne_ceny_vraci_none(self):
        self.assertIsNone(
            calc.project_option_price(0.0, 310.0, 313.0, 312.5, "20260819", 4.0, "C", self.DNES)
        )
        self.assertIsNone(
            calc.project_option_price(1.01, None, 313.0, 312.5, "20260819", 4.0, "C", self.DNES)
        )


class TestSmysluplnostiUrovni(unittest.TestCase):
    """Kontrola, že cíl a stop leží v rozumné vzdálenosti od vstupu."""

    def test_bezny_obchod_projde(self):
        self.assertTrue(calc.levels_sane(311.5, 313.5, 309.5))
        self.assertTrue(calc.levels_sane(545.0, 543.0, 547.0))

    def test_cil_v_milionech_neprojde(self):
        # Následek dřívější chyby, kdy se cíl opakovaným násobením vyšplhal
        self.assertFalse(calc.levels_sane(311.5, 51_898_915.0, 309.5))

    def test_zaporne_nebo_nulove_urovne_neprojdou(self):
        self.assertFalse(calc.levels_sane(311.5, 0.0, 309.5))
        self.assertFalse(calc.levels_sane(311.5, 313.5, -5.0))
        self.assertFalse(calc.levels_sane(0.0, 313.5, 309.5))

    def test_hranice_vzdalenosti(self):
        # Vzdálenost do 50 % vstupu je přijatelná, nad ní už ne
        self.assertTrue(calc.levels_sane(100.0, 145.0, 95.0))
        self.assertFalse(calc.levels_sane(100.0, 155.0, 95.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestUrovneNaOpci(unittest.TestCase):
    """Výpočty pro PT a SL zadané ziskem / ztrátou v USD na kontrakt."""

    def test_limit_pro_zisk_na_kontrakt(self):
        # 10 USD na kontraktu o 100 kusech = posun ceny opce o 0,10
        self.assertAlmostEqual(calc.option_profit_limit(3.00, 10.0, 0.01), 3.10)

    def test_limit_se_zaokrouhli_na_tik(self):
        self.assertAlmostEqual(calc.option_profit_limit(3.00, 7.0, 0.05), 3.05)

    def test_stop_pro_ztratu_na_kontrakt(self):
        self.assertAlmostEqual(calc.option_loss_stop(3.00, 10.0, 0.01), 2.90)

    def test_stop_neklesne_pod_jeden_tik(self):
        # Ztráta větší než celá prémie - stop stojí na nejnižší možné ceně
        self.assertAlmostEqual(calc.option_loss_stop(3.00, 500.0, 0.05), 0.05)

    def test_mnozstvi_ze_ztraty_na_kontrakt(self):
        self.assertEqual(calc.suggest_quantity_for_loss(50.0, 10.0), 5)
        self.assertEqual(calc.suggest_quantity_for_loss(50.0, 12.0), 4)

    def test_mnozstvi_ze_ztraty_dodrzi_meze(self):
        self.assertEqual(calc.suggest_quantity_for_loss(50.0, 1000.0), 1)
        self.assertEqual(calc.suggest_quantity_for_loss(50.0, 0.0), 1)
        self.assertEqual(calc.suggest_quantity_for_loss(5000.0, 1.0, max_quantity=100), 100)

    def test_mnozstvi_pres_deltu_odpovida_obecnemu_vzorci(self):
        # Pohyb 3 body * delta 0,5 * 100 = 150 USD na kontrakt
        self.assertEqual(
            calc.suggest_quantity(600.0, 232.0, 229.0, 0.5),
            calc.suggest_quantity_for_loss(600.0, 150.0),
        )

    def test_inverze_ceny_opce_vrati_puvodni_podklad(self):
        # Cena opce při podkladu S* a zpět musí dát S*
        years, rate, sigma = 30 / 365, 0.04, 0.3
        cena = calc.black_scholes_price(236.0, 235.0, years, rate, sigma, "C")
        uroven = calc.level_for_option_price(cena, 230.0, 235.0, years, rate, sigma, "C")
        self.assertAlmostEqual(uroven, 236.0, places=4)

    def test_inverze_pro_put_klesa(self):
        years, rate, sigma = 30 / 365, 0.04, 0.3
        cena = calc.black_scholes_price(224.0, 226.0, years, rate, sigma, "P")
        uroven = calc.level_for_option_price(cena, 230.0, 226.0, years, rate, sigma, "P")
        self.assertAlmostEqual(uroven, 224.0, places=4)

    def test_nedosazitelna_cena_vraci_none(self):
        years, rate, sigma = 30 / 365, 0.04, 0.3
        self.assertIsNone(calc.level_for_option_price(10000.0, 230.0, 235.0, years, rate, sigma, "C"))
        self.assertIsNone(calc.level_for_option_price(0.0, 230.0, 235.0, years, rate, sigma, "C"))

    def test_projekce_urovne_podkladu_je_inverzi_projekce_ceny(self):
        expirace = (date.today() + timedelta(days=30)).strftime("%Y%m%d")
        cena_na_cili = calc.project_option_price(3.0, 230.0, 234.0, 232.5, expirace, 4.0, "C")
        uroven = calc.project_underlying_level(3.0, 230.0, cena_na_cili, 232.5, expirace, 4.0, "C")
        self.assertAlmostEqual(uroven, 234.0, places=3)

    def test_smer_z_pt_na_podkladu(self):
        self.assertEqual(calc.intended_right(232.0, 235.0, None), "C")
        self.assertEqual(calc.intended_right(232.0, 229.0, None), "P")

    def test_smer_ze_sl_kdyz_pt_je_na_opci(self):
        self.assertEqual(calc.intended_right(232.0, 10.0, 229.0, pt_on_underlying=False), "C")
        self.assertEqual(calc.intended_right(232.0, 10.0, 235.0, pt_on_underlying=False), "P")

    def test_smer_bez_urovni_na_podkladu_nelze_urcit(self):
        self.assertIsNone(calc.intended_right(232.0, 10.0, 10.0, False, False))
        self.assertIsNone(calc.intended_right(232.0, 10.0, None, False, True))

    def test_kontrola_urovni_na_opci(self):
        # Částka v USD nemá ke vstupu vztah - stačí, že je kladná
        self.assertTrue(calc.levels_sane(232.0, 10.0, 10.0, pt_on_underlying=False, sl_on_underlying=False))
        self.assertFalse(calc.levels_sane(232.0, 0.0, 10.0, pt_on_underlying=False, sl_on_underlying=False))
        # Úroveň na podkladu se kontroluje dál
        self.assertFalse(calc.levels_sane(232.0, 10.0, 9999.0, pt_on_underlying=False, sl_on_underlying=True))
