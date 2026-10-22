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
