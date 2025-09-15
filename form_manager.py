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
    
    def get_state_snapshot(self) -> Dict[str, Any]:
        """Get current form state snapshot."""
        return self.form_state.get_snapshot()
    
    def get_missing_fields(self) -> List[str]:
        """Get list of missing fields."""
        return self.form_state.get_missing_fields()
    
    def update_fields(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Update form fields."""
        # For PDF mode, expect updates as JSON string
        updates_json = updates.get("updates", "{}")
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
        first_field = self.form_state.field_names[0] if self.form_state.field_names else "first field"
        return f"{catalog_msg}\n\nCatalog hash: {self.form_state.catalog['hash']}\nBegin by requesting the value for the first missing field: {first_field}"
    
    def get_tool_declarations(self) -> List[Dict[str, Any]]:
        """Get appropriate tool declarations for the form."""
        from config import PDF_TOOL_DECLARATIONS
        
        return PDF_TOOL_DECLARATIONS