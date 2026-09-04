"""
Souhrn obchodního dne pro přehled výsledků.

Modul jen počítá - ze seznamu obchodů sestaví rozdělení na běžící a ukončené,
souhrnná čísla, křivku průběhu dne a součty podle tickeru. Formátování
a vykreslení má na starosti report_dialog.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .models import Flow

# Rozsahy přehledu - dnešní obchodní den, nebo vše, co je v monitoringu
ROZSAH_DNES = "dnes"
ROZSAH_VSE = "vse"


def postup_k_cili(flow: Flow) -> float | None:
    """
    Kde stojí běžící pozice mezi SL a PT: 0 na úrovni SL, 1 na PT.

    Vychází z otevřeného výsledku pozice proti očekávanému zisku na PT
    a ztrátě na SL, takže funguje ve všech režimech zadání úrovní (na
    podkladu i na opci). Bez otevřené pozice nebo bez odhadu vrací None.
    """
    pnl = flow.open_pnl
    if pnl is None:
        return None
    if not flow.expected_profit or not flow.expected_loss:
        return None

    dolni = -abs(flow.expected_loss)
    horni = abs(flow.expected_profit)
    rozpeti = horni - dolni
    if rozpeti <= 0:
        return None
    # Pozice může trh přestřelit (mezera v ceně), hodnota se proto ořezává
    return min(max((pnl - dolni) / rozpeti, 0.0), 1.0)


@dataclass
class TickerSouhrn:
    """Výsledek jednoho tickeru napříč jeho obchody."""

    symbol: str
    realizovano: float = 0.0
    otevreno: float = 0.0
    # Provize zaplacené obchody tohoto tickeru (kladné číslo)
    provize: float = 0.0

    @property
    def celkem(self) -> float:
        """Realizovaný i otevřený výsledek dohromady, ještě bez provizí."""
        return self.realizovano + self.otevreno

    @property
    def celkem_s_provizi(self) -> float:
        """Výsledek tickeru po odečtení provizí - to, co ticker skutečně přinesl."""
        return self.celkem - self.provize


@dataclass
class Souhrn:
    """Souhrnná čísla obchodního dne pro dlaždice nad přehledem."""

    # Výsledek už prodaných kusů (i u obchodů, které dosud běží), bez provizí
    realizovano: float = 0.0
    # Výsledek dosud otevřených pozic oceněný trhem, bez provizí
    otevreno: float = 0.0
    # Provize skutečně účtované TWS, rozdělené podle toho, ke které části
    # pozice patří (obojí kladné číslo). Otevřené části patří jen provize
    # za nákup - prodejní vznikne až prodejem.
    provize_realizovane: float = 0.0
    provize_otevrene: float = 0.0
    # Počty obchodů podle stavu
    bezicich: int = 0
    otevrenych_pozic: int = 0
    ukoncenych: int = 0
    # Ukončené obchody rozdělené podle výsledku
    ziskovych: int = 0
    ztratovych: int = 0
    nulovych: int = 0
    # Bez nákupu, tedy bez výsledku (propásnuté, zrušené před vstupem)
    bez_obchodu: int = 0
    # Součty pro profit factor a průměry - už po odečtení provizí, protože
    # provize umí těsný zisk otočit ve ztrátu a statistika by pak lhala
    hruby_zisk: float = 0.0
    hruba_ztrata: float = 0.0
    nejlepsi: tuple[str, float] | None = None
    nejhorsi: tuple[str, float] | None = None
    # Kolik kontraktů je právě otevřeno v trhu
    otevrenych_kusu: int = 0
    # Velikost účtu, ze které se výsledek přepočítává na procenta. Nula
    # znamená "není známa" - konfigurace ji nemá a z TWS zatím nedorazila
    account_size: float = 0.0

    @property
    def celkem(self) -> float:
        """Výsledek dne dohromady bez provizí - realizovaný i otevřený."""
        return self.realizovano + self.otevreno

    @property
    def provize(self) -> float:
        """Provize zaplacené za celý den (kladné číslo)."""
        return self.provize_realizovane + self.provize_otevrene

    @property
    def realizovano_s_provizi(self) -> float:
        """Realizovaný výsledek po odečtení provizí, které na něj připadají."""
        return self.realizovano - self.provize_realizovane

    @property
    def otevreno_s_provizi(self) -> float:
        """Výsledek otevřených pozic snížený o už zaplacenou nákupní provizi."""
        return self.otevreno - self.provize_otevrene

    @property
    def celkem_s_provizi(self) -> float:
        """Výsledek dne po odečtení všech zaplacených provizí."""
        return self.celkem - self.provize

    @property
    def uzavrenych_s_vysledkem(self) -> int:
        """Ukončené obchody, které skutečně nakoupily, a mají tedy výsledek."""
        return self.ziskovych + self.ztratovych + self.nulovych

    @property
    def uspesnost(self) -> float | None:
        """
        Podíl ziskových obchodů v procentech.
        Nulové obchody (break even) se do jmenovatele počítají, obchody
        bez nákupu ne - ty se nikdy neodehrály.
        """
        celkem = self.uzavrenych_s_vysledkem
        if celkem <= 0:
            return None
        return self.ziskovych / celkem * 100.0

    @property
    def profit_factor(self) -> float | None:
        """
        Poměr součtu ziskových obchodů k součtu ztrátových, obojí po provizích.
        Bez jediné ztráty nemá smysl (dělení nulou), proto None.
        """
        if self.hruba_ztrata <= 0:
            return None
        return self.hruby_zisk / self.hruba_ztrata

    @property
    def prumerny_zisk(self) -> float | None:
        """Průměrný zisk ziskového obchodu po provizích."""
        if self.ziskovych <= 0:
            return None
        return self.hruby_zisk / self.ziskovych

    @property
    def prumerna_ztrata(self) -> float | None:
        """Průměrná ztráta ztrátového obchodu po provizích (kladné číslo)."""
        if self.ztratovych <= 0:
            return None
        return self.hruba_ztrata / self.ztratovych

    def procento_uctu(self, castka: float) -> float | None:
        """
        Částka vyjádřená v procentech velikosti účtu - kolik z účtu obchodní
        den přinesl, nebo ubral.

        Základem je aktuální velikost účtu (z konfigurace, nebo z TWS), takže
        u živého účtu už dnešní výsledek obsahuje; rozdíl je v řádu desetin
        procenta a proti dělení dvěma různými základy je to čitelnější.
        Bez známé velikosti účtu vrací None - dělit nulou nelze.
        """
        if self.account_size <= 0:
            return None
        return castka / self.account_size * 100.0


@dataclass
class DenniReport:
    """Kompletní podklad pro přehled výsledků."""

    rozsah: str = ROZSAH_DNES
    bezici: list[Flow] = field(default_factory=list)
    ukoncene: list[Flow] = field(default_factory=list)
    souhrn: Souhrn = field(default_factory=Souhrn)
    # Kumulovaný realizovaný výsledek po provizích v čase - body křivky dne
    krivka: list[tuple[datetime, float]] = field(default_factory=list)
    podle_tickeru: list[TickerSouhrn] = field(default_factory=list)


def vyber(flows: list[Flow], rozsah: str, den: date | None = None) -> list[Flow]:
    """
    Vybere obchody spadající do zvoleného rozsahu.

    V rozsahu „dnes" projdou obchody založené dnešního dne a navíc všechny
    dosud běžící - ty vyžadují pozornost bez ohledu na to, kdy vznikly
    (aplikace může běžet přes noc nebo obnovit stav z předchozího dne).
    """
    if rozsah == ROZSAH_VSE:
        return list(flows)

    dnesek = den or date.today()
    return [
        flow
        for flow in flows
        if flow.state.is_active or flow.created_at.date() == dnesek
    ]


def _serad_ukoncene(flows: list[Flow]) -> list[Flow]:
    """Ukončené obchody od nejnovějšího - poslední výsledek dne je nahoře."""
    return sorted(flows, key=lambda f: f.updated_at, reverse=True)


def _serad_bezici(flows: list[Flow]) -> list[Flow]:
    """
    Běžící obchody: nejprve ty s otevřenou pozicí (na těch záleží nejvíc),
    uvnitř skupiny abecedně podle tickeru.
    """
    return sorted(flows, key=lambda f: (f.fill_price is None, f.symbol, f.id))


def _spocti_souhrn(
    bezici: list[Flow], ukoncene: list[Flow], account_size: float = 0.0
) -> Souhrn:
    """
    Sečte výsledky obchodů do souhrnných čísel.

    Realizovaná část se bere ze všech obchodů včetně běžících - prodaný
    runner je hotový výsledek, i když zbytek pozice pokračuje. Statistiky
    úspěšnosti počítají jen ukončené obchody, aby je nezkresloval výsledek,
    který se ještě může otočit.
    """
    souhrn = Souhrn(
        bezicich=len(bezici), ukoncenych=len(ukoncene), account_size=account_size
    )

    for flow in bezici:
        realizovano = flow.realized_pnl
        if realizovano:
            souhrn.realizovano += realizovano
        otevreno = flow.open_pnl
        if otevreno is not None:
            souhrn.otevreno += otevreno
        # Provize běžícího obchodu se dělí stejně jako jeho pozice: co je
        # doprodané, patří k realizovanému výsledku, zbytek k otevřenému
        souhrn.provize_realizovane += flow.realized_commission
        souhrn.provize_otevrene += flow.open_commission
        if flow.open_quantity > 0:
            souhrn.otevrenych_pozic += 1
            souhrn.otevrenych_kusu += flow.open_quantity

    for flow in ukoncene:
        vysledek = flow.realized_pnl
        if vysledek is None:
            # Obchod skončil dřív, než se vůbec nakoupilo
            souhrn.bez_obchodu += 1
            continue

        souhrn.realizovano += vysledek
        # Uzavřený obchod už nic nedrží, takže mu patří celá zaplacená provize
        souhrn.provize_realizovane += flow.commission

        # O tom, jestli obchod skončil v zisku, rozhoduje výsledek po provizích
        cisty = vysledek - flow.commission
        if cisty > 0:
            souhrn.ziskovych += 1
            souhrn.hruby_zisk += cisty
        elif cisty < 0:
            souhrn.ztratovych += 1
            souhrn.hruba_ztrata += abs(cisty)
        else:
            souhrn.nulovych += 1

        # Nejlepší a nejhorší obchod dne pro dlaždici s extrémy
        if souhrn.nejlepsi is None or cisty > souhrn.nejlepsi[1]:
            souhrn.nejlepsi = (flow.symbol, cisty)
        if souhrn.nejhorsi is None or cisty < souhrn.nejhorsi[1]:
            souhrn.nejhorsi = (flow.symbol, cisty)

    return souhrn


def _krivka(ukoncene: list[Flow]) -> list[tuple[datetime, float]]:
    """
    Kumulovaný realizovaný výsledek v čase - jak se den vyvíjel.

    Sčítá se výsledek po provizích, aby křivka odpovídala tomu, co obchodní
    den skutečně přinesl. Body vznikají v čase ukončení obchodu (updated_at)
    a řadí se vzestupně; obchody bez nákupu se přeskakují, protože výsledkem
    nepohnuly.
    """
    body: list[tuple[datetime, float]] = []
    soucet = 0.0
    for flow in sorted(ukoncene, key=lambda f: f.updated_at):
        vysledek = flow.realized_pnl
        if vysledek is None:
            continue
        soucet += vysledek - flow.commission
        body.append((flow.updated_at, soucet))
    return body


def _podle_tickeru(bezici: list[Flow], ukoncene: list[Flow]) -> list[TickerSouhrn]:
    """
    Součty výsledků po tickerech, seřazené od nejlepšího po nejhorší.
    Ticker se objeví, jen když má co ukázat - obchod bez nákupu se vynechá.
    """
    souhrny: dict[str, TickerSouhrn] = {}

    def zaznam(symbol: str) -> TickerSouhrn:
        """Souhrn tickeru; při prvním výskytu jej založí."""
        if symbol not in souhrny:
            souhrny[symbol] = TickerSouhrn(symbol=symbol)
        return souhrny[symbol]

    for flow in bezici + ukoncene:
        realizovano = flow.realized_pnl
        otevreno = flow.open_pnl if flow.state.is_active else None
        provize = flow.commission
        # Obchod bez výsledku i bez provize nemá co ukázat; zaplacená provize
        # se ale objevit musí, i když sám výsledek vyšel nulový
        if not realizovano and otevreno is None and not provize:
            continue
        polozka = zaznam(flow.symbol)
        polozka.realizovano += realizovano or 0.0
        polozka.otevreno += otevreno or 0.0
        polozka.provize += provize

    return sorted(souhrny.values(), key=lambda s: s.celkem_s_provizi, reverse=True)


def sestav(
    flows: list[Flow],
    rozsah: str = ROZSAH_DNES,
    den: date | None = None,
    account_size: float = 0.0,
) -> DenniReport:
    """
    Sestaví kompletní přehled výsledků ze seznamu obchodů.

    Parametr rozsah rozhoduje, co se do přehledu dostane (ROZSAH_DNES /
    ROZSAH_VSE), den umožňuje testům určit „dnešek" napevno. Velikost účtu
    slouží k přepočtu výsledku na procenta účtu; nula znamená, že známa není.
    """
    vybrane = vyber(flows, rozsah, den)
    bezici = [flow for flow in vybrane if flow.state.is_active]
    ukoncene = [flow for flow in vybrane if not flow.state.is_active]

    return DenniReport(
        rozsah=rozsah,
        bezici=_serad_bezici(bezici),
        ukoncene=_serad_ukoncene(ukoncene),
        souhrn=_spocti_souhrn(bezici, ukoncene, account_size),
        krivka=_krivka(ukoncene),
        podle_tickeru=_podle_tickeru(bezici, ukoncene),
    )
