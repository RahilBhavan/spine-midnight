"""Maturity outcome card: one matured date from data/midnight/maturity.json as a phone-first bar card.
.venv/bin/python -m spine.outcome_card YYYY-MM-DD                 writes docs/outcome-card/<date>/spec.json (stdlib only)
uv run --with playwright==1.63.0 --with fonttools --with pillow --with numpy \\
    python -m spine.outcome_card YYYY-MM-DD --render              also renders and checks the card

The spec follows Rahil Design's social_bars contract (rahil-design, `RAHIL_DESIGN_SKILL`, default
~/.t3/projects/main/.agents/skills/rahil-design). The card is authored at 390px and exported at 1080x1080; feed-360.png and
feed-390.png are that export averaged down to the width of a phone feed (BOX filter: Lanczos overshoots into off-palette pixels). A date that has not matured is refused. Rendering
checks a draft; it does not approve it."""
import datetime, hashlib, json, os, subprocess, sys
from spine.api import DATA, load
from spine.fetch_midnight import FROM_BLOCK

SKILL = os.environ.get('RAHIL_DESIGN_SKILL') or os.path.expanduser('~/.t3/projects/main/.agents/skills/rahil-design')
OUT = os.path.join(os.path.dirname(DATA), 'docs', 'outcome-card')
SIZE, EXPORT, FEEDS = 390, 1080, (360, 390)
LABEL_COLUMN = '124px'  # "Liquidated" at 22px needs more than social_bars' 96px


def fmt(n):
    return '{:,}'.format(n)


def card_spec(agg, d, last_block, fetched_at):
    """Four outcome rows for a matured date; the largest row is highlighted and named by the title."""
    a = agg['maturities'][d]
    if not a['matured']:
        raise SystemExit('%s has not matured; its card is held' % d)
    o = a['outcomes']
    counts = [('Repaid', o['repaid']), ('Rolled', o['rolled']), ('Liquidated', o['liquidated'] + o['liquidated_late']), ('Open', o['open'])]
    assert sum(n for _, n in counts) == a['positions'], 'outcomes do not add up to positions'
    top = max(n for _, n in counts)
    day = datetime.date.fromisoformat(d)
    long = '%d %s %d' % (day.day, day.strftime('%b'), day.year)
    seen = datetime.datetime.fromtimestamp(fetched_at, datetime.timezone.utc).strftime('%d %b %Y').lstrip('0')
    late = o['liquidated_late']
    return dict(
        id='midnight-%s-outcomes' % d,
        title='%d of %d repaid' % (o['repaid'], a['positions']),
        axis='Positions, %s maturity' % long,
        rows=[dict(label=k, value=n, text=str(n), **({'highlight': True} if n == top else {})) for k, n in counts],
        max=a['positions'],
        source='Source: Midnight events, Base blocks %s–%s. Data %s.' % (fmt(FROM_BLOCK), fmt(last_block), seen),
        alt=('Bar card titled "%d of %d repaid". Of %d Morpho Midnight borrow positions on Base that matured on %s, '
             '%d were repaid (highlighted), %d rolled into Morpho Blue, %d were liquidated (all after maturity) and %d are still open. '
             'Bars share one scale from zero to %d. Source: Midnight events on Base, blocks %s to %s, data as of %s.'
             % (o['repaid'], a['positions'], a['positions'], long, o['repaid'], o['rolled'], o['liquidated'] + late, o['open'],
                a['positions'], fmt(FROM_BLOCK), fmt(last_block), seen)))


def render(folder):
    """Render spec.json with social_bars at 1080x1080, then write the feed images and the check log."""
    sys.path.insert(0, os.path.join(SKILL, 'scripts'))
    import social_bars as sb
    from pathlib import Path
    from PIL import Image
    from playwright.sync_api import sync_playwright
    stock = sb.card_html
    sb.card_html = lambda spec: stock(spec).replace('96px 1fr', LABEL_COLUMN + ' 1fr')

    def render_export(html_path, png_path):
        with sync_playwright() as p:
            browser = p.firefox.launch(headless=True, executable_path=str(sb.FIREFOX))
            page = browser.new_page(viewport={'width': SIZE, 'height': SIZE}, device_scale_factor=EXPORT / SIZE)
            page.goto(Path(html_path).resolve().as_uri())
            page.evaluate('document.fonts.ready')
            page.wait_for_timeout(300)
            measured = page.evaluate(sb.MEASURE)
            page.screenshot(path=str(png_path))
            browser.close()
        return measured

    sb.render = render_export
    sys.argv = ['social_bars', os.path.join(folder, 'spec.json'), folder]
    failed = sb.main()
    brief_path = os.path.join(folder, 'brief.json')
    brief = json.load(open(brief_path))
    brief['sizes_checked'] = ['%dx%d' % (SIZE, SIZE)]  # where the text was measured
    brief['feed_images'] = ['feed-%d.png' % w for w in FEEDS]
    brief['export'] = '%dx%d PNG, authored at %dpx and scaled %.4fx' % (EXPORT, EXPORT, SIZE, EXPORT / SIZE)
    card = Image.open(os.path.join(folder, 'card.png'))
    assert card.size == (EXPORT, EXPORT), card.size
    for w in FEEDS:
        card.resize((w, w), Image.BOX).save(os.path.join(folder, 'feed-%d.png' % w))
    brief['artifacts'] = {n: hashlib.sha256(open(os.path.join(folder, n), 'rb').read()).hexdigest()
                          for n in ('card.html', 'card.png', 'feed-360.png', 'feed-390.png')}
    json.dump(brief, open(brief_path, 'w'), indent=2)
    open(brief_path, 'a').write('\n')
    return failed


if __name__ == '__main__':
    args = sys.argv[1:]
    dates = [a for a in args if not a.startswith('--')]
    if len(dates) != 1 or set(args) - set(dates) - {'--render'}:
        raise SystemExit(__doc__)
    d = dates[0]
    folder = os.path.join(OUT, d)
    os.makedirs(folder, exist_ok=True)
    spec = card_spec(load('midnight/maturity'), d, load('midnight/rpc_checked')['to'], load('midnight/Take')['fetched_at'])
    json.dump(spec, open(os.path.join(folder, 'spec.json'), 'w'), indent=2, ensure_ascii=False)
    open(os.path.join(folder, 'spec.json'), 'a').write('\n')
    print('wrote', os.path.join(folder, 'spec.json'))
    if '--render' in args:
        sys.exit(render(folder))
