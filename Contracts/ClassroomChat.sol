// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

/// A classroom example deployed by a normal transaction, never a privileged native contract.
contract ClassroomChat {
    address public owner;
    bool public membersOnly;
    uint256 public roomCount;
    uint256 public messageCount;
    struct RoomInfo { string name; bool open; }
    mapping(uint256 => RoomInfo) public rooms;
    mapping(address => bool) public members;
    mapping(address => mapping(bytes32 => bool)) public usedIds;
    event RoomCreated(uint256 indexed roomId, string name);
    event RoomOpen(uint256 indexed roomId, bool open);
    event MemberSet(address indexed member, bool allowed);
    event MembershipMode(bool enabled);
    event MessageSent(uint256 indexed roomId, address indexed sender, bytes32 indexed clientId,
        uint256 messageId, string body, string nickname);
    modifier OnlyOwner() { require(msg.sender == owner, "teacher only"); _; }
    constructor() {
        owner = msg.sender;
        members[msg.sender] = true;
        roomCount = 1;
        rooms[1] = RoomInfo(unicode"课堂公共聊天室", true);
        emit RoomCreated(1, unicode"课堂公共聊天室");
    }
    function CreateRoom(string calldata name) external OnlyOwner returns (uint256 id) {
        require(bytes(name).length > 0 && bytes(name).length <= 96, "name: 1..96 bytes");
        id = ++roomCount;
        rooms[id] = RoomInfo(name, true);
        emit RoomCreated(id, name);
    }
    function SetRoomOpen(uint256 id, bool open) external OnlyOwner {
        require(id > 0 && id <= roomCount, "unknown room");
        rooms[id].open = open;
        emit RoomOpen(id, open);
    }
    function SetMember(address member, bool allowed) external OnlyOwner {
        members[member] = allowed;
        emit MemberSet(member, allowed);
    }
    function SetMembersOnly(bool enabled) external OnlyOwner {
        membersOnly = enabled;
        emit MembershipMode(enabled);
    }
    function SendMessage(uint256 roomId, bytes32 clientId, string calldata body, string calldata nickname) external {
        require(roomId > 0 && roomId <= roomCount && rooms[roomId].open, "room closed");
        require(!membersOnly || members[msg.sender], "not a member");
        require(clientId != bytes32(0) && !usedIds[msg.sender][clientId], "duplicate message");
        require(bytes(body).length > 0 && bytes(body).length <= 512, "body: 1..512 bytes");
        require(bytes(nickname).length > 0 && bytes(nickname).length <= 48, "nickname: 1..48 bytes");
        usedIds[msg.sender][clientId] = true;
        emit MessageSent(roomId, msg.sender, clientId, ++messageCount, body, nickname);
    }
}
