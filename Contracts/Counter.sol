// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

contract Counter {
    uint256 public value;
    event Changed(uint256 value);

    function Increment() external returns (uint256) {
        value += 1;
        emit Changed(value);
        return value;
    }

    function Set(uint256 next) external {
        value = next;
    }

    function Rollback() external {
        value = 99;
        revert("rollback");
    }
}

contract Caller {
    function Forward(Counter target) external returns (uint256) {
        return target.Increment();
    }
}
