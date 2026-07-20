"""Core keyword-scan + highlight logic for the bid-scanner tool.

This is the callable core shared by the local run.py entrypoint and the AWS Lambda
handler.py — the same split as schedule-extractor's main2.py. It is pure PyMuPDF +
pandas: none of the Streamlit / auth / utils machinery from the original
`multi-scope-bid-scanner.py` (that file is kept alongside purely as the UI reference).

Given a PDF's bytes and a list of keywords it:
  1. counts case-insensitive keyword occurrences per page,
  2. highlights every on-page match in a copy of the PDF, and
  3. (save_excel) writes the per-keyword tallies to an .xlsx workbook.

Keyword matching mirrors the proven Streamlit tool exactly: substring, case-insensitive.
Stems like "fenc" are intentional so a single keyword catches fence/fencing/fenced.
"""

from __future__ import annotations

import io

import fitz  # PyMuPDF
import pandas as pd

# The standard keyword set the Streamlit app ships with (a fence/barrier bid scope).
# Used whenever the caller supplies no keywords of their own — the bot passes these
# through unchanged when the user doesn't name any.
DEFAULT_KEYWORDS = [
    "chain", "link", "ornamental", "fenc", "gate", "operator", "wood", "steel",
    "bollard", "barrier", "wedge", "crash", "turnstile", "temporary", "rail",
]


def normalize_keywords(keywords) -> list[str]:
    """Accept a list of strings OR a single comma-separated string; fall back to
    DEFAULT_KEYWORDS when nothing usable is given. Trims blanks and dedupes
    (case-insensitively, first spelling wins) so a match isn't double-counted."""
    if isinstance(keywords, str):
        keywords = keywords.split(",")
    cleaned: list[str] = []
    seen: set[str] = set()
    for kw in keywords or []:
        kw = str(kw).strip()
        if kw and kw.lower() not in seen:
            seen.add(kw.lower())
            cleaned.append(kw)
    return cleaned or list(DEFAULT_KEYWORDS)


def scan_pdf(pdf_bytes: bytes, keywords=None, skip_pages=None):
    """Scan a PDF for keywords, highlighting every match.

    Returns (highlighted_pdf_bytes, rows) where rows is a list of
    [keyword, count, pages_str] for every keyword that appeared at least once,
    in the order the keywords were given. `pages_str` is a human-readable
    "1, 4, 9" of the 1-based pages the keyword was found on. `skip_pages` is a
    list of 0-based page indices to exclude from both counting and highlighting.
    """
    keywords = normalize_keywords(keywords)
    skip = set(skip_pages or [])
    tally = {kw: {"count": 0, "pages": []} for kw in keywords}

    pdf = fitz.open(stream=io.BytesIO(pdf_bytes), filetype="pdf")
    try:
        for page_index, page in enumerate(pdf):
            if page_index in skip:
                continue
            page_number = page_index + 1
            lower_text = page.get_text().lower()
            for kw in keywords:
                kw_lower = kw.lower()
                if kw_lower in lower_text:
                    tally[kw]["count"] += lower_text.count(kw_lower)
                    tally[kw]["pages"].append(page_number)
                # Highlight every visual hit on the page (may differ slightly from the
                # text-layer count above — same behaviour as the original tool).
                for rect in page.search_for(kw):
                    annot = page.add_highlight_annot(rect)
                    annot.update()
        rows = [
            [kw, d["count"], ", ".join(str(p) for p in d["pages"])]
            for kw, d in tally.items()
            if d["count"] > 0
        ]
        # Only serialize when there's a match to return. This guards against a zero-page /
        # empty PDF (PyMuPDF's tobytes raises "cannot save with zero pages") — which yields
        # no rows anyway — and skips pointlessly re-serializing a large no-match PDF that the
        # caller would discard. A non-empty `rows` implies at least one page, so the bytes are
        # always valid when returned. Mirrors schedule-extractor's graceful "nothing found".
        highlighted = pdf.tobytes(garbage=3, deflate=True) if rows else b""
    finally:
        pdf.close()

    return highlighted, rows


def save_excel(rows: list, filename: str) -> str:
    """Write the keyword tally to an .xlsx workbook (sheet 'Keyword Results') with
    auto-fitted columns, mirroring the Streamlit tool's output. Returns `filename`."""
    df = pd.DataFrame(rows, columns=["Keyword", "Count", "Pages"])
    with pd.ExcelWriter(filename, engine="xlsxwriter") as writer:
        df.to_excel(writer, sheet_name="Keyword Results", index=False)
        # Auto-adjust column widths (xlsxwriter-specific; best-effort).
        try:
            worksheet = writer.sheets["Keyword Results"]
            for j, col in enumerate(df.columns):
                width = max(df[col].astype(str).map(len).max() if not df.empty else 0, len(str(col)))
                worksheet.set_column(j, j, width + 2)
        except Exception:
            pass
    return filename
