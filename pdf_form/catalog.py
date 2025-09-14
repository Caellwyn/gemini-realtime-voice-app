from __future__ import annotations
"""Catalog utilities for PDF form field listing and hashing.

Provides:
  - compute_field_catalog(fields) -> dict with fields(list) and hash(str)
  - build_initial_system_message(fields, hash) -> str instruction text
"""
import hashlib, json
from typing import List, Dict

CATALOG_HASH_LEN = 16

def compute_field_catalog(field_names: List[str]) -> Dict[str, object]:
    canonical = sorted(field_names)
    raw = json.dumps(canonical, separators=(",", ":"))
    h = hashlib.sha256(raw.encode()).hexdigest()[:CATALOG_HASH_LEN]
    return {"fields": canonical, "hash": h}

def build_initial_system_message(field_names: List[str], catalog_hash: str) -> str:
    json_list = json.dumps(field_names, ensure_ascii=False)
    return (
        f"PDF Form Field Catalog (hash={catalog_hash})\n"
        "Use ONLY these exact field names when calling update_pdf_fields.\n"
        "Field list JSON: " + json_list + "\n"
        "Call update_pdf_fields with 'updates' parameter as a JSON string mapping field names to values.\n"
        "Example: updates = '{\"FirstName\": \"Alice\", \"LastName\": \"Smith\"}'\n"
        "MANDATORY: Call update_pdf_fields immediately after EVERY user utterance that provides any field value(s).\n"
        "If you lose track of fields or values CALL get_form_state instead of guessing.\n"
        "Rules:\n"
        "- ALWAYS call update_pdf_fields when user provides field values, then ask for next field.\n"
        "- Update incrementally; only send fields explicitly provided by the user in that utterance.\n"
        "- Never invent or partially guess; ask a clarifying question instead.\n"
        "- Omit already correct / previously set fields.\n"
        "- If user provides multiple fields in one utterance you may include all of them in one update_pdf_fields call.\n"
        "Field names (original order, one per line):\n" + "\n".join(field_names)
    )
