import { build } from 'esbuild';
import { mkdir, readFile, writeFile, readdir } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { zip } from '../lib/zip';
const built=await build({entryPoints:['scripts/plugin-worker.ts'],bundle:true,platform:'node',target:'node24',format:'esm',write:false});
const outputDir=resolve(process.argv[2]||'public/plugins');
const prefix='plugins/how-do-you-ai',files:{name:string;data:Buffer}[]=[];
async function add(root:string,path=''){for(const entry of await readdir(join(root,path),{withFileTypes:true})){const name=join(path,entry.name);if(entry.isDirectory())await add(root,name);else files.push({name:`${prefix}/${name}`,data:await readFile(join(root,name))});}}
// Package only reviewed plugin assets, never stray transcripts or local credentials.
for(const path of ['.codex-plugin','.claude-plugin','hooks','skills'])await add(prefix,path);
files.push({name:`${prefix}/scripts/run.sh`,data:await readFile(join(prefix,'scripts/run.sh'))});
files.push({name:`${prefix}/scripts/worker.mjs`,data:Buffer.from(built.outputFiles[0].contents)});
files.push({name:`${prefix}/scripts/capture-hook.mjs`,data:await readFile('scripts/capture-hook.mjs')});
files.push({name:`${prefix}/THIRD_PARTY_NOTICES.md`,data:await readFile('THIRD_PARTY_NOTICES.md')});
files.push({name:'.claude-plugin/marketplace.json',data:Buffer.from(JSON.stringify({name:'how-do-you-ai',owner:{name:'how do you ai'},plugins:[{name:'how-do-you-ai',source:`./${prefix}`,description:'Local session capture and hosted daily reports',version:'0.1.0'}]}))});
files.push({name:'.agents/plugins/marketplace.json',data:Buffer.from(JSON.stringify({name:'how-do-you-ai',interface:{displayName:'how do you ai'},plugins:[{name:'how-do-you-ai',source:{source:'local',path:`./${prefix}`},policy:{installation:'AVAILABLE',authentication:'ON_USE'},category:'Productivity'}]}))});
const archive=zip(files),sha256=createHash('sha256').update(archive).digest('hex');await mkdir(outputDir,{recursive:true});await writeFile(join(outputDir,'how-do-you-ai.zip'),archive);await writeFile(join(outputDir,'release.json'),JSON.stringify({version:'0.1.0',sha256,url:'/plugins/how-do-you-ai.zip',platforms:['macOS','Linux'],runtime:'Node.js 24+'}));
console.log(`Plugin package built: ${archive.length} bytes, SHA-256 ${sha256}`);
