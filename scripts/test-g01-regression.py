"""Durable complete-suite shards: preserve each file result and reject missing coverage."""
import hashlib,json,os,subprocess,sys,time,xml.etree.ElementTree as ET
from pathlib import Path
ROOT=Path.cwd();OUT=ROOT/os.getenv('FINAGENT_REVIEW_DIR','var/reviews/g01-phase1-20261017');PARTS=OUT/'backend-parts';PARTS.mkdir(parents=True,exist_ok=True)
files=sorted((ROOT/'backend/tests').glob('test_*.py'))
priority=['test_tool_catalog.py','test_runtime_stability.py','test_personalized_analysis.py','test_g01_execution_graph.py']
files.sort(key=lambda p:(priority.index(p.name) if p.name in priority else len(priority),p.name))
manifest={str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in [*sorted((ROOT/'backend/app').rglob('*.py')),*files]}
version=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
manifest_path=OUT/'backend-tested-source.json'
if manifest_path.exists():
 if json.loads(manifest_path.read_text(encoding='utf-8'))!=manifest:raise SystemExit('Source changed since shard start')
else:manifest_path.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
env=dict(os.environ,PYTHONPATH=str(ROOT/'backend'),PYTHONDONTWRITEBYTECODE='1',FINAGENT_IMPORT_TEST_DB=str(ROOT/'var/TEST-ONLY/f01-20261017/baseline.sqlite3'),FINAGENT_SCHEDULER_ENABLED='0',FINAGENT_SUBSCRIPTIONS_ENABLED='0',FINAGENT_BIRTHDAYS_ENABLED='0')
records=[];started=time.monotonic()
for file in files:
 relative=str(file.relative_to(ROOT)).replace('\\','/');report=PARTS/(file.stem+'.xml');record=PARTS/(file.stem+'.json')
 if any(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=sha for name,sha in manifest.items()):raise SystemExit('Source edited during regression')
 if record.exists():
  item=json.loads(record.read_text(encoding='utf-8'))
  if item['source_version']!=version:raise SystemExit('Old shard cannot certify new source')
 else:
  before=time.monotonic()
  with (PARTS/(file.stem+'.log')).open('w',encoding='utf-8') as log:
   run=subprocess.run([sys.executable,'-B','-m','pytest',relative,'-q','-p','no:cacheprovider','--junitxml='+str(report)],env=env,stdout=log,stderr=subprocess.STDOUT,timeout=360)
  if not report.exists():raise SystemExit('Shard ended without XML: '+relative)
  suites=list(ET.parse(report).getroot().iter('testsuite'))
  item=dict(file=relative,source_version=version,returncode=run.returncode,seconds=round(time.monotonic()-before,2),
   **{key:sum(int(s.attrib.get(key,0)) for s in suites) for key in ['tests','failures','errors','skipped']})
  record.write_text(json.dumps(item,indent=2),encoding='utf-8')
 records.append(item)
 (OUT/'backend-shard-progress.json').write_text(json.dumps(dict(total_files=len(files),completed=records),indent=2),encoding='utf-8')
 print(json.dumps(item),flush=True)
 if item['returncode'] or item['failures'] or item['errors']:raise SystemExit('Failed shard: '+relative)
combined=ET.Element('testsuites');count=0
for file in files:
 for suite in ET.parse(PARTS/(file.stem+'.xml')).getroot().iter('testsuite'):
  combined.append(suite);count+=int(suite.attrib['tests'])
ET.ElementTree(combined).write(OUT/'backend-full-sharded-final.xml',encoding='utf-8',xml_declaration=True)
result=dict(passed=True,execution='sequential file shards, not a single uninterrupted pytest process',source_version=version,file_count=len(files),completed_file_count=len(records),test_records=count,
 failures=sum(i['failures'] for i in records),errors=sum(i['errors'] for i in records),skipped=sum(i['skipped'] for i in records),elapsed_this_invocation=round(time.monotonic()-started,2),parts=records)
(OUT/'backend-full-sharded-summary.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print('COMPLETE '+json.dumps({k:v for k,v in result.items() if k!='parts'}),flush=True)