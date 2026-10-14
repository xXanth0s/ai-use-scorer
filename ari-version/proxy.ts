import { clerkMiddleware } from '@clerk/nextjs/server';
import { NextResponse, type NextRequest, type NextFetchEvent } from 'next/server';
import { privateWebPath, webAuthEnabled } from './lib/web-auth';

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

export default function proxy(request: NextRequest, event: NextFetchEvent) {
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
