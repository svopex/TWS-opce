"""
Testy čtení tržních dat ze skutečné implementace IBService.
Ticker se plní ručně, spojení s TWS není potřeba - ověřuje se, že aplikace
používá pole a metody ib_async správně.
"""

from __future__ import annotations

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
