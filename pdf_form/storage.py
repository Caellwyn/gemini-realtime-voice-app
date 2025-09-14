from __future__ import annotations
import os, time, shutil, uuid, threading
from typing import Optional, Dict

DEFAULT_TIMEOUT_SECS = 600  # 10 minutes inactivity

class FormStorageManager:
    def __init__(self, base_dir: str = "tmp_forms", inactivity_timeout: int = DEFAULT_TIMEOUT_SECS):
        self.base_dir = base_dir
        self.inactivity_timeout = inactivity_timeout
        os.makedirs(self.base_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._sessions: Dict[str, float] = {}

    def _session_path(self, form_id: str) -> str:
        return os.path.join(self.base_dir, form_id)

    def create(self, original_pdf_bytes: bytes, original_filename: str, form_id: str | None = None) -> str:
        form_id = form_id or uuid.uuid4().hex
        path = self._session_path(form_id)
        os.makedirs(path, exist_ok=True)
        pdf_path = os.path.join(path, "original.pdf")
        with open(pdf_path, "wb") as f:
            f.write(original_pdf_bytes)
        meta_path = os.path.join(path, "meta.txt")
        with open(meta_path, "w", encoding="utf-8") as m:
            m.write(original_filename)
        with self._lock:
            self._sessions[form_id] = time.time()
        return form_id

    def touch(self, form_id: str):
        with self._lock:
            if form_id in self._sessions:
                self._sessions[form_id] = time.time()

    def load_original(self, form_id: str) -> Optional[bytes]:
        path = self._session_path(form_id)
        pdf_path = os.path.join(path, "original.pdf")
        if not os.path.exists(pdf_path):
            return None
        with open(pdf_path, "rb") as f:
            return f.read()

    def save_filled(self, form_id: str, filled_pdf_bytes: bytes):
        path = self._session_path(form_id)
        out_path = os.path.join(path, "filled.pdf")
        with open(out_path, "wb") as f:
            f.write(filled_pdf_bytes)

    def get_filled_path(self, form_id: str) -> Optional[str]:
        path = self._session_path(form_id)
        out_path = os.path.join(path, "filled.pdf")
        return out_path if os.path.exists(out_path) else None

    def delete(self, form_id: str):
        path = self._session_path(form_id)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        with self._lock:
            self._sessions.pop(form_id, None)

    def cleanup_inactive(self):
        now = time.time()
        stale = []
        with self._lock:
            for fid, ts in list(self._sessions.items()):
                if now - ts > self.inactivity_timeout:
                    stale.append(fid)
        for fid in stale:
            self.delete(fid)

    def start_background_cleanup(self, interval: int = 120):  # every 2 minutes
        def loop():
            while True:
                try:
                    self.cleanup_inactive()
                except Exception:
                    pass
                time.sleep(interval)
        t = threading.Thread(target=loop, daemon=True)
        t.start()
