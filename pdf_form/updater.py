from __future__ import annotations
"""Updater logic for applying incremental PDF field updates.

apply_pdf_field_updates(updates, session_state, allowed_fields) returns a summary dict.
"""
from typing import Dict, Any, List
import time

def apply_pdf_field_updates(updates: Dict[str, str], state: Dict[str, Any], confirmed: Dict[str, bool], allowed_fields: List[str]):
    allowed = set(allowed_fields)
    applied = {}
    unknown_fields = []
    conflicts_user_locked = []  # placeholder if you later track user vs AI provenance
    unchanged = []

    # Ensure all keys processed deterministically
    for key, value in updates.items():
        if not isinstance(key, str):
            continue
        k = key.strip()
        if k not in allowed:
            unknown_fields.append(k)
            continue
        if value is None:
            continue
        s = str(value).strip()
        if not s:
            continue
        current_val = state.get(k)
        if current_val == s:
            unchanged.append(k)
            continue
        # (Provenance logic could be inserted here)
        state[k] = s[:500]
        confirmed[k] = True
        applied[k] = state[k]

    empty = [f for f in allowed_fields if not state.get(f)]
    filled = [f for f in allowed_fields if state.get(f)]
    summary = {
        "applied": applied,
        "unknown_fields": unknown_fields,
        "conflicts_user_locked": conflicts_user_locked,
        "unchanged": unchanged,
        "remaining_sample": empty[:8],
        "remaining_empty_count": len(empty),
        "filled_count": len(filled),
        "complete": len(empty) == 0,
        }
    return summary
