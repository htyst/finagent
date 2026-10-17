import React from 'react';
import {createRoot} from 'react-dom/client';
import AnalyticsReport from '../src/AnalyticsReport';
import FinancialReadReport from '../src/FinancialReadReport';
import SpendingPanel from '../src/SpendingPanel';
import ImportedSubscriptions from '../src/ImportedSubscriptions';
import {useAgentWorkspace} from '../src/useAgentWorkspace';
import '../src/style.css';
const root=createRoot(document.getElementById('root')!);
function HookCase(){const workspace=useAgentWorkspace(async()=>{},0);Object.assign(window,{workspace});return <p>{workspace.loading?'loading':'ready'}</p>;}
Object.assign(window,{renderCase:(name:string,value:any,filters={})=>{
 if(name==='spending')root.render(<SpendingPanel {...value}/>);
 else if(name==='financial')root.render(<FinancialReadReport initial={value} filters={filters}/>);
 else if(name==='imported')root.render(<ImportedSubscriptions data={value}/>);
 else if(name==='hook')root.render(<HookCase/>);
 else root.render(<AnalyticsReport value={value}/>);
}});
