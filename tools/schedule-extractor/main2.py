import pandas as pd
import fitz
import os
from datetime import datetime
import io
from io import BytesIO, StringIO
import math
import boto3
from PIL import Image
from textractcaller.t_call import call_textract, Textract_Features
from textractprettyprinter.t_pretty_print import Pretty_Print_Table_Format, Textract_Pretty_Print, get_string
from openai import OpenAI
import base64
from dotenv import load_dotenv

load_dotenv()

os.environ['AWS_DEFAULT_REGION'] = os.getenv('AWS_DEFAULT_REGION', 'us-east-1')
# Load credentials from environment (or standard boto3 config)
if not os.getenv('AWS_ACCESS_KEY_ID'):
    print("Warning: AWS credentials not found in environment.")

# Only built when a key is present so the module imports cleanly in environments where the
# OpenAI client isn't needed (e.g. a Lambda with GPT_CLEANUP off). save_excel's GPT path is
# guarded, so a None client is safe.
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY')) if os.getenv('OPENAI_API_KEY') else None

# AWS Settings
BUCKET = "test-s3-schedule-extractor-1-2026-03-17"
IN_PREFIX = "input"
OUT_PREFIX = "output"

# Perform extra formatting, like fixing any typos in the original PDF
GPT_CLEANUP = False
# Often the first column will be a list of random symbols/codes, so we can ignore it
IGNORE_FIRST_COLUMN = True
# Specific page indices to skip across all PDFs (e.g. cover pages)
SKIP_PAGES = []
# Keywords to identify schedules and legends
SCHED_KEYWORDS = ["drawing title", "legend"]
# Keywords to identify the header, where the table's columns are defined
HEAD_KEYWORDS = ["qty", "quantity", "symbol", "key"]
# Whitespace margin for which to break apart schedules (or consider the "end" of a schedule)
SCHED_MARGIN = 50
# Minimum height of a valid schedule, in pixels (so the algorithm doesn't match random "schedules" that are actually keywords within random text blocks)
MIN_SCHED_SIZE = 500

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

# From the given page, find all matches of any of the given keywords; return list of all matches
def find_in_page_from_list(page, keys):
    matches = []

    for match in page.get_text("blocks"):
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

# Get bounding coordinates for all schedules on the page
def get_sched_rects(scheds, heads, page, flip_axis):
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
        for block in page.get_text("blocks"):
            if flip_axis:
                x1, y1, x2, y2 = block[1], block[0], block[3], block[2]
            else:
                x1, y1, x2, y2 = block[0], block[1], block[2], block[3]
            
            if x1 > hx1 - SCHED_MARGIN and x1 < hx2:
                y_list.append(block)

        # Take list of all text blocks within x min/max range and sort by y
        y_list = sorted(y_list, key=lambda x: float(x[1]))

        for block in page.get_text("blocks"):
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

# OpenAI Vision (unused; less accurate & slower. Do not use!)
def extract_table_data_vision(image_bytes):
    img_b64_str = base64.b64encode(image_bytes).decode('utf-8')
    img_type = 'image/png'

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract the table from the following image, and return in a JSON-only format that can easily be converted to a Pandas DataFrame (without including any extra information or codeblock formatting)."},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{img_type};base64,{img_b64_str}"},
                    }
                ]
            }
        ]
    )

    if DEBUG:
        print(response.choices[0].message.content)
    return response.choices[0].message.content

# Process data into an excel file, separated into tabs by each schedule of each page.
# ignore_first_column / gpt_cleanup default to the module-level constants but can be
# overridden per call (the AI-callable run.py entrypoint passes them through).
def save_excel(data : dict, filename : str, stripped : bool = False,
               ignore_first_column : bool = None, gpt_cleanup : bool = None):
    if ignore_first_column is None:
        ignore_first_column = IGNORE_FIRST_COLUMN
    if gpt_cleanup is None:
        gpt_cleanup = GPT_CLEANUP
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
                
                # Extra GPT excel formatting cleanup
                # This is for funnsies, so if it fails, no big deal; move on
                try:
                    if not gpt_cleanup:
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

                    response = client.chat.completions.create(
                        messages=[
                            {"role": "system", "content": instructions[0]},
                            {"role": "user", "content": f"{df.to_json(orient='split')}"}
                        ],
                        temperature=0.0,
                        top_p=0.0,
                        model="gpt-4o"
                    )

                    if DEBUG:
                        print(response.choices[0].message.content)
                    df = pd.read_json(StringIO(response.choices[0].message.content), orient='split')
                except:
                    pass

                sheet_name = f"Page {page_num + 1}, Schedule {i + 1}" if not stripped else "Schedule"
                df.to_excel(writer, sheet_name=sheet_name, index=False)

                # Auto-adjust columns' widths
                try:
                    worksheet = writer.sheets[sheet_name]
                    for j, col in enumerate(df.columns):
                        column_len = max(df[col].astype(str).apply(len).max(), len(str(col)))
                        worksheet.set_column(j, j, column_len + 2)
                except:
                    continue



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

            scheds = find_in_page_from_list(page, SCHED_KEYWORDS)
            heads = find_in_page_from_list(page, HEAD_KEYWORDS)

            if len(scheds) == 0 or len(heads) == 0:
                continue

            flip_axis = True if abs(scheds[0][0] - scheds[0][2]) < abs(scheds[0][1] - scheds[0][3]) else False

            bboxes = get_sched_rects(scheds, heads if len(heads) > 0 else None, page, flip_axis)

            if bboxes is None:
                raise Exception(f"Schedule found on page {page_num + 1}, but its coordinates could not be accessed.")

            # Process each schedule from the current page (in case multiple exist)
            for r, rect in enumerate(bboxes):
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
                    image = extract_pdf_image(page, (rect[0] - 20, rect[1] - 20, rect[2] + 20, rect[3] + 20), dpi=300)

                    if tables.get(page_num) is None:
                        tables[page_num] = []
                    tables[page_num].append(extract_table_data(image_to_bytes(image)))
                else:
                    continue
    finally:
        pdf.close()

    return tables


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
