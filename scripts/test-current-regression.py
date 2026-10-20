"""Whole-suite isolated regression with an immutable source manifest."""
import hashlib,json,os,subprocess,sys,time,xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/os.getenv('FINAGENT_REVIEW_DIR','var/reviews/regression-'+uuid4().hex);OUT.mkdir(parents=True,exist_ok=True)
files=[*sorted((ROOT/'backend/app').rglob('*.py')),*sorted((ROOT/'backend/tests').glob('test_*.py'))]
manifest={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
(OUT/'backend-tested-source.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8');version=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
env=dict(os.environ,PYTHONPATH=str(ROOT/'backend'),PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1',FINAGENT_IMPORT_TEST_DB=str(ROOT/'var/TEST-ONLY/f01-20261017/baseline.sqlite3'),FINAGENT_SCHEDULER_ENABLED='0',FINAGENT_SUBSCRIPTIONS_ENABLED='0',FINAGENT_BIRTHDAYS_ENABLED='0',FINAGENT_WORKFLOWS_ENABLED='0')
subprocess.run([sys.executable,'-B',str(ROOT/'scripts/prepare-test-data.py'),'--target',env['FINAGENT_IMPORT_TEST_DB']],cwd=ROOT,env=env,check=True,timeout=300)
started=time.perf_counter();xml=OUT/'backend-full.xml'
with (OUT/'backend-full.log').open('w',encoding='utf-8') as log:
 run=subprocess.run([sys.executable,'-B','-m','pytest','backend/tests','-q','-p','no:cacheprovider','--junitxml='+str(xml)],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=2400)
same=all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==sha for p,sha in manifest.items())
suites=list(ET.parse(xml).getroot().iter('testsuite')) if xml.exists() else []
result=dict(passed=run.returncode==0 and same and bool(suites),returncode=run.returncode,source_unchanged=same,source_version=version,execution='one complete pytest process',file_count=len(list((ROOT/'backend/tests').glob('test_*.py'))),seconds=round(time.perf_counter()-started,2),**{k:sum(int(s.attrib.get(k,0)) for s in suites) for k in ['tests','failures','errors','skipped']})
(OUT/'backend-summary.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result),flush=True)
