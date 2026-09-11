"""
Testy kontroly propásnutého vstupu podle minutových svíček podkladu.

Okamžik, od kterého se svíčky procházejí, dodává zadání (dialog načtení
pozic ze souboru); zda a od kdy, říká konfigurace import.entry_cross_check.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import BarData

from tests.zaklad import BURZA, ZakladEnginu, svicka
from tws_opce import calc
from tws_opce.models import FlowRequest, FlowState


class TestVypoctuPrurazu(unittest.TestCase):
    """Čistá funkce hledající první svíčku, která překročila vstup."""

    def test_long_prurazi_knotem_nahoru(self):
        bars = [svicka(8, 0, 231.0, 229.0), svicka(8, 1, 232.4, 230.0), svicka(8, 2, 233.0, 232.5)]
        # První svíčka, jejíž high dosáhlo vstupu, ne ta nejvyšší
        self.assertIs(calc.first_entry_cross("C", 232.0, bars), bars[1])

    def test_short_prurazi_knotem_dolu(self):
        bars = [svicka(8, 0, 231.0, 229.0), svicka(8, 1, 230.0, 227.9)]
        self.assertIs(calc.first_entry_cross("P", 228.0, bars), bars[1])

    def test_dotyk_vstupu_staci(self):
        # Cenová podmínka v TWS by se na přesné hodnotě spustila také
        self.assertIsNotNone(calc.first_entry_cross("C", 232.0, [svicka(8, 0, 232.0, 230.0)]))
        self.assertIsNotNone(calc.first_entry_cross("P", 228.0, [svicka(8, 0, 230.0, 228.0)]))

    def test_bez_prurazu_vraci_none(self):
        bars = [svicka(8, 0, 231.0, 229.0), svicka(8, 1, 231.9, 229.5)]
        self.assertIsNone(calc.first_entry_cross("C", 232.0, bars))
        self.assertIsNone(calc.first_entry_cross("P", 228.0, bars))
        self.assertIsNone(calc.first_entry_cross("C", 232.0, []))

    def test_close_na_spravne_strane_nestaci(self):
        # Svíčka se přes vstup přehoupla a vrátila - rozhoduje knot, ne close
        bar = BarData(
            date=datetime(2026, 8, 19, 12, 0, tzinfo=BURZA), open=231.0, high=232.5, low=230.5, close=231.0
        )
        self.assertIs(calc.first_entry_cross("C", 232.0, [bar]), bar)

    def test_neplatne_ceny_se_preskoci(self):
        # TWS občas pošle -1 nebo NaN místo ceny; taková svíčka nic neříká
        bars = [svicka(8, 0, -1.0, -1.0), svicka(8, 1, float("nan"), float("nan"))]
        self.assertIsNone(calc.first_entry_cross("C", 232.0, bars))
        self.assertIsNone(calc.first_entry_cross("P", 228.0, bars))


class TestZadaniSeSvickami(ZakladEnginu):
    """Založení obchodu s kontrolou svíček - jak s ní zachází engine."""

    def setUp(self) -> None:
        super().setUp()
        # Středa 19. 8. 2026, 9:45 čas burzy - svíčky z pre-marketu jsou za námi
        self.podvrhni_cas_burzy(9, 45)
        self.ib.price_underlying = 230.0

    async def zaloz(self, **zmeny):
        """
        CALL se vstupem 232 nad cenou 230; okamžik kontroly svíček bere
        zadání z konfigurace, jako to dělá dialog.
        """
        hodnoty = dict(
            symbol="AAPL",
            entry_price=232.0,
            profit_target=235.0,
            entry_cross_since=self.engine.entry_cross_start(),
        )
        return await self.engine.start_flow(FlowRequest(**{**hodnoty, **zmeny}))

    async def test_pruraz_v_premarketu_zadani_odmitne(self):
        # V 8:12 čas burzy podklad vystoupal nad vstup a zase se vrátil -
        # zadání se odmítne stejně jako při vstupu překonaném podle živé ceny
        self.ib.bars = [svicka(8, 11, 231.5, 230.0), svicka(8, 12, 232.3, 231.0), svicka(9, 40, 230.5, 229.8)]
        with self.assertRaises(ValueError) as ctx:
            await self.zaloz()
        # Hláška nese čas svíčky v čase burzy a cenu, která vstup překročila
        self.assertIn("propásnutý", str(ctx.exception))
        self.assertIn("08:12", str(ctx.exception))
        self.assertIn("high 232.3", str(ctx.exception))
        # Do trhu nešel žádný příkaz a v přehledu žádný obchod nevznikl
        self.assertEqual(self.ib.placed, [])
        self.assertEqual(self.engine.flows, {})

    async def test_short_pruraz_dolu(self):
        self.ib.greek_delta = -0.35
        self.ib.bars = [svicka(7, 30, 229.0, 227.5)]
        with self.assertRaises(ValueError) as ctx:
            await self.zaloz(entry_price=228.0, profit_target=225.0)
        self.assertIn("low 227.5", str(ctx.exception))

    async def test_bez_prurazu_se_obchod_zada(self):
        self.ib.bars = [svicka(8, 12, 231.9, 230.0)]
        flow = await self.zaloz()
        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(len(self.ib.placed), 1)

    async def test_bez_okamziku_se_svicky_neprochazi(self):
        # Formulář zadání ani monitoring okamžik nedodávají - i přes průraz
        # v pre-marketu se obchod zadá a TWS se na svíčky vůbec neptá
        self.ib.bars = [svicka(8, 12, 232.3, 231.0)]
        flow = await self.zaloz(entry_cross_since=None)
        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(self.ib.bars_requests, 0)

    async def test_svicky_pred_zacatkem_kontroly_se_nepocitaji(self):
        # Kontrola od 9:30 pre-market nevidí
        self.cfg.import_.entry_cross_check_from = "09:30"
        self.ib.bars = [svicka(8, 12, 232.3, 231.0), svicka(9, 35, 231.0, 230.0)]
        flow = await self.zaloz()
        self.assertEqual(flow.state, FlowState.ARMED)

    async def test_chyba_dotazu_obchod_nezastavi(self):
        # Bez historických dat se obchod zadá a důvod skončí v logu -
        # živá cena podkladu vstup hlídá dál
        self.ib.bars_error = RuntimeError("HMDS query returned no data")
        flow = await self.zaloz()
        self.assertEqual(flow.state, FlowState.ARMED)
        zpravy = " ".join(text for _, text in self.engine.events)
        self.assertIn("kontrola propásnutého vstupu podle svíček se nezdařila", zpravy)

    async def test_odmitnute_zadani_necha_cekajici_obchod(self):
        # Opakované zadání téhož řádku nahrazuje čekající obchod až po všech
        # kontrolách - odmítnuté zadání ho nechá v trhu beze změny
        puvodni = await self.zaloz()
        self.assertEqual(puvodni.state, FlowState.ARMED)
        self.ib.bars = [svicka(9, 40, 232.6, 231.0)]
        # Druhé zadání přichází později, než paměť svíček dovolí
        self.ib._bars_cache.clear()
        with self.assertRaises(ValueError):
            await self.zaloz()
        self.assertIn(puvodni.id, self.engine.flows)
        self.assertEqual(puvodni.state, FlowState.ARMED)
        self.assertEqual(self.ib.cancelled, [])

    def test_zacatek_kontroly_je_dnesni_cas_burzy(self):
        self.assertEqual(self.engine.entry_cross_start(), datetime(2026, 8, 19, 0, 0, tzinfo=BURZA))
        # Vypnutá kontrola i začátek, který dnes teprve přijde, nedávají co procházet
        self.cfg.import_.entry_cross_check_from = "10:00"
        self.assertIsNone(self.engine.entry_cross_start())
        self.cfg.import_.entry_cross_check_from = "00:00"
        self.cfg.import_.entry_cross_check = False
        self.assertIsNone(self.engine.entry_cross_start())


class TestPametiSvicek(ZakladEnginu):
    """Paměť stažených svíček v TWS vrstvě - proti pacingu historických dotazů."""

    def setUp(self) -> None:
        super().setUp()
        self.podvrhni_cas_burzy(9, 45)
        self.pulnoc = datetime(2026, 8, 19, 0, 0, tzinfo=BURZA)

    async def test_tyz_ticker_a_zacatek_se_stahuje_jednou(self):
        # Dávka se na tentýž podklad ptá opakovaně (long i short řádek)
        kontrakt = await self.ib.qualify_stock("AAPL")
        await self.ib.minute_bars(kontrakt, self.pulnoc)
        await self.ib.minute_bars(kontrakt, self.pulnoc)
        self.assertEqual(self.ib.bars_requests, 1)

    async def test_jiny_zacatek_se_stahuje_znovu(self):
        kontrakt = await self.ib.qualify_stock("AAPL")
        await self.ib.minute_bars(kontrakt, self.pulnoc)
        await self.ib.minute_bars(kontrakt, datetime(2026, 8, 19, 4, 0, tzinfo=BURZA))
        self.assertEqual(self.ib.bars_requests, 2)

    async def test_svicky_pred_zacatkem_se_odfiltruji(self):
        self.ib.bars = [svicka(3, 59, 232.0, 231.0), svicka(4, 0, 231.0, 230.0)]
        kontrakt = await self.ib.qualify_stock("AAPL")
        bars = await self.ib.minute_bars(kontrakt, datetime(2026, 8, 19, 4, 0, tzinfo=BURZA))
        self.assertEqual(bars, [self.ib.bars[1]])


if __name__ == "__main__":
    unittest.main()
