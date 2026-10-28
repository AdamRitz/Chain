const fs = require('fs');
const path = require('path');
const solc = require('solc');
if (!solc.version().startsWith('0.8.28+')) throw Error('Use Solidity 0.8.28');
const root = path.resolve(__dirname, '../..');
const input = {language:'Solidity', sources:{'ClassroomChat.sol':{content:fs.readFileSync(path.join(root,'Contracts/ClassroomChat.sol'),'utf8')}},
  settings:{optimizer:{enabled:true,runs:200},evmVersion:'shanghai',outputSelection:{'*':{'*':['abi','evm.bytecode.object','evm.deployedBytecode.object','evm.methodIdentifiers']}}}};
const output = JSON.parse(solc.compile(JSON.stringify(input)));
for (const e of output.errors || []) if(e.severity==='error') throw Error(e.formattedMessage);
fs.writeFileSync(path.join(root,'Contracts/ClassroomChat.compiled.json'),JSON.stringify({compiler:solc.version(),settings:input.settings,contracts:output.contracts['ClassroomChat.sol']},null,2)+'\n');
console.log('ClassroomChat compiled for Shanghai');
