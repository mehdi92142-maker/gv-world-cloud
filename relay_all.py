"""relay_all.py (GitHub Actions, one job at a time): ALL pending out-* branches -> verified (manifest sha1) -> uploaded in a
few big commits (HF limits commits per hour, not bytes) -> checked on HF -> branches deleted. Batches up to ~4 GB.
orig regions already on HF are never replaced (first copy wins). Private first, public (orig/done) last."""
import hashlib, os, shutil, subprocess, sys, time
from huggingface_hub import HfApi, CommitOperationAdd

REPO, PUB = 'mehdi111222/gv-world', 'mehdi111222/gv-world-pub'
api = HfApi(token=os.environ['HF_TOKEN'])
T0 = time.time()


def sh(*a):
    return subprocess.run(a, check=True, capture_output=True, text=True).stdout


def commit(repo, ops, msg):
    for k in range(8):
        try:
            api.create_commit(repo_id=repo, repo_type='dataset', operations=ops, commit_message=msg)
            break
        except Exception as e:
            print('retry', repo, k, str(e)[:200], flush=True); time.sleep(60 * (k + 1))
    else:
        raise RuntimeError('commit failed')
    sizes = {}
    for i in range(0, len(ops), 50):
        for p in api.get_paths_info(repo, [o.path_in_repo for o in ops[i:i + 50]], repo_type='dataset'):
            sizes[p.path] = getattr(p, 'size', None)
    for o in ops:
        if sizes.get(o.path_in_repo) != os.path.getsize(o.path_or_fileobj):
            raise RuntimeError(f'{o.path_in_repo} not on HF')


while time.time() - T0 < 40 * 60:
    brs = sorted((l.split('refs/heads/')[1] for l in sh('git', 'ls-remote', '--heads', 'origin', 'out-*').splitlines()),
                 key=lambda b: not b.startswith('out-s2'))
    if not brs:
        print('nothing pending'); break
    have = {f.rfilename for f in api.list_repo_tree(PUB, repo_type='dataset', path_in_repo='orig')}
    ops, done, size = {REPO: [], PUB: []}, [], 0
    shutil.rmtree('/tmp/rl', ignore_errors=True)
    for br in brs:
        if size > 4e9:
            break
        d = f'/tmp/rl/{br}'
        try:
            os.makedirs(d); sh('git', '-C', d, 'init', '-q')
            sh('git', '-C', d, 'fetch', '-q', '--depth', '1', os.environ['URL'], f'refs/heads/{br}')
            sh('git', '-C', d, 'checkout', '-q', 'FETCH_HEAD')
            rows = [l.split() for l in open(f'{d}/manifest.txt').read().splitlines() if l.strip()]
            mine = {REPO: [CommitOperationAdd(path_in_repo=f'manifests/{br}.txt', path_or_fileobj=f'{d}/manifest.txt')], PUB: []}
            for path, sz, sha in rows:
                b = open(f'{d}/{path}', 'rb').read()
                if len(b) != int(sz) or hashlib.sha1(b).hexdigest() != sha:
                    raise RuntimeError(f'verify failed {path}')
                if path.startswith('orig/') and path in have:
                    continue
                if any(o.path_in_repo == path for o in ops[PUB]):
                    continue
                mine[PUB if path.startswith(('orig/', 'done/')) else REPO].append(CommitOperationAdd(path_in_repo=path, path_or_fileobj=f'{d}/{path}'))
                size += len(b)
            for r in mine:
                ops[r] += mine[r]
            done.append(br)
        except Exception as e:
            print('skip', br, str(e)[:200], flush=True)
    print(f'batch: {len(done)} branches, {size / 1e9:.2f} GB', flush=True)
    for repo in (REPO, PUB):
        if ops[repo]:
            commit(repo, ops[repo], f'relay batch {len(done)} branches')
    for i in range(0, len(done), 50):
        sh('git', 'push', '-q', 'origin', '--delete', *done[i:i + 50])
    print(f'relayed {len(done)} branches ({time.time() - T0:.0f}s)', flush=True)
