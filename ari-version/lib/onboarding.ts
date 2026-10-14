export const defaultAnalysisService='https://how-do-you-ai-analysis.vercel.app';
export function invitationLink(activationCode:string,origin=process.env.HDYAI_WEB_URL||'http://127.0.0.1:3000'){
 if(!/^invite_[A-Za-z0-9_-]{43}$/.test(activationCode))throw new Error('Invalid invitation.');
 // Fragments are not sent to servers or included in HTTP request/referrer logs.
 const base=new URL(origin);if(base.protocol!=='https:'&&!['localhost','127.0.0.1'].includes(base.hostname))throw new Error('Use HTTPS for the hosted dashboard.');
 return `${base.origin}/setup#invite=${encodeURIComponent(activationCode)}`;
}
export function invitationFromFragment(fragment:string){const code=new URLSearchParams(fragment.replace(/^#/, '')).get('invite');return code&&/^invite_[A-Za-z0-9_-]{43}$/.test(code)?code:null;}
