"""Outcome card spec and the held-until-matured rerun guard (offline)."""
import json
import os
import pytest
from spine import outcome_card, rerun_maturity
from spine.api import load
from spine.midnight import date

T = 1_800_000_000
D = date(T)


def mk(*times):
    return dict(MarketCreated=[dict(maturity=t) for t in times])


def test_ready_at_is_last_market_plus_one_hour():
    assert rerun_maturity.ready_at(mk(T, T + 600, T + 86400 * 5), D) == T + 600 + 3600
    with pytest.raises(SystemExit):
        rerun_maturity.ready_at(mk(T + 86400 * 5), D)


def test_held_date_stops_before_any_command(monkeypatch):
    monkeypatch.setattr(rerun_maturity, 'events', lambda: mk(T))
    monkeypatch.setattr(rerun_maturity.subprocess, 'run', lambda *a, **k: pytest.fail('ran a command while held'))
    assert rerun_maturity.run(D, now=T + 3599) == rerun_maturity.HELD_EXIT
    assert rerun_maturity.run(D, status_only=True, now=T + 3600) == 0


def test_ready_date_runs_fetch_label_check_in_order_and_stops_on_failure(monkeypatch):
    monkeypatch.setattr(rerun_maturity, 'events', lambda: mk(T))
    monkeypatch.setattr(rerun_maturity, 'load', lambda name: {'maturities': {D: {'matured': True}}})
    ran = []

    def fake(cmd, cwd):
        ran.append(cmd[2:])
        return type('R', (), {'returncode': 1 if cmd[2] == 'spine.midnight' else 0})()
    monkeypatch.setattr(rerun_maturity.subprocess, 'run', fake)
    assert rerun_maturity.run(D, now=T + 3600) == 1
    assert ran == [['spine.fetch_midnight'], ['spine.midnight', 'label', D]]  # check_maturity never ran
    ran.clear()
    monkeypatch.setattr(rerun_maturity.subprocess, 'run', lambda cmd, cwd: (ran.append(cmd[2:]), type('R', (), {'returncode': 0})())[1])
    assert rerun_maturity.run(D, now=T + 3600) == 0
    assert ran == [['spine.fetch_midnight'], ['spine.midnight', 'label', D], ['spine.check_maturity', D]]


def test_unmatured_aggregate_after_refresh_is_still_held(monkeypatch):
    monkeypatch.setattr(rerun_maturity, 'events', lambda: mk(T))
    monkeypatch.setattr(rerun_maturity, 'load', lambda name: {'maturities': {D: {'matured': False}}})
    ran = []
    monkeypatch.setattr(rerun_maturity.subprocess, 'run', lambda cmd, cwd: (ran.append(cmd[2:]), type('R', (), {'returncode': 0})())[1])
    assert rerun_maturity.run(D, now=T + 3600) == rerun_maturity.HELD_EXIT
    assert ['spine.check_maturity', D] not in ran


def test_oct_30_is_held_in_the_committed_aggregate():
    agg = load('midnight/maturity')['maturities']
    assert agg['2026-10-30']['matured'] is False
    with pytest.raises(SystemExit, match='held'):
        outcome_card.card_spec(load('midnight/maturity'), '2026-10-30', 1, 1)


def committed_spec():
    return outcome_card.card_spec(load('midnight/maturity'), '2026-09-25', load('midnight/rpc_checked')['to'],
                                  load('midnight/Take')['fetched_at'])


def test_sep_25_card_counts_match_the_aggregate():
    spec = committed_spec()
    o = load('midnight/maturity')['maturities']['2026-09-25']
    rows = {r['label']: r['value'] for r in spec['rows']}
    assert rows == {'Repaid': o['outcomes']['repaid'], 'Rolled': o['outcomes']['rolled'],
                    'Liquidated': o['outcomes']['liquidated'] + o['outcomes']['liquidated_late'], 'Open': o['outcomes']['open']}
    assert sum(rows.values()) == o['positions'] == spec['max']
    hi = [r for r in spec['rows'] if r.get('highlight')]
    assert len(hi) == 1 and hi[0]['text'] in spec['title']
    assert '\n' not in spec['alt'] and str(rows['Repaid']) in spec['alt']
    assert '53,074,299' in spec['source'] and '25 Oct 2026' in spec['source']


def test_committed_spec_is_current():
    path = os.path.join(outcome_card.OUT, '2026-09-25', 'spec.json')
    assert json.load(open(path)) == committed_spec()
