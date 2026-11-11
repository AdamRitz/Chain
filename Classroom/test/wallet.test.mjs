import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ReadWalletBackup, WalletActions, WalletIdentityChanged } from '../src/wallet.ts';
import { ChainDrafts, NetworkView, NetworkRequest } from '../src/model.ts';

test('wallet backup file accepts encrypted legacy format and rejects plaintext or oversized input',()=>{
  const encrypted={version:1,kdf:'argon2id-interactive',salt:'ab'.repeat(16),nonce:'cd'.repeat(24),cipher:'ef'.repeat(48)};
  assert.deepEqual(ReadWalletBackup(JSON.stringify(encrypted)),encrypted);
  for(const value of ['bad json','null',JSON.stringify({main:'secret-seed'}),JSON.stringify({...encrypted,nonce:'00'}),' '.repeat(65537)])
    assert.throws(()=>ReadWalletBackup(value));
});

test('a changed wallet session invalidates the same chain view and overrides caller guard values',()=>{
  const view=new NetworkView();view.Select('A',1);
  const old=view.Capture();view.Select('A',2);view.Select('A',3);
  assert.equal(view.Current(old),false);
  assert.equal(NetworkRequest('message',{wallet_session:1},view.Capture()).wallet_session,3);
  for(const name of ['logout','wallet_select','wallet_import','wallet_add','select_wallet']) assert(WalletActions.has(name));
});

test('logout clears drafts that could be signed with another wallet while preserving saved chain inputs',()=>{
  const drafts=new ChainDrafts();
  for(const id of ['A','B']) drafts.Write(id,{body:'old sender message',receiver:'old payee',amount:'77',roomName:'old group',title:id,invite:'public-node:12',view:'wallet'});
  drafts.ClearWallet();
  for(const id of ['A','B']) {
    const value=drafts.Read(id);
    assert.equal(value.body,'');assert.equal(value.receiver,'');assert.equal(value.roomName,'');assert.equal(value.amount,'10000000');
    assert.equal(value.title,id);assert.equal(value.invite,'public-node:12');assert.equal(value.view,'wallet');
  }
});

test('same-wallet chain switching preserves drafts while logout and another wallet clear them',()=>{
  const before={network:{id:'A'},selected_wallet_id:'wallet-A',active_wallet:'main',unlocked:true,wallet_session:2};
  assert.equal(WalletIdentityChanged(before,{...before,network:{id:'B'},wallet_session:3}),false);
  assert.equal(WalletIdentityChanged(before,{...before,unlocked:false,wallet_session:3}),true);
  assert.equal(WalletIdentityChanged(before,{...before,selected_wallet_id:'wallet-B',wallet_session:3}),true);
  assert.equal(WalletIdentityChanged(before,{...before,active_wallet:'another',wallet_session:3}),true);
});
