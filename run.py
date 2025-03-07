import asyncio
import websockets
import json
import random
import string
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Constants
MAX_PEERS = 4096
MAX_LOBBIES = 1024
ALFNUM = string.ascii_letters + string.digits

# Timeouts and intervals
NO_LOBBY_TIMEOUT = 60  # 60 seconds to join a lobby
SEAL_CLOSE_TIMEOUT = 10  # 10 seconds after sealing before closing
LOBBY_CLEANUP_INTERVAL = 60  # Check for empty lobbies every 60 seconds

# Command types
class CMD:
    JOIN = 0
    ID = 1
    PEER_CONNECT = 2
    PEER_DISCONNECT = 3
    OFFER = 4
    ANSWER = 5
    CANDIDATE = 6
    SEAL = 7
    LIST_LOBBIES = 8  # Added for listing lobbies

# Error messages
STR_NO_LOBBY = 'Have not joined lobby yet'
STR_HOST_DISCONNECTED = 'Room host has disconnected'
STR_ONLY_HOST_CAN_SEAL = 'Only host can seal the lobby'
STR_SEAL_COMPLETE = 'Seal complete'
STR_TOO_MANY_LOBBIES = 'Too many lobbies open, disconnecting'
STR_ALREADY_IN_LOBBY = 'Already in a lobby'
STR_LOBBY_DOES_NOT_EXIST = 'Lobby does not exist'
STR_LOBBY_IS_SEALED = 'Lobby is sealed'
STR_LOBBY_IS_FULL = 'Lobby is full'
STR_INVALID_FORMAT = 'Invalid message format'
STR_NEED_LOBBY = 'Invalid message when not in a lobby'
STR_SERVER_ERROR = 'Server error, lobby not found'
STR_INVALID_DEST = 'Invalid destination'
STR_INVALID_CMD = 'Invalid command'
STR_TOO_MANY_PEERS = 'Too many peers connected'

# Utility functions
def random_id():
    """Generate a random ID for a peer."""
    return random.randint(2, 1000000)  # Start from 2 as 1 is reserved for host

def random_secret(length=16):
    """Generate a random alphanumeric string for lobby names."""
    return ''.join(random.choice(ALFNUM) for _ in range(length))

def create_message(msg_type, msg_id, data=""):
    """Create a JSON message in the protocol format."""
    return json.dumps({
        "type": msg_type,
        "id": msg_id,
        "data": data
    })

# Classes for peers and lobbies
class Peer:
    def __init__(self, peer_id, websocket):
        self.id = peer_id
        self.ws = websocket
        self.lobby = None
        self.lobby_timeout = None
        
    async def close(self, code=1000, reason=""):
        try:
            await self.ws.close(code, reason)
        except Exception as e:
            logger.error(f"Error closing connection: {e}")

class Lobby:
    def __init__(self, name, host_id, host_peer, max_players, mesh=True):
        self.name = name
        self.host_id = host_id
        self.host_peer = host_peer
        self.mesh = mesh
        self.peers = []
        self.sealed = False
        self.close_timer = None
        self.max_players = max_players
        self.description = f"Lobby {name} - {len(self.peers)}/{max_players} players"
        
    def get_peer_id(self, peer):
        """Get the assigned ID for a peer in this lobby."""
        if peer.id == self.host_id:
            return 1
        return peer.id
    
    def is_full(self):
        """Check if the lobby is full."""
        return len(self.peers) >= self.max_players
    
    async def join(self, peer):
        """Add a peer to the lobby and notify all peers."""
        assigned_id = self.get_peer_id(peer)
        
        # Tell the joining peer its ID and mesh mode
        await peer.ws.send(create_message(CMD.ID, assigned_id, "true" if self.mesh else ""))
        
        # Notify all existing peers about the new peer and vice versa
        for p in self.peers:
            await p.ws.send(create_message(CMD.PEER_CONNECT, assigned_id))
            await peer.ws.send(create_message(CMD.PEER_CONNECT, self.get_peer_id(p)))
        
        self.peers.append(peer)
        self.description = f"Lobby {self.name} - {len(self.peers)}/{self.max_players} players"
        
        # Tell the peer which lobby they joined
        await peer.ws.send(create_message(CMD.JOIN, 0, self.name))
    
    async def leave(self, peer):
        """Remove a peer from the lobby and notify others."""
        if peer not in self.peers:
            return False
        
        assigned_id = self.get_peer_id(peer)
        is_host = assigned_id == 1
        
        # Remove from the list first to avoid recursive issues
        self.peers.remove(peer)
        self.description = f"Lobby {self.name} - {len(self.peers)}/{self.max_players} players"
        
        # Notify other peers
        for p in self.peers:
            if is_host:
                # Host left, close everyone's connection
                await p.close(4000, STR_HOST_DISCONNECTED)
            else:
                # Regular peer left, notify others
                await p.ws.send(create_message(CMD.PEER_DISCONNECT, assigned_id))
        
        # Cancel close timer if it's running and we're closing anyway
        if is_host and self.close_timer:
            self.close_timer.cancel()
            self.close_timer = None
            
        return is_host

    async def seal(self, peer):
        """Seal the lobby so no new peers can join."""
        if peer.id != self.host_id:
            raise ValueError(STR_ONLY_HOST_CAN_SEAL)
        
        self.sealed = True
        
        # Notify all peers that the lobby is sealed
        for p in self.peers:
            await p.ws.send(create_message(CMD.SEAL, 0))
        
        logger.info(f"Peer {peer.id} sealed lobby {self.name} with {len(self.peers)} peers")
        
        # Schedule lobby closure
        if self.close_timer:
            self.close_timer.cancel()
            
        self.close_timer = asyncio.create_task(self._schedule_close())
    
    async def _schedule_close(self):
        """Schedule the lobby to close after a timeout."""
        await asyncio.sleep(SEAL_CLOSE_TIMEOUT)
        for p in self.peers:
            await p.close(1000, STR_SEAL_COMPLETE)

# Server state
peers_count = 0
lobbies = {}

async def parse_message(peer, message):
    """Parse and handle incoming messages."""
    logger.info(f"Received message from peer {peer.id}: {message}")
    try:
        msg = json.loads(message)
        
        if not isinstance(msg.get('type'), int) or not isinstance(msg.get('id'), int):
            raise ValueError(STR_INVALID_FORMAT)
        
        msg_type = msg['type']
        msg_id = msg['id']
        data = msg.get('data', '')
        
        if not isinstance(data, str):
            raise ValueError(STR_INVALID_FORMAT)
        
        # Handle JOIN command
        if msg_type == CMD.JOIN:
            await handle_join(peer, data, msg_id == 0)
            return
        
        # Handle LIST_LOBBIES command
        if msg_type == CMD.LIST_LOBBIES:
            await handle_list_lobbies(peer)
            return
        
        # All other commands require being in a lobby
        if not peer.lobby or peer.lobby not in lobbies:
            raise ValueError(STR_NEED_LOBBY)
        
        lobby = lobbies[peer.lobby]
        
        # Handle SEAL command
        if msg_type == CMD.SEAL:
            await lobby.seal(peer)
            return
        
        # Handle signaling messages (OFFER, ANSWER, CANDIDATE)
        if msg_type in [CMD.OFFER, CMD.ANSWER, CMD.CANDIDATE]:
            await handle_signal(peer, lobby, msg_type, msg_id, data)
            return
        
        raise ValueError(STR_INVALID_CMD)
        
    except json.JSONDecodeError:
        raise ValueError(STR_INVALID_FORMAT)

async def handle_join(peer, lobby_name, use_mesh):
    """Handle a request to join a lobby."""
    global peers_count
    
    # If the peer is already in a lobby, leave it first
    if peer.lobby and peer.lobby in lobbies:
        old_lobby = lobbies[peer.lobby]
        is_host = await old_lobby.leave(peer)
        if is_host:
            del lobbies[peer.lobby]
            logger.info(f"Deleted lobby {peer.lobby}, host left")
        peer.lobby = None
    
    # Create a new lobby if no name provided
    if not lobby_name:
        if len(lobbies) >= MAX_LOBBIES:
            raise ValueError(STR_TOO_MANY_LOBBIES)
        
        lobby_name = random_secret()
        # Get max_players from the message if provided (as part of the join data)
        max_players = msg_id if msg_id > 0 else 4
        lobbies[lobby_name] = Lobby(lobby_name, peer.id, peer, max_players, use_mesh)
        logger.info(f"Peer {peer.id} created lobby {lobby_name} with max {max_players} players")
        logger.info(f"Open lobbies: {len(lobbies)}")
    else:
        # Join an existing lobby
        if lobby_name not in lobbies:
            raise ValueError(STR_LOBBY_DOES_NOT_EXIST)
        
        lobby = lobbies[lobby_name]
        if lobby.sealed:
            raise ValueError(STR_LOBBY_IS_SEALED)
        
        if lobby.is_full():
            raise ValueError(STR_LOBBY_IS_FULL)
    
    # Set the peer's lobby and join
    peer.lobby = lobby_name
    await lobbies[lobby_name].join(peer)
    
    # Cancel the no-lobby timeout
    if peer.lobby_timeout:
        peer.lobby_timeout.cancel()
        peer.lobby_timeout = None

async def handle_list_lobbies(peer):
    """Handle a request to list available lobbies."""
    lobby_list = []
    for name, lobby in lobbies.items():
        if not lobby.sealed:
            lobby_list.append({
                "name": name,
                "players": len(lobby.peers),
                "maxPlayers": lobby.max_players,
                "description": lobby.description
            })
    
    await peer.ws.send(create_message(CMD.LIST_LOBBIES, 0, json.dumps(lobby_list)))

async def handle_signal(peer, lobby, msg_type, dest_id, data):
    """Handle WebRTC signaling messages between peers."""
    # Convert ID 1 to actual host ID
    if dest_id == 1:
        dest_id = lobby.host_id
    
    # Find the destination peer
    dest_peer = next((p for p in lobby.peers if p.id == dest_id), None)
    if not dest_peer:
        raise ValueError(STR_INVALID_DEST)
    
    # Forward the message to the destination
    await dest_peer.ws.send(create_message(msg_type, lobby.get_peer_id(peer), data))

async def handle_connection(websocket):
    """Handle a new WebSocket connection."""
    global peers_count
    
    # Check if we have too many peers
    if peers_count >= MAX_PEERS:
        await websocket.close(4000, STR_TOO_MANY_PEERS)
        return
    
    # Create a new peer
    peers_count += 1
    peer_id = random_id()
    peer = Peer(peer_id, websocket)
    
    # Set a timeout for joining a lobby
    peer.lobby_timeout = asyncio.create_task(no_lobby_timeout(peer))
    
    logger.info(f"New connection from peer {peer_id}")
    
    try:
        logger.info(f"Waiting for messages from peer {peer_id}")
        async for message in websocket:
            logger.info(f"Processing message from peer {peer_id}: {message}")
            try:
                await parse_message(peer, message)
            except ValueError as e:
                logger.error(f"Error handling message from peer {peer_id}: {str(e)}")
                await websocket.close(4000, str(e))
                break
    except websockets.exceptions.ConnectionClosed as e:
        logger.info(f"Connection with peer {peer_id} closed with code {e.code}: {e.reason}")
    finally:
        # Clean up
        peers_count -= 1
        
        # Cancel the no-lobby timeout if it's still running
        if peer.lobby_timeout:
            peer.lobby_timeout.cancel()
        
        # Leave any lobby the peer was in
        if peer.lobby and peer.lobby in lobbies:
            lobby = lobbies[peer.lobby]
            is_host = await lobby.leave(peer)
            if is_host:
                del lobbies[peer.lobby]
                logger.info(f"Deleted lobby {peer.lobby}, host disconnected")
                logger.info(f"Open lobbies: {len(lobbies)}")

async def no_lobby_timeout(peer):
    """Close connection if peer doesn't join a lobby within the timeout."""
    await asyncio.sleep(NO_LOBBY_TIMEOUT)
    if not peer.lobby:
        logger.info(f"Peer {peer.id} timed out without joining a lobby")
        await peer.close(4000, STR_NO_LOBBY)

async def cleanup_lobbies():
    """Periodically clean up empty lobbies."""
    while True:
        await asyncio.sleep(LOBBY_CLEANUP_INTERVAL)
        to_remove = []
        
        for name, lobby in lobbies.items():
            if not lobby.peers:
                to_remove.append(name)
        
        for name in to_remove:
            del lobbies[name]
            logger.info(f"Removed empty lobby {name}")
        
        if to_remove:
            logger.info(f"Cleaned up {len(to_remove)} empty lobbies. Open lobbies: {len(lobbies)}")

async def main():
    # Start the lobby cleanup task
    asyncio.create_task(cleanup_lobbies())
    
    # Start the WebSocket server
    async with websockets.serve(handle_connection, "0.0.0.0", 35410):
        logger.info("WebRTC signaling server started on ws://0.0.0.0:35410")
        await asyncio.Future()  # Run forever

if __name__ == "__main__":
    asyncio.run(main())