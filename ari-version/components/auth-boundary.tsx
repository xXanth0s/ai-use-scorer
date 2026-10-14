'use client';

import { Show, SignInButton, SignUpButton, UserButton } from '@clerk/nextjs';
import { Layers, ShieldCheck } from 'lucide-react';

/** Sign-in opens the automatically provisioned private workspace. */
export function AuthBoundary({ children }: { children: React.ReactNode }) {
  return <><Show when="signed-in">{children}</Show><Show when="signed-out"><main className="auth-page"><section className="auth-intro"><span className="auth-brand"><Layers size={22}/>how do you ai</span><span className="section-kicker">YOUR PRIVATE AI WORKSPACE</span><h1>Understand how you<br/>work with AI.</h1><p>Sign in to connect your computer and see the patterns in your agent sessions.</p><p className="auth-note"><ShieldCheck size={17}/>Start with 2 daily reports. No invitation or API key needed.</p><div className="auth-actions"><SignUpButton mode="modal"><button className="button">Create an account</button></SignUpButton><SignInButton mode="modal"><button className="button secondary">Sign in</button></SignInButton></div></section></main></Show></>;
}

export function AuthControls() {
  return <div className="auth-controls"><Show when="signed-in"><span>Your account</span><UserButton/></Show></div>;
}
