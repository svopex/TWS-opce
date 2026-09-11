"""
Testy kontroly propásnutého vstupu podle minutových svíček podkladu.

Kontrolu si žádá jen dialog načtení pozic ze souboru (příznakem v zadání),
zda a od kdy se svíčky procházejí, říká konfigurace import.entry_cross_check.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import BarData

from tests.zaklad import ZakladEnginu
from tws_opce import calc
from tws_opce.models import FlowRequest, FlowState

NY = ZoneInfo("America/New_York")


def svicka(hodina: int, minuta: int, high: float, low: float, den: int = 19) -> BarData:
    """
    Minutová svíčka z 19. 8. 2026 v čase burzy. TWS ji posílá s časem v UTC
    (formatDate=2), proto se převádí - engine si ji má pro hlášku přepočítat
    zpět na čas burzy.
    """
    cas = datetime(2026, 8, den, hodina, minuta, tzinfo=NY).astimezone(timezone.utc)
    return BarData(date=cas, open=low, high=high, low=low, close=high)


class TestVypoctuPrurazu(unittest.TestCase):
    """Čistá funkce hledající první svíčku, která překročila vstup."""

    def test_long_prurazi_knotem_nahoru(self):
        bars = [svicka(8, 0, 231.0, 229.0), svicka(8, 1, 232.4, 230.0), svicka(8, 2, 233.0, 232.5)]
        nalezeno = calc.first_entry_cross("C", 232.0, bars)
        self.assertIsNotNone(nalezeno)
        bar, cena = nalezeno
        # První svíčka, jejíž high dosáhlo vstupu, ne ta nejvyšší
        self.assertIs(bar, bars[1])
        self.assertEqual(cena, 232.4)

    def test_short_prurazi_knotem_dolu(self):
        bars = [svicka(8, 0, 231.0, 229.0), svicka(8, 1, 230.0, 227.9)]
        nalezeno = calc.first_entry_cross("P", 228.0, bars)
        self.assertIsNotNone(nalezeno)
        self.assertIs(nalezeno[0], bars[1])
        self.assertEqual(nalezeno[1], 227.9)

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
            date=datetime(2026, 8, 19, 12, 0, tzinfo=NY), open=231.0, high=232.5, low=230.5, close=231.0
        )
        self.assertIsNotNone(calc.first_entry_cross("C", 232.0, [bar]))

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
        """CALL se vstupem 232 nad cenou 230; kontrolu svíček žádá zadání."""
        pozadavek = FlowRequest(
            symbol="AAPL", entry_price=232.0, profit_target=235.0, entry_cross_check=True
        )
        for klic, hodnota in zmeny.items():
            setattr(pozadavek, klic, hodnota)
        return await self.engine.start_flow(pozadavek)

    async def test_pruraz_v_premarketu_ukonci_obchod_jako_propasnuty(self):
        # V 8:12 čas burzy podklad vystoupal nad vstup a zase se vrátil
        self.ib.bars = [svicka(8, 11, 231.5, 230.0), svicka(8, 12, 232.3, 231.0), svicka(9, 40, 230.5, 229.8)]
        flow = await self.zaloz()
        self.assertEqual(flow.state, FlowState.MISSED)
        self.assertIn("vstup propásnut", flow.message)
        # Hláška nese čas svíčky v čase burzy a cenu, která vstup překročila
        self.assertIn("08:12", flow.message)
        self.assertIn("232.3", flow.message)
        # Do trhu nešel žádný příkaz, obchod ale zůstal v přehledu
        self.assertEqual(self.ib.placed, [])
        self.assertIn(flow.id, self.engine.flows)
        self.assertFalse(flow.state.is_active)
        # Odběry ukončený obchod nedrží (uvolnění maže kontrakty)
        self.assertIsNone(flow.underlying_contract)
        self.assertIsNone(flow.option_contract)

    async def test_short_pruraz_dolu(self):
        self.ib.greek_delta = -0.35
        self.ib.bars = [svicka(7, 30, 229.0, 227.5)]
        flow = await self.engine.start_flow(
            FlowRequest(symbol="AAPL", entry_price=228.0, profit_target=225.0, entry_cross_check=True)
        )
        self.assertEqual(flow.right, "P")
        self.assertEqual(flow.state, FlowState.MISSED)
        self.assertIn("low 227.5", flow.message)

    async def test_bez_prurazu_se_obchod_zada(self):
        self.ib.bars = [svicka(8, 12, 231.9, 230.0)]
        flow = await self.zaloz()
        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(len(self.ib.placed), 1)

    async def test_bez_priznaku_se_svicky_neprochazi(self):
        # Formulář zadání ani monitoring kontrolu nežádají - i přes průraz
        # v pre-marketu se obchod zadá a TWS se na svíčky vůbec neptá
        self.ib.bars = [svicka(8, 12, 232.3, 231.0)]
        flow = await self.zaloz(entry_cross_check=False)
        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(self.ib.bars_requests, 0)

    async def test_vypnuta_konfigurace_kontrolu_preskoci(self):
        self.cfg.import_.entry_cross_check = False
        self.ib.bars = [svicka(8, 12, 232.3, 231.0)]
        flow = await self.zaloz()
        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(self.ib.bars_requests, 0)

    async def test_svicky_pred_zacatkem_kontroly_se_nepocitaji(self):
        # Kontrola od 9:30 pre-market nevidí
        self.cfg.import_.entry_cross_check_from = "09:30"
        self.ib.bars = [svicka(8, 12, 232.3, 231.0), svicka(9, 35, 231.0, 230.0)]
        flow = await self.zaloz()
        self.assertEqual(flow.state, FlowState.ARMED)

    async def test_zacatek_kontroly_v_budoucnu_nic_neprochazi(self):
        # Je 9:45 a kontrola má začínat až v 10:00 - není co procházet
        self.cfg.import_.entry_cross_check_from = "10:00"
        self.ib.bars = [svicka(8, 12, 232.3, 231.0)]
        flow = await self.zaloz()
        self.assertEqual(flow.state, FlowState.ARMED)
        self.assertEqual(self.ib.bars_requests, 0)

    async def test_chyba_dotazu_obchod_nezastavi(self):
        # Bez historických dat se obchod zadá a důvod skončí v logu -
        # živá cena podkladu vstup hlídá dál
        self.ib.bars_error = RuntimeError("HMDS query returned no data")
        flow = await self.zaloz()
        self.assertEqual(flow.state, FlowState.ARMED)
        zpravy = " ".join(text for _, text in self.engine.events)
        self.assertIn("kontrola propásnutého vstupu podle svíček se nezdařila", zpravy)

    async def test_svicky_se_pro_tyz_ticker_stahuji_jednou(self):
        # Dávka se na tentýž podklad ptá opakovaně (long i short řádek);
        # TWS shodné historické dotazy do 15 s odmítá, proto se drží v paměti
        self.ib.bars = [svicka(8, 12, 231.0, 230.0)]
        await self.engine.entry_crossed(await self.ib.qualify_stock("AAPL"), "C", 232.0)
        await self.engine.entry_crossed(await self.ib.qualify_stock("AAPL"), "P", 228.0)
        self.assertEqual(self.ib.bars_requests, 1)

    async def test_pamet_svicek_neprezije_zmenu_zacatku(self):
        kontrakt = await self.ib.qualify_stock("AAPL")
        await self.engine.entry_crossed(kontrakt, "C", 232.0)
        self.cfg.import_.entry_cross_check_from = "04:00"
        await self.engine.entry_crossed(kontrakt, "C", 232.0)
        self.assertEqual(self.ib.bars_requests, 2)

    async def test_propasnuty_nahrazuje_cekajici_obchod(self):
        # Opakované zadání téhož řádku nahrazuje čekající obchod; když mezitím
        # podklad vstup překročil, náhradou je obchod ukončený jako propásnutý
        puvodni = await self.zaloz()
        self.assertEqual(puvodni.state, FlowState.ARMED)
        self.ib.bars = [svicka(9, 40, 232.6, 231.0)]
        # Druhé zadání přichází později než paměť svíček dovolí
        self.engine._bars_cache.clear()
        novy = await self.zaloz()
        self.assertEqual(novy.state, FlowState.MISSED)
        self.assertNotIn(puvodni.id, self.engine.flows)
        self.assertEqual(len(self.ib.cancelled), 1)

    def test_zacatek_kontroly_je_dnesni_cas_burzy(self):
        zacatek = self.engine.entry_cross_start()
        self.assertEqual(zacatek, datetime(2026, 8, 19, 0, 0, tzinfo=NY))
        self.cfg.import_.entry_cross_check = False
        self.assertIsNone(self.engine.entry_cross_start())


if __name__ == "__main__":
    unittest.main()
