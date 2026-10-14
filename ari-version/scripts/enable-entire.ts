import { resolve } from 'node:path';
import { enableEntire } from '../lib/entire-hooks';
import { getState, setState } from '../lib/db';
const repo=resolve(process.argv[2]||process.cwd());
const result=await enableEntire(repo,['claude-code','codex'],process.argv.includes('--init-repo'));
const settings=getState('settings')||{};
setState('settings',{...settings,entireRepos:[...new Set([...(settings.entireRepos||[]),result.repository])]});
console.log(JSON.stringify(result));
