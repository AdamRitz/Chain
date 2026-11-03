// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

/// A peer chat deployed by a normal transaction. Every account has equal access.
contract ClassroomChat {
    uint256 public roomCount;
    uint256 public messageCount;
    struct RoomInfo { string name; bool open; }
    mapping(uint256 => RoomInfo) public rooms;
    mapping(address => mapping(bytes32 => bool)) public usedIds;
    event RoomCreated(uint256 indexed roomId, string name);
    event MessageSent(uint256 indexed roomId, address indexed sender, bytes32 indexed clientId,
        uint256 messageId, string body, string nickname);
    constructor() {
        roomCount = 1;
        rooms[1] = RoomInfo(unicode"公共讨论", true);
        emit RoomCreated(1, unicode"公共讨论");
    }
    function CreateRoom(string calldata name) external returns (uint256 id) {
        require(bytes(name).length > 0 && bytes(name).length <= 96, "name: 1..96 bytes");
        id = ++roomCount;
        rooms[id] = RoomInfo(name, true);
        emit RoomCreated(id, name);
    }
    function SendMessage(uint256 roomId, bytes32 clientId, string calldata body, string calldata nickname) external {
        require(roomId > 0 && roomId <= roomCount && rooms[roomId].open, "room closed");
        require(clientId != bytes32(0) && !usedIds[msg.sender][clientId], "duplicate message");
        require(bytes(body).length > 0 && bytes(body).length <= 512, "body: 1..512 bytes");
        require(bytes(nickname).length > 0 && bytes(nickname).length <= 48, "nickname: 1..48 bytes");
        usedIds[msg.sender][clientId] = true;
        emit MessageSent(roomId, msg.sender, clientId, ++messageCount, body, nickname);
    }
}
