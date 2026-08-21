# Rozsireni moznosti zadani PT a SL

Chtel bych dalsi moznosti co se tyce zadavani PT a SL.

PT by slo zadat i ziskem primo na opci, treba by se zadalo 10, tak by PT byl zisk na jedne opci 10 dolaru.
PT by v tomto pripade bylo realizovano prikazem typu limit primo na cenu opce.
V uzivatelskem rozhrani bych pred `PT na podkladu` dal checkbox, ze se pouzije podklad.
Defaultne by byl zaskrtnuty, dal bych to do nasteveni.

SL by slo zadat i ziskem primo na opci, treba by se zadalo 10, tak by SL byla ztrata na jedne opci 10 dolaru.
SL by v tomto pripade bylo realizovano stopmarketem primo na cenu opce.
V uzivatelskem rozhrani bych pred `ST na podkladu (nepovinne)` dal checkbox, ze se pouzije podklad.
Defaultne by byl zaskrtnuty, dal bych to do nasteveni.
Co se tyce automatickeho vyplneni SL v uzivatelskem rozhrani, tak si se dopocetlo podle zvoleneho RRR z nastaveni opet podle PT.

Nasledujici tabulka ukazuje, jak by program resil prikazy ve fazy, kdy je jiz nakoupeno:

PT poklad, SL podklad - jako ted, jeden prodejni market prikaz s conditions, zustava beze zmeny
PT poklad, SL ztrata na opci - dva prikazy, PT prodejni market prikaz s conditions a druhy stopmarket na SL primo na cenu opce
PT ztrata na opci, SL podklad - dva prikazy, PT prodejni limit prikaz na cenu opce a druhy prodejni market prikaz s conditions na SL
PT ztrata na opci, SL ztrata na opci - dva prikazy, PT prodejni limit prikaz na cenu opce a druhy stopmarket na SL primo na cenu opce

Pokud jsou zadany dva prodejni prikazy, program zadane prikazy monitoruje a v pripade vyplneni jednoho prikazu druhy ihned v trhu rusi,  
aby nedoslo k dvojimu prodeji opce.

Pokud se tedy zada SL podklad a PT podklad, zustava to jako je to ted a nic se nemeni.

Pokud neco nebudes vedet, nebo je v zadani neco nelogickeho, ptej se prosim.
