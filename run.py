#!/usr/bin/env python3
import asyncio
import websockets
import json
import secrets
import string

# Limits and error strings.
MAX_PEERS = 4096
MAX_LOBBIES = 1024
NO_LOBBY_TIMEOUT = 1  # in seconds

STR_NO_LOBBY = "Have not joined lobby yet"
STR_ALREADY_IN_LOBBY = "Already in a lobby"
STR_LOBBY_DOES_NOT_EXIST = "Lobby does not exist"
STR_TOO_MANY_LOBBIES = "Too many lobbies open, disconnecting"
STR_SERVER_ERROR = "Server error, lobby not found"
STR_INVALID_FORMAT = "Invalid message format"
STR_INVALID_CMD = "Invalid command"
STR_INVALID_DEST = "Invalid destination"
STR_TOO_MANY_PEERS = "Too many peers connected"
STR_INVALID_TRANSFER_MODE = "Invalid transfer mode, must be text"

# Command codes.
CMD = {
    "JOIN": 0,           # data: lobby name (empty = create new); id: 0 means mesh mode
    "ID": 1,             # server sends to assign your unique id (host gets 1)
    "PEER_CONNECT": 2,   # notify peers that a new peer has joined
    "PEER_DISCONNECT": 3,# notify peers that a peer has left
    "OFFER": 4,          # signaling offer
    "ANSWER": 5,         # signaling answer
    "CANDIDATE": 6,      # ICE candidate message
}

def random_id():
    # Return a positive 31-bit integer.
    return secrets.randbits(31)

def random_secret():
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(16))

def proto_message(msg_type, dest_id, data=""):
    return json.dumps({
        "type": msg_type,
        "id": dest_id,
        "data": data,
    })

class Peer:
    def __init__(self, peer_id, websocket):
        self.id = peer_id
        self.ws = websocket
        self.lobby = None  # lobby name string
        self.timeout_task = None

class Lobby:
    def __init__(self, name, host_peer, mesh):
        self.name = name
        self.host = host_peer  # Peer object
        self.mesh = mesh      # Boolean; if true, create full mesh
        self.peers = []       # List of Peer objects

    def get_assigned_id(self, peer):
        # Host always gets id 1.
        return 1 if peer.id == self.host.id else peer.id

    async def join(self, peer: Peer):
        assigned = self.get_assigned_id(peer)
        # Tell the new peer its assigned id (and mesh flag as string).
        await peer.ws.send(proto_message(CMD["ID"], assigned, "true" if self.mesh else ""))
        # Notify existing peers about the new peer…
        for p in self.peers:
            await p.ws.send(proto_message(CMD["PEER_CONNECT"], self.get_assigned_id(peer)))
            # …and inform the new peer about each already-connected peer.
            await peer.ws.send(proto_message(CMD["PEER_CONNECT"], self.get_assigned_id(p)))
        self.peers.append(peer)

    async def leave(self, peer: Peer):
        if peer in self.peers:
            assigned = self.get_assigned_id(peer)
            self.peers.remove(peer)
            # Inform remaining peers that this peer has disconnected.
            for p in self.peers:
                await p.ws.send(proto_message(CMD["PEER_DISCONNECT"], assigned))

# Global dictionaries to keep track of lobbies and peers.
lobbies = {}  # lobby_name -> Lobby
peers = {}    # websocket -> Peer

async def handle_connection(websocket, path):
    # Reject connection if maximum peers reached.
    if len(peers) >= MAX_PEERS:
        await websocket.close(code=4000, reason=STR_TOO_MANY_PEERS)
        return

    peer_id = random_id()
    peer = Peer(peer_id, websocket)
    peers[websocket] = peer

    # Disconnect if the peer hasn’t joined a lobby within the timeout.
    async def lobby_timeout():
        await asyncio.sleep(NO_LOBBY_TIMEOUT)
        if peer.lobby is None:
            await websocket.close(code=4000, reason=STR_NO_LOBBY)
    peer.timeout_task = asyncio.create_task(lobby_timeout())

    try:
        async for message in websocket:
            # Ensure message is a text string.
            if not isinstance(message, str):
                await websocket.close(code=4000, reason=STR_INVALID_TRANSFER_MODE)
                return
            try:
                msg = json.loads(message)
            except Exception:
                await websocket.close(code=4000, reason=STR_INVALID_FORMAT)
                return
            # Check message format.
            if "type" not in msg or "id" not in msg or "data" not in msg:
                await websocket.close(code=4000, reason=STR_INVALID_FORMAT)
                return
            msg_type = int(msg["type"])
            dest_id = int(msg["id"])
            data = msg["data"]

            # Handle JOIN.
            if msg_type == CMD["JOIN"]:
                # In JOIN, data is the lobby name.
                # If data is empty, create a new lobby.
                mesh = (dest_id == 0)  # use mesh if id field is 0
                lobby_name = data
                if peer.lobby is not None:
                    await websocket.close(code=4000, reason=STR_ALREADY_IN_LOBBY)
                    return
                if lobby_name == "":
                    if len(lobbies) >= MAX_LOBBIES:
                        await websocket.close(code=4000, reason=STR_TOO_MANY_LOBBIES)
                        return
                    lobby_name = random_secret()
                    lobby = Lobby(lobby_name, peer, mesh)
                    lobbies[lobby_name] = lobby
                else:
                    if lobby_name not in lobbies:
                        await websocket.close(code=4000, reason=STR_LOBBY_DOES_NOT_EXIST)
                        return
                    lobby = lobbies[lobby_name]
                peer.lobby = lobby_name
                if peer.timeout_task:
                    peer.timeout_task.cancel()
                # Join the lobby.
                await lobby.join(peer)
                # Confirm lobby join (send lobby name back).
                await websocket.send(proto_message(CMD["JOIN"], 0, lobby_name))
            # Relay signaling messages.
            elif msg_type in (CMD["OFFER"], CMD["ANSWER"], CMD["CANDIDATE"]):
                if peer.lobby is None:
                    await websocket.close(code=4000, reason=STR_NO_LOBBY)
                    return
                lobby = lobbies.get(peer.lobby)
                if not lobby:
                    await websocket.close(code=4000, reason=STR_SERVER_ERROR)
                    return
                # Determine the target peer.
                target_peer = None
                if dest_id == 1:
                    target_peer = lobby.host
                else:
                    for p in lobby.peers:
                        if p.id == dest_id:
                            target_peer = p
                            break
                if target_peer is None:
                    await websocket.close(code=4000, reason=STR_INVALID_DEST)
                    return
                sender_assigned = lobby.get_assigned_id(peer)
                await target_peer.ws.send(proto_message(msg_type, sender_assigned, data))
            else:
                await websocket.close(code=4000, reason=STR_INVALID_CMD)
                return
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        # On disconnect, remove peer from its lobby (if any) and clean up.
        if peer.lobby:
            lobby = lobbies.get(peer.lobby)
            if lobby:
                await lobby.leave(peer)
                if len(lobby.peers) == 0:
                    del lobbies[peer.lobby]
        if peer.timeout_task:
            peer.timeout_task.cancel()
        del peers[websocket]

async def main():
    async with websockets.serve(handle_connection, "37.194.195.213", 35410):
        print("Signaling server started on ws://37.194.195.213:35410")
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
