import { readFile } from 'node:fs/promises';
import { neon } from '@neondatabase/serverless';
const url=process.env.DATABASE_URL_UNPOOLED;
if(!url)throw new Error('Load the dedicated analysis service environment before migrating.');
// Schema is authored and versioned; execute it as a single SQL transaction.
const schema=await readFile(new URL('../services/analysis/schema.sql',import.meta.url),'utf8');
const statements:string[]=[];let start=0,inFunction=false;
for(let i=0;i<schema.length;i++){if(schema.slice(i,i+2)==='$$'){inFunction=!inFunction;i++;}else if(schema[i]===';'&&!inFunction){statements.push(schema.slice(start,i).trim());start=i+1;}}
const db=neon(url);await db.transaction(statements.filter(Boolean).map(statement=>db.query(statement)));
console.log('Analysis schema migrated. Credentials were not printed.');
