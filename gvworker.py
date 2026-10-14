"""gvworker.py -- one autonomous cloud worker for the seed2k world (radius 10000). Many copies run in parallel (routines
"gv worker K", hourly). Everything a worker needs is public or in this repo; it never needs a token:

  inputs   : public dataset mehdi111222/gv-world-pub  (orig/r.X.Z.mca + done/s2/b_BX_BZ markers) and, for the 332 regions
             made before, the branches orig-1..3 of this repo
  outputs  : git branches out-<kind>-<worker>-<time> pushed to THIS repo; the GitHub Action relay.yml verifies every file
             (size + sha1 from manifest.txt) and uploads it to Hugging Face (orig/ + done/ -> gv-world-pub, cull/ tiles/ ->
             private gv-world), then deletes the branch.

Work (deterministic split, no locks): the world = regions within radius R of 0,0, grouped in 3x3 blocks sorted by
distance; block i belongs to worker i % N.
  1. GEN    my blocks' regions that exist nowhere yet: vanilla 26.2 server (sha1 checked), region + 1-chunk margin,
            tick sprint 6000, every chunk checked (full, lit, DataVersion 4903, position) -> pushed 3 at a time.
  2. STAGE2 my blocks whose 9 targets AND their ring exist: GvCull (targets + ring), BlueMap, owned tiles -> GVC1
            (round-trip checked) -> tiles/r.X.Z.tar per region + cull/r.X.Z.mca + done marker -> pushed per block.
Stops starting new work near the time limit; a crash only loses the unpushed block (done again next hour).
Usage: python3 gvworker.py K N MINUTES [RADIUS]
"""
import glob, hashlib, io, json, math, os, re, shutil, subprocess, sys, tarfile, threading, time, zlib, queue

K, N, MINUTES = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])
RADIUS = int(sys.argv[4]) if len(sys.argv) > 4 else 10000
SEED = '3942280111883238289'
SHA1 = '823e2250d24b3ddac457a60c92a6a941943fcd6a'
PUB = 'mehdi111222/gv-world-pub'
R = os.getcwd()                      # repo checkout (master)
W = '/home/user/gvw'
T0 = time.time()
T_END = T0 + MINUTES * 60
os.makedirs(W, exist_ok=True)


def log(*a):
    print(time.strftime('%H:%M:%S'), f'[w{K}]', *a, flush=True)


def sh(c, timeout=None):
    r = subprocess.run(c, shell=True, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f'{c[:160]} -> {r.returncode}: {r.stderr[-600:]} {r.stdout[-300:]}')
    return r.stdout


def left():
    return T_END - time.time()


# ------------------------------------------------------------------ plan
def regions_in_circle(r):
    out = []
    for rx in range(math.floor(-r / 512), math.floor(r / 512) + 1):
        for rz in range(math.floor(-r / 512), math.floor(r / 512) + 1):
            dx = max(rx * 512, 0, -(rx * 512 + 511)); dz = max(rz * 512, 0, -(rz * 512 + 511))
            if dx * dx + dz * dz <= r * r:
                out.append((rx, rz))
    return out


WORLD_REGS = set(regions_in_circle(RADIUS))
BLOCKS = {}
for q in WORLD_REGS:
    BLOCKS.setdefault((math.floor(q[0] / 3), math.floor(q[1] / 3)), []).append(q)
ORDER = sorted(BLOCKS, key=lambda b: (b[0] * 3 + 1) ** 2 + (b[1] * 3 + 1) ** 2)
MINE = [b for i, b in enumerate(ORDER) if i % N == K]


def ring(targets):
    return {(x + dx, z + dz) for x, z in targets for dx in (-1, 0, 1) for dz in (-1, 0, 1)} & WORLD_REGS


# ------------------------------------------------------------------ state (public dataset + orig branches)
ORIG_BRANCH = {}


def load_orig_branches():
    for b in ('orig-1', 'orig-2', 'orig-3'):
        sh(f'cd {R} && git fetch -q origin {b}:refs/remotes/origin/{b}')
        for f in sh(f'cd {R} && git ls-tree --name-only refs/remotes/origin/{b} region/').split():
            x, z = map(int, os.path.basename(f)[2:-4].split('.'))
            ORIG_BRANCH[(x, z)] = b


def hf_list(prefix):
    """file paths under prefix in the public dataset (unauthenticated API, paginated)"""
    out, url = [], f'https://huggingface.co/api/datasets/{PUB}/tree/main/{prefix}?recursive=true&limit=1000'
    while url:
        hdr = W + '/h.txt'
        r = subprocess.run(f'curl -sSL -m 60 -D {hdr} -w "%{{http_code}}" -o {W}/b.json "{url}"', shell=True, capture_output=True, text=True)
        code = r.stdout.strip()[-3:]
        if code == '404':
            return out                                   # folder does not exist yet
        if code != '200':
            raise RuntimeError(f'HF list {prefix}: HTTP {code}')
        body = open(W + '/b.json').read()
        out += [e['path'] for e in json.loads(body) if e.get('type') == 'file']
        m = re.search(r'<([^>]+)>;\s*rel="next"', open(hdr).read())
        url = m.group(1) if m else None
    return out


def pending_branches():
    return sh(f'cd {R} && git ls-remote --heads origin "out-*"')


def state():
    orig = set(ORIG_BRANCH)
    for p in hf_list('orig'):
        m = re.search(r'r\.(-?\d+)\.(-?\d+)\.mca$', p)
        if m:
            orig.add((int(m.group(1)), int(m.group(2))))
    done = set()
    for p in hf_list('done'):
        m = re.search(r'b_(-?\d+)_(-?\d+)$', p)
        if m:
            done.add((int(m.group(1)), int(m.group(2))))
    return orig, done


# ------------------------------------------------------------------ tools
def setup():
    jdk = W + '/jdk'
    if not os.path.exists(jdk + '/bin/javac'):
        sh(f'curl -sSLf -m 900 -o {W}/jdk.tgz https://api.adoptium.net/v3/binary/latest/25/ga/linux/x64/jdk/hotspot/normal/eclipse')
        os.makedirs(jdk, exist_ok=True)
        sh(f'tar xzf {W}/jdk.tgz -C {jdk} --strip-components=1')
    jar = W + '/server.jar'
    if not os.path.exists(jar) or hashlib.sha1(open(jar, 'rb').read()).hexdigest() != SHA1:
        m = json.loads(sh('curl -sSLf -m 120 https://piston-meta.mojang.com/mc/game/version_manifest_v2.json'))
        v = json.loads(sh(f"curl -sSLf -m 120 {[x for x in m['versions'] if x['id'] == '26.2'][0]['url']}"))
        if v['downloads']['server']['sha1'] != SHA1:
            sys.exit('STOP: Mojang 26.2 server sha1 differs from the user file')
        sh(f"curl -sSLf -m 600 -o {jar} {v['downloads']['server']['url']}")
        if hashlib.sha1(open(jar, 'rb').read()).hexdigest() != SHA1:
            sys.exit('STOP: server.jar sha1 mismatch')
    try:
        import nbtlib, numpy  # noqa: F401
    except ImportError:
        sh(f'{sys.executable} -m pip install -q nbtlib numpy')
    if not os.path.exists(W + '/build/GvCull.class'):
        sh(f'mkdir -p {W}/build && {jdk}/bin/javac -d {W}/build {R}/GvCull.java')
    sh(f'cd {R} && git config user.name gv-cloud && git config user.email gv-cloud@local')
    log('setup ok (JDK 25, server 26.2 sha1 ok, GvCull compiled)')


JAVA = W + '/jdk/bin/java'


def region_ok(path, rx, rz):
    import nbtlib
    buf = open(path, 'rb').read()
    if len(buf) < 8192:
        return False
    for i in range(1024):
        off = int.from_bytes(buf[i * 4:i * 4 + 3], 'big') * 4096
        if off == 0 or off + 5 > len(buf):
            return False
        ln = int.from_bytes(buf[off:off + 4], 'big')
        if buf[off + 4] != 2:
            return False
        c = nbtlib.File.parse(io.BytesIO(zlib.decompress(buf[off + 5:off + 4 + ln])))
        if not str(c.get('Status', '')).endswith('full') or int(c.get('isLightOn', 0)) != 1 or int(c.get('DataVersion', 0)) != 4903:
            return False
        if int(c['xPos']) != rx * 32 + (i % 32) or int(c['zPos']) != rz * 32 + (i // 32):
            return False
    return True


def push_branch(kind, files, extra_name=''):
    """files: {path_in_branch: local_file}; one orphan commit with manifest + relay workflow -> origin out-..."""
    name = f'out-{kind}-w{K}-{int(time.time())}{extra_name}'
    d = W + '/push'
    shutil.rmtree(d, ignore_errors=True)
    sh(f'git clone -q --no-checkout {R} {d}')
    sh(f'cd {d} && git checkout -q --orphan {name} && (git rm -rqf --cached . 2>/dev/null || true)')
    rows = []
    for p, src in sorted(files.items()):
        dst = os.path.join(d, p); os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy(src, dst)
        b = open(dst, 'rb').read()
        rows.append(f'{p} {len(b)} {hashlib.sha1(b).hexdigest()}')
    open(d + '/manifest.txt', 'w').write('\n'.join(rows) + '\n')
    os.makedirs(d + '/.github/workflows', exist_ok=True)
    shutil.copy(R + '/.github/workflows/relay.yml', d + '/.github/workflows/relay.yml')
    sh(f'cd {d} && git config user.name gv-cloud && git config user.email gv-cloud@local && git add -A && git commit -qm "{name}"')
    sh(f'cd {R} && git fetch -q {d} {name}:{name}')
    for k in range(8):
        try:
            sh(f'cd {R} && git push -q origin {name}', timeout=1800)
            log(f'pushed {name} ({len(rows)} files)')
            shutil.rmtree(d, ignore_errors=True)
            sh(f'cd {R} && git branch -D {name} -q')
            return name
        except Exception as e:
            log('push retry', k, str(e)[-200:]); time.sleep(20 * (k + 1))
    raise RuntimeError('push failed: ' + name)


# ------------------------------------------------------------------ 1. generation
def gen_regions(todo):
    """3 servers in parallel, same procedure as the PC; returns {(x, z): verified file}"""
    res, lock = {}, threading.Lock()
    q = queue.Queue()
    for r in todo:
        q.put(r)

    def server(k):
        d = f'{W}/srv{k}'
        shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
        shutil.copy(W + '/server.jar', d)
        open(d + '/eula.txt', 'w').write('eula=true\n')
        open(d + '/server.properties', 'w').write(
            f'level-seed={SEED}\nlevel-type=minecraft\\:normal\ngenerator-settings={{}}\nmax-tick-time=-1\nonline-mode=false\n'
            f'level-name=gen\nview-distance=2\nsimulation-distance=2\nspawn-protection=0\ndifficulty=easy\nserver-port={25600 + k}\n')
        p = subprocess.Popen([JAVA, '-Xmx2G', '-jar', 'server.jar', 'nogui'], cwd=d, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        ev = {'Done (': threading.Event(), 'Saved the game': threading.Event(), 'Sprint completed': threading.Event()}

        def pump():
            for line in p.stdout:
                for key, e in ev.items():
                    if key in line:
                        e.set()
        threading.Thread(target=pump, daemon=True).start()

        def cmd(s):
            p.stdin.write(s + '\n'); p.stdin.flush()
        try:
            if not ev['Done ('].wait(600):
                log('server', k, 'did not start'); return
            while left() > 240:
                try:
                    rx, rz = q.get_nowait()
                except queue.Empty:
                    return
                for attempt in range(2):
                    for fx in range(rx * 32 - 1, rx * 32 + 33, 16):
                        for fz in range(rz * 32 - 1, rz * 32 + 33, 16):
                            ex, ez = min(fx + 15, rx * 32 + 32), min(fz + 15, rz * 32 + 32)
                            cmd(f'forceload add {fx * 16} {fz * 16} {ex * 16 + 15} {ez * 16 + 15}')
                    ev['Saved the game'].clear(); cmd('save-all flush'); ev['Saved the game'].wait(1800)
                    ev['Sprint completed'].clear(); cmd('tick sprint 6000'); ev['Sprint completed'].wait(1800)
                    ev['Saved the game'].clear(); cmd('save-all flush'); ev['Saved the game'].wait(1800)
                    cmd('forceload remove all')
                    src = f'{d}/gen/dimensions/minecraft/overworld/region/r.{rx}.{rz}.mca'
                    if os.path.exists(src):
                        keep = f'{W}/genout/r.{rx}.{rz}.mca'
                        os.makedirs(os.path.dirname(keep), exist_ok=True)
                        shutil.copy(src, keep)
                        if region_ok(keep, rx, rz):
                            with lock:
                                res[(rx, rz)] = keep
                            break
                    log(f'gen {rx},{rz}: verification failed (attempt {attempt + 1})')
        finally:
            try:
                cmd('stop'); p.wait(300)
            except Exception:
                p.kill()
    th = [threading.Thread(target=server, args=(k,)) for k in range(3)]
    for t in th:
        t.start()
    # push finished regions every 3 while the servers keep working
    pushed = set()
    while any(t.is_alive() for t in th):
        time.sleep(10)
        with lock:
            ready = {q_: f for q_, f in res.items() if q_ not in pushed}
        if len(ready) >= 3:
            push_branch('gen', {f'orig/r.{x}.{z}.mca': f for (x, z), f in ready.items()})
            pushed |= set(ready)
    with lock:
        ready = {q_: f for q_, f in res.items() if q_ not in pushed}
    if ready:
        push_branch('gen', {f'orig/r.{x}.{z}.mca': f for (x, z), f in ready.items()})
    return res


# ------------------------------------------------------------------ 2. stage 2
def fetch_orig(q, dst, local_gen):
    x, z = q
    name = f'r.{x}.{z}.mca'
    if q in local_gen:
        shutil.copy(local_gen[q], dst); return
    if q in ORIG_BRANCH:
        sh(f'cd {R} && git show refs/remotes/origin/{ORIG_BRANCH[q]}:region/{name} > {dst}'); return
    sh(f'curl -sSLf -m 600 -o {dst} https://huggingface.co/datasets/{PUB}/resolve/main/orig/{name}')
    if not region_ok(dst, x, z):
        raise RuntimeError(f'downloaded {name} failed the chunk check')


def stage2(block, targets, local_gen):
    t0 = time.time()
    need = ring(targets)
    wd = W + '/s2'
    shutil.rmtree(wd, ignore_errors=True)
    world = wd + '/world'
    src = world + '/dimensions/minecraft/overworld/region'
    os.makedirs(src)
    shutil.copy(R + '/level.dat', world + '/level.dat')
    for q in sorted(need):
        fetch_orig(q, f'{src}/r.{q[0]}.{q[1]}.mca', local_gen)
    cullw = wd + '/wcull'
    os.makedirs(cullw + '/dimensions/minecraft/overworld/region')
    shutil.copy(world + '/level.dat', cullw + '/level.dat')
    from concurrent.futures import ThreadPoolExecutor

    def cull_one(q):
        rx, rz = q
        tmp = f'{wd}/c_{rx}_{rz}'
        sh(f'{JAVA} -Xmx3G -cp {W}/build GvCull {world} {tmp} {rx * 512} {rz * 512} {rx * 512 + 511} {rz * 512 + 511}', timeout=3600)
        os.replace(f'{tmp}/dimensions/minecraft/overworld/region/r.{rx}.{rz}.mca', f'{cullw}/dimensions/minecraft/overworld/region/r.{rx}.{rz}.mca')
        shutil.rmtree(tmp, ignore_errors=True)
    with ThreadPoolExecutor(3) as ex:
        list(ex.map(cull_one, sorted(need)))
    t1 = time.time()
    run = wd + '/bm'
    os.makedirs(run)
    shutil.copy(R + '/bluemap-cli.jar', run)
    shutil.copytree(R + '/bmcfg', run + '/bluemap_config')
    conf = open(run + '/bluemap_config/maps/seed2k_cull.conf').read()
    conf = re.sub(r'(?m)^world: .*$', f'world: "{cullw}"', conf)
    mask = ',\n'.join(f'  {{ min-x: {rx * 512 - 34}, max-x: {rx * 512 + 545}, min-z: {rz * 512 - 34}, max-z: {rz * 512 + 545} }}' for rx, rz in targets)
    conf = re.sub(r'(?ms)^render-mask: \[.*?^\]', f'render-mask: [\n{mask}\n]', conf)
    for f in glob.glob(run + '/bluemap_config/maps/*.conf'):
        os.remove(f)
    open(run + '/bluemap_config/maps/s2.conf', 'w').write(conf)
    sh(f'cd {run} && {JAVA} -Xmx6G -jar bluemap-cli.jar -c bluemap_config -r -f -m s2', timeout=5400)
    t2 = time.time()
    sys.path.insert(0, R)
    from compact import convert
    out = wd + '/out'
    os.makedirs(out)
    tset = set(targets)
    per = {q: [] for q in targets}
    base = run + '/maps/s2/tiles/0'
    for p in glob.glob(base + '/**/*.prbm', recursive=True):
        rel = os.path.relpath(p, base).replace(os.sep, '/')[:-5]
        xs, zs = rel[1:].split('/z', 1)
        tx, tz = int(xs.replace('/', '')), int(zs.replace('/', ''))
        owner = ((tx * 32 + 2) // 512, (tz * 32 + 2) // 512)
        if owner in tset:
            dst = f'{out}/t/{rel}.gvc'
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            convert(p, dst)
            per[owner].append((rel + '.gvc', dst))
    files = {}
    for (rx, rz), lst in per.items():
        tp = f'{out}/r.{rx}.{rz}.tar'
        with tarfile.open(tp, 'w') as tf:
            for rel, dst in sorted(lst):
                tf.add(dst, arcname=rel)
        files[f'tiles/r.{rx}.{rz}.tar'] = tp
        files[f'cull/r.{rx}.{rz}.mca'] = f'{cullw}/dimensions/minecraft/overworld/region/r.{rx}.{rz}.mca'
    json.dump([x['resourcePath'] for x in json.load(open(run + '/maps/s2/textures.json'))], open(out + '/tp.json', 'w'))
    files['textures_paths.json'] = out + '/tp.json'
    mk = f'{out}/b_{block[0]}_{block[1]}'
    open(mk, 'w').write(json.dumps({'targets': targets, 'tiles': {f'{x},{z}': len(per[(x, z)]) for x, z in targets}}))
    files[f'done/s2/b_{block[0]}_{block[1]}'] = mk
    push_branch('s2', files, f'-b{block[0]}_{block[1]}')
    log(f'stage2 block {block}: {len(targets)} targets, {len(need)} culled ({t1 - t0:.0f}s), BlueMap {t2 - t1:.0f}s, '
        f'{sum(len(v) for v in per.values())} tiles, total {time.time() - t0:.0f}s')
    shutil.rmtree(wd, ignore_errors=True)


# ------------------------------------------------------------------ main loop
def main():
    log(f'worker {K}/{N}, {MINUTES} min, radius {RADIUS}: {len(WORLD_REGS)} regions, {len(BLOCKS)} blocks, mine {len(MINE)}')
    setup()
    load_orig_branches()
    local_gen = {}
    idle = 0
    while left() > 300:
        orig, done = state()
        pend = pending_branches()
        todo_gen = [q for b in MINE for q in BLOCKS[b] if q not in orig and q not in local_gen]
        if todo_gen and left() > 600:
            log(f'gen: {len(todo_gen)} of my regions missing')
            local_gen.update(gen_regions(todo_gen[:30]))
            continue
        ready = [b for b in MINE if b not in done and f'-b{b[0]}_{b[1]}' not in pend
                 and all(q in orig or q in local_gen for q in ring(BLOCKS[b]))]
        if ready and left() > 30 * 60:
            stage2(ready[0], sorted(BLOCKS[ready[0]]), local_gen)
            idle = 0
            continue
        todo_s2 = [b for b in MINE if b not in done]
        if not todo_s2 and not todo_gen:
            log('ALL MY WORK IS DONE')
            break
        if ready:
            log('not enough time left for another stage-2 block')
            break
        idle += 1
        log(f'waiting for other workers (stage-2 blocks left: {len(todo_s2)}, inputs not complete yet)')
        time.sleep(180)
    log('WORKER EXIT')


if __name__ == '__main__':
    main()
