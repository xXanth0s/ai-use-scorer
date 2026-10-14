import Link from 'next/link';
import { SignIn, SignUp } from '@clerk/nextjs';
import { Layers, ShieldCheck } from 'lucide-react';
import { webAuthEnabled } from '@/lib/web-auth';

export function AuthScreen({ mode }: { mode: 'sign-in' | 'sign-up' }) {
  return <main className="auth-page"><section className="auth-intro"><Link className="auth-brand" href="/setup"><Layers size={22}/>how do you ai</Link><span className="section-kicker">YOUR PRIVATE AI WORKSPACE</span><h1>{mode === 'sign-in' ? 'Welcome back.' : 'Make room for better\nAI collaboration.'}</h1><p>Connect your agent sessions. Discover your patterns. Build on what works.</p><p className="auth-note"><ShieldCheck size={17}/>Account access is linked through your private invitation.</p><Link className="text-button" href="/setup">Back to setup</Link></section><section className="auth-form" aria-label={mode === 'sign-in' ? 'Sign in' : 'Create an account'}>{webAuthEnabled() ? mode === 'sign-in' ? <SignIn routing="path" path="/sign-in" signUpUrl="/sign-up" fallbackRedirectUrl="/setup"/> : <SignUp routing="path" path="/sign-up" signInUrl="/sign-in" fallbackRedirectUrl="/setup"/> : <div className="auth-unavailable"><h2>{process.env.HDYAI_HOSTED === '1' ? 'Sign-in is being set up.' : 'Your local workspace is ready.'}</h2><p>{process.env.HDYAI_HOSTED === '1' ? 'Account sign-in will be available once setup is complete.' : 'You can use this workspace on your computer without signing in.'}</p><Link className="button secondary" href="/setup">Continue to setup</Link></div>}</section></main>;
}
