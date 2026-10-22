"""Per-outcome table for one maturity date, split into Coinbase-scoped markets and general Midnight activity.

  .venv/bin/python -m spine.cohort YYYY-MM-DD [--out DIR] [--fetch] [--check]

--fetch runs spine.fetch_midnight first; --check runs spine.check_maturity for the date and records PASS/FAIL (exit 1
on FAIL after writing, so a failed check is never silent). Writes DIR/cohort-YYYY-MM-DD.json and .md (default DIR:
private/midnight/cohort, gitignored). Output is aggregate: counts and dollars per outcome and market, no addresses.

Before maturity the same table is a pre-maturity snapshot: 'open' is debt still owed now, and repaid/rolled rows that
closed before maturity are early exits. Dollars: USDC at $1.00; other loan tokens at the DefiLlama price at maturity
(or now, if maturity is in the future). Units are face value, so debt in units is what is owed at maturity.

Scope. A market is Coinbase-scoped only if it is in SCOPE: a USDC loan against cbBTC at 86% LLTV (Coinbase's published
BTC LLTV), maturing 15:00 UTC on a month's last Friday (Coinbase's "end of month"), with Coinbase-flow evidence: most
borrowers are Coinbase Smart Wallet contracts entering through the ERC-4337 EntryPoint with the CDP paymaster, and
BlueFallbackRolling configs target the Morpho Blue cbBTC/USDC market behind Coinbase's variable-rate loans. The evidence
is circumstantial (Smart Wallets and the paymaster also serve other Base apps). Everything else is general Midnight activity."""
import datetime, json, os, statistics, subprocess, sys, time
from collections import Counter
from spine.api import get_json, rpc
from spine.midnight import OUTCOMES, PRIVATE, REPO, USDC, date, dust_units, events, label, private_dir

SCOPE = {  # market id -> why it is Coinbase-scoped; see data/README-scope in the APM export for tx hashes
    '0x549cd072daf99328554f3a6d2d4d6f4a07f1c59369e891e6391946f9cf75f221': 'Sep 25: cbBTC 86% -> USDC',
    '0x0ff32a563814a0e5822441b77c1b463da99f095d686cb09b637d5d80418cffed': 'Oct 30: cbBTC 86% (+USDC 98%) -> USDC',
    '0x6cb9f4a081803895ee5f8cb611c9a7ad7ccadd88d8a8343f478d9e5646efecac': 'Nov 27: cbBTC 86% (+USDC 98%) -> USDC',
}
SYMBOLS = {USDC: 'USDC', '0x4200000000000000000000000000000000000006': 'WETH', '0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf': 'cbBTC',
           '0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22': 'cbETH'}
SIZE_BINS = [1, 100, 1_000, 10_000, 100_000]  # USD edges; the first bucket is dust (< $1)
SIZE_LABELS = ['<$1', '$1-100', '$100-1k', '$1k-10k', '$10k-100k', '$100k+']


def symbol(token):
    token = token.lower()
    if token not in SYMBOLS:
        try:
            raw = bytes.fromhex(rpc('eth_call', [{'to': token, 'data': '0x95d89b41'}, 'latest'])[2:])
            SYMBOLS[token] = raw[64:64 + int.from_bytes(raw[32:64], 'big')].decode(errors='replace') or token[:10]
        except Exception:
            SYMBOLS[token] = token[:10]
    return SYMBOLS[token]


def prices(tokens, ts):
    """{token: (usd, decimals) or None}. USDC is pinned at $1; others come from DefiLlama at ts."""
    out = {USDC: (1.0, 6)}
    rest = sorted(set(t.lower() for t in tokens) - {USDC})
    if rest:
        coins = get_json('https://coins.llama.fi/prices/historical/%d/%s' % (ts, ','.join('base:' + t for t in rest)))['coins']
        for t in rest:
            c = coins.get('base:' + t)
            out[t] = (c['price'], c['decimals']) if c else None
    return out


def usd(units, token, px):
    p = px.get(token.lower())
    return None if p is None else int(units) / 10 ** p[1] * p[0]


def total(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs), 2) if xs else 0.0


def summarize(rows):
    """One table row per outcome plus an 'all' row. Timing is against each position's own market maturity."""
    table = {}
    for o in OUTCOMES + ['all']:
        rs = [r for r in rows if o == 'all' or r['outcome'] == o]
        late = [r['minutes_after_maturity'] for r in rs if r['closed_ts'] and r['closed_ts'] > r['maturity']]
        table[o] = dict(
            positions=len(rs),
            closed_before_maturity=sum(1 for r in rs if r['closed_ts'] and r['closed_ts'] <= r['maturity']),
            owed_at_maturity_positions=sum(1 for r in rs if r['owed_at_T_usd'] and r['owed_at_T_usd'] >= 1),
            peak_usd=total(r['peak_usd'] for r in rs), owed_minus_6h_usd=total(r['owed_minus_6h_usd'] for r in rs),
            owed_at_maturity_usd=total(r['owed_at_T_usd'] for r in rs), owed_now_usd=total(r['owed_now_usd'] for r in rs),
            minutes_past_maturity=dict(n=len(late), median=statistics.median(late), max=max(late)) if late else None,
            penalties_usd=round(sum(r['penalty_usd'] or 0 for r in rs), 4))
    return table


def size_hist(values):
    """Count of USD values per SIZE_BINS bucket."""
    h = dict.fromkeys(SIZE_LABELS, 0)
    for v in values:
        h[SIZE_LABELS[sum(v >= e for e in SIZE_BINS)]] += 1
    return h


def build(day, ev=None, now=None, price_fn=prices):
    ev = ev or events()
    now = now or time.time()
    ev = {n: v if n in ('MarketCreated', '_indexed_to') else [r for r in v if r['ts'] <= now] for n, v in ev.items()}  # as of now
    mk = {m['id']: m for m in ev['MarketCreated']}
    rows = [r for r in label(ev, now=now) if r['maturity_date'] == day]
    if not rows:
        raise SystemExit('%s: no borrowed positions indexed' % day)
    T = Counter(r['maturity'] for r in rows).most_common(1)[0][0]  # the date's main maturity (15:00 UTC); a few markets differ
    matured = max(r['maturity'] for r in rows) <= now
    px = price_fn({r['loan_token'] for r in rows}, int(min(T, now)))
    for r in rows:
        tok = r['loan_token']
        r.update(peak_usd=usd(r['peak_debt'], tok, px), owed_minus_6h_usd=usd(r['debt_minus_6h'], tok, px),
                 owed_at_T_usd=usd(r['debt_at_maturity'], tok, px), owed_now_usd=usd(r['debt_now'], tok, px),
                 penalty_usd=usd(r['late_penalty_units'], tok, px), scope='coinbase_scoped' if r['market'] in SCOPE else 'general')
    markets = []
    for i in sorted({r['market'] for r in rows}, key=lambda i: (i not in SCOPE, i)):
        rs = [r for r in rows if r['market'] == i]
        m = mk[i]
        markets.append(dict(
            market=i, scope='coinbase_scoped' if i in SCOPE else 'general', scope_note=SCOPE.get(i),
            loan=symbol(m['loan_token']), collateral=['%s %.1f%%' % (symbol(c['token']), int(c['lltv']) / 1e16) for c in m['collaterals']],
            maturity_utc=datetime.datetime.fromtimestamp(m['maturity'], datetime.timezone.utc).strftime('%Y-%m-%d %H:%M'),
            positions=len(rs), outcomes={o: sum(r['outcome'] == o for r in rs) for o in OUTCOMES},
            peak_usd=total(r['peak_usd'] for r in rs), owed_at_maturity_usd=total(r['owed_at_T_usd'] for r in rs),
            owed_now_usd=total(r['owed_now_usd'] for r in rs)))
    owed = 'owed_now_usd' if not matured else 'owed_at_T_usd'
    scopes = {}
    for s in ('coinbase_scoped', 'general', 'all'):
        rs = [r for r in rows if s == 'all' or r['scope'] == s]
        scopes[s] = dict(outcomes=summarize(rs),
                         owing_positions_size_hist=size_hist([r[owed] for r in rs if r[owed] is not None and int(r['debt_now' if not matured else 'debt_at_maturity']) > dust_units(r['loan_token'])]),
                         peak_size_hist=size_hist([r['peak_usd'] for r in rs if r['peak_usd'] is not None]),
                         early_exits=dict(repaid=sum(1 for r in rs if r['outcome'] == 'repaid' and r['closed_ts'] and r['closed_ts'] <= min(r['maturity'], now)),
                                          rolled=sum(1 for r in rs if r['outcome'] == 'rolled' and r['closed_ts'] and r['closed_ts'] <= min(r['maturity'], now))))
    indexed = ev.get('_indexed_to')
    return dict(maturity_date=day, maturity_ts=T, matured=matured, generated_at=int(now), indexed_to_block=indexed,
                last_event_ts=max(r['ts'] for n in ('Take', 'Repay', 'Liquidate') for r in ev[n]),
                prices={SYMBOLS.get(t, t): p[0] if p else None for t, p in px.items()}, dust_units={symbol(t): dust_units(t) for t in px},
                scope_markets=SCOPE, scopes=scopes, markets=markets)


def money(x):
    return '$%s' % f'{x:,.0f}' if x >= 10 else '$%.2f' % x


def markdown(rep, check=None):
    T = datetime.datetime.fromtimestamp(rep['maturity_ts'], datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    gen = datetime.datetime.fromtimestamp(rep['generated_at'], datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    last = datetime.datetime.fromtimestamp(rep['last_event_ts'], datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    owed = 'owed_at_maturity_usd' if rep['matured'] else 'owed_now_usd'
    out = ['# Midnight maturity %s (%s)' % (rep['maturity_date'], 'matured' if rep['matured'] else 'pre-maturity snapshot'), '',
           'Maturity %s. Generated %s from events indexed to block %s (latest event %s). check_maturity: %s. One cohort.' % (
               T, gen, rep['indexed_to_block'], last, check or 'not run'), '']
    for s, title in (('coinbase_scoped', 'Coinbase-scoped markets'), ('general', 'General Midnight activity'), ('all', 'All markets')):
        t = rep['scopes'][s]['outcomes']
        out += ['## %s' % title, '',
                '| Outcome | Positions | Closed before maturity | Peak debt | %s | Minutes past maturity (median / max) | Penalties paid |' % (
                    'Owed at maturity' if rep['matured'] else 'Owed now'),
                '|---|---:|---:|---:|---:|---:|---:|']
        for o in OUTCOMES + ['all']:
            r = t[o]
            mins = '%.1f / %.1f (n=%d)' % (r['minutes_past_maturity']['median'], r['minutes_past_maturity']['max'], r['minutes_past_maturity']['n']) if r['minutes_past_maturity'] else '-'
            out.append('| %s | %d | %d | %s | %s | %s | %s |' % (o if o != 'all' else '**total**', r['positions'], r['closed_before_maturity'], money(r['peak_usd']),
                                                                 money(r[owed]), mins, '$%.2f' % r['penalties_usd']))
        h = rep['scopes'][s]['owing_positions_size_hist']
        out += ['', 'Positions still owing above dust, by size: ' + ', '.join('%s: %d' % kv for kv in h.items()), '']
    out += ['## By market', '', '| Market | Scope | Collateral -> loan | Positions | %s | Outcomes |' % ('Owed at maturity' if rep['matured'] else 'Owed now'), '|---|---|---|---:|---:|---|']
    for m in rep['markets']:
        out.append('| `%s` | %s | %s -> %s | %d | %s | %s |' % (m['market'][:12], m['scope'], ' + '.join(m['collateral']), m['loan'], m['positions'],
                                                          money(m['owed_at_maturity_usd' if rep['matured'] else 'owed_now_usd']),
                                                          ', '.join('%s %d' % (o, n) for o, n in m['outcomes'].items() if n)))
    out += ['', 'Dollars: USDC at $1.00; other loan tokens at DefiLlama prices %s. Dust: remainders up to $1 are not open debt.' % (
        'at maturity' if rep['matured'] else 'now'), 'Borrowers are contracts; "Coinbase-scoped" is a market-level inference, not a count of Coinbase users.', '']
    return '\n'.join(out)


def main(args):
    day = next(a for a in args if not a.startswith('--'))
    out_dir = args[args.index('--out') + 1] if '--out' in args else None
    if '--fetch' in args:
        subprocess.run([sys.executable, '-m', 'spine.fetch_midnight'], cwd=REPO, check=True)
    check = None
    if '--check' in args:
        rc = subprocess.run([sys.executable, '-m', 'spine.check_maturity', day], cwd=REPO).returncode
        check = 'PASS' if rc == 0 else 'FAIL'
    ev = events()
    ev['_indexed_to'] = (json.load(open(os.path.join(REPO, 'data', 'midnight', 'rpc_checked.json'))) or {}).get('to')
    rep = build(day, ev)
    rep['check_maturity'] = check
    d = os.path.abspath(out_dir) if out_dir else private_dir('cohort')
    os.makedirs(d, exist_ok=True)
    json.dump(rep, open(os.path.join(d, 'cohort-%s.json' % day), 'w'), indent=1)
    open(os.path.join(d, 'cohort-%s.md' % day), 'w').write(markdown(rep, check))
    print(markdown(rep, check))
    print('wrote %s/cohort-%s.{json,md}' % (d, day))
    return 1 if check == 'FAIL' else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
