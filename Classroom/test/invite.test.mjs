import test from 'node:test';
import assert from 'node:assert/strict';
import { CanJoinPreview, ConnectionText, JoinText, LastConnection, ReceiptText, ShareAddress, ShareAddressLabel, ShareAddresses } from '../src/invite.ts';
import { IPv6Summary, InviteEndpointText } from '../src/network.ts';

test('sharing requires an explicit candidate and never falls back to a different address',()=>{
  const prepared={invite:{seeds:['[240e::1]:8787','192.168.1.2:8787']},token:'NORN1:legacy'};
  assert.equal(ShareAddress(prepared),'');
  assert.equal(ShareAddress(prepared,'[240e::1]:8787'),'[240e::1]:8787');
  assert.equal(ShareAddress(prepared,'192.168.1.2:8787'),'192.168.1.2:8787');
  assert.equal(ShareAddress(prepared,'old:8787'),'');
  assert.equal(ShareAddress({invite:{seeds:['192.168.1.2:8787']}}),'');
  assert.equal(ShareAddress({invite:{seeds:[...prepared.invite.seeds].reverse()}},'[240e::1]:8787'),'[240e::1]:8787');
  assert.equal(ShareAddress({invite:{seeds:['192.168.1.2:8787']}},'[240e::1]:8787'),'');
  assert.equal(ShareAddress(null),'');
});

test('share candidates retain custom endpoints and label network scope without claiming reachability',()=>{
  const prepared={invite:{seeds:['[240e::1]:8787','192.168.1.2:8787','[240e::1]:8787']},connectivity:{addresses:[
    {endpoint:'[240e::1]:8787',scope:'internet_candidate',interface:'Ethernet'},
    {endpoint:'192.168.1.2:8787',scope:'lan',interface:'Wi-Fi'},
  ]}};
  assert.deepEqual(ShareAddresses(prepared),['[240e::1]:8787','192.168.1.2:8787']);
  assert.equal(ShareAddressLabel(prepared,'[240e::1]:8787'),'[240e::1]:8787 · 公网候选 · Ethernet');
  assert.equal(ShareAddressLabel(prepared,'192.168.1.2:8787'),'192.168.1.2:8787 · 局域网 · Wi-Fi');
  const manual={invite:{scope:'custom',seeds:['chat.example:8787']}};
  assert.equal(ShareAddress(manual,'chat.example:8787'),'chat.example:8787');
  assert.equal(ShareAddressLabel(manual,'chat.example:8787'),'chat.example:8787 · 自定义入口');
});

test('opening join prefills the last connection while preserving a draft or received invitation',()=>{
  const status={last_connection:' [240e::1]:8787 '};
  assert.equal(LastConnection(status),'[240e::1]:8787');
  assert.equal(JoinText('',status),'[240e::1]:8787');
  assert.equal(JoinText('  ',status),'[240e::1]:8787');
  assert.equal(JoinText('192.168.1.2:8787',status),'192.168.1.2:8787');
  assert.equal(JoinText('NORN1:received',status),'NORN1:received');
  assert.equal(LastConnection({}),'');
  assert.equal(LastConnection({last_connection:null}),'');
  assert.equal(JoinText('',{}),'');
});
test('link-local IPv6 and a missing default route are distinct from IPv6 Internet access',()=>{
  assert.match(IPv6Summary({ipv6:{global_addresses:0,link_local_addresses:1,default_route:false,route_checked:true}}),/链路本地/);
  assert.match(IPv6Summary({ipv6:{global_addresses:1,link_local_addresses:1,default_route:false,route_checked:true}}),/没有.*默认路由/);
  assert.match(IPv6Summary({ipv6:{global_addresses:1,default_route:true,route_checked:true}}),/待验证/);
  assert.equal(InviteEndpointText({code:'refused'}),'端口拒绝连接');
  assert.equal(InviteEndpointText({code:'no_route'}),'无可用路由');
});
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
