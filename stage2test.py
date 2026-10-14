"""stage2test.py -- run GvCull + BlueMap for region (0,0) of seed2k in the cloud and write digests to compare with the PC.
Inputs in this repo branch: world/ (9 original regions -1..1 + level.dat), GvCull.java, bluemap-cli.jar, bmcfg/, prbm.py.
Output: result.json (cull region per-chunk block hashes + interior tile geometry digests), pushed back to this branch.
"""
import hashlib, io, json, math, os, shutil, subprocess, sys, time, zlib, glob

R = os.getcwd()
W = '/home/user/s2'
T0 = time.time()


def sh(c):
    print('+', c, flush=True)
    return subprocess.run(c, shell=True, check=True, capture_output=True, text=True).stdout


def log(*a):
    print(f'[{time.time() - T0:6.0f}s]', *a, flush=True)


os.makedirs(W, exist_ok=True)
jdk = W + '/jdk'
if not os.path.exists(jdk + '/bin/javac'):
    sh(f'curl -sSLf -m 600 -o {W}/jdk.tgz https://api.adoptium.net/v3/binary/latest/25/ga/linux/x64/jdk/hotspot/normal/eclipse')
    os.makedirs(jdk, exist_ok=True)
    sh(f'tar xzf {W}/jdk.tgz -C {jdk} --strip-components=1')
J = jdk + '/bin/java'
try:
    import nbtlib, numpy  # noqa: F401
except ImportError:
    sh(f'{sys.executable} -m pip install -q nbtlib numpy')
import numpy as np
import nbtlib

# 1. GvCull, exactly as the PC (extend_world): input = original world, region box 0..511, output = culled copy
shutil.rmtree(W + '/build', ignore_errors=True); os.makedirs(W + '/build')
sh(f'{jdk}/bin/javac -d {W}/build {R}/GvCull.java')
shutil.rmtree(W + '/wcull', ignore_errors=True)
t = time.time()
sh(f'{J} -Xmx3G -cp {W}/build GvCull {R}/world {W}/wcull 0 0 511 511')
log(f'GvCull region 0,0 done in {time.time() - t:.0f}s')

# 2. BlueMap (same jar + config as the PC), map 'test' = the culled world, render mask = region 0,0
cfg = W + '/bmcfg'
shutil.rmtree(cfg, ignore_errors=True); shutil.copytree(R + '/bmcfg', cfg)
conf = open(cfg + '/maps/seed2k_cull.conf').read()
import re
conf = re.sub(r'(?m)^world: .*$', f'world: "{W}/wcull"', conf)
conf = re.sub(r'(?ms)^render-mask: \[.*?^\]', 'render-mask: [\n  { min-x: 0, max-x: 511, min-z: 0, max-z: 511 }\n]', conf)
for f in glob.glob(cfg + '/maps/*.conf'):
    os.remove(f)
open(cfg + '/maps/test.conf', 'w').write(conf)
shutil.rmtree(W + '/bmrun', ignore_errors=True); os.makedirs(W + '/bmrun')
shutil.copy(R + '/bluemap-cli.jar', W + '/bmrun/')
shutil.move(cfg, W + '/bmrun/bluemap_config')
t = time.time()
r = subprocess.run(f'cd {W}/bmrun && {J} -jar bluemap-cli.jar -c bluemap_config -r -f -m test', shell=True, capture_output=True, text=True)
log(f'BlueMap exit {r.returncode} in {time.time() - t:.0f}s', r.stdout[-1500:], r.stderr[-800:])

# 3. digests
sys.path.insert(0, R)
import prbm
res = {'cull_chunks': {}, 'tiles': {}}
buf = open(W + '/wcull/dimensions/minecraft/overworld/region/r.0.0.mca', 'rb').read()
res['cull_region_sha1'] = hashlib.sha1(buf).hexdigest()


def state_name(e):
    n = str(e['Name']).replace('minecraft:', '')
    p = e.get('Properties')
    if p:
        n += '[' + ','.join(f'{k}={str(v)}' for k, v in sorted(p.items())) + ']'
    return 'air' if n in ('cave_air', 'void_air') else n


for i in range(1024):
    off = int.from_bytes(buf[i * 4:i * 4 + 3], 'big') * 4096
    ln = int.from_bytes(buf[off:off + 4], 'big')
    c = nbtlib.File.parse(io.BytesIO(zlib.decompress(buf[off + 5:off + 4 + ln])))
    secs = {int(s['Y']): s for s in c['sections']}
    hs = []
    for Y in range(-4, 20):
        s = secs.get(Y)
        if s is None or 'block_states' not in s:
            names = ['air'] * 4096
        else:
            bs = s['block_states']; pal = [state_name(e) for e in bs['palette']]
            if 'data' in bs:
                bits = max(4, math.ceil(math.log2(len(pal)))); per = 64 // bits
                v = np.asarray(bs['data'], np.int64).view(np.uint64); shv = np.arange(per, dtype=np.uint64) * np.uint64(bits)
                idx = ((v[:, None] >> shv) & np.uint64((1 << bits) - 1)).reshape(-1)[:4096].astype(int)
            else:
                idx = np.zeros(4096, int)
            names = [pal[k] for k in idx]
        hs.append(hashlib.sha1('\n'.join(names).encode()).hexdigest()[:16])
    res['cull_chunks'][f'{i % 32},{i // 32}'] = hashlib.sha1(' '.join(hs).encode()).hexdigest()[:12]

tj = json.load(open(W + '/bmrun/bluemap_config/../maps/test/textures.json' if os.path.exists(W + '/bmrun/maps/test/textures.json') else W + '/bmrun/bluemap_config/textures.json'))
rp = [x['resourcePath'] for x in tj]


def digest(path):
    A, idx, groups = prbm.decode(open(path, 'rb').read())
    n = len(A['position'][0])
    mat = np.zeros(n, np.int64)
    for m, s, cc in groups:
        mat[s:s + cc] = m
    P = np.round(A['position'][0].astype(np.float64) * 64).astype(np.int64)
    UV = np.round(A['uv'][0].astype(np.float64) * 4096).astype(np.int64)
    rows = [f'{rp[mat[k]]}|{P[k, 0]},{P[k, 1]},{P[k, 2]}|{UV[k, 0]},{UV[k, 1]}|{",".join(map(str, A["normal"][0][k]))}|{",".join(map(str, A["color"][0][k]))}|{int(A["ao"][0][k, 0])}|{int(A["sunlight"][0][k, 0])}'
            for k in range(n)]
    tris = sorted('/'.join(rows[k:k + 3]) for k in range(0, n - n % 3, 3))
    return hashlib.sha1('\n'.join(tris).encode()).hexdigest()[:16], n // 3


for tx in range(1, 15):
    for tz in range(1, 15):
        p = prbm.find_tile(f'{W}/bmrun/maps/test/tiles/0/' + prbm.tile_path(tx, tz))
        res['tiles'][f'{tx},{tz}'] = digest(p) if p else None
log('tiles digested:', sum(1 for v in res['tiles'].values() if v))
json.dump(res, open(R + '/result.json', 'w'))
sh(f'cd {R} && git add result.json && git commit -qm "stage2 test result" && git push -q origin HEAD:stage2-test')
log('RESULT PUSHED')
