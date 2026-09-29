#!/usr/bin/env bash
# One QuantKitty cycle: desk (analyst, risk, trader) then research.
# Expects /tmp/qk/system/{state,flags,research_state}.json fetched from the ledger (missing files are fine).
# Writes ledger documents to /tmp/qk/out/ledger/.
set -uo pipefail
cd "$(dirname "$0")/.."
pip install --break-system-packages -q -r requirements.txt >/dev/null 2>&1
mkdir -p /tmp/qk/out
S=/tmp/qk/system
python3 -m quantkitty.desk --state "$S/state.json" --flags "$S/flags.json" --out /tmp/qk/out --execute 2>/tmp/qk/desk.err
echo "desk exit $?"
python3 -W ignore -m quantkitty.research --state "$S/research_state.json" --out /tmp/qk/out 2>/tmp/qk/research.err
echo "research exit $?"
git status --porcelain config.json research/queue.json
