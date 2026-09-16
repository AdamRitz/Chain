// node Contracts/Compile.cjs <path to installed solc 0.8.28 module>
const fs = require('fs');
const path = require('path');
const solc = require(process.argv[2] ? path.resolve(process.argv[2]) : 'solc');
if (!solc.version().startsWith('0.8.28+')) throw new Error('Use Solidity 0.8.28');
const source = fs.readFileSync(path.join(__dirname, 'Counter.sol'), 'utf8');
const input = {
    language: 'Solidity', sources: {'Counter.sol': {content: source}},
    settings: {
        optimizer: {enabled: true, runs: 200}, evmVersion: 'shanghai',
        outputSelection: {'*': {'*': ['abi', 'evm.bytecode.object', 'evm.deployedBytecode.object', 'evm.methodIdentifiers']}}
    }
};
const result = JSON.parse(solc.compile(JSON.stringify(input)));
for (const error of result.errors || []) if (error.severity === 'error') throw new Error(error.formattedMessage);
fs.writeFileSync(path.join(__dirname, 'Counter.compiled.json'), JSON.stringify({
    compiler: solc.version(), settings: input.settings, contracts: result.contracts['Counter.sol']
}, null, 2) + '\n');
console.log('Compiled Counter and Caller for Shanghai.');
