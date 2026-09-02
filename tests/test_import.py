"""Testy načítání vstupních pozic ze souboru se zadáním obchodního dne."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fake_ib import FakeIBService
from tws_opce import import_dialog, importer
from tws_opce.config import AppConfig
from tws_opce.engine import FlowEngine
from tws_opce.import_dialog import (
    REZIM_PCT,
    REZIM_PREMIUM,
    REZIM_USD,
    RUNNER_VYPNUTO,
    ImportDialog,
    RadekPozice,
    uroven_cile,
)
from tws_opce.importer import ImportedPosition
from tws_opce.models import (
    MODE_PREMIUM,
    MODE_UNDERLYING,
    MODE_USD,
    Flow,
    FlowRequest,
    FlowState,
    urovne_z_rezimu,
)


class Zaskrtavatko:
    """Náhrada zaškrtávátka - test nepotřebuje vykreslené rozhraní."""

    def __init__(self, value: bool) -> None:
        self.value = value
        self.enabled = True

    def set_value(self, value: bool) -> None:
        self.value = value

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled


class Pole:
    """Náhrada číselného pole řádku (PT, SL, množství) i pole nad tabulkou."""

    def __init__(self, value: float | str | None = None) -> None:
        self.value = value

    def set_value(self, value: float | str | None) -> None:
        self.value = value


class Prepinac:
    """Náhrada přepínače režimu cíle nad tabulkou."""

    def __init__(self, value: str) -> None:
        self.value = value

    def set_value(self, value: str) -> None:
        self.value = value


class Popisek:
    """
    Náhrada popisku - souhrnu pod tabulkou i sloupce Stav v řádku.
    Barvy ani bublinu test nesleduje, jen si je nechá spolknout.
    """

    def __init__(self) -> None:
        self.text = ""
        self.visible = True

    def set_text(self, text: str) -> None:
        self.text = text

    def set_visibility(self, visible: bool) -> None:
        self.visible = visible

    def classes(self, **kwargs: object) -> "Popisek":
        return self

    def tooltip(self, text: str) -> "Popisek":
        return self


class Okno:
    """Náhrada okna dialogu - _zadej je po úspěšné dávce zavírá."""

    def __init__(self) -> None:
        self.value = True

    def close(self) -> None:
        self.value = False


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


class TestRezimuUrovni(unittest.TestCase):
    """
    Volba cíle nad tabulkou určuje režim PT i SL - obě úrovně mají vyjít
    ve stejné jednotce, aby se obchod vrátil do formuláře souhlasně.
    """

    def test_procento_drahy_je_uroven_na_podkladu(self):
        self.assertEqual(uroven_cile(REZIM_PCT), MODE_UNDERLYING)

    def test_usd_na_opci_je_uroven_v_usd(self):
        self.assertEqual(uroven_cile(REZIM_USD), MODE_USD)

    def test_procento_premie_je_uroven_v_procentech(self):
        self.assertEqual(uroven_cile(REZIM_PREMIUM), MODE_PREMIUM)

    def test_neznamy_rezim_neprojde_tise(self):
        # Tiché uhnutí k výchozí hodnotě by z úrovně na opci udělalo cenu
        # podkladu a projevilo by se až špatně zadaným obchodem
        with self.assertRaises(KeyError):
            uroven_cile("procenta")

    def test_jedna_volba_urcuje_pt_i_sl(self):
        # Rozbalení režimu na příznaky zadání smí žít jen na jednom místě,
        # jinak se dá polovina dvojice snadno vynechat
        for rezim in (MODE_UNDERLYING, MODE_USD, MODE_PREMIUM):
            with self.subTest(rezim=rezim):
                urovne = urovne_z_rezimu(rezim, premie=3.00)
                self.assertEqual(
                    urovne["pt_on_underlying"], urovne["sl_on_underlying"]
                )
                self.assertEqual(urovne["pt_in_premium"], urovne["sl_in_premium"])

    def test_procenta_bez_ceny_opce_zustanou_v_usd(self):
        # Ručně přepsané PT prémii zahazuje - bez ní se procenta nedají
        # převést zpět, takže obchod nese úroveň jako částku v USD
        urovne = urovne_z_rezimu(MODE_PREMIUM, premie=None)
        self.assertFalse(urovne["pt_in_premium"])
        self.assertFalse(urovne["sl_in_premium"])
        self.assertIsNone(urovne["premium_base"])


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

    def test_otevreni_zaskrtne_vse_krome_zamcenych(self):
        # Formulář se otevírá s nabídkou zadat vše, co zadat lze
        volny = self.radek(FlowState.ARMED)
        volny.vybrano = Zaskrtavatko(False)
        volny.qty_input = Pole(2)
        volny.rezim_hodnot = REZIM_PCT
        zamceny = RadekPozice(pozice=self.pozice, flow_id="TSLA-1")
        zamceny.vybrano = Zaskrtavatko(True)
        zamceny.qty_input = Pole(3)
        zamceny.rezim_hodnot = REZIM_PCT
        self.engine.flows["TSLA-1"] = Flow(
            id="TSLA-1",
            symbol="TSLA",
            entry_price=346.9,
            profit_target=339.27,
            stop_loss=350.0,
            quantity=2,
            max_spread_pct=5.0,
            state=FlowState.EXIT_ARMED,
        )
        self.dialog.radky = [volny, zamceny]
        self.dialog.rezim = Prepinac(REZIM_PCT)
        self.dialog.souhrn_label = Popisek()
        self.dialog.engine._live_account_size = 6000.0

        self.dialog._vyber_vse()
        self.assertTrue(volny.vybrano.value)
        self.assertFalse(zamceny.vybrano.value)

    def test_otevreni_nezaskrtne_radek_z_jineho_rezimu(self):
        # Řádek zamčený v okamžiku přepnutí režimu si podržel úrovně
        # v původní jednotce; po odemčení se nesmí sám nabídnout k zadání
        radek = self.radek(FlowState.CLOSED)
        radek.vybrano = Zaskrtavatko(False)
        radek.qty_input = Pole(2)
        radek.rezim_hodnot = REZIM_PREMIUM
        self.dialog.radky = [radek]
        self.dialog.rezim = Prepinac(REZIM_PCT)
        self.dialog.souhrn_label = Popisek()
        self.dialog.engine._live_account_size = 6000.0

        self.dialog._vyber_vse()
        self.assertFalse(radek.vybrano.value)

    def test_poznamka_o_nezapnutem_runneru_prezije_obnovu(self):
        radek = self.radek(FlowState.ARMED)
        radek.poznamka = "runner nezapnut: málo kontraktů"
        text, trida = self.dialog._stav_zadaneho(radek)
        self.assertIn("runner nezapnut", text)
        self.assertEqual(trida, "stav-import-varovani")


class TestZadaniDoTrhu(unittest.IsolatedAsyncioTestCase):
    """
    Co dialog skutečně pošle do enginu po stisku „Zadat vybrané pozice".

    Testuje se sestavené FlowRequest, ne jen pomocná funkce nad režimy -
    vynechání poloviny dvojice příznaků (třeba sl_in_premium) by se jinak
    v testech vůbec neprojevilo.
    """

    def setUp(self) -> None:
        cfg = AppConfig()
        cfg.state.enabled = False
        self.engine = FlowEngine(cfg, FakeIBService(cfg))
        self.engine._live_account_size = 6000.0
        self.dialog = ImportDialog(cfg, self.engine, self.engine.ib, None)
        self.pozice = ImportedPosition(
            key="AMZN Long+", symbol="AMZN", entry_price=266.4, target_price=269.33
        )

        # Prvky dialogu, na které zadávání sahá; rozhraní se nevykresluje
        self.dialog.rezim = Prepinac(REZIM_PREMIUM)
        self.dialog.spread_input = Pole(5.0)
        self.dialog.rrr_input = Pole(2.0)
        self.dialog.sl_spread_compensated = Zaskrtavatko(False)
        self.dialog.loading_label = Popisek()
        self.dialog.souhrn_label = Popisek()
        self.dialog.zadat_button = Zaskrtavatko(True)
        self.dialog.dialog = Okno()

        # Hlášky dialogu potřebují vykresleného klienta, test je jen spolkne
        hlaska = mock.patch.object(import_dialog.ui, "notify")
        hlaska.start()
        self.addCleanup(hlaska.stop)

        # Místo skutečného založení obchodu se zadání jen zaznamená
        self.zadani: list[FlowRequest] = []

        async def start_flow(request: FlowRequest) -> Flow:
            self.zadani.append(request)
            flow = Flow(
                id=f"AMZN-{len(self.zadani)}",
                symbol=request.symbol,
                entry_price=request.entry_price,
                profit_target=request.profit_target or 0.0,
                stop_loss=request.stop_loss or 0.0,
                quantity=request.quantity or 1,
                max_spread_pct=request.max_spread_pct or 0.0,
                state=FlowState.ARMED,
            )
            self.engine.flows[flow.id] = flow
            return flow

        self.engine.start_flow = start_flow

    def radek(
        self,
        pt: float | None = 90.0,
        sl: float | None = 45.0,
        premie: float | None = 3.00,
        rezim: str | None = None,
    ) -> RadekPozice:
        """Připravený a zaškrtnutý řádek; rezim=None znamená režim dialogu."""
        radek = RadekPozice(
            pozice=self.pozice,
            premie=premie,
            rezim_hodnot=self.dialog.rezim.value if rezim is None else rezim,
        )
        radek.vybrano = Zaskrtavatko(True)
        radek.pt_input = Pole(pt)
        radek.sl_input = Pole(sl)
        radek.qty_input = Pole(4)
        radek.runner_select = Pole(RUNNER_VYPNUTO)
        radek.stav_label = Popisek()
        radek.obnovit_button = Zaskrtavatko(True)
        return radek

    async def zadej(self, *radky: RadekPozice) -> None:
        """Pošle dané řádky do trhu tak, jak to dělá tlačítko dialogu."""
        self.dialog.radky = list(radky)
        await self.dialog._zadej()

    async def test_procenta_premie_plati_pro_pt_i_sl(self):
        # Jádro opravy: SL se do obchodu ukládá v téže jednotce jako PT,
        # jinak by formulář každou úroveň ukázal jinak
        await self.zadej(self.radek())
        zadani = self.zadani[0]
        self.assertTrue(zadani.pt_in_premium)
        self.assertTrue(zadani.sl_in_premium)
        self.assertEqual(zadani.premium_base, 3.00)
        self.assertFalse(zadani.pt_on_underlying)
        self.assertFalse(zadani.sl_on_underlying)

    async def test_usd_na_opci_nechava_obe_urovne_v_usd(self):
        self.dialog.rezim.set_value(REZIM_USD)
        await self.zadej(self.radek(premie=None))
        zadani = self.zadani[0]
        self.assertFalse(zadani.pt_in_premium)
        self.assertFalse(zadani.sl_in_premium)
        self.assertFalse(zadani.pt_on_underlying)
        self.assertFalse(zadani.sl_on_underlying)
        self.assertIsNone(zadani.premium_base)

    async def test_procento_drahy_da_obe_urovne_na_podkladu(self):
        self.dialog.rezim.set_value(REZIM_PCT)
        await self.zadej(self.radek(pt=269.33, sl=265.0, premie=None))
        zadani = self.zadani[0]
        self.assertTrue(zadani.pt_on_underlying)
        self.assertTrue(zadani.sl_on_underlying)
        self.assertFalse(zadani.pt_in_premium)
        self.assertFalse(zadani.sl_in_premium)

    async def test_rucne_prepsane_pt_jde_do_trhu_v_usd(self):
        # Ruční úprava PT prémii zahodila, takže se procenta nemají čeho chytit
        await self.zadej(self.radek(premie=None))
        zadani = self.zadani[0]
        self.assertFalse(zadani.pt_in_premium)
        self.assertFalse(zadani.sl_in_premium)
        self.assertIsNone(zadani.premium_base)

    async def test_prvotni_uroven_je_zadany_cil(self):
        await self.zadej(self.radek())
        self.assertEqual(self.zadani[0].primary_level, "pt")

    async def test_bez_pt_je_prvotni_urovni_sl(self):
        # Obchod si nemá pamatovat úroveň, kterou obchodník nezadal - jinak
        # by formulář při načtení přepsal ručně zadaný SL dopočtem z PT
        await self.zadej(self.radek(pt=None))
        self.assertEqual(self.zadani[0].primary_level, "sl")

    async def test_radek_z_jineho_rezimu_se_neposle(self):
        # Řádek spočítaný v jiném režimu by odešel ve špatné jednotce
        radek = self.radek(rezim=REZIM_USD)
        await self.zadej(radek)
        self.assertEqual(self.zadani, [])
        self.assertIn("přepočítejte", radek.stav_label.text.lower())

    async def test_radek_po_neuspesne_priprave_se_neposle(self):
        # Neúspěšná příprava nechává řádek bez platných hodnot
        radek = self.radek(rezim="")
        await self.zadej(radek)
        self.assertEqual(self.zadani, [])

    async def test_druhy_stisk_behem_davky_neposle_pozici_podruhe(self):
        # Bez zámku by druhý běh pracoval se snímkem výběru pořízeným ještě
        # před odškrtnutím řádků a poslal tytéž pozice do trhu znovu
        radek = self.radek()
        puvodni = self.engine.start_flow
        stisknuto_znovu = False

        async def start_flow_a_znovu_stisk(request: FlowRequest) -> Flow:
            nonlocal stisknuto_znovu
            flow = await puvodni(request)
            # Jen jednou - bez zámku by se stisky řetězily až k RecursionError
            # a test by místo jasného selhání spadl na hloubce zásobníku
            if not stisknuto_znovu:
                stisknuto_znovu = True
                await self.dialog._zadej()
            return flow

        self.engine.start_flow = start_flow_a_znovu_stisk
        await self.zadej(radek)
        self.assertTrue(stisknuto_znovu)
        self.assertEqual(len(self.zadani), 1)

    async def test_behem_pripravy_se_nezadava(self):
        # Rozpracovaný přepočet nechává v polích čísla z obou výpočtů
        self.dialog.priprava = True
        await self.zadej(self.radek())
        self.assertEqual(self.zadani, [])

    async def test_zadany_radek_se_odskrtne_a_zapamatuje_obchod(self):
        radek = self.radek()
        await self.zadej(radek)
        self.assertFalse(radek.vybrano.value)
        self.assertEqual(radek.flow_id, "AMZN-1")


class TestMinimumProRunner(unittest.TestCase):
    """
    Rozdělení runneru podle velikosti pozice: volbu dostanou jen řádky
    s větším množstvím, než je minimum nad tabulkou; ostatní zůstanou "Bez".
    """

    def setUp(self) -> None:
        self.cfg = AppConfig()
        self.cfg.state.enabled = False
        self.engine = FlowEngine(self.cfg, FakeIBService(self.cfg))
        # Dialog se nevykresluje, ovládací prvky nahrazují jednoduché atrapy
        self.dialog = ImportDialog(self.cfg, self.engine, self.engine.ib, None)
        self.dialog.runner_buttons = {}
        self.dialog.runner_min_input = Pole(3)
        self.pozice = importer.ImportedPosition(
            key="AMZN Long+", symbol="AMZN", entry_price=266.4, target_price=269.33
        )

    def radek(self, mnozstvi: float | None, flow_id: str = "") -> RadekPozice:
        """Řádek s daným množstvím a zatím nezvoleným runnerem."""
        radek = RadekPozice(pozice=self.pozice, flow_id=flow_id)
        radek.qty_input = Pole(mnozstvi)
        radek.runner_select = Pole(RUNNER_VYPNUTO)
        return radek

    def volby(self, *mnozstvi: float | None) -> list[str]:
        """Volby runneru, které řádkům s daným množstvím dialog přiřadí."""
        self.dialog.radky = [self.radek(ks) for ks in mnozstvi]
        self.dialog._obnov_runner_vsech()
        return [radek.runner_select.value for radek in self.dialog.radky]

    def test_runner_dostanou_jen_vetsi_pozice(self):
        self.dialog.runner_value = "1.5"
        # Minimum 3 znamená runner od čtyř kontraktů výš
        self.assertEqual(
            self.volby(5, 4, 3, 2, None),
            ["1.5", "1.5", RUNNER_VYPNUTO, RUNNER_VYPNUTO, RUNNER_VYPNUTO],
        )

    def test_zmena_minima_prerozdeli_runnery(self):
        self.dialog.runner_value = "2"
        self.dialog.runner_min_input.set_value(1)
        self.assertEqual(self.volby(2, 1), ["2", RUNNER_VYPNUTO])

    def test_globalni_volba_respektuje_minimum(self):
        maly = self.radek(2)
        velky = self.radek(4)
        self.dialog.radky = [maly, velky]
        self.dialog._nastav_runner("3")
        self.assertEqual(maly.runner_select.value, RUNNER_VYPNUTO)
        self.assertEqual(velky.runner_select.value, "3")

    def test_vypnuty_runner_nedostane_ani_velka_pozice(self):
        self.dialog.runner_value = RUNNER_VYPNUTO
        self.assertEqual(self.volby(10), [RUNNER_VYPNUTO])

    def test_prazdne_pole_minima_bere_hodnotu_z_konfigurace(self):
        # Vymazané pole nesmí runner rozdat podle náhodného čísla
        self.cfg.trading.runner_min_quantity = 4
        self.dialog.runner_value = "1"
        self.dialog.runner_min_input.set_value(None)
        self.assertEqual(self.volby(5, 4), ["1", RUNNER_VYPNUTO])

    def test_zmena_mnozstvi_v_radku_prepocita_runner(self):
        self.dialog.runner_value = "1.5"
        radek = self.radek(2)
        self.dialog.radky = [radek]
        self.dialog._obnov_runner_radku(radek)
        self.assertEqual(radek.runner_select.value, RUNNER_VYPNUTO)
        # Obchodník množství ručně zvedl - runner se má objevit
        radek.qty_input.set_value(6)
        self.dialog._obnov_runner_radku(radek)
        self.assertEqual(radek.runner_select.value, "1.5")

    def test_zamceny_radek_si_volbu_podrzi(self):
        # Obchod už drží pozici; runner se u něj přepíná tlačítky v přehledu
        radek = self.radek(10, flow_id="AMZN-1")
        radek.runner_select.set_value("2")
        self.engine.flows["AMZN-1"] = Flow(
            id="AMZN-1",
            symbol="AMZN",
            entry_price=266.4,
            profit_target=269.33,
            stop_loss=265.0,
            quantity=10,
            max_spread_pct=5.0,
            state=FlowState.EXIT_ARMED,
        )
        self.dialog.radky = [radek]
        self.dialog._nastav_runner(RUNNER_VYPNUTO)
        self.assertEqual(radek.runner_select.value, "2")


if __name__ == "__main__":
    unittest.main()
