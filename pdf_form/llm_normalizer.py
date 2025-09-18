from __future__ import annotations
"""LLM-based field normalization for PDF forms.

Enriches raw widget metadata with nearby text, calls Gemini to propose
friendly display names, short spoken prompts, and logical groupings.

This module is optional and controlled via config flags.
"""
from typing import List, Dict, Any
import os
import json

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover
    fitz = None  # type: ignore

try:
    from google import genai
except Exception:  # pragma: no cover
    genai = None  # type: ignore

from config import (
    MAX_PDF_FIELDS,
)

# Default constants are imported from config lazily to avoid hard dependency
def _get_cfg():
    try:
        from config import (
            ENABLE_LLM_FIELD_NORMALIZATION,
            LLM_NORMALIZER_MODEL,
            LLM_NORMALIZER_MAX_FIELDS,
            LLM_NORMALIZER_NEAR_TEXT_RADIUS,
            LLM_NORMALIZER_TEMPERATURE,
        )
        return {
            "enable": ENABLE_LLM_FIELD_NORMALIZATION,
            "model": LLM_NORMALIZER_MODEL,
            "max_fields": int(LLM_NORMALIZER_MAX_FIELDS or MAX_PDF_FIELDS),
            "radius": int(LLM_NORMALIZER_NEAR_TEXT_RADIUS or 36),
            "temperature": float(LLM_NORMALIZER_TEMPERATURE or 0.2),
        }
    except Exception:
        # Safe defaults if config not populated yet
        return {
            "enable": False,
            "model": "gemini-2.5-flash",
            "max_fields": min(120, MAX_PDF_FIELDS),
            "radius": 36,
            "temperature": 0.2,
        }


def _extract_nearby_text(pdf_bytes: bytes, fields: List[Dict[str, Any]], radius: int) -> List[Dict[str, Any]]:
    """Enrich each field with nearby text context using PyMuPDF.

    We sample text above and around the widget's rectangle to capture labels.
    Returns list mirroring input fields with an added "nearby_text" key.
    """
    if not fitz:
        return [{**f, "nearby_text": ""} for f in fields]

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return [{**f, "nearby_text": ""} for f in fields]

    enriched: List[Dict[str, Any]] = []
    for f in fields:
        page_idx = int(f.get("page", 0))
        rect = f.get("rect") or [0, 0, 0, 0]
        x0, y0, x1, y1 = rect
        try:
            page = doc[page_idx]
        except Exception:
            enriched.append({**f, "nearby_text": ""})
            continue

        # Sampling zone: mostly above and slightly around the field
        top = max(0, min(y0, y1) - radius * 2)
        bottom = min(max(y0, y1) + radius, page.rect.height)  # guard bounds
        left = max(0, min(x0, x1) - radius)
        right = min(max(x0, x1) + radius, page.rect.width)
        search_rect = fitz.Rect(left, top, right, bottom)

        # Collect intersecting text blocks
        nearby_lines: List[str] = []
        try:
            blocks = page.get_text("blocks") or []
        except Exception:
            blocks = []
        for b in blocks:
            try:
                if len(b) < 5:
                    continue
                bx0, by0, bx1, by1, text = b[:5]
                brect = fitz.Rect(bx0, by0, bx1, by1)
                if brect.intersects(search_rect) and isinstance(text, str):
                    snippet = " ".join(line.strip() for line in text.splitlines() if line.strip())
                    if snippet:
                        nearby_lines.append(snippet)
            except Exception:
                continue
        nearby_text = " ".join(nearby_lines)[:2000]
        enriched.append({**f, "nearby_text": nearby_text})
    return enriched


def _build_llm_payload(fields_with_context: List[Dict[str, Any]]) -> str:
    safe = []
    for idx, f in enumerate(fields_with_context):
        safe.append({
            "index": idx,
            "base_name": f.get("pdf_field_name") or "",
            "base_type": f.get("type") or f.get("base_type") or "",
            "page": int(f.get("page", 0)),
            "rect": f.get("rect") or [0, 0, 0, 0],
            "options": f.get("options") or None,
            "tooltip": (f.get("tooltip") or "")[:500],
            "nearby_text": (f.get("nearby_text") or "")[:2000],
            "export_value": f.get("export_value"),
        })
    return json.dumps({"fields": safe}, ensure_ascii=False)


def _normalize_with_llm(model: str, payload_json: str, temperature: float) -> List[Dict[str, Any]]:
    if not genai:
        return []

    # Ensure API key
    os.environ.setdefault("GOOGLE_API_KEY", os.getenv("GEMINI_API_KEY", ""))

    client = genai.Client()
    instructions = (
        "You are normalizing PDF form fields for a voice assistant.\n"
        "Input is JSON with 'fields': an array. Each field has index, base_name, base_type, page, rect, options, tooltip, nearby_text.\n\n"
        "Goals:\n"
        "1) Provide a clear display_name for each field (spoken-friendly).\n"
        "2) Provide a concise spoken_prompt (<=140 chars).\n"
        "3) If multiple fields form a logical question group, assign identical group_id and group_label, and include a shared options array. This includes: (a) radio button sets (single select), and (b) clusters of checkboxes that belong to one prompt such as 'check all that apply' (multi-select).\n"
        "4) Keep the same length/order; preserve index. Do not invent or drop fields.\n"
        "Return pure JSON: { \"normalized\": [ {\"index\":int, \"display_name\":str, \"spoken_prompt\":str, \"group_id\":str|null, \"group_label\":str|null, \"options\":array|null} ] }"
    )

    try:
        resp = client.models.generate_content(
            model=model,
            contents=[instructions, payload_json],
            config={
                "temperature": temperature,
                "response_mime_type": "application/json",
            },
        )
        text = (getattr(resp, "text", None) or "").strip()
        data = json.loads(text) if text else {}
        items = data.get("normalized") or []
        out: List[Dict[str, Any]] = []
        for it in items:
            out.append({
                "index": int(it.get("index", -1)),
                "display_name": (it.get("display_name") or "").strip()[:80],
                "spoken_prompt": (it.get("spoken_prompt") or "").strip()[:140],
                "group_id": it.get("group_id") or None,
                "group_label": it.get("group_label") or None,
                "options": it.get("options") or None,
            })
        return out
    except Exception:
        return []


def normalize_fields(pdf_bytes: bytes, raw_fields: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return normalization hints: by_index map and group summaries.

    Output:
      {
        "by_index": { idx: {display_name, spoken_prompt, group_id?, group_label?, options?} },
        "groups": [ { group_id, group_label, members:[idx,...], options:[...] } ]
      }
    """
    cfg = _get_cfg()
    if not cfg["enable"] or not raw_fields:
        return {"by_index": {}, "groups": []}

    trimmed = raw_fields[: int(cfg["max_fields"])]
    with_ctx = _extract_nearby_text(pdf_bytes, trimmed, cfg["radius"])
    payload = _build_llm_payload(with_ctx)
    norm = _normalize_with_llm(cfg["model"], payload, cfg["temperature"])

    by_index: Dict[int, Dict[str, Any]] = {
        n["index"]: n
        for n in norm
        if isinstance(n.get("index"), int) and 0 <= n["index"] < len(trimmed)
    }

    groups_tmp: Dict[str, Dict[str, Any]] = {}
    for idx, item in by_index.items():
        gid = item.get("group_id")
        if not gid:
            continue
        g = groups_tmp.setdefault(
            str(gid),
            {"group_id": str(gid), "group_label": item.get("group_label"), "members": [], "options": item.get("options"), "kind": None},
        )
        g["members"].append(idx)
        if not g.get("group_label") and item.get("group_label"):
            g["group_label"] = item["group_label"]
        if not g.get("options") and item.get("options"):
            g["options"] = item["options"]

    # Infer group kind (checkbox vs radio) using raw_fields kinds if available
    groups = list(groups_tmp.values())
    try:
        for g in groups:
            kinds = set()
            for m in g.get("members", []):
                try:
                    # Look up original base_type/type info
                    rf = raw_fields[m]
                    t = rf.get("type") or rf.get("base_type") or ""
                    kinds.add(t)
                except Exception:
                    continue
            # Heuristics: if any 'checkbox' present and no 'radio' => checkbox (multi-select)
            # if any 'radio' present => radio (single-select)
            if "radio" in kinds:
                g["kind"] = "radio"
            elif "checkbox" in kinds:
                g["kind"] = "checkbox"
            else:
                g["kind"] = None
    except Exception:
        pass
    return {"by_index": by_index, "groups": groups}
