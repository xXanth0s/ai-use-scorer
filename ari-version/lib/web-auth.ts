import { createCipheriv, createDecipheriv, createHash, randomBytes } from 'node:crypto';

type AuthEnvironment = Record<string, string | undefined>;

export function webAuthEnabled(env: AuthEnvironment = process.env): boolean {
  return env.HDYAI_HOSTED === '1' && !!env.CLERK_SECRET_KEY && !!env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;
}

export function privateWebPath(path: string): boolean {
  return /^\/(daily|reports|sessions|settings)(\/|$)/.test(path) || path.startsWith('/api/');
}

export async function webIdentity(): Promise<{ userId: string; sessionId?: string } | null> {
  if (process.env.HDYAI_HOSTED !== '1') return null;
  if (!webAuthEnabled()) throw new Error('Sign-in is temporarily unavailable.');
  const { auth } = await import('@clerk/nextjs/server');
  const session = await auth();
  return session.userId ? { userId: session.userId, sessionId: session.sessionId || undefined } : null;
}

export async function requireWebIdentity(): Promise<{ userId: string }> {
  const identity = await webIdentity();
  if (!identity) throw new Error('Sign in to continue.');
  return identity;
}

const maxAge = 30 * 24 * 60 * 60 * 1000;
function credentialKey() {
  if (!webAuthEnabled()) throw new Error('Sign-in is temporarily unavailable.');
  return createHash('sha256').update('hdyai-web-v1\0').update(process.env.CLERK_SECRET_KEY!).digest();
}

/** The plugin credential stays encrypted and can only be used by this Clerk account. */
export function sealWebCredential(userId: string, deviceToken: string): string {
  if (!/^user_[A-Za-z0-9_-]{1,180}$/.test(userId) || !/^hdyai_[A-Za-z0-9_-]{43}$/.test(deviceToken)) throw new Error('Invalid browser connection.');
  const nonce = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', credentialKey(), nonce);
  cipher.setAAD(Buffer.from('hdyai-web-v1'));
  const payload = Buffer.from(JSON.stringify({ v: 1, userId, deviceToken, expiresAt: Date.now() + maxAge }));
  const ciphertext = Buffer.concat([cipher.update(payload), cipher.final()]);
  return `v1.${Buffer.concat([nonce, cipher.getAuthTag(), ciphertext]).toString('base64url')}`;
}

export function unsealWebCredential(userId: string, cookie: string): string | null {
  try {
    if (!/^v1\.[A-Za-z0-9_-]{60,1600}$/.test(cookie)) return null;
    const encoded = cookie.slice(3), bytes = Buffer.from(encoded, 'base64url');
    if (bytes.toString('base64url') !== encoded) return null;
    const decipher = createDecipheriv('aes-256-gcm', credentialKey(), bytes.subarray(0, 12));
    decipher.setAAD(Buffer.from('hdyai-web-v1'));
    decipher.setAuthTag(bytes.subarray(12, 28));
    const payload = JSON.parse(Buffer.concat([decipher.update(bytes.subarray(28)), decipher.final()]).toString('utf8'));
    const now = Date.now();
    return payload.v === 1 && payload.userId === userId && /^user_[A-Za-z0-9_-]{1,180}$/.test(userId)
      && /^hdyai_[A-Za-z0-9_-]{43}$/.test(payload.deviceToken) && Number.isSafeInteger(payload.expiresAt)
      && payload.expiresAt > now && payload.expiresAt <= now + maxAge ? payload.deviceToken : null;
  } catch { return null; }
}

