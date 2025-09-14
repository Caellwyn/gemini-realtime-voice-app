"""Shared in-memory session state for PDF form mode.

Provides:
  - storage_manager (FormStorageManager)
  - FORM_SESSIONS mapping
  - touch(form_id)
  - background cleanup for inactive sessions (10 min)
"""
from __future__ import annotations
import time
import threading
from typing import Dict, Any
from .storage import FormStorageManager

INACTIVITY_TIMEOUT = 600  # 10 minutes

storage_manager = FormStorageManager()
storage_manager.start_background_cleanup()  # file-level cleanup (original + filled)

# form_id -> { schema, state, confirmed, last_activity, completed }
FORM_SESSIONS: Dict[str, Dict[str, Any]] = {}

def touch(form_id: str):
    sess = FORM_SESSIONS.get(form_id)
    if sess:
        sess['last_activity'] = time.time()
        storage_manager.touch(form_id)


def _cleanup_form_sessions(interval: int = 180):  # every 3 minutes
    def loop():
        while True:
            try:
                now = time.time()
                stale = []
                for fid, data in list(FORM_SESSIONS.items()):
                    if now - data.get('last_activity', now) > INACTIVITY_TIMEOUT:
                        stale.append(fid)
                for fid in stale:
                    try:
                        del FORM_SESSIONS[fid]
                        storage_manager.delete(fid)
                        print(f"[session_state] Removed inactive form session {fid}")
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(interval)
    t = threading.Thread(target=loop, daemon=True)
    t.start()

_cleanup_form_sessions()

__all__ = [
    'FORM_SESSIONS',
    'storage_manager',
    'touch',
    'INACTIVITY_TIMEOUT'
]
