"""Offline check of distributable sources, dataset bytes and credential exclusion."""
import argparse,hashlib,json,re,runpy,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--report',type=Path);args=parser.parse_args()
 package=runpy.run_path(str(ROOT/'scripts/package-source.py'));paths=sorted(package['source_paths']());names={p.relative_to(ROOT).as_posix() for p in paths};errors=[]
 for name in sorted(package['REQUIRED']-names):errors.append('Missing source: '+name)
 data=ROOT/'datasets/demo_financial_v2';manifest=json.loads((data/'csv_file_manifest.json').read_text(encoding='utf-8'))
 for entry in manifest['files']:
  p=data/entry['file']
  if p.relative_to(ROOT).as_posix() not in names or hashlib.sha256(p.read_bytes()).hexdigest()!=entry['sha256']:errors.append('Dataset missing/modified: '+entry['file'])
 private=[];env=ROOT/'.env'
 if env.exists():
  for line in env.read_text(encoding='utf-8-sig').splitlines():
   match=re.match(r'\s*([A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD))\s*=\s*(.+)',line)
   if match:
    value=match[2].strip().strip('\"\'')
    if len(value)>=12:private.append(value.encode())
 actual_found=False
 for p in paths:
  contents=p.read_bytes()
  if any(value in contents for value in private):
   actual_found=True;errors.append('Private credential present: '+p.relative_to(ROOT).as_posix())
  if re.search(rb'(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{24,}',contents) and p.name!='check-github-readiness.py':
   # Explicitly synthetic test keys are allowed; never print matched content.
   candidates=re.findall(rb'(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{24,}',contents)
   if any(not x.startswith((b'sk-test-',b'sk-placeholder-',b'sk-not-a-real-',b'sk-your-')) for x in candidates):errors.append('Review possible credential: '+p.relative_to(ROOT).as_posix())
 git_available=True
 try:
  query=subprocess.run(['git','-C',str(ROOT),'rev-parse','--is-inside-work-tree'],capture_output=True,encoding='utf-8')
  if query.returncode==0 and query.stdout.strip()=='true':
   tracked=subprocess.run(['git','-C',str(ROOT),'ls-files','-z'],capture_output=True,check=True).stdout.decode().split('\0')
   excluded=package['EXCLUDED_DIRS']
   for name in filter(None,tracked):
    p=Path(name)
    if any(part in excluded or part.startswith('.venv') or part.endswith('.egg-info') for part in p.parts) or p.name.startswith('.env') and p.name!='.env.example' or (p.suffix in {'.sqlite3','.db','.key','.pem','.pfx','.p12'} or p.suffix.lower()=='.exe' and (len(p.parts)!=1 or p.name not in package.get('LAUNCHERS',set()))):errors.append('Private/generated file already tracked: '+name)
  else:git_available=False
 except FileNotFoundError:git_available=False
 report=dict(passed=not errors,source_files=len(paths),source_bytes=sum(p.stat().st_size for p in paths),dataset_csv_verified=len(manifest['files']),actual_configured_credentials_found=actual_found,git_index_checked=git_available,errors=errors,scope='candidate-source inventory + current index when this directory is a repository; no remote/history scan')
 if args.report:args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(report,ensure_ascii=False,indent=2));return int(bool(errors))
if __name__=='__main__':raise SystemExit(main())
