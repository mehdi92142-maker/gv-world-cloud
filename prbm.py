"""BlueMap .prbm (PRWM v1) tile decoder -> numpy, same layout as the webapp's decodePrwm()."""
import os
import numpy as np

_TYPES = {1: np.float32, 3: np.int8, 4: np.int16, 6: np.int32, 7: np.uint8, 8: np.uint16, 10: np.uint32}


def decode(buf):
    if bytes(buf[:4]) == GVC_MAGIC:
        return decode_gvc(buf)
    t = memoryview(buf)
    b = np.frombuffer(buf, np.uint8)
    version, flags = b[0], b[1]
    indexed = bool(flags >> 7 & 1)
    idx32 = flags >> 6 & 1
    big = (flags >> 5 & 1) == 1
    nattr = flags & 31
    if big:
        nverts = (int(b[2]) << 16) + (int(b[3]) << 8) + int(b[4])
        nidx = (int(b[5]) << 16) + (int(b[6]) << 8) + int(b[7])
    else:
        nverts = int(b[2]) + (int(b[3]) << 8) + (int(b[4]) << 16)
        nidx = int(b[5]) + (int(b[6]) << 8) + (int(b[7]) << 16)
    assert version == 1
    d = 8
    attrs = {}
    for _ in range(nattr):
        name = bytearray()
        while d < len(b):
            c = b[d]; d += 1
            if c == 0:
                break
            name.append(c)
        r = b[d]
        normalized = (r >> 6 & 1) == 1
        r = int(r)
        card = (r >> 4 & 3) + 1
        dt = np.dtype(_TYPES[r & 15]).newbyteorder('>' if big else '<')
        d += 1
        d = -(-d // 4) * 4
        n = card * nverts
        vals = np.frombuffer(buf, dt, n, d)
        d += dt.itemsize * n
        attrs[name.decode()] = (vals.reshape(nverts, card), normalized)
    indices = None
    if indexed:
        d = -(-d // 4) * 4
        dt = np.dtype(np.uint32 if idx32 else np.uint16).newbyteorder('>' if big else '<')
        indices = np.frombuffer(buf, dt, nidx, d)
        d += dt.itemsize * nidx
    groups = []
    d = -(-d // 4) * 4
    while d + 4 <= len(b):
        m = int(np.frombuffer(buf, '<i4', 1, d)[0])
        if m == -1:
            break
        s, c = np.frombuffer(buf, '<i4', 2, d + 4)
        groups.append((m, int(s), int(c)))
        d += 12
    return attrs, indices, groups


def tile_path(x, z):
    """webapp pathFromCoords: x=-10, z=3 -> 'x-1/0/z3'"""
    def split(n):
        s = '-' if n < 0 else ''
        return s + ''.join(ch + '/' for ch in str(abs(n)))
    return ('x' + split(x) + 'z' + split(z))[:-1]


_CODES = {np.dtype(np.float32): 1, np.dtype(np.int8): 3, np.dtype(np.int16): 4, np.dtype(np.int32): 6,
          np.dtype(np.uint8): 7, np.dtype(np.uint16): 8, np.dtype(np.uint32): 10}


def encode(attrs, groups, integer=()):
    """inverse of decode() for non-indexed little-endian tiles: attrs = {name: (array[n, card], normalized)}"""
    names = list(attrs)
    n = next(iter(attrs.values()))[0].shape[0]
    out = bytearray([1, len(names) & 31, n & 255, (n >> 8) & 255, (n >> 16) & 255, 0, 0, 0])
    for name in names:
        arr, norm = attrs[name]
        arr = np.ascontiguousarray(arr)
        card = arr.shape[1] if arr.ndim > 1 else 1
        out += name.encode() + b'\0'
        out.append(((1 if name in integer else 0) << 7) | ((1 if norm else 0) << 6) | ((card - 1) << 4)
                   | _CODES[arr.dtype.newbyteorder('=') if arr.dtype.byteorder == '>' else np.dtype(arr.dtype.type)])
        out += b'\0' * (-len(out) % 4)
        out += arr.astype(arr.dtype.newbyteorder('<')).tobytes()
    out += b'\0' * (-len(out) % 4)
    for m, s, c in groups:
        out += np.array([m, s, c], '<i4').tobytes()
    out += np.array([-1], '<i4').tobytes()
    return bytes(out)


# ---------------------------------------------------------------- GVC1: compact tile (2026-10-14), ~13x smaller than .prbm
# Same content as a decoded .prbm (non-indexed triangles, any attributes, groups) -> decode() returns the same structure.
# position quantized to 1/64 block, uv to 1/4096 (studio GPU precision); other attributes exact. Vertices deduplicated
# (index), every attribute column stored as deltas over the sorted unique vertices, everything zlib.
import json as _json
import zlib as _zlib
GVC_MAGIC = b'GVC1'
_QUANT = {'position': 64.0, 'uv': 4096.0}


def encode_gvc(attrs, groups):
    names = list(attrs)
    n = len(attrs[names[0]][0])
    cols, meta = [], []
    for k in names:
        v, norm = attrs[k]
        v = np.asarray(v)
        card = v.shape[1]
        if k in _QUANT:
            q = np.round(v.astype(np.float64) * _QUANT[k]).astype(np.int64)
        elif v.dtype == np.float32:
            q = v.view(np.int32).astype(np.int64)
        else:
            q = v.astype(np.int64)
        cols.append(q.reshape(n, card))
        meta.append([k, v.dtype.str, card, bool(norm)])
    M = np.concatenate(cols, 1) if cols else np.zeros((n, 0), np.int64)
    uniq, inv = np.unique(M, axis=0, return_inverse=True)
    parts = []
    for c in range(uniq.shape[1]):
        d = np.diff(uniq[:, c], prepend=0)
        parts.append((d.astype(np.int32) if np.abs(d).max(initial=0) < 2 ** 31 else d).astype(np.int32))
    idx = np.diff(inv.reshape(-1).astype(np.int64), prepend=0).astype(np.int32)
    head = _json.dumps({'n': int(n), 'u': int(len(uniq)), 'attrs': meta, 'groups': [list(map(int, g)) for g in groups]}).encode()
    body = _zlib.compress(b''.join(p.tobytes() for p in parts) + idx.tobytes(), 6)
    return GVC_MAGIC + len(head).to_bytes(4, 'little') + head + body


def decode_gvc(buf):
    buf = bytes(buf)
    hl = int.from_bytes(buf[4:8], 'little')
    h = _json.loads(buf[8:8 + hl])
    raw = _zlib.decompress(buf[8 + hl:])
    n, u = h['n'], h['u']
    ncol = sum(m[2] for m in h['attrs'])
    D = np.frombuffer(raw, np.int32, ncol * u).reshape(ncol, u).astype(np.int64)
    uniq = np.cumsum(D, axis=1)
    inv = np.cumsum(np.frombuffer(raw, np.int32, n, ncol * u * 4).astype(np.int64))
    attrs, c = {}, 0
    for k, dt, card, norm in h['attrs']:
        q = uniq[c:c + card].T[inv]
        c += card
        dt = np.dtype(dt)
        if k in _QUANT:
            v = (q / _QUANT[k]).astype(dt)
        elif dt == np.float32:
            v = q.astype(np.int32).view(np.float32)
        else:
            v = q.astype(dt)
        attrs[k] = (np.ascontiguousarray(v), norm)
    return attrs, None, [tuple(g) for g in h['groups']]


def find_tile(base):
    """tile path without extension -> existing .gvc or .prbm file (or None)"""
    for ext in ('.gvc', '.prbm'):
        if os.path.exists(base + ext):
            return base + ext
    return None
