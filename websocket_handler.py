"""
WebSocket session handling for Gemini API integration.
Broken down into smaller, focused functions for better maintainability.
"""

import asyncio
import json
import time
import traceback
import logging
import websockets
from typing import Dict, Any, Optional
import urllib.request
import urllib.error

try:
    import aiohttp
except ImportError:
    aiohttp = None

from google import genai
from google.genai import types
from form_manager import FormManager
from audio_handler import get_audio_handler
from logging_utils import log_tool_call
from config import (
    WEBSOCKET_PING_INTERVAL, WEBSOCKET_PING_TIMEOUT, DEFAULT_MODEL,
    LATENCY_MEASUREMENT_INTERVAL, LOG_FILE_LATENCY, LOG_FORMAT,
    PDF_SYNC_DELAY
)


class LatencyLogger:
    """Handles WebSocket latency logging."""
    
    def __init__(self):
        self.logger = logging.getLogger('websocket_latency')
        self.logger.setLevel(logging.INFO)
        
        handler = logging.FileHandler(LOG_FILE_LATENCY)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        
        self.logger.addHandler(handler)
        self.logger.propagate = False
    
    def log_connection(self, client_addr: str):
        """Log new WebSocket connection."""
        self.logger.info(f"New WebSocket connection from {client_addr}")
    
    def log_gemini_connection(self, client_addr: str, connect_time: float):
        """Log Gemini API connection timing."""
        self.logger.info(f"Gemini API connected in {connect_time:.2f}s (client: {client_addr})")
    
    def log_latency(self, client_addr: str, latency_ms: float):
        """Log ping latency measurement."""
        self.logger.info(f"Ping latency: {latency_ms:.2f}ms (client: {client_addr})")
    
    def log_error(self, client_addr: str, error: str):
        """Log error message."""
        self.logger.error(f"Error: {error} (client: {client_addr})")
    
    def log_warning(self, client_addr: str, message: str):
        """Log warning message."""
        self.logger.warning(f"Warning: {message} (client: {client_addr})")


class SessionConfig:
    """Handles session configuration and setup."""
    
    @staticmethod
    def parse_config_message(config_message: str) -> Dict[str, Any]:
        """Parse and validate configuration message from client."""
        config_data = json.loads(config_message)
        config = config_data.get("setup", {})
        
        # Extract and process configuration options
        model_override = config.pop("model", None)
        pdf_field_names = config.pop("pdf_field_names", [])
        pdf_form_id = config.pop("pdf_form_id", None)
        
        # Flatten generation_config into main config
        if 'generation_config' in config:
            gen_config = config.pop('generation_config')
            config.update(gen_config)
        
        return {
            "config": config,
            "model_override": model_override,
            "pdf_field_names": pdf_field_names,
            "pdf_form_id": pdf_form_id
        }
    
    @staticmethod
    def setup_voice_config(config: Dict[str, Any], voice_name: Optional[str]):
        """Setup voice configuration if provided."""
        if not voice_name:
            return
        
        try:
            speech_cfg = types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
                ),
                language_code="en-US",
            )
            config["speech_config"] = speech_cfg
        except Exception:
            # Failed to build speech config, continue without it
            pass
    
    @staticmethod
    def setup_vad_config(config: Dict[str, Any], enable_vad: bool):
        """Setup Voice Activity Detection if enabled."""
        if not enable_vad:
            return
        
        try:
            aad = types.AutomaticActivityDetection(
                disabled=False,
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                prefix_padding_ms=20,
                silence_duration_ms=100,
            )
            config["realtime_input_config"] = types.RealtimeInputConfig(
                automatic_activity_detection=aad
            )
        except Exception:
            # Failed to build realtime input config, continue without it
            pass


class PDFSyncManager:
    """Synchronize PDF field updates.

    Dual-mode behavior:
      1. Direct in-process sync (preferred): When running inside the unified app
         (HTTP + WS in same process) we can update the SessionManager directly
         without an HTTP round trip.
      2. HTTP fallback: If direct session lookup fails (e.g., legacy multi-process
         invocation) we fall back to the previous HTTP POST behavior.

    This preserves backward compatibility while eliminating latency and
    intermittent 404s that occurred when the HTTP server had not yet registered
    the form session at the moment of a tool call.
    """

    def __init__(self, form_id: Optional[str]):
        self.form_id = form_id
        self.full_sync_pending = False
        self._direct_mode_checked = False
        self._direct_mode = False
        self._session_manager = None

    def _detect_direct_mode(self):
        if self._direct_mode_checked:
            return
        self._direct_mode_checked = True
        if not self.form_id:
            return
        try:
            # Use the same session manager instance as HTTP server
            import server
            if server.session_manager.get_session(self.form_id) is not None:
                self._direct_mode = True
                self._session_manager = server.session_manager
        except Exception:  # noqa: BLE001
            # Direct mode detection failed, will use HTTP fallback
            pass

    async def sync_updates(self, applied: Dict[str, Any]):
        """Apply field updates either directly or via HTTP fallback."""
        if not self.form_id or not applied:
            return

        # Detect mode once
        self._detect_direct_mode()

        if self._direct_mode and self._session_manager is not None:
            try:
                changed = self._session_manager.update_session_state(self.form_id, applied)
                if changed is None:
                    # Session vanished; fall back to HTTP so caller behavior stays consistent
                    self._direct_mode = False
                else:
                    return  # Direct path done
            except Exception:  # noqa: BLE001
                # Direct update failed, falling back to HTTP
                self._direct_mode = False

        # HTTP fallback path
        payload = {"form_id": self.form_id, "updates": applied}
        try:
            if aiohttp is not None:
                await self._sync_with_aiohttp(payload)
            else:
                await self._sync_with_urllib(payload)
        except Exception:  # noqa: BLE001
            # PDF sync failed silently
            pass

    async def _sync_with_aiohttp(self, payload: Dict[str, Any]):
        """Sync using aiohttp if available."""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
            async with session.post("http://localhost:8000/update_form_state", json=payload) as resp:
                # Silently handle HTTP errors without logging
                pass

    async def _sync_with_urllib(self, payload: Dict[str, Any]):
        """Sync using urllib as fallback."""
        sync_payload = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url="http://localhost:8000/update_form_state",
            data=sync_payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        loop = asyncio.get_event_loop()

        def _do_sync():
            try:
                with urllib.request.urlopen(req, timeout=2) as r:
                    r.read()
            except Exception:  # noqa: BLE001
                # Sync failed silently
                pass

        await loop.run_in_executor(None, _do_sync)

    async def schedule_full_sync(self, form_manager: FormManager):
        """Debounced full sync (no-op when in direct mode)."""
        if not self.form_id or self.full_sync_pending:
            return
        # If direct mode we do not need full sync because each update is applied immediately
        if self._direct_mode:
            return

        self.full_sync_pending = True
        await asyncio.sleep(PDF_SYNC_DELAY)

        try:
            if form_manager.form_state:
                filled_state = {k: v for k, v in form_manager.form_state.state.items() if v}
                await self.sync_updates(filled_state)
        finally:
            self.full_sync_pending = False


async def measure_latency(client_websocket: websockets.ServerProtocol, logger: LatencyLogger, client_addr: str):
    """Periodically measure and log WebSocket latency."""
    while not client_websocket.closed:
        try:
            start_time = time.time()
            pong_waiter = await client_websocket.ping()
            await pong_waiter
            latency = time.time() - start_time
            latency_ms = latency * 1000
            logger.log_latency(client_addr, latency_ms)
            
            # Log built-in latency if available
            if hasattr(client_websocket, 'latency') and client_websocket.latency is not None:
                built_in_latency_ms = client_websocket.latency * 1000
                logger.logger.info(f"Built-in latency: {built_in_latency_ms:.2f}ms (client: {client_addr})")
            
            await asyncio.sleep(LATENCY_MEASUREMENT_INTERVAL)
        except websockets.exceptions.ConnectionClosed:
            logger.logger.info(f"Latency measurement stopped - connection closed (client: {client_addr})")
            break
        except Exception as e:
            logger.log_warning(client_addr, f"Latency measurement error: {e}")
            await asyncio.sleep(LATENCY_MEASUREMENT_INTERVAL)


async def setup_session(session, form_manager: FormManager):
    """Send initial system instruction and priming message."""
    try:
        # Send system instruction
        system_instruction = form_manager.get_system_instruction()
        await session.send_realtime_input(text=system_instruction)
        
        # Send initial message
        initial_message = form_manager.get_initial_message()
        await session.send_realtime_input(text=initial_message)
        
        # Small delay to ensure messages are processed
        await asyncio.sleep(0.1)
    except Exception:
        # Failed to send system instruction, continue silently
        pass


async def handle_realtime_input(data: Dict[str, Any], session, form_manager: FormManager, pdf_sync: PDFSyncManager):
    """Handle realtime input from client."""
    ri = data["realtime_input"]
    audio_handler = get_audio_handler()
    
    # Handle audio chunks
    await audio_handler.handle_realtime_audio_input(session, ri)
    
    # Handle text messages
    text_msg = ri.get("text")
    if isinstance(text_msg, str) and text_msg.strip():
        await audio_handler.handle_text_input(session, text_msg)
    
    # Handle audio stream end
    if ri.get("audio_stream_end") is True:
        await audio_handler.handle_audio_stream_end(session)


async def handle_user_edit(data: Dict[str, Any], session, form_manager: FormManager, pdf_sync: PDFSyncManager):
    """Handle user field edits from the client."""
    ue = data["user_edit"]
    field = ue.get("field")
    value = ue.get("value")
    
    if not field or not form_manager.form_state:
        return
    
    if field in form_manager.form_state.state:
        form_manager.form_state.state[field] = str(value)[:500]
        form_manager.form_state.confirmed[field] = True
        
        missing = form_manager.get_missing_fields()
        msg = (
            f"User explicitly set {field} = {value}. Ask only for the next missing field."
            if missing else "All fields now provided. Ask user for final confirmation."
        )
        
        try:
            await session.send_realtime_input(text=msg)
        except Exception:
            # Error sending user edit delta, continue silently
            pass
        
        # Sync this user edit
        await pdf_sync.sync_updates({field: form_manager.form_state.state[field]})
        await pdf_sync.schedule_full_sync(form_manager)


async def handle_form_confirmation(data: Dict[str, Any], session, form_manager: FormManager, 
                                 client_websocket: websockets.ServerProtocol, pdf_sync: PDFSyncManager):
    """Handle form confirmation from client."""
    if "confirm_form" not in data:
        return
    
    # Mark session as download confirmed regardless of completeness
    if form_manager.form_state:
        # Use the same session manager instance as the HTTP server
        import server
        form_id = form_manager.form_state.form_id
        server.session_manager.confirm_session_download(form_id)
    
    # Final full sync before signaling readiness
    try:
        if form_manager.form_state:
            filled_state = {k: v for k, v in form_manager.form_state.state.items() if v}
            await pdf_sync.sync_updates(filled_state)
    except Exception:
        # Final sync error, continue silently
        pass
    
    try:
        await session.send_realtime_input(text="User confirmed all fields. Session will conclude.")
    except Exception:
        pass
    
    # Notify UI to enable download
    try:
        form_id = getattr(form_manager.form_state, 'form_id', None)
        await client_websocket.send(json.dumps({"download_ready": True, "form_id": form_id}))
    except Exception:
        # Failed to send download_ready message
        pass
    
    await asyncio.sleep(0.4)
    try:
        await session.close()
    except Exception:
        pass


def create_websocket_server():
    """Create and configure the WebSocket server."""
    return websockets.serve(
        gemini_session_handler,
        "localhost",
        9082,
        ping_interval=WEBSOCKET_PING_INTERVAL,
        ping_timeout=WEBSOCKET_PING_TIMEOUT
    )


# This will be implemented in the next step when we refactor main.py
async def gemini_session_handler(client_websocket: websockets.ServerProtocol):
    """Main WebSocket session handler - to be implemented in main.py refactor."""
    pass