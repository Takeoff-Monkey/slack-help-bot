# bid-scanner

Scans a PDF (a bid, spec, or plan set) for a list of **keywords**, **highlights** every
match in a copy of the PDF, and writes a **spreadsheet** tallying how many times each
keyword appears and on which pages.

This is a **bot-callable tool**: the Takeoff Monkey Slack bot ([app2.py](../../app2.py))
discovers it via [`tool.json`](tool.json) and runs it when a teammate attaches a PDF and
asks to scan/search/highlight it for terms. It is the same tool as the Streamlit "Bid
Scanner (Keyword Highlighter)" — the original UI script is kept alongside as
[`multi-scope-bid-scanner.py`](multi-scope-bid-scanner.py) purely for reference; all of
its logic (minus Streamlit/auth) lives in [`scanner.py`](scanner.py).

---

## What it does

For each page (except any in `skip_pages`) it:
1. Counts case-insensitive **substring** occurrences of each keyword in the page text.
2. Highlights every on-page match (`page.search_for` → `add_highlight_annot`).

Then it writes:
- `<name>-highlighted.pdf` — the original PDF with every match highlighted.
- `<name>-keywords.xlsx` — sheet `Keyword Results` with columns `Keyword`, `Count`, `Pages`.

Matching is substring-based on purpose: the default keyword `fenc` catches
fence / fencing / fenced with one entry.

## When the bot should use it

> Attach a PDF and ask to scan / search / highlight it for keywords — e.g. "highlight
> every mention of fence and gate", "scan this bid for these scopes", "find where
> 'bollard' shows up".

- If the user **names keywords**, the bot passes exactly those.
- If the user **doesn't name any**, the bot omits the `keywords` field and the tool
  falls back to its standard **bid-scope** set (see below).

It is **not** for extracting structured schedule/legend tables (that's
[`schedule-extractor`](../schedule-extractor/)) and **not** for answering free-text
questions about a document.

### Default keyword set

When no keywords are supplied, the tool uses the set the Streamlit app ships with:

```
chain, link, ornamental, fenc, gate, operator, wood, steel,
bollard, barrier, wedge, crash, turnstile, temporary, rail
```

Defined as `scanner.DEFAULT_KEYWORDS` — the single source of truth (also mirrored in
`tool.json`'s `keywords.default` so the model sees it).

---

## How the bot calls it (contract)

The bot resolves the user's attachment to a file handle (e.g. `file_1`) and invokes the
tool via the backend selected by `TOOL_BACKEND`:

- **local** → runs [`run.py`](run.py) as a subprocess in this directory's venv.
- **lambda** → invokes the `tm-tool-bid-scanner` Lambda (see [`lambda/`](lambda/)).

Both honor the same JSON contract:

**Input (stdin / event):**
```json
{
  "input":      { "input_file": "file_1", "keywords": ["fence", "gate"], "skip_pages": [] },
  "input_path": "/abs/path/to/staged.pdf",
  "work_dir":   "/abs/path/to/work-<uuid>",
  "backend":    "local"
}
```

**Output (`work_dir/result.json`):**
```json
{
  "status": "ok",
  "summary": "Scanned site for 15 keyword(s): 42 match(es) across 8 keyword(s). Highlighted the PDF and wrote a keyword tally.",
  "artifacts": [
    { "kind": "pdf",  "ref": "/abs/.../site-highlighted.pdf", "filename": "site-highlighted.pdf", "title": "Highlighted PDF" },
    { "kind": "xlsx", "ref": "/abs/.../site-keywords.xlsx",   "filename": "site-keywords.xlsx",   "title": "Keyword tally" }
  ],
  "error": null
}
```

If no keyword matches, `artifacts` is empty and the summary says so (no files produced).

### Configurable inputs (from `tool.json`)
| field | type | default | meaning |
|---|---|---|---|
| `input_file` | string (handle) | — (required) | which attached PDF to scan |
| `keywords` | string[] | the bid-scope set | terms to search for; omit to use the default set |
| `skip_pages` | integer[] | `[]` | zero-based page indices to skip (e.g. a cover sheet) |

The bot processes **one** PDF per call (`accepts.max_files: 1`), matching
schedule-extractor. If several PDFs are attached, the model calls the tool once per file.

---

## Run it standalone (terminal)

```bash
cd tools/bid-scanner
./setup.sh                       # one-time: builds .venv from requirements.txt
# Default keyword set:
echo '{"input":{"skip_pages":[]},"input_path":"/path/to/in.pdf","work_dir":"."}' \
  | .venv/bin/python run.py
# Custom keywords:
echo '{"input":{"keywords":["fence","gate","bollard"]},"input_path":"/path/to/in.pdf","work_dir":"."}' \
  | .venv/bin/python run.py
```

## Secrets & deployment

This tool needs **no secrets** — it's pure PyMuPDF + pandas, no cloud services for the
scan itself. Under the `lambda` backend the function only needs read/write on the
scratch S3 bucket (files round-trip through S3); it uses its execution role, no static
keys. See [`lambda/`](lambda/) and the deploy steps in [`../README.md`](../README.md).

Region convention: `us-east-1`. Owner: Konur Papageorgiou. General escalation: Tommy Lather.
