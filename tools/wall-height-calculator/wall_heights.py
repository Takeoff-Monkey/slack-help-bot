"""Core wall-height extraction logic for the wall-height-calculator tool.

This is the callable core shared by the local run.py entrypoint and the AWS Lambda
handler.py — the same split as schedule-extractor's main2.py and bid-scanner's scanner.py.
It is pure PyMuPDF + pandas: none of the Streamlit / auth / utils machinery from the
original `wall-height-calculator.py` (that file is kept alongside purely as the UI
reference).

It reads the TW/BW wall-elevation callout **annotations** off a marked-up plan PDF, pairs
each wall ID with its length and its top/bottom-of-wall elevations, computes every wall's
calculated height, rounded height, and area, and writes the result to an .xlsx workbook.

The wall-detection algorithm (get_wall_data) is a verbatim port of the proven Streamlit
tool — the intricate index/regex logic is left untouched so results are identical. The only
changes are: the Streamlit progress-bar (`load_bar`) calls are dropped, `project_type` is a
parameter instead of a module global, and `list_annots` honours `skip_pages`.
"""

from __future__ import annotations

import io
import math
import re

import fitz  # PyMuPDF
import pandas as pd

# The height rounding depends on the project type (matches the Streamlit radio, which
# defaults to "Single-Family"). Single-Family rounds up to the nearest 0.1 ft;
# Multi-Family/Commercial rounds up to the nearest 0.5 ft.
PROJECT_TYPES = ("Single-Family", "Multi-Family/Commercial")
DEFAULT_PROJECT_TYPE = "Single-Family"


def normalize_project_type(project_type) -> str:
    """Map a (possibly loose) project-type string to one of PROJECT_TYPES, defaulting to
    Single-Family when unset or unrecognized. ANY value indicating multi-family or commercial
    — 'multi', 'multifamily', 'multi-family', 'multifamily/commercial', 'commercial', 'comm',
    'mf', 'mfc', etc., in any casing/combination — selects Multi-Family/Commercial. Everything
    else, including 'single', 'single-family', 'sf', or anything unrecognized (and unset),
    falls back to Single-Family."""
    if not project_type:
        return DEFAULT_PROJECT_TYPE
    pt = str(project_type).strip().lower()
    if "multi" in pt or "comm" in pt or pt in ("mf", "mfc"):
        return "Multi-Family/Commercial"
    return "Single-Family"


# Round up to nearest 0.1 (0.05 rounds up)
def sf_round(num):
    return round((num * 10) + 0.1) / 10


# Round up to nearest 0.5
def mf_round(num):
    return math.ceil(num * 2) / 2


def _verify_key(key, obj1):
    if key not in obj1:
        obj1[key] = {"tops": [], "bottoms": []}


# Transpose nested dictionary
def _trans_nested_dict(nested_dict):
    trans_dict = {}
    for outer_key, inner_dict in nested_dict.items():
        for inner_key, value in inner_dict.items():
            if inner_key not in trans_dict:
                trans_dict[inner_key] = {}
            trans_dict[inner_key][outer_key] = value
    return trans_dict


# Sort dataframe alphabetically & by length (1A, 1Z, 1AA, etc.)
def _sort_alpha_len(val):
    val = str(val)
    try:
        return (
            re.match(r"^[A-Z]*", val)[0],
            int("".join(re.findall(r"\d+", val))),
            len("".join(re.findall(r"[A-Z]+$", val))),
            "".join(re.findall(r"[A-Z]+$", val)),
        ) if re.match(r"^[A-Z]*\d+[A-Z]+", val) else ("", "", "", "")
    except Exception:
        return ("", "", "", "")


def list_annots(pdf, skip_pages=None) -> list:
    """Flatten every annotation's text `content` across the PDF (in page/creation order)
    into a single list — the input the wall-detection algorithm walks. `skip_pages` is a
    list of 0-based page indices to exclude (e.g. a cover sheet)."""
    skip = set(skip_pages or [])
    annots = []
    for page_index, page in enumerate(pdf):
        if page_index in skip:
            continue
        for annot in page.annots():
            content = (annot.info.get("content") or "").replace("\r", "").replace("\n", "")
            annots.append(content)
    return annots


def get_wall_data(annots: list, project_type: str = DEFAULT_PROJECT_TYPE) -> dict:
    """Verbatim port of the Streamlit tool's wall detection. Walks the flat annotation list,
    using wall-ID markups as anchors, and reads the neighbouring TW/BW elevation callouts and
    wall length to compute each wall's height + area. `project_type` selects the rounding."""
    walls = {}

    for i in range(len(annots)):
        info = annots[i]

        # Use wall ids as anchor indices
        if re.match(r"^[A-Z]?\d+[A-Z]+", info):
            # Find TW/BW & calc height data
            if (i > 1 and re.match(r"^TW[ ]*\d+", annots[i - 2])) or (i < len(annots) - 1 and re.match(r"^BW[ ]*\d+", annots[i + 1])):
                id = info.split(",")
                if len(id) > 1:
                    if not re.search(r"\d", id[0]) and re.search(r"\d", id[1]):
                        id[0] = re.findall(r"\d", id[1])[0] + id[0]
                    elif not re.search(r"\d", id[1]) and re.search(r"\d", id[0]):
                        id[1] = re.findall(r"\d", id[0])[0] + id[1]
                    elif not re.search(r"\d", id[0]) and not re.search(r"\d", id[1]):
                        continue

                for key in id:
                    _verify_key(key, walls)

                    # Save TW/BWs for wall
                    try:
                        walls[key]["bottoms"].append(float(re.sub(r"^BW[ ]*", "", annots[i + 1].strip())) if i < len(annots) - 1 and re.match(r"^BW[ ]*", annots[i + 1].strip()) else float(re.sub(r"^BW[ ]*", "", annots[i - 3].strip())))
                        walls[key]["tops"].append(float(re.sub(r"^TW[ ]*", "", annots[i + 2].strip())) if i < len(annots) - 2 and re.match(r"^TW[ ]*", annots[i + 2].strip()) else float(re.sub(r"^TW[ ]*", "", annots[i - 2].strip())))
                    except IndexError:
                        continue

                    # Set wall top/bottom
                    if len(walls[key]["tops"]) > 1 and len(walls[key]["bottoms"]) > 1:
                        walls[key]["fit"] = round(abs(walls[key]["tops"][0] - walls[key]["bottoms"][0]), 2)
                        walls[key]["fib"] = round(abs(walls[key]["tops"][1] - walls[key]["bottoms"][1]), 2)
                        walls[key]["ht_calc"] = round((walls[key]["fit"] + walls[key]["fib"]) / 2, 2)
                    else:
                        walls[key]["ht_calc"] = None

            # Find wall ID & length
            else:
                _verify_key(info, walls)
                walls[info]["id"] = info
                walls[info]["length"] = float(re.match(r"^\d+(\.\d+)?", annots[i - 1])[0]) if i > 0 and re.match(r"^\d+(\.\d+)?", annots[i - 1]) is not None else None

    # Calculate rounded height & area for each wall
    for wall in walls:
        walls[wall]["ht_round"] = (sf_round(walls[wall].get("ht_calc", 0)) if project_type == "Single-Family" else mf_round(walls[wall].get("ht_calc", 0))) if walls[wall].get("ht_calc") is not None else None
        walls[wall]["area"] = walls[wall]["ht_round"] * walls[wall].get("length", 0) if walls[wall]["ht_round"] is not None and walls[wall].get("length") is not None else None

    return walls


def process_pdf(pdf_bytes: bytes, project_type: str = DEFAULT_PROJECT_TYPE, skip_pages=None) -> dict:
    """Open an in-memory PDF, collect its annotations, and run wall detection. Returns the
    walls dict get_wall_data produces ({} when no walls are found). Shared by run.py and the
    Lambda handler; mirrors schedule-extractor's process_pdf entrypoint."""
    project_type = normalize_project_type(project_type)
    pdf = fitz.open(stream=io.BytesIO(pdf_bytes), filetype="pdf")
    try:
        return get_wall_data(list_annots(pdf, skip_pages=skip_pages), project_type=project_type)
    finally:
        pdf.close()


# Column order + human-readable headers for the output workbook (matches the Streamlit tool).
_COLUMNS = ["id", "fit", "fib", "ht_calc", "ht_round", "length", "area"]
_HEADERS = {
    "id": "Wall Type",
    "fit": "First Input Top",
    "fib": "First Input Bottom",
    "ht_calc": "Wall Height in Feet (Calculated)",
    "ht_round": "Wall Height (Roundup)",
    "length": "Wall Length in Feet",
    "area": "Wall Area (SF)",
}


def save_excel(walls: dict, filename: str) -> str:
    """Write the walls dict to an .xlsx workbook (sheet 'Wall Data'), one row per wall, with
    columns ordered + renamed and rows sorted by wall ID — mirroring the Streamlit tool.
    Returns `filename`."""
    trans_dict = _trans_nested_dict(walls)
    df = pd.DataFrame(trans_dict).reindex(columns=_COLUMNS)
    if "id" in df.columns:
        df = df.sort_values(by="id", key=lambda x: x.apply(_sort_alpha_len))
    df = df.rename(columns=_HEADERS)
    with pd.ExcelWriter(filename, engine="xlsxwriter") as writer:
        df.to_excel(writer, sheet_name="Wall Data", index=False)
        # Auto-adjust column widths (xlsxwriter-specific; best-effort).
        try:
            worksheet = writer.sheets["Wall Data"]
            for j, col in enumerate(df.columns):
                width = max(df[col].astype(str).map(len).max() if not df.empty else 0, len(str(col)))
                worksheet.set_column(j, j, width + 2)
        except Exception:
            pass
    return filename
