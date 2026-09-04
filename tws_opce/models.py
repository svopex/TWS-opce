"""Datové modely obchodního flow a jeho stavu."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from . import calc

# Popisky typu opce pro zobrazení v UI
RIGHT_LABELS = {"C": "CALL", "P": "PUT"}

# Násobky původní vzdálenosti cíle od vstupu nabízené v rozhraní - tlačítky
# pod řádkem obchodu v přehledu i při hromadném zadání ze souboru
PT_MULTIPLES = (1.0, 1.5, 2.0, 2.5, 3.0)


def cislo_text(hodnota: float, desetin: int = 2) -> str:
    """Číslo pro zobrazení - tisíce oddělené mezerou, desetinná čárka jako tečka."""
    return f"{hodnota:,.{desetin}f}".replace(",", " ")


def format_countdown(sekundy: float) -> str:
    """
    Zbývající čas pro odpočty v hlavičce. Pod hodinu vyjde MM:SS, do dne
    H:MM:SS a přes den se přidá počet dní (odpočet do otevření trhu běží
    i přes víkend, takže může jít o desítky hodin).
    """
    celkem = max(0, int(sekundy))
    dny, zbytek = divmod(celkem, 86400)
    hodiny, zbytek = divmod(zbytek, 3600)
    minuty, sek = divmod(zbytek, 60)
    if dny:
        return f"{dny} d {hodiny}:{minuty:02d}:{sek:02d}"
    if hodiny:
        return f"{hodiny}:{minuty:02d}:{sek:02d}"
    return f"{minuty:02d}:{sek:02d}"


def pomer_z_rrr(rrr: float) -> float:
    """
    Poměr SL:PT pro engine z RRR zadaného ve formuláři.

    Formuláře se ptají na RRR (kolikrát je PT dál než SL), engine i konfigurace
    pracují s obrácenou hodnotou - RRR 2 je tedy poměr 0,5. Nekladné RRR nemá
    smysl a vrací se nezměněné, ať si jej ošetří volající.
    """
    return 1.0 / rrr if rrr > 0 else rrr


def rrr_z_pomeru(pomer: float) -> float:
    """
    RRR do formuláře z poměru SL:PT (typicky z konfigurace) - opačný převod
    k pomer_z_rrr. Zaokrouhluje se na dvě desetinná místa, aby v poli
    nestál nekonečný rozvoj jako 3,3333.
    """
    return round(1.0 / pomer, 2) if pomer > 0 else pomer


# Režimy zadání úrovně PT a SL. Podklad je cena podkladu hlídaná podmíněným
# příkazem, zbylé dva jsou tatáž úroveň na opci - jednou zapsaná přímo v USD
# na kontrakt, podruhé podílem ze zaplacené prémie. Procento je jen jednotka
# zadání: před odesláním se z ceny opce převede na USD, takže engine i stav
# obchodu pracují s USD stejně jako dosud.
#
# Slovník režimů je společný celé aplikaci - běžný formulář si drží režim PT
# a SL zvlášť, hromadné načtení ze souboru odvozuje oba z jediné volby cíle.
MODE_UNDERLYING = "underlying"
MODE_USD = "usd"
MODE_PREMIUM = "premium"

# Režimy, ve kterých úroveň leží na opci
MODES_ON_OPTION = (MODE_USD, MODE_PREMIUM)

# Příznaky (na podkladu, v procentech prémie), kterými se režim ukládá
# k obchodu. Jediná tabulka pro oba směry převodu, aby se nemohly rozejít
LEVEL_FLAGS = {
    MODE_UNDERLYING: (True, False),
    MODE_USD: (False, False),
    MODE_PREMIUM: (False, True),
}


def rezim_urovne(na_podkladu: bool, v_procentech: bool = False) -> str:
    """
    Režim odpovídající uloženým příznakům obchodu.

    Na podkladu procento nedává smysl - úroveň je cena podkladu, ne podíl
    z prémie -, proto se příznak procenta uplatní jen u úrovně na opci.
    """
    if na_podkladu:
        return MODE_UNDERLYING
    return MODE_PREMIUM if v_procentech else MODE_USD


def priznaky_urovne(rezim: str) -> tuple[bool, bool]:
    """
    Příznaky (na podkladu, v procentech prémie) pro daný režim - opačný
    převod k rezim_urovne.

    Neznámý režim je chyba volajícího, ne důvod tiše zvolit výchozí hodnotu:
    ticho by z úrovně na opci udělalo cenu podkladu a naopak, což se pozná
    až na špatně zadaném obchodu. Proto padá na KeyError.
    """
    return LEVEL_FLAGS[rezim]


def urovne_z_rezimu(rezim: str, premie: float | None = None) -> dict[str, Any]:
    """
    Příznaky úrovní do FlowRequest pro zadání, kde PT i SL vznikly v jednom
    režimu - tak zadává hromadné načtení pozic ze souboru.

    Jediná volba se tu rozbaluje na všechny čtyři příznaky zadání i na základ
    prémie, takže se PT a SL nemohou rozejít ani se nedá vynechat polovina
    dvojice. Procento prémie potřebuje cenu opce, ze které vyšlo; bez ní se
    úroveň zapíše jako prostá částka v USD na kontrakt.

    Vrací pojmenované argumenty pro FlowRequest, určené k rozbalení (**).
    """
    na_podkladu, v_premiu = priznaky_urovne(rezim)
    v_premiu = v_premiu and bool(premie)
    return {
        "pt_on_underlying": na_podkladu,
        "sl_on_underlying": na_podkladu,
        "pt_in_premium": v_premiu,
        "sl_in_premium": v_premiu,
        "premium_base": premie if v_premiu else None,
    }


def level_text(
    druh: str,
    hodnota: float,
    na_podkladu: bool,
    fill_price: float | None = None,
    min_tick: float = 0.01,
) -> str:
    """
    Popis úrovně PT ('pt') nebo SL ('sl') pro přehled, log i hlášky.

    Na podkladu je to cena podkladu. Na opci je to zisk, resp. ztráta v USD
    na jeden kontrakt; je-li známa nákupní cena, uvede se navíc cena opce,
    na kterou příkaz míří - například „3,10 (+10,00 USD)". Nulová ztráta na
    opci je stop na nákupní ceně, tedy break even.
    """
    if na_podkladu:
        return cislo_text(hodnota)

    if druh == "sl" and hodnota == 0:
        castka = "BE"
    else:
        castka = f"{'+' if druh == 'pt' else '-'}{cislo_text(hodnota)} USD"
    if fill_price is None:
        return castka

    if druh == "pt":
        cena = calc.option_profit_limit(fill_price, hodnota, min_tick)
    else:
        cena = calc.option_loss_stop(fill_price, hodnota, min_tick)
    return f"{cislo_text(cena)} ({castka})"


class FlowState(str, Enum):
    """Stavy životního cyklu jednoho obchodu."""

    NEW = "NEW"
    ARMED = "ARMED"
    SPREAD_BLOCKED = "SPREAD_BLOCKED"
    NO_QUOTES = "NO_QUOTES"
    FILLED = "FILLED"
    EXIT_ARMED = "EXIT_ARMED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    MISSED = "MISSED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"

    @property
    def label(self) -> str:
        """Český popisek stavu pro monitorovací tabulku."""
        return {
            FlowState.NEW: "Připravuje se",
            FlowState.ARMED: "Před nákupem",
            FlowState.SPREAD_BLOCKED: "Blokováno spreadem",
            FlowState.NO_QUOTES: "Čeká na kotace opce",
            FlowState.FILLED: "Nakoupeno",
            FlowState.EXIT_ARMED: "Nakoupeno – výstup aktivní",
            FlowState.CLOSING: "Uzavírá se",
            FlowState.CLOSED: "Uzavřeno",
            FlowState.MISSED: "Vstup propásnut",
            FlowState.CANCELLED: "Zrušeno",
            FlowState.ERROR: "Chyba",
        }[self]

    @property
    def css_class(self) -> str:
        """
        CSS třída pro barevné odlišení stavu - používá ji monitorovací
        tabulka i přehled výsledků, aby stav vypadal všude stejně.
        """
        return {
            FlowState.NEW: "stav-ceka",
            FlowState.ARMED: "stav-ceka",
            FlowState.SPREAD_BLOCKED: "stav-blokovano",
            FlowState.NO_QUOTES: "stav-blokovano",
            FlowState.FILLED: "stav-nakoupeno",
            FlowState.EXIT_ARMED: "stav-nakoupeno",
            FlowState.CLOSING: "stav-uzavira",
            FlowState.CLOSED: "stav-uzavreno",
            FlowState.MISSED: "stav-propasnuto",
            FlowState.CANCELLED: "stav-zruseno",
            FlowState.ERROR: "stav-chyba",
        }[self]

    @property
    def is_active(self) -> bool:
        """Flow, které ještě vyžaduje pozornost monitorovací smyčky."""
        return self in (
            FlowState.NEW,
            FlowState.ARMED,
            FlowState.SPREAD_BLOCKED,
            FlowState.NO_QUOTES,
            FlowState.FILLED,
            FlowState.EXIT_ARMED,
            FlowState.CLOSING,
        )

    @property
    def is_before_entry(self) -> bool:
        """Flow, u kterého ještě nedošlo k nákupu opce."""
        return self in (
            FlowState.NEW,
            FlowState.ARMED,
            FlowState.SPREAD_BLOCKED,
            FlowState.NO_QUOTES,
        )


@dataclass
class FlowRequest:
    """
    Zadání obchodu z formuláře.

    PT a SL se zadávají buď jako cena podkladu (výchozí), nebo jako zisk,
    resp. ztráta v USD na jeden opční kontrakt - o tom rozhodují přepínače
    pt_on_underlying a sl_on_underlying. Stačí zadat jednu z úrovní,
    chybějící se dopočítá podle poměru SL:PT z formuláře (sl_to_pt_ratio),
    nebo - není-li zadán - z konfigurace.
    """

    symbol: str
    entry_price: float
    profit_target: float | None = None
    stop_loss: float | None = None
    quantity: int | None = None
    max_spread_pct: float | None = None
    pt_on_underlying: bool = True
    sl_on_underlying: bool = True
    # Připočtení spreadu k SL zadanému na opci - viz Flow.sl_spread_compensated
    sl_spread_compensated: bool = False
    # Poměr SL:PT pro dopočet chybějící úrovně. None znamená "vzít
    # z konfigurace" - formulář sem posílá hodnotu ze svého pole
    sl_to_pt_ratio: float | None = None
    # Která úroveň se zadávala jako prvotní: 'sl' (PT se dopočítá), nebo 'pt'.
    # Prázdné znamená "neuvedeno" - viz Flow.primary_level
    primary_level: str = ""
    # Jednotka, ve které byla úroveň na opci zadána: True = procento zaplacené
    # prémie. Engine i obchod pracují vždy s USD na kontrakt, tohle je jen
    # paměť formuláře - viz Flow.pt_in_premium
    pt_in_premium: bool = False
    sl_in_premium: bool = False
    # Cena opce (USD za kus), ze které se procenta na USD převáděla
    premium_base: float | None = None
    # Přepočet po otevření burzy: za kolik sekund od otevření se čekajícímu
    # obchodu přepočítají PT, SL a množství podle živých kotací. None znamená
    # nepřepočítávat - viz Flow.refresh_after_open_sec
    refresh_after_open_sec: float | None = None
    # Zamýšlený směr obchodu ('C' = long, 'P' = short), zná-li jej zadavatel
    # nezávisle na úrovních - hromadný import jej čte ze souboru (cíl pod
    # vstupem = short). Jsou-li PT i SL zadané na opci, z čísel se směr
    # odvodit nedá a bez tohoto údaje by o typu opce rozhodla okamžitá
    # poloha ceny podkladu: short zadaný pod aktuální cenou by se založil
    # jako CALL. None znamená "neuvedeno" - směr určí engine sám
    intended_right: str | None = None


@dataclass
class Flow:
    """
    Jeden obchod - od zadání příkazu do trhu až po uzavření pozice.
    Uchovává zadané parametry, vybraný opční kontrakt i aktuální tržní data.
    """

    id: str
    symbol: str
    entry_price: float
    profit_target: float
    stop_loss: float
    quantity: int
    max_spread_pct: float
    right: str = "C"

    # Režim PT a SL: True = úroveň je cena podkladu a hlídá ji podmíněný
    # příkaz; False = hodnota je zisk, resp. ztráta v USD na jeden kontrakt
    # a prodává se příkazem přímo na cenu opce (PT limitem, SL stop-marketem).
    # Jsou-li oba na podkladu, stačí jediný příkaz s podmínkami PT OR SL;
    # jinak vznikají dva příkazy a po vyplnění jednoho se druhý ruší.
    pt_on_underlying: bool = True
    sl_on_underlying: bool = True

    # Kompenzace spreadu u SL zadaného na opci. Nakupuje se u ASKu, ale stop
    # se spouští BIDem, takže SL je ve skutečnosti blíž o celý spread: SL 30
    # při spreadu 10 USD/ks se spustí už po pohybu ceny opce o 20 USD.
    # Se zapnutou kompenzací se při nákupu k SL připočte skutečně zaplacený
    # spread (nákupní cena minus BID), takže zadaná hodnota odpovídá pohybu
    # trhu - za cenu úměrně větší ztráty. Break even (SL 0) se nekompenzuje.
    sl_spread_compensated: bool = False
    # Kolik USD na kontrakt už bylo k SL připočteno; formulář o to zapsanou
    # hodnotu zase snižuje, aby se navýšení při dalším zadání neřetězilo
    sl_spread_usd: float = 0.0
    # Odhad kompenzace SL se stropuje limitem spreadu - nad něj se příkaz
    # do trhu nedostane, takže širší spread obchod nezaplatí. Vypnuté
    # trading.cancel_on_spread_breach strop ruší: takový příkaz zůstává
    # v trhu i po rozšíření spreadu a vyplnit se může za jakýkoliv
    sl_spread_capped: bool = True

    # Jednotka, ve které obchodník úroveň na opci zadal: True = procento
    # zaplacené prémie. Obchod i engine počítají výhradně s USD na kontrakt,
    # tenhle příznak slouží jen formuláři - při načtení obchodu se úroveň
    # nabídne v téže jednotce, v jaké vznikla, a ne přepočtená na USD
    pt_in_premium: bool = False
    sl_in_premium: bool = False
    # Cena opce (USD za kus), ze které se procenta na USD převáděla. Zpětný
    # převod musí vyjít z téže hodnoty, jinak by se zadaná procenta posunula
    # podle aktuální kotace. None znamená, že se procenta nepoužila
    premium_base: float | None = None

    # Přepočet po otevření burzy. Obchod zadaný před otevřením má úrovně
    # i množství z odhadu prémie (typicky ze závěrečné ceny), který po gapu
    # neplatí; tolik sekund po otevření se čekajícímu obchodu PT, SL
    # a množství dopočítají znovu z živých kotací. None = nepřepočítávat.
    # Hotový přepočet si obchod poznamená, aby proběhl jen jednou
    refresh_after_open_sec: float | None = None
    refresh_after_open_done: bool = False

    # Prvotní úroveň zadání: 'sl' znamená, že obchodník zadal SL a PT se
    # dopočítalo, 'pt' naopak. Z uložených úrovní to poznat nejde (obě se
    # ukládají stejně), přitom formulář to při načtení obchodu potřebuje.
    # Prázdné znamená "neuvedeno" - obchod ze starší verze aplikace
    primary_level: str = ""
    # Poměr SL:PT, kterým obchod skutečně vznikl - buď z pole RRR ve
    # formuláři, nebo (nebylo-li vyplněné) z konfigurace. Zpětně se z úrovní
    # dopočítat nedá: ve smíšeném režimu nejde o podíl dvou srovnatelných
    # čísel a posun cíle násobkem by ho stejně změnil
    sl_to_pt_ratio: float | None = None

    # PT zadané při založení obchodu; násobky cíle se počítají z něj,
    # aby opakovaná změna nevycházela z už posunuté hodnoty
    original_profit_target: float = 0.0

    # SL zadaný při založení obchodu - tlačítko "Počáteční SL" se na něj
    # vrací poté, co byl stop posunut na break even. None znamená "neznámý"
    # (stav uložený starší verzí); nula je naopak platná hodnota - u SL
    # na opci je to break even, proto se nesmí posuzovat pravdivostí čísla.
    original_stop_loss: float | None = None

    # Vybraný opční kontrakt
    expiration: str = ""
    strike: float = 0.0
    option_conid: int = 0
    underlying_conid: int = 0
    min_tick: float = 0.01

    state: FlowState = FlowState.NEW
    message: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    # Aktuální tržní data (plní je monitorovací smyčka)
    underlying_price: float | None = None
    option_bid: float | None = None
    option_ask: float | None = None
    option_spread_pct: float | None = None
    delta: float | None = None

    # Stav příkazů
    entry_limit: float | None = None
    entry_order_id: int | None = None
    # Zrušení nevyplněného zbytku nákupu bylo vyžádáno (čeká se na potvrzení TWS)
    entry_cancel_requested: bool = False
    # Kdy byl příkaz naposledy odstraněn z trhu kvůli spreadu
    blocked_since: datetime | None = None
    exit_order_id: int | None = None
    # Druhý prodejní příkaz hlavní části (SL), když se PT a SL realizují
    # odděleně; při obou úrovních na podkladu zůstává None
    exit_sl_order_id: int | None = None
    fill_price: float | None = None
    fill_time: datetime | None = None
    # Skutečně nakoupené množství - při částečném plnění je nižší než zadané
    filled_quantity: int = 0
    exit_fill_price: float | None = None
    exit_reason: str = ""
    # Kusy hlavní části prodané dosavadními příkazy (i částečným vyplněním),
    # dokud se zbytek neprodá; hodnota = součet cena × kusy pro vážený průměr
    # výsledné prodejní ceny
    main_sold_quantity: int = 0
    main_sold_value: float = 0.0
    # Kolik z toho pochází z právě běžících prodejních příkazů. Účtuje se
    # přírůstkově, aby opakovaný průchod smyčkou totéž vyplnění nezapočítal
    # dvakrát; se zadáním nové generace příkazů se počitadlo vynuluje.
    main_counted_quantity: int = 0
    main_counted_value: float = 0.0

    # Runner - část pozice prodávaná samostatným příkazem s vlastním cílem.
    # None v runner_profit_target znamená, že runner není aktivní.
    runner_profit_target: float | None = None
    runner_quantity: int = 0
    # Vlastní SL runneru - při zapnutí přebírá SL obchodu a dál se přepíná
    # nezávisle na hlavní části (počáteční SL / break even)
    runner_stop_loss: float | None = None
    runner_order_id: int | None = None
    # Druhý prodejní příkaz runneru (SL) při odděleném PT a SL
    runner_sl_order_id: int | None = None
    runner_fill_price: float | None = None
    # Souhrn dříve prodaných runnerů - po prodeji se runner zúčtuje sem
    # a jeho pole se uvolní, takže lze nastartovat další
    runner_sold_quantity: int = 0
    runner_realized_pnl: float = 0.0
    # Kusy runneru už zúčtované z právě běžících příkazů (přírůstkové
    # účtování stejně jako u hlavní části)
    runner_counted_quantity: int = 0
    runner_counted_value: float = 0.0

    # Provize skutečně účtované TWS, vedené po jednotlivých vyplněních podle
    # jejich identifikátoru (execId). Zpráva o provizi chodí odděleně od
    # vyplnění a po novém spojení ji TWS pošle za celý den znovu - podle
    # execId se tedy táž provize nezapočítá dvakrát. Nákup se drží zvlášť
    # od prodejů, aby šlo provize rozdělit na uzavřenou a otevřenou část pozice.
    entry_commissions: dict[str, float] = field(default_factory=dict)
    exit_commissions: dict[str, float] = field(default_factory=dict)

    # Vyžádané uzavření trhem - hlavní části, resp. runneru. Podmíněný příkaz
    # se nejprve ruší a tržní prodej se zadává až po potvrzení zrušení.
    main_close_requested: bool = False
    runner_close_requested: bool = False

    # Hlídání tržního prodeje: kdy byl příkaz odeslán a kolik pokusů proběhlo.
    # TWS občas nechá tržní příkaz nevyplněný; takový se po prodlevě zadá znovu.
    exit_market_sent: datetime | None = None
    exit_market_attempts: int = 0
    runner_market_sent: datetime | None = None
    runner_market_attempts: int = 0

    # Očekávaný výsledek obchodu v USD, pokud podklad dosáhne PT resp. SL.
    # Přepočítává se průběžně podle aktuální ceny opce a podkladu, takže
    # odráží měnící se podmínky na trhu.
    expected_profit: float | None = None
    expected_loss: float | None = None

    # Runtime objekty z ib_async - nezobrazují se a neserializují
    option_contract: Any = field(default=None, repr=False, compare=False)
    underlying_contract: Any = field(default=None, repr=False, compare=False)
    entry_trade: Any = field(default=None, repr=False, compare=False)
    exit_trade: Any = field(default=None, repr=False, compare=False)
    exit_sl_trade: Any = field(default=None, repr=False, compare=False)
    runner_trade: Any = field(default=None, repr=False, compare=False)
    runner_sl_trade: Any = field(default=None, repr=False, compare=False)

    @property
    def right_label(self) -> str:
        """CALL / PUT pro zobrazení."""
        return RIGHT_LABELS.get(self.right, self.right)

    @property
    def original_sl_known(self) -> bool:
        """
        True, pokud obchod zná svůj počáteční SL.
        Nula je platná hodnota jen u SL na opci (break even); u SL na podkladu
        znamená chybějící údaj ve stavu uloženém starší verzí.
        """
        if self.original_stop_loss is None:
            return False
        return not (self.sl_on_underlying and self.original_stop_loss <= 0)

    @property
    def exit_split(self) -> bool:
        """
        True, pokud se PT a SL realizují dvěma samostatnými příkazy.
        Jediný podmíněný příkaz (PT OR SL) stačí jen tehdy, když jsou
        obě úrovně zadané na podkladu.
        """
        return not (self.pt_on_underlying and self.sl_on_underlying)

    @property
    def break_even_sl(self) -> float:
        """
        Hodnota SL odpovídající break even: na podkladu vstupní cena,
        na opci nulová ztráta (stop na nákupní ceně opce).
        """
        return self.entry_price if self.sl_on_underlying else 0.0

    @property
    def pending_sl_spread(self) -> float:
        """
        Spread, který se k SL na opci teprve připočte při nákupu (USD/ks).

        Kompenzace vychází ze skutečně zaplaceného spreadu, a ten je znám až
        z vyplněného nákupu. Do té doby se pracuje s odhadem z aktuální kotace
        opce, aby přehled ukazoval skutečné riziko, a ne hodnotu, která se po
        nákupu skokem změní. Po nákupu, u SL na podkladu, u break even i bez
        kotací je nula.

        Odhad se stropuje limitem spreadu, stejně jako při dopočtu množství -
        nad limitem se nenakupuje, takže širší spread obchod nezaplatí a bez
        stropu by přehled ukazoval riziko, které nemůže nastat. Základem
        procenta je střed trhu; odhad nákupní ceny obchod na rozdíl od
        náhledu nedrží.
        """
        if not self.sl_spread_compensated or self.sl_on_underlying:
            return 0.0
        if self.sl_spread_usd or self.fill_price is not None or self.stop_loss <= 0:
            return 0.0
        return calc.capped_spread_usd(
            self.option_bid,
            self.option_ask,
            self.max_spread_pct if self.sl_spread_capped else None,
        )

    def sl_with_pending(self, hodnota: float) -> float:
        """
        Ztráta na opci i s dosud nepřipočteným spreadem. Break even (nula)
        zůstává nulou - jeho stop má stát na nákupní ceně.
        """
        return hodnota + self.pending_sl_spread if hodnota > 0 else hodnota

    def level_text(self, druh: str, hodnota: float | None = None) -> str:
        """
        Popis vlastní úrovně PT ('pt') nebo SL ('sl').
        Bez zadané hodnoty se bere aktuální úroveň obchodu; jinak se popíše
        libovolná hodnota v témže režimu (například cíl runneru).

        U SL čekajícího na kompenzaci spreadu se uvádí odhad včetně něj,
        odlišený znaménkem přibližné rovnosti.
        """
        if druh == "pt":
            na_podkladu = self.pt_on_underlying
            if hodnota is None:
                hodnota = self.profit_target
        else:
            na_podkladu = self.sl_on_underlying
            if hodnota is None:
                hodnota = self.stop_loss
            s_kompenzaci = self.sl_with_pending(hodnota)
            if s_kompenzaci != hodnota:
                popis = level_text(
                    druh, s_kompenzaci, na_podkladu, self.fill_price, self.min_tick
                )
                return f"≈ {popis}"
        return level_text(druh, hodnota, na_podkladu, self.fill_price, self.min_tick)

    def scaled_target(self, multiple: float) -> float:
        """
        Cíl na násobku původní vzdálenosti od vstupu.
        Na podkladu se násobí vzdálenost od vstupní ceny, na opci přímo
        zisk v USD (jeho „vstupem" je nula).
        """
        zaklad = self.original_profit_target or self.profit_target
        if self.pt_on_underlying:
            return round(self.entry_price + (zaklad - self.entry_price) * multiple, 2)
        return round(zaklad * multiple, 2)

    def pt_distance(self, level: float) -> float:
        """Vzdálenost cílové úrovně od vstupu v jednotkách daného režimu PT."""
        if self.pt_on_underlying:
            return abs(level - self.entry_price)
        return abs(level)

    @property
    def unrealized_pnl(self) -> float | None:
        """
        Nerealizovaný zisk/ztráta pozice v USD.
        Počítá se ze středu trhu opce proti nákupní ceně,
        u uzavřené pozice z dosažené prodejní ceny.
        """
        if self.fill_price is None:
            return None

        # Pozice se skládá z hlavní části, případného běžícího runneru
        # a realizovaného výsledku dříve prodaných runnerů. Prodaná část se
        # oceňuje dosaženou cenou, běžící středem trhu.
        mid = None
        if self.option_bid is not None and self.option_ask is not None:
            mid = (self.option_bid + self.option_ask) / 2.0

        casti: list[tuple[float | None, int]] = []
        if self.exit_fill_price is not None:
            casti.append((self.exit_fill_price, self.main_quantity))
        else:
            # Část hlavní části už mohla být prodaná (částečné vyplnění příkazu) -
            # ta se oceňuje dosaženou cenou, zbytek středem trhu
            if self.main_sold_quantity > 0:
                casti.append(
                    (self.main_sold_value / self.main_sold_quantity, self.main_sold_quantity)
                )
            zbytek = self.main_quantity - self.main_sold_quantity
            if zbytek > 0:
                casti.append((None, zbytek))
        if self.runner_active and self.runner_quantity <= self.held_quantity:
            casti.append((self.runner_fill_price, self.runner_quantity))

        vysledek = self.runner_realized_pnl
        for cena, mnozstvi in casti:
            if cena is None:
                cena = mid
            if cena is None:
                return None
            vysledek += (cena - self.fill_price) * mnozstvi * calc.OPTION_MULTIPLIER
        return vysledek

    @property
    def realized_pnl(self) -> float | None:
        """
        Realizovaný zisk/ztráta obchodu v USD - jen skutečně prodané kusy.

        Na rozdíl od unrealized_pnl se sem nepočítá dosud otevřená část pozice
        oceněná trhem; hodnota se tedy už nezmění. Sečte se výsledek dříve
        prodaných runnerů, prodané (i jen částečně) hlavní části a runneru,
        který se prodal, ale ještě nebyl zúčtován do souhrnu.
        Bez nákupu (a tedy bez čeho realizovat) None.
        """
        if self.fill_price is None:
            return None

        vysledek = self.runner_realized_pnl
        if self.exit_fill_price is not None:
            # Hlavní část je doprodaná, známa je její výsledná průměrná cena
            vysledek += (
                (self.exit_fill_price - self.fill_price)
                * self.main_quantity
                * calc.OPTION_MULTIPLIER
            )
        elif self.main_sold_quantity > 0:
            # Prodala se jen část hlavní pozice - zbytek zůstává otevřený
            prumer = self.main_sold_value / self.main_sold_quantity
            vysledek += (
                (prumer - self.fill_price)
                * self.main_sold_quantity
                * calc.OPTION_MULTIPLIER
            )
        # Prodaný, ale zatím nezúčtovaný runner (stav uložený starší verzí)
        if (
            self.runner_active
            and self.runner_fill_price is not None
            and self.runner_quantity <= self.held_quantity
        ):
            vysledek += (
                (self.runner_fill_price - self.fill_price)
                * self.runner_quantity
                * calc.OPTION_MULTIPLIER
            )
        return vysledek

    @property
    def entry_commission(self) -> float:
        """Provize zaplacená za nákup opce - všechna jeho vyplnění dohromady."""
        return sum(self.entry_commissions.values())

    @property
    def exit_commission(self) -> float:
        """Provize zaplacené za prodeje - hlavní části i runnerů."""
        return sum(self.exit_commissions.values())

    @property
    def commission(self) -> float:
        """Provize, které obchod dosud skutečně zaplatil (kladné číslo)."""
        return self.entry_commission + self.exit_commission

    @property
    def open_commission(self) -> float:
        """
        Část nákupní provize připadající na dosud otevřené kusy.

        Prodejní provize se otevřené části netýká - ta se zaplatí až při
        prodeji, a dokud k němu nedojde, není co započítat.
        """
        koupeno = self.filled_quantity
        if koupeno <= 0:
            return 0.0
        return self.entry_commission * self.open_quantity / koupeno

    @property
    def realized_commission(self) -> float:
        """
        Provize připadající na už uzavřenou část obchodu - nákup prodaných
        kusů a všechny prodeje. Zbytek nákupní provize drží otevřená pozice.
        """
        return self.commission - self.open_commission

    @property
    def realized_pnl_net(self) -> float | None:
        """
        Realizovaný výsledek po odečtení provizí. Bez nákupu (a tedy bez
        čeho realizovat) None, stejně jako realized_pnl.
        """
        hruby = self.realized_pnl
        if hruby is None:
            return None
        return hruby - self.realized_commission

    @property
    def open_pnl_net(self) -> float | None:
        """
        Výsledek otevřené části po odečtení nákupní provize, která na ni
        připadá. Prodejní provize v něm být nemůže - ta ještě nevznikla.
        """
        hruby = self.open_pnl
        if hruby is None:
            return None
        return hruby - self.open_commission

    @property
    def holding_seconds(self) -> float | None:
        """
        Jak dlouho obchod drží (resp. držel) pozici, v sekundách.
        Před nákupem None; u běžící pozice se počítá do teď, u ukončené
        do času poslední změny stavu.
        """
        if self.fill_time is None:
            return None
        konec = datetime.now() if self.state.is_active else self.updated_at
        return max((konec - self.fill_time).total_seconds(), 0.0)

    @property
    def result_reason(self) -> str:
        """
        Krátký popis toho, jak obchod dopadl - pro přehled výsledků.

        U uzavřené pozice je to důvod výstupu zapsaný enginem (PT, SL, ručně),
        u obchodů ukončených bez pozice jejich stav.
        """
        if self.exit_reason:
            return self.exit_reason
        if self.state == FlowState.MISSED:
            return "propásnuto"
        if self.state == FlowState.CANCELLED:
            return "zrušeno"
        if self.state == FlowState.ERROR:
            return "chyba"
        return "-"

    @property
    def risk_reward(self) -> float | None:
        """Poměr očekávaného zisku k očekávané ztrátě."""
        if not self.expected_profit or not self.expected_loss:
            return None
        if self.expected_loss == 0:
            return None
        return abs(self.expected_profit / self.expected_loss)

    @property
    def spread_ok(self) -> bool:
        """True, pokud je aktuální spread v povoleném limitu."""
        if self.option_spread_pct is None:
            return False
        return self.option_spread_pct <= self.max_spread_pct

    def touch(self, message: str = "") -> None:
        """Aktualizuje čas poslední změny a volitelně poznámku ke stavu."""
        self.updated_at = datetime.now()
        if message:
            self.message = message

    def set_state(self, state: FlowState, message: str = "") -> None:
        """Změní stav flow a zaznamená čas změny."""
        self.state = state
        self.touch(message)

    @property
    def runner_active(self) -> bool:
        """True, pokud má obchod aktivní runner s vlastním cílem."""
        return self.runner_profit_target is not None and self.runner_quantity > 0

    @property
    def runner_sl(self) -> float:
        """SL runneru - vlastní hodnota; bez ní (starší stav) společný SL obchodu."""
        if self.runner_stop_loss is not None:
            return self.runner_stop_loss
        return self.stop_loss

    @property
    def held_quantity(self) -> int:
        """Počet kontraktů, které pozice ještě drží (po prodaných runnerech)."""
        total = (self.filled_quantity or self.quantity) - self.runner_sold_quantity
        return max(total, 0)

    @property
    def main_quantity(self) -> int:
        """Počet kontraktů hlavní části pozice (bez běžícího runneru)."""
        drzeno = self.held_quantity
        if self.runner_active and self.runner_quantity < drzeno:
            return drzeno - self.runner_quantity
        return drzeno

    @property
    def open_quantity(self) -> int:
        """
        Počet kontraktů právě otevřených v trhu.
        Před nákupem nula; po nákupu skutečně nakoupené množství snížené
        o prodané runnery a o prodanou hlavní část.
        """
        if self.fill_price is None:
            return 0
        drzeno = self.held_quantity
        if self.exit_fill_price is not None:
            drzeno -= self.main_quantity
        else:
            # Část hlavní části už mohla být prodaná před zrušením příkazů
            drzeno -= self.main_sold_quantity
        return max(drzeno, 0)

    @property
    def open_pnl(self) -> float | None:
        """
        Zisk/ztráta pouze dosud otevřené části pozice v USD.

        Otevřené kusy se oceňují BIDem proti nákupní ceně - prodává se tržním
        příkazem, takže BID odpovídá ceně, za kterou lze pozici právě teď
        skutečně prodat. Realizovaný výsledek už prodaných částí se
        nezapočítává. Bez otevřených kusů None.
        """
        if self.fill_price is None:
            return None
        mnozstvi = self.open_quantity
        if mnozstvi <= 0:
            return None
        if self.option_bid is None:
            return None
        return (self.option_bid - self.fill_price) * mnozstvi * calc.OPTION_MULTIPLIER

    @property
    def runner_multiple(self) -> float | None:
        """Kolikanásobek původní vzdálenosti cíle od vstupu je cíl runneru."""
        if not self.runner_active:
            return None
        zaklad = self.original_profit_target or self.profit_target
        puvodni_vzdalenost = self.pt_distance(zaklad)
        if puvodni_vzdalenost <= 0:
            return None
        return self.pt_distance(self.runner_profit_target) / puvodni_vzdalenost

    @property
    def pt_multiple(self) -> float | None:
        """Kolikanásobek původní vzdálenosti cíle od vstupu je aktuální PT."""
        zaklad = self.original_profit_target or self.profit_target
        puvodni_vzdalenost = self.pt_distance(zaklad)
        if puvodni_vzdalenost <= 0:
            return None
        return self.pt_distance(self.profit_target) / puvodni_vzdalenost

    def option_label(self) -> str:
        """Popis opčního kontraktu pro tabulku, například 'AAPL 20260828 C 230'."""
        if not self.expiration:
            return self.symbol
        return f"{self.symbol} {self.expiration} {self.right} {self.strike:g}"
