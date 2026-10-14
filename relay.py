"""relay.py (GitHub Actions): every file listed in manifest.txt of this branch is checked (size + sha1) and uploaded in ONE
commit to the private dataset mehdi111222/gv-world at the same path; the manifest goes to manifests/<branch>.txt. Retries
on errors; exits non-zero (branch is then NOT deleted) if anything does not verify or upload."""
import hashlib, os, sys, time
from huggingface_hub import HfApi, CommitOperationAdd

REPO = 'mehdi111222/gv-world'
branch = os.environ['BRANCH']
tok = os.environ.get('HF_TOKEN')
if not tok:
    sys.exit('HF_TOKEN secret missing')
rows = [l.split() for l in open('manifest.txt').read().splitlines() if l.strip()]
ops = []
for path, size, sha in rows:
    b = open(path, 'rb').read()
    if len(b) != int(size) or hashlib.sha1(b).hexdigest() != sha:
        sys.exit(f'verify failed: {path}')
    ops.append(CommitOperationAdd(path_in_repo=path, path_or_fileobj=path))
ops.append(CommitOperationAdd(path_in_repo=f'manifests/{branch}.txt', path_or_fileobj='manifest.txt'))
api = HfApi(token=tok)
for k in range(6):
    try:
        api.create_commit(repo_id=REPO, repo_type='dataset', operations=ops, commit_message=f'relay {branch} ({len(rows)} files)')
        break
    except Exception as e:
        print('upload retry', k, str(e)[:300], flush=True)
        time.sleep(30 * (k + 1))
else:
    sys.exit('upload failed')
have = {f.rfilename: f.size for f in api.list_repo_tree(REPO, repo_type='dataset', recursive=True) if hasattr(f, 'size')}
bad = [p for p, s, _ in rows if have.get(p) != int(s)]
if bad:
    sys.exit(f'{len(bad)} files missing/size mismatch on HF after upload, e.g. {bad[:3]}')
print(f'relayed {len(rows)} files of {branch} to {REPO}')
