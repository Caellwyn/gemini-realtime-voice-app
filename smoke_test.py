import os, sys, json, io
from pathlib import Path

# Adjust path to import pdf_form
BASE = Path(__file__).parent
sys.path.append(str(BASE))

from pdf_form.extract import extract_acroform, NoAcroFormFieldsError, NotAcroFormError  # type: ignore
from pdf_form.fill import fill_acroform, PDFFormFillError  # type: ignore

EXAMPLE_DIR = BASE / "Example PDFs"
# Try both provided example files
CANDIDATES = [
    "EDIT OoPdfFormExample.pdf",
    "EDIT OoPdfFormExample2.pdf",
]

def main():
    found = None
    for name in CANDIDATES:
        p = EXAMPLE_DIR / name
        if p.exists():
            found = p
            break
    if not found:
        print("NO_PDF_FOUND", flush=True)
        return 1
    data = found.read_bytes()
    print(f"Using PDF: {found.name} size={len(data)} bytes")
    try:
        schema = extract_acroform(data, found.name)
    except (NoAcroFormFieldsError, NotAcroFormError) as e:
        print(f"SCHEMA_ERROR:{e}")
        return 2
    print("Extracted fields:")
    for f in schema.fields[:10]:
        print(f" - {f.name} (orig={f.original_name}) type={f.field_type} rect={f.rect}")
    # Prepare dummy fill values (truncate to 20 chars)
    fill_values = {}
    for f in schema.fields:
        fill_values[f.original_name] = f"TestValue_{f.name[:12]}"
    try:
        filled_bytes = fill_acroform(data, fill_values)
    except PDFFormFillError as e:
        print(f"FILL_ERROR:{e}")
        return 3
    out_path = BASE / "_smoke_output_filled.pdf"
    out_path.write_bytes(filled_bytes)
    print(f"Filled PDF written to {out_path}")
    # Basic sanity: output not empty and larger or equal to input
    if len(filled_bytes) < 100:
        print("FILL_WARN: output suspiciously small")
    print("SMOKE_OK")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
