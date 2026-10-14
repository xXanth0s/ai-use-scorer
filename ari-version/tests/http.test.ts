import { test } from 'node:test';
import assert from 'node:assert/strict';
import { assertLocal } from '../lib/http';
test('local Next internal URL can differ from the browser host without rejecting same-origin requests',()=>{assert.doesNotThrow(()=>assertLocal(new Request('http://localhost:3000/api/reports',{headers:{host:'127.0.0.1:3000',origin:'http://127.0.0.1:3000'}})));});
test('cross-site requests and remote hosts are rejected',()=>{assert.throws(()=>assertLocal(new Request('http://localhost:3000',{headers:{host:'127.0.0.1:3000',origin:'https://attacker.example'}})));assert.throws(()=>assertLocal(new Request('http://remote.example',{headers:{host:'remote.example'}})));});
