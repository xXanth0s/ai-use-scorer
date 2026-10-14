import { test } from 'node:test';
import assert from 'node:assert/strict';
import { invitationLink, invitationFromFragment } from '../lib/onboarding';
test('invitation links keep credentials in a fragment and reject malformed invitations',()=>{
 const code=`invite_${'a'.repeat(43)}`;const url=new URL(invitationLink(code));assert.equal(url.search,'');assert.equal(url.pathname,'/setup');assert.equal(invitationFromFragment(url.hash),code);
 assert.equal(invitationFromFragment('#invite=bad'),null);assert.throws(()=>invitationLink('not-an-invite'));
});
