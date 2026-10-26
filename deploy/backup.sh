#!/usr/bin/env bash
# Consistent online backup of the trading database; keeps 30 days.
set -euo pipefail
cd "$(dirname "$0")/.."
DB="${NQT_DB:-var/nqtrader.db}"
mkdir -p var/backups
python3 - "$DB" "var/backups/nqtrader-$(date +%Y%m%d-%H%M).db" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
print("backup written:", dst)
PY
find var/backups -name 'nqtrader-*.db' -mtime +30 -delete
