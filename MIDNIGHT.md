# Midnight maturity runbook

This repository indexes Morpho Midnight loans on Base and labels the outcome of each maturity. The September 25, 2026 maturity is indexed and verified. The October 30, 2026 maturity is future work. All times below are UTC.

## Commands

| Command | Output | Visibility |
|---|---|---|
| `python -m spine.fetch_midnight` | `data/midnight/<Event>.json` and `rpc_checked.json` | Public chain data |
| `python -m spine.midnight label [YYYY-MM-DD ...]` | `data/midnight/maturity.json` | Aggregate counts only |
| `python -m spine.midnight snapshot [--no-fetch]` | `private/midnight/snapshots/` | Private |
| `python -m spine.midnight tag` | `private/midnight/coinbase_split.json` | Private; never commit |
| `python -m spine.check_maturity [YYYY-MM-DD ...]` | PASS/FAIL checks; no file | Verification |

Prefix each command with `.venv/bin/` when using a virtual environment. `private/` is gitignored. The draft page is at `midnight/maturity.html`; it reads aggregate data from `data/midnight/maturity.json` when served from the repository root. It has `noindex` and is not deployed.

## October 30 maturity

The target maturity is 15:00 UTC. Run the event fetch ahead of time, then take snapshots at 09:00, 15:00, and 16:00 UTC. At 15:00 use `snapshot --no-fetch` so the read lands near the checkpoint. After 16:00:

```sh
.venv/bin/python -m spine.fetch_midnight
.venv/bin/python -m spine.midnight label 2026-10-30
.venv/bin/python -m spine.check_maturity
.venv/bin/python -m spine.midnight tag
```

The event replay does not require snapshots; they provide an independent live cross-check. If the public RPC lacks historical state, record that skipped check and obtain a suitable historical RPC before declaring verification complete.

## Publication gate

1. Check every matured date. Require zero replay and live debt mismatches, zero total-unit mismatches, roll-to-Blue reconciliation, and a DefiLlama priced-token borrowed gap at most 2% at its snapshot time. Inspect any unpriced token exclusions separately.
2. Review the private Coinbase wallet split with Coinbase and Morpho before publishing. If a roll failed or a Coinbase wallet was liquidated at maturity, report it privately first. Sending anything requires the owner's explicit authorization.
3. Review the aggregate for addresses or wallet-type tags. The public output is counts only, with no estimate of any party's exposure or losses.
4. Write a one-page memo on the first two Coinbase maturities, including data sources, methods, exceptions, and limitations. Check every number against the generated aggregate.
5. Only after those steps, publish the final page and memo. The source repository is public; the draft page is not deployed.
