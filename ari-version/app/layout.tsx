import type { Metadata } from 'next';
import { ClerkProvider } from '@clerk/nextjs';
import { AuthControls } from '@/components/auth-boundary';
import { webAuthEnabled } from '@/lib/web-auth';
import './globals.css';
import './dashboard.css';
import '../components/auth.css';
export const metadata:Metadata={title:'how do you ai — AI session analysis',description:'Review your agent sessions and understand how you work with AI.'};
export default function Layout({children}:{children:React.ReactNode}){return <html lang="en"><body>{webAuthEnabled()?<ClerkProvider signInUrl="/sign-in" signUpUrl="/sign-up" signInFallbackRedirectUrl="/setup" signUpFallbackRedirectUrl="/setup" appearance={{variables:{colorPrimary:'#4265dc',borderRadius:'0.75rem',fontFamily:'Arial, Helvetica, sans-serif'}}}>{children}<AuthControls/></ClerkProvider>:children}</body></html>;}
