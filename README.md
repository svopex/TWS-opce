# Obchodování opcí přes TWS API

Formulářová aplikace pro obchodování opcí přes Interactive Brokers TWS API.
Zadaný obchod čeká na dosažení cenové úrovně podkladu, nakoupí opci
a po nákupu zajistí pozici prodejním příkazem pro PT i SL; část pozice může
běžet jako runner s vlastním, vzdálenějším cílem.

Běží na Windows, macOS i Linuxu — Python + `ib_async` + webové rozhraní NiceGUI.

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
| `-c CESTA`, `--config CESTA` | jiný konfigurační soubor (výchozí `config.yaml`) |
| `--no-connect` | nepřipojovat se k TWS při startu, spojení se naváže tlačítkem |
| `--verbose` | podrobné logování včetně komunikace `ib_async` |

### Nastavení TWS

V TWS (nebo IB Gateway) je nutné povolit API:
*Global Configuration → API → Settings → Enable ActiveX and Socket Clients*.
Číslo v poli **Socket port** musí souhlasit s `connection.port` v `config.yaml`.

Při prvním spuštění vznikne `config.yaml` jako kopie komentované šablony
`config.example.yaml`, kde je popsána každá volba.

## Jak aplikace pracuje

1. **Zadání** — vyplní se ticker, cena podkladu pro nákup, PT a volitelně SL.
   Aplikace načte cenu podkladu a sama určí zbytek:
   - **PUT/CALL** podle toho, zda vstupní cena leží nad nebo pod aktuální cenou
     (vstup nad trhem = průraz nahoru = CALL, vstup pod trhem = PUT),
   - **strike** jako nejbližší dostupný k ceně PT,
   - **expiraci** podle konfigurace (výchozí je nejbližší),
   - **SL**, pokud nebyl zadán, v poměru k PT z konfigurace (výchozí 1:1),
   - **množství** z velikosti účtu, povoleného rizika a delty opce:
     `riskovaná částka / (|vstup − SL| × |delta| × 100)`. Riskovaná částka
     vychází z velikosti účtu — buď z pevné hodnoty v konfiguraci, nebo
     ze skutečného stavu účtu, je-li `account.size: 0`.

   TWS model greeks u opcí neposílá spolehlivě — závisí to na účtu
   a předplatném dat. Chybí-li delta, aplikace ji dopočítá z tržní ceny opce
   (implikovaná volatilita a z ní delta podle Black-Scholes) a ve formuláři
   ji označí jako dopočítanou. Teprve když nelze ani to, sáhne po náhradní
   hodnotě z konfigurace.
   Formulář má k tomu dvě tlačítka:
   **Načíst** obnoví údaje z TWS (cena podkladu, typ opce, expirace, strike,
   kotace, delta) a vyplněná pole nechá být — doplní jen ta prázdná.
   **Přepočítat** navíc přepíše SL i množství vypočtenými hodnotami; zadaný SL
   se přitom zahodí a spočítá znovu podle poměru z konfigurace. Ručně zadané
   hodnoty tedy zmizí pouze na výslovné kliknutí, ne samovolně při psaní.
   V náhledu je vždy vidět, co by výpočet doporučil. Dokud načítání dat
   z TWS běží, ukazuje formulář pulzující text „Načítám data z TWS…".

   Běží-li na zadaném tickeru obchod, **Načíst** naplní formulář jeho
   parametry — i přes ručně zadané hodnoty. Přechod na ticker bez obchodu
   pole naopak vyprázdní, aby se do nového zadání nepřenesly ceny toho
   předchozího; limit spreadu se vrátí na hodnotu z konfigurace. Samotné
   opuštění pole hodnoty nikdy nepřepisuje, mění je jen změna tickeru.

2. **Nákup** — příkaz se do trhu zadá jen tehdy, pokud cena podkladu vstupní
   úroveň ještě nepřekonala: u CALL musí být pod vstupem, u PUT nad ním.
   Jinak obchod ujel a aplikace jej ukončí ve stavu *Vstup propásnut*, aniž by
   cokoliv zadala — platí to i pro opětovné zadání po zablokování spreadem.

   Do TWS se zadá příkaz na opci s cenovou podmínkou na podkladu.
   Dokud se nevyplní, aplikace průběžně upravuje jeho limitní cenu podle
   aktuálního ASK (resp. MID) a hlídá spread.
3. **Spread** — překročí-li nastavené procento, nevyplněný příkaz se odstraní
   z trhu; jakmile se spread vrátí do limitu, příkaz se zadá znovu. Aby se
   příkaz při kolísání kolem limitu nezadával a nerušil stále dokola, musí
   spread klesnout s rezervou pod limit a od odstranění musí uplynout
   nastavená prodleva (`rearm_spread_margin_pct`, `rearm_delay_sec`).
4. **Zajištění** — po nákupu se zadá prodejní příkaz se dvěma cenovými
   podmínkami na podklad spojenými logickým OR: dosažení PT nebo SL.
   S aktivním runnerem vzniknou příkazy dva — hlavní část a runner, každý
   s vlastním cílem a společným SL.
   Vyplnil-li se nákup jen částečně, aplikace nejprve zruší jeho nevyplněný
   zbytek a zajistí skutečně nakoupené množství — TWS totiž nepovolí mít
   na jednom opčním kontraktu současně nákupní i prodejní příkaz.
5. **Monitoring** — tabulka ukazuje všechny obchody, jejich ceny a stav.
   Sloupec *Ks* ukazuje zadané množství a za lomítkem počet kontraktů právě
   otevřených v trhu: před nákupem `4/0`, po částečném vyplnění tří ze čtyř
   `4/3`, po prodeji runneru `4/2` a po uzavření celé pozice opět `4/0`.
   Pod každým rozpracovaným obchodem je řada tlačítek **1× 1,5× 2× 2,5× 3×**;
   posunou cíl na násobek jeho původní vzdálenosti od
   vstupu — u vstupu 232 a cíle 235 (tedy 3 body) znamená 2× cíl 238. Počítá
   se vždy z původního zadání, takže opakované klikání násobky neřetězí,
   a tlačítko odpovídající aktuálnímu cíli je barevně zvýrazněné. U nakoupené
   pozice se rovnou upraví podmínka zajišťovacího příkazu; u obchodu před
   nákupem záleží na `trading.pt_change_strike` — buď zůstane původní strike,
   nebo se podle nového cíle vybere jiný a příkaz se přezadá. Ve formuláři se
   zadává vždy základní cíl 1:1.

   U nakoupené pozice jsou před tlačítky cíle ještě tlačítka **Počáteční SL**
   a **SL BE** — první vrací stop na hodnotu ze zadání, druhé jej posouvá
   na vstupní cenu (break even). Aktivní volba je zvýrazněná stejně jako
   násobek cíle. Před nákupem se tlačítka nenabízejí — SL tam řídí zadání
   ve formuláři. Je-li cena podkladu ve chvíli přepnutí už na zvolené úrovni
   SL, nebo za ní, nemá smysl čekat na podmínku: aplikace podmíněný příkaz
   rovnou zruší a příslušnou část pozice prodá trhem.

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

   U nakoupené pozice je na konci sekce Cíl tlačítko **Uzavřít pozici** —
   zruší zajišťovací příkaz a prodá hlavní část trhem (bez runneru celou
   pozici); případný runner běží dál se svým cílem. Obdobně **Uzavřít
   runner** na konci sekce Runner prodá trhem jen runner a hlavní část
   nechá být. Tržní prodej se v obou případech zadává až po potvrzení
   zrušení podmíněného příkazu, aby se neprodalo víc kusů, než pozice
   drží. Prodej všeho najednou zůstává v dialogu tlačítka *Zrušit*.

   Tlačítka se zobrazují jen tehdy, když má jejich akce smysl, a mizí
   s částí pozice, které se týkají: po prodeji hlavní části zmizí sekce
   Cíl (její cíl už není co řídit — spolu s ní zmizí i *Zrušit runner*,
   protože sloučení už není kam provést), po prodeji runneru jeho sekce,
   a během uzavírání trhem obojí, aby do rozjetého prodeje nešlo zasahovat.
   Stejná pravidla vynucuje i aplikace sama, takže se změna cíle nemůže
   omylem zapsat do tržního příkazu.

   Sloupce *Zisk na PT* a *Ztráta na SL* říkají, jak otevřená část pozice
   dopadne, když podklad dosáhne cílové, resp. stop úrovně (runner se
   oceňuje na svém vlastním cíli a SL). Opce se přecení z implikované
   volatility odvozené z její aktuální ceny. Po nákupu se počítá ze skutečně
   dosažené ceny, před nákupem z ceny, na kterou opce vyjde **až podklad
   dosáhne vstupní úrovně** — tam se totiž bude kupovat.
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

Aplikace zvládá více obchodů současně; na jednom tickeru může běžet
zároveň jeden long (CALL) a jeden short (PUT) obchod. Směr zadání určuje
poloha PT vůči vstupu a nové zadání nahrazuje jen čekající obchod
stejného směru.

### Mimo obchodní hodiny

Před otevřením amerického trhu (15:30–22:00 SEČ / SELČ) TWS u opcí neposílá
BID ani ASK. Bez nich nelze určit limitní cenu, proto obchod zůstane ve stavu
**Čeká na kotace opce** a příkaz se do trhu zadá automaticky, jakmile kotace
dorazí. Aplikace v takové situaci záměrně nezadává tržní příkaz, který by se
vyplnil za neznámou cenu. Při nastavení `entry_order_type: MKT` se příkaz
zadá i bez kotací.

### Automatické uzavření před koncem obchodování

Patnáct minut před zavřením burzy (volitelné přes
`trading.auto_close_minutes_before`) aplikace sama ukončí všechny běžící
obchody: čekající obchody zruší a odstraní jejich nákupní příkazy z trhu,
otevřené pozice prodá tržním příkazem. Do hlavičky stránky se přes den
promítá odpočet do začátku uzavírání.

Čas se počítá v časové zóně burzy (`trading.exchange_timezone`, výchozí
`America/New_York`), takže posuny letního a zimního času vůči místnímu času
počítače nehrají roli. Čas zavření burzy určuje `trading.exchange_close_time`
(výchozí 16:00 newyorského času); zkrácené obchodní dny před svátky aplikace
nezná. Funkci lze vypnout pomocí `trading.auto_close_enabled: false`.

### Stavy obchodu

| Stav | Význam |
| --- | --- |
| Před nákupem | příkaz je v trhu a čeká na cenovou podmínku |
| Blokováno spreadem | spread je nad limitem, příkaz není v trhu |
| Čeká na kotace opce | z TWS nedorazily BID/ASK, limitní příkaz zatím nelze zadat |
| Nakoupeno | opce koupena, zadává se prodejní příkaz |
| Nakoupeno – výstup aktivní | pozice je zajištěna příkazem pro PT i SL |
| Uzavírá se | pozice se na pokyn obchodníka uzavírá tržním příkazem |
| Uzavřeno | pozice uzavřena na PT nebo SL |
| Vstup propásnut | cena překonala vstupní úroveň, příkaz se nezadal |
| Zrušeno | obchod ukončen uživatelem |
| Chyba | zásah zvenčí, například ruční zrušení příkazu v TWS |

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

Aplikace průběžně kontroluje opční pozice na účtu a na ty, ke kterým nemá
obchod ani zajišťovací příkaz, upozorní červeným pruhem v záhlaví a hláškou
v průběhu. Běží-li přitom obchod na stejném tickeru, upozornění výslovně uvede,
že se týká **jiného kontraktu** — jinak snadno vznikne dojem, že je pozice
pod dozorem, přestože obchod míří na jiný strike nebo expiraci. Sama k nim nic nezadává — nezná jejich PT ani SL. Interval kontroly
je `engine.unmanaged_check_sec` (výchozí 30 s, `0` kontrolu vypne).

## Velikost účtu

Riskovaná částka se počítá z velikosti účtu, kterou lze zadat dvěma způsoby:

| `account.size` | Chování |
| --- | --- |
| kladná hodnota (např. `5000.0`) | použije se přesně tato částka |
| `0` | velikost se převezme z TWS (NetLiquidation) a průběžně obnovuje |

Při `0` odpovídá riziko skutečnému stavu účtu včetně otevřených pozic; hodnota
se načítá po připojení a dál se obnovuje v intervalu `engine.account_refresh_sec`
(výchozí 60 s). Dokud ji TWS nepošle, aplikace na to upozorní ve formuláři
a množství nedoporučí. V panelu *Konfigurace* je vždy vidět, odkud hodnota
pochází — `(config)`, nebo `(z TWS)`.

## Konfigurace

Vše podstatné je v `config.yaml` (podrobné komentáře u každé položky):
spojení s TWS, velikost účtu a risk, typ nákupního příkazu
(`LMT_ASK` / `MKT` / `LMT_MID`), typ prodejního příkazu, limit spreadu,
poměr SL:PT a výběr expirace.

## Testy

```bash
python -m unittest discover -s . -p "test_*.py"
```

Testy běží proti náhradě TWS (`tests/fake_ib.py`) — pokrývají výpočty,
čtení tržních dat i celý průběh obchodu včetně příkazů, jejich podmínek,
runneru a obnovy po restartu. Spojení s TWS není potřeba.

## Struktura

```
run.sh, run.bat          spuštění na macOS/Linuxu, resp. Windows
main.py                  spuštění aplikace
config.example.yaml      komentovaná šablona konfigurace
tws_opce/
  config.py              načtení a validace konfigurace
  calc.py                výpočty (typ opce, SL, spread, množství, limity)
  models.py              model obchodu a jeho stavy
  ib_service.py          obálka nad ib_async (kontrakty, data, příkazy)
  engine.py              řízení obchodů a monitorovací smyčka
  store.py               ukládání stavu obchodů na disk
  ui.py                  webové rozhraní
  static/styles.css      styly
tests/                   testy
```

## Stav obchodů a restart

Stav obchodů se průběžně zapisuje do `state.json`, takže restart ani pád
aplikace o rozpracované obchody nepřipraví. Po startu se uložený stav **vždy
srovná se skutečností v TWS** — rozhoduje to, co je v TWS, nikoliv zápis
v souboru:

| Co aplikace po startu najde | Jak zareaguje |
| --- | --- |
| pozice a k ní prodejní příkaz | pokračuje v hlídání |
| pozice bez prodejního příkazu | zajištění doplní |
| pozice uzavřená během výpadku | označí obchod za uzavřený |
| nákupní příkaz čekající v trhu | naváže na něj |
| nákupní příkaz, který v TWS není | zadá jej znovu |
| nakoupený obchod bez pozice i příkazu | označí jako chybu k ruční kontrole |

Aby aplikace své příkazy poznala, značkuje je v poli `orderRef` zápisem
`TWSOPCE:<obchod>:entry`, `:exit`, resp. `:runner`. Cizích příkazů na účtu
si nevšímá,
takže vedle ní můžete obchodovat i ručně.

Ztratí-li se soubor se stavem, aplikace podle těchto značek dohledá alespoň
**čekající příkazy** a obchody z nich sestaví — vstupní cenu z cenové podmínky
příkazu, PT a SL z podmínek zajišťovacího příkazu. Chybí-li zajišťovací příkaz,
odvodí PT ze strike a SL z poměru v konfiguraci a obchod označí za dopočítaný.

Co takto zachránit nelze, je **už nakoupená pozice bez zajišťovacího příkazu**:
vyplněné příkazy TWS vrací bez `orderRef`, takže je k obchodu přiřadit nejde.
Aplikace na každou opční pozici, ke které nemá obchod, upozorní hláškou
„POZOR" v průběhu a nechá ji na vás — sama k ní nic nezadává, protože nezná
původní PT ani SL.

Totéž proběhne po **každém obnovení spojení** — po ručním odpojení a připojení
tlačítkem i po výpadku sítě. Objekty příkazů z minulého spojení už nejsou platné,
takže se obchody pokaždé znovu spárují s tím, co je skutečně v TWS.

Podmínkou je, aby aplikace používala **stejné `client_id`** — jinak jí TWS
vlastní příkazy nevydá. Ukládání lze vypnout přes `state.enabled: false`.

## Upozornění

Aplikace zadává skutečné příkazy do trhu. Vyzkoušejte ji nejprve na papírovém
účtu (port 7497), případně s `connection.readonly: true`, kdy aplikace příkazy
nezadává.
