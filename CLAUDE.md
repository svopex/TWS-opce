# CLAUDE.md

## Popis funkcionality

Aplikace je napojena na Interactive Brokers TWS API.
Slouzi k obchodovani opci.
Jedna se o formularovou aplikaci, na pozadi vola Interactive Brokers TWS API.

Po spusteni se zobrazi formular, kde se zadava:

* Nazev tickeru - napriklad `AAPL`
* Mnozstvi opci - spocitano a prevyplneno podle velikosti uctu, mozne ztraty podle SL a defaultniho risku na velikost uctu, podle delty opce
* Cena podkladoveho aktiva, napriklad `APPL`, kdy dojde k nakupu opce
* Automaticky se urci, zda se jedna o PUT/CALL opci podle toho, zda aktualni cena je pod nebo nad zadanou cenou podkladoveho aktiva
* `PT` - cena podkladoveho aktiva, kde se ma prodat opce v zisku. Toto bude zaroven strike, ktery se pouzije pro nakup (nejblizsi mozny strike k teto cene)
* `SL` - cena podkladoveho aktiva, kde se ma prodat opce ve ztrate. Nemusi se zadavat, pak to bude vuci PT 1:1. Pokud se zada, pouzije se.
* Maximalni hodnota spreadu, v procentech BID-ASK (naprklad Bid = 7.75, Ask = 8.85, 8.85 - 7.75 = 0.1 / (8.85 - (8.85 - 7.75) / 2)) = 1.14%. 
  Pokud spread bude vyssi, nakupni prikaz se nenastavi, respektive odstrani, pokud jeste nebyl provedeny.
* Tlacitko, ktere potvrdi zadani opce do trhu a spusteni flow
* Tlacitko, ktere zrusi zadani opce do trhu a zruseni flow (podle tickeru)

K aplikaci bude konfiguracni soubor, kde bude:

* Velikost uctu - defaultne 5000 USD
* Defaultni risk na velikost uctu, defaultne nastavit 1%
* Defaultni pomer `SL` vuci `PT`, pokud se hodnota nezada nastaven 1:1
* Defaultni maximalni hodnota spreadu = 5%

Aplikace po potvrzeni zada nakupni prikaz do trhu.
Monitoruje spread, pokud je priliz vysoky a jeste nedoslo k nakupu, prikaz se zrusi.
Po nakupu aktivuje prodejni prikaz pro SL a PT.
Vyuziva v TWS moznosti u opci `Conditions` pro prodej a nakup podle hodnoty podkladoveho aktiva.

Aplikace zobrazuje i monitoring ve forme tabulky, kde je videt aktualni stav jednotlivych nakupu.
Nakupu aplikace podporuje vice. Mezi jednotlivymi nakupy/tickery se prepina na ne klikem v monitorovaci tabulce.
V monitorovaci tabulce je ticker, cena kde se nakoupilo, cena PT a SL, v jakem stavu je flow (pred nakupem, nakoupeno).

Aplikace musi bezet na Windows, MAC OS i Linux. Technologie Python + ib_async + web UI (NiceGUI).

Expirace opce bude zadana v configu. Defaultne vzdy ta nejblizsi.

Jakým typem příkazu nakupovat opci po splnění cenové podmínky na podkladu? Bude nastaveni v configu, vsechny tri moznosti:

* LMT na ASK + tolerance z configu, defaultni nastaveni config
* Market
* LMT za MID

Prodejni prikaz pro PT a SL bude jeden. Bude zadan az po nakupu. V `Conditions` budou prodejni podminky na podkladove aktivum.

Spojeni 127.0.0.1:7497 defaultni, lze nastavit v configu.
