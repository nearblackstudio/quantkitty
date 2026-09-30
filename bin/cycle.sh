#!/usr/bin/env bash
# One QuantKitty cycle: desk (analyst, risk, trader) then research.
# Reads ledger documents fetched into /tmp/qk/system/ (state, flags, research_state, config, queue);
# missing files are fine (config and queue fall back to the copies in this repo).
# Writes ledger documents to /tmp/qk/out/ledger/. Never writes to the repo.
set -uo pipefail
cd "$(dirname "$0")/.."
pip install --break-system-packages -q -r requirements.txt >/dev/null 2>&1
mkdir -p /tmp/qk/out/ledger /tmp/qk/cfg
S=/tmp/qk/system
python3 - <<'PY'
import json, os, shutil
S, C = "/tmp/qk/system", "/tmp/qk/cfg"
for name, key, fallback in [("config", "config", "config.json"), ("queue", "queue", "research/queue.json")]:
    src = f"{S}/{name}.json"
    try:
        json.dump(json.load(open(src))[key], open(f"{C}/{name}.json", "w"), indent=2)
        print(f"{name}: from ledger")
    except Exception:
        shutil.copy(fallback, f"{C}/{name}.json"); print(f"{name}: from repo")
PY
python3 -m quantkitty.desk --state "$S/state.json" --flags "$S/flags.json" --config /tmp/qk/cfg/config.json \
  --out /tmp/qk/out --execute 2>/tmp/qk/desk.err
echo "desk exit $?"
python3 -W ignore -m quantkitty.research --state "$S/research_state.json" --config /tmp/qk/cfg/config.json \
  --queue /tmp/qk/cfg/queue.json --out /tmp/qk/out 2>/tmp/qk/research.err
echo "research exit $?"
ls /tmp/qk/out/ledger
