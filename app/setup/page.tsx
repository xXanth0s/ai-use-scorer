import { AnalysisSettings } from '@/components/analysis-settings';
import { HostedSetup } from '@/components/hosted-setup';
import { AuthBoundary } from '@/components/auth-boundary';
import { isHosted } from '@/lib/hosted';
import { webAuthEnabled } from '@/lib/web-auth';
export const dynamic='force-dynamic';
export default function Page(){if(!isHosted())return <AnalysisSettings/>;return webAuthEnabled()?<AuthBoundary><HostedSetup/></AuthBoundary>:<main className="setup-page"><h1>Sign-in is being configured.</h1><p>Please check back shortly. Your private sessions remain protected.</p></main>;}
