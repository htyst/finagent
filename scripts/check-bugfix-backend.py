"""Run every backend test file in two separate processes with source fingerprints."""
from pathlib import Path
from uuid import uuid4
import hashlib,json,os,subprocess,sys
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1]
RUN=ROOT/'var/TEST-ONLY'/('bugfix-backend-'+uuid4().hex);RUN.mkdir(parents=True)
files=sorted(set((ROOT/'backend/tests').rglob('test_*.py'))|set((ROOT/'backend/tests').rglob('*_test.py')))
source={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for base in ['backend/app','backend/tests','backend/migrations'] for p in (ROOT/base).rglob('*.py')}
processes=[];logs=[]
for shard in range(2):
    log=(RUN/f'shard-{shard}.log').open('w',encoding='utf-8');logs.append(log)
    names=[str(p.relative_to(ROOT/'backend')) for p in files[shard::2]]
    command=[sys.executable,'-B','-m','pytest',*names,'-q','--tb=short','-rs',f'--junitxml={RUN/f"shard-{shard}.xml"}']
    processes.append(subprocess.Popen(command,cwd=ROOT/'backend',stdout=log,stderr=subprocess.STDOUT,env=dict(os.environ,PYTHONIOENCODING='utf-8'),creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0))
codes=[p.wait() for p in processes]
for log in logs:log.close()
totals=dict(tests=0,failures=0,errors=0,skipped=0)
for shard in range(2):
    for suite in ET.parse(RUN/f'shard-{shard}.xml').getroot().iter('testsuite'):
        for key in totals:totals[key]+=int(suite.get(key,'0'))
same=all(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest for name,digest in source.items())
report=dict(passed=all(code==0 for code in codes) and same,source_unchanged=same,returncodes=codes,test_files=len(files),**totals,source=source,output=str(RUN))
(RUN/'results.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
(ROOT/'var/TEST-ONLY/latest-backend-result.json').write_text(json.dumps({k:v for k,v in report.items() if k!='source'},indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in report.items() if k!='source'}),flush=True)
raise SystemExit(0 if report['passed'] else 1)
