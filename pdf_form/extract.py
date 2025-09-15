from __future__ import annotations
from typing import List, Dict, Any, Tuple
from .schema import FormField, FormSchema
import uuid
import io

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None  # type: ignore

MAX_FIELDS = 300  # safety cap

# Explicit internal / non-user-visible field names observed in sample PDFs that should not
# be presented to the user for data entry. These commonly represent hidden workflow or
# submission controls (e.g. Adobe auto form IDs, spacer fields, submission triggers).
INTERNAL_FIELD_EXACT_LOWER = {
    # Use all lowercase for case-insensitive matching
    "formid", "pdf_submission_new", "simple_spc", "adobewarning",
    # Common non-user interactive button names sometimes exposed as widgets
    "submit", "print", "clear", "reset"
}

# Substring / pattern heuristics (case-insensitive) – kept intentionally conservative to
# avoid stripping legitimate fields. Extend cautiously.
INTERNAL_FIELD_CONTAINS = [
    "adobewarning",  # redundancy / safety
    "_spc",          # spacer artifacts
]

class AcroFormError(Exception):
    pass

class NoAcroFormFieldsError(AcroFormError):
    pass

class NotAcroFormError(AcroFormError):
    pass

def extract_acroform(pdf_bytes: bytes, original_filename: str) -> FormSchema:
    """Extract first-page AcroForm text-like fields using page annotations.

    Rationale: Some PDFs list fields in /AcroForm/Fields with nested /Kids; others rely on
    page /Annots entries. We focus on first page widgets (Subtype /Widget) to build a stable
    ordering by geometry and keep a safety cap.
    """
    if PdfReader is None:
        raise RuntimeError("pypdf is required for PDF extraction. Install pypdf.")

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:  # pragma: no cover
        raise NotAcroFormError(f"Failed to read PDF bytes: {e}")

    root = reader.trailer.get("/Root") if hasattr(reader, "trailer") else None
    if root is None:
        raise NotAcroFormError("PDF structure unreadable (no /Root)")
    acro = root.get("/AcroForm")
    if acro is None:
        raise NotAcroFormError("PDF has no /AcroForm")

    # Gather first-page widget annotations
    collected: List[FormField] = []
    name_counts: Dict[str, int] = {}
    try:
        first_page = reader.pages[0]
    except Exception as e:  # pragma: no cover
        raise NotAcroFormError(f"Cannot access first page: {e}")

    annots = first_page.get("/Annots") or []
    limit = 0
    filtered_internal: List[str] = []
    for ref in annots:
        if limit >= MAX_FIELDS:
            break
        try:
            annot = ref.get_object() if hasattr(ref, "get_object") else ref
            if annot.get("/Subtype") != "/Widget":
                continue
            field_name = annot.get("/T")
            if not field_name:
                continue
            # Filter internal / non-user-visible fields
            lower = field_name.lower()
            if lower in INTERNAL_FIELD_EXACT_LOWER or any(p in lower for p in INTERNAL_FIELD_CONTAINS):
                filtered_internal.append(field_name)
                continue
            original_name = field_name
            base = field_name
            if base in name_counts:
                name_counts[base] += 1
                field_name = f"{base}_{name_counts[base]}"
            else:
                name_counts[base] = 1
            # Field type may reside on parent if not directly present
            ft = annot.get("/FT") or (annot.get("/Parent").get("/FT") if annot.get("/Parent") else None)
            field_type = ft if isinstance(ft, str) else getattr(ft, "name", "Unknown")
            rect = None
            rect_array = annot.get("/Rect")
            if rect_array:
                try:
                    rect = tuple(float(x) for x in rect_array)
                except Exception:
                    rect = None
            collected.append(FormField(
                name=field_name,
                display_name=original_name,
                page=0,
                rect=rect,
                field_type=field_type or "Unknown",
                original_name=original_name
            ))
            limit += 1
        except Exception:
            continue

    # Fallback: if no annotations captured, try legacy /AcroForm /Fields list.
    if not collected:
        fields_raw = acro.get("/Fields") or []
        for f in fields_raw[:MAX_FIELDS]:
            try:
                field_name = f.get("/T")
                if not field_name:
                    continue
                lower = field_name.lower()
                if lower in INTERNAL_FIELD_EXACT_LOWER or any(p in lower for p in INTERNAL_FIELD_CONTAINS):
                    filtered_internal.append(field_name)
                    continue
                original_name = field_name
                base = field_name
                if base in name_counts:
                    name_counts[base] += 1
                    field_name = f"{base}_{name_counts[base]}"
                else:
                    name_counts[base] = 1
                ft = f.get("/FT")
                field_type = ft if isinstance(ft, str) else getattr(ft, "name", "Unknown")
                rect = None
                try:
                    widget = f.get("/Kids")[0] if f.get("/Kids") else f
                    rect_array = widget.get("/Rect")
                    if rect_array:
                        rect = tuple(float(x) for x in rect_array)
                except Exception:
                    rect = None
                collected.append(FormField(
                    name=field_name,
                    display_name=original_name,
                    page=0,
                    rect=rect,
                    field_type=field_type or "Unknown",
                    original_name=original_name
                ))
            except Exception:
                continue

    if not collected:
        raise NoAcroFormFieldsError("No first-page fields extracted")

    with_rect = [c for c in collected if c.rect]
    without_rect = [c for c in collected if not c.rect]
    with_rect_sorted = sorted(with_rect, key=lambda c: (-c.rect[1], c.rect[0]))  # type: ignore
    ordered = with_rect_sorted + without_rect

    schema = FormSchema(
        form_id=uuid.uuid4().hex,
        fields=ordered,
        metadata={
            "original_filename": original_filename,
            "truncated_to_first_page": True,
            "field_cap_reached": len(collected) >= MAX_FIELDS,
            "total_fields_raw": len(collected),
            "filtered_internal_count": len(filtered_internal),
            "filtered_internal_sample": filtered_internal[:10],
        }
    )
    return schema
