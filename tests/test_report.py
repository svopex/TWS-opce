"""
Testy souhrnu obchodního dne - realizovaného výsledku obchodu, rozdělení
obchodů podle rozsahu a souhrnných čísel přehledu výsledků.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tws_opce import report
from tws_opce.models import Flow, FlowState
from tws_opce.report_dialog import (
    doba_drzeni,
    mez_osy,
    penize,
    penize_s_provizi,
    procenta,
    procenta_s_provizi,
    sklonuj,
    trida_vysledku,
)
from tws_opce.ui import pnl_text


def obchod(
    symbol: str = "AAPL",
    stav: FlowState = FlowState.CLOSED,
    fill: float | None = 3.00,
    exit_cena: float | None = 4.00,
    mnozstvi: int = 2,
    **zmeny,
) -> Flow:
    """
    Připraví obchod pro testy souhrnu - nakoupený za fill a prodaný
    za exit_cena. Další pole (runner, časy) doplní pojmenované argumenty.

    Nezadaný čas poslední změny se odvozuje od času založení, ne od aktuálního
    času - jinak by řazení ukončených obchodů záviselo na denní době, kdy sada
    zrovna běží.
    """
    flow = Flow(
        id=zmeny.pop("id", f"{symbol}-1"),
        symbol=symbol,
        entry_price=230.0,
        profit_target=235.0,
        stop_loss=228.0,
        quantity=mnozstvi,
        max_spread_pct=5.0,
        state=stav,
        fill_price=fill,
        filled_quantity=mnozstvi if fill is not None else 0,
        exit_fill_price=exit_cena,
    )
    for klic, hodnota in zmeny.items():
        setattr(flow, klic, hodnota)
    if "updated_at" not in zmeny:
        flow.updated_at = flow.created_at
    return flow


class TestRealizovanyVysledek(unittest.TestCase):
    """Vlastnost Flow.realized_pnl - výsledek skutečně prodaných kusů."""

    def test_bez_nakupu_neni_co_realizovat(self):
        flow = obchod(fill=None, exit_cena=None, stav=FlowState.MISSED)
        self.assertIsNone(flow.realized_pnl)

    def test_prodana_pozice_da_zisk_na_kontrakt_krat_sto(self):
        # Nákup 3,00, prodej 4,00, 2 kontrakty -> 200 USD
        flow = obchod()
        self.assertAlmostEqual(flow.realized_pnl, 200.0)

    def test_ztratovy_obchod_vyjde_zaporne(self):
        flow = obchod(fill=3.00, exit_cena=2.50)
        self.assertAlmostEqual(flow.realized_pnl, -100.0)

    def test_otevrena_pozice_nema_co_realizovat(self):
        flow = obchod(stav=FlowState.EXIT_ARMED, exit_cena=None)
        self.assertAlmostEqual(flow.realized_pnl, 0.0)

    def test_castecny_prodej_se_zapocita(self):
        # Z dvou kontraktů se prodal jeden za 4,20, zbytek běží dál
        flow = obchod(
            stav=FlowState.EXIT_ARMED,
            exit_cena=None,
            main_sold_quantity=1,
            main_sold_value=4.20,
        )
        self.assertAlmostEqual(flow.realized_pnl, 120.0)

    def test_prodany_runner_se_pricte(self):
        # K prodeji hlavní části se přidá dříve zúčtovaný runner
        flow = obchod(runner_realized_pnl=75.0)
        self.assertAlmostEqual(flow.realized_pnl, 275.0)


class TestProvize(unittest.TestCase):
    """Rozdělení skutečně účtovaných provizí mezi uzavřenou a otevřenou část."""

    def test_bez_provizi_je_cisty_vysledek_shodny_s_hrubym(self):
        flow = obchod()
        self.assertEqual(flow.commission, 0.0)
        self.assertAlmostEqual(flow.realized_pnl_net, flow.realized_pnl)

    def test_uzavreny_obchod_odecte_obe_strany_provize(self):
        # Nákup 3,00 -> prodej 4,00 na dvou kusech je +200, provize 7 USD
        flow = obchod(
            entry_commissions={"E1": 3.5},
            exit_commissions={"X1": 3.5},
        )
        self.assertAlmostEqual(flow.commission, 7.0)
        self.assertAlmostEqual(flow.realized_pnl_net, 193.0)

    def test_provize_se_nescitaji_pres_stejny_execid(self):
        # Táž exekuce načtená znovu jen přepíše hodnotu, nepřičte se podruhé
        flow = obchod(entry_commissions={"E1": 3.5})
        flow.entry_commissions["E1"] = 3.5
        self.assertAlmostEqual(flow.entry_commission, 3.5)

    def test_otevrene_pozici_patri_jen_pomerna_cast_nakupni_provize(self):
        # Ze čtyř kusů je jeden prodán; nákupní provize 2,60 se dělí 3:1
        flow = obchod(
            stav=FlowState.EXIT_ARMED,
            exit_cena=None,
            mnozstvi=4,
            main_sold_quantity=1,
            main_sold_value=4.20,
            entry_commissions={"E1": 2.60},
            exit_commissions={"X1": 0.65},
        )
        self.assertEqual(flow.open_quantity, 3)
        self.assertAlmostEqual(flow.open_commission, 1.95)
        # Zbytek nákupní provize a celá prodejní patří už prodanému kusu
        self.assertAlmostEqual(flow.realized_commission, 1.30)

    def test_prodejni_provize_otevrenou_cast_nesnizuje(self):
        # Nakoupeno, nic neprodáno - otevřené části patří celá nákupní provize
        flow = obchod(
            stav=FlowState.EXIT_ARMED, exit_cena=None, option_bid=3.40, option_ask=3.50,
            entry_commissions={"E1": 1.30},
        )
        self.assertAlmostEqual(flow.open_pnl, 80.0)
        self.assertAlmostEqual(flow.open_pnl_net, 78.70)
        self.assertAlmostEqual(flow.realized_commission, 0.0)

    def test_bez_nakupu_neni_z_ceho_odecitat(self):
        flow = obchod(fill=None, exit_cena=None, stav=FlowState.MISSED)
        self.assertIsNone(flow.realized_pnl_net)
        self.assertIsNone(flow.open_pnl_net)
        self.assertAlmostEqual(flow.open_commission, 0.0)


class TestPostupKCili(unittest.TestCase):
    """Ukazatel, kde pozice stojí mezi SL a PT."""

    def zaloz(self, bid: float) -> Flow:
        """Běžící pozice s odhadem zisku 200 USD a ztráty 100 USD."""
        return obchod(
            stav=FlowState.EXIT_ARMED,
            exit_cena=None,
            option_bid=bid,
            option_ask=bid + 0.05,
            expected_profit=200.0,
            expected_loss=-100.0,
        )

    def test_bez_odhadu_neni_co_ukazat(self):
        flow = self.zaloz(3.00)
        flow.expected_profit = None
        self.assertIsNone(report.postup_k_cili(flow))

    def test_na_nakupni_cene_je_pozice_v_tretine(self):
        # Rozpětí -100..+200, nulový výsledek leží v jedné třetině
        flow = self.zaloz(3.00)
        self.assertAlmostEqual(report.postup_k_cili(flow), 1 / 3)

    def test_na_urovni_pt_je_ukazatel_na_konci(self):
        # Zisk 200 USD = 1,00 na kontrakt při dvou kusech
        flow = self.zaloz(4.00)
        self.assertAlmostEqual(report.postup_k_cili(flow), 1.0)

    def test_prestreleny_pohyb_se_orezava(self):
        flow = self.zaloz(6.00)
        self.assertAlmostEqual(report.postup_k_cili(flow), 1.0)


class TestVyberRozsahu(unittest.TestCase):
    """Rozsah přehledu - dnešní den, nebo vše z monitoringu."""

    def setUp(self) -> None:
        self.dnes = date(2026, 8, 25)
        self.vcerejsi = obchod(
            symbol="TSLA",
            id="TSLA-1",
            created_at=datetime(2026, 8, 24, 15, 0),
            updated_at=datetime(2026, 8, 24, 16, 0),
        )
        self.dnesni = obchod(
            symbol="AMZN",
            id="AMZN-1",
            created_at=datetime(2026, 8, 25, 15, 0),
            updated_at=datetime(2026, 8, 25, 16, 0),
        )
        # Obchod z předchozího dne, který stále běží - patří do přehledu vždy
        self.bezici = obchod(
            symbol="NFLX",
            id="NFLX-1",
            stav=FlowState.EXIT_ARMED,
            exit_cena=None,
            created_at=datetime(2026, 8, 24, 15, 30),
        )

    def test_dnesek_vynecha_vcerejsi_uzavrene(self):
        vybrane = report.vyber(
            [self.vcerejsi, self.dnesni, self.bezici], report.ROZSAH_DNES, self.dnes
        )
        self.assertEqual({f.id for f in vybrane}, {"AMZN-1", "NFLX-1"})

    def test_bezici_obchod_ze_vcerejska_v_dnesku_zustava(self):
        vybrane = report.vyber([self.bezici], report.ROZSAH_DNES, self.dnes)
        self.assertEqual(len(vybrane), 1)

    def test_rozsah_vse_bere_uplne_vsechno(self):
        vybrane = report.vyber(
            [self.vcerejsi, self.dnesni, self.bezici], report.ROZSAH_VSE, self.dnes
        )
        self.assertEqual(len(vybrane), 3)


class TestSouhrn(unittest.TestCase):
    """Souhrnná čísla nad seznamem obchodů."""

    def setUp(self) -> None:
        self.dnes = date(2026, 8, 25)
        zaklad = datetime(2026, 8, 25, 15, 0)
        # Dva ziskové, jeden ztrátový, jeden propásnutý a jedna běžící pozice
        self.flows = [
            obchod(symbol="AAPL", id="AAPL-1", exit_cena=4.00, created_at=zaklad,
                   updated_at=zaklad + timedelta(minutes=10)),
            obchod(symbol="AMZN", id="AMZN-1", exit_cena=3.50, created_at=zaklad,
                   updated_at=zaklad + timedelta(minutes=20)),
            obchod(symbol="TSLA", id="TSLA-1", exit_cena=2.00, created_at=zaklad,
                   updated_at=zaklad + timedelta(minutes=30)),
            obchod(symbol="SPY", id="SPY-1", stav=FlowState.MISSED, fill=None,
                   exit_cena=None, created_at=zaklad),
            obchod(symbol="NFLX", id="NFLX-1", stav=FlowState.EXIT_ARMED,
                   exit_cena=None, option_bid=3.40, option_ask=3.50,
                   created_at=zaklad),
        ]

    def sestav(self) -> report.DenniReport:
        """Přehled nad připravenými obchody v rozsahu dnešního dne."""
        return report.sestav(self.flows, report.ROZSAH_DNES, self.dnes)

    def test_rozdeleni_na_bezici_a_ukoncene(self):
        podklad = self.sestav()
        self.assertEqual([f.id for f in podklad.bezici], ["NFLX-1"])
        self.assertEqual(len(podklad.ukoncene), 4)

    def test_ukoncene_se_radi_od_nejnovejsiho(self):
        podklad = self.sestav()
        self.assertEqual(podklad.ukoncene[0].id, "TSLA-1")

    def test_realizovany_soucet_secte_uzavrene(self):
        # +200, +100, -200 -> +100 USD
        self.assertAlmostEqual(self.sestav().souhrn.realizovano, 100.0)

    def test_otevrena_pozice_se_ocenuje_bidem(self):
        # Běžící NFLX: BID 3,40 proti nákupu 3,00 na dvou kontraktech
        souhrn = self.sestav().souhrn
        self.assertAlmostEqual(souhrn.otevreno, 80.0)
        self.assertEqual(souhrn.otevrenych_pozic, 1)
        self.assertEqual(souhrn.otevrenych_kusu, 2)

    def test_celkovy_vysledek_scita_obe_slozky(self):
        self.assertAlmostEqual(self.sestav().souhrn.celkem, 180.0)

    def test_pocty_obchodu_podle_vysledku(self):
        souhrn = self.sestav().souhrn
        self.assertEqual(souhrn.ziskovych, 2)
        self.assertEqual(souhrn.ztratovych, 1)
        self.assertEqual(souhrn.bez_obchodu, 1)
        self.assertEqual(souhrn.bezicich, 1)

    def test_uspesnost_pocita_jen_obchody_s_nakupem(self):
        # Dva ziskové ze tří uzavřených s nákupem; propásnutý se nepočítá
        self.assertAlmostEqual(self.sestav().souhrn.uspesnost, 200 / 3)

    def test_profit_factor_je_pomer_zisku_ke_ztrate(self):
        # Hrubý zisk 300, hrubá ztráta 200
        self.assertAlmostEqual(self.sestav().souhrn.profit_factor, 1.5)

    def test_bez_ztraty_nema_profit_factor_smysl(self):
        self.flows = [f for f in self.flows if f.id != "TSLA-1"]
        self.assertIsNone(self.sestav().souhrn.profit_factor)

    def test_nejlepsi_a_nejhorsi_obchod(self):
        souhrn = self.sestav().souhrn
        self.assertEqual(souhrn.nejlepsi, ("AAPL", 200.0))
        self.assertEqual(souhrn.nejhorsi, ("TSLA", -200.0))

    def test_krivka_kumuluje_vysledky_v_case(self):
        # Obchody se řadí podle času uzavření: +200, +300, +100
        hodnoty = [round(h, 2) for _, h in self.sestav().krivka]
        self.assertEqual(hodnoty, [200.0, 300.0, 100.0])

    def test_soucty_podle_tickeru_jsou_serazene(self):
        podklad = self.sestav()
        podle = {p.symbol: round(p.celkem, 2) for p in podklad.podle_tickeru}
        self.assertEqual(podle["AAPL"], 200.0)
        self.assertEqual(podle["NFLX"], 80.0)
        self.assertEqual(podle["TSLA"], -200.0)
        # Propásnutý obchod nemá co ukázat, v grafu tickerů chybí
        self.assertNotIn("SPY", podle)
        # Řazení od nejlepšího po nejhorší
        self.assertEqual(podklad.podle_tickeru[0].symbol, "AAPL")
        self.assertEqual(podklad.podle_tickeru[-1].symbol, "TSLA")

    def test_prazdny_prehled(self):
        self.flows = []
        podklad = self.sestav()
        self.assertEqual(podklad.bezici, [])
        self.assertEqual(podklad.ukoncene, [])
        self.assertEqual(podklad.souhrn.celkem, 0.0)


class TestSouhrnSProvizemi(unittest.TestCase):
    """Souhrn dne po odečtení provizí účtovaných TWS."""

    def setUp(self) -> None:
        self.dnes = date(2026, 8, 25)
        zaklad = datetime(2026, 8, 25, 15, 0)
        # Ziskový obchod (+200, provize 7) a ztrátový (-200, provize 7)
        self.flows = [
            obchod(symbol="AAPL", id="AAPL-1", exit_cena=4.00, created_at=zaklad,
                   updated_at=zaklad + timedelta(minutes=10),
                   entry_commissions={"E1": 3.5}, exit_commissions={"X1": 3.5}),
            obchod(symbol="TSLA", id="TSLA-1", exit_cena=2.00, created_at=zaklad,
                   updated_at=zaklad + timedelta(minutes=20),
                   entry_commissions={"E2": 3.5}, exit_commissions={"X2": 3.5}),
            # Běžící pozice: nakoupeno za 3,00, BID 3,40, nákupní provize 1,30
            obchod(symbol="NFLX", id="NFLX-1", stav=FlowState.EXIT_ARMED,
                   exit_cena=None, option_bid=3.40, option_ask=3.50,
                   created_at=zaklad, entry_commissions={"E3": 1.30}),
        ]

    def sestav(self) -> report.DenniReport:
        """Přehled nad připravenými obchody v rozsahu dnešního dne."""
        return report.sestav(self.flows, report.ROZSAH_DNES, self.dnes)

    def test_hrube_soucty_provize_neobsahuji(self):
        souhrn = self.sestav().souhrn
        self.assertAlmostEqual(souhrn.realizovano, 0.0)
        self.assertAlmostEqual(souhrn.otevreno, 80.0)
        self.assertAlmostEqual(souhrn.celkem, 80.0)

    def test_provize_se_deli_na_uzavrenou_a_otevrenou_cast(self):
        souhrn = self.sestav().souhrn
        self.assertAlmostEqual(souhrn.provize_realizovane, 14.0)
        self.assertAlmostEqual(souhrn.provize_otevrene, 1.30)
        self.assertAlmostEqual(souhrn.provize, 15.30)

    def test_vysledek_dne_je_hruby_soucet_minus_provize(self):
        souhrn = self.sestav().souhrn
        self.assertAlmostEqual(souhrn.realizovano_s_provizi, -14.0)
        self.assertAlmostEqual(souhrn.otevreno_s_provizi, 78.70)
        self.assertAlmostEqual(souhrn.celkem_s_provizi, 64.70)

    def test_profit_factor_pocita_z_hodnot_po_provizich(self):
        # Zisk 193, ztráta 207 -> poměr pod jednou, ačkoli hrubě byl den nula
        souhrn = self.sestav().souhrn
        self.assertAlmostEqual(souhrn.hruby_zisk, 193.0)
        self.assertAlmostEqual(souhrn.hruba_ztrata, 207.0)
        self.assertAlmostEqual(souhrn.profit_factor, 193.0 / 207.0)

    def test_tesny_zisk_umi_provize_prevratit_ve_ztratu(self):
        # Obchod +100 se sedmidolarovou provizí zůstává ziskový, s provizí 120 ne
        self.flows = [
            obchod(symbol="AAPL", id="AAPL-1", exit_cena=3.50,
                   created_at=datetime(2026, 8, 25, 15, 0),
                   entry_commissions={"E1": 60.0}, exit_commissions={"X1": 60.0}),
        ]
        souhrn = self.sestav().souhrn
        self.assertEqual(souhrn.ziskovych, 0)
        self.assertEqual(souhrn.ztratovych, 1)
        self.assertEqual(souhrn.nejhorsi, ("AAPL", -20.0))

    def test_krivka_i_soucty_podle_tickeru_jsou_po_provizich(self):
        podklad = self.sestav()
        self.assertEqual([round(h, 2) for _, h in podklad.krivka], [193.0, -14.0])
        podle = {p.symbol: round(p.celkem_s_provizi, 2) for p in podklad.podle_tickeru}
        self.assertEqual(podle, {"AAPL": 193.0, "NFLX": 78.70, "TSLA": -207.0})


class TestProcentUctu(unittest.TestCase):
    """Výsledek dne vyjádřený podílem z účtu."""

    def setUp(self) -> None:
        self.dnes = date(2026, 8, 25)
        zaklad = datetime(2026, 8, 25, 15, 0)
        # Ziskový obchod (+200, provize 7) a běžící pozice (+80, provize 1,30)
        self.flows = [
            obchod(symbol="AAPL", id="AAPL-1", exit_cena=4.00, created_at=zaklad,
                   updated_at=zaklad + timedelta(minutes=10),
                   entry_commissions={"E1": 3.5}, exit_commissions={"X1": 3.5}),
            obchod(symbol="NFLX", id="NFLX-1", stav=FlowState.EXIT_ARMED,
                   exit_cena=None, option_bid=3.40, option_ask=3.50,
                   created_at=zaklad, entry_commissions={"E3": 1.30}),
        ]

    def sestav(self, account_size: float = 5000.0) -> report.DenniReport:
        """Přehled nad připravenými obchody se zadanou velikostí účtu."""
        return report.sestav(
            self.flows, report.ROZSAH_DNES, self.dnes, account_size=account_size
        )

    def test_vysledek_dne_v_procentech_uctu(self):
        # +193 realizováno a +78,70 v pozicích z účtu 5 000 USD
        souhrn = self.sestav().souhrn
        self.assertAlmostEqual(souhrn.celkem_s_provizi, 271.70)
        self.assertAlmostEqual(souhrn.procento_uctu(souhrn.celkem_s_provizi), 5.434)

    def test_procenta_pocitaji_i_ztratu(self):
        souhrn = self.sestav().souhrn
        self.assertAlmostEqual(souhrn.procento_uctu(-250.0), -5.0)

    def test_bez_znameho_uctu_procenta_nejsou(self):
        # Velikost účtu z TWS nedorazila a konfigurace ji nemá - dělit nulou
        # nelze a odhad by lhal
        souhrn = self.sestav(account_size=0.0).souhrn
        self.assertIsNone(souhrn.procento_uctu(souhrn.celkem_s_provizi))

    def test_vychozi_sestaveni_ucet_nezna(self):
        # Volání bez velikosti účtu zůstává platné, procenta jen nejsou
        souhrn = report.sestav(self.flows, report.ROZSAH_DNES, self.dnes).souhrn
        self.assertEqual(souhrn.account_size, 0.0)
        self.assertIsNone(souhrn.procento_uctu(100.0))


class TestFormatovani(unittest.TestCase):
    """Pomocné funkce pro zobrazení hodnot v přehledu."""

    def test_castka_se_znamenkem_a_mezerou_v_tisicich(self):
        self.assertEqual(penize(1234.5), "+1 234.50")
        self.assertEqual(penize(-1234.5), "-1 234.50")

    def test_cena_se_zobrazuje_bez_znamenka(self):
        self.assertEqual(penize(3.1, znamenko=False), "3.10")

    def test_chybejici_hodnota_je_pomlcka(self):
        self.assertEqual(penize(None), "-")

    def test_zavorka_ukazuje_hodnotu_bez_provizi(self):
        self.assertEqual(penize_s_provizi(-92.0, -85.0), "(-85.00)")

    def test_bez_provize_se_zavorka_vynechava(self):
        self.assertEqual(penize_s_provizi(-85.0, -85.0), "")
        self.assertEqual(penize_s_provizi(None, -85.0), "")
        self.assertEqual(penize_s_provizi(-85.0, None), "")

    def test_procenta_uctu_maji_znamenko_i_jednotku(self):
        self.assertEqual(procenta(0.62), "+0.62 %")
        self.assertEqual(procenta(-1.5), "-1.50 %")
        self.assertEqual(procenta(None), "-")

    def test_zavorka_procent_ukazuje_hodnotu_bez_provizi(self):
        self.assertEqual(procenta_s_provizi(0.62, 0.68), "(+0.68 %)")
        # Shodná procenta se neopakují dvakrát
        self.assertEqual(procenta_s_provizi(0.62, 0.62), "")
        self.assertEqual(procenta_s_provizi(None, 0.68), "")

    def test_pnl_v_tabulce_spojuje_obe_hodnoty(self):
        self.assertEqual(pnl_text(-92.0, -85.0), "-92.00 (-85.00)")
        self.assertEqual(pnl_text(-85.0, -85.0), "-85.00")
        self.assertEqual(pnl_text(None, -85.0), "-")

    def test_barva_podle_vysledku(self):
        self.assertEqual(trida_vysledku(10.0), "zisk")
        self.assertEqual(trida_vysledku(-10.0), "ztrata")
        # Nulový výsledek ani chybějící hodnota se nebarví
        self.assertEqual(trida_vysledku(0.0), "")
        self.assertEqual(trida_vysledku(None), "")

    def test_doba_drzeni_ve_zkracenem_tvaru(self):
        self.assertEqual(doba_drzeni(45), "45 s")
        self.assertEqual(doba_drzeni(12 * 60), "12 min")
        self.assertEqual(doba_drzeni(65 * 60), "1:05 h")
        self.assertEqual(doba_drzeni(None), "-")

    def test_sklonovani_poctu(self):
        self.assertEqual(sklonuj(1, "obchod", "obchody", "obchodů"), "1 obchod")
        self.assertEqual(sklonuj(3, "obchod", "obchody", "obchodů"), "3 obchody")
        self.assertEqual(sklonuj(7, "obchod", "obchody", "obchodů"), "7 obchodů")
        self.assertEqual(sklonuj(0, "obchod", "obchody", "obchodů"), "0 obchodů")

    def test_meze_osy_jsou_kulate_a_s_rezervou(self):
        self.assertEqual(mez_osy([300.0, -300.0]), (-400.0, 400.0))

    def test_bez_zapornych_hodnot_osa_zacina_nulou(self):
        self.assertEqual(mez_osy([135.0, 60.0]), (0.0, 200.0))

    def test_bez_kladnych_hodnot_osa_konci_nulou(self):
        self.assertEqual(mez_osy([-45.0, -12.0]), (-55.0, 0.0))

    def test_same_nuly_dostanou_nahradni_rozsah(self):
        self.assertEqual(mez_osy([0.0]), (-1.0, 1.0))


if __name__ == "__main__":
    unittest.main()
