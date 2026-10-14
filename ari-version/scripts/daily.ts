import { collect } from '../lib/collector';
import { getReport } from '../lib/db';
import { dayOf, prepareDaily } from '../lib/daily';
import { generateReport } from '../lib/report';
const today=dayOf(new Date().toISOString());
const dateArg=process.argv.find(x=>/^\d{4}-\d{2}-\d{2}$/.test(x));
const target=dateArg||new Date(Date.parse(`${today}T12:00:00Z`)-86400000).toISOString().slice(0,10);
await collect();
const result=prepareDaily(target,process.argv.includes('--force'));
if(!result){console.log(JSON.stringify({status:'no-sessions',date:target}));process.exit(0);}
if(!result.fresh){console.log(JSON.stringify({status:result.report.status==='complete'?'already-generated':'in-progress',date:target,reportIds:[result.report.id]}));process.exit(0);}
await generateReport(result.report.id);
const report=getReport(result.report.id)!;
if(report.status!=='complete')throw new Error(report.error||'Daily report failed.');
console.log(JSON.stringify({status:'generated',date:target,sessions:report.sessionIds.length,reportIds:[report.id]}));
