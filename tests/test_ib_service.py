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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import OptionComputation, Stock, Ticker

from tws_opce.config import AppConfig
from tws_opce.ib_service import IBService, valid_price

NAN = float("nan")


def vloz_ticker(sluzba: IBService, **hodnoty) -> Stock:
    """
    Vloží do služby kontrakt s předvyplněnými tržními daty.
    Ceny se nastavují až po vytvoření Tickeru - ib_async je v __post_init__
    přepisuje na nevyplněné hodnoty, takže konstruktorem je předat nelze.
    """
    kontrakt = Stock("AAPL", "SMART", "USD")
    kontrakt.conId = 265598
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


if __name__ == "__main__":
    unittest.main(verbosity=2)


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
