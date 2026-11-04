import test from 'node:test';
import assert from 'node:assert/strict';
import { CanJoinPreview, ConnectionText, ReceiptText } from '../src/invite.ts';
test('node startup is not reported as a connected and ready chat', () => {
  assert.equal(ConnectionText({running:true,connection_state:{phase:'connecting'}}),'正在连接聊天室');
  assert.equal(ConnectionText({running:true,connection_state:{phase:'syncing'}}),'正在同步聊天记录');
  assert.equal(ConnectionText({connection_state:{phase:'ready'}}),'聊天室已就绪');
});
test('a preview from another input or stale preflight cannot enable joining', () => {
  const preview={id:'fresh',phase:'ready',checked_ms:Date.now()};
  assert.ok(CanJoinPreview(preview,'fresh'));
  assert.equal(CanJoinPreview(preview,'old'),false);
  assert.equal(CanJoinPreview({...preview,phase:'checking'},'fresh'),false);
  assert.equal(CanJoinPreview({...preview,checked_ms:Date.now()-61000},'fresh'),false);
});
test('fee request identifies the target chain as well as the recipient', () => {
  const text=ReceiptText({network:{title:'论文讨论',genesis_root:'root'}},'key');
  assert.ok(text.includes('论文讨论')&&text.includes('root')&&text.includes('key'));
});
