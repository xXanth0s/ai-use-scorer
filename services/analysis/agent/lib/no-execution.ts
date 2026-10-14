import type { SandboxSession } from 'eve/sandbox';
import { defineSandboxProvider } from 'eve/sandbox/provider';
// Assessments need inference only. Even an accidental future sandbox request
// fails closed; there is no container to start, prewarm, or bill.
export const NoExecution=defineSandboxProvider<Record<string,never>,Record<string,never>,Record<string,never>,Record<string,never>,SandboxSession>({
 name:'assessment-no-execution',
 environment:()=>({
  async prepare(){return {};},
  async start(){throw new Error('Execution is disabled for the assessment agent.');},
  async resume(){throw new Error('Execution is disabled for the assessment agent.');},
  async onSessionEnd(){},
 }),
});
