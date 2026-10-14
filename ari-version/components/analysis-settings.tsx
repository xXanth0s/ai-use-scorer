'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { ArrowRight, Check, ChevronDown, Circle, Cloud, Fingerprint, Layers, LoaderCircle, LockKeyhole, Settings2, ShieldCheck, Terminal, Unplug } from 'lucide-react';
import { EntireSettings } from './entire-settings';
import { AppNavigation } from './app-navigation';
import { defaultAnalysisService, invitationFromFragment } from '@/lib/onboarding';
type Connection={connected:boolean;url:string;userId:string|null;expiresAt:string|null};
type Setup={connection:Connection;agents:{agent:string;detected:boolean;installed:boolean;issue:string|null}[];reportName:string;sessionCount:number;configured:boolean};
async function request<T>(url:string,method='GET',body?:unknown):Promise<T>{const r=await fetch(url,{method,headers:{'content-type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});const data=await r.json();if(!r.ok)throw new Error(data.error||'Could not complete setup.');return data;}
const steps=['Connect your device','Set up local capture','Find your sessions'];
export function AnalysisSettings(){
 const router=useRouter();
 const [state,setState]=useState<Setup|null>(null),[url,setUrl]=useState(defaultAnalysisService),[code,setCode]=useState(''),[invited,setInvited]=useState(false),[reportName,setReportName]=useState('');
 const [stage,setStage]=useState<number|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notes,setNotes]=useState<string[]>([]),[saved,setSaved]=useState(false);
 useEffect(()=>{
  let alive=true;const fragment=window.location.hash;
  if(fragment){const invite=invitationFromFragment(fragment);if(invite){setCode(invite);setInvited(true);}else setError('This invitation link is incomplete. Open a fresh invitation or paste the code below.');window.history.replaceState(null,'',window.location.pathname);}
  request<Setup>('/api/setup').then(s=>{if(alive){setState(s);setReportName(s.reportName==='Candidate'?'':s.reportName);setUrl(s.connection.url||defaultAnalysisService);}}).catch(e=>{if(alive)setError(e.message);});
  return()=>{alive=false;};
 },[]);
 const ready=!!state?.connection.connected&&state.configured;
 const detected=state?.agents.filter(a=>a.detected)||[];
 async function start(){
  if(!state||busy)return;setBusy(true);setError('');setNotes([]);setStage(0);
  let warnings:string[]=[];
  try{
   if(!state.connection.connected){await request('/api/analysis-connection','POST',{url,activationCode:code.trim()});setCode('');setInvited(false);}
   if(reportName.trim()&&reportName.trim()!==state.reportName)await request('/api/analysis-connection','PATCH',{reportName});
   setStage(1);const capture=await request<{results:{issue:string|null}[]}>('/api/setup','POST');warnings=capture.results.flatMap(r=>r.issue?[r.issue]:[]);
   setStage(2);const collection=await request<{errors:string[]}>('/api/collect','POST');warnings.push(...collection.errors);
   const latest=await request<Setup>('/api/setup');setState(latest);setStage(3);setNotes([...new Set(warnings)]);
   if(!warnings.length&&latest.sessionCount>0)router.push('/daily');
  }catch(e){setError((e as Error).message);const latest=await request<Setup>('/api/setup').catch(()=>null);if(latest)setState(latest);setNotes([...new Set(warnings)]);setStage(null);}
  finally{setBusy(false);}
 }
 async function saveName(){setBusy(true);setError('');setSaved(false);try{await request('/api/analysis-connection','PATCH',{reportName:reportName||'Candidate'});setSaved(true);}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
 async function disconnect(){setBusy(true);setError('');try{await request('/api/analysis-connection','DELETE');setState(await request<Setup>('/api/setup'));}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
 return <><AppNavigation active="settings"/><main className="setup-page">
  <header className="setup-heading"><span className="section-kicker">{ready?'YOUR CONNECTION':'GET STARTED'}</span><h1>{ready?'Your device is ready.':'Connect once. Start working.'}</h1><p>{ready?'Your supported local sessions are available for daily analysis.':'We find Codex and Claude Code sessions on this computer and prepare your daily reports.'}</p></header>
  <div className="setup-layout"><section className="setup-main" aria-label="Device setup">
   <div className={`setup-symbol ${ready?'is-ready':''}`}>{ready?<Check size={28}/>:<Layers size={28}/>}</div>
   <h2>{ready?'Connected to how do you ai':invited?'Your invitation is ready.':state?.connection.connected?'Finish your capture setup.':'Open your invitation to connect.'}</h2>
   <p className="setup-description">{ready?`${state!.sessionCount} sessions in your local library. Use your agents as usual; the next daily analysis picks up their latest work.`:invited?'Your device will be connected securely. No API keys or integration settings to configure.':state?.connection.connected?'We’ll prepare capture for your detected agents and find their existing sessions.':'Your platform administrator provides a private invitation link. You can also paste an invitation code below.'}</p>
   {!state?<p className="setup-checking" role="status"><LoaderCircle size={16} className="spin"/>Checking this computer…</p>:<div className="setup-agents">{state.agents.map(agent=><div key={agent.agent}><span className="setup-agent-icon"><Terminal size={18}/></span><div><strong>{agent.agent==='codex'?'Codex':'Claude Code'}</strong><small>{agent.installed?'Capture hooks installed · approval may be required':agent.detected?'Found on this computer':'No local session folder found'}</small></div>{agent.detected?<Check size={17}/>:<Circle size={15}/>}</div>)}</div>}
   {!ready&&!state?.connection.connected&&!invited?<label className="field setup-invite">Invitation code<input type="password" autoComplete="off" value={code} onChange={e=>setCode(e.target.value)} placeholder="Paste your private invitation code" disabled={busy}/></label>:null}
   {!ready?<label className="field setup-name">Your name <span>optional</span><input value={reportName} maxLength={100} onChange={e=>setReportName(e.target.value)} placeholder="Name shown on your reports" disabled={busy}/></label>:null}
   {error?<p className="setup-error" role="alert">{error}</p>:null}
   {stage!==null?<ol className="setup-progress" aria-live="polite">{steps.map((text,i)=><li key={text} className={stage>i?'done':stage===i?'active':''}>{stage>i?<Check size={15}/>:stage===i?<LoaderCircle size={15} className="spin"/>:<Circle size={15}/>}<span>{text}</span></li>)}</ol>:null}
   {notes.map(note=><p className="setup-warning" key={note} role="status">{note}</p>)}
   {ready?<Link className="button setup-primary" href="/daily">Open daily analysis<ArrowRight size={17}/></Link>:<button className="button setup-primary" disabled={busy||!state||(!state.connection.connected&&!code.trim())} onClick={start}>{busy?<><LoaderCircle size={17} className="spin"/>Setting things up…</>:<>{state?.connection.connected?'Start collecting sessions':'Connect & start'}<ArrowRight size={17}/></>}</button>}
   {ready&&!state?.sessionCount?<p className="setup-hint">Start a session in a supported local agent, then open daily analysis.</p>:null}
   {!ready?<p className="setup-consent">This connects your device, imports local sessions, and adds capture hooks for {detected.length?detected.map(a=>a.agent==='codex'?'Codex':'Claude Code').join(' and '):'detected agents'}. Opening daily analysis sends sampled excerpts to our hosted AI service.</p>:null}
  </section><aside className="setup-context"><span className="section-kicker">WHAT HAPPENS NEXT</span><div><Fingerprint size={21}/><h3>One private connection</h3><p>Your device gets its own revocable access. Model keys stay on our server.</p></div><div><Layers size={21}/><h3>All your local threads</h3><p>Existing sessions import automatically. Hooks save future snapshots, including subagent transcripts where available.</p></div><div><Cloud size={21}/><h3>Your daily report</h3><p>The latest day is selected for you. Unchanged reports reopen instantly.</p></div><p className="setup-boundary"><ShieldCheck size={16}/>Local originals stay here. Reports use a limited, best-effort-redacted sample processed by Eve and AI Gateway.</p></aside></div>
  <details className="setup-advanced"><summary><Settings2 size={16}/>Advanced settings<ChevronDown size={16}/></summary><div className="setup-advanced-body">
   {ready?<div className="setup-profile"><label className="field">Name on your reports<input value={reportName} maxLength={100} onChange={e=>{setReportName(e.target.value);setSaved(false);}}/></label><button className="button secondary" disabled={busy} onClick={saveName}>{saved?'Saved':'Save name'}</button></div>:null}
   <label className="field">Analysis service<input type="url" value={url} onChange={e=>setUrl(e.target.value)} disabled={busy||state?.connection.connected}/></label>
   <p><LockKeyhole size={15}/>Invitations expire after 15 minutes. Device access expires after 30 days{state?.connection.expiresAt?` (${new Date(state.connection.expiresAt).toLocaleDateString()})`:''}. Credentials stay in a private local file and never enter browser storage.</p>
   {ready?<button className="button secondary" onClick={start} disabled={busy}>Repair capture & import sessions</button>:null}
   {state?.connection.connected?<button className="text-button setup-revoke" disabled={busy} onClick={disconnect}><Unplug size={15}/>Disconnect analysis on this device</button>:null}
   <p>Start a new agent session after setup and approve any hook review requested by your agent. Native transcript reading continues when hooks are unavailable. Hooks work while this web app is closed; scheduled analysis needs the local collector running.</p>
   <details className="setup-legacy"><summary>Import older Entire checkpoints</summary><EntireSettings/></details>
  </div></details>
 </main></>;
}
