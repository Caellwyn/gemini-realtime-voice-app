"""
Audio handling utilities for WebSocket communication.
Consolidates audio processing logic that was scattered across multiple functions.
"""

import base64
import json
from typing import Dict, Any, List, Optional
import websockets
from google.genai import types
from config import AUDIO_MIME_TYPE


class AudioChunk:
    """Represents a single audio chunk with metadata."""
    
    def __init__(self, data: bytes, mime_type: str = AUDIO_MIME_TYPE):
        self.data = data
        self.mime_type = mime_type
        self.size = len(data)
    
    @classmethod
    def from_base64(cls, base64_data: str, mime_type: str = AUDIO_MIME_TYPE) -> 'AudioChunk':
        """Create AudioChunk from base64 encoded data."""
        try:
            data = base64.b64decode(base64_data)
            return cls(data, mime_type)
        except Exception as e:
            raise ValueError(f"Invalid base64 audio data: {e}")
    
    def to_base64(self) -> str:
        """Convert audio data to base64 string."""
        return base64.b64encode(self.data).decode('utf-8')
    
    def to_gemini_blob(self) -> types.Blob:
        """Convert to Gemini API Blob format."""
        return types.Blob(data=self.data, mime_type=self.mime_type)


class AudioProcessor:
    """Processes audio data for WebSocket communication."""
    
    @staticmethod
    def extract_audio_chunks(realtime_input: Dict[str, Any]) -> List[AudioChunk]:
        """
        Extract audio chunks from realtime input message.
        
        Args:
            realtime_input: The realtime_input portion of a WebSocket message
            
        Returns:
            List of AudioChunk objects
        """
        chunks = []
        media_chunks = realtime_input.get("media_chunks", [])
        
        for chunk_data in media_chunks:
            if chunk_data.get("mime_type") == "audio/pcm" and "data" in chunk_data:
                try:
                    audio_chunk = AudioChunk.from_base64(
                        chunk_data["data"], 
                        chunk_data.get("mime_type", AUDIO_MIME_TYPE)
                    )
                    chunks.append(audio_chunk)
                except ValueError as e:
                    print(f"Failed to process audio chunk: {e}")
                    continue
        
        return chunks
    
    @staticmethod
    def create_audio_response(audio_data: bytes, mime_type: str = AUDIO_MIME_TYPE) -> Dict[str, Any]:
        """
        Create a WebSocket audio response message.
        
        Args:
            audio_data: Raw audio bytes
            mime_type: MIME type of the audio
            
        Returns:
            Dictionary ready to be sent as JSON over WebSocket
        """
        base64_audio = base64.b64encode(audio_data).decode('utf-8')
        return {
            "audio": base64_audio,
            "audio_mime_type": mime_type
        }


class AudioStreamHandler:
    """Handles audio streaming for WebSocket sessions."""
    
    def __init__(self):
        self.chunks_processed = 0
        self.total_bytes_processed = 0
        self.last_chunk_time = None
    
    async def send_audio_chunks_to_gemini(self, session, audio_chunks: List[AudioChunk]) -> int:
        """
        Send multiple audio chunks to Gemini API.
        
        Args:
            session: Gemini API session
            audio_chunks: List of audio chunks to send
            
        Returns:
            Number of chunks successfully sent
        """
        sent_count = 0
        
        for chunk in audio_chunks:
            try:
                await session.send_realtime_input(media=chunk.to_gemini_blob())
                sent_count += 1
                self.chunks_processed += 1
                self.total_bytes_processed += chunk.size
                
            except websockets.exceptions.ConnectionClosed as e:
                if e.code == 1011:
                    print("Audio send failed: keepalive timeout")
                else:
                    print(f"Audio send failed: connection closed ({e})")
                raise
            except Exception as e:
                print(f"Failed to send audio chunk: {e}")
                continue
        
        return sent_count
    
    async def send_audio_response_to_client(self, client_websocket: websockets.ServerProtocol, 
                                          audio_data: bytes, mime_type: str = AUDIO_MIME_TYPE) -> bool:
        """
        Send audio response to client WebSocket.
        
        Args:
            client_websocket: Client WebSocket connection
            audio_data: Raw audio bytes to send
            mime_type: MIME type of the audio
            
        Returns:
            True if sent successfully, False otherwise
        """
        try:
            response = AudioProcessor.create_audio_response(audio_data, mime_type)
            await client_websocket.send(json.dumps(response))
            return True
        except Exception as e:
            print(f"Failed to send audio to client: {e}")
            return False
    
    def get_stats(self) -> Dict[str, Any]:
        """Get audio processing statistics."""
        return {
            "chunks_processed": self.chunks_processed,
            "total_bytes_processed": self.total_bytes_processed,
            "average_chunk_size": (
                self.total_bytes_processed / self.chunks_processed 
                if self.chunks_processed > 0 else 0
            )
        }
    
    def reset_stats(self):
        """Reset processing statistics."""
        self.chunks_processed = 0
        self.total_bytes_processed = 0
        self.last_chunk_time = None


class AudioMessageHandler:
    """High-level handler for audio-related WebSocket messages."""
    
    def __init__(self):
        self.stream_handler = AudioStreamHandler()
        self.processor = AudioProcessor()
    
    async def handle_realtime_audio_input(self, session, realtime_input: Dict[str, Any]) -> bool:
        """
        Handle audio input from realtime_input WebSocket message.
        
        Args:
            session: Gemini API session
            realtime_input: The realtime_input portion of the message
            
        Returns:
            True if any audio was processed, False otherwise
        """
        audio_chunks = self.processor.extract_audio_chunks(realtime_input)
        
        if not audio_chunks:
            return False
        
        sent_count = await self.stream_handler.send_audio_chunks_to_gemini(session, audio_chunks)
        return sent_count > 0
    
    async def handle_text_input(self, session, text: str) -> bool:
        """
        Handle text input from realtime_input WebSocket message.
        
        Args:
            session: Gemini API session
            text: Text message to send
            
        Returns:
            True if sent successfully, False otherwise
        """
        if not text or not text.strip():
            return False
        
        try:
            await session.send_realtime_input(text=text.strip())
            return True
        except Exception as e:
            print(f"Error sending text to Gemini: {e}")
            return False
    
    async def handle_audio_stream_end(self, session) -> bool:
        """
        Handle audio stream end signal.
        
        Args:
            session: Gemini API session
            
        Returns:
            True if sent successfully, False otherwise
        """
        try:
            await session.send_realtime_input(audio_stream_end=True)
            return True
        except Exception as e:
            print(f"audio_stream_end error: {e}")
            return False
    
    async def process_gemini_audio_response(self, client_websocket: websockets.ServerProtocol, 
                                          audio_part) -> bool:
        """
        Process audio response from Gemini and send to client.
        
        Args:
            client_websocket: Client WebSocket connection
            audio_part: Audio part from Gemini response
            
        Returns:
            True if processed successfully, False otherwise
        """
        if not hasattr(audio_part, 'inline_data') or audio_part.inline_data is None:
            return False
        
        mime_type = getattr(audio_part.inline_data, 'mime_type', AUDIO_MIME_TYPE)
        success = await self.stream_handler.send_audio_response_to_client(
            client_websocket, 
            audio_part.inline_data.data, 
            mime_type
        )
        
        return success
    
    def get_processing_stats(self) -> Dict[str, Any]:
        """Get audio processing statistics."""
        return self.stream_handler.get_stats()


# Global audio handler instance for convenience
_audio_handler = None


def get_audio_handler() -> AudioMessageHandler:
    """Get or create the global audio handler instance."""
    global _audio_handler
    if _audio_handler is None:
        _audio_handler = AudioMessageHandler()
    return _audio_handler


def reset_audio_handler():
    """Reset the global audio handler (useful for testing)."""
    global _audio_handler
    _audio_handler = None