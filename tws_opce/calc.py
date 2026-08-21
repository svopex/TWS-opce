"""
Čisté výpočetní funkce obchodní logiky (bez závislosti na TWS API).
Díky oddělení je lze testovat samostatně.
"""

from __future__ import annotations

import math
from datetime import date, datetime

# Multiplikátor standardní akciové opce (1 kontrakt = 100 kusů podkladu)
OPTION_MULTIPLIER = 100


def determine_right(current_price: float, entry_price: float) -> str:
    """
    Určí typ opce podle vztahu aktuální ceny podkladu a zadané vstupní ceny.
    Vstup nad aktuální cenou = čeká se průraz nahoru -> CALL ('C'),
    vstup pod aktuální cenou = čeká se průraz dolů -> PUT ('P').
    """
    if entry_price >= current_price:
        return "C"
    return "P"


def default_stop_loss(entry_price: float, profit_target: float, sl_to_pt_ratio: float) -> float:
    """
    Dopočítá SL na podkladu, pokud jej uživatel nezadal.
    Vzdálenost SL od vstupu = vzdálenost PT od vstupu * poměr z konfigurace,
    na opačnou stranu než PT.
    """
    distance = abs(profit_target - entry_price) * sl_to_pt_ratio
    # SL leží vždy na opačné straně vstupu než PT
    if profit_target >= entry_price:
        return entry_price - distance
    return entry_price + distance


def spread_pct(bid: float | None, ask: float | None) -> float | None:
    """
    Spočítá spread v procentech ze středu trhu: (ASK - BID) / MID * 100.
    Vrací None, pokud nejsou k dispozici platné kotace.
    """
    if bid is None or ask is None:
        return None
    if not (math.isfinite(bid) and math.isfinite(ask)):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (ask + bid) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid * 100.0


def suggest_quantity_for_loss(
    risk_amount: float,
    loss_per_contract: float,
    min_quantity: int = 1,
    max_quantity: int = 100,
) -> int:
    """
    Doporučené množství kontraktů ze známé ztráty na jeden kontrakt v USD.
    Množství = riskovaná částka / ztráta na kontrakt, zaokrouhleno dolů
    a oříznuto do povoleného rozsahu z konfigurace.
    """
    if risk_amount <= 0 or loss_per_contract <= 0 or not math.isfinite(loss_per_contract):
        return min_quantity

    qty = int(math.floor(risk_amount / loss_per_contract))
    return max(min_quantity, min(qty, max_quantity))


def suggest_quantity(
    risk_amount: float,
    entry_price: float,
    stop_loss: float,
    delta: float,
    min_quantity: int = 1,
    max_quantity: int = 100,
) -> int:
    """
    Doporučené množství opčních kontraktů při SL zadaném na podkladu.
    Odhadovaná ztráta na 1 kontrakt = pohyb podkladu ke SL * |delta| * multiplikátor.
    """
    move = abs(entry_price - stop_loss)
    d = abs(delta)
    if move <= 0 or d <= 0:
        return min_quantity

    return suggest_quantity_for_loss(
        risk_amount, move * d * OPTION_MULTIPLIER, min_quantity, max_quantity
    )


def option_profit_limit(fill_price: float, profit_usd: float, min_tick: float) -> float:
    """
    Limitní cena prodeje opce pro zisk zadaný v USD na jeden kontrakt.
    Zisk 10 USD na kontraktu o 100 kusech = posun ceny opce o 0,10.
    """
    return round_to_tick(fill_price + profit_usd / OPTION_MULTIPLIER, min_tick)


def option_loss_stop(fill_price: float, loss_usd: float, min_tick: float) -> float:
    """
    Stop cena prodeje opce pro ztrátu zadanou v USD na jeden kontrakt.
    Nemůže klesnout pod jeden tik - opce nemá zápornou cenu; je-li povolená
    ztráta větší než celá zaplacená prémie, stop stojí na nejnižší možné ceně.
    """
    tick = min_tick if min_tick and min_tick > 0 else 0.01
    stop = round_to_tick(fill_price - loss_usd / OPTION_MULTIPLIER, min_tick)
    return max(stop, tick)


def max_option_loss(fill_price: float, min_tick: float) -> float:
    """
    Největší ztráta na jeden kontrakt v USD, kterou SL na opci může způsobit.
    Stop nemůže klesnout pod jeden tik (opce nemá zápornou cenu), takže
    ztráta je omezená zaplacenou prémií sníženou o tento tik.
    """
    tick = min_tick if min_tick and min_tick > 0 else 0.01
    return max((fill_price - tick) * OPTION_MULTIPLIER, 0.0)


def entry_limit_price(
    order_type: str,
    bid: float | None,
    ask: float | None,
    ask_tolerance_pct: float,
) -> float | None:
    """
    Limitní cena nákupního příkazu podle typu z konfigurace.
    LMT_ASK = ASK navýšený o toleranci, LMT_MID = střed trhu, MKT = bez limitu (None).
    """
    if order_type == "MKT":
        return None

    if order_type == "LMT_ASK":
        if ask is None or not math.isfinite(ask) or ask <= 0:
            return None
        return ask * (1.0 + ask_tolerance_pct / 100.0)

    if order_type == "LMT_MID":
        if bid is None or ask is None or not (math.isfinite(bid) and math.isfinite(ask)):
            return None
        if bid <= 0 or ask <= 0:
            return None
        return (bid + ask) / 2.0

    return None


def exit_limit_price(bid: float | None, bid_tolerance_pct: float) -> float | None:
    """Limitní cena prodejního příkazu - BID snížený o toleranci z konfigurace."""
    if bid is None or not math.isfinite(bid) or bid <= 0:
        return None
    return bid * (1.0 - bid_tolerance_pct / 100.0)


def round_to_tick(price: float, min_tick: float) -> float:
    """Zaokrouhlí cenu na nejbližší násobek minimálního tiku kontraktu."""
    if min_tick is None or min_tick <= 0 or not math.isfinite(min_tick):
        return round(price, 2)
    steps = round(price / min_tick)
    # Počet desetinných míst se odvodí od velikosti tiku, aby nevznikaly zbytky z float aritmetiky
    decimals = max(0, min(6, int(round(-math.log10(min_tick))) + 2))
    return round(steps * min_tick, decimals)


def nearest_strike(strikes: list[float], target: float) -> float | None:
    """Najde strike nejbližší zadané ceně. Vrací None při prázdném seznamu."""
    if not strikes:
        return None
    return min(strikes, key=lambda s: (abs(s - target), s))


def days_to_expiry(expiration: str, today: date | None = None) -> int:
    """Počet dní do expirace ze zápisu YYYYMMDD."""
    ref = today or date.today()
    exp = datetime.strptime(expiration, "%Y%m%d").date()
    return (exp - ref).days


def select_expiration(
    expirations: list[str],
    mode: str,
    min_dte: int,
    fixed_date: str = "",
    today: date | None = None,
) -> str | None:
    """
    Vybere expiraci podle konfigurace.
    mode = 'fixed' vrátí zadané datum, pokud je v nabídce.
    mode = 'nearest' vrátí nejbližší expiraci s počtem dní >= min_dte;
    pokud taková neexistuje, vrátí nejvzdálenější dostupnou.
    """
    if not expirations:
        return None

    available = sorted(expirations)

    if mode == "fixed":
        return fixed_date if fixed_date in available else None

    ref = today or date.today()
    # Již proběhlé expirace se přeskakují, dále platí minimální počet dní z konfigurace
    suitable = [e for e in available if days_to_expiry(e, ref) >= max(0, min_dte)]
    if suitable:
        return suitable[0]
    return available[-1]


def levels_sane(
    entry_price: float,
    profit_target: float,
    stop_loss: float,
    max_distance_pct: float = 50.0,
    pt_on_underlying: bool = True,
    sl_on_underlying: bool = True,
) -> bool:
    """
    Ověří, že cíl a stop leží v rozumné vzdálenosti od vstupní ceny.

    Chrání před obchodem s poškozenými úrovněmi - například z dřívější
    chyby ve výpočtu, kdy se cíl opakovaným násobením vyšplhal do milionů.
    Úroveň zadaná na opci (USD na kontrakt) nemá ke vstupní ceně podkladu
    vztah - u ní se kontroluje jen, že je konečná a nezáporná; zisk (PT)
    musí být kladný, ztráta (SL) smí být nulová - to je stop na break even.
    """
    if entry_price <= 0:
        return False
    for uroven, na_podkladu, nula_povolena in (
        (profit_target, pt_on_underlying, False),
        (stop_loss, sl_on_underlying, not sl_on_underlying),
    ):
        if not math.isfinite(uroven):
            return False
        if uroven < 0 or (uroven == 0 and not nula_povolena):
            return False
        if not na_podkladu:
            continue
        odchylka = abs(uroven - entry_price) / entry_price * 100.0
        if odchylka > max_distance_pct:
            return False
    return True


def entry_still_valid(right: str, current_price: float, entry_price: float) -> bool:
    """
    Ověří, že cena podkladu ještě nedosáhla vstupní úrovně.

    U CALL se čeká průraz nahoru, takže cena musí být pod vstupem; u PUT
    je to zrcadlově. Pokud už cena vstupní úroveň překonala, obchod ujel
    a nemá smysl jej do trhu zadávat - podmínka by se splnila okamžitě
    a nakupovalo by se za horší cenu.
    """
    if right == "C":
        return current_price < entry_price
    return current_price > entry_price


def condition_directions(right: str) -> tuple[bool, bool, bool]:
    """
    Směry cenových podmínek na podkladu pro daný typ opce.
    Vrací trojici (vstup, PT, SL), kde True znamená 'cena je vyšší nebo rovna'.
    CALL: vstup a PT nahoru, SL dolů. PUT: opačně.
    """
    if right == "C":
        return True, True, False
    return False, False, True


# ---------------------------------------------------------------------------
# Odhad delty z ceny opce
#
# TWS model greeks (a s nimi deltu) u opcí neposílá spolehlivě - podle
# nastavení účtu a předplatného dat nemusí dorazit vůbec. Delta je přitom
# potřeba pro výpočet množství kontraktů, proto se dá dopočítat z ceny opce:
# nejprve se z tržní ceny odvodí implikovaná volatilita, z ní pak delta
# podle modelu Black-Scholes.
# ---------------------------------------------------------------------------

# Meze pro hledání implikované volatility (0,1 % až 500 %)
_IV_MIN = 0.001
_IV_MAX = 5.0
# Dnešní expirace se počítá jako půl dne, aby vzorce nedělily nulou
_MIN_YEARS = 0.5 / 365.0


def norm_cdf(x: float) -> float:
    """Distribuční funkce standardního normálního rozdělení."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def years_to_expiry(expiration: str, today: date | None = None) -> float:
    """Doba do expirace v letech, nejméně půl dne."""
    days = days_to_expiry(expiration, today)
    return max(days, 0) / 365.0 + _MIN_YEARS


def black_scholes_price(
    spot: float, strike: float, years: float, rate: float, sigma: float, right: str
) -> float:
    """
    Teoretická cena opce podle modelu Black-Scholes.
    rate je bezriziková sazba v desetinném tvaru (0.04 = 4 %), sigma volatilita.
    """
    if years <= 0 or sigma <= 0:
        # Bez času nebo volatility zbývá pouze vnitřní hodnota
        return max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)

    d1 = (math.log(spot / strike) + (rate + sigma * sigma / 2.0) * years) / (sigma * math.sqrt(years))
    d2 = d1 - sigma * math.sqrt(years)
    discount = math.exp(-rate * years)

    if right == "C":
        return spot * norm_cdf(d1) - strike * discount * norm_cdf(d2)
    return strike * discount * norm_cdf(-d2) - spot * norm_cdf(-d1)


def black_scholes_delta(
    spot: float, strike: float, years: float, rate: float, sigma: float, right: str
) -> float:
    """Delta opce podle modelu Black-Scholes (u PUT záporná)."""
    if years <= 0 or sigma <= 0:
        # Bez času je delta buď 1 (v penězích), nebo 0 (mimo peníze)
        if right == "C":
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0

    d1 = (math.log(spot / strike) + (rate + sigma * sigma / 2.0) * years) / (sigma * math.sqrt(years))
    if right == "C":
        return norm_cdf(d1)
    return norm_cdf(d1) - 1.0


def implied_volatility(
    option_price: float, spot: float, strike: float, years: float, rate: float, right: str
) -> float | None:
    """
    Implikovaná volatilita odvozená z tržní ceny opce metodou půlení intervalu.
    Vrací None, pokud cena leží mimo rozsah, který model dokáže vysvětlit.
    """
    if option_price <= 0 or spot <= 0 or strike <= 0 or years <= 0:
        return None

    # Cena pod vnitřní hodnotou nebo nad cenou podkladu je pro model neřešitelná
    intrinsic = max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)
    if option_price < intrinsic or option_price > spot:
        return None

    low, high = _IV_MIN, _IV_MAX
    if black_scholes_price(spot, strike, years, rate, high, right) < option_price:
        return None

    # Padesát půlení dává přesnost hluboko pod rozlišení tržních cen
    for _ in range(50):
        mid = (low + high) / 2.0
        if black_scholes_price(spot, strike, years, rate, mid, right) < option_price:
            low = mid
        else:
            high = mid

    return (low + high) / 2.0


def estimate_delta(
    option_price: float,
    spot: float,
    strike: float,
    expiration: str,
    rate_pct: float,
    right: str,
    today: date | None = None,
) -> float | None:
    """
    Odhad delty opce z její tržní ceny.
    Slouží jako náhrada, když TWS nepošle model greeks.
    Vrací None, pokud odhad nelze spolehlivě provést.
    """
    if option_price is None or spot is None or option_price <= 0 or spot <= 0:
        return None

    years = years_to_expiry(expiration, today)
    rate = rate_pct / 100.0

    sigma = implied_volatility(option_price, spot, strike, years, rate, right)
    if sigma is None:
        return None

    return black_scholes_delta(spot, strike, years, rate, sigma, right)


def project_option_price(
    option_price: float,
    spot: float,
    target_spot: float,
    strike: float,
    expiration: str,
    rate_pct: float,
    right: str,
    today: date | None = None,
) -> float | None:
    """
    Odhadne cenu opce, až podklad dosáhne cílové úrovně.

    Z aktuální ceny opce se odvodí implikovaná volatilita a s ní se opce
    přecení pro cílovou cenu podkladu. Předpokládá se, že pohyb nastane
    brzy a volatilita zůstane stejná - dojde-li k němu později, bude cena
    opce nižší o časový rozpad.
    """
    if option_price is None or spot is None or option_price <= 0 or spot <= 0:
        return None

    years = years_to_expiry(expiration, today)
    rate = rate_pct / 100.0

    sigma = implied_volatility(option_price, spot, strike, years, rate, right)
    if sigma is None:
        return None

    return black_scholes_price(target_spot, strike, years, rate, sigma, right)


def level_for_option_price(
    target_price: float,
    spot: float,
    strike: float,
    years: float,
    rate: float,
    sigma: float,
    right: str,
) -> float | None:
    """
    Cena podkladu, při které má opce s danou volatilitou zadanou cenu.

    Obrácený výpočet k black_scholes_price: cena CALL s podkladem roste,
    cena PUT klesá, takže stačí půlení intervalu kolem dnešní ceny podkladu.
    Vrací None, pokud cílové ceny v prohledávaném rozsahu nelze dosáhnout.
    """
    if target_price <= 0 or spot <= 0 or strike <= 0 or years <= 0 or sigma <= 0:
        return None

    # Rozsah desetina až desetinásobek dnešní ceny pokrývá každý rozumný pohyb
    low, high = spot / 10.0, spot * 10.0
    cena_low = black_scholes_price(low, strike, years, rate, sigma, right)
    cena_high = black_scholes_price(high, strike, years, rate, sigma, right)
    if not (min(cena_low, cena_high) <= target_price <= max(cena_low, cena_high)):
        return None

    # U CALL cena s podkladem roste, u PUT klesá - podle toho se volí polovina
    roste = right == "C"
    for _ in range(80):
        mid = (low + high) / 2.0
        cena = black_scholes_price(mid, strike, years, rate, sigma, right)
        if (cena < target_price) == roste:
            low = mid
        else:
            high = mid

    return (low + high) / 2.0


def project_underlying_level(
    option_price: float,
    spot: float,
    target_option_price: float,
    strike: float,
    expiration: str,
    rate_pct: float,
    right: str,
    today: date | None = None,
) -> float | None:
    """
    Odhadne cenu podkladu, při které opce dosáhne cílové ceny.

    Z aktuální ceny opce se odvodí implikovaná volatilita a s ní se hledá
    úroveň podkladu, na které má opce cílovou cenu. Protějšek funkce
    project_option_price - ta z úrovně podkladu počítá cenu opce, tato
    z ceny opce úroveň podkladu. Platí stejný předpoklad: pohyb nastane
    brzy a volatilita se nezmění.
    """
    if option_price is None or spot is None or option_price <= 0 or spot <= 0:
        return None
    if target_option_price is None or target_option_price <= 0:
        return None

    years = years_to_expiry(expiration, today)
    rate = rate_pct / 100.0

    sigma = implied_volatility(option_price, spot, strike, years, rate, right)
    if sigma is None:
        return None

    return level_for_option_price(target_option_price, spot, strike, years, rate, sigma, right)


def intended_right(
    entry_price: float,
    profit_target: float | None,
    stop_loss: float | None,
    pt_on_underlying: bool = True,
    sl_on_underlying: bool = True,
) -> str | None:
    """
    Zamýšlený směr obchodu podle polohy úrovní na podkladu vůči vstupu.
    PT nad vstupem (nebo SL pod ním) znamená průraz nahoru = CALL, opačně PUT.
    Jsou-li PT i SL zadané na opci (v USD), směr z nich určit nelze a vrací
    se None - rozhodne pak poloha vstupu vůči aktuální ceně podkladu.
    """
    if pt_on_underlying and profit_target is not None and profit_target != entry_price:
        return "C" if profit_target > entry_price else "P"
    if sl_on_underlying and stop_loss is not None and stop_loss != entry_price:
        return "C" if stop_loss < entry_price else "P"
    return None


def default_profit_target(entry_price: float, stop_loss: float, sl_to_pt_ratio: float) -> float:
    """
    Dopočítá PT na podkladu, pokud uživatel zadal jen SL.
    Obrácený výpočet k default_stop_loss: vzdálenost PT od vstupu =
    vzdálenost SL od vstupu / poměr, na opačnou stranu než SL.
    """
    distance = abs(stop_loss - entry_price) / sl_to_pt_ratio
    # PT leží vždy na opačné straně vstupu než SL
    if stop_loss < entry_price:
        return entry_price + distance
    return entry_price - distance
