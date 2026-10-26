"""Rerun the maturity report for one date, but only once it has matured.
.venv/bin/python -m spine.rerun_maturity YYYY-MM-DD [--status]

A date is HELD until its last market matures plus one hour, the same cut-off spine.check_maturity uses for the
maturity + 1h debt check. HELD exits 3 before any network call. Once READY it runs, in order, and stops at the first
failure: spine.fetch_midnight, spine.midnight label <date>, spine.check_maturity <date>. If the refreshed aggregate
still says the date has not matured, it exits 3 as well. --status only prints the state (exit 0 READY, 3 HELD)."""
import subprocess, sys, time
from spine.api import load
from spine.midnight import REPO, date, events

HELD_EXIT = 3
GRACE = 3600


def ready_at(ev, d):
    """Unix time after which date d can be reported: its last market's maturity plus one hour."""
    times = [m['maturity'] for m in ev['MarketCreated'] if date(m['maturity']) == d]
    if not times:
        raise SystemExit('no indexed market matures on %s' % d)
    return max(times) + GRACE


def stamp(ts):
    return time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(ts))


def run(d, status_only=False, now=None):
    t = ready_at(events(), d)
    if (now or time.time()) < t:
        print('HELD %s: not matured yet; rerun after %s' % (d, stamp(t)))
        return HELD_EXIT
    print('READY %s: matured, reporting allowed since %s' % (d, stamp(t)))
    if status_only:
        return 0
    for step in (['spine.fetch_midnight'], ['spine.midnight', 'label', d]):
        done = subprocess.run([sys.executable, '-m', *step], cwd=REPO)
        if done.returncode:
            return done.returncode
    if not load('midnight/maturity')['maturities'][d]['matured']:
        print('HELD %s: the refreshed aggregate still has unmatured markets' % d)
        return HELD_EXIT
    return subprocess.run([sys.executable, '-m', 'spine.check_maturity', d], cwd=REPO).returncode


if __name__ == '__main__':
    args = sys.argv[1:]
    dates = [a for a in args if not a.startswith('--')]
    if len(dates) != 1 or set(args) - set(dates) - {'--status'}:
        raise SystemExit(__doc__)
    sys.exit(run(dates[0], status_only='--status' in args))
