"""cloudgen.py -- generate Minecraft 26.2 regions exactly like the user's PC (tools/extend_world.py gen phase) and push each
verified region to the git branch of this session. Usage: python3 cloudgen.py <branch> <minutes> <rx,rz> <rx,rz> ...
"""
import hashlib, io, json, math, os, queue, shutil, subprocess, sys, threading, time, urllib.request, zlib

SEED = '3942280111883238289'
SHA1 = '823e2250d24b3ddac457a60c92a6a941943fcd6a'
HOME = '/home/user/mcw'
REPO = os.getcwd()
BRANCH, MINUTES = sys.argv[1], float(sys.argv[2])
REGIONS = [tuple(map(int, a.split(','))) for a in sys.argv[3:]]
NSERV = 3
T_END = time.time() + MINUTES * 60
LOCK = threading.Lock()
LOG = open(os.path.join(HOME, 'cloudgen.log'), 'a') if os.path.isdir(HOME) else None


def log(*a):
    s = time.strftime('%H:%M:%S ') + ' '.join(map(str, a))
    print(s, flush=True)


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True, **kw).stdout


def setup():
    os.makedirs(HOME, exist_ok=True)
    jre = os.path.join(HOME, 'jre')
    if not os.path.exists(os.path.join(jre, 'bin', 'java')):
        urllib.request.urlretrieve('https://api.adoptium.net/v3/binary/latest/25/ga/linux/x64/jre/hotspot/normal/eclipse', HOME + '/jre.tgz')
        os.makedirs(jre, exist_ok=True)
        sh(f'tar xzf {HOME}/jre.tgz -C {jre} --strip-components=1')
    jar = os.path.join(HOME, 'server.jar')
    if not os.path.exists(jar):
        m = json.load(urllib.request.urlopen('https://piston-meta.mojang.com/mc/game/version_manifest_v2.json'))
        v = json.load(urllib.request.urlopen([x for x in m['versions'] if x['id'] == '26.2'][0]['url']))
        d = v['downloads']['server']
        if d['sha1'] != SHA1:
            sys.exit('manifest sha1 differs from the user file -- STOP')
        urllib.request.urlretrieve(d['url'], jar)
    if hashlib.sha1(open(jar, 'rb').read()).hexdigest() != SHA1:
        sys.exit('server.jar sha1 mismatch -- STOP')
    try:
        import nbtlib  # noqa: F401
    except ImportError:
        sh(f'{sys.executable} -m pip install -q nbtlib')
    log('setup ok: java 25, server 26.2 sha1', SHA1)


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


def push(rx, rz, src):
    data = open(src, 'rb').read()
    h = hashlib.sha1(data).hexdigest()
    with LOCK:
        os.makedirs(os.path.join(REPO, 'regions'), exist_ok=True)
        dst = os.path.join(REPO, 'regions', f'r.{rx}.{rz}.mca')
        open(dst, 'wb').write(data)
        with open(os.path.join(REPO, 'manifest.txt'), 'a') as f:
            f.write(f'r.{rx}.{rz}.mca {len(data)} {h} ok\n')
        sh(f'git -C {REPO} add regions manifest.txt')
        sh(f'git -C {REPO} commit -qm "region {rx},{rz}"')
        for k in range(6):
            try:
                sh(f'git -C {REPO} push -q origin HEAD:{BRANCH}')
                break
            except subprocess.CalledProcessError as e:
                log('push retry', k, (e.stderr or '')[-200:])
                time.sleep(5 * (k + 1))
        else:
            log('PUSH FAILED for', rx, rz)


def server(k, q, done):
    d = os.path.join(HOME, f'srv{k}')
    shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    shutil.copy(os.path.join(HOME, 'server.jar'), d)
    open(os.path.join(d, 'eula.txt'), 'w').write('eula=true\n')
    open(os.path.join(d, 'server.properties'), 'w').write(
        f'level-seed={SEED}\nlevel-type=minecraft\\:normal\ngenerator-settings={{}}\nmax-tick-time=-1\nonline-mode=false\n'
        f'level-name=gen\nview-distance=2\nsimulation-distance=2\nspawn-protection=0\ndifficulty=easy\nserver-port={25600 + k}\n')
    p = subprocess.Popen([os.path.join(HOME, 'jre', 'bin', 'java'), '-Xmx2G', '-jar', 'server.jar', 'nogui'], cwd=d,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
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
        while time.time() < T_END:
            try:
                rx, rz = q.get_nowait()
            except queue.Empty:
                return
            t0 = time.time()
            for attempt in range(2):
                for fx in range(rx * 32 - 1, rx * 32 + 33, 16):
                    for fz in range(rz * 32 - 1, rz * 32 + 33, 16):
                        ex, ez = min(fx + 15, rx * 32 + 32), min(fz + 15, rz * 32 + 32)
                        cmd(f'forceload add {fx * 16} {fz * 16} {ex * 16 + 15} {ez * 16 + 15}')
                ev['Saved the game'].clear(); cmd('save-all flush'); ev['Saved the game'].wait(1800)
                ev['Sprint completed'].clear(); cmd('tick sprint 6000'); ev['Sprint completed'].wait(1800)
                ev['Saved the game'].clear(); cmd('save-all flush'); ev['Saved the game'].wait(1800)
                cmd('forceload remove all')
                src = os.path.join(d, 'gen', 'dimensions', 'minecraft', 'overworld', 'region', f'r.{rx}.{rz}.mca')
                if os.path.exists(src) and region_ok(src, rx, rz):
                    push(rx, rz, src)
                    done.append((rx, rz))
                    log(f'region {rx},{rz} ok on server {k} in {time.time() - t0:.0f} s')
                    break
                log(f'region {rx},{rz} failed verification (attempt {attempt + 1})')
    finally:
        try:
            cmd('stop'); p.wait(300)
        except Exception:
            p.kill()


def main():
    setup()
    sh(f'git -C {REPO} checkout -q -B {BRANCH}')
    q = queue.Queue()
    for r in REGIONS:
        q.put(r)
    done = []
    th = [threading.Thread(target=server, args=(k, q, done)) for k in range(NSERV)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    log(f'FINISHED: {len(done)} of {len(REGIONS)} regions pushed to branch {BRANCH}:', ' '.join(f'{a},{b}' for a, b in done))


if __name__ == '__main__':
    main()
