"""Midnight replay and labels on a synthetic event log (offline), plus a shape check of the committed aggregate."""
import pytest
import urllib.error
from io import BytesIO
from spine.api import load
from spine import fetch_midnight
from spine.fetch_midnight import ROLLING
from spine.midnight import DUST_UNITS, OUTCOMES, USDC, dust_units, label, late_penalty, max_lif, replay
from spine import check_maturity

T = 1_800_000_000
M = '0x' + '11' * 32
COL = '0x' + 'cb' * 20
LENDER, A, B, C, D = ('0x' + c * 40 for c in 'abcd9')
n = iter(range(1, 1000))


def ev(ts, **kw):
    i = next(n)
    return dict(ts=ts, block=i, log_index=0, tx='0x%064x' % i, id=M, **kw)


def take(ts, buyer, seller, units):  # a fresh lender buys from a fresh borrower: all credit and debt are new
    return ev(ts, maker=buyer, taker=seller, offer_is_buy=True, units=str(units), total_units_delta=str(units))


def log():
    market = dict(id=M, maturity=T, loan_token='0x' + '33' * 20,
                  collaterals=[dict(token=COL, lltv=str(86 * 10 ** 16), cursor=str(3 * 10 ** 17))])
    takes = [take(T - 9000, LENDER, x, 100) for x in (A, B, C, D)]
    repays = [ev(T - 3600, caller=A, on_behalf=A, units='100'), ev(T - 60, caller=ROLLING.lower(), on_behalf=B, units='100')]
    liqs = [ev(T + 900, borrower=C, collateral=COL, repaid_units='100', bad_debt='0', post_maturity=True)]
    return dict(MarketCreated=[market], Take=takes, Repay=repays, Liquidate=liqs, Withdraw=[], UpdatePosition=[], ClaimContinuousFee=[], Roll=[], SetConfig=[])


def test_replay_and_labels():
    e = log()
    pos, total, bad = replay(e)
    assert not bad and total[M] == 400 and pos[M, LENDER]['credit'] == 400
    rows = {r['user']: r for r in label(e, now=T + 7200)}
    assert set(rows) == {A, B, C, D}
    assert [rows[x]['outcome'] for x in (A, B, C, D)] == ['repaid', 'rolled', 'liquidated_late', 'open']  # no dust allowance for this token
    assert rows[C]['late_minutes'] == 15 and abs(rows[C]['late_penalty_pct'] - 4.384 / 4) < 0.01
    assert rows[A]['debt_at_maturity'] == '0' and rows[D]['debt_plus_1h'] == '100' and rows[C]['debt_at_maturity'] == '100'


def test_penalty_rule():
    m = dict(collaterals=[dict(token=COL, lltv=str(86 * 10 ** 16), cursor=str(3 * 10 ** 17))])
    assert abs(max_lif(86 * 10 ** 16, 3 * 10 ** 17) - 1.04384) < 1e-5
    assert late_penalty(m, COL, 0) == 0 and abs(late_penalty(m, COL, 7200) - 4.384) < 1e-3


def test_maturity_json():
    d = load('midnight/maturity')
    if d is None:
        pytest.skip('data/midnight/maturity.json absent')
    for day, m in d['maturities'].items():
        assert set(m['outcomes']) == set(OUTCOMES) and sum(m['outcomes'].values()) == m['positions'], day
        assert len(m['late_minutes_hist']) == len(d['late_bins_min'])
        assert 'coinbase' not in str(m).lower()  # the Coinbase split stays in private/


def test_block_at_uses_chain_timestamps(monkeypatch):
    timestamps = [100, 102, 104, 109, 111, 118]
    def fake_rpc(method, params):
        assert method == 'eth_getBlockByNumber'
        return {'timestamp': hex(timestamps[int(params[0], 16)])}
    monkeypatch.setattr(check_maturity, 'rpc', fake_rpc)
    assert check_maturity.block_at(108, 5) == 2
    assert check_maturity.block_at(109, 5) == 3
    assert check_maturity.block_at(120, 5) == 5


@pytest.mark.parametrize('code', [500, 403])
def test_blockscout_server_error_uses_rpc_fallback(monkeypatch, code):
    def fail(_req, timeout):
        raise urllib.error.HTTPError('https://base.blockscout.com', code, 'blocked', {}, BytesIO())
    monkeypatch.setattr(fetch_midnight.urllib.request, 'urlopen', fail)
    monkeypatch.setattr(fetch_midnight.time, 'sleep', lambda _seconds: None)
    with pytest.raises(fetch_midnight.Throttled):
        fetch_midnight.get_logs(fetch_midnight.MIDNIGHT, fetch_midnight.EVENTS['Take'][1], 1)


def usdc_log(*extra):
    """The log() market with USDC as the loan token, so the $1 dust threshold applies, plus extra events."""
    e = log()
    e['MarketCreated'][0]['loan_token'] = USDC
    big = [take(T - 9000, LENDER, x, 5 * 10 ** 6) for x in (E, F, G, H)]
    e['Take'] += big
    for kind, row in extra:
        e[kind].append(row)
    rows = sorted((r for items in e.values() for r in items if 'block' in r), key=lambda r: (r['ts'], r['block']))
    for b, r in enumerate(rows, 1):  # replay goes in block order; extra events were built first, so order them by time
        r['block'] = b
    return e


E, F, G, H = ('0x' + c * 40 for c in 'ef78')


def late_liq(user, minutes, units):
    return ev(T + 60 * minutes, borrower=user, collateral=COL, repaid_units=str(units), bad_debt='0', post_maturity=True)


def test_dust_remainder_is_not_open():
    e = usdc_log(
        ('Liquidate', late_liq(E, 4, 5 * 10 ** 6 - 473)),                  # leaves 473 units ($0.0005)
        ('Liquidate', late_liq(F, 1, 5 * 10 ** 6)),                        # clears it, then a zero-unit repay
        ('Repay', ev(T + 3 * 3600, caller=F, on_behalf=F, units='0')),
        ('Repay', ev(T - 60, caller=G, on_behalf=G, units=str(5 * 10 ** 6 - DUST_UNITS[USDC]))),  # leaves exactly $1
        ('Repay', ev(T - 60, caller=H, on_behalf=H, units=str(5 * 10 ** 6 - DUST_UNITS[USDC] - 1))))  # leaves $1.000001
    rows = {r['user']: r for r in label(e, now=T + 7 * 3600)}
    assert rows[E]['outcome'] == 'liquidated_late' and rows[E]['dust_remainder'] == '473' and rows[E]['minutes_after_maturity'] == 4
    assert rows[F]['outcome'] == 'liquidated_late' and rows[F]['closed_ts'] == T + 60 and rows[F]['dust_remainder'] is None
    assert rows[G]['outcome'] == 'repaid' and rows[G]['dust_remainder'] == str(DUST_UNITS[USDC])
    assert rows[H]['outcome'] == 'open' and rows[H]['dust_remainder'] is None
    assert rows[D]['outcome'] == 'dust' and rows[D]['closed_ts'] is None  # borrowed 100 units ($0.0001), never more


def test_dust_rule_needs_debt_to_stay_below_threshold():
    e = usdc_log(('Repay', ev(T - 600, caller=E, on_behalf=E, units=str(5 * 10 ** 6 - 10))),
                 ('Take', take(T - 300, LENDER, E, 5 * 10 ** 6)),  # borrows again after the dust point
                 ('Liquidate', late_liq(E, 2, 5 * 10 ** 6)))
    r = {r['user']: r for r in label(e, now=T + 7200)}[E]
    assert r['outcome'] == 'liquidated_late' and r['peak_debt'] == str(5 * 10 ** 6 + 10) and r['borrowed'] == str(10 ** 7)


def test_late_penalty_units_sum_every_late_liquidation():
    e = usdc_log(('Liquidate', late_liq(E, 15, 4 * 10 ** 6)), ('Liquidate', late_liq(E, 30, 10 ** 6)))
    r = {r['user']: r for r in label(e, now=T + 7200)}[E]
    m = e['MarketCreated'][0]
    want = 4 * 10 ** 6 * late_penalty(m, COL, 900) / 100 + 10 ** 6 * late_penalty(m, COL, 1800) / 100
    assert r['late_repaid_units'] == str(5 * 10 ** 6) and int(r['late_penalty_units']) == round(want)
    assert r['late_minutes'] == 15 and r['outcome'] == 'liquidated_late'


def test_unlisted_loan_token_gets_no_dust_allowance():
    assert dust_units('0x' + '33' * 20) == 0 and dust_units(USDC.upper().replace('0X', '0x')) == 10 ** 6


def test_rpc_retries_in_band_rate_limit(monkeypatch):
    from spine import api
    answers = iter([{'error': {'code': -32011, 'message': 'request limit reached'}}, {'result': '0x1'}])
    monkeypatch.setattr(api, '_post', lambda url, body: next(answers))
    monkeypatch.setattr(api.time, 'sleep', lambda _s: None)
    assert api.rpc('eth_blockNumber', []) == '0x1'
    monkeypatch.setattr(api, '_post', lambda url, body: {'error': {'code': -32000, 'message': 'execution reverted'}})
    with pytest.raises(RuntimeError):
        api.rpc('eth_call', [])


def test_cohort_table_splits_scope_and_counts_penalties(monkeypatch):
    from spine import cohort
    e = usdc_log(('Liquidate', late_liq(E, 4, 5 * 10 ** 6 - 473)))
    monkeypatch.setitem(cohort.SCOPE, M, 'test market')
    monkeypatch.setattr(cohort, 'symbol', lambda t: t[:6])
    rep = cohort.build(cohort.date(T), e, now=T + 7200, price_fn=lambda tokens, ts: {USDC: (1.0, 6)})
    t = rep['scopes']['coinbase_scoped']['outcomes']
    assert rep['matured'] and rep['scopes']['general']['outcomes']['all']['positions'] == 0
    assert t['liquidated_late']['positions'] == 2 and t['liquidated_late']['minutes_past_maturity']['max'] == 15
    assert t['open']['positions'] == 3 and t['open']['owed_at_maturity_usd'] == 15.0 and t['dust']['positions'] == 1  # F, G, H owe $5; D only borrowed 100 units
    assert t['all']['positions'] == 8 and t['repaid']['closed_before_maturity'] == 1
    want = (5 * 10 ** 6 - 473) * late_penalty(e['MarketCreated'][0], COL, 240) / 100 / 1e6 + 100 * late_penalty(e['MarketCreated'][0], COL, 900) / 100 / 1e6
    assert abs(t['all']['penalties_usd'] - want) < 1e-3
    assert rep['scopes']['coinbase_scoped']['owing_positions_size_hist']['$1-100'] == 4  # owing at maturity: E, F, G, H


def test_cohort_before_maturity_counts_early_exits():
    from spine import cohort
    e = usdc_log()
    cfg = lambda user, enabled, b: dict(ts=T - 9000, block=b, log_index=0, user=user, midnight_id=M, enabled=enabled)
    e['SetConfig'] = [cfg(E, True, 900), cfg(F, True, 901), cfg(F, False, 902)]  # F switched it off again
    rep = cohort.build(cohort.date(T), e, now=T - 30, price_fn=lambda tokens, ts: {USDC: (1.0, 6)})
    a = rep['scopes']['all']
    assert a['roll_configs'] == dict(positions_with_config=2, enabled=1, open_with_enabled_config=1)
    assert not rep['matured'] and a['early_exits'] == dict(repaid=1, rolled=1)
    assert a['outcomes']['open']['owed_now_usd'] == 20.0 and a['outcomes']['liquidated_late']['positions'] == 0


def test_size_hist_buckets():
    from spine.cohort import size_hist
    assert list(size_hist([0.5, 1, 99.9, 100, 2e5]).values()) == [1, 2, 1, 0, 0, 1]
# --- Blockscout and RPC read the same logs and must save identical events (no network) ---------------------------------
import json, os
from spine import api

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'base_logs.json')  # eth_getLogs results recorded from Base on 2026-10-25
RPC_LOGS = json.load(open(FIXTURE))
TIP = max(int(l['blockNumber'], 16) for l in RPC_LOGS) + 10


def blockscout_shape(log):
    """The etherscan-style getLogs row: hex timeStamp, topics padded to four with null. (Blockscout answers 403 to keyless
    scripts now, so this shape is built from the documented format rather than recorded.)"""
    return dict(address=log['address'], blockNumber=log['blockNumber'], timeStamp=log['blockTimestamp'], logIndex=log['logIndex'],
                transactionHash=log['transactionHash'], data=log['data'], topics=log['topics'] + [None] * (4 - len(log['topics'])))


def saved(tmp_path):
    out = {}
    for name in fetch_midnight.EVENTS:
        d = json.load(open(tmp_path / 'midnight' / (name + '.json')))
        assert d['count'] == len(d['items'])
        out[name] = d['items']
    return out


def run_blockscout(monkeypatch, tmp_path):
    monkeypatch.setattr(api, 'DATA', str(tmp_path))
    def get_logs(address, topic0, frm):
        return [blockscout_shape(l) for l in RPC_LOGS if l['address'].lower() == address.lower() and l['topics'][0] == topic0
                and int(l['blockNumber'], 16) >= frm]
    monkeypatch.setattr(fetch_midnight, 'get_logs', get_logs)
    for name in fetch_midnight.EVENTS:
        fetch_midnight.fetch_event(name)
    return saved(tmp_path)


def run_rpc(monkeypatch, tmp_path, step=50_000):
    monkeypatch.setattr(api, 'DATA', str(tmp_path))
    windows = []
    def get(b):
        lo, hi = b, min(b + step - 1, TIP)
        windows.append((lo, hi))
        return [l for l in RPC_LOGS if lo <= int(l['blockNumber'], 16) <= hi]
    ev = {name: [] for name in fetch_midnight.EVENTS}
    api.save('midnight/rpc_checked', {'to': min(int(l['blockNumber'], 16) for l in RPC_LOGS)})
    fetch_midnight.fill_gaps(ev, step=step, tip=TIP, get=get)
    return saved(tmp_path), windows


def test_blockscout_and_rpc_paths_save_identical_events(monkeypatch, tmp_path):
    via_blockscout = run_blockscout(monkeypatch, tmp_path / 'b')
    via_rpc, _ = run_rpc(monkeypatch, tmp_path / 'r')
    assert via_blockscout == via_rpc
    assert sum(map(len, via_rpc.values())) == len(RPC_LOGS) and len(via_rpc['Take']) and len(via_rpc['Roll'])


def test_rpc_rows_match_committed_index(monkeypatch):
    committed = {n: {fetch_midnight.key(r): r for r in (load('midnight/%s' % n) or {'items': []})['items']} for n in fetch_midnight.EVENTS}
    if not any(committed.values()):
        pytest.skip('data/midnight absent')
    names = {t: n for n, (_, t, _) in fetch_midnight.EVENTS.items()}
    for l in RPC_LOGS:
        r = fetch_midnight.row(names[l['topics'][0]], l)
        assert committed[names[l['topics'][0]]][fetch_midnight.key(r)] == r


def test_rpc_windows_are_bounded_and_contiguous(monkeypatch, tmp_path):
    windows = sorted(run_rpc(monkeypatch, tmp_path, step=50_000)[1])  # the pool reads windows concurrently
    assert all(hi - lo < 50_000 for lo, hi in windows)
    assert all(b[0] == a[1] + 1 for a, b in zip(windows, windows[1:])) and windows[-1][1] == TIP
    assert json.load(open(tmp_path / 'midnight' / 'rpc_checked.json')) == {'to': TIP + 1}


def test_rpc_rerun_is_idempotent(monkeypatch, tmp_path):
    first, _ = run_rpc(monkeypatch, tmp_path)
    api.save('midnight/rpc_checked', {'to': 0})  # force a full re-read over the saved files
    ev = {n: list(items) for n, items in first.items()}
    added = fetch_midnight.fill_gaps(ev, step=50_000, tip=TIP, get=lambda b: [l for l in RPC_LOGS if b <= int(l['blockNumber'], 16) < b + 50_000])
    assert not any(added.values()) and saved(tmp_path) == first


def test_blockscout_403_is_not_retried_and_skips_later_events(monkeypatch):
    calls = []
    def fail(_req, timeout):
        calls.append(1)
        raise urllib.error.HTTPError('https://base.blockscout.com', 403, 'forbidden', {}, BytesIO(b'challenge'))
    monkeypatch.setattr(fetch_midnight.urllib.request, 'urlopen', fail)
    monkeypatch.setattr(fetch_midnight.time, 'sleep', lambda _s: None)
    monkeypatch.setattr(fetch_midnight, 'blockscout_down', False)
    for _ in range(2):
        with pytest.raises(fetch_midnight.Throttled):
            fetch_midnight.get_logs(fetch_midnight.MIDNIGHT, fetch_midnight.EVENTS['Take'][1], 1)
    assert len(calls) == 1


def test_rpc_request_backs_off_then_falls_back_to_next_endpoint(monkeypatch):
    seen, sleeps, b_calls = [], [], []
    def post(url, body, retries):
        seen.append(url)
        if url == 'https://a':
            return {'error': {'code': -32011, 'message': 'request limit reached'}}
        b_calls.append(1)
        if len(b_calls) < 2:
            raise urllib.error.HTTPError(url, 429, 'slow down', {}, BytesIO())
        return {'result': ['ok']}
    monkeypatch.setattr(fetch_midnight, '_post', post)
    monkeypatch.setattr(fetch_midnight.time, 'sleep', sleeps.append)
    monkeypatch.setattr(fetch_midnight, '_preferred', None)
    assert fetch_midnight.rpc_logs({}, urls=['https://a', 'https://b'], tries=3) == ['ok']
    assert seen == ['https://a'] * 3 + ['https://b'] * 2 and sleeps == [1, 2, 4, 1]
    seen.clear()
    fetch_midnight.rpc_logs({}, urls=['https://a', 'https://b'], tries=3)
    assert seen == ['https://b']  # the endpoint that answered goes first
    monkeypatch.setattr(fetch_midnight, '_post', lambda *a, **k: {'error': 'nope'})
    with pytest.raises(RuntimeError):
        fetch_midnight.rpc_logs({}, urls=['https://a'], tries=2)
