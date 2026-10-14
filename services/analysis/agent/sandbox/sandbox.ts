import { defineSandbox } from 'eve/sandbox';
import { NoExecution } from '../lib/no-execution';
export const environment=NoExecution.environment();
export default defineSandbox(()=>environment.open());
