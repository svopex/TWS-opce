"""
Testy čtení tržních dat ze skutečné implementace IBService.
Ticker se plní ručně, spojení s TWS není potřeba - ověřuje se, že aplikace
používá pole a metody ib_async správně.
"""

from __future__ import annotations

import asyncio
import math
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import OptionComputation, Stock, Ticker

from tws_opce.config import AppConfig
from tws_opce.ib_service import IBService, valid_price
from tws_opce.ui import linka_varuje, oer_popis, oer_text, stari_text, stav_linky_text

NAN = float("nan")


def vloz_ticker(sluzba: IBService, conid: int = 265598, **hodnoty) -> Stock:
    """
    Vloží do služby kontrakt s předvyplněnými tržními daty.
    Ceny se nastavují až po vytvoření Tickeru - ib_async je v __post_init__
    přepisuje na nevyplněné hodnoty, takže konstruktorem je předat nelze.

    conid odlišuje kontrakty, když test potřebuje víc odběrů naráz.
    """
    kontrakt = Stock("AAPL", "SMART", "USD")
    kontrakt.conId = conid
    ticker = Ticker(contract=kontrakt)
    for nazev, hodnota in hodnoty.items():
        setattr(ticker, nazev, hodnota)
    sluzba._tickers[kontrakt.conId] = ticker
    return kontrakt


class TestPlatnostCeny(unittest.TestCase):
    """Filtrování nepoužitelných hodnot z TWS."""

    def test_zaporna_a_nulova_cena_je_neplatna(self):
        # TWS posílá -1, pokud kotace není k dispozici
        self.assertIsNone(valid_price(-1))
        self.assertIsNone(valid_price(0))

    def test_nan_je_neplatny(self):
        self.assertIsNone(valid_price(NAN))
        self.assertIsNone(valid_price(math.inf))

    def test_platna_cena_projde(self):
        self.assertAlmostEqual(valid_price(7.75), 7.75)


class TestCenaPodkladu(unittest.TestCase):
    """Výběr ceny podkladu z dostupných zdrojů."""

    def setUp(self) -> None:
        self.sluzba = IBService(AppConfig())

    def test_prednost_ma_posledni_obchod(self):
        kontrakt = vloz_ticker(self.sluzba, last=231.5, bid=231.0, ask=232.0, close=230.0)
        self.assertAlmostEqual(self.sluzba.underlying_price(kontrakt), 231.5)

    def test_bez_posledniho_obchodu_se_pouzije_stred_trhu(self):
        # midpoint() z ib_async vyžaduje i velikosti kotací, jinak vrací nevyplněnou hodnotu
        kontrakt = vloz_ticker(
            self.sluzba, bid=231.0, ask=232.0, bidSize=100, askSize=120, close=230.0
        )
        self.assertAlmostEqual(self.sluzba.underlying_price(kontrakt), 231.5)

    def test_kotace_bez_velikosti_spadnou_na_zaverecnou_cenu(self):
        # Neúplná kotace z TWS (chybí velikosti) se pro cenu podkladu nepoužije
        kontrakt = vloz_ticker(self.sluzba, bid=231.0, ask=232.0, close=230.0)
        self.assertAlmostEqual(self.sluzba.underlying_price(kontrakt), 230.0)

    def test_bez_kotaci_se_pouzije_mark_price(self):
        # markPrice je datové pole ib_async, nikoliv metoda
        kontrakt = vloz_ticker(self.sluzba, markPrice=229.5, close=230.0)
        self.assertAlmostEqual(self.sluzba.underlying_price(kontrakt), 229.5)

    def test_posledni_moznosti_je_zaverecna_cena(self):
        kontrakt = vloz_ticker(self.sluzba, close=230.0)
        self.assertAlmostEqual(self.sluzba.underlying_price(kontrakt), 230.0)

    def test_bez_jakychkoliv_dat_vraci_none(self):
        kontrakt = vloz_ticker(self.sluzba)
        self.assertIsNone(self.sluzba.underlying_price(kontrakt))

    def test_zaporne_hodnoty_se_preskoci(self):
        # TWS označuje chybějící kotaci hodnotou -1
        kontrakt = vloz_ticker(self.sluzba, last=-1, bid=-1, ask=-1, close=230.0)
        self.assertAlmostEqual(self.sluzba.underlying_price(kontrakt), 230.0)

    def test_neodebirany_kontrakt_vraci_none(self):
        neznamy = Stock("MSFT", "SMART", "USD")
        neznamy.conId = 999
        self.assertIsNone(self.sluzba.underlying_price(neznamy))
        self.assertIsNone(self.sluzba.underlying_price(None))


class TestKotaceOpce(unittest.TestCase):
    """Čtení BID, ASK a delty opce."""

    def setUp(self) -> None:
        self.sluzba = IBService(AppConfig())

    def test_kotace_a_delta_z_modelu(self):
        kontrakt = vloz_ticker(self.sluzba, bid=3.00, ask=3.10)
        self.sluzba._tickers[kontrakt.conId].modelGreeks = OptionComputation(
            tickAttrib=0, delta=0.42
        )
        bid, ask, delta = self.sluzba.option_quotes(kontrakt)
        self.assertAlmostEqual(bid, 3.00)
        self.assertAlmostEqual(ask, 3.10)
        self.assertAlmostEqual(delta, 0.42)

    def test_bez_greeks_je_delta_none(self):
        kontrakt = vloz_ticker(self.sluzba, bid=3.00, ask=3.10)
        _, _, delta = self.sluzba.option_quotes(kontrakt)
        self.assertIsNone(delta)

    def test_nahradni_zdroj_delty(self):
        # Když chybí model, použije se delta z posledního obchodu
        kontrakt = vloz_ticker(self.sluzba, bid=3.00, ask=3.10)
        self.sluzba._tickers[kontrakt.conId].lastGreeks = OptionComputation(
            tickAttrib=0, delta=-0.31
        )
        _, _, delta = self.sluzba.option_quotes(kontrakt)
        self.assertAlmostEqual(delta, -0.31)

    def test_nan_delta_se_ignoruje(self):
        kontrakt = vloz_ticker(self.sluzba, bid=3.00, ask=3.10)
        self.sluzba._tickers[kontrakt.conId].modelGreeks = OptionComputation(
            tickAttrib=0, delta=NAN
        )
        _, _, delta = self.sluzba.option_quotes(kontrakt)
        self.assertIsNone(delta)

    def test_chybejici_kotace(self):
        kontrakt = vloz_ticker(self.sluzba)
        bid, ask, _ = self.sluzba.option_quotes(kontrakt)
        self.assertIsNone(bid)
        self.assertIsNone(ask)


class TestOdbery(unittest.TestCase):
    """Počítadlo odběratelů tržních dat."""

    def test_odber_se_rusi_az_po_poslednim_odberateli(self):
        sluzba = IBService(AppConfig())
        kontrakt = vloz_ticker(sluzba, last=231.0)
        # Ticker byl vložen ručně, počítadlo se nastaví jako po prvním odběru
        sluzba._subscribers[kontrakt.conId] = 2

        sluzba.unsubscribe(kontrakt)
        self.assertIn(kontrakt.conId, sluzba._tickers)

        sluzba.unsubscribe(kontrakt)
        self.assertNotIn(kontrakt.conId, sluzba._tickers)


class TestCenaOpceProModel(unittest.TestCase):
    """Cena opce pro model - pořadí zdrojů: střed kotace, jedna strana, last, close."""

    def setUp(self) -> None:
        self.sluzba = IBService(AppConfig())

    def test_stred_kotace_ma_prednost(self):
        kontrakt = vloz_ticker(self.sluzba, bid=3.00, ask=3.10, last=2.50, close=2.00)
        cena, zdroj = self.sluzba.option_price(kontrakt)
        self.assertAlmostEqual(cena, 3.05)
        self.assertEqual(zdroj, "BID/ASK")

    def test_jedna_strana_kotace(self):
        kontrakt = vloz_ticker(self.sluzba, ask=3.10, last=2.50)
        cena, zdroj = self.sluzba.option_price(kontrakt)
        self.assertAlmostEqual(cena, 3.10)
        self.assertEqual(zdroj, "ASK")

    def test_bez_kotaci_posledni_obchod(self):
        kontrakt = vloz_ticker(self.sluzba, last=2.50, close=2.00)
        cena, zdroj = self.sluzba.option_price(kontrakt)
        self.assertAlmostEqual(cena, 2.50)
        self.assertEqual(zdroj, "last")

    def test_nakonec_zaverecna_cena(self):
        kontrakt = vloz_ticker(self.sluzba, close=2.00)
        cena, zdroj = self.sluzba.option_price(kontrakt)
        self.assertAlmostEqual(cena, 2.00)
        self.assertEqual(zdroj, "close")

    def test_bez_jakekoliv_ceny(self):
        kontrakt = vloz_ticker(self.sluzba, bid=-1.0, ask=NAN)
        cena, zdroj = self.sluzba.option_price(kontrakt)
        self.assertIsNone(cena)
        self.assertEqual(zdroj, "")


class TestCekaniNaKotace(unittest.IsolatedAsyncioTestCase):
    """Čekání na data kontraktu dává kotacím šanci dorazit po závěrečné ceně."""

    def setUp(self) -> None:
        self.sluzba = IBService(AppConfig())

    async def test_s_kotacemi_vraci_ihned(self):
        kontrakt = vloz_ticker(self.sluzba, bid=3.00, ask=3.10)
        loop = asyncio.get_running_loop()
        start = loop.time()
        await self.sluzba.wait_for_quotes(kontrakt, timeout=5.0, quotes_grace=2.0)
        self.assertLess(loop.time() - start, 0.5)

    async def test_jen_zaverecna_cena_ceka_odklad(self):
        kontrakt = vloz_ticker(self.sluzba, close=2.00)
        loop = asyncio.get_running_loop()
        start = loop.time()
        await self.sluzba.wait_for_quotes(kontrakt, timeout=5.0, quotes_grace=0.5)
        uplynulo = loop.time() - start
        # Počká zhruba odklad, ale ne celý časový limit
        self.assertGreaterEqual(uplynulo, 0.4)
        self.assertLess(uplynulo, 2.0)

    async def test_kotace_behem_odkladu_ukonci_cekani(self):
        kontrakt = vloz_ticker(self.sluzba, close=2.00)
        ticker = self.sluzba._tickers[kontrakt.conId]

        async def dodej_kotace():
            await asyncio.sleep(0.3)
            ticker.bid, ticker.ask = 3.00, 3.10

        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.gather(
            self.sluzba.wait_for_quotes(kontrakt, timeout=5.0, quotes_grace=3.0),
            dodej_kotace(),
        )
        self.assertLess(loop.time() - start, 1.5)

    async def test_bez_dat_vyprsi_limit(self):
        kontrakt = vloz_ticker(self.sluzba)
        loop = asyncio.get_running_loop()
        start = loop.time()
        await self.sluzba.wait_for_quotes(kontrakt, timeout=0.5)
        self.assertGreaterEqual(loop.time() - start, 0.4)

    async def test_odklad_se_ceka_jen_poprve(self):
        # Příprava zadání běží po každé změně formuláře; u opce, ze které
        # TWS posílá jen závěrečnou cenu, se odklad čeká jen napoprvé
        kontrakt = vloz_ticker(self.sluzba, close=2.00)
        loop = asyncio.get_running_loop()
        await self.sluzba.wait_for_quotes(kontrakt, timeout=5.0, quotes_grace=0.5)

        start = loop.time()
        await self.sluzba.wait_for_quotes(kontrakt, timeout=5.0, quotes_grace=0.5)
        self.assertLess(loop.time() - start, 0.3)

    async def test_nove_spojeni_odklad_obnovi(self):
        kontrakt = vloz_ticker(self.sluzba, close=2.00)
        await self.sluzba.wait_for_quotes(kontrakt, timeout=5.0, quotes_grace=0.3)
        # Ztráta spojení zahodí tržní data i paměť odkladů
        self.sluzba._on_disconnected()
        vloz_ticker(self.sluzba, close=2.00)

        loop = asyncio.get_running_loop()
        start = loop.time()
        await self.sluzba.wait_for_quotes(kontrakt, timeout=5.0, quotes_grace=0.3)
        self.assertGreaterEqual(loop.time() - start, 0.2)

    async def test_jednostranna_kotace_ceka_jen_odklad(self):
        # Jen BID bez last/close - nečeká se celý limit, jen odklad na ASK
        kontrakt = vloz_ticker(self.sluzba, bid=3.00)
        loop = asyncio.get_running_loop()
        start = loop.time()
        await self.sluzba.wait_for_quotes(kontrakt, timeout=5.0, quotes_grace=0.3)
        uplynulo = loop.time() - start
        self.assertGreaterEqual(uplynulo, 0.2)
        self.assertLess(uplynulo, 1.5)


class TestStariKotaci(unittest.TestCase):
    """Stáří tržních dat pro ukazatel kvality spojení v hlavičce."""

    def setUp(self) -> None:
        self.sluzba = IBService(AppConfig())

    def test_bez_odberu_neni_co_merit(self):
        self.assertIsNone(self.sluzba.quotes_age())

    def test_ticker_bez_casu_se_preskoci(self):
        vloz_ticker(self.sluzba, bid=3.00)
        self.assertIsNone(self.sluzba.quotes_age())

    def test_rozhoduje_nejnovejsi_kotace(self):
        # Nelikvidní opce se aktualizuje zřídka i při zcela zdravém spojení,
        # proto rozhoduje nejčerstvější čas, ne nejstarší
        ted = datetime.now()
        vloz_ticker(self.sluzba, conid=1, time=ted - timedelta(seconds=90))
        vloz_ticker(self.sluzba, conid=2, time=ted - timedelta(seconds=2))
        self.assertLess(self.sluzba.quotes_age(), 10)

    def test_cas_s_casovou_zonou(self):
        # ib_async dodává časy kotací v UTC s časovou zónou
        vloz_ticker(self.sluzba, time=datetime.now(timezone.utc) - timedelta(seconds=3))
        stari = self.sluzba.quotes_age()
        self.assertGreaterEqual(stari, 2.0)
        self.assertLess(stari, 10)

    def test_hodiny_tws_napred_davaji_nulu(self):
        # Záporné stáří by v hlavičce mátlo víc než nula
        vloz_ticker(self.sluzba, time=datetime.now() + timedelta(seconds=30))
        self.assertEqual(self.sluzba.quotes_age(), 0.0)


class TestOdezvyTws(unittest.IsolatedAsyncioTestCase):
    """Měření odezvy TWS dotazem na aktuální čas."""

    def setUp(self) -> None:
        self.sluzba = IBService(AppConfig())

    def predstirej_spojeni(self, odpoved) -> None:
        """Podvrhne spojení i dotaz na čas, aby test nepotřeboval TWS."""
        self.sluzba.ib.isConnected = lambda: True
        self.sluzba.ib.reqCurrentTimeAsync = odpoved

    async def test_bez_spojeni_se_nemeri(self):
        # Stará hodnota se musí zahodit, ať v hlavičce nestraší
        self.sluzba.rtt_ms = 12.0
        self.assertIsNone(await self.sluzba.measure_rtt())
        self.assertIsNone(self.sluzba.rtt_ms)

    async def test_zmerena_odezva_se_zapamatuje(self):
        async def odpoved():
            await asyncio.sleep(0.05)
            return datetime.now()

        self.predstirej_spojeni(odpoved)
        rtt = await self.sluzba.measure_rtt()
        self.assertGreaterEqual(rtt, 40.0)
        self.assertEqual(self.sluzba.rtt_ms, rtt)

    async def test_chyba_dotazu_vraci_none(self):
        # Nedostupná odpověď je údaj o spojení, ne chyba k vyhození
        async def selze():
            raise ConnectionError("spojení spadlo")

        self.predstirej_spojeni(selze)
        self.sluzba.rtt_ms = 12.0
        self.assertIsNone(await self.sluzba.measure_rtt())
        self.assertIsNone(self.sluzba.rtt_ms)


class TestPopisuLinky(unittest.TestCase):
    """Text ukazatele kvality spojení v hlavičce."""

    def test_odezva_i_stari(self):
        self.assertEqual(stav_linky_text(0.84, 0.42), "TWS 0,8 ms · data 0,4 s")

    def test_nezmerena_odezva_je_pomlcka(self):
        self.assertEqual(stav_linky_text(None, 1.0), "TWS - · data 1,0 s")

    def test_bez_odberu_je_pomlcka_u_dat(self):
        self.assertEqual(stav_linky_text(3.0, None), "TWS 3,0 ms · data -")

    def test_stari_se_zaokrouhluje_podle_velikosti(self):
        # Do deseti sekund je vidět desetina, výš už jen celé sekundy
        self.assertEqual(stari_text(0.42), "0,4 s")
        self.assertEqual(stari_text(9.94), "9,9 s")
        self.assertEqual(stari_text(42.4), "42 s")
        self.assertEqual(stari_text(185.0), "3 min")
        self.assertEqual(stari_text(None), "-")


class TestPopisuOer(unittest.TestCase):
    """Order Efficiency Ratio dne v hlavičce."""

    def test_jedno_desetinne_misto_s_carkou(self):
        self.assertEqual(oer_text(10.0), "OER: 10,0")
        self.assertEqual(oer_text(175.666), "OER: 175,7")

    def test_tooltip_s_rozpoctem_i_bez_hlidani(self):
        self.assertIn("30 zpráv do TWS / (2 vyplněných příkazů + 1)", oer_popis(30, 2, 200.0))
        self.assertIn("Rozpočet dne 200 zpráv", oer_popis(30, 2, 200.0))
        self.assertIn("Hlídání OER je vypnuté", oer_popis(30, 2, None))


class TestVarovaniLinky(unittest.TestCase):
    """Kdy se ukazatel kvality spojení zvýrazní."""

    def test_zdrava_linka_nevaruje(self):
        self.assertFalse(linka_varuje(1.0, 0.5, trh_otevren=True))

    def test_pomala_odezva_varuje_i_po_zavreni_trhu(self):
        # Nestíhající TWS je problém bez ohledu na denní dobu
        self.assertTrue(linka_varuje(900.0, 0.5, trh_otevren=False))

    def test_stojici_kotace_varuji_jen_behem_seance(self):
        self.assertTrue(linka_varuje(1.0, 120.0, trh_otevren=True))
        # Mimo obchodní hodiny trh nic neposílá - varování by svítilo pořád
        self.assertFalse(linka_varuje(1.0, 120.0, trh_otevren=False))

    def test_chybejici_hodnoty_nevaruji(self):
        # Nezměřená odezva ani žádný odběr nejsou známkou potíží
        self.assertFalse(linka_varuje(None, None, trh_otevren=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
