import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { StaleViewError } from '../src/model.ts';

// Only public test values are used. Fetch is replaced before each request.
globalThis.location = {hash:'',pathname:'/'};
globalThis.sessionStorage = {getItem:()=>'',setItem:()=>{}};
globalThis.history = {replaceState:()=>{}};
const {api,apiView} = await import('../src/api.ts');
const originalFetch = globalThis.fetch;
afterEach(()=>{globalThis.fetch=originalFetch;});

test('switch requests carry the source network id and subsequent transactions carry the selected id', async()=>{
  const requests=[];
  globalThis.fetch=async(_url,options)=>{
    requests.push(JSON.parse(options.body));
    return Response.json({has_wallet:true,network:{id:'B'}});
  };
  apiView.Select('A');
  const status=await api('network_switch',{id:'B'});
  assert.deepEqual(requests[0],{action:'network_switch',id:'B',network_id:'A'});
  apiView.Select(status.network.id);
  await api('message',{room:1,body:'B message'});
  assert.equal(requests[1].network_id,'B');
});

test('a late successful source-chain response is rejected after switching views', async()=>{
  let release;
  globalThis.fetch=()=>new Promise(resolve=>{release=resolve;});
  apiView.Select('A');
  const pending=api('message',{body:'source draft'});
  apiView.Invalidate(); apiView.Select('B');
  release(Response.json({hash:'source-transaction'}));
  await assert.rejects(pending,StaleViewError);
});

test('returning to A does not accept history from its earlier view', async()=>{
  let release;
  globalThis.fetch=()=>new Promise(resolve=>{release=resolve;});
  apiView.Select('A');
  const pending=api('history',{offset:50});
  apiView.Select('B'); apiView.Select('A');
  release(Response.json([{body:'obsolete history'}]));
  await assert.rejects(pending,StaleViewError);
});

test('obsolete network failures are suppressed while current action errors remain visible', async()=>{
  let reject;
  globalThis.fetch=()=>new Promise((_resolve,failure)=>{reject=failure;});
  apiView.Select('A');
  const pending=api('block',{height:1});
  apiView.Select('B'); reject(Error('obsolete connection failure'));
  await assert.rejects(pending,StaleViewError);
  globalThis.fetch=async()=>Response.json({error:'代币不足'},{status:400});
  await assert.rejects(api('message',{body:'current message'}),/代币不足/);
});

test('wallet mutations bind the current session and reject an old response after logout and relogin on the same chain', async()=>{
  let release, submitted;
  globalThis.fetch=(_url,options)=>{submitted=JSON.parse(options.body);return new Promise(resolve=>{release=resolve;});};
  apiView.Select('wallet-chain',41);
  const pending=api('transfer',{receiver:'public-test-key',wallet_session:0});
  assert.equal(submitted.wallet_session,41);
  apiView.Select('wallet-chain',42); apiView.Select('wallet-chain',43);
  release(Response.json({hash:'old-wallet-transfer'}));
  await assert.rejects(pending,StaleViewError);
});
