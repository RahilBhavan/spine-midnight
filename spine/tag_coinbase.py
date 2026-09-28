"""Tag every borrower in data/positions_*.json and data/liquidations_*.json as a Coinbase Smart Wallet.
A Coinbase Smart Wallet is an ERC-1967 proxy whose implementation() (slot 0x3608...2bbc) is the v1.0 or
v1.1 impl. We ask Multicall3 to call implementation() on 1000 addresses per eth_call (~1s); any chunk
that fails falls back to eth_getStorageAt per address. Cache: data/coinbase_wallets.json
{addr_lowercase: "v1.0"|"v1.1"|null}. Run: .venv/bin/python -m spine.tag_coinbase"""
import glob, json, os, time
from spine.api import rpc, MARKETS, DATA, load, save

SLOT = '0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc'
IMPLS = {'000100abaad02f1cfc8bbe32bd5a564817339e72': 'v1.0', '00000110dcdedc9581cb5ecb8467282f2926534d': 'v1.1'}
MULTICALL3 = '0xcA11bde05977b3631167028862bE2a173976CA11'
CHUNK = 1000


def borrowers():
    out = set()
    for p in glob.glob(os.path.join(DATA, 'positions_*.json')):
        out.update(x['user'].lower() for x in json.load(open(p))['positions'])
    for p in glob.glob(os.path.join(DATA, 'liquidations_*.json')):
        out.update(x['borrower'].lower() for x in json.load(open(p))['items'])
    return sorted(out)


def version(hexdata):
    return IMPLS.get(hexdata[-40:].lower()) if len(hexdata) >= 40 else None


def word(n):
    return '%064x' % n


def encode_aggregate3(addrs):
    """aggregate3((address target, bool allowFailure, bytes callData)[]) with callData = implementation()."""
    item = lambda a: word(int(a, 16)) + word(1) + word(96) + word(4) + '5c60da1b' + '0' * 56
    items = [item(a) for a in addrs]
    offsets = ''.join(word(len(addrs) * 32 + i * len(items[0]) // 2) for i in range(len(addrs)))
    return '0x82ad56cb' + word(32) + word(len(addrs)) + offsets + ''.join(items)


def decode_aggregate3(hexdata):
    """Returns the returnData hex of each (success, returnData) tuple; '' on failure or empty."""
    d = bytes.fromhex(hexdata[2:])
    w = lambda i: int.from_bytes(d[i:i + 32], 'big')
    arr = w(0)
    out = []
    for k in range(w(arr)):
        off = arr + 32 + w(arr + 32 + k * 32)
        roff = off + w(off + 32)
        out.append(d[roff + 32:roff + 32 + w(roff)].hex() if w(off) else '')
    return out


def tag_chunk(addrs):
    try:
        vals = decode_aggregate3(rpc('eth_call', [{'to': MULTICALL3, 'data': encode_aggregate3(addrs)}, 'latest']))
        assert len(vals) == len(addrs)
    except Exception:  # multicall failed: read the slot one address at a time
        vals = [rpc('eth_getStorageAt', [a, SLOT, 'latest']) for a in addrs]
    return dict(zip(addrs, map(version, vals)))


def tag(addrs, cache):
    todo = [a for a in addrs if a not in cache]
    for i in range(0, len(todo), CHUNK):
        cache.update(tag_chunk(todo[i:i + CHUNK]))
        save('coinbase_wallets', cache)
    return cache


def report(cache):
    for name in MARKETS:
        raw = load('positions_%s' % name)
        if not raw:
            continue
        pos = raw['positions']
        cb = [x for x in pos if cache.get(x['user'].lower())]
        usd, cb_usd = sum(x['borrow_usd'] for x in pos), sum(x['borrow_usd'] for x in cb)
        print('%-8s positions %6d  coinbase %6d (%5.1f%%)  borrow_usd %14.0f  coinbase %14.0f (%5.1f%%)' % (
            name, len(pos), len(cb), 100 * len(cb) / max(len(pos), 1), usd, cb_usd, 100 * cb_usd / max(usd, 1)))


if __name__ == '__main__':
    t = time.time()
    cache = load('coinbase_wallets') or {}
    addrs = borrowers()
    known = ['0x74459ea7df673cfd90afbe39f635ace08ccb97c4', '0xd3d7900a30f4016bc9945f7f2bf3a028fe9307fc']
    print('%d borrowers, %d cached' % (len(addrs), sum(a in cache for a in addrs)))
    tag(addrs + known, cache)
    print('tagged in %.0fs, %d coinbase of %d' % (time.time() - t, sum(1 for a in addrs if cache.get(a)), len(addrs)))
    assert cache[known[0]] == 'v1.1' and cache[known[1]] == 'v1.1', [cache[k] for k in known]
    # storage slot must agree with implementation() on a sample
    for a in known + addrs[:3]:
        assert version(rpc('eth_getStorageAt', [a, SLOT, 'latest'])) == cache[a], a
    report(cache)
    print('ok')
