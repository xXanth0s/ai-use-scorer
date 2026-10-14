import {test} from 'node:test';
import assert from 'node:assert/strict';
import {compactTokens} from '../lib/format';
test('tokens use K/M/B with stable rounded boundaries',()=>{
 for(const [input,expected] of [[0,'0'],[999,'999'],[999.99,'1K'],[1000,'1K'],[1250,'1.3K'],[999949,'999.9K'],[999950,'1M'],[1e6,'1M'],[1250000,'1.3M'],[999950000,'1B'],[1e9,'1B'],[2.34e9,'2.3B']] as const) assert.equal(compactTokens(input),expected);
});
test('missing and invalid counts are unavailable',()=>{for(const v of [null,undefined,NaN,Infinity,-1])assert.equal(compactTokens(v),'—');});
