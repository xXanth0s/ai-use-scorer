import { test } from 'node:test';
import assert from 'node:assert/strict';
import { privateWebPath, sealWebCredential, unsealWebCredential, webAuthEnabled, webIdentity } from '../lib/web-auth';

test('Clerk is enabled only for a configured hosted app; private routes include all APIs', () => {
  const keys = { CLERK_SECRET_KEY: 'sk_test_synthetic', NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: 'pk_test_synthetic' };
  assert.equal(webAuthEnabled({ ...keys, HDYAI_HOSTED: '1' }), true);
  for (const env of [{ ...keys }, { HDYAI_HOSTED: '1' }, { ...keys, HDYAI_HOSTED: '0' }, { HDYAI_HOSTED: '1', CLERK_SECRET_KEY: keys.CLERK_SECRET_KEY }]) assert.equal(webAuthEnabled(env), false);
  for (const path of ['/daily', '/reports', '/reports/secret', '/sessions', '/settings', '/api/setup', '/api/reports/x', '/api/plugin-setup']) assert.equal(privateWebPath(path), true);
  for (const path of ['/', '/setup', '/sign-in', '/sign-up', '/architecture', '/plugins/how-do-you-ai.zip', '/reports-public']) assert.equal(privateWebPath(path), false);
});

test('browser credentials are encrypted, account-bound, expiring, and fail closed', async () => {
  const old = { hosted: process.env.HDYAI_HOSTED, secret: process.env.CLERK_SECRET_KEY, publicKey: process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY };
  const clock = Date.now;
  const token = `hdyai_${'a'.repeat(43)}`, user = 'user_syntheticA';
  let now = 1800000000000;
  try {
    process.env.HDYAI_HOSTED = '1'; process.env.CLERK_SECRET_KEY = 'sk_test_synthetic'; process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY = 'pk_test_synthetic';
    Date.now = () => now;
    const sealed = sealWebCredential(user, token);
    assert.equal(unsealWebCredential(user, sealed), token);
    assert.notEqual(sealWebCredential(user, token), sealed);
    assert.equal(sealed.includes(token), false);
    assert.equal(Buffer.from(sealed.slice(3), 'base64url').includes(Buffer.from(token)), false);
    assert.equal(unsealWebCredential('user_syntheticB', sealed), null);
    assert.equal(unsealWebCredential(user, token), null);
    const bytes = Buffer.from(sealed.slice(3), 'base64url');
    for (const index of [0, 12, 28, bytes.length - 1]) { const changed = Buffer.from(bytes); changed[index] ^= 1; assert.equal(unsealWebCredential(user, `v1.${changed.toString('base64url')}`), null); }
    assert.equal(unsealWebCredential(user, `${sealed}=`), null);
    assert.equal(unsealWebCredential(user, 'v1.invalid'), null);
    assert.throws(() => sealWebCredential('ari', token), /Invalid/);
    assert.throws(() => sealWebCredential(user, 'invalid'), /Invalid/);
    process.env.CLERK_SECRET_KEY = 'sk_test_other'; assert.equal(unsealWebCredential(user, sealed), null);
    process.env.CLERK_SECRET_KEY = 'sk_test_synthetic';
    now += 30 * 24 * 60 * 60 * 1000; assert.equal(unsealWebCredential(user, sealed), null);
    now -= 30 * 24 * 60 * 60 * 1000 + 1; assert.equal(unsealWebCredential(user, sealed), null);
    delete process.env.CLERK_SECRET_KEY;
    assert.equal(unsealWebCredential(user, sealed), null);
    assert.throws(() => sealWebCredential(user, token), /temporarily unavailable/);
    await assert.rejects(webIdentity(), /temporarily unavailable/);
    process.env.HDYAI_HOSTED = '0'; assert.equal(await webIdentity(), null);
  } finally {
    Date.now = clock;
    for (const [key, value] of Object.entries({ HDYAI_HOSTED: old.hosted, CLERK_SECRET_KEY: old.secret, NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: old.publicKey })) { if (value === undefined) delete process.env[key]; else process.env[key] = value; }
  }
});
