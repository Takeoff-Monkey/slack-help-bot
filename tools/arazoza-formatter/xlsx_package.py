"""Write the formatter's edits into the ORIGINAL workbook package, part by part.

Why not just `openpyxl.save()`? Because openpyxl rebuilds the whole package from its own
model, and the Landscape Hub / Arazoza template carries things that model doesn't hold. Saved
through openpyxl, the file opened without the "Takeoff Monkey" ribbon tab (the `customUI`
relationship in `_rels/.rels` is dropped, though the part itself is copied), so the
**Arazoza Export** button and the rest of the macros had no way to run. The HOME sheet's
shapes and the `customXml` part went missing too.

So openpyxl only *reads* the workbook here: the formatter's steps run on its in-memory model
and record every cell they change in an `EditLog`. `write_package` then copies the original
zip entry by entry, byte for byte, and changes only:

  - the formatted sheet's XML: just the edited `<c>` cells (value, plus font or red fill);
  - `xl/styles.xml`: one red fill and the extra cell formats those edits need, appended;
  - a new sheet holding an untouched copy of the original data (values and styling, no
    table, formulas, drawings or links), added to the workbook, its rels and content types;
  - `xl/calcChain.xml` is removed and `fullCalcOnLoad` set, so Excel rebuilds the chain and
    recalculates the table's formula columns from the new values when the file opens.

The VBA project, ribbon, drawings, tables, defined names, printer settings and every other
sheet come through unchanged.
"""

from __future__ import annotations

import copy
import datetime as _dt
import posixpath
import re
import zipfile
from dataclasses import dataclass

from lxml import etree

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS_XR = "http://schemas.microsoft.com/office/spreadsheetml/2014/revision"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

REL_WORKSHEET = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
REL_CALCCHAIN = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/calcChain"
CT_WORKSHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"

MAX_SHEET_NAME = 31
RED_ARGB = "FFFF0000"

_UNSET = object()


def _m(tag: str) -> str:
    return f"{{{NS_MAIN}}}{tag}"


# ---------------------------------------------------------------------------------------
# The edit log the formatter steps write into
# ---------------------------------------------------------------------------------------

@dataclass
class CellEdit:
    value: object = _UNSET     # the new value (None clears the cell); _UNSET = value unchanged
    font_from: str | None = None   # coordinate of the ORIGINAL cell whose font this one now wears
    red: bool = False


class EditLog:
    """Every cell change the steps make, keyed by coordinate, in the order first touched."""

    def __init__(self):
        self.cells: dict[str, CellEdit] = {}

    def _get(self, coord: str) -> CellEdit:
        return self.cells.setdefault(coord, CellEdit())

    def set_value(self, coord: str, value) -> None:
        self._get(coord).value = value

    def copy_font(self, dst: str, src: str) -> None:
        """dst now wears src's font. A src that had already taken a font from another cell
        passes that one on, so the writer always resolves to an original cell's style."""
        prior = self.cells.get(src)
        self._get(dst).font_from = prior.font_from if prior and prior.font_from else src

    def fill_red(self, coord: str) -> None:
        self._get(coord).red = True

    def __len__(self) -> int:
        return len(self.cells)


# ---------------------------------------------------------------------------------------
# Small XML helpers
# ---------------------------------------------------------------------------------------

def _parse(data: bytes):
    return etree.fromstring(data, etree.XMLParser(remove_blank_text=False, huge_tree=True))


def _dump(root) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _split_coord(coord: str) -> tuple[int, int]:
    m = re.fullmatch(r"\$?([A-Z]{1,3})\$?(\d+)", coord)
    if not m:
        raise ValueError(f"bad cell reference {coord!r}")
    col = 0
    for ch in m.group(1):
        col = col * 26 + (ord(ch) - 64)
    return int(m.group(2)), col


def _resolve_target(base_part: str, target: str) -> str:
    """A relationship Target, relative to the part that owns the rels, as a zip entry name."""
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))


def _rels_name(part: str) -> str:
    d, f = posixpath.split(part)
    return posixpath.join(d, "_rels", f + ".rels")


# ---------------------------------------------------------------------------------------
# styles.xml
# ---------------------------------------------------------------------------------------

class _Styles:
    """Appends to styles.xml without disturbing the indexes anything already uses."""

    def __init__(self, data: bytes):
        self.root = _parse(data)
        self.cell_xfs = self.root.find(_m("cellXfs"))
        if self.cell_xfs is None or not len(self.cell_xfs):
            raise ValueError("styles.xml has no cellXfs")
        self._red_fill: str | None = None
        self._derived: dict = {}

    def font_of(self, xf_index: int) -> str:
        xf = self.cell_xfs[min(xf_index, len(self.cell_xfs) - 1)]
        return xf.get("fontId", "0")

    def red_fill(self) -> str:
        if self._red_fill is None:
            fills = self.root.find(_m("fills"))
            if fills is None:
                fills = etree.Element(_m("fills"))
                fonts = self.root.find(_m("fonts"))
                (fonts.addnext(fills) if fonts is not None else self.root.insert(0, fills))
            fill = etree.SubElement(fills, _m("fill"))
            pattern = etree.SubElement(fill, _m("patternFill"), patternType="solid")
            etree.SubElement(pattern, _m("fgColor"), rgb=RED_ARGB)
            etree.SubElement(pattern, _m("bgColor"), indexed="64")
            fills.set("count", str(len(fills)))
            self._red_fill = str(len(fills) - 1)
        return self._red_fill

    def derive(self, base: int, font_id: str | None, red: bool) -> int:
        """The cellXfs index for `base` with its font and/or fill swapped (reused if asked twice)."""
        base = min(base, len(self.cell_xfs) - 1)
        fill_id = self.red_fill() if red else None
        xf_base = self.cell_xfs[base]
        if (font_id is None or font_id == xf_base.get("fontId", "0")) and fill_id is None:
            return base
        key = (base, font_id, fill_id)
        if key not in self._derived:
            xf = copy.deepcopy(xf_base)
            if font_id is not None:
                xf.set("fontId", font_id)
                xf.set("applyFont", "1")
            if fill_id is not None:
                xf.set("fillId", fill_id)
                xf.set("applyFill", "1")
            self.cell_xfs.append(xf)
            self.cell_xfs.set("count", str(len(self.cell_xfs)))
            self._derived[key] = len(self.cell_xfs) - 1
        return self._derived[key]

    def to_bytes(self) -> bytes:
        return _dump(self.root)


# ---------------------------------------------------------------------------------------
# The formatted sheet
# ---------------------------------------------------------------------------------------

def _clear_value(c) -> None:
    for child in list(c):
        if child.tag in (_m("f"), _m("v"), _m("is")):
            c.remove(child)
    c.attrib.pop("t", None)


def _write_value(c, value) -> None:
    _clear_value(c)
    if value is None or (isinstance(value, str) and value == ""):
        return
    first = c[0] if len(c) else None      # keep any extLst last

    def put(el):
        if first is not None:
            first.addprevious(el)
        else:
            c.append(el)

    if isinstance(value, bool):
        c.set("t", "b")
        v = etree.Element(_m("v")); v.text = "1" if value else "0"; put(v)
    elif isinstance(value, (int, float)):
        v = etree.Element(_m("v")); v.text = repr(value); put(v)
    elif isinstance(value, str) and value.startswith("=") and len(value) > 1:
        f = etree.Element(_m("f")); f.text = value[1:]; put(f)
    else:
        if isinstance(value, _dt.datetime) and value.time() == _dt.time(0):
            value = value.date().isoformat()
        elif isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
            value = value.isoformat()
        text = str(value)
        c.set("t", "inlineStr")
        is_ = etree.Element(_m("is"))
        t = etree.SubElement(is_, _m("t")); t.text = text
        if text != text.strip() or "\n" in text:
            t.set(XML_SPACE, "preserve")
        put(is_)


class _Sheet:
    def __init__(self, data: bytes):
        self.root = _parse(data)
        self.data = self.root.find(_m("sheetData"))
        if self.data is None:
            raise ValueError("worksheet has no sheetData")
        self.rows: dict[int, object] = {}
        self.cells: dict[str, object] = {}
        for row in self.data.iterfind(_m("row")):
            self.rows[int(row.get("r"))] = row
            for c in row.iterfind(_m("c")):
                if c.get("r"):
                    self.cells[c.get("r")] = c
        # Styles as they were before any edit, so a moved font always comes from the original.
        self.original_style = {coord: int(c.get("s", "0")) for coord, c in self.cells.items()}

    def style_of(self, coord: str) -> int:
        if coord in self.original_style:
            return self.original_style[coord]
        row, col = _split_coord(coord)
        r = self.rows.get(row)
        if r is not None and r.get("s") and r.get("customFormat") == "1":
            return int(r.get("s"))
        for col_el in self._cols():
            if int(col_el.get("min")) <= col <= int(col_el.get("max")) and col_el.get("style"):
                return int(col_el.get("style"))
        return 0

    def _cols(self):
        cols = self.root.find(_m("cols"))
        return list(cols) if cols is not None else []

    def cell(self, coord: str):
        c = self.cells.get(coord)
        if c is not None:
            return c
        row_n, col_n = _split_coord(coord)
        row = self.rows.get(row_n)
        if row is None:
            row = etree.Element(_m("row"), r=str(row_n))
            later = [n for n in self.rows if n > row_n]
            if later:
                self.rows[min(later)].addprevious(row)
            else:
                self.data.append(row)
            self.rows[row_n] = row
        row.attrib.pop("spans", None)        # a hint only; stale once a cell is added
        c = etree.Element(_m("c"), r=coord)
        style = self.style_of(coord)
        if style:
            c.set("s", str(style))
        after = None
        for existing in row.iterfind(_m("c")):
            if _split_coord(existing.get("r"))[1] > col_n:
                after = existing
                break
        if after is not None:
            after.addprevious(c)
        else:
            row.append(c)
        self.cells[coord] = c
        return c

    def to_bytes(self) -> bytes:
        return _dump(self.root)


def _apply_edits(sheet: _Sheet, styles: _Styles, edits: EditLog) -> None:
    for coord, edit in edits.cells.items():
        c = sheet.cell(coord)
        if edit.value is not _UNSET:
            _write_value(c, edit.value)
        font_id = styles.font_of(sheet.style_of(edit.font_from)) if edit.font_from else None
        if font_id is not None or edit.red:
            new = styles.derive(int(c.get("s", "0")), font_id, edit.red)
            if new:
                c.set("s", str(new))
            else:
                c.attrib.pop("s", None)


# ---------------------------------------------------------------------------------------
# The copy of the original data
# ---------------------------------------------------------------------------------------

# Elements that only make sense with the sheet's own relationships (the table, drawings,
# links, embedded objects). The copy has no rels part, so they go; the values stay.
_REL_BOUND = ("tableParts", "drawing", "legacyDrawing", "legacyDrawingHF", "picture",
              "oleObjects", "controls", "hyperlinks", "webPublishItems")


def _original_copy(data: bytes) -> bytes:
    """The sheet as it arrived, as plain values: formulas replaced by the values they last
    showed (table-relative formulas would break off the table), and nothing that needs the
    original sheet's rels."""
    root = _parse(data)
    root.attrib.pop(f"{{{NS_XR}}}uid", None)
    pr = root.find(_m("sheetPr"))
    if pr is not None:
        pr.attrib.pop("codeName", None)      # that name belongs to the original's VBA module
    for view in root.iter(_m("sheetView")):
        view.attrib.pop("tabSelected", None)
    for tag in _REL_BOUND:
        for el in root.findall(_m(tag)):
            root.remove(el)
    for el in list(root.iter()):
        rid = f"{{{NS_R}}}id"
        if rid in el.attrib:
            if el.tag == _m("pageSetup"):
                del el.attrib[rid]           # printer settings blob stays with the original
            elif el.getparent() is not None:
                el.getparent().remove(el)
    for c in root.iter(_m("c")):
        f = c.find(_m("f"))
        if f is None:
            continue
        c.remove(f)
        v = c.find(_m("v"))
        if v is None:
            c.attrib.pop("t", None)
        elif c.get("t") == "str":
            text = v.text or ""
            c.remove(v)
            c.set("t", "inlineStr")
            is_ = etree.SubElement(c, _m("is"))
            t = etree.SubElement(is_, _m("t")); t.text = text
            if text != text.strip():
                t.set(XML_SPACE, "preserve")
    return _dump(root)


def _unique_sheet_name(wanted: str, taken: set) -> str:
    name = re.sub(r"[\[\]:*?/\\]", "-", wanted)[:MAX_SHEET_NAME].strip("' ")
    lowered = {t.lower() for t in taken}
    candidate, n = name, 2
    while candidate.lower() in lowered:
        suffix = f" {n}"
        candidate = name[: MAX_SHEET_NAME - len(suffix)] + suffix
        n += 1
    return candidate


# ---------------------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------------------

def original_copy_name(sheet_title: str, existing: list[str]) -> str:
    return _unique_sheet_name(f"{sheet_title} (original)", set(existing))


def write_package(src, dst, sheet_title: str, edits: EditLog, copy_title: str | None) -> None:
    """Copy the workbook at `src` to `dst`, applying `edits` to the sheet called
    `sheet_title` and (when `copy_title` is given) adding a copy of that sheet's original
    data under that name. Every other part is copied byte for byte."""
    with zipfile.ZipFile(src) as zin:
        infos = zin.infolist()
        parts = {i.filename: zin.read(i.filename) for i in infos}

    wb_part = "xl/workbook.xml"
    for name, data in parts.items():
        if name == "_rels/.rels":
            for rel in _parse(data):
                if rel.get("Type", "").endswith("/officeDocument"):
                    wb_part = _resolve_target("", rel.get("Target"))
    wb_rels_part = _rels_name(wb_part)
    workbook = _parse(parts[wb_part])
    wb_rels = _parse(parts[wb_rels_part])
    content_types = _parse(parts["[Content_Types].xml"])
    rel_target = {rel.get("Id"): rel for rel in wb_rels}

    sheets_el = workbook.find(_m("sheets"))
    sheet_part = None
    for sh in sheets_el:
        if sh.get("name") == sheet_title:
            sheet_part = _resolve_target(wb_part, rel_target[sh.get(f"{{{NS_R}}}id")].get("Target"))
    if sheet_part is None or sheet_part not in parts:
        raise ValueError(f"sheet {sheet_title!r} not found in the workbook package")

    original_sheet_xml = parts[sheet_part]
    styles_part = next((_resolve_target(wb_part, r.get("Target")) for r in wb_rels
                        if r.get("Type", "").endswith("/styles")), "xl/styles.xml")
    styles = _Styles(parts[styles_part])
    sheet = _Sheet(original_sheet_xml)
    _apply_edits(sheet, styles, edits)
    parts[sheet_part] = sheet.to_bytes()
    parts[styles_part] = styles.to_bytes()

    # calcChain lists formula cells by position; drop it and let Excel rebuild it, and make
    # the table's formula columns recalculate from the new Description/UOM/Package values.
    for rel in list(wb_rels):
        if rel.get("Type") == REL_CALCCHAIN:
            chain = _resolve_target(wb_part, rel.get("Target"))
            parts.pop(chain, None)
            wb_rels.remove(rel)
            for o in list(content_types):
                if o.get("PartName", "").lstrip("/") == chain:
                    content_types.remove(o)
    calc = workbook.find(_m("calcPr"))
    if calc is not None:
        calc.set("fullCalcOnLoad", "1")

    new_entries: list[str] = []
    if copy_title:
        n = 1
        while f"xl/worksheets/sheet{n}.xml" in parts:
            n += 1
        copy_part = f"xl/worksheets/sheet{n}.xml"
        parts[copy_part] = _original_copy(original_sheet_xml)
        new_entries.append(copy_part)

        ids = {rel.get("Id") for rel in wb_rels}
        k = 1
        while f"rId{k}" in ids:
            k += 1
        rid = f"rId{k}"
        etree.SubElement(wb_rels, f"{{{NS_PKG_REL}}}Relationship", Id=rid, Type=REL_WORKSHEET,
                         Target=posixpath.relpath(copy_part, posixpath.dirname(wb_part)))
        sheet_ids = [int(s.get("sheetId")) for s in sheets_el]
        new_sheet = etree.SubElement(sheets_el, _m("sheet"), name=copy_title,
                                     sheetId=str(max(sheet_ids, default=0) + 1))
        new_sheet.set(f"{{{NS_R}}}id", rid)
        etree.SubElement(content_types, f"{{{NS_CT}}}Override", PartName="/" + copy_part,
                         ContentType=CT_WORKSHEET)

    parts[wb_part] = _dump(workbook)
    parts[wb_rels_part] = _dump(wb_rels)
    parts["[Content_Types].xml"] = _dump(content_types)

    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            if info.filename in parts:
                zout.writestr(info, parts[info.filename], compress_type=info.compress_type)
        for name in new_entries:
            zout.writestr(name, parts[name])
