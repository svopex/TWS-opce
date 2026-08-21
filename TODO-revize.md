# Předávací poznámky – otevřené nálezy revize větve `Rozsireni-moznosti-zadani-PT-a-SL`

Stav: HEAD `0733ea7`, pushnuto, 345 testů zelených (`python -m unittest discover -s . -p "test_*.py"`).
Kontext: viz `rozsireni-zadani-sl-pt.md` (zadání) a README sekce „PT a SL na podkladu, nebo na opci“.
Nálezy pocházejí z revize 24 agentů; A1 a dřívější body (SL BE po restartu, vážený průměr prodeje,
náhradní nákupní cena, race v CLOSING) jsou už opravené. Pracovat sám, bez dalších agentů.

Klíčové pojmy v `tws_opce/engine.py`: „část“ = `exit` (hlavní) / `runner`; sloty `exit_trade` (PT nebo
společný příkaz) + `exit_sl_trade` (SL při odděleném výstupu), `runner_trade` + `runner_sl_trade`.
Pomocné: `_legs`, `_part_fill_summary`, `_settle_part_fills`, `_resize_part`, `_part_modifiable`,
`_part_all_dead`, `_filled_leg`. Model: `Flow.held_quantity`, `main_quantity`, `open_quantity`,
`main_sold_quantity/main_sold_value`, `exit_split`, `break_even_sl` (= 0.0 u SL na opci).
Testy režimů: `tests/test_rezimy.py` (základ `ZakladRezimu`, helpery `zaloz`, `nakup`, `prodeje`).

## A. Riziko přeprodeje / nefunkčnost – opravit přednostně

### A2 Obnova po restartu s dvojicí příkazů (`_restore_state`, `_handle_filled`)
- `_restore_state`: při `exit_split` vyžaduje 2 živé příkazy (`potrebne = 2`), *Filled* počítá jako živý.
  Prodaná hlavní část (PT Filled, SL zrušen OCA) + běžící runner → větev „bez zajištění“ → zruší se
  zdravé příkazy runneru, stav FILLED, zajištění se založí znovu. Totéž při restartu uprostřed ručního
  uzavírání (MKT ve slotu `pt`, `exit_sl_trade` None → 1 < 2 → MKT se zruší, `main_close_requested`
  se vynuluje; při vyplnění během výpadku `_part_reason` dá „PT“ místo „ručně“).
  Návrh: je-li `exit_fill_price` uložené (hlavní část prodaná), posuzovat jen runner; Filled nebrat
  jako živý; živý MKT bez podmínek uznat jako uzavírání (ponechat CLOSING / `main_close_requested`).
- `_handle_filled`: `filled = int(trade.orderStatus.filled)` → `_place_exit(flow, filled)` bere množství
  z nákupního příkazu (např. 4), ne držené (1). Návrh: zajišťovat `flow.held_quantity - main_sold_quantity`;
  `_restore_state` nastavit `filled_quantity = drzeno + runner_sold_quantity + main_sold_quantity`
  (held_quantity odečítá runner_sold).
- Testy: PT Filled + SL Cancelled + runner běží → po restartu EXIT_ARMED, runner netknut, žádný nový
  příkaz; runner prodán dřív (runner_sold=1, drženo 3), SL ručně zrušen → nové zajištění na 3 ks.

### A3 Částečné vyplnění PT limitu za běhu (`_resize_part`, `held_quantity`, tabulka)
- PT LMT vyplní 1 ze 4 (status Submitted, filled 1); TWS přes OCA typ 2 zmenší STP na 3. Aplikace to
  nevidí, dokud není Filled: `Zrušit runner` / dorovnání nákupu / převzetí kusů po ztraceném runneru
  volají `_resize_part(flow, "exit", held_quantity)` = 4 → PT total 4 (zbývá 3) + SL total 4 → short.
  Tabulka ukazuje 4/4.
  Návrh: `_resize_part` odečíst součet `filled` přes příkazy části: `zbyva = quantity - sum(filled)`;
  každému příkazu `totalQuantity = zbyva + jeho_filled`. Průběžně promítat částečné prodeje do
  `main_sold_quantity` (pozor na dvojí započtení po `_clear_part`) a ukazovat v `open_quantity`.
- Test: 4 ks, runner 1, PT filled 1 (Submitted) → cancel_runner → PT total 4 (tj. zbývá 3), SL total 3.

### A4 Převzetí bez souboru stavu (`_flow_from_trade`)
- `nakup = valid_price(trade.orderStatus.avgFillPrice)` se nikdy nezapíše do `flow.fill_price`
  (ani `filled_quantity`). U úrovní na opci pak `change_profit_target` → `_modify_part_levels` →
  `calc.option_profit_limit(None, …)` = TypeError; `set_stop_loss` odmítne. Návrh: předat
  `fill_price=nakup` do Flow (a `fill_time`), náhradu z `_build_part_orders` přesunout do `_place_exit`.
- `vystup_sl` (`:exitsl`) se čte jen uvnitř `if vystup is not None:` – přežije-li jen SL příkaz,
  obchod se převezme jako „oba na podkladu“ a `_modify_part_levels` by STP příkazu přepsal conditions.
- SL BE na opci: `ztrata == 0` neprojde `if ztrata > 0` → `sl_on=True`, SL z poměru. Povolit 0.
- Testy: ztracený state.json + LMT/STP → change_profit_target funguje; jen `:exitsl` STP přežil →
  `sl_on_underlying=False`, `exit_split=True`; STP na nákupní ceně → `stop_loss == 0.0`.

### A5 `_split_exit_for_runner` není atomické
- `_resize_part("exit", …)` proběhne dřív než `_place_part("runner", …)`; selže-li sestavení
  příkazů runneru (ValueError bez ceny opce, výjimka TWS), hlavní zajištění zůstane zmenšené.
  Návrh: nejdřív `_build_part_orders(flow, "runner", q)`, pak resize, pak place.

### A6 `_sl_breached` pro SL na opci
- Porovnává BID s `Flow.sl_option_price` (nezaokrouhlené `fill - sl/100`, může být záporné), zatímco
  STP stojí na `calc.option_loss_stop` (zaokrouhleno, min. 1 tik). Při SL > prémie se proražení nikdy
  nevyhodnotí. Návrh: `stop = calc.option_loss_stop(flow.fill_price, sl, flow.min_tick)`; smazat
  nepoužívané `Flow.pt_option_price` a `sl_option_price`.

### A7 `_lost_leg_warned` se nikdy nemaže
- Klíč `f"{flow.id}:{part}"`; po novém zajištění (nová dvojice) druhá ztráta příkazu už nevaruje.
  Návrh: `self._lost_leg_warned.discard(klic)` v `_clear_part`.

## B. Bez rizika přeprodeje

- B1 `ui._submit` vyžaduje prvotní pole (PT/SL podle zaškrtávátka), engine přijme kteroukoli úroveň;
  `Přepočítat` s vyplněným PT a prvotním SL vynuluje PT → `prepare` nic nedopočte. Návrh: vyžadovat
  aspoň jednu úroveň; při přepočtu nulovat dopočítávanou jen, je-li prvotní vyplněná.
- B2 Po `SL BE` na opci UI zapíše do pole SL 0,00 (`_on_set_sl`, `_fill_from_flow`); `_validate`
  odmítá `sl <= 0`, `Přepočítat` dá min. množství. Návrh: u SL na opci povolit 0 ve `_validate`
  (`suggest_quantity_for_loss` pak vrací min – ošetřit), nebo do formuláře zapisovat prázdné pole.
- B3 `break_even_sl = 0.0` je falsy: `_resolve_sl("puvodni")`, `_restore_flow`, `store.dict_to_flow`
  používají `if not flow.original_stop_loss` → může se nastavit původní SL = 0. Testovat `is None`
  (pole je float s výchozí 0.0 – zvážit `float | None`).
- B4 (na rozhodnutí) `_compute_expected_pnl` a `suggest_quantity_for_loss` berou SL na opci v plné výši,
  i když max. ztráta je prémie (`fill_price × 100`). Zastropovat, je-li nákupní cena známá.
- B5 `background_tasks.create(self._load_preview("auto"))` v `_on_mode_change` / `_on_primary_change`
  běží bez slot kontextu → `ui.notify` uvnitř může vyhodit „current slot cannot be determined“.
  Návrh: `ui.timer(0, callback, once=True)` vytvořený v kontextu prvku, nebo `with self.radek_prvotni:`.
- B6 `_build_part_orders` jako vedlejší účinek zapisuje `flow.fill_price` (náhradní cena). Přesunout
  do `_place_exit` (jednou) a u převzetí použít skutečnou cenu (viz A4).
- B7 Nové testy v `tests/test_calc.py` (`TestUrovneNaOpci`) a `tests/test_ib_service.py`
  (`TestCenaOpceProModel`, `TestCekaniNaKotace`) jsou za blokem `if __name__ == "__main__"` –
  přesunout blok na konec souboru.
- B8 `_handle_closing` po vyplnění tržního prodeje čistí jen `runner_trade`/`runner_order_id`;
  použít `_clear_part(flow, "runner")`.
- B9 (na rozhodnutí) IV ze staré `close` ceny při gapu podkladu (pre-market) může vyjít nesmyslně;
  zdroj je vidět v náhledu („z ceny opce (close)“). Případně při zdroji `close` varovat.

## C. Úklid kódu (volitelné, bez vlivu na chování)
- `FlowEngine.level_text` a `ui.uroven_text` (+ formátování v `_apply_preview`) – sloučit do jedné
  metody na `Flow`.
- Převod USD ↔ úroveň podkladu je čtyřikrát (`_level_from_option_profit`,
  `_profit_from_underlying_level`, inline v `_default_stop_loss`, `vysledek_na_kontrakt`
  v `_compute_expected_pnl`); `_default_stop_loss` přepsat přes první dvě. Fallback delty (3 kopie)
  vyčlenit do `_model_delta`.
- Kontrola „PT na správné straně / na opci kladné“ je třikrát (`_validate`, `change_profit_target`,
  `set_runner`).
- `popisek_urovne` má nepoužitý parametr `dopocitana`; `_set_modes`/`_set_primary` volají
  `_refresh_mode_labels` (a `move()`) opakovaně – zkontrolovat zámek jako první řádek obsluh.
- `prepare()` kvalifikuje tentýž kontrakt dvakrát, je-li referenční = vybraná opce; odklad
  `quotes_grace_sec` se počítá při každém volání znovu; rozběhnutý `prepare()` se při novém
  nezruší (držet `self._preview_task` a `cancel()`).
- `FakeIBService` překrývá `option_price` a `wait_for_quotes`; lepší překrýt jen `ticker()` a vracet
  `Ticker` s bid/ask/last/close, aby se testovala ostrá implementace.
- Základy testů (`vychozi_config`, `ZakladRezimu`, `TestObnovaDvojice.setUp`) duplikují
  `test_engine.ZakladTestu` a `test_obnova.ZakladObnovy`.
- Odběr referenční opce v `prepare()` se uvolňuje jen v happy path a při ValueError – lépe vlastnit
  v `Preview` a uvolňovat v `_replace_preview` / `release_preview`.

## Doporučený postup
1. A2–A7 (jeden commit s testem pro každý scénář přeprodeje), spustit celou sadu.
2. B1–B3, B5–B8.
3. C podle chuti.
Commit message česky, přítomný čas, tečka na konci, bez Co-Authored-By; pak push na
`origin Rozsireni-moznosti-zadani-PT-a-SL`.

## Poznamky, na co prisel dalsi agent
Co ale riziko nese nezávisle na modelu
- A2 a A3 se protínají – obnova po restartu i _resize_part sahají na stejné veličiny (main_sold_quantity, filled_quantity, held_quantity). Dělal bych A3 první (zavedení „průběžně promítat částečné prodeje"), pak A2 nad už opraveným modelem, jinak si to model bude dvakrát přepisovat.
- Dvojí započtení po _clear_part zmiňované v A3 je klasické místo, kde agent udělá tiše chybu, kterou testy nechytí. Stojí za to na to napsat test explicitně, ne se spolehnout na „345 zelených".
- Body B4, B9 a float | None v B3 jsou rozhodnutí o chování, ne implementace – ta si rozhodni sám dopředu a dej je do zadání, ať to model neřeší za tebe. At se mne zepta a da mi moznosti.
