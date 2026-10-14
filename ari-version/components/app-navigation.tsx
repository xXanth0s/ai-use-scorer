'use client';
import Link from 'next/link';
import { Activity, Layers, FileText, Workflow, ShieldCheck, Settings2 } from 'lucide-react';
export function AppNavigation({active,hosted=false}:{active:'daily'|'sessions'|'reports'|'architecture'|'settings';hosted?:boolean}){
 return <nav className="app-navigation" aria-label="Main navigation"><Link className="trace-logo" href="/daily"><span><Layers size={21}/></span>how do you ai<span className="local-tag">LOCAL</span></Link><div className="app-nav-links">{[{id:'daily',label:'Daily analysis',icon:Activity},{id:'sessions',label:'Sessions',icon:Layers},{id:'reports',label:'Report history',icon:FileText},{id:'architecture',label:'How it works',icon:Workflow},{id:'settings',label:'Setup & settings',icon:Settings2}].map(n=><Link key={n.id} href={n.id==='architecture'?'/architecture':`/${n.id}`} aria-current={active===n.id?'page':undefined}><n.icon size={16}/><span>{n.label}</span></Link>)}</div><span className="local-status"><ShieldCheck size={14}/>{hosted?'Private dashboard':'Local workspace'}</span></nav>;
}
