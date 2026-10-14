"""cloudstage2.py -- stage 2 in the cloud for one block of target regions: GvCull + BlueMap + GVC1 compaction, exactly the
PC pipeline (tested 100 % identical on region 0,0). Run inside a checkout of branch s2in-NN which contains:
  world/dimensions/minecraft/overworld/region/r.X.Z.mca   original regions: the targets + their 8-neighbour ring
  world/level.dat, targets.txt ("rx,rz" per line), GvCull.java, bluemap-cli.jar, bmcfg/, prbm.py, compact.py
Output (pushed to branch s2out-NN, then the input branch is left alone):
  cull/r.X.Z.mca             culled targets
  tiles/<tile path>.gvc      every hires tile OWNED by a target (owner region = floor((t*32 + 2) / 512) on both axes)
  textures_paths.json        resourcePath order of this BlueMap run (the PC remaps material ids if it differs)
  manifest.txt               "<path> <bytes> <sha1>" for every file
"""
import glob, hashlib, json, os, re, shutil, subprocess, sys, time

R = os.getcwd()
OUT_BRANCH = sys.argv[1]
W = '/home/user/s2'
T0 = time.time()


def sh(c):
    r = subprocess.run(c, shell=True, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f'{c[:120]} -> {r.returncode}: {r.stderr[-800:]} {r.stdout[-800:]}')
    return r.stdout


def log(*a):
    print(f'[{time.time() - T0:6.0f}s]', *a, flush=True)


targets = [tuple(map(int, a.split(','))) for a in sys.argv[2:]]
# inputs: the original regions (targets + 8-neighbour ring) from the branches orig-1..3 (uploaded once from the PC)
WORLD = W + '/world'
src = WORLD + '/dimensions/minecraft/overworld/region'
os.makedirs(src, exist_ok=True)
shutil.copy(R + '/level.dat', WORLD + '/level.dat')
avail = {}
for b in ('orig-1', 'orig-2', 'orig-3'):
    sh(f'cd {R} && git fetch -q origin {b}:refs/remotes/origin/{b}')
    for f in sh(f'cd {R} && git ls-tree --name-only refs/remotes/origin/{b} region/').split():
        avail[os.path.basename(f)] = b
need = {(x + dx, z + dz) for x, z in targets for dx in (-1, 0, 1) for dz in (-1, 0, 1)}
have = set()
for x, z in sorted(need):
    name = f'r.{x}.{z}.mca'
    if name in avail:
        sh(f'cd {R} && git show refs/remotes/origin/{avail[name]}:region/{name} > {src}/{name}')
        have.add((x, z))
missing_t = [q for q in targets if q not in have]
if missing_t:
    raise RuntimeError(f'targets missing in orig branches: {missing_t}')
log('targets', len(targets), 'input regions', len(have))

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
sh(f'mkdir -p {W}/build && {jdk}/bin/javac -d {W}/build {R}/GvCull.java')

# 1. GvCull for the targets AND the ring (the ring's culled cells near the targets are what BlueMap needs at the edges;
#    its far side lacks neighbours and is never used: only target-owned tiles are kept)
cullw = W + '/wcull'
shutil.rmtree(cullw, ignore_errors=True)
os.makedirs(cullw + '/dimensions/minecraft/overworld/region')
shutil.copy(WORLD + '/level.dat', cullw + '/level.dat')
from concurrent.futures import ThreadPoolExecutor


def cull_one(q):
    rx, rz = q
    tmp = f'{W}/c_{rx}_{rz}'
    shutil.rmtree(tmp, ignore_errors=True)
    sh(f'{J} -Xmx3G -cp {W}/build GvCull {WORLD} {tmp} {rx * 512} {rz * 512} {rx * 512 + 511} {rz * 512 + 511}')
    os.replace(f'{tmp}/dimensions/minecraft/overworld/region/r.{rx}.{rz}.mca', f'{cullw}/dimensions/minecraft/overworld/region/r.{rx}.{rz}.mca')
    shutil.rmtree(tmp, ignore_errors=True)
    return q


t = time.time()
with ThreadPoolExecutor(3) as ex:
    list(ex.map(cull_one, sorted(have)))
log(f'GvCull {len(have)} regions in {time.time() - t:.0f}s')

# 2. BlueMap on the culled block, mask = target boxes grown by one tile (so owned edge tiles are complete)
run = W + '/bm'
shutil.rmtree(run, ignore_errors=True); os.makedirs(run)
shutil.copy(R + '/bluemap-cli.jar', run)
shutil.copytree(R + '/bmcfg', run + '/bluemap_config')
conf = open(run + '/bluemap_config/maps/seed2k_cull.conf').read()
conf = re.sub(r'(?m)^world: .*$', f'world: "{cullw}"', conf)
mask = ',\n'.join(f'  {{ min-x: {rx * 512 - 34}, max-x: {rx * 512 + 545}, min-z: {rz * 512 - 34}, max-z: {rz * 512 + 545} }}' for rx, rz in targets)
conf = re.sub(r'(?ms)^render-mask: \[.*?^\]', f'render-mask: [\n{mask}\n]', conf)
for f in glob.glob(run + '/bluemap_config/maps/*.conf'):
    os.remove(f)
open(run + '/bluemap_config/maps/s2.conf', 'w').write(conf)
t = time.time()
sh(f'cd {run} && {J} -Xmx6G -jar bluemap-cli.jar -c bluemap_config -r -f -m s2')
log(f'BlueMap in {time.time() - t:.0f}s')

# 3. keep owned tiles, compact to GVC1 (round-trip checked), write outputs
sys.path.insert(0, R)
import prbm
from compact import convert
out = W + '/out'
shutil.rmtree(out, ignore_errors=True); os.makedirs(out + '/cull'); os.makedirs(out + '/tiles')
tset = set(targets)
base = run + '/maps/s2/tiles/0'
n = 0
for p in glob.glob(base + '/**/*.prbm', recursive=True):
    rel = os.path.relpath(p, base).replace(os.sep, '/')[:-5]
    xs, zs = rel[1:].split('/z', 1)
    tx, tz = int(xs.replace('/', '')), int(zs.replace('/', ''))
    owner = ((tx * 32 + 2) // 512, (tz * 32 + 2) // 512)
    if owner not in tset:
        continue
    dst = out + '/tiles/' + rel + '.gvc'
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    convert(p, dst)
    n += 1
for rx, rz in targets:
    shutil.copy(f'{cullw}/dimensions/minecraft/overworld/region/r.{rx}.{rz}.mca', f'{out}/cull/r.{rx}.{rz}.mca')
tj = json.load(open(run + '/maps/s2/textures.json'))
json.dump([x['resourcePath'] for x in tj], open(out + '/textures_paths.json', 'w'))
with open(out + '/manifest.txt', 'w') as m:
    for p in sorted(glob.glob(out + '/**/*', recursive=True)):
        if os.path.isfile(p) and not p.endswith('manifest.txt'):
            b = open(p, 'rb').read()
            m.write(f'{os.path.relpath(p, out)} {len(b)} {hashlib.sha1(b).hexdigest()}\n')
log(f'{n} owned tiles compacted, {len(targets)} culled targets')

# 4. push the outputs as an orphan branch (no input files in it)
o = W + '/pushrepo'
shutil.rmtree(o, ignore_errors=True)
url = sh(f'git -C {R} remote get-url origin').strip()
sh(f'git clone -q --no-checkout {R} {o} && cd {o} && git checkout -q --orphan {OUT_BRANCH} && git rm -rqf --cached . 2>/dev/null || true')
for f in os.listdir(out):
    s, d = os.path.join(out, f), os.path.join(o, f)
    (shutil.copytree if os.path.isdir(s) else shutil.copy)(s, d)
sh(f'cd {o} && git config user.name gv-cloud && git config user.email gv-cloud@local && git add -A && git commit -qm "stage2 {OUT_BRANCH}"')
for k in range(6):
    try:
        sh(f'cd {R} && git fetch -q {o} {OUT_BRANCH}:{OUT_BRANCH} && git push -q origin {OUT_BRANCH}')
        break
    except RuntimeError as e:
        log('push retry', k, str(e)[-200:]); time.sleep(10 * (k + 1))
else:
    raise RuntimeError('push failed')
log('STAGE2 DONE', OUT_BRANCH)
