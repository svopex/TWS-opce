"""Testy načítání vstupních pozic ze souboru se zadáním obchodního dne."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fake_ib import FakeIBService
from tws_opce import importer
from tws_opce.config import AppConfig
from tws_opce.engine import FlowEngine
from tws_opce.import_dialog import ImportDialog, RadekPozice
from tws_opce.models import Flow, FlowState

# Zkrácená obdoba skutečného souboru: ke každému obchodu je starší varianta
# bez plusu (ta se má přeskočit) i „plus" položka, ze které se čerpá
SOUBOR = """
AMZN Long:
  trade_type: OptTradeV2
  symbol: AMZN
  entry_price: 266.4
  call_option_strike: 275

AMZN Long+:
  trade_type: OptTradeV3
  symbol: AMZN
  entry_price: 266.4
  target_price: 269.33
  exit_target_pct: 33
  call_option_strike: 275

SPY Short+:
  trade_type: OptTradeV3
  symbol: SPY
  entry_price: 762.04
  target_price: 758.15
  put_option_strike: 753
"""


class TestNacteniPozic(unittest.TestCase):
    """Výběr položek ze souboru a převod na pozice."""

    def test_cerpa_se_jen_z_plus_polozek(self):
        vysledek = importer.parse_positions(SOUBOR)
        self.assertEqual([p.key for p in vysledek.positions], ["AMZN Long+", "SPY Short+"])
        self.assertEqual(vysledek.warnings, [])

    def test_prebiraji_se_jen_tri_udaje(self):
        pozice = importer.parse_positions(SOUBOR).positions[0]
        self.assertEqual(pozice.symbol, "AMZN")
        self.assertAlmostEqual(pozice.entry_price, 266.4)
        self.assertAlmostEqual(pozice.target_price, 269.33)

    def test_smer_podle_polohy_cile_vuci_vstupu(self):
        amzn, spy = importer.parse_positions(SOUBOR).positions
        # Cíl nad vstupem = průraz nahoru = CALL
        self.assertEqual(amzn.right, "C")
        self.assertEqual(amzn.right_label, "LONG")
        # Cíl pod vstupem = průraz dolů = PUT
        self.assertEqual(spy.right, "P")
        self.assertEqual(spy.right_label, "SHORT")

    def test_ticker_se_prevadi_na_velka_pismena(self):
        vysledek = importer.parse_positions(
            "X+:\n  symbol: nflx\n  entry_price: 81.16\n  target_price: 82.46\n"
        )
        self.assertEqual(vysledek.positions[0].symbol, "NFLX")

    def test_klic_s_mezerou_pred_plusem_se_prijme(self):
        # Klíč se před testem přípony ořezává o okolní mezery
        vysledek = importer.parse_positions(
            '"NFLX Long+ ":\n  symbol: NFLX\n  entry_price: 81.16\n  target_price: 82.46\n'
        )
        self.assertEqual(len(vysledek.positions), 1)


class TestVadnePolozky(unittest.TestCase):
    """Vadná položka se přeskočí, zbytek souboru zůstává použitelný."""

    def test_chybejici_cilova_cena_polozku_preskoci(self):
        vysledek = importer.parse_positions(
            "A+:\n  symbol: AMZN\n  entry_price: 266.4\n"
            "B+:\n  symbol: SPY\n  entry_price: 762.04\n  target_price: 758.15\n"
        )
        self.assertEqual([p.symbol for p in vysledek.positions], ["SPY"])
        self.assertEqual(len(vysledek.warnings), 1)
        self.assertIn("target_price", vysledek.warnings[0])

    def test_chybejici_ticker_polozku_preskoci(self):
        vysledek = importer.parse_positions(
            "A+:\n  entry_price: 266.4\n  target_price: 269.33\n"
            "B+:\n  symbol: SPY\n  entry_price: 762.04\n  target_price: 758.15\n"
        )
        self.assertEqual([p.symbol for p in vysledek.positions], ["SPY"])
        self.assertIn("symbol", vysledek.warnings[0])

    def test_necislena_cena_polozku_preskoci(self):
        vysledek = importer.parse_positions(
            "A+:\n  symbol: AMZN\n  entry_price: nevim\n  target_price: 269.33\n"
        )
        self.assertEqual(vysledek.positions, [])
        self.assertIn("entry_price", vysledek.warnings[0])

    def test_zaporna_cena_polozku_preskoci(self):
        vysledek = importer.parse_positions(
            "A+:\n  symbol: AMZN\n  entry_price: -5\n  target_price: 269.33\n"
        )
        self.assertEqual(vysledek.positions, [])
        self.assertIn("kladné číslo", vysledek.warnings[0])

    def test_cil_shodny_se_vstupem_polozku_preskoci(self):
        # Nulová dráha ke cíli neurčuje směr obchodu ani PT
        vysledek = importer.parse_positions(
            "A+:\n  symbol: AMZN\n  entry_price: 266.4\n  target_price: 266.4\n"
        )
        self.assertEqual(vysledek.positions, [])
        self.assertIn("směr obchodu", vysledek.warnings[0])

    def test_polozka_bez_hodnot_se_preskoci(self):
        vysledek = importer.parse_positions("A+:\nB+:\n  symbol: SPY\n"
                                            "  entry_price: 762.04\n  target_price: 758.15\n")
        self.assertEqual([p.symbol for p in vysledek.positions], ["SPY"])
        self.assertIn("slovník", vysledek.warnings[0])


class TestNecitelnySoubor(unittest.TestCase):
    """Soubor, ze kterého nelze načíst vůbec nic, se odmítne s hláškou."""

    def test_prazdny_soubor(self):
        with self.assertRaises(ValueError) as chyba:
            importer.parse_positions("")
        self.assertIn("prázdný", str(chyba.exception))

    def test_soubor_bez_plus_polozek(self):
        with self.assertRaises(ValueError) as chyba:
            importer.parse_positions("AMZN Long:\n  symbol: AMZN\n  entry_price: 266.4\n")
        self.assertIn("plusem", str(chyba.exception))

    def test_soubor_neni_slovnik(self):
        with self.assertRaises(ValueError) as chyba:
            importer.parse_positions("- AMZN\n- SPY\n")
        self.assertIn("slovník", str(chyba.exception))

    def test_vadne_yaml(self):
        with self.assertRaises(ValueError) as chyba:
            importer.parse_positions("A+:\n  symbol: [nedokoncene\n")
        self.assertIn("YAML", str(chyba.exception))


class TestCilZProcent(unittest.TestCase):
    """PT jako podíl dráhy ze vstupu k cílové ceně ze souboru."""

    def test_sto_procent_je_presne_cilova_cena(self):
        self.assertAlmostEqual(importer.profit_target_from_pct(266.4, 269.33, 100.0), 269.33)

    def test_polovicni_drahu_pro_long(self):
        self.assertAlmostEqual(importer.profit_target_from_pct(266.4, 269.33, 50.0), 267.87)

    def test_polovicni_drahu_pro_short(self):
        # U shortu je cíl pod vstupem, znaménko rozdílu to řeší samo
        self.assertAlmostEqual(importer.profit_target_from_pct(762.04, 758.15, 50.0), 760.10)

    def test_procenta_nad_sto_cil_prodlouzi(self):
        # 266,40 + 1,5 x 2,93 = 270,795, zaokrouhleno na centy
        self.assertAlmostEqual(importer.profit_target_from_pct(266.4, 269.33, 150.0), 270.79)

    def test_vysledek_je_zaokrouhlen_na_centy(self):
        self.assertAlmostEqual(importer.profit_target_from_pct(266.4, 269.33, 60.0), 268.16)

    def test_nekladne_procento_se_odmitne(self):
        with self.assertRaises(ValueError):
            importer.profit_target_from_pct(266.4, 269.33, 0.0)
        with self.assertRaises(ValueError):
            importer.profit_target_from_pct(266.4, 269.33, -10.0)


class TestCilZPremie(unittest.TestCase):
    """PT (resp. SL) na opci zadaný podílem ze zaplacené prémie."""

    def test_procento_z_premie_je_cena_opce_krat_procento(self):
        # Prémie kontraktu = cena opce x 100, takže 1 % prémie = cena opce v USD
        self.assertAlmostEqual(importer.profit_target_from_premium_pct(30.0, 3.00), 90.0)

    def test_desetina_z_opce_za_sto_dolaru(self):
        # Opce za 1,00 stojí 100 USD za kontrakt, 10 % z ní je 10 USD
        self.assertAlmostEqual(importer.profit_target_from_premium_pct(10.0, 1.00), 10.0)

    def test_stejne_procento_skaluje_s_cenou_opce(self):
        # Táž volba dá u levné opce úměrně menší a u drahé úměrně větší částku
        self.assertAlmostEqual(importer.profit_target_from_premium_pct(10.0, 0.50), 5.0)
        self.assertAlmostEqual(importer.profit_target_from_premium_pct(10.0, 2.00), 20.0)

    def test_sto_procent_je_cela_premie(self):
        self.assertAlmostEqual(importer.profit_target_from_premium_pct(100.0, 2.50), 250.0)

    def test_vysledek_je_zaokrouhlen_na_centy(self):
        self.assertAlmostEqual(importer.profit_target_from_premium_pct(33.0, 1.37), 45.21)

    def test_nekladne_procento_se_odmitne(self):
        with self.assertRaises(ValueError):
            importer.profit_target_from_premium_pct(0.0, 3.00)
        with self.assertRaises(ValueError):
            importer.profit_target_from_premium_pct(-5.0, 3.00)

    def test_nekladna_cena_opce_se_odmitne(self):
        with self.assertRaises(ValueError):
            importer.profit_target_from_premium_pct(30.0, 0.0)


class TestSkutecnySoubor(unittest.TestCase):
    """Načtení souboru dodaného s aplikací, pokud je k dispozici."""

    def test_soubor_obchodniho_dne(self):
        cesta = Path(__file__).resolve().parent.parent / "2026-08-25.yaml"
        if not cesta.exists():
            self.skipTest("vzorový soubor se zadáním dne není k dispozici")

        vysledek = importer.parse_positions(cesta.read_text(encoding="utf-8"))
        self.assertEqual(
            [p.symbol for p in vysledek.positions], ["AMZN", "NFLX", "SPY", "TSLA"]
        )
        self.assertEqual(vysledek.warnings, [])
        # Směry vycházejí z polohy cíle vůči vstupu
        self.assertEqual([p.right for p in vysledek.positions], ["C", "C", "P", "P"])


class TestZamekRadku(unittest.TestCase):
    """
    Kdy lze řádek importního dialogu zadat znovu (přepsat obchod).

    Zámek drží jedině obchod s otevřenou pozicí - čekající na vstup engine
    při novém zadání sám nahradí a ukončený už nepřekáží.
    """

    def setUp(self) -> None:
        cfg = AppConfig()
        cfg.state.enabled = False
        self.engine = FlowEngine(cfg, FakeIBService(cfg))
        # Dialog se nevykresluje - zámek se ptá jen na engine a id obchodu
        self.dialog = ImportDialog(cfg, self.engine, self.engine.ib, None)
        self.pozice = importer.ImportedPosition(
            key="AMZN Long+", symbol="AMZN", entry_price=266.4, target_price=269.33
        )

    def radek(self, stav: FlowState | None) -> RadekPozice:
        """Řádek se založeným obchodem v daném stavu; None = obchod v přehledu není."""
        radek = RadekPozice(pozice=self.pozice, flow_id="AMZN-1")
        if stav is not None:
            self.engine.flows["AMZN-1"] = Flow(
                id="AMZN-1",
                symbol="AMZN",
                entry_price=266.4,
                profit_target=269.33,
                stop_loss=265.0,
                quantity=2,
                max_spread_pct=5.0,
                state=stav,
            )
        return radek

    def test_nezadany_radek_neni_zamceny(self):
        self.assertFalse(self.dialog._zamceno(RadekPozice(pozice=self.pozice)))

    def test_obchod_cekajici_na_vstup_lze_prepsat(self):
        # Tyhle stavy engine při novém zadání sám zruší a nahradí
        for stav in (
            FlowState.NEW,
            FlowState.ARMED,
            FlowState.SPREAD_BLOCKED,
            FlowState.NO_QUOTES,
        ):
            with self.subTest(stav=stav):
                self.assertFalse(self.dialog._zamceno(self.radek(stav)))

    def test_obchod_s_pozici_je_zamceny(self):
        for stav in (FlowState.FILLED, FlowState.EXIT_ARMED, FlowState.CLOSING):
            with self.subTest(stav=stav):
                self.assertTrue(self.dialog._zamceno(self.radek(stav)))

    def test_ukonceny_obchod_uz_neprekazi(self):
        for stav in (
            FlowState.CLOSED,
            FlowState.CANCELLED,
            FlowState.MISSED,
            FlowState.ERROR,
        ):
            with self.subTest(stav=stav):
                self.assertFalse(self.dialog._zamceno(self.radek(stav)))

    def test_obchod_smazany_z_prehledu_radek_odemkne(self):
        # Přesně situace po tlačítku „Zrušit a smazat vše"
        radek = self.radek(FlowState.EXIT_ARMED)
        self.assertTrue(self.dialog._zamceno(radek))
        self.engine.flows.clear()
        self.assertFalse(self.dialog._zamceno(radek))

    def test_popis_stavu_sleduje_obchod(self):
        radek = self.radek(FlowState.EXIT_ARMED)
        text, trida = self.dialog._stav_zadaneho(radek)
        self.assertIn("Nakoupeno – výstup aktivní", text)
        self.assertEqual(trida, "stav-import-ok")

    def test_ukonceny_obchod_pozve_k_novemu_zadani(self):
        radek = self.radek(FlowState.CLOSED)
        text, trida = self.dialog._stav_zadaneho(radek)
        self.assertIn("lze zadat znovu", text)
        self.assertEqual(trida, "stav-import-varovani")

    def test_smazany_obchod_se_pozna_z_popisu(self):
        radek = self.radek(None)
        text, _ = self.dialog._stav_zadaneho(radek)
        self.assertIn("už není v přehledu", text)

    def test_poznamka_o_nezapnutem_runneru_prezije_obnovu(self):
        radek = self.radek(FlowState.ARMED)
        radek.poznamka = "runner nezapnut: málo kontraktů"
        text, trida = self.dialog._stav_zadaneho(radek)
        self.assertIn("runner nezapnut", text)
        self.assertEqual(trida, "stav-import-varovani")


if __name__ == "__main__":
    unittest.main()
