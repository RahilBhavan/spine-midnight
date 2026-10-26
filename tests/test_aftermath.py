"""spine.aftermath on synthetic rows (offline): re-borrow counting and onchain-liquidity coverage."""
from spine.aftermath import fisher_two_sided, observed_lif, ramp_fit, liquidity, midnight_borrows, reborrow
from spine.midnight import replay
from tests.test_midnight import A, B, C, D, LENDER, M, T, log, take

E = '0x' + 'e' * 40


def rows():
    return [dict(user=A, outcome='repaid'), dict(user=B, outcome='rolled'), dict(user=C, outcome='liquidated_late'),
            dict(user=E, outcome='liquidated_late'), dict(user=D, outcome='open')]


def test_reborrow_counts_only_after_maturity_and_dedupes_sources():
    mid = {A: [T - 100, T + 50], C: [T - 9000]}            # A borrows again after T; C's only borrow is before T
    blue = {A: [T + 10], B: [T], E: [T + 3600]}             # B's Blue borrow at exactly T does not count
    out = reborrow(rows(), mid, blue, T)
    r, l = out['repaid_or_rolled_before'], out['liquidated_after_maturity']
    assert (r['positions'], r['addresses'], r['reborrowed_midnight'], r['reborrowed_blue'], r['reborrowed_any']) == (2, 2, 1, 1, 1)
    assert r['reborrow_rate'] == 0.5
    assert (l['addresses'], l['reborrowed_midnight'], l['reborrowed_blue'], l['reborrowed_any']) == (2, 0, 1, 1)


def test_midnight_borrows_from_replay():
    e = log()
    e['Take'].append(take(T - 500, LENDER, A, 50))           # A borrows a second time
    pos, _, _ = replay(e)
    got = midnight_borrows(pos)
    assert sorted(got[A]) == [T - 9000, T - 500] and got[C] == [T - 9000] and got.get(LENDER, []) == []


def test_liquidity_coverage():
    owing = [(A, 5 * 10 ** 6), (B, 10 ** 5), (C, 2 * 10 ** 6)]  # B owes $0.10, below the $1 line
    at_T = {A: (6 * 10 ** 6, 0), B: (0, 10), C: (10 ** 6, 0)}
    before = {A: (0, 0), B: (10 ** 5, 0), C: (3 * 10 ** 6, 0)}
    out = liquidity(owing, at_T, before)
    assert out['owing_positions'] == 3 and out['owing_above_1usd'] == 2
    assert out['usdc_covers_debt_at_maturity'] == 1 and out['usdc_covers_debt_24h_before'] == 2
    assert out['usdc_covers_debt_at_maturity_above_1usd'] == 1 and out['usdc_covers_debt_24h_before_above_1usd'] == 1
    assert out['covered_debts_usd_at_maturity_above_1usd'] == [5.0]
    assert out['any_usdc_at_maturity'] == 2 and out['any_cbbtc_at_maturity'] == 1 and out['debt_usd_total'] == 7.1


def test_fisher_two_sided():
    assert abs(fisher_two_sided(3, 1, 1, 3) - 0.4857) < 1e-3      # textbook tea-tasting table
    assert fisher_two_sided(5, 5, 5, 5) == 1.0
    assert fisher_two_sided(10, 0, 0, 10) < 1e-4


def test_observed_lif_and_ramp_fit():
    # 0.0001 cbBTC (10**4 sats) at an oracle price of $100,000 (1e36 * 1e6 / 1e8 scale) for 9.9 USDC repaid -> LIF 1.0101
    px = 100_000 * 10 ** 36 * 10 ** 6 // 10 ** 8
    assert abs(observed_lif(10 ** 4, 9_900_000, px) - 10 / 9.9) < 1e-9
    m = 1.04384
    on_60 = [(t, 1 + (m - 1) * t / 60) for t in (3, 6, 30)]       # points exactly on the 60-minute ramp
    fit = ramp_fit(on_60, m)
    assert fit[60] < 1e-9 < fit[15]
