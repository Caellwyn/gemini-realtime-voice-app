import json, time, threading, os
from typing import Dict, Any

_LOG_LOCK = threading.Lock()
LOG_FILE = os.path.join(os.getcwd(), "tool_calls.log")

def log_tool_call(session_id: str, tool_name: str, request: Dict[str, Any], response: Dict[str, Any], started_ts: float):
    try:
        rec = {
            "ts": time.time(),
            "duration_ms": round((time.time() - started_ts) * 1000, 2),
            "session_id": session_id,
            "tool": tool_name,
            "request": request,
            "response_meta": {
                "applied_count": len(response.get("applied", {})) if isinstance(response, dict) else None,
                "unknown_count": len(response.get("unknown_fields", [])) if isinstance(response, dict) else None,
                "conflict_count": len(response.get("conflicts_user_locked", [])) if isinstance(response, dict) else None,
                "catalog_hash": response.get("catalog_hash") if isinstance(response, dict) else None,
            }
        }
        line = json.dumps(rec, ensure_ascii=False)
        with _LOG_LOCK:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass
