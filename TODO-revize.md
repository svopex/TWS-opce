# Revize větve `Rozsireni-moznosti-zadani-PT-a-SL` – vyřešeno

Všechny nálezy revize (A1–A7, B1–B9, úklid C) jsou zapracované.
Sada testů je zelená: `python -m unittest discover -s . -p "test_*.py"` – 377 testů.
Zadání viz `rozsireni-zadani-sl-pt.md`, chování je popsané v README
(sekce „PT a SL na podkladu, nebo na opci“ a „Stav obchodů a restart“).

## Rozhodnutí o chování, která si vyžádala odpověď

- **B4 – ztráta na opci se stropí prémií.** SL zadaný na opci nemůže odnést víc
  než `(nákupní cena − tik) × 100` USD, protože stop nemůže klesnout pod jeden
  tik. Strop platí pro sloupec *Ztráta na SL* i pro doporučené množství
  (`calc.max_option_loss`, `FlowEngine._capped_option_loss`).
- **B9 – model ze závěrečné ceny varuje.** Počítá-li se implikovaná volatilita
  ze zdroje `close`, náhled i log to výslovně uvedou; dopočet se ale nezakazuje.
- **B3 – `Flow.original_stop_loss` je `float | None`.** Nula je platná hodnota
  (break even u SL na opci), takže „nezadáno“ se pozná přes
  `Flow.original_sl_known`, ne pravdivostí čísla. `original_profit_target`
  zůstal `float` – nulový cíl nedává smysl v žádném režimu.

## Co se změnilo (podle commitů)

| Nález | Řešení |
| --- | --- |
| A3 | Vyplnění prodejních příkazů se účtuje přírůstkově (`_account_part_fills`, `_sync_part_fills`, `main_counted_*` / `runner_counted_*`); `_resize_part` nastavuje zbývající kusy a každému příkazu je zvyšuje o jeho vlastní vyplnění. |
| A2 | `_restore_state` posuzuje zajištění po částech pozice, vyplněný příkaz nebere jako živý a rozdělané uzavírání trhem dokončí; zajišťuje se držené množství z TWS. |
| A4 | `_flow_from_trade` převezme nákupní cenu i množství, čte samostatný `:exitsl` a rozpozná stop na nákupní ceně jako break even. |
| A5 | `_split_exit_for_runner` sestaví příkazy runneru dřív, než zmenší hlavní zajištění, a při selhání vrátí obojí zpět. |
| A6 | `_sl_breached` porovnává BID se stop cenou z `calc.option_loss_stop`; nepoužívané `Flow.pt_option_price` / `sl_option_price` zrušeny. |
| A7 | `_clear_part` zapomíná varování o ztraceném příkazu, takže nová dvojice varuje znovu. |
| B1, B2, B5 | Formulář přijme kteroukoliv úroveň, přepočet nezahodí zadání, break even na opci se do pole SL nepřenáší a náhled se spouští časovačem v kontextu prvku. |
| B6, B8 | Náhradní nákupní cena se doplňuje v `_ensure_fill_price`; `_handle_closing` uvolňuje sloty runneru přes `_clear_part`. |
| B7 | Bloky `if __name__ == "__main__"` jsou na konci testovacích souborů. |
| C | Popis úrovní je jedinou funkcí v `models.py`, kontrola cíle a převody USD ↔ podklad na jednom místě, příprava zadání uvolňuje odběry i při chybě či zrušení, `FakeIBService` překrývá jen `ticker()` a základy testů jsou v `tests/zaklad.py`. |

Nad rámec nálezů: vyplní-li se prodejní příkaz na menší množství, než pozice
drží, aplikace to hlásí jako chybu s počtem nezajištěných kusů a uzavření trhem
takový případ dokončí.
