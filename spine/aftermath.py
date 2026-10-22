"""What happened to one maturity's borrowers afterwards, for the Coinbase-scoped market of that date.

  python -m spine.aftermath YYYY-MM-DD [--out DIR]

A. Re-borrow. For each borrower in the scoped market, did the same address borrow again after maturity: a new Midnight
   borrow (a Take that adds debt) in any market, or a Morpho Blue Borrow on behalf of that address in the cbBTC/USDC Blue
   market behind Coinbase's variable-rate loans? Compared between borrowers liquidated after maturity and borrowers who
   repaid or rolled before it. A position is (market, address), so in one market positions = distinct addresses; one
   person may still own several addresses, so these are addresses, not people.
B. Liquidity at maturity. For each borrower liquidated after maturity (all of them owed at maturity), the address's onchain USDC and cbBTC balance at the
   last block at or before maturity and 24 hours earlier, against the debt owed then (debt units are USDC owed at
   maturity). Coinbase may hold a customer's USDC off-chain in their Coinbase account, so an onchain balance is a lower
   bound on the funds the borrower had.
Also tags each group as Coinbase Smart Wallet contracts or not (tag_coinbase), aggregate counts only.

Output is aggregate: counts per group, no addresses. Default DIR: private/midnight/aftermath (gitignored)."""
import datetime, json, os, sys
from concurrent.futures import ThreadPoolExecutor
from spine.api import MARKETS, MORPHO_BLUE, rpc
from spine.check_maturity import block_at
from spine.cohort import SCOPE
from spine.midnight import USDC, events, label, private_dir, replay
from spine.tag_coinbase import tag_chunk, word

CBBTC = '0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf'
BLUE_CBBTC = MARKETS['cbBTC']['id']
BLUE_BORROW = '0x570954540bed6b1304a87dfe815a5eda4a648f7097a16240dcd85c9b5fd42a43'  # Borrow(id, caller, onBehalf, receiver, assets, shares)
BALANCE_OF = '70a08231'
GROUPS = {'liquidated_after_maturity': ('liquidated_late',), 'repaid_or_rolled_before': ('repaid', 'rolled')}
STEP = int(os.environ.get('RPC_LOGS_STEP', 1000))


def midnight_borrows(pos):
    """{address: [ts of every Midnight borrow, any market]} from the replayed debt history."""
    out = {}
    for (_, user), p in pos.items():
        out.setdefault(user, []).extend(t for t, _, kind, _ in p['hist'] if kind == 'borrow')
    return out


def reborrow(rows, mid_borrows, blue_borrows, after_ts, groups=GROUPS):
    """Per group: addresses, and how many borrowed again strictly after after_ts on Midnight, on Blue, or either.
    rows: label() rows of one market; mid_borrows / blue_borrows: {address: [ts, ...]}."""
    out = {}
    for g, outcomes in groups.items():
        users = sorted({r['user'] for r in rows if r['outcome'] in outcomes})
        mid = {u for u in users if any(t > after_ts for t in mid_borrows.get(u, []))}
        blue = {u for u in users if any(t > after_ts for t in blue_borrows.get(u, []))}
        out[g] = dict(positions=sum(1 for r in rows if r['outcome'] in outcomes), addresses=len(users),
                      reborrowed_midnight=len(mid), reborrowed_blue=len(blue), reborrowed_any=len(mid | blue),
                      reborrow_rate=round(len(mid | blue) / len(users), 3) if users else None)
    return out


def liquidity(owing, balances_at_T, balances_before):
    """owing: [(address, debt_units_at_maturity)]; balances_*: {address: (usdc_units, cbbtc_sats)}.
    Counts how many could have repaid from onchain USDC alone, at maturity and 24h before."""
    def covered(bal):
        return sum(1 for u, d in owing if bal[u][0] >= d)
    above = [(u, d) for u, d in owing if d >= 10 ** 6]
    return dict(owing_positions=len(owing), owing_above_1usd=len(above),
                usdc_covers_debt_at_maturity=covered(balances_at_T), usdc_covers_debt_24h_before=covered(balances_before),
                usdc_covers_debt_at_maturity_above_1usd=sum(1 for u, d in above if balances_at_T[u][0] >= d),
                usdc_covers_debt_24h_before_above_1usd=sum(1 for u, d in above if balances_before[u][0] >= d),
                any_usdc_at_maturity=sum(1 for u, _ in owing if balances_at_T[u][0] > 0),
                any_cbbtc_at_maturity=sum(1 for u, _ in owing if balances_at_T[u][1] > 0),
                covered_debts_usd_at_maturity_above_1usd=sorted(round(d / 1e6, 2) for u, d in above if balances_at_T[u][0] >= d),
                debt_usd_total=round(sum(d for _, d in owing) / 1e6, 2),
                usdc_usd_total_at_maturity=round(sum(balances_at_T[u][0] for u, _ in owing) / 1e6, 2))


def fisher_two_sided(a, b, c, d):
    """Two-sided Fisher exact p for the 2x2 table [[a, b], [c, d]] (sum of tables no more likely than the observed)."""
    from math import comb
    r1, c1, n = a + b, a + c, a + b + c + d
    p = lambda x: comb(c1, x) * comb(n - c1, r1 - x) / comb(n, r1)
    obs = p(a)
    return min(1.0, sum(p(x) for x in range(max(0, r1 + c1 - n), min(r1, c1) + 1) if p(x) <= obs * (1 + 1e-9)))


def balances(users, block):
    """{address: (USDC units, cbBTC sats)} at a block, by historical eth_call balanceOf."""
    def one(u):
        call = lambda tok: int(rpc('eth_call', [{'to': tok, 'data': '0x' + BALANCE_OF + word(int(u, 16))}, hex(block)]), 16)
        return u, (call(USDC), call(CBBTC))
    with ThreadPoolExecutor(4) as pool:
        return dict(pool.map(one, users))


def blue_borrows_for(users, from_block, to_block):
    """{address: [ts]} of Blue cbBTC/USDC Borrow events on behalf of users, from eth_getLogs in STEP-block windows."""
    topics = [BLUE_BORROW, BLUE_CBBTC, ['0x' + word(int(u, 16)) for u in users]]
    spans = [(b, min(b + STEP - 1, to_block)) for b in range(from_block, to_block + 1, STEP)]

    def get(span):
        return rpc('eth_getLogs', [{'address': MORPHO_BLUE, 'topics': topics, 'fromBlock': hex(span[0]), 'toBlock': hex(span[1])}])
    with ThreadPoolExecutor(int(os.environ.get('RPC_THREADS', 4))) as pool:
        logs = [l for chunk in pool.map(get, spans) for l in chunk]
    ts_cache, out = {}, {}
    for l in logs:
        b = int(l['blockNumber'], 16)
        if b not in ts_cache:
            ts_cache[b] = int(rpc('eth_getBlockByNumber', [hex(b), False])['timestamp'], 16)
        out.setdefault('0x' + l['topics'][2][-40:], []).append(ts_cache[b])
    return out


def run(day, ev=None):
    ev = ev or events()
    market = next(i for i, note in SCOPE.items() if note.startswith(datetime.date.fromisoformat(day).strftime('%b %-d')))
    rows = [r for r in label(ev) if r['market'] == market]
    T = rows[0]['maturity']
    pos, _, _ = replay(ev)
    tip = int(rpc('eth_blockNumber', []), 16)
    last_ts = max(r['ts'] for n in ('Take', 'Repay', 'Liquidate') for r in ev[n])
    end_block = max(r['block'] for n in ('Take', 'Repay', 'Liquidate') for r in ev[n])  # Blue window ends where the Midnight index ends
    bT, b24 = block_at(T, tip), block_at(T - 86400, tip)
    users = sorted({r['user'] for r in rows})
    blue = blue_borrows_for(users, bT + 1, end_block)
    mids = midnight_borrows(pos)
    A = reborrow(rows, mids, blue, T)
    big = [r for r in rows if r['outcome'] == 'liquidated_late' and int(r['debt_at_maturity']) >= 10 ** 6]
    A['liquidated_after_maturity_owing_1usd_plus'] = reborrow(big, mids, blue, T, {'g': ('liquidated_late',)})['g']
    tags = tag_chunk(users)
    for g, outcomes in GROUPS.items():
        A[g]['smart_wallet_addresses'] = sum(1 for r in rows if r['outcome'] in outcomes and tags.get(r['user']))
    l, r = A['liquidated_after_maturity'], A['repaid_or_rolled_before']
    A['fisher_p_two_sided'] = round(fisher_two_sided(l['reborrowed_any'], l['addresses'] - l['reborrowed_any'], r['reborrowed_any'], r['addresses'] - r['reborrowed_any']), 3)
    A['liquidated_after_maturity_owing_1usd_plus']['smart_wallet_addresses'] = sum(1 for r in big if tags.get(r['user']))
    owing = [(r['user'], int(r['debt_at_maturity'])) for r in rows if r['outcome'] == 'liquidated_late' and int(r['debt_at_maturity']) > 0]
    late = [u for u, _ in owing]
    B = liquidity(owing, balances(late, bT), balances(late, b24))
    return dict(maturity_date=day, market=market, maturity_ts=T, maturity_block=bT, minus_24h_block=b24,
                window_end_block=end_block, window_end_ts=last_ts, reborrow=A, liquidity=B,
                notes=['Addresses, not people: one person may own several addresses.',
                       'Re-borrow = a Midnight borrow in any market or a Blue cbBTC/USDC Borrow on behalf of the address, strictly after maturity, up to the window end.',
                       'Onchain USDC is a lower bound on available funds: Coinbase may hold customer USDC off-chain.'])


def markdown(rep):
    f = lambda ts: datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    A, B = rep['reborrow'], rep['liquidity']
    out = ['# After the %s maturity: re-borrow and liquidity (Coinbase-scoped market `%s`)' % (rep['maturity_date'], rep['market'][:12]), '',
           'Maturity %s (block %d). Re-borrow window: maturity to %s (block %d). One cohort.' % (
               f(rep['maturity_ts']), rep['maturity_block'], f(rep['window_end_ts']), rep['window_end_block']), '',
           '## A. Re-borrow after maturity', '',
           '| Group | Positions | Distinct addresses | Smart Wallet addresses | Re-borrowed on Midnight | Re-borrowed on Blue cbBTC/USDC | Re-borrowed (either) | Rate |',
           '|---|---:|---:|---:|---:|---:|---:|---:|']
    for g, a in A.items():
        if not isinstance(a, dict):
            continue
        out.append('| %s | %d | %d | %d | %d | %d | %d | %s |' % (g, a['positions'], a['addresses'], a['smart_wallet_addresses'], a['reborrowed_midnight'],
                                                         a['reborrowed_blue'], a['reborrowed_any'], '%.0f%%' % (100 * a['reborrow_rate']) if a['reborrow_rate'] is not None else '-'))
    out += ['', 'Two-sided Fisher exact p (liquidated vs repaid/rolled, re-borrowed either way): %s.' % A['fisher_p_two_sided']]
    out += ['', '## B. Onchain liquidity of the borrowers liquidated after maturity', '',
            '| Measure | Value |', '|---|---:|'] + ['| %s | %s |' % (k, v) for k, v in B.items()] + ['', *('- ' + n for n in rep['notes']), '']
    return '\n'.join(out)


if __name__ == '__main__':
    args = sys.argv[1:]
    day = args[0]
    d = os.path.abspath(args[args.index('--out') + 1]) if '--out' in args else private_dir('aftermath')
    os.makedirs(d, exist_ok=True)
    rep = run(day)
    json.dump(rep, open(os.path.join(d, 'aftermath-%s.json' % day), 'w'), indent=1)
    open(os.path.join(d, 'aftermath-%s.md' % day), 'w').write(markdown(rep))
    print(markdown(rep))
