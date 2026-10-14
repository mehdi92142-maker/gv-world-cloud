"""relay.py (GitHub Actions): every file listed in manifest.txt of this branch is checked (size + sha1) and uploaded in ONE
commit to the private dataset mehdi111222/gv-world at the same path; the manifest goes to manifests/<branch>.txt. Retries
on errors; exits non-zero (branch is then NOT deleted) if anything does not verify or upload."""
import hashlib, os, sys, time
from huggingface_hub import HfApi, CommitOperationAdd

REPO = 'mehdi111222/gv-world'          # private: cull/ tiles/ manifests/ textures_paths.json
PUB = 'mehdi111222/gv-world-pub'      # public: orig/ done/ (read by the cloud workers without a token)
branch = os.environ['BRANCH']
tok = os.environ.get('HF_TOKEN')
if not tok:
    sys.exit('HF_TOKEN secret missing')
rows = [l.split() for l in open('manifest.txt').read().splitlines() if l.strip()]
ops = {REPO: [], PUB: []}
for path, size, sha in rows:
    b = open(path, 'rb').read()
    if len(b) != int(size) or hashlib.sha1(b).hexdigest() != sha:
        sys.exit(f'verify failed: {path}')
    ops[PUB if path.startswith(('orig/', 'done/')) else REPO].append(CommitOperationAdd(path_in_repo=path, path_or_fileobj=path))
ops[REPO].append(CommitOperationAdd(path_in_repo=f'manifests/{branch}.txt', path_or_fileobj='manifest.txt'))
api = HfApi(token=tok)
# private first, public (done markers) last: a block is only marked done when its tiles are safely stored
for repo in (REPO, PUB):
    if not ops[repo]:
        continue
    for k in range(6):
        try:
            api.create_commit(repo_id=repo, repo_type='dataset', operations=ops[repo], commit_message=f'relay {branch} ({len(ops[repo])} files)')
            break
        except Exception as e:
            print('upload retry', repo, k, str(e)[:300], flush=True)
            time.sleep(30 * (k + 1))
    else:
        sys.exit(f'upload to {repo} failed')
    for op in ops[repo]:
        info = api.get_paths_info(repo, [op.path_in_repo], repo_type='dataset')
        if not info or getattr(info[0], 'size', None) != os.path.getsize(op.path_or_fileobj):
            sys.exit(f'{op.path_in_repo} missing/size mismatch on {repo} after upload')
print(f'relayed {len(rows)} files of {branch} to {REPO}')
