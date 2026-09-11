import pandas as pd
import fitz
import os
from datetime import datetime
import io
from io import BytesIO, StringIO
import math
import re
import boto3
from PIL import Image, ImageOps, UnidentifiedImageError
from textractcaller.t_call import call_textract, Textract_Features
from textractprettyprinter.t_pretty_print import Pretty_Print_Table_Format, Textract_Pretty_Print, get_string
import anthropic
import base64
from dotenv import load_dotenv

load_dotenv()

os.environ['AWS_DEFAULT_REGION'] = os.getenv('AWS_DEFAULT_REGION', 'us-east-1')
# Load credentials from environment (or standard boto3 config)
if not os.getenv('AWS_ACCESS_KEY_ID'):
    print("Warning: AWS credentials not found in environment.")

# Only built when a key is present so the module imports cleanly in environments where the
# Anthropic client isn't needed (e.g. a Lambda with AI_CLEANUP off). save_excel's AI path is
# guarded, so a None client is safe.
client = anthropic.Anthropic() if os.getenv('ANTHROPIC_API_KEY') else None

# AWS Settings
BUCKET = "test-s3-schedule-extractor-1-2026-03-17"
IN_PREFIX = "input"
OUT_PREFIX = "output"

# Perform extra formatting, like fixing any typos in the original PDF
AI_CLEANUP = False
# Claude model IDs — env-overridable so they can be bumped without a code change
# (set these in the tool's .env locally, or on the Lambda via template.yaml).
# CLEANUP_MODEL: the active AI cleanup pass in save_excel (only used when AI_CLEANUP is on).
# VISION_MODEL: the (currently unused) vision table-extraction fallback.
CLEANUP_MODEL = os.getenv("CLEANUP_MODEL", "claude-sonnet-5")
VISION_MODEL = os.getenv("VISION_MODEL", "claude-haiku-4-5")
# Often the first column will be a list of random symbols/codes, so we can ignore it
IGNORE_FIRST_COLUMN = True
# Specific page indices to skip across all PDFs (e.g. cover pages)
SKIP_PAGES = []
# Keywords to identify schedules and legends. Must include "schedule" — a sheet's table is
# usually labelled "PLANT SCHEDULE" / "IRRIGATION SCHEDULE" etc., so the old "drawing title"
# value (a title-block field) missed them entirely. Mirrors the proven streamlit_app.py.
SCHED_KEYWORDS = ["schedule", "legend"]
# Keywords to identify the header, where the table's columns are defined
HEAD_KEYWORDS = ["qty", "quantity", "symbol", "key"]
# Whitespace margin for which to break apart schedules (or consider the "end" of a schedule)
SCHED_MARGIN = 50
# Minimum height of a valid schedule, in pixels (so the algorithm doesn't match random "schedules" that are actually keywords within random text blocks)
MIN_SCHED_SIZE = 500

# --- Image inputs ---------------------------------------------------------------------
# Extensions handled as a schedule *image* rather than a PDF: a photo or screenshot of a
# whole sheet, or a crop of just the table. Mirrors tool.json's accepts.file_types.
IMAGE_EXTS = ("png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff", "gif")
# Textract's synchronous calls have a byte ceiling (a few MB) and a per-side pixel ceiling.
# A phone photo or a large scan goes straight through both, so everything on its way to
# Textract passes through fit_for_ocr() first. Deliberately under the documented limits:
# these are the numbers the API stops accepting, not the numbers it reads well.
MAX_OCR_SIDE = 8000
MAX_OCR_BYTES = 4_500_000
# Render DPI for the two rasterization steps. A PDF page is vector, so rendering it larger
# than life genuinely recovers detail; an image-derived page is already pixels (built at
# 1pt = 1px, see image_page_pdf), and upscaling it only adds megabytes for Textract to
# reject — so those pages render 1:1 at NATIVE_DPI.
CROP_DPI = 300
OCR_DPI = 200
NATIVE_DPI = 72

# Switch to debug mode (verbose logging during runtime)
DEBUG = False

# Internal global variables
START_TIME = None
END_TIME = None
ERRORS = 0


# Match vector to closest vector from the given array
def closest_point(v : tuple, arr : list) -> int:
    return min(range(len(arr)), key=lambda i: math.sqrt((v[0] - arr[i][0]) ** 2 + (v[1] - arr[i][1]) ** 2))

# Match float to closest float from a given array; return index
def closest_index_from(val : float, arr : list):
    return min(range(len(arr)), key=lambda i: abs(val - arr[i]))

def closest_match(val : float, arr : list, threshold : float) -> float:
    closest_index = closest_index_from(val, arr)
    closest_value = arr[closest_index]
    return closest_value if abs(val - closest_value) <= threshold else None

def near(val : float, to : float, buffer : float = 0):
    return abs(val - to) <= buffer

# Constrain value to within min and max
def clamp(value, min_value, max_value):
    return max(min_value, min(value, max_value))

# Take the bounding box of the schedule and convert to an image
def extract_pdf_image(page, rect, dpi=300):
    # 72 is the default PDF DPI
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, clip=rect)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    
    return img

# From the given text blocks, find all matches of any of the given keywords; return matches.
# `blocks` is a list in PyMuPDF get_text("blocks") shape: (x0, y0, x1, y1, text, ...). It can
# be the page's own text layer OR OCR'd blocks from Textract (see textract_page_blocks).
def find_in_page_from_list(blocks, keys):
    matches = []

    for match in blocks:
        for key in keys:
            if key in match[4].lower():
                matches.append(match)

    return matches

def force_arr_len(arr : list, length : int):
    while len(arr) < length:
        arr.append("")
    return arr

def flip_axes(base, flip):
    return base[1], base[0], base[3], base[2] if flip else base[0], base[1], base[2], base[3]

# Get bounding coordinates for all schedules on the page. `blocks` is the same list passed to
# find_in_page_from_list (text-layer or OCR), so detection + region-mapping use one source.
def get_sched_rects(scheds, heads, blocks, flip_axis):
    anchors = []
    # default_heads = ["landscape schedule", "irrigation schedule", "plant schedule", "landscape legend", "irrigation legend", "plant legend"]

    # Find schedules on page
    for sched in scheds:

        # if heads is None:
        #     head_match = False
        #     for i in range(len(default_heads)):
        #         if default_heads[i] in sched[4].lower():
        #             head_match = True
        #             break
            
        #     if not head_match:
        #         continue
        # else:
        if flip_axis:
            sx1, sy1, sx2, sy2 = sched[1], sched[0], sched[3], sched[2]
        else:
            sx1, sy1, sx2, sy2 = sched[0], sched[1], sched[2], sched[3]

        closest_head = closest_point((sx1, sy1), [(head[1], head[0]) if flip_axis else (head[0], head[1]) for head in heads])
        
        # Set coordinate system
        if flip_axis:
            hx1, hy1, hx2, hy2 = heads[closest_head][1], heads[closest_head][0], heads[closest_head][3], heads[closest_head][2]
        else:
            hx1, hy1, hx2, hy2 = heads[closest_head][0], heads[closest_head][1], heads[closest_head][2], heads[closest_head][3]

        # If there's a nearby header, this is the start of a schedule; otherwise move on
        if near(sx1, hx1, SCHED_MARGIN) and near(sy1, hy1, SCHED_MARGIN):
            if DEBUG:
                print(f"HEADER MATCH:", (sched[4], sx1, sy1), (heads[closest_head][4], hx1, hy1))
            pass
        else:
            continue
        
        anchors.append([None, None, None, None])
        y_list = []

        # Iterate through all text blocks in the page
        for block in blocks:
            if flip_axis:
                x1, y1, x2, y2 = block[1], block[0], block[3], block[2]
            else:
                x1, y1, x2, y2 = block[0], block[1], block[2], block[3]
            
            if x1 > hx1 - SCHED_MARGIN and x1 < hx2:
                y_list.append(block)

        # Take list of all text blocks within x min/max range and sort by y
        y_list = sorted(y_list, key=lambda x: float(x[1]))

        for block in blocks:
            if flip_axis:
                x1, y1, x2, y2 = block[1], block[0], block[3], block[2]
            else:
                x1, y1, x2, y2 = block[0], block[1], block[2], block[3]
            
            # Create a list of other schedules on the same page in the same section (other than the current schedule)
            other_sched_y = sorted([s[1] for s in scheds if s != sched and s in y_list and s[1] > sched[1]], key=lambda x: float(x))

            # print(y_list[y_list.index(sched)][1])
            # print(other_sched_y)

            # If block is within x bounds of header and comes after the schedule's y and before the next schedule's y (reverse if axis is flipped)
            if (not flip_axis and block in y_list and y1 >= y_list[y_list.index(sched)][1] and (y1 < other_sched_y[0] if len(other_sched_y) > 0 else True)) or \
                (flip_axis and block in y_list and y1 <= y_list[y_list.index(sched)][1] and (y1 > other_sched_y[0] if len(other_sched_y) > 0 else True)):
            # if (not flip_axis and block in y_list and y1 >= y_list[y_list.index(sched)][1]) or \
            #     (flip_axis and block in y_list and y1 <= y_list[y_list.index(sched)][1]):

                # Update anchor coords as we iterate over text blocks, to make sure we save the furthest x and y coords
                if anchors[-1][0] is None or x1 < anchors[-1][0]:
                    anchors[-1][0] = x1
                if anchors[-1][1] is None or y1 < anchors[-1][1]:
                    anchors[-1][1] = y1
                if anchors[-1][2] is None or x2 > anchors[-1][2]:
                    anchors[-1][2] = x2
                if anchors[-1][3] is None or y2 > anchors[-1][3]:
                    anchors[-1][3] = y2
    
    # Clear empty schedules
    for anchor in anchors:
        if anchor[0] is None or anchor[1] is None or anchor[2] is None or anchor[3] is None:
            anchors.remove(anchor)
    
    return anchors if anchors != [] else None

# Convert image to bytes
def image_to_bytes(image):
    img_byte_arr = io.BytesIO()
    image.save(img_byte_arr, format='PNG')
    return img_byte_arr.getvalue()

# Shrink an image until Textract will take it: no side over MAX_OCR_SIDE, and no more than
# MAX_OCR_BYTES once encoded. Halving the scale keeps the aspect ratio (the geometry below
# reads coordinates back off this image) and holds small type legible much longer than
# re-encoding as a lossy JPEG would. Returns the image unchanged when it already fits.
def fit_for_ocr(image):
    longest = max(image.width, image.height)
    if longest > MAX_OCR_SIDE:
        scale = MAX_OCR_SIDE / longest
        image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                             Image.LANCZOS)
    # PNG size isn't predictable from dimensions (a dense scan carries far more entropy than
    # a screenshot), so shrink until it actually fits rather than guessing an upfront scale.
    while len(image_to_bytes(image)) > MAX_OCR_BYTES and min(image.width, image.height) > 600:
        image = image.resize((max(1, image.width // 2), max(1, image.height // 2)), Image.LANCZOS)
    return image

# PNG bytes of an image, sized so a Textract call won't be rejected. Every Textract call
# goes through here — including the whole-page OCR fallback, which on a large sheet at
# OCR_DPI would otherwise hand over a ~7000px render the API refuses.
def image_to_ocr_bytes(image):
    return image_to_bytes(fit_for_ocr(image))

# Wrap a raster image in a one-page PDF so the whole PDF pipeline can run over it unchanged.
#
# Building the page rather than calling fitz.open(stream=..., filetype="png") buys two things:
#   * 1 point = 1 pixel. Opening an image directly scales the page by whatever DPI is tagged
#     in the file's metadata (a 1200px image tagged 300dpi opens as a 288pt page), which would
#     silently change what every geometry constant here — SCHED_MARGIN, MIN_SCHED_SIZE — means
#     from one attachment to the next. A page sized in pixels pins them to pixels.
#   * The page has no text layer, so detection takes the Textract OCR path on its own, and
#     get_sched_rects + extract_pdf_image then crop the schedule out of the image exactly the
#     way they crop it out of a drawing sheet.
#
# EXIF orientation is applied first (a sideways phone photo would otherwise defeat the
# row/column geometry entirely), and the image is fitted to Textract's ceiling before the page
# is sized, so page coordinates and OCR coordinates stay in one space. Multi-frame files
# (animated GIF, multi-page TIFF) contribute their first frame.
def image_page_pdf(image_bytes : bytes) -> bytes:
    img = ImageOps.exif_transpose(Image.open(BytesIO(image_bytes)))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img = fit_for_ocr(img)
    doc = fitz.open()
    try:
        page = doc.new_page(width=img.width, height=img.height)
        page.insert_image(page.rect, stream=image_to_bytes(img))
        return doc.tobytes()
    finally:
        doc.close()

def is_image_name(name : str) -> bool:
    return (name or "").rsplit(".", 1)[-1].lower() in IMAGE_EXTS

# OCR an entire page with Textract and return its LINE blocks in the SAME shape as
# page.get_text("blocks") — (x0, y0, x1, y1, text, idx, 0) with coords scaled to PDF points.
# This is the image-OCR fallback: when a page has no usable text layer (a scanned/flattened
# sheet with no selectable text), detection can still find schedule/header keywords from the
# OCR'd text. No tesseract binary required — Textract does the OCR in the cloud.
def textract_page_blocks(page, dpi=OCR_DPI):
    pw, ph = page.rect.width, page.rect.height
    img = extract_pdf_image(page, page.rect, dpi=dpi)
    response = call_textract(input_document=image_to_ocr_bytes(img))
    blocks = []
    for idx, b in enumerate(response.get("Blocks", [])):
        if b.get("BlockType") != "LINE":
            continue
        geo = b.get("Geometry", {}).get("BoundingBox", {})
        x1 = geo.get("Left", 0) * pw
        y1 = geo.get("Top", 0) * ph
        x2 = x1 + geo.get("Width", 0) * pw
        y2 = y1 + geo.get("Height", 0) * ph
        # Trailing "\n" matches PyMuPDF's block-text convention (downstream splits on it).
        blocks.append((x1, y1, x2, y2, b.get("Text", "") + "\n", idx, 0))
    return blocks


# Amazon Textract (once we know table coords, we use this to extract table data)
def extract_table_data(image_bytes):
    textract_data = call_textract(input_document=image_bytes, features=[Textract_Features.TABLES])
    textract_str = get_string(textract_json=textract_data, table_format=Pretty_Print_Table_Format.github, output_type=[Textract_Pretty_Print.TABLES])

    if DEBUG:
        print(textract_str)
    # cell_data = textract_str.replace("\r", "|").replace(' ",', "|").replace(" ,", "|").replace('""', '"').split("|")
    cell_data = textract_str.split("|")
    table = [[]]

    for cell in cell_data:
        if "\n" in cell:
            table.append([])

        # Add string to current row
        table[-1].append(cell.strip())

    # Remove the first column, since it's always empty
    for i in range(0, len(table)):
        if i == 0:
            continue
        table[i].pop(0)

    if DEBUG:
        print(table)
    return table

# Claude Vision (unused; less accurate & slower. Do not use!)
def extract_table_data_vision(image_bytes):
    img_b64_str = base64.b64encode(image_bytes).decode('utf-8')
    img_type = 'image/png'

    response = client.messages.create(
        model=VISION_MODEL,
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract the table from the following image, and return in a JSON-only format that can easily be converted to a Pandas DataFrame (without including any extra information or codeblock formatting)."},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": img_type, "data": img_b64_str},
                    }
                ]
            }
        ]
    )

    content = next((b.text for b in response.content if b.type == "text"), "")
    if DEBUG:
        print(content)
    return content

# Excel is strict about worksheet names — 31 characters, none of []:*?/\, no duplicates —
# and pandas' writers raise rather than truncate. Image inputs put a user-supplied filename
# into the name, so sanitize, fit and de-duplicate here instead of letting one long photo
# name abort the whole workbook.
_SHEET_BAD = re.compile(r"[\[\]:*?/\\]")

def _sheet_name(page_num : int, index : int, labels : dict, stripped : bool, used : set) -> str:
    if stripped:
        base = "Schedule"
    else:
        suffix = f", Schedule {index + 1}"
        stem = (labels or {}).get(page_num)
        if stem:
            # Trim the stem, never the suffix — "Schedule 2" is the part that disambiguates.
            stem = _SHEET_BAD.sub("-", stem).strip("'") or "Schedule"
            base = f"{stem[:31 - len(suffix)]}{suffix}"
        else:
            base = f"Page {page_num + 1}{suffix}"
    name = base[:31]
    # Two photos can share a stem, and trimming can collide two long ones.
    n = 2
    while name.lower() in used:
        tag = f" ({n})"
        name = f"{base[:31 - len(tag)]}{tag}"
        n += 1
    used.add(name.lower())
    return name


# Process data into an excel file, separated into tabs by each schedule of each page.
# ignore_first_column / ai_cleanup default to the module-level constants but can be
# overridden per call (the AI-callable run.py entrypoint passes them through).
def save_excel(data : dict, filename : str, stripped : bool = False,
               ignore_first_column : bool = None, ai_cleanup : bool = None,
               labels : dict = None):
    if ignore_first_column is None:
        ignore_first_column = IGNORE_FIRST_COLUMN
    if ai_cleanup is None:
        ai_cleanup = AI_CLEANUP
    used_sheets = set()
    with pd.ExcelWriter(filename) as writer:
        for page_num, page_data in data.items():
            for i, table_data in enumerate(page_data):
                # Attempt to do a better job of formatting
                try:
                    # Remove any empty leading rows
                    w = 0
                    while table_data[0][w] == "":
                        table_data = table_data[1:]
                        w += 1
                    
                    # Format header row
                    num_columns = max(len(row) for row in table_data)
                    df_columns = table_data[0] + [" " * k for k in range(1, (num_columns - len(table_data[0])) + 1)]
                    df = pd.DataFrame(table_data[1:], columns=df_columns)
                    # Format text cells from all caps (besides first key/code column)
                    df.iloc[:, 1 if ignore_first_column else 0:] = df.iloc[:, 1 if ignore_first_column else 0:].map(lambda x: x.title() if isinstance(x, str) else x)
                except Exception:
                    df = pd.DataFrame(table_data)
                
                # Extra AI (Claude) excel formatting cleanup
                # This is for funnsies, so if it fails, no big deal; move on
                try:
                    if not ai_cleanup:
                        raise Exception
                    instructions = [
                        """
                        I have the following planting/irrigation data in JSON. Perform these formatting steps on it:
                        (1) If there is only 1 name column, and it contains both common and botanical/latin names together, separate these into 2 different columns called "Common Name" and "Botanical Name". If there is already a botanical/latin name column, ignore this step.
                        (2) Merge subheaders split across multiple cells, and change them from all caps to title case; for example, the subheader cells "EVERGREEN", "SHRUBS" should be combined into a single cell called "Evergreen Shrubs". Do not change the keys/codes that may be in the same column, such as "QUE ALB".
                        (3) Clean up any typos in the English data (all other columns except the botanical names).
                        (4) Now check the botanical name column, and make sure that any typos in the latin name are fixed. If there is an extra specification in English (usually in quotes, like "Ilex Vomitoria 'Stokes'"), leave it in the cell.
                        Return only JSON data, without any explanation of changes, or wrapping the JSON in a code block.
                        """,
                        """
                        I have the following planting/irrigation data in JSON. Perform these formatting steps on it:
                        (1) Merge numbers with commas (like "1,000") split across multiple cells; for example, the cells '"1' and '000"' should be combined into a single cell with the value '1,000'.
                        (2) If there is only 1 Name column, and it contains both common and botanical names together, separate these into 2 different columns called "Common Name" and "Botanical Name".
                        (3) Merge subheaders split across multiple cells, and change them from all caps to title case; for example, the subheader cells "EVERGREEN", "SHRUBS" should be combined into a single cell called "Evergreen Shrubs". Do not change the keys/codes that may be in the same column, such as "QUE ALB".
                        (4) Clean up any typos in the English data (all other columns except the botanical names).
                        (5) Now check the botanical name column, and make sure that any typos in the latin name are fixed. If there is an extra specification in English (usually in quotes, like "Ilex Vomitoria 'Stokes'"), leave it in the cell.
                        Return only JSON data, without any explanation of changes, or wrapping the JSON in a code block.
                        """
                    ]

                    response = client.messages.create(
                        model=CLEANUP_MODEL,
                        max_tokens=16000,
                        thinking={"type": "disabled"},
                        system=instructions[0],
                        messages=[
                            {"role": "user", "content": f"{df.to_json(orient='split')}"}
                        ],
                    )

                    cleaned = next(b.text for b in response.content if b.type == "text")
                    if DEBUG:
                        print(cleaned)
                    df = pd.read_json(StringIO(cleaned), orient='split')
                except:
                    pass

                sheet_name = _sheet_name(page_num, i, labels, stripped, used_sheets)
                df.to_excel(writer, sheet_name=sheet_name, index=False)

                # Auto-adjust columns' widths
                try:
                    worksheet = writer.sheets[sheet_name]
                    for j, col in enumerate(df.columns):
                        column_len = max(df[col].astype(str).apply(len).max(), len(str(col)))
                        worksheet.set_column(j, j, column_len + 2)
                except:
                    continue



# Find every schedule on ONE page and return its tables (a list, possibly empty).
#
# This is the single code path both input kinds take: an image is wrapped in a one-page PDF
# by image_page_pdf and arrives here, so it gets the same keyword detection, bounding-box
# crop and Textract table recovery as a page out of a drawing set.
#
# crop_dpi / ocr_dpi are the two raster scales (NATIVE_DPI for image-derived pages, which are
# already pixels). whole_page_fallback decides what "nothing detected" means and is on only
# for images: an attachment that is already a tight crop of the table has no "PLANT SCHEDULE"
# title left to anchor on, so the frame itself is the schedule. PDFs keep the old behaviour of
# skipping such a page — running table recovery over every page of a 60-sheet set would return
# junk for most of them.
def _process_page(page, page_label : str = "page", crop_dpi : int = CROP_DPI,
                  ocr_dpi : int = OCR_DPI, whole_page_fallback : bool = False) -> list:
    tables = []

    # Primary: the PDF's own text layer (fast + exact). Empty for an image.
    blocks = page.get_text("blocks")
    scheds = find_in_page_from_list(blocks, SCHED_KEYWORDS)
    heads = find_in_page_from_list(blocks, HEAD_KEYWORDS)

    # Fallback: no schedule found in the text layer (a scanned/flattened sheet with no
    # selectable text, or any image input) — OCR the whole page with Textract and retry
    # detection on those blocks. extract_table_data already works from a rasterized region,
    # so table extraction works on image-only pages too once we have the coordinates.
    if len(scheds) == 0 or len(heads) == 0:
        try:
            ocr_blocks = textract_page_blocks(page, dpi=ocr_dpi)
        except Exception as ocr_err:
            if DEBUG:
                print(f"  {page_label}: OCR fallback failed: {ocr_err}")
            ocr_blocks = []
        if ocr_blocks:
            ocr_scheds = find_in_page_from_list(ocr_blocks, SCHED_KEYWORDS)
            ocr_heads = find_in_page_from_list(ocr_blocks, HEAD_KEYWORDS)
            if len(ocr_scheds) > 0 and len(ocr_heads) > 0:
                blocks, scheds, heads = ocr_blocks, ocr_scheds, ocr_heads
                if DEBUG:
                    print(f"  {page_label}: detected via image OCR.")

    bboxes = None
    if len(scheds) > 0 and len(heads) > 0:
        flip_axis = True if abs(scheds[0][0] - scheds[0][2]) < abs(scheds[0][1] - scheds[0][3]) else False

        bboxes = get_sched_rects(scheds, heads if len(heads) > 0 else None, blocks, flip_axis)

        if bboxes is None and not whole_page_fallback:
            raise Exception(f"Schedule found on {page_label}, but its coordinates could not be accessed.")

    # Process each schedule from the current page (in case multiple exist)
    for r, rect in enumerate(bboxes or []):
        if rect[0] is not None and rect[1] is not None and rect[2] is not None and rect[3] is not None:
            # Make sure the min size is MIN_SCHED_SIZE
            x_dist = abs(rect[0] - rect[2])
            y_dist = abs(rect[1] - rect[3])
            if x_dist < MIN_SCHED_SIZE:
                x_diff = MIN_SCHED_SIZE - x_dist
                rect[0] -= x_diff / 2
                rect[2] += x_diff / 2
                rect[0] = clamp(rect[0], 0, page.rect.width)
                rect[2] = clamp(rect[2], 0, page.rect.width)
            if y_dist < MIN_SCHED_SIZE:
                y_diff = MIN_SCHED_SIZE - y_dist
                rect[1] -= y_diff / 2
                rect[3] += y_diff / 2
                rect[1] = clamp(rect[1], 0, page.rect.height)
                rect[3] = clamp(rect[3], 0, page.rect.height)

            # Take an image of the schedule using rect coords, and use Textract to get table data
            image = extract_pdf_image(page, (rect[0] - 20, rect[1] - 20, rect[2] + 20, rect[3] + 20), dpi=crop_dpi)

            tables.append(extract_table_data(image_to_ocr_bytes(image)))
        else:
            continue

    # Nothing anchored on this page. For an image that most likely means the user cropped the
    # schedule themselves before sending it, so the whole frame is the table.
    if not tables and whole_page_fallback:
        image = extract_pdf_image(page, page.rect, dpi=crop_dpi)
        table = extract_table_data(image_to_ocr_bytes(image))
        # Textract returns an empty shell when it finds no table at all; don't make a sheet of it.
        if any(any(str(cell).strip() for cell in row) for row in table):
            tables.append(table)
        elif DEBUG:
            print(f"  {page_label}: no table found in the whole frame either.")

    return tables


# Extract every schedule/legend table from a single in-memory PDF.
# Returns {page_index: [table, ...]} where each table is a list of row-lists —
# exactly the structure save_excel() consumes. This is the callable core shared by
# the S3 batch main() and the single-file run.py entrypoint; the detection/geometry
# helpers above are untouched so extraction behaviour is identical either way.
def process_pdf(pdf_bytes : bytes, skip_pages : list = None) -> dict:
    skip_pages = SKIP_PAGES if skip_pages is None else skip_pages
    pdf = fitz.open(stream=BytesIO(pdf_bytes), filetype="pdf")
    tables = {}
    try:
        # Process each page
        for page_num, page in enumerate(pdf):
            if page_num in skip_pages:
                continue
            page_tables = _process_page(page, page_label=f"page {page_num + 1}")
            if page_tables:
                tables[page_num] = page_tables
    finally:
        pdf.close()

    return tables


# Extract every schedule from a single image. Returns [table, ...] — the same shape one PDF
# page produces. Handles both kinds of attachment: a photo/screenshot of a full sheet (the
# schedule is found and cropped out of it, as on a PDF page) and an image of nothing but the
# schedule (no title to anchor on, so the frame is taken as the table).
def process_image(image_bytes : bytes) -> list:
    pdf = fitz.open(stream=BytesIO(image_page_pdf(image_bytes)), filetype="pdf")
    try:
        return _process_page(pdf[0], page_label="the image", crop_dpi=NATIVE_DPI,
                             ocr_dpi=NATIVE_DPI, whole_page_fallback=True)
    finally:
        pdf.close()


# One line about a failed source that the bot can repeat to the user as-is. PIL's
# UnidentifiedImageError says "cannot identify image file <_io.BytesIO object at 0x7f...>",
# which tells a reader nothing and puts a memory address in the Slack thread.
def _why_failed(err : Exception) -> str:
    if isinstance(err, UnidentifiedImageError):
        return "not a readable image (or the file is corrupt)"
    return re.sub(r"<[^>]*object at 0x[0-9a-f]+>", "the file", f"{type(err).__name__}: {err}")


# Extract every schedule from a batch of attachments — any mix of images and PDFs.
#
# `sources` is [(filename, raw bytes), ...] in the order the user sent them. Returns
# (tables, labels, errors):
#   tables — {key: [table, ...]}, the structure save_excel consumes
#   labels — {key: sheet-name stem}: the image's name, or "<pdf> p4" for a page. Empty for a
#            lone PDF, which keeps the original "Page N, Schedule M" sheet names.
#   errors — ["photo.jpg: ...", ...] for sources that failed
# A failed source is recorded and skipped rather than raised: when several images are
# attached, one unreadable file must not throw away the schedules found in the rest.
def process_inputs(sources : list, skip_pages : list = None) -> tuple:
    lone_pdf = len(sources) == 1 and not is_image_name(sources[0][0])
    tables, labels, errors = {}, {}, []
    key = 0

    for name, raw in sources:
        stem = os.path.splitext(os.path.basename(name or ""))[0] or "file"
        try:
            found = {0: process_image(raw)} if is_image_name(name) else process_pdf(raw, skip_pages=skip_pages)
        except Exception as err:
            if DEBUG:
                import traceback
                traceback.print_exc()
            errors.append(f"{name}: {_why_failed(err)}")
            continue

        for page_num, page_tables in sorted(found.items()):
            if not page_tables:
                continue
            if lone_pdf:
                tables[page_num] = page_tables      # legacy key space + sheet naming
                continue
            tables[key] = page_tables
            labels[key] = stem if is_image_name(name) else f"{stem} p{page_num + 1}"
            key += 1

    return tables, labels, errors


def main():
    global START_TIME, END_TIME, ERRORS
    START_TIME = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    s3 = boto3.client("s3")
    # Get all PDF documents from S3 bucket
    objects = s3.list_objects_v2(Bucket=BUCKET, Prefix=IN_PREFIX).get("Contents", [])
    docs = [doc["Key"] for doc in objects if doc["Size"] > 0 and doc["Key"].endswith(".pdf")]

    print(f"[ STARTED PROCESS at {START_TIME} ]\n")

    for doc_key in docs:
        doc_name = doc_key.split("/")[-1]
        print(f"Processing {doc_name}...")
        try:
            doc = s3.get_object(Bucket=BUCKET, Key=doc_key)["Body"].read()
            tables = process_pdf(doc, skip_pages=SKIP_PAGES)

            if tables != {}:
                # Processed successfully, now we save to excel locally and upload it
                out_filename = f"{doc_name.replace('.pdf', '')}.xlsx"
                save_excel(tables, out_filename)

                # Upload to S3
                with open(out_filename, "rb") as xl_file:
                    s3.put_object(Body=xl_file.read(), Bucket=BUCKET, Key=f"{OUT_PREFIX}/{out_filename}")

                # Cleanup local file
                os.remove(out_filename)

            print(f"Successfully processed {doc_name}.\n")

        except Exception as err:
            import traceback
            traceback.print_exc()
            print(f"Failed to process file {doc_name}:\n{err}\n")
            ERRORS += 1
            continue

    END_TIME = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[ ENDED PROCESS at {END_TIME} ]")
    if ERRORS > 0:
        print(f"Completed with {ERRORS} errors.")

if __name__ == "__main__":
    main()
