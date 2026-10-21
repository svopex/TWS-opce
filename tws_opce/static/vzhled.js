// Hlásí serveru systémové nastavení světlého/tmavého vzhledu prohlížeče.
// V automatickém režimu přebarvuje stránku Quasar sám, grafy přehledu
// výsledků ale kreslí canvas barvami ze serveru - ten proto musí vědět,
// jaký vzhled systém právě chce (viz tws_opce/vzhled.py).
(() => {
  const dotaz = window.matchMedia("(prefers-color-scheme: dark)");
  const nahlas = () => emitEvent("systemovy-vzhled", dotaz.matches);

  // Skript běží dřív než nicegui.js a připojení aplikace, emitEvent tedy
  // jde volat až po načtení stránky. Na spojení se serverem počká NiceGUI
  document.addEventListener("DOMContentLoaded", () => {
    nahlas();
    // Změna v systému (např. večerní přepnutí na tmavý) se hlásí hned
    dotaz.addEventListener("change", nahlas);
  });
})();
