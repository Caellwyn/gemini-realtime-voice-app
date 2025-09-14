from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
import time

Rect = Tuple[float, float, float, float]

@dataclass
class FormField:
    name: str
    display_name: str
    page: int
    rect: Optional[Rect]
    field_type: str  # Raw AcroForm type e.g. 'Tx', 'Btn', 'Ch'
    original_name: str

@dataclass
class FormSchema:
    form_id: str
    fields: List[FormField]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def ordered_field_names(self) -> List[str]:
        return [f.name for f in self.fields]

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "form_id": self.form_id,
            "field_count": len(self.fields),
            "fields": [
                {
                    "name": f.name,
                    "display_name": f.display_name,
                    "page": f.page,
                    "field_type": f.field_type,
                    "rect": f.rect,
                } for f in self.fields
            ],
            "metadata": self.metadata,
        }
