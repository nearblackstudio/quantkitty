# QuantKitty

AI trading desk for Hyperliquid perpetuals. Paper trading on testnet; no real funds.

The full rationale, evidence and risk rules are in the **QuantKitty Prop Playbook** (Claude Doc).

## How it runs

Every 4 hours a scheduled Claude task downloads this repo (read-only) and runs `bin/cycle.sh`. The live strategy config and research queue are stored in the dashboard's database, not in this repo; `config.json` and `research/queue.json` here are only the starting copies.

| Role | Code | What it does |
| --- | --- | --- |
| Market analyst | `quantkitty/desk.py` (top) | Pulls every Hyperliquid perp, 4h candles for liquid ones, builds the point-in-time universe (top 40 by 30-day volume, at least $3M a day, 90 days listed) |
| Strategist | `quantkitty/model.py` | Runs the live sleeves from `config.json` and combines them by risk parity at a 25% volatility target |
| Risk manager | `quantkitty/desk.py` (middle) | Gross cap 2.5x, 0.25x per coin, daily -6% and weekly -12% entry locks, soft drawdown at 80% of peak halves size, hard stop at 65% flattens and halts |
| Trader | `quantkitty/desk.py` (bottom) | Rebalances a virtual $10,000 paper book at Hyperliquid mainnet prices with fees, slippage and funding; mirrors the largest positions on the testnet account |
| Quant researcher | `quantkitty/research.py` | Tests one hypothesis from `research/queue.json` per cycle against six promotion gates |

A weekly head-of-desk review reads the ledger and reports.

Results are written to the QuantKitty Desk dashboard (a Claude artifact with a small database).

## Rules the agents follow

- No agent edits code in `quantkitty/`. Strategies are combinations of the families in `model.LIBRARY`.
- The research run may add a sleeve to the live config only as `probation` (half risk), and only after passing all gates on two cycles at least 24 hours apart. Probation becomes live after 30 days if the last 90 days are profitable; sleeves losing over 90 days are retired.
- The Claude researcher may append new hypotheses to the queue in the database (existing families, new parameters, with a rationale).
- Scheduled runs never write to this repo.
- Secrets never live in this repo.

## Local use

```bash
pip install -r requirements.txt
python -m quantkitty.desk --out out/                 # dry run: paper book only
HL_ACCOUNT=0x... HL_AGENT_KEY=0x... python -m quantkitty.desk --state s.json --flags f.json --out out/ --execute
python -m quantkitty.research --out out/
```
