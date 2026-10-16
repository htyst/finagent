"""Offline source fingerprint, installed-license metadata and credential exclusion checks."""
import hashlib
import argparse
from importlib import metadata
import json
from pathlib import Path
import runpy
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--name',default='p1-source-and-licenses.json')
parser.add_argument('--baseline',default='P1-candidate-20261015')
args=parser.parse_args()
if Path(args.name).name!=args.name or not args.name.endswith('.json'):
    parser.error('Report name must be a .json filename without directories')
paths = sorted(runpy.run_path(str(ROOT/'scripts/package-source.py'))['source_paths']())
key = (dotenv_values(ROOT/'.env').get('MODEL_API_KEY') or '').strip()
assert all(p.name != '.env' for p in paths)
if key:
    assert not any(key.encode() in p.read_bytes() for p in paths), 'Actual model credential occurs in exportable source'
# Stable code fingerprint does not recursively include the report itself or Markdown.
code = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths if p.relative_to(ROOT).parts[0] in {'backend','frontend','scripts'}
        and p.suffix not in {'.md','.xml'} or p.name in {'requirements.lock','package-lock.json','pyproject.toml','package.json','dialogue-v1.json'}}
fingerprint = hashlib.sha256(json.dumps(code, sort_keys=True).encode()).hexdigest()
lock = (ROOT/'backend/requirements.lock').read_text(encoding='utf-8').splitlines()
python_packages = []
for line in lock:
    if '==' not in line or line.startswith('#'): continue
    name, pinned = line.split('==', 1)
    try:
        dist = metadata.distribution(name)
        license_text = dist.metadata.get('License-Expression') or dist.metadata.get('License') or 'not declared'
        python_packages.append({'name':name,'pinned':pinned,'installed':dist.version,'license_metadata':license_text[:1000]})
    except metadata.PackageNotFoundError:
        python_packages.append({'name':name,'pinned':pinned,'installed':None,'license_metadata':'not installed'})
node = json.loads((ROOT/'frontend/package-lock.json').read_text(encoding='utf-8'))
node_packages = [{'path':name, 'version':p.get('version'), 'license_metadata':p.get('license','not declared')}
                 for name,p in node.get('packages',{}).items() if name]
report = {'baseline':args.baseline, 'code_sha256':fingerprint, 'code_files':code,
    'database_head':'0013_security', 'seed_version':'m2-2026-09-25-1',
    'actual_model_key_found_in_distributable_sources':False, 'private_env_excluded':True,
    'python_packages':python_packages, 'node_packages':node_packages,
    'license_review':'Metadata inventory only; absent project LICENSE and third-party notices require team review. No legal compliance conclusion.',
    'not_final_submission':True}
destination=ROOT/'docs/evidence'/args.name
destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'report':str(destination),'code_sha256':fingerprint,'code_files':len(code),
    'python_packages':len(python_packages),'node_packages':len(node_packages),'key_scan':'passed'},ensure_ascii=False))
