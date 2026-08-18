#!/usr/bin/env bash
# Spuštění aplikace na macOS a Linuxu.
# Skript sám najde interpret, při prvním spuštění založí virtuální prostředí
# a doinstaluje závislosti. Případné přepínače se předávají aplikaci,
# například: ./run.sh --no-connect
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"

# Na macOS bývá k dispozici pouze python3, na některých distribucích jen python
najdi_python() {
  for kandidat in python3 python; do
    if command -v "$kandidat" >/dev/null 2>&1; then
      # Aplikace vyžaduje Python 3.10 nebo novější
      if "$kandidat" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        echo "$kandidat"
        return 0
      fi
    fi
  done
  return 1
}

if [ ! -d "$VENV" ]; then
  PYTHON="$(najdi_python)" || {
    echo "Nenalezen Python 3.10 nebo novější. Nainstalujte jej z https://www.python.org/downloads/" >&2
    exit 1
  }
  echo "Zakládám virtuální prostředí ($("$PYTHON" --version)) ..."
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  echo "Instaluji závislosti ..."
  "$VENV/bin/pip" install --quiet -r requirements.txt
fi

exec "$VENV/bin/python" main.py "$@"
