from pathlib import Path
import hashlib,json,os,re,shutil,subprocess
root=Path(__file__).resolve().parents[1]
out=root/os.getenv('FINAGENT_REVIEW_DIR','var/reviews/frontend-current');out.mkdir(parents=True,exist_ok=True)
local=root/'var/tools/node-v24.22.0-win-x64/node.exe';node=str(local) if local.exists() else shutil.which('node')
if not node:raise SystemExit('Node is missing; run scripts/setup.ps1 first')
files=[*sorted((root/'frontend/src').rglob('*')),*sorted((root/'frontend/tests').glob('*'))]
manifest={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}
results=[];tests=0
for name,args in [('unit',['--test','tests/api-regression.cjs','tests/g01-progress.cjs']),('types',['node_modules/typescript/bin/tsc','-b']),('build',['node_modules/vite/bin/vite.js','build'])]:
 p=out/('frontend-'+name+'.log')
 with p.open('w',encoding='utf-8') as log:r=subprocess.run([node,*args],cwd=root/'frontend',stdout=log,stderr=subprocess.STDOUT,timeout=120)
 if name=='unit':
  matched=re.search(r'(?:tests|# tests)\s+(\d+)',p.read_text(encoding='utf-8'));tests=int(matched[1]) if matched else 0
 results.append(dict(check=name,returncode=r.returncode));print(name,r.returncode,flush=True)
 if r.returncode:break
same=all(hashlib.sha256((root/n).read_bytes()).hexdigest()==sha for n,sha in manifest.items())
(out/'frontend-source.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
report=dict(passed=len(results)==3 and all(r['returncode']==0 for r in results) and tests>0 and same,tests=tests,source_unchanged=same,source_version=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest(),results=results)
(out/'frontend.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
raise SystemExit(0 if report['passed'] else 1)