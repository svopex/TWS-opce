# Obchodování opcí přes TWS API

Formulářová aplikace pro obchodování opcí přes Interactive Brokers TWS API.
Zadaný obchod čeká na dosažení cenové úrovně podkladu, nakoupí opci
a po nákupu zajistí pozici prodejním příkazem pro PT i SL; část pozice může
běžet jako runner s vlastním, vzdálenějším cílem.

Běží na Windows, macOS i Linuxu — Python + `ib_async` + webové rozhraní NiceGUI.

![Webové rozhraní aplikace – vlevo zadání obchodu a konfigurace, vpravo monitoring obchodů a průběh](obchodovani-opci-tws.png)

## Spuštění

Nejjednodušší cesta — skript sám najde interpret, při prvním spuštění založí
virtuální prostředí, doinstaluje závislosti a aplikaci spustí:

```bash
# macOS a Linux
./run.sh
```

```bat
REM Windows
run.bat
```

Přepínače se skriptu předávají beze změny, například `./run.sh --no-connect`.

Rozhraní pak běží na <http://127.0.0.1:8080>.

### Ruční instalace a spuštění

Interpret se na jednotlivých systémech jmenuje různě — na macOS je to zpravidla
jen `python3`, na Windows `python`, na Linuxu podle distribuce jedno či druhé.

```bash
# macOS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

```bash
# Linux
python3 -m venv .venv          # na některých distribucích: python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

```bat
REM Windows
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Po aktivaci prostředí příkazem `activate` funguje `python` na všech systémech.
Bez aktivace lze aplikaci spustit přímo: `.venv/bin/python main.py`
(na Windows `.venv\Scripts\python.exe main.py`).

Vyžadován je Python 3.10 nebo novější.

### Přepínače

| Přepínač | Význam |
| --- | --- |
| `-c CESTA`, `--config CESTA` | jiný konfigurační soubor (výchozí `config.yaml` vedle `main.py`) |
| `--no-connect` | nepřipojovat se k TWS při startu, spojení se naváže tlačítkem |
| `--verbose` | podrobné logování včetně komunikace `ib_async` |

### Nastavení TWS

V TWS (nebo IB Gateway) je nutné povolit API:
*Global Configuration → API → Settings → Enable ActiveX and Socket Clients*.
Číslo v poli **Socket port** musí souhlasit s `connection.port` v `config.yaml`.

Při prvním spuštění vznikne `config.yaml` jako kopie komentované šablony
`config.example.yaml`, kde je popsána každá volba (není-li šablona po ruce,
vznikne soubor z výchozích hodnot bez komentářů). Chybí-li v souboru některý
klíč, platí výchozí hodnota z kódu — a právě ty se v tomto dokumentu označují
jako „výchozí". Šablona se od nich v několika položkách záměrně liší (port,
velikost účtu a risk, limit spreadu, `trading.auto_close_minutes_before`,
`expiration.min_dte`), takže čerstvě založený `config.yaml` je vždy dobré
projít.

## Jak aplikace pracuje

1. **Zadání** — vyplní se ticker, cena podkladu pro nákup a PT nebo SL
   (druhá úroveň se dopočítá). Aplikace načte cenu podkladu a sama určí zbytek:
   - **PUT/CALL** podle toho, zda vstupní cena leží nad nebo pod aktuální cenou
     (vstup nad trhem = průraz nahoru = CALL, vstup pod trhem = PUT),
   - **strike** odsazený od vstupní ceny na stranu mimo peníze
     (viz [Výběr strike](#výběr-strike)),
   - **expiraci** podle konfigurace (`expiration.mode`: `nearest` = nejbližší
     s aspoň `min_dte` dny do expirace, `fixed` = pevné datum `fixed_date`),
   - **SL**, pokud nebyl zadán, v poměru k PT podle pole *RRR (PT:SL)*
     (výchozí hodnota pole vychází z `trading.sl_to_pt_ratio`, standardně 1:1),
   - **množství** z velikosti účtu, povoleného rizika a delty opce:
     `riskovaná částka / (|vstup − SL| × |delta| × 100)`. Riskovaná částka
     vychází z velikosti účtu — buď z pevné hodnoty v konfiguraci, nebo
     ze skutečného stavu účtu, je-li `account.size: 0`. Delta se přitom bere
     **ve chvíli nákupu** (viz [Delta při vstupu](#delta-při-vstupu)).

   Určený směr ukazuje odznak **LONG (CALL)** / **SHORT (PUT)** vedle
   nadpisu *Zadání obchodu*. Je to jen indikace — objeví se hned po zadání
   tickeru a vstupní ceny (po opuštění pole se načte aktuální cena podkladu
   a směr se určí z polohy vstupu vůči ní), při dalších úpravách vstupu se
   průběžně přepočítává, při načtení běžícího obchodu přebírá jeho směr
   a při přechodu na jiný ticker zmizí. Bez spojení s TWS, kdy cena podkladu
   známá není, se směr napoví aspoň z polohy PT nebo SL na podkladu vůči
   vstupu.

   **PT a SL na podkladu, nebo na opci.** Pod řádkem s polem *Množství* stojí
   oddělené bloky voleb *SL na podkladu (cena podkladu) / SL na opci (ztráta
   v USD/ks) / SL na opci (% prémie)* a *PT na podkladu (cena podkladu) /
   PT na opci (zisk v USD/ks) / PT na opci (% prémie)* (výchozí stav určuje
   `trading.pt_on_underlying` a `trading.sl_on_underlying`). Volba
   *na podkladu* znamená cenu podkladu a hlídá
   ji podmíněný příkaz, jak je popsáno výše. Volba *na opci* je **zisk
   (PT), resp. ztráta (SL) v USD na jeden kontrakt** — 10 znamená posun
   ceny opce o 0,10 (nákup 3,00 → PT limit 3,10, SL stop 2,90). PT na opci
   se po nákupu realizuje limitním příkazem přímo na cenu opce, SL na opci
   stop-market příkazem. Oba režimy lze libovolně kombinovat.
   **PT a SL v procentech prémie.** Třetí volba v obou blocích — *PT na opci
   (% prémie)*, resp. *SL na opci (% prémie)* — zapisuje tutéž úroveň na opci
   podílem ze zaplacené prémie místo částky v USD. Prémie kontraktu je cena
   opce krát 100, takže **jedno procento prémie je právě cena opce v USD**:
   30 % z opce za 3,00 (300 USD) je 90 USD na kontrakt, z opce za 0,50 jen
   15 USD. Napříč různě drahými kontrakty tak každý obchod riskuje stejný díl
   vložených peněz, což pevná částka v USD nedělá — u levné opce bývá neúměrně
   velká (a naráží na strop prémie), u drahé zanedbatelná.

   Je to **jen jednotka formuláře**: procento se před odesláním přepočte na USD
   na kontrakt a obchod, engine i uložený stav dál pracují s USD úplně stejně
   jako u zápisu *na opci [USD/ks]*. Základem přepočtu je **odhadovaná nákupní
   cena** opce, tedy cena v okamžiku, kdy podklad dosáhne vstupní úrovně;
   náhled ji uvádí jako `základ procent: prémie ≈ 317.00 USD/ks`. Celý přepočet
   drží jednu jedinou prémii — do USD i zpět do procent — takže si PT a SL
   navzájem odpovídají, i když příprava nakonec vybere jinak drahý strike.
   Protože se procenta převádějí z ceny opce, kterou aplikace teprve musí
   načíst, sáhne si v tomto režimu do TWS dvakrát — poprvé jen pro odhad ceny
   opce, podruhé už se skutečnými úrovněmi; jakmile odhad pro daný ticker má,
   další načtení jsou jednoprůchodová. V režimech `otm_offset` a `atm` je
   navíc odhad přesný, protože oba průchody vybírají tentýž kontrakt — strike
   na zadaných úrovních nezávisí. Dopočítávaná
   úroveň se do pole vrací také v procentech. Kompenzace **SL o zaplacený
   spread dál** je dostupná i zde — připočte se k přepočtené částce v USD.

   Odeslání bez načtených dat z TWS přepočet provést nemůže a formulář na to
   upozorní; stačí kliknout na *Načíst*. Načtený běžící obchod — ať tlačítkem
   *Načíst*, nebo kliknutím na řádek v přehledu — se do formuláře vrací
   **v téže jednotce, v jaké byl zadán**: obchod si s sebou nese i cenu opce,
   ze které se procenta počítala, takže se přepínač nastaví zpět na *% prémie*
   a úrovně se přepočtou na procenta. Základem zůstává původní prémie, ne
   aktuální kotace — jinak by se zapsaná procenta při každém načtení posunula.
   Obchody z verzí, které si jednotku zadání ještě nepamatovaly, se vrací
   v USD na kontrakt.

   Při PT na opci aplikace dopočítá **úroveň podkladu, kde zisk nastane**:
   z aktuální kotace opce se strikem u vstupu zjistí
   implikovanou volatilitu, spočítá cenu opce v okamžiku vstupu, přičte
   požadovaný zisk a najde úroveň podkladu, kde opce této ceny dosáhne.
   Náhled ji ukazuje jako `cíl na podkladu ≈`; k výběru strike slouží jen
   v režimu `strike.mode: target`. Cena opce pro model se bere
   ze středu BID/ASK, při jednostranné kotaci z ASK (resp. BID), bez kotací
   z poslední (last) a nakonec ze závěrečné
   (close) ceny — TWS obvykle pošle závěrečnou cenu dřív než kotace, proto
   se po ní ještě `engine.quotes_grace_sec` počká na BID/ASK. Teprve bez
   jakékoliv ceny se použije lineární odhad přes deltu, bez delty vstupní
   cena. Zvolená úroveň i zdroj odhadu („z ceny opce (BID/ASK)“, „… (close)“,
   „z delty“) jsou vidět v náhledu formuláře. Počítá-li model ze závěrečné
   ceny, náhled i log na to výslovně upozorní: mimo obchodní hodiny je
   závěrečná cena jediná dostupná, a pohnul-li se mezitím podklad (typicky
   pre-market gap), vyjde z ní nesmyslná volatilita a s ní i dopočítané
   úrovně a množství.
   SL se bez zadání dopočítá stejným poměrem jako dosud (na opci
   `PT × poměr`); při smíšeném režimu se zisk na PT převede mezi podkladem
   a cenou opce stejným modelem, jakým se počítá sloupec *Zisk na PT*.
   Množství při SL na opci vychází přímo ze zadané ztráty:
   `riskovaná částka / SL v USD`. Ztráta se přitom stropí zaplacenou prémií —
   stop nemůže klesnout pod jeden tik, takže i SL zadaný nad prémii odnese
   nejvýš `(nákupní cena − tik) × 100` USD. Stejným stropem prochází
   i sloupec *Ztráta na SL*. Takový stop ale pozici prakticky nechrání —
   spustí se až u téměř bezcenné opce — proto na SL převyšující prémii
   upozorní náhled formuláře (z odhadované ceny opce při vstupu) a po nákupu
   znovu průběh (ze skutečné nákupní ceny), včetně skutečného stropu ztráty.
   Přepnutí režimu vyprázdní přepnuté pole i dopočítávanou úroveň,
   protože hodnota by v novém režimu znamenala něco jiného.

   **SL o zaplacený spread dál.** Zaškrtávátko odsazené pod volbou režimu
   SL (výchozí stav `trading.sl_spread_compensated`), dostupné jen
   při SL zadaném na opci. Řeší to, že opce se kupuje u ASKu, ale stop
   se spouští BIDem: pozice je hned po nákupu v mínusu o celý spread,
   takže SL je ve skutečnosti blíž, než odpovídá zadané ztrátě. Při
   kotaci 3,85 / 3,95 a nákupu za 3,95 stojí SL 30 na 3,65 — stačí pokles
   BIDu o 0,20, ne o 0,30. Se zaškrtnutým přepínačem se k SL při nákupu
   připočte **skutečně zaplacený spread** (nákupní cena minus BID), takže
   zadaná hodnota odpovídá potřebnému pohybu ceny opce: stop klesne
   na 3,55. Ztráta na kontrakt o tentýž spread naroste (30 → 40 USD),
   proto s ní počítá i doporučené množství — náhled používá spread
   z aktuální kotace a uvádí jej jako `+ spread ≈ 10.00 USD`, skutečnou
   hodnotu určí až nákup a zapíše ji do průběhu. Než k němu dojde, ukazují
   sloupce *SL* a *Ztráta na SL* v přehledu odhad **včetně** spreadu
   z aktuální kotace (`≈ -20.00 USD`, resp. odpovídající ztráta), aby se
   hodnota po nákupu neměnila skokem; jakmile je spread znám, značka
   přibližné rovnosti zmizí. Připočtený spread nese
   obchod v poli `sl_spread_usd`, takže *Načíst* vrátí do formuláře
   původně zadanou hodnotu a kompenzace se při dalším zadání neřetězí.
   Break even se nekompenzuje — jeho stop má stát na zaplacené ceně;
   tlačítko *Počáteční SL* se naopak vrací na úroveň včetně spreadu.
   Bez známého BIDu (chybí kotace), nebo při plnění na BIDu či pod ním
   (zaplacený spread je nula), se kompenzace neuplatní a průběh
   to zaznamená. PT se nekompenzuje: dráha k němu je o spread naopak
   delší, protože limitní prodej se vyplní, až na jeho cenu dosáhne BID.

   **Která úroveň je prvotní.** Oranžová dvojice voleb *Zadává se SL, PT se
   dopočítá podle RRR* / *Zadává se PT, SL se dopočítá podle RRR* v prvním
   bloku přepínačů pod řádkem s polem *Množství* (výchozí stav
   `trading.primary_level`, standardně `sl`)
   určuje, která z úrovní se zadává a která se dopočítává podle poměru
   z pole *RRR (PT:SL)*. Zadávaná úroveň stojí vždy vedle vstupu, dopočítávaná
   v dalším řádku – přepnutím si pole PT a SL vymění místo. Prvotní SL =
   povinný je SL a PT se dopočte (na podkladu zrcadlově
   `vstup ± |SL − vstup| / poměr`, na opci `SL / poměr`, ve smíšeném režimu
   přes cenu opce jako výše), prvotní PT = povinný je PT a dopočte se SL.
   Bez vstupní ceny dopočet neproběhne – úrovně se zrcadlí kolem vstupu.
   Poměr se bere z pole **RRR (PT:SL)** vedle pole *Množství*, v řádku
   s tlačítkem *Přepočítat* (tedy nad přepínači). Zadává se jako
   poměr zisku ku riziku — *RRR 2* znamená, že PT je dvakrát dál než SL —
   tedy obráceně než konfigurační `sl_to_pt_ratio`, ze kterého vychází
   výchozí hodnota pole (`RRR = 1 / sl_to_pt_ratio`). Prázdné či nekladné
   pole se vrací ke konfiguraci; obchod si pak uloží poměr, který se
   z konfigurace skutečně vzal, takže se do pole při načtení vrátí vyplněný.
   Dopočítanou úroveň lze vždy přepsat ručně; **Přepočítat** ji spočítá
   znovu — je-li ale prvotní pole prázdné, počítá se naopak z toho vyplněného,
   aby zadání nezmizelo celé. K odeslání proto stačí vstupní cena a kterákoliv
   z úrovní. Na dopočtu PT strike nezávisí — vybírá se od vstupní ceny.

   TWS model greeks u opcí neposílá spolehlivě — závisí to na účtu
   a předplatném dat. Chybí-li delta, aplikace ji dopočítá z tržní ceny opce
   (implikovaná volatilita a z ní delta podle Black-Scholes) a ve formuláři
   ji označí jako dopočítanou. Teprve když nelze ani to, sáhne po náhradní
   hodnotě z konfigurace. Obě tyto delty platí pro **dnešní** cenu podkladu;
   pro výpočty se z nich odvozuje delta v okamžiku nákupu, viz
   [Delta při vstupu](#delta-při-vstupu).
   Formulář má k tomu dvě tlačítka:
   **Načíst** obnoví údaje z TWS (cena podkladu, typ opce, expirace, strike,
   kotace, delta) a vyplněná pole nechá být — doplní jen ta prázdná.
   **Přepočítat** navíc přepíše dopočítávanou úroveň (SL, nebo PT podle
   volby prvotní úrovně) i množství vypočtenými hodnotami; zadaná
   hodnota se přitom zahodí a spočítá znovu podle RRR z formuláře (při
   prázdném poli z konfigurace). Stejně jako *Přepočítat* se chová
   i opuštění pole *RRR* — změna poměru se rovnou promítne do dopočítávané
   úrovně a množství. Ručně zadané hodnoty tedy zmizí pouze na výslovné
   kliknutí nebo po změně RRR, ne samovolně při psaní.
   V náhledu je vždy vidět, co by výpočet doporučil. Dokud načítání dat
   z TWS běží, ukazuje formulář pulzující text „Načítám data z TWS…".

   Běží-li na zadaném tickeru obchod, **Načíst** naplní formulář jeho
   parametry — i přes ručně zadané hodnoty. Totéž udělá kliknutí na řádek
   v přehledu obchodů, jen se obchod nehledá podle tickeru, ale vezme se ten
   kliknutý. Vrací se **celé zadání**: ticker, vstup, obě úrovně, množství,
   limit spreadu, oba přepínače režimu PT a SL (včetně jednotky *% prémie*),
   kompenzace spreadu, volba prvotní úrovně *Zadává se SL / PT* i pole
   *RRR (PT:SL)*. Prvotní úroveň a poměr se z uložených čísel dopočítat nedají
   — obě úrovně se ukládají stejně a poměr by po posunu cíle násobkem vyšel
   jinak —, proto si je obchod pamatuje ze zadání. Obchody z verzí, které je
   ještě neukládaly, obě volby nechávají tak, jak právě jsou.

   Přechod na ticker bez obchodu pole naopak vyprázdní, aby se do nového
   zadání nepřenesly ceny toho předchozího; limit spreadu, přepínače režimů
   PT a SL, kompenzace spreadu i volba prvotní úrovně se vrátí na hodnoty
   z konfigurace a odznak směru zhasne. Samotné opuštění pole hodnoty
   nepřepisuje (s výjimkou pole *RRR*, viz výše), mění je jen změna tickeru.

2. **Nákup** — obchod má smysl jen tehdy, pokud cena podkladu vstupní
   úroveň ještě nepřekonala: u CALL musí být pod vstupem, u PUT nad ním.
   Je-li vstup propásnutý už při odeslání formuláře, aplikace zadání rovnou
   odmítne chybou a obchod nevznikne. Překoná-li cena vstup později, obchod
   skončí ve stavu *Vstup propásnut*, aniž by cokoliv zadal. Monitoring to
   hlídá ve všech stavech před nákupem — tedy i u obchodu blokovaného
   spreadem nebo čekajícího na kotace, a stejně tak mimo obchodní hodiny.
   Právě tam na to dojde nejčastěji: spread opce bývá před otevřením trhu
   široký, takže obchod čeká, a podklad se mezitím přes vstupní úroveň
   propadne či vystřelí (přes noc nebo v pre-marketu).

   Příkaz už čekající v trhu se ruší jen mimo obchodní hodiny, kdy jeho
   cenová podmínka spustit nemůže — jinak by po otevření trhu nakoupil na
   dávno propásnuté úrovni. Během seance zůstává v trhu: cena, která
   vstupní úroveň překonala, je právě ta, na kterou se příkaz plní, a jeho
   zrušení by se s dobíhajícím vyplněním závodilo. (Pracuje-li podmínka
   i mimo obchodní hodiny — `trading.outside_rth` —, platí totéž nepřetržitě.)

   Do TWS se zadá příkaz na opci s cenovou podmínkou na podkladu.
   Dokud se nevyplní, aplikace průběžně upravuje jeho limitní cenu podle
   aktuálního ASK (resp. MID) — lze vypnout přes `trading.relimit_enabled`,
   práh změny udává `trading.relimit_min_change_pct` — a hlídá spread.
   Není-li k dispozici ani cena podkladu, obchod čeká ve stavu *Čeká na
   kotace opce* stejně jako bez kotací opce. Zruší-li obchodník nákupní
   příkaz ručně v TWS, obchod skončí ve stavu *Zrušeno*.
3. **Spread** — překročí-li nastavené procento (`trading.max_spread_pct`,
   ve formuláři pole *Max. spread*), nevyplněný příkaz se odstraní
   z trhu; jakmile se spread vrátí do limitu, příkaz se zadá znovu (obojí
   lze vypnout přes `trading.cancel_on_spread_breach`, resp.
   `trading.rearm_on_spread_ok`). Aby se
   příkaz při kolísání kolem limitu nezadával a nerušil stále dokola, musí
   spread klesnout s rezervou pod limit a od odstranění musí uplynout
   nastavená prodleva (`trading.rearm_spread_margin_pct`,
   `trading.rearm_delay_sec`). Obchod založený při spreadu nad limitem
   začíná rovnou ve stavu *Blokováno spreadem*; totéž platí, přijdou-li
   kotace ze stavu *Čeká na kotace opce* s příliš širokým spreadem.
4. **Zajištění** — po nákupu se zadá prodejní příkaz se dvěma cenovými
   podmínkami na podklad spojenými logickým OR: dosažení PT nebo SL.
   S aktivním runnerem vzniknou příkazy dva — hlavní část a runner, každý
   s vlastním cílem; SL runner při zapnutí přebírá od zbytku pozice a dál
   se přepíná samostatně (u úrovní na opci má každá část svou dvojici
   příkazů, viz níže).
   Vyplnil-li se nákup jen částečně, aplikace nejprve zruší jeho nevyplněný
   zbytek a zajistí skutečně nakoupené množství — TWS totiž nepovolí mít
   na jednom opčním kontraktu současně nákupní i prodejní příkaz.

   Je-li PT nebo SL zadané na opci, nahradí jediný podmíněný příkaz
   **dvojice příkazů**:

   | PT | SL | Příkazy po nákupu |
   | --- | --- | --- |
   | podklad | podklad | jeden MKT (příp. LMT) s podmínkami PT OR SL — beze změny |
   | podklad | opce | MKT s podmínkou PT + stop-market na cenu opce |
   | opce | podklad | limit na cenu opce + MKT s podmínkou SL |
   | opce | opce | limit na cenu opce + stop-market na cenu opce |

   Dvojice je v TWS svázaná OCA skupinou (po vyplnění jednoho příkazu TWS
   druhý úměrně zmenší, resp. zruší — funguje to i ve chvíli, kdy aplikace
   neběží) a navíc ji hlídá aplikace sama: jakmile se jeden příkaz vyplní,
   druhý ihned ruší, aby se opce neprodala dvakrát. Prodá-li se část kusů
   na PT a zbytek po zmenšení na SL, je prodejní cenou vážený průměr obou
   a důvod výstupu „PT+SL“. **Částečné vyplnění** se do přehledu i do modelu
   pozice promítá hned, jak k němu dojde, ne až po dokončení prodeje: prodané
   kusy zmizí z otevřeného množství a další úpravy (sloučení runneru zpět,
   dorovnání po doplněném nákupu, uzavření trhem) se týkají jen zbytku.
   Množství se každému příkazu nastavuje jako „kolik ještě prodat“ zvýšené
   o jeho vlastní vyplnění — TWS totiž bere `totalQuantity` včetně už
   prodaných kusů. Vyplní-li se příkaz na menší množství, než pozice drží,
   skončí obchod ve stavu *Chyba* s údajem, kolik kusů zůstalo bez zajištění. Nepošle-li TWS nákupní cenu opce (tržní nákup
   bez limitu), vezme se jako základ pro úrovně na opci aktuální cena opce
   a aplikace na to upozorní v průběhu. Runner má vlastní
   dvojici ve vlastní OCA skupině. Zmizí-li z dvojice jeden příkaz bez
   vyplnění (například ručním zrušením v TWS), aplikace na to upozorní
   v průběhu, ale nenahrazuje jej naslepo — TWS ruší druhý příkaz i ve
   chvíli, kdy se první teprve vyplňuje, a nový příkaz by opci prodal
   podruhé. Zmizí-li oba příkazy hlavní části, obchod skončí ve stavu
   *Chyba*; zmizí-li oba příkazy runneru, jeho kusy převezme hlavní prodejní
   příkaz a *Chyba* nastane, jen když hlavní příkaz upravit nelze.
   Nastavení `trading.exit_order_type` se týká jen společného podmíněného
   příkazu hlavní části; podmíněný příkaz v dvojici i všechny příkazy
   runneru jsou vždy MKT.
5. **Monitoring** — tabulka ukazuje všechny obchody, jejich ceny a stav.
   Řadí se do čtyř sekcí: nejdřív obchody **držící pozici** (*Nakoupeno*,
   *Nakoupeno – výstup aktivní*, *Uzavírá se*), protože jen u nich jsou
   peníze v trhu, pak ostatní běžící (tedy ty před nákupem), pak dnešní
   ukončené a nakonec starší ukončené sestupně po dnech. Uvnitř každé sekce
   platí abeceda podle tickeru a u téhož tickeru jde nahoru novější obchod.
   Sloupec *Ks* ukazuje zadané množství a za lomítkem počet kontraktů právě
   otevřených v trhu: před nákupem `4/0`, po částečném vyplnění tří ze čtyř
   `4/3`, po prodeji runneru `4/2` a po uzavření celé pozice opět `4/0`.
   Pod každým rozpracovaným obchodem je řada tlačítek **1× 1,5× 2× 2,5× 3×**;
   posunou cíl na násobek jeho původní vzdálenosti od
   vstupu — u vstupu 232 a cíle 235 (tedy 3 body) znamená 2× cíl 238. Počítá
   se vždy z původního zadání, takže opakované klikání násobky neřetězí,
   a tlačítko odpovídající aktuálnímu cíli je barevně zvýrazněné. U nakoupené
   pozice se rovnou upraví podmínka zajišťovacího příkazu; u obchodu před
   nákupem záleží na `trading.pt_change_strike` — `keep` (výchozí) ponechá
   původní strike, `recalculate` podle nového cíle vybere jiný a příkaz
   přezadá (nenajde-li se obchodovatelný strike, zůstane původní). Přepočet se
   uplatní jen v režimu `strike.mode: target`, kde strike na cíli skutečně
   závisí; při výběru od vstupní ceny kontrakt zůstává. Ve formuláři se
   zadává vždy základní cíl 1:1.

   U pozice ve stavu *Nakoupeno – výstup aktivní* jsou před tlačítky cíle
   ještě tlačítka **Počáteční SL**
   a **SL BE** — první vrací stop na hodnotu ze zadání, druhé jej posouvá
   na vstupní cenu (break even). Aktivní volba je zvýrazněná stejně jako
   násobek cíle. Před nákupem se tlačítka nenabízejí — SL tam řídí zadání
   ve formuláři. Je-li cena podkladu ve chvíli přepnutí už na zvolené úrovni
   SL, nebo za ní, nemá smysl čekat na podmínku: aplikace podmíněný příkaz
   rovnou zruší a příslušnou část pozice prodá trhem.
   U úrovní zadaných na opci fungují tlačítka obdobně: násobky cíle násobí
   zisk v USD (2× z 10 USD je 20 USD, limit se posune na 3,20), *SL BE* je
   stop na nákupní ceně opce (ztráta 0 USD) a proražení se měří BIDem opce
   proti stop ceně příkazu (tedy po zaokrouhlení na tik). Nulová ztráta se
   do pole SL ve formuláři nepřenáší — tam by znamenala „nezadáno“ a nešlo
   by s ní přepočítat ani založit obchod; skutečnou úroveň ukazuje přehled.
   Ve sloupcích *PT* a *SL* se taková úroveň ukazuje jako částka v USD
   a po nákupu i s cenou opce, na kterou příkaz míří, např. `3.10 (+10.00 USD)`;
   stop na break even se vypisuje jako `3.00 (BE)`.

   U obchodů, které drží více kontraktů, než kolik jich zabírá runner
   (`trading.runner_quantity`, výchozí 1), je vedle tlačítek cíle i sekce
   **Runner**. Runner je část pozice prodávaná samostatným příkazem
   s vlastním cílem — kliknutím na násobek se zapne (nebo se mu cíl změní),
   *Zrušit runner* ho vypne a prodej se sloučí zpět do jednoho příkazu.
   SL přebírá runner při zapnutí od zbytku pozice; vlastní dvojicí tlačítek
   **Počáteční SL** a **SL BE** se pak jeho stop přepíná nezávisle, takže
   hlavní část může stát na break even a runner dál na původním stopu.
   Když hlavní část dosáhne PT (nebo ji prodáte tlačítkem), obchod zůstává
   otevřený, dokud runner nedoběhne; cíl běžícího runneru jde posouvat
   i poté. Runner jde zapnout před nákupem i za běhu a přežije restart
   aplikace.

   Prodaný runner se zúčtuje do realizovaného výsledku obchodu a jeho
   místo se uvolní — sekce Runner se znovu objeví a ze zbývající hlavní
   části lze oddělit **další runner**, dokud pozice drží víc kontraktů,
   než runner zabírá. Výsledek uzavřeného obchodu sčítá hlavní část se
   všemi prodanými runnery (v závěrečné zprávě jako „runnery N ks ±X USD").
   Sloupce *P/L*, *Zisk na PT* a *Ztráta na SL* naproti tomu ukazují vždy
   jen **dosud otevřený zbytek pozice** — realizovaný výsledek prodaných
   částí do nich nevstupuje a po uzavření obchodu zůstává pomlčka.
   P/L oceňuje otevřené kusy BIDem: prodává se tržním příkazem, takže BID
   odpovídá ceně, za kterou lze pozici právě teď skutečně prodat. Hlavní
   hodnota je po odečtení provize zaplacené za nákup těchto kusů, v závorce
   za ní stojí tatáž částka bez ní — `118.05 (120.00)`; podrobněji
   viz [Provize](#provize).

   U pozice ve stavu *Nakoupeno – výstup aktivní* je na konci sekce Cíl
   tlačítko **Uzavřít pozici** —
   zruší zajišťovací příkaz a prodá hlavní část trhem (bez runneru celou
   pozici); případný runner běží dál se svým cílem. Obdobně **Uzavřít
   runner** na konci sekce Runner prodá trhem jen runner a hlavní část
   nechá být. Tržní prodej se v obou případech zadává až po potvrzení
   zrušení podmíněného příkazu, aby se neprodalo víc kusů, než pozice
   drží; prodá-li se část mezitím na PT či SL, jde trhem jen zbytek.
   Nevyplní-li se tržní prodej do 30 s (TWS jej občas nechá viset ve stavu
   PreSubmitted), aplikace jej zruší a zadá znovu, nejvýše pětkrát; pak
   poslední příkaz ponechá v trhu a průběh vyzve ke kontrole pozice v TWS.
   Prodej všeho najednou zůstává v dialogu tlačítka *Zrušit*.

   Tlačítka se zobrazují jen tehdy, když má jejich akce smysl, a mizí
   s částí pozice, které se týkají: po prodeji hlavní části zmizí sekce
   Cíl (její cíl už není co řídit — spolu s ní zmizí i *Zrušit runner*,
   protože sloučení už není kam provést), po prodeji runneru jeho sekce.
   Po *Uzavřít pozici* zmizí sekce Cíl, běžící runner ale jde řídit dál;
   ve stavu *Uzavírá se* (uzavření celé pozice) zmizí obojí, aby do
   rozjetého prodeje nešlo zasahovat.
   Stejná pravidla vynucuje i aplikace sama, takže se změna cíle nemůže
   omylem zapsat do tržního příkazu.

   Sloupce *Zisk na PT* a *Ztráta na SL* říkají, jak otevřená část pozice
   dopadne, když podklad dosáhne cílové, resp. stop úrovně (runner se
   oceňuje na svém vlastním cíli a SL). Prodejní ceny počítají s vyplněním
   u BIDu — od modelového středu trhu se odečítá půl aktuálního spreadu,
   stejně jako se u nákupu půl spreadu přičítá. Opce se přecení z implikované
   volatility odvozené z její aktuální ceny. Po nákupu se počítá ze skutečně
   dosažené ceny, před nákupem z ceny, na kterou opce vyjde **až podklad
   dosáhne vstupní úrovně** — tam se totiž bude kupovat. Úroveň zadaná
   na opci žádný model nepotřebuje: zisk, resp. ztráta je rovnou zadaná
   částka krát počet otevřených kontraktů.
   Hodnoty se přepočítávají s pohybem trhu. Předpokládá se, že podklad
   úrovně dosáhne brzy a volatilita zůstane stejná — při pozdějším pohybu
   bude výsledek nižší o časový rozpad.
   Tepající zelený puntík v prvním sloupci znamená, že obchod je pod dohledem
   aplikace. Objeví se jen u rozpracovaných obchodů a jen tehdy, když hlídání
   skutečně běží — vyžaduje spuštěnou monitorovací smyčku, navázané spojení
   s TWS a čerstvý průchod. Zhasne tedy i v případě, že se smyčka zasekne.
   Každý řádek má ve sloupci *Stav*, pod odznakem stavu, akci celého
   obchodu: běžící obchod tlačítko **Zrušit** (drží-li pozici, aplikace se
   nejprve zeptá, co s ní), ukončený obchod **Odstranit z přehledu**.
   Vpravo v nadpisu přehledu stojí tlačítko **Uklidit neobchodované**.
   Odstraní z přehledu zrušené a propásnuté obchody — ty, které se nikdy
   nedostaly k nákupu, nenesou žádný výsledek a jen zabírají místo. Do TWS
   se přitom nesahá a na potvrzení se aplikace neptá, protože se nemá co
   ztratit. Zůstávají čekající, otevřené i uzavřené obchody a k nim dvě
   výjimky, které vyžadují pozornost: obchod skončený **chybou** a zrušený
   obchod, který stihl nakoupit a **drží v TWS otevřenou pozici**.
   Vedle stojí tlačítko **Zrušit a smazat vše**. Po potvrzení zruší všechny
   běžící obchody i jejich příkazy v TWS a přehled vyprázdní. Držené pozice se přitom trhem neuzavírají — zajišťovací příkazy
   pro PT a SL zmizí a pozice zůstanou v TWS otevřené bez zajištění, na což
   potvrzovací dialog výslovně upozorní. Uzavřete je proto ručně, nebo místo
   hromadné akce použijte **Zrušit** v řádku, kde lze uzavření trhem zvolit.

Aplikace zvládá více obchodů současně; na jednom tickeru může běžet
zároveň jeden long (CALL) a jeden short (PUT) obchod. Směr zadání určuje
poloha PT na podkladu vůči vstupu (je-li PT na opci, poloha SL; jsou-li obě
úrovně na opci, poloha vstupu vůči aktuální ceně podkladu). Nové zadání
nahrazuje jen **čekající** obchod stejného směru — runner nastavený na
nahrazeném obchodu se přenese. Běží-li na tickeru obchod stejného směru
s otevřenou (nebo právě uzavíranou) pozicí, aplikace zadání odmítne
a obchod je třeba nejprve zrušit v monitoringu.

### Výběr strike

Strike se vybírá **od vstupní ceny**, ne od cíle. Rozhoduje `strike.mode`:

| Režim | Strike |
| --- | --- |
| `otm_offset` | odsazený od vstupu na stranu mimo peníze (výchozí) |
| `atm` | nejbližší vstupní ceně, ať leží nad ní, nebo pod ní |
| `target` | nejbližší cílové úrovni (PT) |

Odsazení v režimu `otm_offset` udává `strike.otm_steps`, a to **v krocích
rastru** opčního řetězce, ne v bodech — jedna hodnota tak platí pro všechny
tickery bez ohledu na jejich cenu. Hodnota `1` znamená první strike nad
vstupem u CALL a první pod vstupem u PUT, `2` ten následující, `0` se chová
jako `atm`. U SPY (rastr 1) je krok dolar, u AAPL (rastr 2,5) dva a půl,
u NVDA (rastr 5) pět. Leží-li vstupní cena přesně na striku — což u kulatých
průrazových úrovní není nic neobvyklého — posune se strike o celý krok dál,
aby kontrakt zůstal mimo peníze.

Proč od vstupu: opce se kupuje teprve ve chvíli, kdy podklad na vstupní
úroveň dorazí, takže právě tam bude trh v okamžiku nákupu stát. Mírně OTM
kontrakt u vstupu má **zdravou deltu** (zhruba 0,30–0,40) a jakmile se cena
vydá k cíli, přechází do peněz a jeho prémium roste zrychleně, jak stoupá
delta. Strike posazený až na cílovou úroveň (`mode: target`) naproti tomu
zůstane po celý obchod mimo peníze: má nízkou deltu, z pohybu podkladu
vytěží málo a časový rozpad ho stačí sníst dřív, než cíl nastane.

Vzdálenost mezi vstupem a cílem tedy strike neurčuje — promítá se do
**množství** (`riskovaná částka / (|vstup − SL| × |delta| × 100)`) a do
úrovní zajišťovacích příkazů. Z toho plyne, že posun cíle u obchodu před
nákupem strike nemění; `trading.pt_change_strike: recalculate` se uplatní
jen v režimu `target`.

Není-li vybraný strike pro zvolenou expiraci v TWS obchodovatelný, zkusí se
sousední. Rastr bývá rovnoměrný, takže oba sousedé leží od hledané úrovně
stejně daleko — v režimech `otm_offset` a `atm` pak dostane přednost ten
mimo peníze, aby režim dodržel, co slibuje; v režimu `target` rozhoduje jen
vzdálenost od cíle. Náhradu aplikace hlásí varováním v náhledu.

**Kontrola delty.** Meze `strike.delta_warn_min` a `strike.delta_warn_max`
(výchozí 0,25 a 0,60) nejsou kritériem výběru, jen kontrolou: vypadne-li
delta vybrané opce z pásma, náhled upozorní. Nízká delta znamená kontrakt
tak daleko mimo peníze, že se z pohybu podkladu skoro nezhodnotí; vysoká
kontrakt hluboko v penězích, který zbytečně draho platí vnitřní hodnotu.
Porovnává se absolutní hodnota, protože u PUT je delta záporná, a bere se
delta **při vstupu** (viz níže). Hodnota `0` příslušnou kontrolu vypne.
Chybí-li delta úplně, upozorní na to samostatné varování a množství se
spočítá s náhradní hodnotou `trading.default_delta`.

### Delta při vstupu

Delta, kterou posílá TWS — a stejně tak ta, kterou si aplikace dopočítá
z ceny opce — popisuje **dnešní** cenu podkladu. Jenže obchod nakupuje až
ve chvíli, kdy podklad dosáhne vstupní úrovně, a do té doby se delta změní.
U zadání vzdáleného od trhu je rozdíl zásadní:

| | strike | podklad teď | vstup | delta teď | delta při vstupu |
| --- | --- | --- | --- | --- | --- |
| TSLA CALL | 367,5 | 347,42 | 366,50 | 0,07 | **0,48** |
| QQQ CALL | 722,5 | 718,74 | 722,13 | 0,28 | **0,49** |

Opce je dnes hluboko mimo peníze, u vstupu ale bude prakticky na penězích.
Aplikace proto deltu **přepočítá na vstupní úroveň**: z ceny opce odvodí
implikovanou volatilitu a s ní spočítá deltu pro cenu podkladu ve chvíli
nákupu. Je to týž model, jakým se odhaduje nákupní cena opce, a platí pro
něj stejný předpoklad — pohyb nastane brzy a volatilita se nezmění.

Podle této delty se řídí **doporučené množství** i **kontrola mezí**.
Kdyby se počítalo z dnešní delty, vyšla by odhadovaná ztráta na kontrakt
mnohem menší, než jaká ve skutečnosti hrozí, a obchod by nakoupil násobně
víc kontraktů, než odpovídá povolenému riziku; kontrola mezí by zase planě
varovala u každého zadání položeného dál od trhu.

Náhled formuláře ukazuje obě hodnoty vedle sebe — `delta 0.068 → při vstupu
0.480` — kdykoliv se liší aspoň o 0,005. Bez ceny opce (chybí kotace,
typicky mimo obchodní hodiny) model spočítat nelze a zbývá delta z TWS,
stejně jako dřív.

### Mimo obchodní hodiny

Před otevřením amerického trhu (15:30–22:00 SEČ / SELČ) TWS u opcí neposílá
BID ani ASK. Bez nich nelze určit limitní cenu, proto obchod zůstane ve stavu
**Čeká na kotace opce** a příkaz se do trhu zadá automaticky, jakmile kotace
dorazí. Aplikace v takové situaci záměrně nezadává tržní příkaz, který by se
vyplnil za neznámou cenu. Při nastavení `entry_order_type: MKT` se příkaz
zadá i bez kotací.

### Automatické uzavření před koncem obchodování

Několik minut před zavřením burzy (`trading.auto_close_minutes_before` —
v šabloně 5 minut, chybí-li klíč, 15) aplikace sama ukončí všechny běžící
obchody: čekající obchody zruší a odstraní jejich nákupní příkazy z trhu,
otevřené pozice prodá tržním příkazem. Uzavírací okno trvá až do zavření
burzy, takže obchod založený uvnitř okna se zruší, resp. uzavře hned;
o víkendu se nic neděje. Do hlavičky stránky se přes den promítá odpočet
do začátku uzavírání. Takto uzavřená pozice má v přehledu výsledků důvod
výstupu „ručně".

Čas se počítá v časové zóně burzy (`trading.exchange_timezone`, výchozí
`America/New_York`), takže posuny letního a zimního času vůči místnímu času
počítače nehrají roli. Čas zavření burzy určuje `trading.exchange_close_time`
(výchozí 16:00 newyorského času); zkrácené obchodní dny před svátky aplikace
nezná. Funkci lze vypnout pomocí `trading.auto_close_enabled: false`.

### Odpočet do otevření trhu

Mimo obchodní hodiny ukazuje hlavička odpočet do nejbližšího otevření burzy.
Čas otevření určuje `trading.exchange_open_time` (výchozí 09:30 newyorského
času) a počítá se ve stejné časové zóně jako uzavírání. Po zavření a o víkendu
odpočet míří na otevření následujícího obchodního dne, proto se u delších pauz
vypisuje i počet dní; svátky aplikace nezná. Během seance je odpočet skrytý
a nastavení nijak neovlivňuje obchodování.

### Kvalita spojení

Mezi stavem spojení a tlačítkem *Odpojit* stojí ukazatel ve tvaru
`TWS 0,8 ms · data 0,4 s`. Odpojená aplikace jej skrývá.

**TWS** je odezva na dotaz na aktuální čas — nejlevnější zprávu API, která jde
tam a zpět bez tržních dat. Měří tedy **samotnou aplikaci TWS**, ne síť k IB:
běží-li TWS na témže počítači, jsou to jednotky milisekund a hodnota říká
hlavně to, že TWS není zatuhlá. Přes síť je to desítky milisekund. Měří se
vlastním, řidším tempem než překreslování tabulky — interval určuje
`ui.latency_interval_sec` (výchozí 5 s), hodnota `0` měření vypne a místo
odezvy se ukáže pomlčka.

**data** je stáří nejčerstvější kotace ze všech odebíraných kontraktů, tedy
odpověď na otázku, zda proud dat teče. Bere se nejnovější čas, ne nejstarší:
nelikvidní opce se aktualizuje zřídka i při zcela zdravém spojení, kdežto
stojící maximum znamená, že nepřichází nic. Bez odběrů je i tady pomlčka.

Ukazatel **zoranžoví** při odezvě nad 500 ms nebo když kotace stojí déle než
15 s. Stará data se hlásí jen během seance — mimo obchodní hodiny trh nic
neposílá a trvale svítící varování by ztratilo význam.

Údaj o kvalitě spojení je informativní; obchodování neovlivňuje.

### Stavy obchodu

| Stav | Význam |
| --- | --- |
| Připravuje se | obchod se právě zakládá, příkaz ještě není v trhu (přechodný stav) |
| Před nákupem | příkaz je v trhu a čeká na cenovou podmínku |
| Blokováno spreadem | spread je nad limitem, příkaz není v trhu |
| Čeká na kotace opce | z TWS nedorazily BID/ASK (nebo cena podkladu), limitní příkaz zatím nelze zadat |
| Nakoupeno | opce koupena, zadává se prodejní příkaz |
| Nakoupeno – výstup aktivní | pozice je zajištěna příkazem pro PT i SL |
| Uzavírá se | pozice se na pokyn obchodníka (nebo automaticky před koncem seance) uzavírá tržním příkazem |
| Uzavřeno | pozice uzavřena — na PT, SL, trhem na pokyn obchodníka nebo automaticky před koncem seance |
| Vstup propásnut | cena překonala vstupní úroveň, příkaz se nezadal (nebo se čekající příkaz odstranil z trhu) |
| Zrušeno | obchod ukončen uživatelem, nebo nákupní příkaz zrušen ručně v TWS |
| Chyba | zásah zvenčí (například ruční zrušení prodejního příkazu v TWS), selhání obnovy po restartu nebo chyba monitoringu |

### Zrušení obchodu, který drží pozici

Zrušení obchodu vždy odstraní i zajišťovací příkaz pro PT a SL. Drží-li obchod
otevřenou pozici, aplikace se proto nejprve zeptá, co s ní:

* **Uzavřít pozici trhem a ukončit obchod** — zruší příkazy a pozici prodá
  příkazem MKT. Obchod projde stavem *Uzavírá se* a skončí jako *Uzavřeno*.
* **Ponechat pozici otevřenou** — zruší jen příkazy. Pozice zůstane v TWS
  **bez zajištění** a musíte ji ohlídat sami.
* **Nedělat nic** — zrušení se neprovede.

U obchodu, který ještě nenakoupil, se nic nedotazuje — zruší se rovnou.

### Pozice bez dozoru

Aplikace průběžně kontroluje opční pozice na účtu a na ty, ke kterým nevede
žádný běžící obchod (zajišťovací příkazy v TWS se přitom neposuzují), upozorní
červeným pruhem v záhlaví a hláškou
v průběhu. Běží-li přitom obchod na stejném tickeru, upozornění výslovně uvede,
že se týká **jiného kontraktu** — jinak snadno vznikne dojem, že je pozice
pod dozorem, přestože obchod míří na jiný strike nebo expiraci. Sama k nim
nic nezadává — nezná jejich PT ani SL. Interval kontroly
je `engine.unmanaged_check_sec` (výchozí 30 s); `0` vypne průběžnou kontrolu,
při startu a po každém připojení k TWS však proběhne vždy.

## Přehled výsledků

Vedle nadpisu *Monitoring obchodů* stojí tlačítko **Výsledky**. Otevře popup
přes celou obrazovku s přehledem obchodního dne — co se obchodovalo, co ještě
běží a s jakým výsledkem. Monitorovací tabulka zůstává beze změny, přehled je
pouze pohled navíc; po zavření se nic neděje.

Přepínač **Dnes / Vše** v pravém horním rohu určuje rozsah. *Dnes* bere obchody
založené dnešního dne a k nim všechny, které stále běží (aplikace může běžet
přes noc nebo obnovit stav z předchozího dne). *Vše* ukazuje celý obsah
monitoringu bez ohledu na datum.

Vedle něj je **přepínač světlého a tmavého vzhledu** — tentýž, jaký stojí
v hlavičce stránky, jen ji přehled přes celou obrazovku zakrývá. Přepíná
vzhled celé aplikace včetně barev grafů, volba se pamatuje mezi spuštěními
(výchozí hodnota je `ui.dark` v konfiguraci) a přehled se překreslí ihned.

Obsah se obnovuje ze stejné smyčky jako tabulka, takže otevřené pozice v něm
tikají živě. Rozvržení je navržené na jednu obrazovku bez posuvníku — posouvají
se nejvýš samotné seznamy uvnitř svých panelů.

### Provize

Všechny výsledky v přehledu jsou uvedené **po odečtení provizí**, které
skutečně naúčtovalo TWS; v závorce za nimi stojí tatáž částka bez nich:

```text
-92.00 (-85.00)     výsledek s provizí a bez ní
```

Provize se přebírají z hlášení o vyplnění (`commissionReport`) a vedou se
po jednotlivých exekucích, takže se nemohou započítat dvakrát. Hlášení dorazí
z TWS až krátce po vyplnění příkazu — hodnota se proto může o vteřinu opozdit.
Zaplacené provize se ukládají do stavu obchodů, takže restart aplikace o ně
nepřijde. Bez zaplacené provize (a bez spojení s TWS) se závorka nevypisuje.

U otevřené pozice je odečtena jen provize za nákup — prodejní vznikne teprve
prodejem. Uzavřenému obchodu patří obě strany.

### Souhrnné dlaždice

| Dlaždice | Co ukazuje |
| --- | --- |
| Výsledek dne | realizovaný i otevřený výsledek dohromady, v popisku i celkem zaplacené provize |
| Realizováno | výsledek už prodaných kusů; zvlášť se uvádí část z obchodů, které dosud běží (prodaný runner) |
| Otevřené pozice | nerealizovaný výsledek otevřených pozic oceněný BIDem |
| Úspěšnost | podíl ziskových obchodů z ukončených, které skutečně nakoupily |
| Profit factor | poměr součtu ziskových obchodů ke ztrátovým, pod ním průměrný zisk a průměrná ztráta |
| Obchody | kolik jich běží, kolik skončilo a kolik se nedostalo k nákupu |

Statistiky úspěšnosti počítají **jen ukončené obchody s nákupem** — běžící
pozice se do nich nezapočítává, dokud se výsledek může ještě otočit, a
propásnutý či před vstupem zrušený obchod se nikdy neodehrál. O tom, zda obchod
skončil v zisku, rozhoduje výsledek **po provizích** — těsný zisk umí provize
otočit ve ztrátu a úspěšnost i profit factor by jinak byly optimističtější,
než jaká byla skutečnost.

### Panely

* **Běží teď** — otevřené i čekající obchody s nákupní cenou, aktuálním BIDem
  a otevřeným P/L. Pruh *SL → PT* ukazuje, kde pozice stojí mezi stop-lossem
  (vlevo) a cílem (vpravo); vychází z otevřeného výsledku proti očekávanému
  zisku na PT a ztrátě na SL, takže funguje ve všech režimech zadání úrovní.
* **Uzavřené obchody** — od nejnovějšího, s datem a časem vstupu (nákup)
  a výstupu (uzavření pozice), dosaženými cenami, dobou držení
  a důvodem výstupu (PT, SL, PT+SL při prodeji na obou příkazech dvojice,
  PT/SL nelze-li rozlišit, ručně — včetně automatického uzavření před koncem
  seance —, propásnuto, zrušeno, chyba). V hlavičce panelu stojí nejlepší
  a nejhorší obchod dne. Pruh *Porovnání*
  vynáší výsledek proti největšímu výsledku dne — ztráta doleva, zisk doprava.
* **Průběh dne** — kumulovaný realizovaný výsledek po provizích, bod za každý
  uzavřený obchod.
* **Výsledek podle tickeru** — součet realizovaného i otevřeného výsledku
  po provizích, po tickerech, seřazený od nejlepšího po nejhorší.

## Načtení pozic ze souboru

Vedle nadpisu *Zadání obchodu* stojí tlačítko **Načíst ze souboru**. Otevře
popup formulář, ve kterém se vybere YAML soubor se zadáním obchodního dne
(například `2026-08-25.yaml`) a všechny pozice z něj se zadají naráz.

Ze souboru se čerpá **jen z položek, jejichž klíč končí plusem** (`AMZN Long+`),
a to pouze ze tří údajů — `symbol`, `entry_price` a `target_price`. Ostatní
pole souboru patří jinému nástroji a aplikace je ignoruje; položky bez plusu
se přeskočí. Vadná položka (chybějící ticker či cena, cíl shodný se vstupem)
načtení nezastaví — přeskočí se a důvod se vypíše nad tabulkou, aby zbytek
souboru zůstal použitelný.

Nad tabulkou se volí režim cíle, společný všem načteným pozicím:

- **PT na podkladu v % dráhy k cíli** — PT je cena
  podkladu, spočítaná jako `vstup + (target_price − vstup) × %/100`. Při 100 %
  je to přesně cílová cena ze souboru, při 50 % půlka cesty k ní; znaménko
  rozdílu řeší směr, takže vzorec platí pro long i short. SL se dopočítá
  rovněž **na podkladu** podle pole *RRR (PT:SL)*.
- **PT na opci v USD/ks** — PT je zisk na jedné opci v USD, společný
  všem pozicím, a SL je ztráta na opci podle téhož poměru. Cílová cena ze
  souboru se v tomto režimu nepoužívá.
- **PT na opci v % prémie** — totéž, ale zadané podílem ze zaplacené prémie.
  Prémie kontraktu je cena opce krát 100, takže jedno procento prémie je právě
  cena opce v USD: 30 % z opce za 3,00 (300 USD) je 90 USD na kontrakt, z opce
  za 0,50 jen 15 USD. Napříč košíkem tak každá pozice riskuje stejný **díl
  vložených peněz**, což pevná částka v USD nedělá — u levné opce bývá
  neúměrně velká (a naráží na strop prémie), u drahé zanedbatelná.

  Procento se vztahuje k **odhadované nákupní ceně** opce, tedy k ceně
  v okamžiku, kdy podklad dosáhne vstupní úrovně. Aplikace ji zjistí tak, že
  nejprve připraví zadání s cílem ze souboru na podkladu, z vybraného kontraktu
  vezme odhad ceny a teprve z něj spočítá PT v USD — proto sahá do TWS dvakrát
  a příprava je o něco pomalejší. Použitá prémie se ukáže ve sloupci *Stav*
  (`Připraveno · prémie ≈ 300.00 USD`). Do pole *PT* se zapíše výsledek v USD/ks, takže se dá
  ručně doladit; dál obchod běží jako běžné zadání na opci. Použitá prémie jde
  s obchodem dál, takže se v běžném formuláři vrátí zase v procentech —
  s výjimkou ručně přepsaného PT, které už z prémie nevychází.

Volba cíle určuje **shodně režim PT i SL** — obě úrovně tak vyjdou ve stejné
jednotce a po kliknutí na řádek v přehledu se ve formuláři ukážou souhlasně:

| Režim cíle v dialogu | PT i SL v běžném formuláři |
|---|---|
| *PT na podkladu v % dráhy k cíli* | *na podkladu (cena podkladu)* |
| *PT na opci v USD/ks* | *na opci (zisk / ztráta v USD/ks)* |
| *PT na opci v % prémie* | *na opci (% prémie)* |

Běžný formulář má pro PT a SL **dva samostatné přepínače** a dá se v něm
nastavit každá úroveň jinak; dialog obě úrovně záměrně sváže do jediné volby,
protože dopočítaný SL tu vlastní přepínač nemá a bez svázání by se do obchodu
uložil v jiné jednotce než cíl.

Do tabulky se PT i SL zapisují vždy v jednotce, se kterou počítá aplikace —
v režimu na podkladu je to cena podkladu, v obou opčních režimech **USD/ks**.
Jednotku nese hlavička sloupce (*PT [podklad]*, resp. *PT [USD/ks]*, obdobně
u SL). Procento prémie
je jen jednotka zadání: v tabulce je už přepočtené na USD/ks, ale obchod si ji
pamatuje, takže tytéž úrovně ukáže běžný formulář zase v procentech — a tedy
jiným číslem než dialog.

Řádek, jehož čísla vznikla ještě v předchozím režimu (typicky proto, že byl
při přepnutí zamčený otevřenou pozicí), nebo u kterého příprava selhala, se
sám nezaškrtne a tlačítko *Zadat* jej odmítne s výzvou k přepočtu — jinak by
úroveň odešla do trhu ve špatné jednotce.

V obou opčních režimech (USD/ks i % prémie) má smysl zaškrtávátko
**SL o zaplacený spread dál** — chová se stejně jako v běžném formuláři.

**Runner** se nastavuje u každé pozice zvlášť — comboboxem ve stejnojmenném
sloupci tabulky (*Bez* / *1×* / *1,5×* / *2×* / *2,5×* / *3×*). Sada tlačítek
*Nepoužít runner* / *1×* … *3×* nad tabulkou slouží jako **výchozí hodnota**:
přepne volbu u všech nezamčených řádků naráz (tedy i u řádků, jejichž obchod
teprve čeká před nákupem nebo už skončil), takže stačí nastavit ji globálně
a jednotlivé pozice pak jen doladit. Výchozí stav je *Nepoužít runner*.

Zvolený násobek zapne u založeného obchodu runner (počet kusů podle
`trading.runner_quantity`) s cílem na tomto násobku původní vzdálenosti PT od
vstupu — přesně jako tlačítka runneru v řádku přehledu. Zapíná se až na hotovém
obchodu, takže před nákupem si obchod volbu jen zapamatuje a zajišťovací příkazy
se po nákupu založí rovnou rozdělené. Pozice s příliš malým množstvím runner
nedostane (vyžaduje víc kontraktů, než je jeho velikost); obchod se přesto
založí a důvod se objeví ve sloupci *Stav*.

Vedle režimu se zadává **Max. spread [%]** a **RRR (PT:SL)** pro dopočet SL —
*RRR 2* dá SL na polovině vzdálenosti PT. Obojí vychází z konfigurace. Tlačítkem
**Přepočítat** se PT, SL i množství u všech nezamčených pozic spočítají
znovu — i u těch, jejichž obchod ještě čeká před nákupem (jejich sloupec
*Stav* pak ukazuje výsledek přípravy, dokud se řádek znovu nezadá);
tlačítko ↻ v řádku přepočte jedinou pozici a **ponechá** v ní ručně
upravené PT. Množství se určuje stejně jako v běžném formuláři — z riskované
částky, delty opce při vstupu a vzdálenosti ke SL, resp. přímo ze ztráty
na kontrakt.

Tabulka ukazuje u každé pozice směr ze souboru, vstupní i cílovou cenu, vybraný
opční kontrakt a **editovatelná pole PT, SL a Ks**. Skutečný směr určuje
aplikace z aktuální ceny podkladu jako vždy; liší-li se od směru daného souborem,
trh už vstupní úroveň překonal — takový řádek se označí jako *Vstup propásnut*
a **odškrtne**, aby se omylem nezaložil obchod na opačnou stranu. Zároveň se
zahodí dopočet (*SL* a *Ks*) — patřil by k opačnému kontraktu, než jaký by
obchod koupil. Samotné zaškrtnutí proto k zadání nestačí: řádek jde poslat do
trhu, až když se hodnoty vyplní ručně, nebo se přepočet podaří ve správném
směru.

Tlačítko **Zadat vybrané pozice do trhu** založí obchody postupně, každý stejným
způsobem jako ruční zadání formulářem — včetně všech kontrol. Chyba jedné pozice
ostatní nezastaví, zapíše se do jejího sloupce *Stav*. Po dobu zakládání je
tlačítko zakázané, takže druhý stisk nespustí souběžnou dávku a tytéž pozice
neodejdou do trhu dvakrát; zadávat nelze ani během probíhajícího přepočtu. Bez
spojení s TWS se pozice načtou a PT vyplní (je to čistý výpočet ze zadání) —
s výjimkou režimu *% prémie*, kde PT potřebuje odhad ceny opce z TWS; SL ani
množství se bez spojení dopočítat nedají.

### Opakované zadání téže pozice

Řádek si obchod, který z něj vznikl, pamatuje a jeho sloupec *Stav* pak ukazuje
**živý stav toho obchodu** (`Zadáno AMZN-5 – Před nákupem`). Zaškrtnutí se po
zadání sundá, aby druhý stisk tlačítka tentýž řádek neposlal do trhu podruhé —
zaškrtnout jej ale lze znovu a pozici tím **přepsat**. Platí přitom stejné
pravidlo jako ve formuláři zadání:

| Stav založeného obchodu | Řádek |
| --- | --- |
| Připravuje se, Před nákupem, Blokováno spreadem, Čeká na kotace opce | lze zadat znovu — engine původní obchod zruší, odstraní z přehledu a nahradí novým (nastavení runneru se přenese) |
| Nakoupeno, Nakoupeno – výstup aktivní, Uzavírá se | **zamčeno** — obchod drží pozici, nejprve jej zrušte v monitoringu |
| Uzavřeno, Zrušeno, Vstup propásnut, Chyba | lze zadat znovu — vznikne nový obchod, ten původní zůstává v přehledu |
| smazán z monitoringu | lze zadat znovu |

Zámek se přepočítává průběžně, dokud je dialog otevřený. Vyplní-li se nákup,
řádek se zamkne sám; smažete-li obchod z monitoringu, sám se odemkne — soubor
kvůli tomu není potřeba načítat znovu.

Každé otevření formuláře navíc **zaškrtne všechny řádky, které lze zadat**, takže
se dá celý soubor poslat do trhu znovu jedním tlačítkem. Obnoví se tím i zaškrtnutí
sundané dřívějším zadáním; zamčený řádek a řádek s propásnutým vstupem (nebo
bez platné přípravy) zůstávají odškrtnuté, dokud se hodnoty nevyplní ručně
nebo přepočet neprojde. Kdyby se přesto takový řádek poslal do trhu, zadání
odmítne ještě engine a důvod zapíše do sloupce *Stav* — obchod na opačnou
stranu tedy nevznikne.

## Velikost účtu

Riskovaná částka se počítá z velikosti účtu, kterou lze zadat dvěma způsoby:

| `account.size` | Chování |
| --- | --- |
| kladná hodnota (např. `5000.0`) | použije se přesně tato částka |
| `0` | velikost se převezme z TWS (NetLiquidation) a průběžně obnovuje |

Při `0` odpovídá riziko skutečnému stavu účtu včetně otevřených pozic; hodnota
se načítá po připojení a dál se obnovuje v intervalu `engine.account_refresh_sec`
(výchozí 60 s). Dokud ji TWS nepošle, aplikace na to upozorní ve formuláři
a doporučené množství spadne na minimum `trading.min_quantity`. V panelu
*Konfigurace* je vždy vidět, odkud hodnota pochází — `(config)`, `(z TWS)`,
nebo že se na hodnotu z TWS teprve čeká.

## Konfigurace

Vše podstatné je v `config.yaml` (podrobné komentáře u každé položky):

* `connection` — spojení s TWS (adresa, port, `client_id`, účet, typ tržních
  dat `market_data_type`, automatické znovupřipojení `auto_reconnect`),
* `account` — velikost účtu a riskované procento `risk_pct`,
* `trading` — typ nákupního příkazu (`LMT_ASK` / `MKT` / `LMT_MID`) a jeho
  průběžný přepočet, typ prodejního příkazu, limit spreadu a jeho
  hlídání, poměr SL:PT, výchozí režimy PT a SL, prvotní úroveň, kompenzace
  spreadu, meze množství, runner, chování strike při posunu cíle, doba
  platnosti příkazů a automatické uzavírání před koncem seance,
* `expiration` a `strike` — výběr expirace a strike,
* `engine` — časování monitorovací smyčky, čekání na tržní data, kontrola
  pozic bez dozoru a obnova velikosti účtu,
* `state` — ukládání stavu obchodů,
* `ui` — adresa a port rozhraní, tempo překreslování, měření odezvy, tmavý
  vzhled a délka provozního logu.

## Testy

```bash
python -m unittest discover -s . -p "test_*.py"
```

Testy běží proti náhradě TWS (`tests/fake_ib.py`, společný základ
v `tests/zaklad.py`) — pokrývají výpočty (`tests/test_calc.py`),
čtení tržních dat (`tests/test_ib_service.py`) i celý průběh obchodu včetně
příkazů, jejich podmínek a runneru (`tests/test_engine.py`), režimy PT/SL
na opci (`tests/test_rezimy.py`), načítání pozic ze
souboru (`tests/test_import.py`), souhrn obchodního dne
(`tests/test_report.py`) a obnovu po restartu (`tests/test_obnova.py`).
Spojení s TWS není potřeba. Jeden test se přeskočí, není-li v kořeni
repozitáře vzorový soubor se zadáním dne `2026-08-25.yaml`.

## Struktura

```
run.sh, run.bat          spuštění na macOS/Linuxu, resp. Windows
main.py                  spuštění aplikace
requirements.txt         závislosti (ib_async, nicegui, PyYAML)
config.example.yaml      komentovaná šablona konfigurace
obchodovani-opci-tws.png snímek rozhraní pro tento dokument
tws_opce/
  config.py              načtení, validace a založení konfigurace ze šablony
  calc.py                výpočty (typ opce, SL, spread, množství, limity)
  models.py              model obchodu a jeho stavy
  ib_service.py          obálka nad ib_async (kontrakty, data, příkazy)
  engine.py              řízení obchodů a monitorovací smyčka
  store.py               ukládání stavu obchodů na disk
  importer.py            načtení vstupních pozic ze souboru se zadáním dne
  import_dialog.py       popup formulář hromadného zadání načtených pozic
  report.py              souhrn obchodního dne pro přehled výsledků
  report_dialog.py       popup s přehledem výsledků na celou obrazovku
  ui.py                  webové rozhraní
  static/styles.css      styly
tests/                   testy (fake_ib.py = náhrada TWS, zaklad.py = společný základ)
```

Za běhu vznikají (a ve verzování jsou ignorované) `config.yaml`, `state.json`
a adresář `.nicegui/`, kde si rozhraní pamatuje volbu tmavého vzhledu.

## Stav obchodů a restart

Stav obchodů se průběžně zapisuje do `state.json`, takže restart ani pád
aplikace o rozpracované obchody nepřipraví. Součástí zápisu jsou i provize
naúčtované TWS — po novém spojení je TWS pošle jen za dnešní den, takže bez
uložení by se u starších obchodů ztratily. Po startu se uložený stav **vždy
srovná se skutečností v TWS** — rozhoduje to, co je v TWS, nikoliv zápis
v souboru:

| Co aplikace po startu najde | Jak zareaguje |
| --- | --- |
| pozice a k ní prodejní příkaz | pokračuje v hlídání |
| pozice bez prodejního příkazu | zajištění doplní |
| prodaná hlavní část a běžící runner | nechá runner běžet, nic nezadává (chybí-li i příkaz runneru, zadá jej znovu) |
| rozdělané uzavírání trhem | dokončí je (tržní prodej dál hlídá) |
| pozice uzavřená během výpadku | označí obchod za uzavřený (pozná to z vyplněného prodejního příkazu se značkou) |
| nákupní příkaz čekající v trhu | naváže na něj |
| nákupní příkaz vyplněný jen zčásti | zbytek nákupu zruší a zajistí vyplněné kusy |
| nákupní příkaz, který v TWS není | zadá jej znovu za stejných podmínek jako nové zadání — propásnutý vstup nebo široký spread jej zastaví (platí i pro příkaz zrušený ručně v TWS během výpadku) |
| nakoupený obchod bez pozice i příkazu | označí jako chybu k ruční kontrole |

Zajištění se posuzuje po částech pozice zvlášť: hlavní část, která už je
prodaná, ani část právě uzavíraná trhem žádné nepotřebují, takže se kvůli
nim nesahá na zdravé příkazy té druhé. Vyplněný příkaz se přitom za živý
nepovažuje — prodal, co měl, a v trhu po něm nic nezůstalo. Kolik kusů
se má zajistit, říká **pozice v TWS**, ne nákupní příkaz: po restartu
uprostřed rozprodávání bývá nižší.

Aby aplikace své příkazy poznala, značkuje je v poli `orderRef` zápisem
`TWSOPCE:<obchod>:entry`, `:exit`, resp. `:runner`; druhý příkaz dvojice
(SL na opci nebo na podkladu při odděleném výstupu) nese `:exitsl`,
resp. `:runnersl`. Cizích příkazů na účtu si nevšímá,
takže vedle ní můžete obchodovat i ručně.

Drží-li účet pozici a z dvojice prodejních příkazů přežil jen jeden (nebo
chybí jen příkaz runneru), aplikace přeživší příkazy zruší, počká na
potvrzení a zajištění založí znovu celé. Chybí-li zajištění části pozice
a zároveň probíhá uzavírání trhem, nic nezakládá a jen vyzve ke kontrole
pozice v TWS — nové zajištění by se sčítalo s běžícím tržním prodejem.

Ztratí-li se soubor se stavem, aplikace podle značek dohledá **nákupní
příkazy `:entry`** — čekající i vyplněné, vrátí-li je TWS (po novém spojení
je posílá jen za dnešní den) — a obchody z nich sestaví: vstupní cenu
z cenové podmínky příkazu, PT a SL z podmínek zajišťovacího příkazu;
u příkazů na cenu opce odvodí zisk a ztrátu v USD z limitní, resp. stop ceny
proti nákupní ceně. Převzatý obchod si nese i **nákupní cenu opce**
z vyplněného příkazu — bez ní by úrovně zadané na cenu opce nešlo spočítat
ani měnit. Přežije-li jen příkaz pro SL (`:exitsl`), pozná se z něj režim SL
i jeho hodnota; stop na nákupní ceně znamená break even. Chybí-li některá
úroveň v příkazech, dopočítá se (PT ze strike, SL z poměru v konfiguraci)
a obchod se označí za dopočítaný. K vyplněnému nákupu bez zajištění
aplikace zajištění doplní. Samotné zajišťovací příkazy (`:exit`, `:runner`)
bez nalezeného `:entry` obchod neobnoví; runner se z příkazů nerekonstruuje
vůbec — jeho příkazy zůstanou v TWS živé, ale obchod je nehlídá.

Co takto zachránit nelze, je **už nakoupená pozice, k níž TWS nevrátí ani
vyplněný nákupní příkaz se značkou**. Aplikace na každou opční pozici, ke
které nemá obchod, upozorní hláškou „POZOR" v průběhu a nechá ji na vás —
sama k ní nic nezadává, protože nezná původní PT ani SL.

Totéž proběhne po **každém obnovení spojení** — po ručním odpojení a připojení
tlačítkem i po výpadku sítě. Objekty příkazů z minulého spojení už nejsou platné,
takže se obchody pokaždé znovu spárují s tím, co je skutečně v TWS.

Podmínkou je, aby aplikace používala **stejné `connection.client_id`** —
s jiným by své dřívější příkazy nemohla rušit ani měnit. Ukládání lze vypnout
přes `state.enabled: false`; převzetí značkovaných příkazů z TWS po startu
probíhá i tak.

## Upozornění

Aplikace zadává skutečné příkazy do trhu. Vyzkoušejte ji nejprve na papírovém
účtu — u TWS je to zpravidla port 7497 (šablona konfigurace míří na 7496,
`connection.port` proto upravte podle svého nastavení TWS). Pro zkoušení bez
rizika lze v TWS zapnout *Read-Only API* a v konfiguraci nastavit
`connection.readonly: true` — příkazy pak odmítá TWS, aplikace sama
zadávání neblokuje a chybu jen zapíše do průběhu.

Webové rozhraní nemá přihlašování. Ponechte `ui.host: 127.0.0.1`; při
`0.0.0.0` by mohl příkazy zadávat kdokoliv v síti.
