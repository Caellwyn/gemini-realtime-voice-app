"""Unified application server: serves both HTTP (static + PDF REST) and WebSocket realtime voice AI

Run with:
    python app.py

Replaces the previous two–process model (`server.py` + `main.py`). Those files remain for
reference but are deprecated. All development should target this entry point.
"""
import asyncio
import json
import os
import signal
import threading
import time
import socketserver
import websockets
from google import genai

# Local imports (reuse existing modules)
from form_manager import FormManager
from websocket_handler import (
    SessionConfig, PDFSyncManager, measure_latency, setup_session,
    handle_realtime_input, handle_user_edit, handle_form_confirmation
)
from tool_response_builder import ToolCall, ToolCallHandler
from connection_manager import ConnectionManager, SessionContext
from audio_handler import get_audio_handler
from config import DEFAULT_MODEL, WEBSOCKET_PING_INTERVAL, WEBSOCKET_PING_TIMEOUT, HTTP_PORT

# Import the HTTP handler & storage/session singletons from existing server module
import server as legacy_http
from server import NoCacheHandler  # noqa: F401  (imported for clarity / reuse)

# Ensure API key wiring (retain previous behavior)
os.environ['GOOGLE_API_KEY'] = os.getenv('GEMINI_API_KEY')
client = genai.Client()

###################################################################################################
# WebSocket (Gemini realtime) logic – largely adapted from previous main.py
###################################################################################################
async def handle_tool_calls(response, form_manager: FormManager, client_websocket: websockets.ServerProtocol, pdf_sync: PDFSyncManager):
    if response.server_content is not None or response.tool_call is None:
        return []
    all_responses = []
    for function_call in response.tool_call.function_calls:
        tool_call = ToolCall(function_call.name, function_call.args, function_call.id)
        responses = await ToolCallHandler.handle_pdf_form_tools(
            tool_call, form_manager, client_websocket, pdf_sync
        )
        all_responses.extend(responses)
    return all_responses

async def send_to_gemini(client_websocket: websockets.ServerProtocol, session, form_manager: FormManager, pdf_sync: PDFSyncManager, session_closed: asyncio.Event):
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
                    return
            except Exception as e:  # noqa: BLE001
                print(f"Error processing client message: {e}")
        print("Client connection closed (send)")
    except websockets.exceptions.ConnectionClosed as e:  # noqa: PERF203
        if getattr(e, 'code', None) == 1011:
            print("Send connection closed due to keepalive timeout")
        else:
            print(f"Send websocket connection closed: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"Error sending to Gemini: {e}")
    finally:
        session_closed.set()

async def receive_from_gemini(session, client_websocket: websockets.ServerProtocol, form_manager: FormManager, pdf_sync: PDFSyncManager, session_closed: asyncio.Event):
    try:
        while True:
            async for response in session.receive():
                if response.server_content is None and response.tool_call is not None:
                    function_responses = await handle_tool_calls(response, form_manager, client_websocket, pdf_sync)
                    if function_responses:
                        await session.send_tool_response(function_responses=function_responses)
                    continue
                if response.server_content is not None:
                    model_turn = response.server_content.model_turn
                    if model_turn:
                        audio_handler = get_audio_handler()
                        for part in model_turn.parts:
                            if hasattr(part, 'text') and part.text is not None:
                                await client_websocket.send(json.dumps({"text": part.text}))
                            elif hasattr(part, 'inline_data') and part.inline_data is not None:
                                await audio_handler.process_gemini_audio_response(client_websocket, part)
    except websockets.exceptions.ConnectionClosedOK:  # type: ignore[attr-defined]
        print("Client connection closed normally (receive)")
    except websockets.exceptions.ConnectionClosed as e:  # noqa: PERF203
        if getattr(e, 'code', None) == 1011:
            print("Connection closed due to keepalive timeout")
        else:
            print(f"Websocket connection closed: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"Error receiving from Gemini: {e}")
    finally:
        session_closed.set()

async def gemini_session_handler(client_websocket: websockets.ServerProtocol):
    client_addr = f"{client_websocket.remote_address[0]}:{client_websocket.remote_address[1]}"
    session_context = SessionContext(client_websocket, client_addr)
    try:
        latency_task = asyncio.create_task(measure_latency(client_websocket, session_context.logger, client_addr))
        session_context.add_task(latency_task)
        config_message = await client_websocket.recv()
        parsed_config = SessionConfig.parse_config_message(config_message)
        config = parsed_config["config"]
        model_override = parsed_config["model_override"]
        pdf_field_names = parsed_config["pdf_field_names"]
        pdf_form_id = parsed_config["pdf_form_id"]
        voice_name = config.pop("voice_name", None)
        enable_vad = bool(config.pop("enable_vad", False))
        SessionConfig.setup_voice_config(config, voice_name)
        SessionConfig.setup_vad_config(config, enable_vad)
        if not pdf_field_names or not pdf_form_id:
            await client_websocket.close(code=1011, reason="PDF metadata not provided.")
            return
        form_manager = FormManager(pdf_field_names, pdf_form_id)
        config["tools"] = [{"function_declarations": form_manager.get_tool_declarations()}]
        pdf_sync = PDFSyncManager(pdf_form_id)
        start_ts = time.time()
        async with client.aio.live.connect(model=(model_override or DEFAULT_MODEL), config=config) as session:
            print(f"Connected to Gemini API (latency {time.time()-start_ts:.2f}s)")
            await setup_session(session, form_manager)
            async def send_handler():
                await send_to_gemini(client_websocket, session, form_manager, pdf_sync, session_context.session_closed)
            async def receive_handler():
                await receive_from_gemini(session, client_websocket, form_manager, pdf_sync, session_context.session_closed)
            send_task = asyncio.create_task(send_handler())
            receive_task = asyncio.create_task(receive_handler())
            session_context.add_task(send_task)
            session_context.add_task(receive_task)
            await session_context.wait_for_completion()
    except Exception as e:  # noqa: BLE001
        print(f"Error in Gemini session: {e}")
    finally:
        session_context.cancel_tasks()

###################################################################################################
# HTTP server thread startup
###################################################################################################
class ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

_http_server = None


def start_http_server():
    global _http_server
    if _http_server is not None:
        return
    _http_server = ThreadingHTTPServer(("", HTTP_PORT), legacy_http.NoCacheHandler)
    print(f"HTTP server running on http://localhost:{HTTP_PORT}/index.html")
    try:
        _http_server.serve_forever()
    except Exception as e:  # noqa: BLE001
        print(f"HTTP server stopped: {e}")

###################################################################################################
# Unified main entry
###################################################################################################
async def run_websocket_server():
    connection_manager = ConnectionManager()
    async def session_wrapper(context: SessionContext):
        await gemini_session_handler(context.client_websocket)
    async def websocket_handler(ws, path=None):  # noqa: D401, ANN001
        client_addr = f"{ws.remote_address[0]}:{ws.remote_address[1]}"
        await connection_manager.handle_session(ws, client_addr, session_wrapper)
    async with websockets.serve(
        websocket_handler,
        "localhost",
        9082,
        ping_interval=WEBSOCKET_PING_INTERVAL,
        ping_timeout=WEBSOCKET_PING_TIMEOUT
    ):
        print("WebSocket server running on ws://localhost:9082")
        await asyncio.Future()  # run forever


def main():
    # Start HTTP server in background thread BEFORE event loop
    http_thread = threading.Thread(target=start_http_server, name="http-server", daemon=True)
    http_thread.start()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown_handler(*_):  # noqa: D401, ANN002
        print("\nShutdown signal received. Stopping services...")
        if _http_server:
            try:
                _http_server.shutdown()
            except Exception:  # noqa: BLE001
                pass
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_handler)
        except NotImplementedError:  # Windows may not support SIGTERM
            pass

    try:
        loop.run_until_complete(run_websocket_server())
    except KeyboardInterrupt:
        shutdown_handler()
    finally:
        loop.close()
        print("Unified server stopped.")

if __name__ == "__main__":
    main()
