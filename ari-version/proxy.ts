import { clerkMiddleware } from '@clerk/nextjs/server';
import { NextResponse, type NextRequest, type NextFetchEvent } from 'next/server';
import { privateWebPath, webAuthEnabled } from './lib/web-auth';
import { DEMO_API, DEMO_HOME, isDemo } from './lib/demo';

const withClerk = clerkMiddleware(async (auth, request) => {
  if (!privateWebPath(request.nextUrl.pathname)) return;
  const session = await auth();
  if (session.userId) return;
  if (request.nextUrl.pathname.startsWith('/api/')) {
    return NextResponse.json({ error: 'Sign in to continue.' }, { status: 401, headers: { 'cache-control': 'no-store' } });
  }
  // Invitation fragments stay in the browser; no private value belongs in a redirect URL.
  return NextResponse.redirect(new URL('/sign-in', request.url));
});

function demoProxy(request: NextRequest) {
  const path = request.nextUrl.pathname;
  if (path.startsWith('/api/')) {
    const allowed = DEMO_API.some(r => r.test(path)) && (request.method === 'GET' || path === '/api/analyst');
    return allowed ? NextResponse.next() : NextResponse.json({ error: 'This demo is read-only.' }, { status: 403 });
  }
  // Collection, setup and live daily runs need a local machine; send everything else to the bundled report.
  if (path === '/' || /^\/(daily|setup|settings|sign-in|sign-up)(\/|$)/.test(path)) return NextResponse.redirect(new URL(DEMO_HOME, request.url));
  return NextResponse.next();
}

export default function proxy(request: NextRequest, event: NextFetchEvent) {
  if (isDemo()) return demoProxy(request);
  if (process.env.HDYAI_HOSTED !== '1') return NextResponse.next();
  if (webAuthEnabled()) return withClerk(request, event);
  if (privateWebPath(request.nextUrl.pathname)) {
    return NextResponse.json({ error: 'Sign-in is temporarily unavailable.' }, { status: 503, headers: { 'cache-control': 'no-store' } });
  }
  return NextResponse.next();
}

export const config = {
  matcher: ['/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)', '/(api|trpc)(.*)'],
};
