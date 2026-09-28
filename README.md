# Maturity day on Morpho Midnight

An independent study of how fixed-maturity loans on Morpho Midnight (Base) end: repayment, a roll into Morpho Blue, liquidation, or debt still open. It replays public contract events, checks reconstructed balances against contract state, and produces aggregate counts by maturity date.

This is a **private working repository**. The September 25, 2026 maturity has been indexed and checked. The October 30 maturity and the public writeup are pending. The draft page at `midnight/maturity.html` is not deployed or linked publicly. This work is not affiliated with Coinbase or Morpho.

## Reproduce the September checkpoint

Python 3.12 and `pytest` are needed for the tests; the production scripts use the standard library. From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q tests
.venv/bin/python -m spine.check_maturity 2026-09-25
```

The live check needs access to public Base RPC, DefiLlama, and a current event index. Refresh and rebuild the aggregate with:

```sh
.venv/bin/python -m spine.fetch_midnight
.venv/bin/python -m spine.midnight label 2026-09-25
.venv/bin/python -m spine.check_maturity 2026-09-25
```

The fetcher resumes from its saved block and uses a public RPC sweep to fill gaps left by Blockscout. `data/midnight/` contains generated public chain data; edit the producer, not these files. Raw on-chain integers are stored as JSON strings. To preview the draft locally, run `.venv/bin/python -m http.server 8000` and open `/midnight/maturity.html`.

## What the checks cover

`spine.check_maturity` compares replayed debt and total units with live contract state, checks the aggregate borrowed value against DefiLlama, reads the actual block at maturity plus one hour, and matches rolls with Morpho Blue borrow receipts. See [MIDNIGHT.md](MIDNIGHT.md) for the runbook and publication gate.

Wallet tagging output and snapshots belong under `private/`, which is ignored by Git. The public aggregate has no wallet-type split. A private review and the October 30 maturity are required before publishing the page or memo.

The original Morpho Blue dashboard and backtest remain in [Spine](https://github.com/RahilBhavan/spine). The code here reuses its standard-library API and wallet-tagging helpers under the MIT license.
