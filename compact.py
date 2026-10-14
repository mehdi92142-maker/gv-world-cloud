"""compact.py -- one .prbm -> .gvc with the same round-trip check as tools/compact_tiles.py (cloud copy)."""
import numpy as np
import prbm


def convert(src, dst):
    raw = open(src, 'rb').read()
    a, _, g = prbm.decode(raw)
    enc = prbm.encode_gvc(a, g)
    b, _, g2 = prbm.decode(enc)
    if g != g2 or list(a) != list(b):
        raise RuntimeError(f'{src}: groups/attrs differ after compaction')
    for k in a:
        x, y = a[k][0], b[k][0]
        lim = 1 / 128 + 1e-6 if k == 'position' else 1 / 8192 + 1e-6 if k == 'uv' else 0
        if x.shape != y.shape or np.abs(x.astype(np.float64) - y.astype(np.float64)).max(initial=0) > lim:
            raise RuntimeError(f'{src}: {k} differs after compaction')
    open(dst, 'wb').write(enc)
