const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),{renderToStaticMarkup}=require('react-dom/server');
const cache=new Map();
function load(file){
 file=path.resolve(file);if(cache.has(file))return cache.get(file);
 const exports={};cache.set(file,exports);
 const code=ts.transpileModule(fs.readFileSync(file,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 vm.runInNewContext(code,{exports,require:name=>name.startsWith('.')?load(path.resolve(path.dirname(file),name)+(fs.existsSync(path.resolve(path.dirname(file),name)+'.tsx')?'.tsx':'.ts')):require(name)},{filename:file});return exports;
}
const Summary=load(path.join(__dirname,'../src/AnalysisWorkflowSummary.tsx')).default;
function graph(status='waiting_input'){return {schema_version:'g01-v1',task_id:'t1',runtime_id:'r1',plan_version:2,status,serial:true,goals:[{id:'g1',text:'分析旅行预算',kind:'analysis',status,next_step:'请明确目标预算。',receipt_refs:[],evidence_refs:[]}],nodes:[]};}
function html(g,w){return renderToStaticMarkup(React.createElement(Summary,{graph:g,workflow:w}));}
for(const [state,label] of [['waiting_input','待补充'],['waiting_authorization','待页面授权'],['running','执行中'],['completed','已完成'],['failed','失败，可处理后继续'],['cancelled','已取消']]){
 test('goal status '+state,()=>{const result=html(graph(state));assert(result.includes(label));assert(result.includes('分析旅行预算'));assert(!result.includes('模型私有思维'));});
}
test('greeting goal has no fabricated execution steps',()=>{const g=graph('completed');g.goals[0].text='你好';assert(!html(g).includes('实际工具记录'));});
test('domain receipt and real dependency appear together',()=>{const g=graph('completed');g.goals[0].receipt_refs=['proposal:p1'];g.nodes=[{id:'s1',title:'读取账户',status:'completed',depends_on:[]},{id:'s2',title:'准备转账',status:'completed',depends_on:['s1'],domain_status:'executed'}];const result=html(g);assert(result.includes('已取得业务回执'));assert(result.includes('前置调用：读取账户'));assert(result.includes('已执行'));});
test('legacy workflow fallback remains supported',()=>{const result=html(undefined,{objective:'历史分析',status:'completed',read_only:true,nodes:[{id:'s1',title:'读取账户',status:'succeeded',depends_on:[]}]});assert(result.includes('历史分析'));assert(result.includes('读取账户'));});
test('long untrusted goal is escaped',()=>{const g=graph();g.goals[0].text='<script>attack()</script>'+('长目标'.repeat(80));const result=html(g);assert(!result.includes('<script>'));assert(result.includes('&lt;script&gt;'));});
test('corrected rejected attempt stays visible without a false completion label',()=>{const g=graph('waiting_input');g.nodes=[{id:'s1',title:'精确计算',status:'failed',depends_on:[],recovered_by:'s2',error_code:'INVALID_CALCULATION'}];const result=html(g);assert(result.includes('参数已修正'));assert(result.includes('INVALID_CALCULATION'));});

test('invalidated result remains visible as historical, never current completed evidence',()=>{const g=graph();g.nodes=[{id:'s1',title:'旧预算计算',status:'completed',depends_on:[],invalidated:true}];assert(html(g).includes('已失效（保留记录）'));});
test('unknown outcome explicitly waits verification',()=>{assert(html(graph('waiting_verification')).includes('结果待核实'));});

function answer(id,status,goal='g1',version=2){return {id,name:'respond',title:'整理回答',status,goal_ids:[goal],plan_version:version,depends_on:[],policy:{effect:'answer'}};}
test('answer retries are separate from actual tool records',()=>{
 const g=graph('completed');g.nodes=[{id:'s1',name:'list_accounts',title:'读取账户',status:'completed',depends_on:[]}, {...answer('s2','failed'),error_code:'ANALYSIS_ANSWER_INVALID'},answer('s3','completed')];
 const result=html(g),tools=result.match(/<details[^>]*data-testid="execution-tools"[\s\S]*?<\/details>/)[0];
 assert(tools.includes('读取账户'));assert(!tools.includes('整理回答'));assert(result.includes('回答已重新生成'));assert(result.includes('ANALYSIS_ANSWER_INVALID'));assert(!result.includes('data-testid="answer-adjustments" open'));
});
test('unresolved answer rejection is never labelled corrected',()=>{
 const g=graph('waiting_input');g.nodes=[{...answer('s1','failed'),error_code:'ANALYSIS_REFERENCE'}];const result=html(g);assert(result.includes('回答仍待调整'));assert(!result.includes('回答已重新生成'));
});
test('a successful answer to another goal does not repair the first',()=>{
 const g=graph('waiting_input');g.nodes=[answer('s1','failed'),answer('s2','completed','other')];assert(!html(g).includes('回答已重新生成'));
});
test('a later plan version does not silently repair an older answer',()=>{
 const g=graph('waiting_input');g.nodes=[answer('s1','failed'),answer('s2','completed','g1',3)];assert(!html(g).includes('回答已重新生成'));
});
test('invalidated answer is preserved as history',()=>{
 const g=graph();g.nodes=[{...answer('s1','failed'),invalidated:true},answer('s2','completed')];assert(html(g).includes('历史回答（已失效）'));assert(!html(g).includes('回答已重新生成'));
});
test('invalidated successful answer is not presented as current validation',()=>{
 const g=graph();g.nodes=[answer('s1','failed'),{...answer('s2','completed'),invalidated:true}];const result=html(g);assert(result.includes('历史回答（已失效）'));assert(!result.includes('回答通过校验'));assert(!result.includes('回答已重新生成'));
});
test('real read failure remains a visible business failure',()=>{
 const g=graph('failed');g.nodes=[{id:'s1',name:'list_accounts',title:'读取账户',status:'failed',depends_on:[],error_code:'READ_TIMEOUT'}];const result=html(g);assert(result.includes('失败，可处理后继续'));assert(result.includes('READ_TIMEOUT'));assert(!result.includes('回答调整记录'));
});
