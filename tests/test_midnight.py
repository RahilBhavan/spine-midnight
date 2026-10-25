"""Midnight replay and labels on a synthetic event log (offline), plus a shape check of the committed aggregate."""
import pytest
import urllib.error
from io import BytesIO
from spine.api import load
from spine import fetch_midnight
from spine.fetch_midnight import ROLLING
from spine.midnight import OUTCOMES, label, late_penalty, max_lif, replay
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
    assert [rows[x]['outcome'] for x in (A, B, C, D)] == ['repaid', 'rolled', 'liquidated_late', 'open']
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


def test_blockscout_server_error_uses_rpc_fallback(monkeypatch):
    def fail(_req, timeout):
        raise urllib.error.HTTPError('https://base.blockscout.com', 500, 'server error', {}, BytesIO())
    monkeypatch.setattr(fetch_midnight.urllib.request, 'urlopen', fail)
    monkeypatch.setattr(fetch_midnight.time, 'sleep', lambda _seconds: None)
    with pytest.raises(fetch_midnight.Throttled):
        fetch_midnight.get_logs(fetch_midnight.MIDNIGHT, fetch_midnight.EVENTS['Take'][1], 1)


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
