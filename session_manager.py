"""
Session management for form sessions.
Replaces the global FORM_SESSIONS dictionary with a proper class-based approach.
"""

import time
import threading
from typing import Dict, Optional, Any, List
from dataclasses import dataclass
from pdf_form.schema import FormSchema
from config import FORM_SESSION_TIMEOUT, SESSION_CLEANUP_INTERVAL


@dataclass
class FormSession:
    """Represents a form session with metadata."""
    form_id: str
    schema: FormSchema
    state: Dict[str, Any]
    confirmed: Dict[str, bool]
    last_activity: float
    completed: bool = False
    created_at: float = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = time.time()
    
    def touch(self):
        """Update the last activity timestamp."""
        self.last_activity = time.time()
    
    def is_expired(self, timeout: float = FORM_SESSION_TIMEOUT) -> bool:
        """Check if the session has expired."""
        return time.time() - self.last_activity > timeout
    
    def get_missing_fields(self) -> List[str]:
        """Get list of fields that are not filled."""
        return [k for k, v in self.state.items() if not v]
    
    def is_complete(self) -> bool:
        """Check if all fields are filled."""
        return all(self.state.values())
    
    def update_field(self, field_name: str, value: Any) -> bool:
        """Update a single field value. Returns True if field exists."""
        if field_name not in self.state:
            return False
        
        # Coerce value to string and truncate if necessary
        if isinstance(value, str):
            coerced_value = value.strip()
        elif isinstance(value, (int, float)):
            coerced_value = str(value)
        elif value is None:
            return False
        else:
            # Serialize complex objects to JSON string (truncated)
            try:
                import json
                coerced_value = json.dumps(value)[:500]
            except Exception:
                coerced_value = str(value)[:500]
        
        if not coerced_value:
            return False
        
        self.state[field_name] = coerced_value[:500]
        self.confirmed[field_name] = True
        self.touch()
        return True
    
    def update_multiple_fields(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Update multiple fields. Returns dict of successfully updated fields."""
        updated = {}
        for field_name, value in updates.items():
            if self.update_field(field_name, value):
                updated[field_name] = self.state[field_name]
        
        if updated:
            self.completed = self.is_complete()
        
        return updated


class SessionManager:
    """Manages form sessions with automatic cleanup and thread safety."""
    
    def __init__(self, storage_manager=None):
        self._sessions: Dict[str, FormSession] = {}
        self._lock = threading.RLock()
        self._storage_manager = storage_manager
        self._cleanup_thread = None
        self._stop_cleanup = False
        
    def start_cleanup_thread(self):
        """Start the background cleanup thread."""
        if self._cleanup_thread is not None:
            return
        
        self._stop_cleanup = False
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, 
            daemon=True,
            name="SessionCleanup"
        )
        self._cleanup_thread.start()
    
    def stop_cleanup_thread(self):
        """Stop the background cleanup thread."""
        self._stop_cleanup = True
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=1.0)
            self._cleanup_thread = None
    
    def _cleanup_loop(self):
        """Background cleanup loop."""
        while not self._stop_cleanup:
            try:
                self.cleanup_expired_sessions()
                time.sleep(SESSION_CLEANUP_INTERVAL)
            except Exception as e:
                print(f"[SessionManager] Cleanup error: {e}")
                time.sleep(SESSION_CLEANUP_INTERVAL)
    
    def create_session(self, form_id: str, schema: FormSchema) -> FormSession:
        """Create a new form session."""
        with self._lock:
            # Clean up any existing session with the same ID
            if form_id in self._sessions:
                self.delete_session(form_id)
            
            session = FormSession(
                form_id=form_id,
                schema=schema,
                state={fname: None for fname in schema.ordered_field_names()},
                confirmed={fname: False for fname in schema.ordered_field_names()},
                last_activity=time.time()
            )
            
            self._sessions[form_id] = session
            return session
    
    def get_session(self, form_id: str) -> Optional[FormSession]:
        """Get a session by form ID."""
        with self._lock:
            session = self._sessions.get(form_id)
            if session:
                session.touch()
            return session
    
    def delete_session(self, form_id: str) -> bool:
        """Delete a session by form ID."""
        with self._lock:
            if form_id not in self._sessions:
                return False
            
            del self._sessions[form_id]
            
            # Also delete from storage if available
            if self._storage_manager:
                try:
                    self._storage_manager.delete(form_id)
                except Exception as e:
                    print(f"[SessionManager] Storage deletion error for {form_id}: {e}")
            
            return True
    
    def update_session_state(self, form_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Update session state with multiple field values."""
        with self._lock:
            session = self.get_session(form_id)
            if not session:
                return None
            
            return session.update_multiple_fields(updates)
    
    def get_session_status(self, form_id: str) -> Optional[Dict[str, Any]]:
        """Get session status information."""
        with self._lock:
            session = self.get_session(form_id)
            if not session:
                return None
            
            missing_fields = session.get_missing_fields()
            return {
                'remaining': missing_fields,
                'complete': len(missing_fields) == 0,
                'last_activity': session.last_activity,
                'created_at': session.created_at
            }
    
    def cleanup_expired_sessions(self) -> int:
        """Clean up expired sessions. Returns number of sessions cleaned up."""
        cleaned_count = 0
        
        with self._lock:
            expired_ids = []
            for form_id, session in self._sessions.items():
                if session.is_expired():
                    expired_ids.append(form_id)
            
            for form_id in expired_ids:
                if self.delete_session(form_id):
                    cleaned_count += 1
                    print(f"[SessionManager] Removed expired session {form_id}")
        
        return cleaned_count
    
    def clear_all_sessions(self):
        """Clear all sessions (useful for reset operations)."""
        with self._lock:
            form_ids = list(self._sessions.keys())
            for form_id in form_ids:
                self.delete_session(form_id)
    
    def get_session_count(self) -> int:
        """Get the current number of active sessions."""
        with self._lock:
            return len(self._sessions)
    
    def get_all_session_ids(self) -> List[str]:
        """Get list of all active session IDs."""
        with self._lock:
            return list(self._sessions.keys())
    
    def __len__(self):
        """Return number of active sessions."""
        return self.get_session_count()
    
    def __contains__(self, form_id: str):
        """Check if a session exists."""
        with self._lock:
            return form_id in self._sessions
    
    def __del__(self):
        """Cleanup when the manager is destroyed."""
        self.stop_cleanup_thread()


# Global session manager instance
_session_manager = None


def get_session_manager(storage_manager=None) -> SessionManager:
    """Get or create the global session manager instance."""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager(storage_manager)
        _session_manager.start_cleanup_thread()
    return _session_manager


def reset_session_manager():
    """Reset the global session manager (useful for testing)."""
    global _session_manager
    if _session_manager:
        _session_manager.stop_cleanup_thread()
    _session_manager = None