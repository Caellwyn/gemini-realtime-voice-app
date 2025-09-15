"""
Connection management for WebSocket sessions.
Handles session lifecycle, error handling, and task coordination.
"""

import asyncio
from typing import Optional, Callable, Any
import websockets

from websocket_handler import LatencyLogger


class SessionContext:
    """Context for a WebSocket session with managed lifecycle."""
    
    def __init__(self, client_websocket: websockets.ServerProtocol, client_addr: str):
        self.client_websocket = client_websocket
        self.client_addr = client_addr
        self.session_closed = asyncio.Event()
        # LatencyLogger now takes no constructor arguments; pass client address in each log call
        self.logger = LatencyLogger()
        self._tasks: list[asyncio.Task] = []
    
    def add_task(self, task: asyncio.Task):
        """Add a task to be managed by this session context."""
        self._tasks.append(task)
    
    async def wait_for_completion(self):
        """Wait for all session tasks to complete."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
    
    def cancel_tasks(self):
        """Cancel all pending tasks."""
        for task in self._tasks:
            if not task.done():
                task.cancel()
    
    def close_session(self):
        """Signal that the session should close."""
        self.session_closed.set()


class ConnectionManager:
    """Manages WebSocket connections and session lifecycle."""
    
    def __init__(self):
        self.active_sessions: dict[str, SessionContext] = {}
    
    async def handle_session(self, client_websocket: websockets.ServerProtocol, 
                           client_addr: str, session_handler: Callable) -> None:
        """
        Handle a complete WebSocket session lifecycle.
        
        Args:
            client_websocket: The client WebSocket connection
            client_addr: Client address string
            session_handler: Async function to handle the session business logic
        """
        context = SessionContext(client_websocket, client_addr)
        session_id = f"{client_addr}_{id(client_websocket)}"
        
        try:
            # Register the active session
            self.active_sessions[session_id] = context
            
            print(f"New WebSocket connection from {client_addr}")
            context.logger.log_connection(client_addr)
            
            # Latency monitoring is started inside gemini_session_handler (measure_latency task).
            # Removed call to non-existent log_latency_continuously().
            
            # Handle the session
            await session_handler(context)
            
        except websockets.exceptions.ConnectionClosedOK:
            print(f"Client {client_addr} disconnected normally")
        except websockets.exceptions.ConnectionClosed as e:
            if e.code == 1011:
                print(f"Client {client_addr} connection closed due to keepalive timeout")
            else:
                print(f"Client {client_addr} WebSocket connection closed: {e}")
        except Exception as e:
            print(f"Error in session for {client_addr}: {e}")
            context.logger.log_error(client_addr, str(e))
        finally:
            # Clean up session
            context.cancel_tasks()
            if session_id in self.active_sessions:
                del self.active_sessions[session_id]
            
            print(f"Session ended for {client_addr}")
            context.logger.logger.info(f"Session ended (client: {client_addr})")
    
    async def create_session_tasks(self, context: SessionContext, 
                                 send_handler: Callable, receive_handler: Callable) -> None:
        """
        Create and manage send/receive tasks for a session.
        
        Args:
            context: Session context
            send_handler: Async function to handle sending messages
            receive_handler: Async function to handle receiving messages
        """
        # Create send and receive tasks
        send_task = asyncio.create_task(send_handler())
        receive_task = asyncio.create_task(receive_handler())
        
        context.add_task(send_task)
        context.add_task(receive_task)
        
        # Wait for completion
        await context.wait_for_completion()
    
    def get_active_session_count(self) -> int:
        """Get the number of currently active sessions."""
        return len(self.active_sessions)
    
    async def shutdown_all_sessions(self):
        """Gracefully shutdown all active sessions."""
        for context in self.active_sessions.values():
            context.close_session()
        
        # Wait a bit for graceful shutdowns
        await asyncio.sleep(1.0)
        
        # Cancel any remaining tasks
        for context in self.active_sessions.values():
            context.cancel_tasks()


class WebSocketServer:
    """High-level WebSocket server with connection management."""
    
    def __init__(self, host: str = "localhost", port: int = 9082):
        self.host = host
        self.port = port
        self.connection_manager = ConnectionManager()
        self._server: Optional[Any] = None
    
    async def start(self, session_handler: Callable, ping_interval: int = 20, 
                   ping_timeout: int = 10) -> None:
        """
        Start the WebSocket server.
        
        Args:
            session_handler: Function to handle individual sessions
            ping_interval: WebSocket ping interval in seconds
            ping_timeout: WebSocket ping timeout in seconds
        """
        async def connection_wrapper(client_websocket: websockets.ServerProtocol, path: str):
            client_addr = f"{client_websocket.remote_address[0]}:{client_websocket.remote_address[1]}"
            await self.connection_manager.handle_session(
                client_websocket, client_addr, session_handler
            )
        
        async with websockets.serve(
            connection_wrapper,
            self.host,
            self.port,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout
        ):
            print(f"WebSocket server running on {self.host}:{self.port}")
            print(f"Ping interval: {ping_interval}s, Ping timeout: {ping_timeout}s")
            
            try:
                await asyncio.Future()  # Keep running indefinitely
            except KeyboardInterrupt:
                print("Shutting down server...")
                await self.connection_manager.shutdown_all_sessions()
    
    def get_status(self) -> dict[str, Any]:
        """Get server status information."""
        return {
            "host": self.host,
            "port": self.port,
            "active_sessions": self.connection_manager.get_active_session_count(),
            "server_running": self._server is not None
        }


# Utility functions for backward compatibility
async def create_session_context(client_websocket: websockets.ServerProtocol, 
                               client_addr: str) -> SessionContext:
    """Create a session context for the given connection."""
    return SessionContext(client_websocket, client_addr)


async def handle_connection_errors(operation: Callable, context: SessionContext) -> bool:
    """
    Handle common WebSocket connection errors.
    
    Returns:
        True if operation completed successfully, False if connection was closed
    """
    try:
        await operation()
        return True
    except websockets.exceptions.ConnectionClosedOK:
        print(f"Client {context.client_addr} connection closed normally")
        return False
    except websockets.exceptions.ConnectionClosed as e:
        if e.code == 1011:
            print(f"Client {context.client_addr} connection closed due to keepalive timeout")
        else:
            print(f"Client {context.client_addr} WebSocket connection closed: {e}")
        return False
    except Exception as e:
        print(f"Error in operation for {context.client_addr}: {e}")
        context.logger.log_error(context.client_addr, str(e))
        return False