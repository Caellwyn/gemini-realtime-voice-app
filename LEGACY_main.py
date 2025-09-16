## pip install google-genai==0.3.0

import asyncio
import json
import os
import time
import websockets
from google import genai
from google.genai import types

from form_manager import FormManager
from websocket_handler import (
    LatencyLogger, SessionConfig, PDFSyncManager, 
    measure_latency, setup_session, handle_realtime_input,
    handle_user_edit, handle_form_confirmation
)
from audio_handler import get_audio_handler
from tool_response_builder import ToolCall, ToolCallHandler
from connection_manager import ConnectionManager, SessionContext
from config import DEFAULT_MODEL

# Load API key from environment
os.environ['GOOGLE_API_KEY'] = os.getenv('GEMINI_API_KEY')

client = genai.Client()


async def handle_tool_calls(response, form_manager: FormManager, client_websocket: websockets.ServerProtocol, pdf_sync: PDFSyncManager):
    """Handle tool calls from Gemini API using ToolResponseBuilder."""
    if response.server_content is not None or response.tool_call is None:
        return []
    
    function_calls = response.tool_call.function_calls
    all_responses = []
    
    for function_call in function_calls:
        tool_call = ToolCall(function_call.name, function_call.args, function_call.id)
        
        responses = await ToolCallHandler.handle_pdf_form_tools(
            tool_call, form_manager, client_websocket, pdf_sync
        )
        all_responses.extend(responses)
    
    return all_responses


async def send_to_gemini(client_websocket: websockets.ServerProtocol, session, form_manager: FormManager, pdf_sync: PDFSyncManager, session_closed: asyncio.Event):
    """Handle sending messages from client to Gemini."""
    try:
        async for message in client_websocket:
            try:
                data = json.loads(message)
                
                if "realtime_input" in data:
                    await handle_realtime_input(data, session, form_manager, pdf_sync)
                elif "user_edit" in data:
                    await handle_user_edit(data, session, form_manager, pdf_sync)
                elif "confirm_form" in data:
                    await handle_form_confirmation(data, session, form_manager, client_websocket, pdf_sync)
                    return  # Session ends after confirmation
                    
            except Exception as e:
                print(f"Error processing client message: {e}")
                
        print("Client connection closed (send)")
    except websockets.exceptions.ConnectionClosed as e:
        if e.code == 1011:
            print("Send connection closed due to keepalive timeout")
        else:
            print(f"Send websocket connection closed: {e}")
    except Exception as e:
        print(f"Error sending to Gemini: {e}")
    finally:
        print("send_to_gemini closed")
        session_closed.set()


async def receive_from_gemini(session, client_websocket: websockets.ServerProtocol, form_manager: FormManager, pdf_sync: PDFSyncManager, session_closed: asyncio.Event):
    """Handle receiving responses from Gemini and forwarding to client."""
    try:
        while True:
            async for response in session.receive():
                # Handle tool calls
                if response.server_content is None and response.tool_call is not None:
                    function_responses = await handle_tool_calls(response, form_manager, client_websocket, pdf_sync)
                    if function_responses:
                        await session.send_tool_response(function_responses=function_responses)
                    continue
                
                # Handle model content
                if response.server_content is not None:
                    model_turn = response.server_content.model_turn
                    if model_turn:
                        audio_handler = get_audio_handler()
                        for part in model_turn.parts:
                            if hasattr(part, 'text') and part.text is not None:
                                await client_websocket.send(json.dumps({"text": part.text}))
                            elif hasattr(part, 'inline_data') and part.inline_data is not None:
                                await audio_handler.process_gemini_audio_response(client_websocket, part)
                    
                    if response.server_content.turn_complete:
                        pass  # Turn completed
                        
    except websockets.exceptions.ConnectionClosedOK:
        print("Client connection closed normally (receive)")
    except websockets.exceptions.ConnectionClosed as e:
        if e.code == 1011:
            print("Connection closed due to keepalive timeout")
        else:
            print(f"Websocket connection closed: {e}")
    except Exception as e:
        print(f"Error receiving from Gemini: {e}")
    finally:
        print("Gemini connection closed (receive)")
        session_closed.set()


async def gemini_session_handler(client_websocket: websockets.ServerProtocol):
    """Main WebSocket session handler with connection management."""
    client_addr = f"{client_websocket.remote_address[0]}:{client_websocket.remote_address[1]}"
    
    # Create session context
    session_context = SessionContext(client_websocket, client_addr)
    
    try:
        # Start latency monitoring
        latency_task = asyncio.create_task(measure_latency(client_websocket, session_context.logger, client_addr))
        session_context.add_task(latency_task)
        
        # Parse configuration message
        config_message = await client_websocket.recv()
        parsed_config = SessionConfig.parse_config_message(config_message)
        
        config = parsed_config["config"]
        model_override = parsed_config["model_override"]
        pdf_field_names = parsed_config["pdf_field_names"]
        pdf_form_id = parsed_config["pdf_form_id"]
        
        # Setup voice and VAD configuration
        voice_name = config.pop("voice_name", None)
        enable_vad = bool(config.pop("enable_vad", False))
        
        SessionConfig.setup_voice_config(config, voice_name)
        SessionConfig.setup_vad_config(config, enable_vad)
        
        # Initialize form manager
        if not pdf_field_names or not pdf_form_id:
            print("Error: pdf_field_names and pdf_form_id are required for the session.")
            await client_websocket.close(code=1011, reason="PDF metadata not provided.")
            return
            
        form_manager = FormManager(pdf_field_names, pdf_form_id)
        
        # Setup tool declarations
        config["tools"] = [{"function_declarations": form_manager.get_tool_declarations()}]
        
        # Initialize PDF sync manager
        pdf_sync = PDFSyncManager(pdf_form_id)
        
        # Connect to Gemini API
        session_context.logger.logger.info(f"Attempting Gemini API connection (client: {client_addr})")
        gemini_connect_start = time.time()
        
        async with client.aio.live.connect(model=(model_override or DEFAULT_MODEL), config=config) as session:
            gemini_connect_time = time.time() - gemini_connect_start
            print("Connected to Gemini API")
            session_context.logger.log_gemini_connection(client_addr, gemini_connect_time)
            
            # Setup session with system instructions
            await setup_session(session, form_manager)
            
            # Create send and receive handlers
            async def send_handler():
                await send_to_gemini(client_websocket, session, form_manager, pdf_sync, session_context.session_closed)
            
            async def receive_handler():
                await receive_from_gemini(session, client_websocket, form_manager, pdf_sync, session_context.session_closed)
            
            # Create and manage session tasks
            send_task = asyncio.create_task(send_handler())
            receive_task = asyncio.create_task(receive_handler())
            
            session_context.add_task(send_task)
            session_context.add_task(receive_task)
            
            # Wait for completion
            await session_context.wait_for_completion()
            
    except Exception as e:
        print(f"Error in Gemini session: {e}")
        session_context.logger.log_error(client_addr, str(e))
    finally:
        print("Gemini session closed.")
        session_context.logger.logger.info(f"Gemini session ended (client: {client_addr})")
        session_context.cancel_tasks()


async def main() -> None:
    """Start the WebSocket server with connection management."""
    from config import WEBSOCKET_PING_INTERVAL, WEBSOCKET_PING_TIMEOUT
    
    # Create connection manager
    connection_manager = ConnectionManager()
    
    async def session_wrapper(context: SessionContext):
        """Wrapper to integrate connection management with session handling."""
        await gemini_session_handler(context.client_websocket)
    
    try:
        # websockets v12+ calls the handler with a single argument (the connection).
        # Older versions passed (websocket, path). We accept an optional path for compatibility.
        async def websocket_handler(ws, path=None):  # path kept optional for backward compatibility
            client_addr = f"{ws.remote_address[0]}:{ws.remote_address[1]}"
            await connection_manager.handle_session(ws, client_addr, session_wrapper)
        
        async with websockets.serve(
            websocket_handler,
            "localhost", 
            9082,
            ping_interval=WEBSOCKET_PING_INTERVAL,
            ping_timeout=WEBSOCKET_PING_TIMEOUT
        ):
            print("Running WebSocket server on localhost:9082 with connection management...")
            timeout_display = "disabled" if WEBSOCKET_PING_TIMEOUT is None else f"{WEBSOCKET_PING_TIMEOUT}s"
            print(f"Ping interval: {WEBSOCKET_PING_INTERVAL}s, Ping timeout: {timeout_display}")
            await asyncio.Future()  # Keep the server running indefinitely
    except KeyboardInterrupt:
        print("Shutting down server...")
        await connection_manager.shutdown_all_sessions()


if __name__ == "__main__":
    asyncio.run(main())