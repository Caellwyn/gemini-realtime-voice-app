"""PDF form processing package (v1).

Provides modular extraction and filling for AcroForm PDFs.
Future versions may extend to flat PDFs with OCR and layout overlays.
"""
from .schema import FormField, FormSchema
from .extract import extract_acroform, NoAcroFormFieldsError, NotAcroFormError, AcroFormError
from .fill import fill_acroform
from .storage import FormStorageManager

__all__ = [
    "FormField",
    "FormSchema",
    "extract_acroform",
    "AcroFormError",
    "NoAcroFormFieldsError",
    "NotAcroFormError",
    "fill_acroform",
    "FormStorageManager",
]
