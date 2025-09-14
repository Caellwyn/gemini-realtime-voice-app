from __future__ import annotations
from typing import Dict
import io
from pypdf.generic import NameObject  # type: ignore
try:
    from pypdf import PdfReader, PdfWriter  # type: ignore
except ImportError:  # pragma: no cover
    PdfReader = None  # type: ignore
    PdfWriter = None  # type: ignore

class PDFFormFillError(Exception):
    pass

def fill_acroform(original_pdf_bytes: bytes, values: Dict[str, str]) -> bytes:
    if PdfReader is None or PdfWriter is None:
        raise PDFFormFillError("pypdf not installed; cannot fill forms. Install pypdf first.")
    try:
        reader = PdfReader(io.BytesIO(original_pdf_bytes))
    except Exception as e:  # pragma: no cover
        raise PDFFormFillError(f"Failed to parse original PDF bytes: {e}")

    root = reader.trailer.get("/Root") if reader.trailer else None
    if not root or "/AcroForm" not in root:
        raise PDFFormFillError("No AcroForm present when filling")

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    acro = root["/AcroForm"]
    writer._root_object[NameObject("/AcroForm")] = acro
    acro_form = writer._root_object[NameObject("/AcroForm")]
    try:  # Ensure appearance refresh in viewers
        acro_form.update({NameObject("/NeedAppearances"): True})
    except Exception:
        pass

    # pypdf helper handles setting /V and related appearances; apply to each page (safe)
    for i, page in enumerate(writer.pages):
        try:
            writer.update_page_form_field_values(page, values)
        except Exception:
            # continue filling other pages even if some fields mismatch
            continue

    bio = io.BytesIO()
    writer.write(bio)
    return bio.getvalue()
