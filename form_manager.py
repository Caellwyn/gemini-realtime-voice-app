"""
Unified form management for both dating profiles and PDF forms.
Consolidates state management and validation logic.
"""

import json
import time
from typing import Dict, Any, List, Optional, Tuple
from pdf_form.catalog import compute_field_catalog, build_initial_system_message
from pdf_form.updater import apply_pdf_field_updates
from config import MAX_FIELD_VALUE_LENGTH

import fitz  # PyMuPDF

def extract_pdf_form_metadata(pdf_path: str):
    """
    Extract ordered field metadata from a fillable PDF form,
    distinguishing between checkboxes and radio buttons when possible.

    Args:
        pdf_path (str): Path to the PDF file.

    Returns:
        list[dict]: Ordered list of field metadata dictionaries.
    """
    type_map = {
        7: "string",   # text
        3: "dropdown", # choice field (list/combo box)
        2: "button"    # checkbox or radio (we refine below)
    }

    doc = fitz.open(pdf_path)
    fields = []

    for page_num, page in enumerate(doc):
        widgets = page.widgets()
        if not widgets:
            continue

        for w in widgets:
            base_type = type_map.get(w.field_type, "unknown")

            field_info = {
                "pdf_field_name": w.field_name,
                "base_type": base_type,
                "options": getattr(w, "choice_values", None),
                "tooltip": w.field_label or "",
                "rect": [w.rect.x0, w.rect.y0, w.rect.x1, w.rect.y1],
                "page": page_num,
                "export_value": getattr(w, "field_value", None),  # helps distinguish radios
            }
            fields.append(field_info)

    # --- Group detection for buttons ---
    # If multiple widgets share the same name => radio group
    name_counts = {}
    for f in fields:
        if f["base_type"] == "button":
            name_counts[f["pdf_field_name"]] = name_counts.get(f["pdf_field_name"], 0) + 1

    for f in fields:
        if f["base_type"] != "button":
            continue
        if name_counts[f["pdf_field_name"]] > 1:
            f["type"] = "radio"
        else:
            f["type"] = "checkbox"

    # Normalize non-buttons
    for f in fields:
        if f["base_type"] != "button":
            f["type"] = f["base_type"]

    # Sort by tab order if present, else fallback to visual order
    fields_sorted = sorted(fields, key=lambda f: (f["page"], f["rect"][1], f["rect"][0]))
    return fields_sorted

def extract_pdf_form_metadata_from_bytes(pdf_bytes: bytes):
    """
    Extract ordered field metadata from a fillable PDF form given raw bytes.
    Mirrors extract_pdf_form_metadata but accepts bytes and keeps the same
    output shape so downstream code can reuse it.

    Returns:
        list[dict]: Ordered list of field metadata dictionaries.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    type_map = {
        7: "string",   # text
        3: "dropdown", # choice field (list/combo box)
        2: "button"    # checkbox or radio (we refine below)
    }

    fields = []
    for page_num, page in enumerate(doc):
        widgets = page.widgets()
        if not widgets:
            continue
        for w in widgets:
            base_type = type_map.get(w.field_type, "unknown")
            field_info = {
                "pdf_field_name": w.field_name,
                "base_type": base_type,
                "options": getattr(w, "choice_values", None),
                "tooltip": w.field_label or "",
                "rect": [w.rect.x0, w.rect.y0, w.rect.x1, w.rect.y1],
                "page": page_num,
                "export_value": getattr(w, "field_value", None),
            }
            fields.append(field_info)

    name_counts = {}
    for f in fields:
        if f["base_type"] == "button":
            name_counts[f["pdf_field_name"]] = name_counts.get(f["pdf_field_name"], 0) + 1

    for f in fields:
        if f["base_type"] != "button":
            continue
        f["type"] = "radio" if name_counts.get(f["pdf_field_name"], 0) > 1 else "checkbox"

    for f in fields:
        if f["base_type"] != "button":
            f["type"] = f["base_type"]

    fields_sorted = sorted(fields, key=lambda f: (f["page"], f["rect"][1], f["rect"][0]))
    return fields_sorted

class FormState:
    """Base class for form state management."""
    
    def __init__(self):
        self.state: Dict[str, Any] = {}
        self.confirmed: Dict[str, bool] = {}
        self.complete = False
        self.last_activity = time.time()
    
    def get_missing_fields(self) -> List[str]:
        """Return list of fields that are not filled."""
        return [k for k, v in self.state.items() if not v]
    
    def is_complete(self) -> bool:
        """Check if all fields are filled."""
        return all(self.state.values())
    
    def touch(self):
        """Update last activity timestamp."""
        self.last_activity = time.time()
    
    def get_snapshot(self) -> Dict[str, Any]:
        """Get current state snapshot."""
        return {
            "state": self.state.copy(),
            "missing": self.get_missing_fields(),
            "confirmed": self.confirmed.copy(),
            "complete": self.is_complete()
        }


class PDFFormState(FormState):
    """State management for PDF forms."""
    
    def __init__(self, field_names: List[str], form_id: str):
        super().__init__()
        self.field_names = field_names
        self.form_id = form_id
        self.state = {name: None for name in field_names}
        self.confirmed = {name: False for name in field_names}
        self.catalog = compute_field_catalog(field_names)
        self.all_confirmed = False
    
    def validate_and_update(self, updates_json: str) -> Dict[str, Any]:
        """Validate and apply updates to PDF form fields."""
        try:
            # Parse JSON string to dictionary
            if isinstance(updates_json, str):
                updates_dict = json.loads(updates_json)
            else:
                updates_dict = updates_json
                
            if not isinstance(updates_dict, dict):
                return {"applied": {}, "unknown_fields": [], "errors": ["updates must be a JSON object"]}
                
        except (json.JSONDecodeError, TypeError) as e:
            return {"applied": {}, "unknown_fields": [], "errors": [f"Invalid JSON: {e}"]}
        
        # Apply updates using existing updater logic
        summary = apply_pdf_field_updates(
            updates_dict, self.state, self.confirmed, self.field_names
        )
        summary["catalog_hash"] = self.catalog["hash"]
        
        self.touch()
        return summary
    
    def get_snapshot(self) -> Dict[str, Any]:
        """Get current state snapshot with PDF-specific metadata."""
        snapshot = super().get_snapshot()
        snapshot.update({
            "catalog_hash": self.catalog["hash"],
            "remaining_count": len(self.get_missing_fields()),
            "filled_count": len([k for k, v in self.state.items() if v]),
            "remaining_sample": self.get_missing_fields()[:10],
            "form_id": self.form_id
        })
        return snapshot


class FormManager:
    """Manager for PDF forms."""
    
    def __init__(self, field_names: List[str], form_id: str):
        self.form_state: PDFFormState = PDFFormState(field_names, form_id)
        self._alias_to_canonical = None  # populated from session schema metadata when available
    
    def get_state_snapshot(self) -> Dict[str, Any]:
        """Get current form state snapshot."""
        return self.form_state.get_snapshot()
    
    def get_missing_fields(self) -> List[str]:
        """Get list of missing fields."""
        return self.form_state.get_missing_fields()
    
    def update_fields(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Update form fields."""
        # For PDF mode, expect updates as JSON string mapping names->values
        updates_json = updates.get("updates", "{}")
        # Try to map display aliases to canonical names using the live session schema metadata
        try:
            # Import server session manager in-process (unified app)
            import server  # type: ignore
            session = server.session_manager.get_session(self.form_state.form_id)
            alias_map = None
            if session and session.schema and isinstance(updates_json, str):
                alias_map = session.schema.metadata.get("display_alias_to_canonical")
            if alias_map and isinstance(updates_json, str):
                try:
                    parsed = json.loads(updates_json)
                    if isinstance(parsed, dict):
                        # Build a fallback map that accepts base display names when unique
                        base_map = {}
                        try:
                            tmp = {}
                            for alias, canon in alias_map.items():
                                # Treat suffix pattern " #<n>" only at the end as disambiguation
                                base = alias.rsplit(" #", 1)[0] if alias.endswith(tuple(f" #{i}" for i in range(2, 10))) else alias
                                tmp.setdefault(base, set()).add(canon)
                            for base, cset in tmp.items():
                                if len(cset) == 1:
                                    base_map[base] = next(iter(cset))
                        except Exception:
                            pass
                        remapped = {}
                        for k, v in parsed.items():
                            key = alias_map.get(k)
                            if not key:
                                key = base_map.get(k, k)
                            remapped[key] = v
                        updates_json = json.dumps(remapped)
                except Exception:
                    # fall back to raw
                    pass
        except Exception:
            # If any error in alias mapping, continue with original
            pass
        return self.form_state.validate_and_update(updates_json)
    
    def get_system_instruction(self) -> str:
        """Get appropriate system instruction for the form."""
        from config import PDF_FORM_INSTRUCTION_TEMPLATE
        
        total_fields = len(self.form_state.field_names)
        return PDF_FORM_INSTRUCTION_TEMPLATE.format(total=total_fields)
    
    def get_initial_message(self) -> str:
        """Get initial message to send to the AI model."""
        catalog_msg = build_initial_system_message(
            self.form_state.field_names, 
            self.form_state.catalog["hash"]
        )
        # Prefer to speak and USE display names for tool calls; the backend maps to canonical.
        try:
            import server  # type: ignore
            session = server.session_manager.get_session(self.form_state.form_id)
            display_list = None
            group_lines: List[str] = []
            allowed_lines: List[str] = []
            if session and session.schema:
                # Build disambiguated display alias list in order, matching server mapping logic
                raw_list = [getattr(f, 'display_name', f.name) or f.name for f in session.schema.fields]
                counts = {}
                disamb = []
                for name in raw_list:
                    base = name
                    if base in counts:
                        counts[base] += 1
                        disamb.append(f"{base} #{counts[base]}")
                    else:
                        counts[base] = 1
                        disamb.append(base)
                display_list = disamb
                # Build allowed values lines for radio/choice fields with their disambiguated aliases
                try:
                    for i, field in enumerate(session.schema.fields):
                        try:
                            kind = getattr(field, 'kind', None) or getattr(field, 'field_type', None)
                            allowed = getattr(field, 'allowed_values', None)
                            alias = display_list[i]
                            if kind in ('choice', 'radio') and allowed:
                                allowed_lines.append(f"{alias}: {', '.join(map(str, allowed))}")
                        except Exception:
                            continue
                except Exception:
                    pass
                # Surface any groups from normalizer metadata
                try:
                    groups = (session.schema.metadata or {}).get('groups') or []
                    for g in groups:
                        label = (g.get('group_label') or g.get('group_id') or '').strip()
                        opts = g.get('options') or []
                        kind = (g.get('kind') or '').strip()
                        suffix = ''
                        if kind == 'checkbox':
                            suffix = ' (multi-select: check all that apply)'
                        elif kind == 'radio':
                            suffix = ' (single select)'
                        if label:
                            if opts:
                                group_lines.append(f"Group: {label}{suffix} — options: {', '.join(opts)}")
                            else:
                                group_lines.append(f"Group: {label}{suffix}")
                except Exception:
                    pass
            if display_list:
                first_display = display_list[0]
                alias_note = (
                    "IMPORTANT: When calling update_pdf_fields, use the DISPLAY NAMES exactly as listed below. "
                    "The server will map them to canonical field names."
                )
                msg = (
                    f"{catalog_msg}\n\nCatalog hash: {self.form_state.catalog['hash']}\n"
                    f"Display names (use these in tool calls, same order):\n" + "\n".join(display_list) + "\n" + alias_note + "\n"
                )
                if allowed_lines:
                    msg += ("\nAllowed values for dropdown/radio fields (use exactly as shown):\n" + "\n".join(allowed_lines) + "\n")
                if group_lines:
                    msg += ("\nRecognized groups (some fields are part of a single question with options):\n" + "\n".join(group_lines) + "\n")
                    msg += ("When updating grouped fields, send updates for each field within the group as needed. For checkbox groups, multiple options may be true. For radio groups, choose exactly one value.\n")
                msg += (f"Begin by requesting the value for the first missing field: {first_display}")
                return msg
        except Exception:
            pass
        first_field = self.form_state.field_names[0] if self.form_state.field_names else "first field"
        return f"{catalog_msg}\n\nCatalog hash: {self.form_state.catalog['hash']}\nBegin by requesting the value for the first missing field: {first_field}"
    
    def get_tool_declarations(self) -> List[Dict[str, Any]]:
        """Get appropriate tool declarations for the form."""
        from config import PDF_TOOL_DECLARATIONS
        
        return PDF_TOOL_DECLARATIONS