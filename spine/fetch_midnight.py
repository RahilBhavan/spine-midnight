"""Every Morpho Midnight event the maturity labels need, plus BlueFallbackRolling, on Base -> data/midnight/<event>.json.
Source: the free Blockscout etherscan-style getLogs (1000 logs per call, ascending). We walk fromBlock up to the
last block seen, dedupe on (tx, log index), and resume from the saved max block minus one. Blockscout rate-limits,
so calls are paced and a 429 sleeps out the whole x-ratelimit-reset window. Addresses: docs.morpho.org/get-started/resources/addresses (Base).
Topics are keccak of the signatures in midnight/src/libraries/EventsLib.sol and IBlueFallbackRolling.sol.
Withdraw and UpdatePosition are indexed too: they move credit, and credit decides whether a Take sell adds debt.
ClaimContinuousFee cuts a market's totalUnits, which is what DefiLlama reports as borrowed.
Blockscout misses some logs, so fill_gaps() then sweeps the public Base RPC and merges what is missing.
Run: .venv/bin/python -m spine.fetch_midnight"""
import http.client, json, os, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from spine.api import UA, load, rpc, save

BLOCKSCOUT = 'https://base.blockscout.com/api'
MIDNIGHT = '0xAdedD8ab6dE832766Fedf0FaC4992E5C4D3EA18A'
ROLLING = '0x7bfA8207B818cc0e23a7aBdCBe56572fAbCa831F'  # BlueFallbackRolling
PACE = 0.5 if os.environ.get('BLOCKSCOUT_API_KEY') else 7  # keyless limit is 10 calls per window; ~1 call per 7s never tripped it
MAX_WAIT = 400  # seconds; a longer Blockscout block hands the run to the RPC sweep
FROM_BLOCK = 48286884  # Midnight deployment on Base (DefiLlama adapter fromBlock; no event before it)

W = lambda d, i: int(d[2 + 64 * i:2 + 64 * (i + 1)], 16)  # i-th 32-byte word of hex data
A = lambda x: '0x' + ('%064x' % x)[-40:]                    # word -> address
H = lambda x: '0x%064x' % x                                  # word -> bytes32
S = lambda x: x - (1 << 256) if x >> 255 else x              # word -> int256


def market_created(t, d):
    """MarketCreated(Market market, bytes32 indexed id_): Market is a dynamic tuple with a static-tuple array."""
    o = W(d, 0) // 32
    c = o + W(d, o + 3) // 32
    cols = [dict(token=A(W(d, c + 1 + 4 * k)), lltv=str(W(d, c + 2 + 4 * k)), cursor=str(W(d, c + 3 + 4 * k)),
                 oracle=A(W(d, c + 4 + 4 * k))) for k in range(W(d, c))]
    return dict(id=t[1], chain_id=W(d, o), loan_token=A(W(d, o + 2)), collaterals=cols, maturity=W(d, o + 4),
                rcf_threshold=str(W(d, o + 5)), enter_gate=A(W(d, o + 6)), liquidator_gate=A(W(d, o + 7)))


# name: (contract, topic0, decoder(topics as ints-or-hex, data hex) -> dict)
EVENTS = {
    'MarketCreated': (MIDNIGHT, '0xdbf3e95a2290945645820c722294f678e0b3522a7dcf3cf2e2870268bf6c9472', market_created),
    'Liquidate': (MIDNIGHT, '0xb137b989b9fd54b984273db8f16364f52f383aaca56076a320c1896e9fc2dad9', lambda t, d: dict(
        id=t[1], collateral=A(int(t[2], 16)), borrower=A(int(t[3], 16)), caller=A(W(d, 0)), seized=str(W(d, 1)),
        repaid_units=str(W(d, 2)), post_maturity=bool(W(d, 3)), bad_debt=str(W(d, 6)))),
    'Repay': (MIDNIGHT, '0x8b2d9c5b5a1393c4a278da77a782a77bfd7ef753c26429830b006487d3167088', lambda t, d: dict(
        caller=A(int(t[1], 16)), id=t[2], on_behalf=A(int(t[3], 16)), units=str(W(d, 0)))),
    'Take': (MIDNIGHT, '0xbd88656d0ee14d32d4f74f814f6ccb5c750a4c542d6fdf83ee00ddca4edebff0', lambda t, d: dict(
        id=t[1], maker=A(int(t[2], 16)), taker=A(int(t[3], 16)), offer_is_buy=bool(W(d, 2)), units=str(W(d, 6)),
        buyer_assets=str(W(d, 7)), seller_assets=str(W(d, 8)), total_units_delta=str(S(W(d, 12))))),
    'Withdraw': (MIDNIGHT, '0x7a5e8e1731f88cf0c25f88fc7d5618e481e87be4d83f614e601850c2ff082fd7', lambda t, d: dict(
        id=t[1], on_behalf=A(int(t[2], 16)), units=str(W(d, 1)))),
    'UpdatePosition': (MIDNIGHT, '0x8fd212bc8fa18d807a9b47aa2de07104bf036cfcef8ea259157085a1b618a77c', lambda t, d: dict(
        id=t[1], user=A(int(t[2], 16)), credit_decrease=str(W(d, 0)))),
    'ClaimContinuousFee': (MIDNIGHT, '0xd64453ff5184816a23d670b5224717cd441c4a7cd5f4e7dbc30f8511d35f597b', lambda t, d: dict(
        id=t[2], amount=str(W(d, 0)))),
    'SetConfig': (ROLLING, '0x5a688e1dd9756a8fb6f9121f5bfb867eb485cf594652cab2a837f25c3e43d0d7', lambda t, d: dict(
        user=A(int(t[1], 16)), midnight_id=t[2], blue_id=t[3], caller=A(W(d, 0)), start=W(d, 1), end=W(d, 2),
        incentive_at_start=str(W(d, 3)), incentive_at_end=str(W(d, 4)), min_rollable=str(W(d, 5)), enabled=bool(W(d, 6)))),
    'Roll': (ROLLING, '0x48b0106d0e5756564fec4b993afdd2145168802e7122df03495d5155fb5aa31e', lambda t, d: dict(
        user=A(int(t[1], 16)), midnight_id=t[2], blue_id=t[3], caller=A(W(d, 0)), config_id=H(W(d, 1)),
        debt_assets=str(W(d, 2)), collateral_assets=str(W(d, 3)), incentive_assets=str(W(d, 4)))),
}


class Throttled(Exception):
    pass


def get_logs(address, topic0, frm):
    """One getLogs page. Blockscout answers 200 with status 0 both for 'no logs' and for throttling, and a 429
    carries x-ratelimit-reset (ms until the window reopens: ~6 min on a first trip, ~1h on a repeat). Short blocks are
    slept out; longer ones raise Throttled and the RPC sweep covers the run. BLOCKSCOUT_API_KEY lifts the keyless limit."""
    url = '%s?module=logs&action=getLogs&address=%s&topic0=%s&fromBlock=%d&toBlock=latest' % (BLOCKSCOUT, address, topic0, frm)
    if os.environ.get('BLOCKSCOUT_API_KEY'):
        url += '&apikey=' + os.environ['BLOCKSCOUT_API_KEY']
    for i in range(8):
        time.sleep(PACE)
        try:
            r = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60))
        except urllib.error.HTTPError as e:
            wait = int(e.headers.get('x-ratelimit-reset') or 0) / 1000 if e.code == 429 else 0
            if wait > MAX_WAIT:
                raise Throttled('blockscout blocked for %.0f min' % (wait / 60))
            if e.code >= 500:
                raise Throttled('blockscout returned HTTP %d' % e.code)
            print('  blockscout %d, sleeping %.0fs' % (e.code, max(wait, 2 ** i)), flush=True)
            time.sleep(max(wait, 2 ** i) + 5)
            continue
        except http.client.RemoteDisconnected:
            raise Throttled('blockscout disconnected')
        except (urllib.error.URLError, TimeoutError, ValueError):  # dropped connection or truncated body
            time.sleep(2 ** i)
            continue
        if r.get('status') == '1':
            return r['result']
        if 'no logs' in (r.get('message') or '').lower():
            return []
        time.sleep(4 * 2 ** i)
    raise RuntimeError('blockscout getLogs failed after retries: %s' % url)


def row(name, log):
    ts, block, li = (int(log[k], 16) for k in ('timeStamp' if 'timeStamp' in log else 'blockTimestamp', 'blockNumber', 'logIndex'))
    topics = log['topics']
    return dict(ts=ts, block=block, log_index=li, tx=log['transactionHash'], **EVENTS[name][2](topics, log['data']))


def key(r):
    return r['tx'], r['log_index']


def fetch_event(name):
    address, topic0, _ = EVENTS[name]
    old = (load('midnight/%s' % name) or {'items': []})['items']
    seen = {key(r): r for r in old}
    frm = max((r['block'] for r in old), default=FROM_BLOCK + 1) - 1
    while True:
        try:
            logs = get_logs(address, topic0, frm)
        except Throttled as e:  # keep what we have; fill_gaps() sweeps the RPC from its own checkpoint
            print('  %s: %s, leaving it to the RPC sweep' % (name, e), flush=True)
            break
        new = [r for r in (row(name, l) for l in logs) if key(r) not in seen]
        seen.update((key(r), r) for r in new)
        last = max((int(l['blockNumber'], 16) for l in logs), default=frm)
        if len(logs) < 1000:
            break
        if last == frm:  # ponytail: >1000 logs of one type in one block would loop; never seen, fail loudly
            raise RuntimeError('%s: 1000+ logs in block %d' % (name, frm))
        frm = last  # re-read the last block: a page can end mid-block
    items = sorted(seen.values(), key=lambda r: (r['block'], r['log_index']))
    save('midnight/%s' % name, dict(fetched_at=int(time.time()), contract=address, event=name, count=len(items), items=items),
         separators=(',', ':'))
    return items


def fill_gaps(ev, step=2000):
    """Blockscout's index has holes (a Take in block 48635200 is missing from it entirely), so we sweep the public RPC's
    eth_getLogs (2000-block cap) for every topic on both contracts and merge what Blockscout lacks. Incremental:
    data/midnight/rpc_checked.json holds the last block swept. Returns {event: logs added}."""
    names = {topic0: name for name, (_, topic0, _) in EVENTS.items()}
    frm = (load('midnight/rpc_checked') or {'to': FROM_BLOCK})['to']
    tip = int(rpc('eth_blockNumber', []), 16)
    seen = {name: {key(r) for r in items} for name, items in ev.items()}
    added = {name: 0 for name in EVENTS}
    def checkpoint(to):
        for name in EVENTS:
            ev[name].sort(key=lambda r: (r['block'], r['log_index']))
            save('midnight/%s' % name, dict(fetched_at=int(time.time()), contract=EVENTS[name][0], event=name, count=len(ev[name]),
                                            items=ev[name]), separators=(',', ':'))
        save('midnight/rpc_checked', {'to': to})

    get = lambda b: rpc('eth_getLogs', [{'address': [MIDNIGHT, ROLLING], 'topics': [list(names)], 'fromBlock': hex(b),
                                          'toBlock': hex(min(b + step - 1, tip))}])
    starts = list(range(frm, tip + 1, step))
    with ThreadPoolExecutor(8) as pool:  # ~1700 windows for a full sweep; 8 in flight keeps the public RPC happy
        for k in range(0, len(starts), 200):
            for logs in pool.map(get, starts[k:k + 200]):
                for l in logs:
                    name = names[l['topics'][0]]
                    r = row(name, l)
                    if key(r) not in seen[name]:
                        seen[name].add(key(r))
                        ev[name].append(r)
                        added[name] += 1
            end = starts[min(k + 200, len(starts)) - 1] + step
            checkpoint(min(end, tip + 1))  # resumable: a killed sweep restarts from here
            print('  rpc sweep at block %d of %d, added %d' % (min(end, tip + 1), tip, sum(added.values())), flush=True)
    checkpoint(tip + 1)
    return added


def fetch_all():
    ev = {name: fetch_event(name) for name in EVENTS}
    added = fill_gaps(ev)
    if any(added.values()):
        print('public RPC filled logs Blockscout lacked:', {k: v for k, v in added.items() if v})
    return ev


if __name__ == '__main__':
    t = time.time()
    ev = fetch_all()
    for name, items in ev.items():
        extra = ' (post-maturity %d)' % sum(r['post_maturity'] for r in items) if name == 'Liquidate' else ''
        print('%-15s %6d%s' % (name, len(items), extra))
    print('fetched in %.0fs' % (time.time() - t))
    assert len(ev['Liquidate']) >= 762, 'spec counted 762 Liquidate logs on 2026-09-23'
    assert ev['MarketCreated'] and all(m['chain_id'] == 8453 for m in ev['MarketCreated'])
    print('ok')
