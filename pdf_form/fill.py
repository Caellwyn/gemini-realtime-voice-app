from __future__ import annotations
from typing import Dict, Any, Optional
import io
from pypdf.generic import NameObject  # type: ignore
try:
    from pypdf import PdfReader, PdfWriter  # type: ignore
except ImportError:  # pragma: no cover
    PdfReader = None  # type: ignore
    PdfWriter = None  # type: ignore

class PDFFormFillError(Exception):
    pass

def fill_acroform(original_pdf_bytes: bytes, values: Dict[str, Any]) -> bytes:
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

    # We attempt a two-phase fill:
    # 1. Use pypdf bulk updater for text / basic fields
    # 2. Manually adjust checkbox / radio appearance states where needed
    bulk_values: Dict[str, Any] = {}
    for k, v in values.items():
        # Map booleans to standard on/off tokens recognized by many PDFs
        if isinstance(v, bool):
            bulk_values[k] = "Yes" if v else "Off"
        else:
            bulk_values[k] = v

    for page in writer.pages:
        try:
            writer.update_page_form_field_values(page, bulk_values)
        except Exception:
            continue

    # Manual widget pass for button fields (checkbox / radio) to ensure /AS updated.
    try:
        root = writer._root_object  # type: ignore[attr-defined]
        acro_form = root.get("/AcroForm") if root else None
        fields = acro_form.get("/Fields") if acro_form else []
        for f in fields:
            try:
                name = f.get("/T")
                if not name:
                    continue
                supplied: Optional[Any] = None
                # Accept both raw name and any suffix variants (pypdf may expand names)
                if name in values:
                    supplied = values[name]
                if supplied is None:
                    continue
                ft = f.get("/FT")
                if ft == "/Btn":
                    # Interpret boolean-like inputs for checkboxes
                    val = supplied
                    if isinstance(val, str):
                        lower = val.lower()
                        if lower in {"true", "yes", "on", "1"}:
                            val = True
                        elif lower in {"false", "no", "off", "0", ""}:
                            val = False
                    kids = f.get("/Kids") or []
                    if not kids:
                        # single widget button (likely checkbox)
                        widget = f
                        ap = widget.get("/AP")
                        if isinstance(val, bool) and ap and ap.get("/N"):
                            # choose first non Off appearance as on-state
                            on_state = None
                            try:
                                for k_ap in ap.get("/N").keys():
                                    if k_ap != "/Off":
                                        on_state = k_ap
                                        break
                            except Exception:
                                pass
                            if on_state:
                                if val:
                                    widget.update({NameObject("/V"): NameObject(on_state)})
                                    widget.update({NameObject("/AS"): NameObject(on_state)})
                                else:
                                    widget.update({NameObject("/V"): NameObject("/Off")})
                                    widget.update({NameObject("/AS"): NameObject("/Off")})
                    else:
                        # Radio group (multiple kids); set selected and clear others
                        if isinstance(supplied, str):
                            target_state = supplied
                        else:
                            target_state = None
                        if target_state:
                            for kid in kids:
                                try:
                                    ap = kid.get("/AP")
                                    if ap and ap.get("/N"):
                                        for state_name in ap.get("/N").keys():
                                            if state_name.lstrip("/") == target_state:
                                                kid.update({NameObject("/AS"): NameObject(state_name)})
                                                f.update({NameObject("/V"): NameObject(state_name)})
                                                break
                                except Exception:
                                    continue
            except Exception:
                continue
    except Exception:
        pass

    bio = io.BytesIO()
    writer.write(bio)
    return bio.getvalue()
