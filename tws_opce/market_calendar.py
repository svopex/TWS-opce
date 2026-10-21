"""
Kalendář obchodních dní - rozbor obchodních hodin, které vydává TWS,
a americké bank holidays podle pravidel Federal Reserve.

Čisté funkce bez závislosti na TWS API, takže je lze testovat samostatně.
Používá je naplánované zadání po otevření trhu, které o víkendu, ve svátek,
ve zkrácený obchodní den ani v bank holiday neobchoduje.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo


def parse_liquid_hours(text: str, tz: tzinfo) -> dict[date, datetime | None]:
    """
    Rozebere běžné obchodní hodiny (liquidHours) z ContractDetails v TWS.

    TWS je posílá jako řetězec na zhruba týden dopředu, například
    "20261126:CLOSED;20261127:0930-20261127:1300". Výsledkem je okamžik
    zavření burzy podle dne seance; None znamená, že burza ten den zavřená.
    Nesrozumitelné položky se přeskočí - den pak zůstane neznámý.
    ContractDetails.liquidSessions() z ib_async se nepoužívá: zavřené dny
    zahazuje (svátek by nešel odlišit od dne mimo rozvrh) a jediná vadná
    položka shodí celý rozbor.

    text - liquidHours z ContractDetails
    tz   - časová zóna rozvrhu (ContractDetails.timeZoneId)
    """
    zavreni: dict[date, datetime | None] = {}
    for polozka in text.split(";"):
        try:
            den_text, _, cas = polozka.strip().partition(":")
            den = datetime.strptime(den_text, "%Y%m%d").date()
            if cas == "CLOSED":
                # Zavřený den nepřebije seanci téhož dne zapsanou zvlášť
                zavreni.setdefault(den, None)
                continue
            # Seance má tvar "0930-20261127:1300" - za pomlčkou je konec
            # i s datem, protože seance může přecházet přes půlnoc
            konec = datetime.strptime(cas.split("-", 1)[1], "%Y%m%d:%H%M").replace(tzinfo=tz)
        except (ValueError, IndexError):
            continue
        # Více seancí téhož dne (polední přestávka) - rozhoduje ta poslední
        dosavadni = zavreni.get(den)
        if dosavadni is None or konec > dosavadni:
            zavreni[den] = konec
    return zavreni


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """
    N-tý den v týdnu v měsíci, například třetí pondělí v lednu.

    weekday - den v týdnu jako datetime.weekday() (0 = pondělí)
    n       - pořadí od začátku měsíce; -1 znamená poslední v měsíci
    """
    if n > 0:
        prvni = date(year, month, 1)
        posun = (weekday - prvni.weekday()) % 7
        return prvni + timedelta(days=posun + 7 * (n - 1))
    # Poslední výskyt se hledá od posledního dne měsíce zpět
    dalsi_mesic = date(year + month // 12, month % 12 + 1, 1)
    posledni = dalsi_mesic - timedelta(days=1)
    return posledni - timedelta(days=(posledni.weekday() - weekday) % 7)


def us_bank_holidays(year: int) -> dict[date, str]:
    """
    Bank holidays Federal Reserve v daném roce podle dne, kdy je banky drží.

    Burza má většinu z nich zavřenou také, otevřená bývá jen o Columbus Day
    a Veterans Day. Svátek s pevným datem, který připadne na neděli, se drží
    v pondělí; na sobotu se Fed nepřesouvá (víkend je mimo obchod stejně).
    """
    svatky = {
        date(year, 1, 1): "New Year's Day",
        _nth_weekday(year, 1, 0, 3): "Martin Luther King Jr. Day",
        _nth_weekday(year, 2, 0, 3): "Washington's Birthday",
        _nth_weekday(year, 5, 0, -1): "Memorial Day",
        date(year, 6, 19): "Juneteenth",
        date(year, 7, 4): "Independence Day",
        _nth_weekday(year, 9, 0, 1): "Labor Day",
        _nth_weekday(year, 10, 0, 2): "Columbus Day",
        date(year, 11, 11): "Veterans Day",
        _nth_weekday(year, 11, 3, 4): "Thanksgiving Day",
        date(year, 12, 25): "Christmas Day",
    }
    return {
        (den + timedelta(days=1) if den.weekday() == 6 else den): nazev
        for den, nazev in svatky.items()
    }


def non_trading_reason(
    day: date,
    closes: dict[date, datetime | None],
    normal_close: time,
    tz: tzinfo,
) -> str | None:
    """
    Důvod, proč den není řádným obchodním dnem, nebo None.

    Neobchoduje se o víkendu, v den, kdy je burza zavřená, ve zkrácený
    obchodní den (burza zavírá dřív než obvykle) a v bank holiday. Den,
    který rozvrh z TWS nepokrývá, se posuzuje jen podle víkendu a bank
    holidays - o zbytku rozhodne volající.

    day          - posuzovaný den (v časové zóně burzy)
    closes       - zavření burzy podle dne (viz parse_liquid_hours)
    normal_close - obvyklý čas zavření burzy (trading.exchange_close_time)
    tz           - časová zóna burzy, ve které se čas zavření porovnává
    """
    if day.weekday() >= 5:
        return "víkend"
    if day in closes:
        zavreni = closes[day]
        if zavreni is None:
            return "burza je zavřená (svátek)"
        zavreni = zavreni.astimezone(tz)
        if zavreni.time() < normal_close:
            return f"zkrácené obchodování (burza zavírá v {zavreni:%H:%M})"
    svatek = us_bank_holidays(day.year).get(day)
    if svatek:
        return f"bank holiday ({svatek})"
    return None
