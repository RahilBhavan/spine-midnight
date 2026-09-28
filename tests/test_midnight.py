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
