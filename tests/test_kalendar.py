"""
Testy kalendáře obchodních dní - rozbor obchodních hodin z TWS, americké
bank holidays a jejich ověřování v enginu pro zadání po otevření trhu.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.zaklad import BURZA, HODINY_THANKSGIVING, ZakladEnginu
from tws_opce import market_calendar

# Obvyklé zavření burzy (trading.exchange_close_time)
ZAVRENI = time(16, 0)


class TestRozboruObchodnichHodin(unittest.TestCase):
    """Řetězec liquidHours z TWS se rozebere na zavření burzy podle dne."""

    def test_zavreny_zkraceny_i_bezny_den(self):
        zavreni = market_calendar.parse_liquid_hours(HODINY_THANKSGIVING, BURZA)
        self.assertIsNone(zavreni[date(2026, 11, 26)])
        self.assertEqual(zavreni[date(2026, 11, 27)], datetime(2026, 11, 27, 13, 0, tzinfo=BURZA))
        self.assertEqual(zavreni[date(2026, 11, 30)], datetime(2026, 11, 30, 16, 0, tzinfo=BURZA))

    def test_vice_seanci_dne_rozhoduje_posledni(self):
        zavreni = market_calendar.parse_liquid_hours(
            "20261130:0930-20261130:1200;20261130:1300-20261130:1600", BURZA
        )
        self.assertEqual(zavreni[date(2026, 11, 30)].hour, 16)

    def test_nesrozumitelna_polozka_se_preskoci(self):
        zavreni = market_calendar.parse_liquid_hours("nesmysl;20261130:0930-20261130:1600;", BURZA)
        self.assertEqual(list(zavreni), [date(2026, 11, 30)])


class TestBankHolidays(unittest.TestCase):
    """Bank holidays Federal Reserve včetně přesunu z neděle na pondělí."""

    def test_svatky_roku_2026(self):
        svatky = market_calendar.us_bank_holidays(2026)
        self.assertEqual(svatky[date(2026, 1, 19)], "Martin Luther King Jr. Day")
        self.assertEqual(svatky[date(2026, 5, 25)], "Memorial Day")
        self.assertEqual(svatky[date(2026, 9, 7)], "Labor Day")
        self.assertEqual(svatky[date(2026, 10, 12)], "Columbus Day")
        self.assertEqual(svatky[date(2026, 11, 11)], "Veterans Day")
        self.assertEqual(svatky[date(2026, 11, 26)], "Thanksgiving Day")
        self.assertEqual(len(svatky), 11)

    def test_nedelni_svatek_se_drzi_v_pondeli(self):
        # 4. 7. 2027 je neděle - banky mají zavřeno v pondělí 5. 7.
        svatky = market_calendar.us_bank_holidays(2027)
        self.assertEqual(svatky[date(2027, 7, 5)], "Independence Day")
        self.assertNotIn(date(2027, 7, 4), svatky)

    def test_sobotni_svatek_se_nepresouva(self):
        # 25. 12. 2027 je sobota - Fed v pátek předtím nezavírá
        self.assertNotIn(date(2027, 12, 24), market_calendar.us_bank_holidays(2027))


class TestDuvoduNeobchodovani(unittest.TestCase):
    """Posouzení dne: víkend, zavřená burza, zkrácený den, bank holiday."""

    def setUp(self) -> None:
        self.zavreni = market_calendar.parse_liquid_hours(HODINY_THANKSGIVING, BURZA)

    def duvod(self, den: date) -> str | None:
        return market_calendar.non_trading_reason(den, self.zavreni, ZAVRENI, BURZA)

    def test_vikend(self):
        self.assertEqual(self.duvod(date(2026, 11, 28)), "víkend")

    def test_zavrena_burza(self):
        self.assertEqual(self.duvod(date(2026, 11, 26)), "burza je zavřená (svátek)")

    def test_zkraceny_den(self):
        self.assertEqual(
            self.duvod(date(2026, 11, 27)), "zkrácené obchodování (burza zavírá v 13:00)"
        )

    def test_bank_holiday_s_otevrenou_burzou(self):
        # O Columbus Day burza obchoduje, banky ale mají zavřeno
        self.assertEqual(self.duvod(date(2026, 10, 12)), "bank holiday (Columbus Day)")

    def test_bezny_den(self):
        self.assertIsNone(self.duvod(date(2026, 11, 30)))
        # Den mimo rozvrh posoudí jen víkend a bank holidays
        self.assertIsNone(self.duvod(date(2026, 12, 1)))


class TestOvereniObchodnihoDne(ZakladEnginu):
    """Engine ověřuje den proti obchodním hodinám načteným z TWS."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg.import_.plan_trading_days_only = True
        self.podvrhni_cas_burzy(18, 0, den=25, mesic=11)

    async def test_overeni_nacte_cerstve_hodiny(self):
        self.ib.liquid_hours_text = HODINY_THANKSGIVING
        duvod = await self.engine.verify_trading_day(date(2026, 11, 27))
        self.assertEqual(duvod, "zkrácené obchodování (burza zavírá v 13:00)")
        self.assertEqual(self.ib.liquid_hours_requests, 1)
        self.assertIsNone(await self.engine.verify_trading_day(date(2026, 11, 30)))

    async def test_bez_hodin_z_tws_se_neobchoduje(self):
        # Den, který nepokrývá žádný rozvrh, nejde ověřit - plán neobchoduje
        duvod = await self.engine.verify_trading_day(date(2026, 11, 30))
        self.assertIn("nepodařilo načíst", duvod)

    async def test_bez_odpovedi_plati_drive_nactene(self):
        self.ib.liquid_hours_text = HODINY_THANKSGIVING
        await self.engine._load_trading_hours()
        self.ib.liquid_hours_text = ""
        self.assertIsNone(await self.engine.verify_trading_day(date(2026, 11, 30)))

    async def test_smycka_obnovuje_hodiny_jen_jednou_za_interval(self):
        self.ib.liquid_hours_text = HODINY_THANKSGIVING
        await self.engine._tick()
        await self.engine._tick()
        self.assertEqual(self.ib.liquid_hours_requests, 1)
        self.assertIn(date(2026, 11, 27), self.engine.trading_hours)

    async def test_vypnuta_kontrola_hodiny_nenacita(self):
        self.cfg.import_.plan_trading_days_only = False
        await self.engine._tick()
        self.assertEqual(self.ib.liquid_hours_requests, 0)


if __name__ == "__main__":
    unittest.main()
