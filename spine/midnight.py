"""How Morpho Midnight fixed-rate loans end at maturity, from the indexed events alone (data/midnight/*.json, see
fetch_midnight). No archive state: debt and credit per (market, user) are replayed in log order, so a maturity can be
labeled after the fact.

Replay rules (midnight/src/Midnight.sol): a Take moves `units` from seller to buyer. The buyer first pays down debt,
the rest becomes credit; the seller first spends credit, the rest becomes debt. Repay and Liquidate (repaidUnits +
badDebt) cut debt; Withdraw and UpdatePosition cut credit; ClaimContinuousFee cuts totalUnits. Each Take carries totalUnitsDelta = buyer credit increase -
seller credit decrease, which checks the credit replay on every trade.

Outcome of a position (market, borrower) = what removed its last debt: repaid (Repay, or buying units back), rolled
(Repay called by BlueFallbackRolling), liquidated (normal mode, price), liquidated_late (postMaturityMode), or open.
Late liquidations carry minutes past maturity and the implied penalty: LIF grows linearly from 1 at maturity to maxLif
at maturity + TIME_TO_MAX_LIF (60 min), maxLif = 1 / (1 - cursor * (1 - lltv)) (ConstantsLib.sol).

Commands (.venv/bin/python -m spine.midnight ...):
  label [YYYY-MM-DD ...]   outcome counts per maturity date; writes all-Midnight aggregates to data/midnight/maturity.json
  snapshot [--no-fetch]    live debt of every open position from the public RPC -> private/midnight/snapshots/
  tag                      Coinbase Smart Wallet split -> private/midnight/ only. PRIVATE: never commit or publish."""
import datetime, json, os, subprocess, sys, time
from collections import Counter, defaultdict
from spine.api import DATA, rpc, load, save
from spine.fetch_midnight import ROLLING, MIDNIGHT, EVENTS, fetch_all
from spine.tag_coinbase import MULTICALL3, word, decode_aggregate3, tag_chunk

REPO = os.path.dirname(DATA)
PRIVATE = os.path.join(REPO, 'private', 'midnight')
WAD = 10 ** 18
TIME_TO_MAX_LIF = 3600
DEBT, CREDIT, TOTAL_UNITS = '93af51c2', 'ca286bb9', '53a6332b'  # debt(bytes32,address) credit(bytes32,address) totalUnits(bytes32)
OUTCOME = {'repay': 'repaid', 'buyback': 'repaid', 'roll': 'rolled', 'liquidate': 'liquidated', 'liquidate_late': 'liquidated_late'}
OUTCOMES = ['repaid', 'rolled', 'liquidated', 'liquidated_late', 'open']
LATE_BINS = list(range(0, 65, 5))  # minutes-late histogram edges; the last bin is 60+


def events():
    ev = {name: (load('midnight/%s' % name) or {'items': []})['items'] for name in EVENTS}
    if not ev['MarketCreated']:
        raise SystemExit('no data/midnight/*.json; run .venv/bin/python -m spine.fetch_midnight first')
    return ev


def date(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%Y-%m-%d')


def max_lif(lltv, cursor):
    return WAD / (WAD - cursor * (WAD - lltv) / WAD)


def late_penalty(market, collateral, seconds_late):
    """Implied liquidation penalty (LIF - 1, in %) of a postMaturityMode liquidation seconds_late past maturity."""
    c = next(c for c in market['collaterals'] if c['token'] == collateral)
    m = max_lif(int(c['lltv']), int(c['cursor']))
    return 100 * (min(m, 1 + (m - 1) * max(seconds_late, 0) / TIME_TO_MAX_LIF) - 1)


def replay(ev):
    """Returns (positions, total_units, take_mismatches). positions[(id, user)] = dict(debt, credit, hist) where hist is
    [(ts, debt_after, kind, row)] for every debt change; total_units[id] mirrors the contract's totalUnits."""
    pos = defaultdict(lambda: dict(debt=0, credit=0, hist=[]))
    total, bad = defaultdict(int), []
    order = sorted((r['block'], r['log_index'], n, r) for n in ('Take', 'Repay', 'Liquidate', 'Withdraw', 'UpdatePosition', 'ClaimContinuousFee')
                   for r in ev[n])

    def debt(p, delta, kind, r):
        p['debt'] += delta
        p['hist'].append((r['ts'], p['debt'], kind, r))
        assert p['debt'] >= 0, (kind, r)

    for _, _, n, r in order:
        i = r['id']
        if n == 'Take':
            u = int(r['units'])
            buyer, seller = (r['maker'], r['taker']) if r['offer_is_buy'] else (r['taker'], r['maker'])
            b, s = pos[i, buyer], pos[i, seller]
            paid = min(u, b['debt'])
            if paid:
                debt(b, -paid, 'buyback', r)
            b['credit'] += u - paid
            spent = min(u, s['credit'])
            s['credit'] -= spent
            if u - spent:
                debt(s, u - spent, 'borrow', r)
            total[i] += u - paid - spent
            if u - paid - spent != int(r['total_units_delta']):
                bad.append(r)
        elif n == 'Repay':
            debt(pos[i, r['on_behalf']], -int(r['units']), 'roll' if r['caller'] == ROLLING.lower() else 'repay', r)
        elif n == 'Liquidate':
            cut = int(r['repaid_units']) + int(r['bad_debt'])
            total[i] -= int(r['bad_debt'])
            if cut:
                debt(pos[i, r['borrower']], -cut, 'liquidate_late' if r['post_maturity'] else 'liquidate', r)
        elif n == 'Withdraw':
            pos[i, r['on_behalf']]['credit'] -= int(r['units'])
            total[i] -= int(r['units'])
        elif n == 'ClaimContinuousFee':
            total[i] -= int(r['amount'])
        else:  # UpdatePosition: loss-factor slash plus continuous-fee accrual
            pos[i, r['user']]['credit'] -= int(r['credit_decrease'])
    return dict(pos), dict(total), bad


def debt_at(p, ts):
    """Debt after every event with timestamp <= ts."""
    return next((d for t, d, _, _ in reversed(p['hist']) if t <= ts), 0)


def label(ev, now=None):
    """One row per position that ever borrowed. Rows are public chain data; the Coinbase split never lands here."""
    now = now or time.time()
    mk = {m['id']: m for m in ev['MarketCreated']}
    pos, _, _ = replay(ev)
    out = []
    for (i, user), p in sorted(pos.items()):
        if not any(k == 'borrow' for _, _, k, _ in p['hist']) or i not in mk:
            continue
        m, T = mk[i], mk[i]['maturity']
        close = next((h for h in reversed(p['hist']) if h[1] == 0), None) if p['debt'] == 0 else None
        late = next((h for h in p['hist'] if h[2] == 'liquidate_late'), None)
        out.append(dict(
            market=i, user=user, maturity=T, maturity_date=date(T), matured=T <= now, loan_token=m['loan_token'],
            outcome=OUTCOME[close[2]] if close else 'open', closed_ts=close[0] if close else None,
            minutes_after_maturity=round((close[0] - T) / 60, 1) if close and close[0] > T else None,
            debt_minus_6h=str(debt_at(p, T - 6 * 3600)), debt_at_maturity=str(debt_at(p, T)),
            debt_plus_1h=str(debt_at(p, T + 3600)), debt_now=str(p['debt']),
            late_minutes=round((late[0] - T) / 60, 1) if late else None,
            late_penalty_pct=round(late_penalty(m, late[3]['collateral'], late[0] - T), 3) if late else None))
    return out


def late_hist(rows):
    h = [0] * len(LATE_BINS)
    for r in rows:
        if r['late_minutes'] is not None:
            h[min(int(r['late_minutes'] // 5), len(LATE_BINS) - 1)] += 1
    return h


def aggregate(rows, ev):
    """All-Midnight counts per maturity date. No addresses, no wallet types: this is what the Maturity tab shows."""
    by = defaultdict(list)
    for r in rows:
        by[r['maturity_date']].append(r)
    mat = {m['id']: m['maturity'] for m in ev['MarketCreated']}
    rolls = Counter(date(mat[x['midnight_id']]) for x in ev['Roll'] if x['midnight_id'] in mat)
    out = {}
    for d, rs in sorted(by.items()):
        at_T = [r for r in rs if int(r['debt_at_maturity']) > 0]
        out[d] = dict(markets=len({r['market'] for r in rs}), positions=len(rs), matured=all(r['matured'] for r in rs),
                      outcomes={o: sum(r['outcome'] == o for r in rs) for o in OUTCOMES},
                      open_at_maturity={o: sum(r['outcome'] == o for r in at_T) for o in OUTCOMES},
                      late_minutes_hist=late_hist(rs), roll_events=rolls.get(d, 0))
    return dict(generated_at=int(time.time()), late_bins_min=LATE_BINS, maturities=out)


def print_counts(agg, dates):
    print('%-10s %4s %5s  %s' % ('maturity', 'mkts', 'pos', '  '.join('%s' % o for o in OUTCOMES) + '   | debt>0 at T: same order'))
    for d in dates:
        a = agg['maturities'].get(d)
        if not a:
            print('%s: no positions' % d)
            continue
        print('%-10s %4d %5d  %s   | %s%s' % (d, a['markets'], a['positions'], '  '.join('%d' % a['outcomes'][o] for o in OUTCOMES),
                                            ' '.join('%d' % a['open_at_maturity'][o] for o in OUTCOMES), '' if a['matured'] else '  (not matured)'))
        if any(a['late_minutes_hist']):
            print('  minutes late (5-min bins, last 60+):', a['late_minutes_hist'])


def multicall(calls, block='latest', chunk=400):
    """[(target, calldata hex without 0x)] -> [returnData hex] via Multicall3 aggregate3 (allowFailure on)."""
    out = []
    for k in range(0, len(calls), chunk):
        items = [word(int(t, 16)) + word(1) + word(96) + word(len(c) // 2) + c + '0' * (-len(c) % 64) for t, c in calls[k:k + chunk]]
        offs, off = [], len(items) * 32
        for it in items:
            offs.append(word(off))
            off += len(it) // 2
        data = '0x82ad56cb' + word(32) + word(len(items)) + ''.join(offs) + ''.join(items)
        out += decode_aggregate3(rpc('eth_call', [{'to': MULTICALL3, 'data': data}, block]))
    return out


def read_debts(keys, block='latest'):
    """{(id, user): on-chain debt} at a block."""
    vals = multicall([(MIDNIGHT, DEBT + i[2:] + word(int(u, 16))) for i, u in keys], block)
    return {k: int(v or '0', 16) for k, v in zip(keys, vals)}


def private_dir(*sub):
    """private/midnight/<sub>, refusing to write unless git ignores it (the Coinbase split must never be committed)."""
    p = os.path.join(PRIVATE, *sub)
    os.makedirs(p, exist_ok=True)
    if subprocess.run(['git', 'check-ignore', '-q', p], cwd=REPO).returncode != 0:
        raise SystemExit('%s is not gitignored; refusing to write private data' % p)
    return p


def snapshot(fetch=True):
    """Live debt of every position with debt, read from the public RPC at the latest block. Run around maturity."""
    ev = fetch_all() if fetch else events()
    pos, _, _ = replay(ev)
    keys = sorted(k for k, p in pos.items() if p['debt'] > 0)
    blk = rpc('eth_getBlockByNumber', ['latest', False])
    live = read_debts(keys, blk['number'])
    ts = int(blk['timestamp'], 16)
    rows = [dict(market=i, user=u, debt=str(live[i, u]), rebuilt_debt=str(pos[i, u]['debt'])) for i, u in keys]
    stamp = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%Y%m%dT%H%MZ')
    path = os.path.join(private_dir('snapshots'), stamp + '.json')
    json.dump(dict(block=int(blk['number'], 16), ts=ts, positions=rows), open(path, 'w'))
    diff = sum(r['debt'] != r['rebuilt_debt'] for r in rows)
    print('snapshot %s block %d: %d positions with debt, %d differ from the event replay (new events since fetch?) -> %s'
          % (stamp, int(blk['number'], 16), len(rows), diff, path))


def tag():
    """Coinbase Smart Wallet split per maturity. Writes only under the gitignored private/midnight/."""
    ev = events()
    rows = label(ev)
    d = private_dir()
    cache_path = os.path.join(d, 'coinbase_wallets.json')
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    addrs = sorted({r['user'] for r in rows} | {x['user'] for x in ev['Roll'] + ev['SetConfig']})
    todo = [a for a in addrs if a not in cache]
    for k in range(0, len(todo), 1000):
        cache.update(tag_chunk(todo[k:k + 1000]))  # tag_chunk only reads chain state; tag_coinbase.tag() would write data/
    json.dump(cache, open(cache_path, 'w'))
    for r in rows:
        r['coinbase'] = bool(cache.get(r['user']))
    split = {}
    for dt in sorted({r['maturity_date'] for r in rows}):
        rs = [r for r in rows if r['maturity_date'] == dt]
        split[dt] = {side: dict(positions=len(g), outcomes={o: sum(r['outcome'] == o for r in g) for o in OUTCOMES},
                                late_minutes_hist=late_hist(g))
                     for side, g in (('coinbase', [r for r in rs if r['coinbase']]), ('other', [r for r in rs if not r['coinbase']]))}
    rolls = [dict(x, coinbase=bool(cache.get(x['user']))) for x in ev['Roll']]
    json.dump(dict(generated_at=int(time.time()), split=split, rolls=rolls, positions=rows), open(os.path.join(d, 'coinbase_split.json'), 'w'), indent=1)
    for dt, s in split.items():
        if s['coinbase']['positions']:
            print(dt, 'coinbase', s['coinbase']['outcomes'], '| other', s['other']['outcomes'])
    print('roll users coinbase: %d of %d; wrote %s' % (len({r['user'] for r in rolls if r['coinbase']}), len({r['user'] for r in rolls}), d))


if __name__ == '__main__':
    args = sys.argv[1:]
    cmd = args.pop(0) if args else 'label'
    if cmd == 'label':
        ev = events()
        agg = aggregate(label(ev), ev)
        save('midnight/maturity', agg, indent=1)
        print_counts(agg, args or sorted(agg['maturities']))
    elif cmd == 'snapshot':
        snapshot(fetch='--no-fetch' not in args)
    elif cmd == 'tag':
        tag()
    else:
        raise SystemExit(__doc__)
