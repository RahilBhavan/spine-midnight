"""Evidence for which Midnight markets carry Coinbase's fixed-rate loan flow (feeds cohort.SCOPE).

  .venv/bin/python -m spine.scope YYYY-MM-DD [--out DIR]

For every USDC-loan market with cbBTC collateral maturing that day, read each Take transaction (tx `to`, and the
ERC-4337 UserOperationEvent paymaster when the tx goes through the EntryPoint) and tag the market's borrowers as Coinbase
Smart Wallet contracts or not. Also counts BlueFallbackRolling configs and rolls into Morpho Blue markets. Output is per
market: counts, shares and example tx hashes. No borrower addresses. Default DIR: private/midnight/scope."""
import json, os, sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from spine.api import MARKETS, rpc
from spine.midnight import USDC, date, events, label, private_dir
from spine.tag_coinbase import tag_chunk

CBBTC = '0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf'
ENTRYPOINT_V06 = '0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789'
USER_OP_EVENT = '0x49628fd1471006c1482da88028e9ce4dbb080b815c9b0344d39e5a8e6ec1419f'
CDP_PAYMASTER = '0x2faeb0760d4230ef2ac21496bb4f0b47d634fd4c'  # Coinbase Developer Platform ERC-20 paymaster (docs.base.org)
BUNDLER3 = '0x6bfd8137e702540e7a42b74178a4a49ba43920c4'  # Morpho Bundler3 on Base
BLUE_NAMES = {m['id']: 'Blue %s/USDC (Coinbase variable-rate market)' % k for k, m in MARKETS.items()}


def tx_route(h):
    tx = rpc('eth_getTransactionByHash', [h])
    rc = rpc('eth_getTransactionReceipt', [h])
    pms = {'0x' + l['topics'][3][-40:] for l in rc['logs'] if l['topics'] and l['topics'][0] == USER_OP_EVENT}
    return tx['to'].lower(), sorted(pms)


def evidence(day):
    ev = events()
    rows = [r for r in label(ev) if r['maturity_date'] == day]
    mk = {m['id']: m for m in ev['MarketCreated']}
    cands = [i for i in sorted({r['market'] for r in rows})
             if mk[i]['loan_token'] == USDC and any(c['token'] == CBBTC for c in mk[i]['collaterals'])]
    out = []
    for i in cands:
        m = mk[i]
        txs = sorted({t['tx'] for t in ev['Take'] if t['id'] == i})
        with ThreadPoolExecutor(4) as pool:
            routes = dict(zip(txs, pool.map(tx_route, txs)))
        to = Counter(r[0] for r in routes.values())
        pm = Counter(p for r in routes.values() for p in r[1])
        users = sorted({r['user'] for r in rows if r['market'] == i})
        tags = tag_chunk(users)
        sc = [x for x in ev['SetConfig'] if x['midnight_id'] == i]
        ro = [x for x in ev['Roll'] if x['midnight_id'] == i]
        out.append(dict(
            market=i, maturity_utc=date(m['maturity']), loan='USDC',
            collateral=[dict(token=c['token'], lltv=int(c['lltv']) / 1e18, oracle=c['oracle']) for c in m['collaterals']],
            borrowers=len(users), smart_wallet_borrowers=sum(1 for v in tags.values() if v),
            take_txs=len(txs), tx_to=dict(to.most_common()), entrypoint_share=round(to[ENTRYPOINT_V06] / max(len(txs), 1), 3),
            bundler3_share=round(to[BUNDLER3] / max(len(txs), 1), 3), paymasters=dict(pm.most_common()),
            cdp_paymaster_txs=sum(1 for r in routes.values() if CDP_PAYMASTER in r[1]),
            example_cdp_paymaster_txs=[h for h, r in routes.items() if CDP_PAYMASTER in r[1]][:3],
            example_other_txs=[h for h, r in routes.items() if CDP_PAYMASTER not in r[1]][:3],
            roll_configs=len(sc), roll_config_users=len({x['user'] for x in sc}), rolls=len(ro),
            roll_targets=dict(Counter(BLUE_NAMES.get(x['blue_id'], x['blue_id']) for x in sc + ro)),
            example_roll_txs=[x['tx'] for x in ro[:3]]))
    return out


if __name__ == '__main__':
    args = sys.argv[1:]
    day = args[0]
    d = os.path.abspath(args[args.index('--out') + 1]) if '--out' in args else private_dir('scope')
    os.makedirs(d, exist_ok=True)
    res = evidence(day)
    json.dump(dict(maturity_date=day, markets=res), open(os.path.join(d, 'scope-%s.json' % day), 'w'), indent=1)
    for r in res:
        print('%s %s borrowers %d (smart wallet %d) txs %d entrypoint %.0f%% bundler3 %.0f%% cdp-paymaster %d rolls %d configs %d' % (
            r['market'][:12], '+'.join('%s@%.3g' % (c['token'][:6], c['lltv']) for c in r['collateral']), r['borrowers'], r['smart_wallet_borrowers'],
            r['take_txs'], 100 * r['entrypoint_share'], 100 * r['bundler3_share'], r['cdp_paymaster_txs'], r['rolls'], r['roll_configs']))
    print('wrote', os.path.join(d, 'scope-%s.json' % day))
