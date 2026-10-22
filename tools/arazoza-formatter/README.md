# arazoza-formatter

Reshapes an **Arazoza Brothers takeoff worksheet** (`.xlsx` / `.xlsm`) into the standard
Takeoff Monkey worksheet layout, fixes misspelled Latin plant names, and paints red anything a
person still has to look at. The output keeps the original data on its own tab, and the
template's macros keep working.

This is a **bot-callable tool**: the Takeoff Monkey Slack bot ([app2.py](../../app2.py))
discovers it via [`tool.json`](tool.json) and runs it when a teammate says a job is an
Arazoza project, or attaches a spreadsheet whose filename contains *Arazoza*. It can also be
run standalone from the terminal.

---

## Why it exists

Arazoza's worksheets arrive *almost* in our layout, shifted one column right: the item name
sits in **Size**, the sizing sits in **Notes**, **Package** is empty, and the group headers in
**Description** carry Arazoza's own names ("Trees", "Palms", "Bed Prep"), which stay as they
are. Putting that right by hand is a dozen careful copy/paste passes per sheet, and the passes
have to happen in one particular order or the data comes out mangled.

## What it does — in this order, always

The order **is** the specification (TODO #6). Each step reads what the previous one wrote, so
`formatter.format_worksheet` is the only place the steps are sequenced and nothing calls them
individually.

| # | Step | Detail |
|---|------|--------|
| 1 | **Read group headers** | Every **black-text** cell in `Description` is a group header. Its text is **left exactly as written** (TODO #2: the old relabelling to `1 - Trees & Palms` etc. was too generous). The tool still works out which takeoff grouping in [`takeoff-groupings.txt`](takeoff-groupings.txt) each header names, but only so step 5 knows which items sit under a Soil or Mulch group. Cells in any other colour (the red `Landscape` / `IRRIGATION` section markers) are skipped. See *How headers are read* below. |
| 2 | **Size → Description** | Every row with a value in `Size` has it moved into `Description`. Existing `Description` values stay (they are always on other rows); a row that has *both* is flagged red and left exactly as it came. |
| 2b | **Latin names** | The item names just moved get a spell check on their **botanical (Latin) words only**, by Claude Haiku 5.5 ([`latin.py`](latin.py)). Every proposed fix is then checked locally and dropped unless it keeps the word count, changes at most two words, each changed word is alphabetic, 4+ letters and a near spelling of the original, and nothing inside quotes (cultivars) changes. The estimator's capitalisation is kept. Group headers, common names, sizes and abbreviations are never re-spelled. Every fix is listed in the summary (`row 19: 'Quercus virginana' → 'Quercus virginiana'`). With no `ANTHROPIC_API_KEY`, or if the call fails, nothing changes and the summary says the check was skipped. |
| 3 | **Notes → Size** | `Notes` holds the sizing; it moves into the now-empty `Size` on item rows. A note sitting on a header row stays in `Notes`. |
| 4 | **Merge** | `Description` = `Description` + `" - "` + `Size` wherever both exist. Skipped only when the description already *ends with* `" - <size>"` (so `Cont` inside *Contorta* still gets its size appended). |
| 5 | **Soil & mulch depth** | For soil/mulch items whose text says *depth* (`Mulch 3" Depth`, `12" Depth of Planting Soil`, `Mulch 3"-4" depth`, `Mulch 2 1/2" depth`), `Size` becomes the depth measurement (`3"`, `3"-4"`, `2 1/2"`) and `Depth` is added to `Package`. A generic size with no word *depth* is left alone. An item counts as soil/mulch if its text says soil/topsoil/mulch **or** the nearest header above it is a matched Soil / Mulch grouping — a flagged header or a coloured section marker ends that context, so an irrigation sleeve's "18" depth cover" under `IRRIGATION` is never treated as soil. |
| 6 | **UOM** | Case-insensitive, punctuation-tolerant: `count`/`ea`/`each` → `Unit`, `sf` → `Square Feet`, `lf` → `Linear Feet`. Applied to the primary `UOM` column only — `UOM2` keeps `SF`/`CY`, matching every finished Arazoza sheet we have. |
| 7 | **Packages** | Package tokens found in `Size` are copied into `Package` in canonical form — `FG`/`field grown` → `FG`, `cont`/`container` → `Container`, `b&b`/`B.&B.`/`balled and burlapped` → `B&B` — and `/`-joined when there are several (`FG/B&B`). A `Size` with a gallon plant size (`3 Gal`, `15 gallon`, `1Gal.`, `45 G`) and none of those tokens gets `Container`. Merged with anything already in `Package`, never duplicated. |

Rows are bounded by the sheet's Excel table when the header row is a table header; anything
with data pasted *below* the table is formatted as well and called out in the summary (the
table's import formulas won't cover it until someone extends the table in Excel).

A row with text in `Description` is treated as a group header **unless it also carries a
quantity** — no header on these sheets ever does, and an item somebody had already typed into
the right column would otherwise be overwritten with a grouping label. Such rows are handled
as items (steps 4 to 7 apply to them).

### How headers are read

Headers are never rewritten. This matching only tells step 5 which group an item sits under.
A header's words are compared with each grouping's words. A grouping is a *candidate* only if
at least one header word **is** one of its words — exactly (`Trees`/`tree`) or by stem
(`Mulching`/`mulch`, `Protection`/`protect`). Mere resemblances (`Streetscape`~tree,
`Project`~protect, `Palmetto`~palm) never qualify on their own, so they can't quietly put a header
in the wrong group. A small alias list in `formatter.ALIASES` translates
estimator vocabulary to grouping words (`Ground Cover` → groundcover, `Hydroseed` → seed,
`Hedges` → shrub, `Pavers` → hardscape, `Rip Rap` → rock, `Pine Straw` → mulch,
`Topsoil`/`Planting Mix` → soil, `Sodding` → sod, `Transplant` → relocate) plus Arazoza's own
OST group names learnt from their finished Vela Cove worksheet (`Bed Areas` → mulch,
`Edging` → bed preparation) — extend it there. A bare `GC` is deliberately not expanded:
Arazoza files "Additional Shrubs & Gc" under Shrubs.

Among candidates, one that explains every matched word another explains wins
(`Remove & Replace` → `126 - Remove/Replace`, not `27 - Replacements`). Two candidates that
each explain words the other doesn't are the spec's "more than one match" —
`Shrubs & Groundcover`, `Trees, Palms & Shrubs`, `Tree Protection`, `Sod/Turf`. For such a
header (or one that matched nothing) the **items listed under it** get a say: each item is
matched like a header and votes for the grouping it names. An ambiguous header goes to the
one candidate its items back (`07 - Seed & Sod` over three sod rows → Sod); an unknown header
gets a grouping only when at least two items vote, they all agree, and they are at least half
of its items. A header with no grouping simply isn't a Soil/Mulch context for step 5.

### The output file

A **new** copy of the workbook, `<original name> - formatted.<ext>`. The upload is never
written to.

- The formatted data is on the worksheet's **own sheet** (`Project Totals`). That sheet holds
  the Excel table, the named ranges and the cells the template's macros read, so the
  **Arazoza Export** button and the rest of the ribbon work on the formatted data.
- A new tab, **`Project Totals (original)`**, holds the data exactly as it arrived: values and
  styling, with formulas frozen to the values they showed. It has no table, drawings or links.
- Everything else comes through **byte for byte**: VBA project, custom ribbon, shapes and
  buttons, other sheets, defined names, printer settings, customXml.

How: openpyxl only *reads* the workbook. The steps record each cell they change, and
[`xlsx_package.py`](xlsx_package.py) applies those edits directly to a copy of the original
zip. It touches only the formatted sheet's XML, appends a red fill and a few cell formats to
`styles.xml`, adds the original-data sheet, drops `calcChain.xml` and sets `fullCalcOnLoad` so
Excel rebuilds the chain and recalculates the table's formula columns on open. Saving through
openpyxl, as the tool used to, dropped the ribbon's relationship (no ribbon, so no way to run
the macros), the shapes and the customXml. That was the "macros stop working" bug in TODO #2.

### Red cells

Red **fill** (not red text, which already means "section marker" on these sheets) marks a row
where `Description` and `Size` were **both** filled, so nothing could be moved. The bot's
summary lists every red cell with its row.

### Guard rail: it refuses a sheet that isn't raw

If nothing sits in `Size` with an empty `Description` on its row, the sheet is either already
formatted or laid out differently — and running step 1 on it would paint every item name red.
The tool returns an error explaining that instead. `force: true` overrides it, and the bot is
told to set that **only** when the user explicitly asks.

### Known limits

- The groupings list is the landscape set from TODO #6, used only to spot Soil/Mulch groups.
  Add lines to [`takeoff-groupings.txt`](takeoff-groupings.txt) (`<code> - <name>`) and they
  are picked up on the next run, no code change.
- The Latin check only catches near-misses of real names (it can't fix a wrong species, and
  rejects any fix that changes 3+ words or a cultivar in quotes). It sends the item names,
  nothing else from the file, to the Claude API.
- A gallon size that also names another package (`65 gal FG`, `3 Gal B&B`) gets only that
  package, not `Container` as well — the gallon rule only applies when Size has no package token.
  Hand-made sheets also put `-` in Package where there is none; the tool leaves it empty.
- In the Vela Cove worksheet the estimator changed OST's `LF` to `Unit` on every shrub row
  (the counts were plants, not feet). The spec says `lf` → `Linear Feet`, so the tool does that;
  a "plant groups count in Units" rule would need the user's say-so.
- *depth* is matched literally (not *deep*), per the spec.
- `FG` in a Size cell is always read as *field grown*; a soil note like `6" below FG` (finish
  grade) would also put `FG` in Package.
- Excel sometimes turns a typed size like `3-4` into a date; the tool can't undo that, but it
  writes the date as `2023-03-04` and warns about the row.
- The **Column1 / schedule QTY** feature in TODO #6 is explicitly deferred and not implemented.

## When the bot should use it

> The user says it's an Arazoza project/job/worksheet, or the attached spreadsheet's filename
> contains "Arazoza", and they want it formatted / cleaned up / prepared.

A worksheet **must** be attached. `tool.json` declares `triggers`: the keyword `arazoza`, the
action words that turn a mention into a request (*format, clean up, prep, fix, process,
convert, run, worksheet, spreadsheet*), and the filename pattern `arazoza`. Keyword + action
word forces the action path; a bare mention ("what does the Arazoza db worker do?") is left to
the selector so it is still answered as a question. On the action path the bot adds a *routing
note* to the model's context naming this tool, and — when no `.xlsx`/`.xlsm` is attached to the
message or earlier in the thread (nothing, or only schedule images) — the note tells the model
to stop and ask for the worksheet with `ask_user` instead of starting work or calling the tool
on the wrong file. The tool itself also requires `input_file` of an accepted type.

It is **not** for non-Arazoza worksheets and **not** for extracting schedules from PDFs
(that's [`schedule-extractor`](../schedule-extractor/)).

---

## How the bot calls it (contract)

- **local** → runs [`run.py`](run.py) as a subprocess in this directory's venv.
- **lambda** → invokes the `tm-tool-arazoza-formatter` Lambda (see [`lambda/`](lambda/)).

**Input (stdin / event):**
```json
{
  "input":      { "input_file": "file_1", "sheet_name": null, "force": false },
  "input_path": "/abs/path/to/file_1-Arazoza - Job - Worksheet 2026-09-04.xlsm",
  "work_dir":   "/abs/path/to/work-<uuid>",
  "backend":    "local"
}
```

**Output (`work_dir/result.json`):**
```json
{
  "status": "ok",
  "summary": "Formatted sheet 'Project Totals' of Arazoza - Job - Worksheet 2026-09-04.xlsm → ... - formatted.xlsm.\n2 cells filled RED for a human to check:\n  - row 25: 'Shrubs & Groundcover' (ambiguous: 2 - Shrubs / 9 - Groundcover)\n  ...",
  "artifacts": [
    { "kind": "xlsm", "ref": "/abs/.../Arazoza - Job - Worksheet 2026-09-04 - formatted.xlsm",
      "filename": "Arazoza - Job - Worksheet 2026-09-04 - formatted.xlsm", "title": "Formatted Arazoza worksheet" }
  ],
  "error": null
}
```

### Configurable inputs (from `tool.json`)
| field | type | default | meaning |
|---|---|---|---|
| `input_file` | string (handle) | — (required) | the attached `.xlsx`/`.xlsm` worksheet |
| `sheet_name` | string | auto | which sheet; by default the visible sheet with a `Description`+`Size` header row and the most data |
| `force` | boolean | `false` | run even though the sheet doesn't look raw (user's explicit call only) |

Column positions are **not** assumed: the tool finds the header row (`Description`, `Size`,
`Package`, `Notes`, `Qty 1`, `UOM`) by name, and bounds the data by the Excel table when the
header row is a table header.

---

## Run it standalone (terminal)

```bash
cd tools/arazoza-formatter
./setup.sh                       # one-time: builds .venv from requirements.txt
echo '{"input":{},"input_path":"/path/to/Arazoza - Job - Worksheet.xlsm","work_dir":"."}' \
  | .venv/bin/python run.py
```

## Tests

```bash
cd tools/arazoza-formatter
.venv/bin/python -m unittest discover -s tests -v
```

[`tests/test_triggers.py`](tests/test_triggers.py) covers the bot-side routing this tool added
(`tool.json` `triggers` → `tool_registry`), including the rule that a request with no
`.xlsx`/`.xlsm` attached must ask for the worksheet rather than start.

The tests never call the Claude API (they clear `ANTHROPIC_API_KEY`); the Latin step is
tested with a stand-in.

`tests/make_fixture.py` builds a synthetic raw worksheet covering every rule and edge case
(also handy to *see* what the tool expects: `.venv/bin/python tests/make_fixture.py /tmp/raw.xlsx`).

Two real-file checks run when the files are on the machine:
- **Golden test** — `Vela Cove OST Output.xlsm` (a worksheet straight out of OST) formatted by
  the tool must match `Arazoza - Vela Cove - Worksheet 2026-09-01.xlsm` (the same worksheet
  finished by hand): every header, description, size, depth row and UOM. The files are looked
  for in `tests/samples/` and then the repo's `docs/`. Three differences are expected and pinned
  by the test because the spec doesn't cover them: the human's `-`/`Container` placeholders in
  Package, `LF` → `Unit` on the shrub rows, and two descriptions with hand-typed additions.
  (The human also relabelled the group headers; the tool now keeps them, so headers are
  compared with the input instead.) It also checks that every package part except the five
  the writer is meant to touch comes out byte-identical, ribbon and VBA included.
- **Round trip** — if `../../../Sample_test_sheets/` is present, the four finished landscape
  Arazoza `.xlsm` files from 2022–23 are reversed into the raw layout, run through the tool, and
  compared with what the human produced, macro workbook intact.

## Secrets & deployment

One optional secret: `ANTHROPIC_API_KEY` for the Latin-name check (without it the step is
skipped and the summary says so). Under the `lambda` backend the function otherwise only needs
read/write on the scratch S3 bucket via its execution role. Deploy with
`cd lambda && SCRATCH_BUCKET=help-bot-code-scratchpad ANTHROPIC_API_KEY=sk-ant-... ./deploy.sh`
(see [`../README.md`](../README.md)). `LATIN_MODEL` / `LATIN_EFFORT` override the model
(default `claude-haiku-5-5`, effort `medium`).

Region convention: `us-east-1`. Owner: Konur Papageorgiou. General escalation: Tommy Lather.
