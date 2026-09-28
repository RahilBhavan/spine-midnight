"""Done check for the Midnight maturity labels (spine.midnight). Run after spine.fetch_midnight:
.venv/bin/python -m spine.check_maturity [YYYY-MM-DD ...]   (default: every matured date plus 2026-09-25 and 2026-10-30)

1. Replay: every Take's totalUnitsDelta matches the credit replay; each position has one outcome, zero debt if closed.
2. Live state: debt(id, user) and totalUnits(id) read now over the public RPC equal the replay.
3. DefiLlama: indexed Base borrowed (sum of totalUnits, the adapter's own definition) within 2% of api.llama.fi.
4. Per maturity, after maturity + 1h: debt(id, user) at the maturity + 1h block (historical eth_call; Base blocks are 2s)
   equals the replay, and we count closed-labeled positions still holding debt then. Skips until the date has passed.
5. Rolls: each Roll's tx borrowed debtAssets + incentiveAssets on Morpho Blue for the user (Blue Borrow log in the
   receipt) and repaid debtAssets on Midnight. Private snapshots in private/midnight/snapshots are compared when present.
Exits 1 on any failure. Counts only, no rates."""
import glob, json, os, sys, time
from spine.api import MORPHO_BLUE, get_json, load, rpc
from spine.fetch_midnight import ROLLING
from spine.midnight import PRIVATE, TOTAL_UNITS, MIDNIGHT, events, label, multicall, read_debts, replay, debt_at

LLAMA = 'https://api.llama.fi/protocol/morpho-midnight'
BLUE_BORROW = '0x570954540bed6b1304a87dfe815a5eda4a648f7097a16240dcd85c9b5fd42a43'
COINBASE_DATES = ['2026-09-25', '2026-10-30']
fails = []


def check(ok, msg):
    print(('PASS ' if ok else 'FAIL ') + msg)
    if not ok:
        fails.append(msg)


def skip(msg):
    print('SKIP ' + msg)


def block_at(ts, tip):
    """Last block whose on-chain timestamp is at or before ts."""
    lo, hi = 0, tip
    while lo < hi:
        mid = (lo + hi + 1) // 2
        block = rpc('eth_getBlockByNumber', [hex(mid), False])
        if int(block['timestamp'], 16) <= ts:
            lo = mid
        else:
            hi = mid - 1
    return lo


if __name__ == '__main__':
    ev = events()
    age = time.time() - max(r['ts'] for n in ('Take', 'Repay', 'Liquidate') for r in ev[n])
    print('latest indexed event %.0f min ago' % (age / 60))
    pos, total, bad = replay(ev)
    rows = label(ev)
    mk = {m['id']: m for m in ev['MarketCreated']}

    # 1. replay consistency
    check(not bad, 'take replay: %d of %d Takes disagree with totalUnitsDelta' % (len(bad), len(ev['Take'])))
    closed_bad = [r for r in rows if (r['outcome'] == 'open') != (int(r['debt_now']) > 0)]
    check(not closed_bad, 'labels: %d positions, %d with outcome inconsistent with replayed debt' % (len(rows), len(closed_bad)))

    # 2. live state
    indexed_block = (load('midnight/rpc_checked') or {}).get('to')
    if indexed_block is None:
        raise SystemExit('no completed RPC sweep; run spine.fetch_midnight')
    tip = rpc('eth_getBlockByNumber', [hex(indexed_block), False])
    print('checking contract state at indexed Base block %d' % indexed_block)
    keys = sorted(pos)
    live = read_debts(keys, tip['number'])
    diff = [k for k in keys if live[k] != pos[k]['debt']]
    check(not diff, 'live debt: %d of %d (market, user) pairs differ from the replay%s' % (
        len(diff), len(keys), ' (events after the last fetch? rerun fetch_midnight)' if diff else ''))
    ids = sorted(mk)
    tu = dict(zip(ids, (int(v or '0', 16) for v in multicall([(MIDNIGHT, TOTAL_UNITS + i[2:]) for i in ids], tip['number']))))
    tdiff = [i for i in ids if tu[i] != total.get(i, 0)]
    check(not tdiff, 'live totalUnits: %d of %d markets differ from the replay' % (len(tdiff), len(ids)))

    # 3. Compare at DefiLlama's snapshot time; its latest snapshot can lag the RPC tip.
    llama_data = get_json(LLAMA)['chainTvls']['Base-borrowed']
    snapshot = llama_data['tvl'][-1]
    snapshot_ts = snapshot['date']
    token_snapshot = llama_data['tokens'][-1]
    usd_snapshot = llama_data['tokensInUsd'][-1]
    check(token_snapshot['date'] == snapshot_ts and usd_snapshot['date'] == snapshot_ts,
          'DefiLlama token and TVL snapshots have matching timestamps')
    _, then_total, then_bad = replay({n: [r for r in items if r['ts'] <= snapshot_ts] for n, items in ev.items()})
    check(not then_bad, 'DefiLlama-time replay: %d Take mismatches' % len(then_bad))
    by_token = {}
    for i in ids:
        token = mk[i]['loan_token']
        by_token[token] = by_token.get(token, 0) + then_total.get(i, 0)
    coins = get_json('https://coins.llama.fi/prices/current/' + ','.join('base:' + t for t in by_token))['coins']
    ours, omitted = 0.0, []
    for token, units in by_token.items():
        if not units:
            continue
        meta = coins.get('base:' + token)
        if not meta:
            decimals = int(rpc('eth_call', [{'to': token, 'data': '0x313ce567'}, 'latest']), 16)
            amount = units / 10 ** decimals
            omitted.append((token, amount))
            continue
        amount = units / 10 ** meta['decimals']
        symbol = meta['symbol']
        snapshot_amount = token_snapshot['tokens'].get(symbol)
        snapshot_usd = usd_snapshot['tokens'].get(symbol)
        if snapshot_amount is None or snapshot_usd is None:
            omitted.append((token, amount))
            continue
        ours += amount * snapshot_usd / snapshot_amount
    llama = snapshot['totalLiquidityUSD']
    gap = abs(ours - llama) / llama
    if omitted:
        skip('DefiLlama price comparison excludes unpriced loan tokens (address, units): %s' % omitted)
    check(gap <= 0.02, 'DefiLlama priced Base borrowed at %s UTC: indexed $%.0f vs DefiLlama $%.0f, gap %.2f%% (limit 2%%)' % (
        time.strftime('%Y-%m-%d %H:%M', time.gmtime(snapshot_ts)), ours, llama, 100 * gap))

    # 4. contract state at maturity + 1h
    dates = sys.argv[1:] or sorted({r['maturity_date'] for r in rows if r['matured']} | set(COINBASE_DATES))
    for d in dates:
        rs = [r for r in rows if r['maturity_date'] == d]
        if not rs:
            skip('%s: no borrowed positions indexed' % d)
            continue
        T1 = max(r['maturity'] for r in rs) + 3600
        if T1 > time.time():
            skip('%s: runs after maturity + 1h (%s UTC); rerun then' % (d, time.strftime('%Y-%m-%d %H:%M', time.gmtime(T1))))
            continue
        try:
            blk = hex(block_at(T1, int(tip['number'], 16)))
            then = read_debts([(r['market'], r['user']) for r in rs], blk)
        except Exception as e:  # public RPC without that much history
            skip('%s: no historical state (%s)' % (d, str(e)[:80]))
            continue
        wrong = [r for r in rs if then[r['market'], r['user']] != debt_at(pos[r['market'], r['user']], T1)]
        check(not wrong, '%s: debt at maturity + 1h matches the replay for %d of %d positions' % (d, len(rs) - len(wrong), len(rs)))
        closed = [r for r in rs if r['closed_ts'] is not None and r['closed_ts'] <= T1]
        remaining = [r for r in closed if then[r['market'], r['user']] > 0]
        check(not remaining, '%s: %d of %d positions closed by maturity + 1h have zero contract debt' % (d, len(closed) - len(remaining), len(closed)))

    # 5. rolls against Morpho Blue
    ok = 0
    for x in ev['Roll']:
        rc = rpc('eth_getTransactionReceipt', [x['tx']])
        borrow = [l for l in rc['logs'] if l['address'].lower() == MORPHO_BLUE.lower() and l['topics'][0] == BLUE_BORROW
                  and l['topics'][1] == x['blue_id'] and int(l['topics'][2], 16) == int(x['user'], 16)]
        assets = int(borrow[0]['data'][66:130], 16) if borrow else None
        repaid = sum(int(r['units']) for r in ev['Repay'] if r['tx'] == x['tx'] and r['caller'] == ROLLING.lower() and r['on_behalf'] == x['user'])
        ok += assets == int(x['debt_assets']) + int(x['incentive_assets']) and repaid == int(x['debt_assets'])
    check(ok == len(ev['Roll']), 'rolls: %d of %d Blue borrows equal debtAssets + incentiveAssets with the Midnight repay in the same tx' % (ok, len(ev['Roll'])))

    # private snapshots, if any were taken around maturity
    for p in sorted(glob.glob(os.path.join(PRIVATE, 'snapshots', '*.json'))):
        s = json.load(open(p))
        n = sum(int(r['debt']) != debt_at(pos[r['market'], r['user']], s['ts']) for r in s['positions'])
        check(n == 0, 'snapshot %s: %d of %d positions differ from the replay at its timestamp' % (os.path.basename(p), n, len(s['positions'])))

    print('ok' if not fails else '%d check(s) failed' % len(fails))
    sys.exit(1 if fails else 0)
