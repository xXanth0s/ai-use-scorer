import { eveChannel } from 'eve/channels/eve';
// Platform clients use the bounded analysis channel. Never expose a free-form
// Eve session route that could bypass the rubric, quotas, or ownership checks.
export default eveChannel({auth:[]});
