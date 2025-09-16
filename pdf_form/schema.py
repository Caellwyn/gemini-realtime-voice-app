from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple, Literal
import time

Rect = Tuple[float, float, float, float]

FieldKind = Literal["text", "checkbox", "radio", "choice"]


@dataclass
class FormField:
    """Represents a single form field with normalized kind.

    Original extraction used raw AcroForm /FT values (Tx, Btn, Ch). We now map those to
    higher level kinds for application + model use while retaining backward compatibility.
    """
    name: str
    display_name: str
    page: int
    rect: Optional[Rect]
    raw_field_type: str  # Raw AcroForm type e.g. 'Tx', 'Btn', 'Ch'
    original_name: str
    kind: FieldKind = "text"  # normalized kind (text, checkbox, radio, choice)
    allowed_values: Optional[List[str]] = None  # radio/choice enumerations
    group_name: Optional[str] = None  # radio grouping key

    def to_public(self) -> Dict[str, Any]:  # stable outward shape
        return {
            "name": self.name,
            "display_name": self.display_name,
            "page": self.page,
            "field_type": self.kind,  # expose normalized kind
            "rect": self.rect,
            **({"allowed_values": self.allowed_values} if self.allowed_values else {}),
        }

@dataclass
class FormSchema:
    form_id: str
    fields: List[FormField]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def ordered_field_names(self) -> List[str]:
        return [f.name for f in self.fields]

    def to_public_dict(self) -> Dict[str, Any]:
        """Return a public dictionary consumed by the frontend & model tooling.

        Backward compatibility: Previously "field_type" exposed raw PDF /FT tokens. Now
        we expose normalized kinds. If a field lacks normalization (older sessions), we
        fall back to treating raw_field_type == 'Tx' as text, else text.
        """
        return {
            "form_id": self.form_id,
            "field_count": len(self.fields),
            "fields": [f.to_public() for f in self.fields],
            "metadata": self.metadata,
        }
