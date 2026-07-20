import streamlit as st
import re as regex
import pandas as pd
import fitz
import os
from localStoragePy import localStoragePy
from datetime import datetime
import zipfile
from io import BytesIO
import math


fitz.TOOLS.set_small_glyph_heights(True)
ls = localStoragePy("tom_schedule-extractor", "json")
st.set_page_config(page_title="Schedule Extractor", page_icon="https://avatars.githubusercontent.com/u/154240431?s=400&u=0c23bffefdf0d19a524eb945ac3e3affaa635cdf&v=4", layout="centered", initial_sidebar_state="auto", menu_items=None)

START_TIME = None
END_TIME = None

DEBUG = False
RUNNING = False
ERRORS = 0


def get_filename(path : str):
    return regex.sub(r"\.[^.]+$", "", path.split("/")[-1])

# Instantiate localStorage variables so anything accessing them prior to their existence doesn't return an error
def set_ls_defaults(vars : list[str]):
    for var in vars:
        if ls.getItem(var) is None:
            ls.setItem(var, vars[var])

def set_session_state_defaults(vars : dict):
    for var in vars:
        if var not in st.session_state:
            st.session_state[var] = vars[var]

# Return localStorage variable as list, adding any default values
def get_ls_as_list(var : str, add : list[str] = []):
    return [i.strip() for i in ls.getItem(var).split(",")] + add if ls.getItem(var) != "" else add

# Updates localStorage with the session_state key, removing any default values
def set_ls_from_list(var : str, remove : list[str] = []):
    filtered = [i for i in st.session_state[var] if i not in remove]
    ls.setItem(var, ",".join(filtered))

# Get the max number of pages that exist from any of the PDFs, and save to session_state
def get_max_page_count():
    st.session_state.max_pages = 1
    try:
        for file in st.session_state.uploaded_files:
            pdf_bytes = file.getvalue()
            pdf = fitz.open(stream=pdf_bytes, filetype="pdf")
            if len(pdf) > st.session_state.max_pages:
                st.session_state.max_pages = len(pdf)
            pdf.close()
    except Exception as err:
        pass

# Match vector to closest vector from the given array
def closest_point(v : tuple, arr : list) -> int:
    return min(range(len(arr)), key=lambda i: math.sqrt((v[0] - arr[i][0]) ** 2 + (v[1] - arr[i][1]) ** 2))

# Match float to closest float from a given array; return index
def closest_index_from(val : float, arr : list):
    return min(range(len(arr)), key=lambda i: abs(val - arr[i]))

def closest_match(val : float, arr : list, threshold : float) -> float:
    closest_index = closest_index_from(val, arr)
    closest_value = arr[closest_index]
    if abs(val - closest_value) <= threshold:
        return closest_value
    else:
        return val

def near(val : float, to : float, buffer : float = 0):
    return abs(val - to) <= buffer

# Currently unused (delete?)
def get_rect_data(doc):
    pages = []
    rects = []
    for page_num, page in enumerate(doc):
        if page_num in SKIP_PAGES:
            continue
        for drawing in page.get_drawings():
            if drawing["color"] == (1, 0, 0):
                x1, y1, x2, y2 = drawing["rect"]
                pages.append(page_num)
                rects.append((x1, y1, x2, y2))
    
    return { "pages": pages, "rects": rects }

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

class Terminal:
    # Instantiates a new text block that can be edited later
    def __init__(self, text : str, lang : str = "markdown"):
        self.container = st.empty()
        self.text = text
        self.lang = lang
        self.last_len = len(text)
        self.container = st.code(body=self.text, language=self.lang, line_numbers=False)
    
    # Adds text to an existing text block
    def update(self, text : str, newline : bool = True):
        with self.container.container():
            if newline:
                self.container = st.code(self.text + "\n" + text, language=self.lang, line_numbers=False)
                self.text = self.text + "\n" + text
            else:
                self.container = st.code(self.text + text, language=self.lang, line_numbers=False)
                self.text = self.text + text
            self.last_len = len(text)
    
    # Replaces the previous text update with this text
    def replace_last(self, text : str, newline : bool = False):
        with self.container.container():
            if newline:
                self.container = st.code(self.text[:-self.last_len] + "\n" + text, language=self.lang, line_numbers=False)
                self.text = self.text[:-self.last_len] + "\n" + text
            else:
                self.container = st.code(self.text[:-self.last_len] + text, language=self.lang, line_numbers=False)
                self.text = self.text[:-self.last_len] + text
            self.last_len = len(text)
    
    # A loading bar [==>] with progress (from 0 to 1) and a total length of segments
    # Must start at 0, which instantiates a new loading bar
    def loading(self, progress: float, total_length: int = 20) -> str:
        # Clamp progress between 0 and 1
        progress = max(0, min(1, progress))
        filled_length = round(total_length * progress)
        
        # Fill loading bar
        if filled_length < total_length:
            bar = "=" * (filled_length - 1)
            bar += ">"
        else:
            bar = "=" * filled_length
        bar = bar.ljust(total_length)
        
        text = f"[{bar}]"

        if self.text[:-self.last_len] + text == self.text:
            return

        with self.container.container():
            if progress == 0:
                self.container = st.code(self.text + "\n" + text, language=self.lang, line_numbers=False)
                self.text = self.text + "\n" + text
            else:
                self.container = st.code(self.text[:-self.last_len] + text, language=self.lang, line_numbers=False)
                self.text = self.text[:-self.last_len] + text
            self.last_len = len(text)

# TODO: clean up variable names
def get_sched_data(pdf, load_bar):
    data = {}
    load_bar.loading(0)
    
    for page_num, page in enumerate(pdf):
        if page_num in SKIP_PAGES:
            continue
        
        scheds = find_in_page_from_list(page, SCHED_KEYWORDS)
        heads = find_in_page_from_list(page, HEAD_KEYWORDS)
        sched_num = 0

        if len(scheds) == 0 or len(heads) == 0:
            continue

        if DEBUG:
            print(f"SCHEDS: {scheds}\n")

        flip_axis = True if abs(scheds[0][0] - scheds[0][2]) < abs(scheds[0][1] - scheds[0][3]) else False

        for sched in scheds:
            if flip_axis:
                sx1, sy1, sx2, sy2 = sched[1], sched[0], sched[3], sched[2]
            else:
                sx1, sy1, sx2, sy2 = sched[0], sched[1], sched[2], sched[3]

            line_height = abs(sy2 - sy1)
            closest_head = closest_point((sx1, sy1), [(head[1], head[0]) if flip_axis else (head[0], head[1]) for head in heads])
            
            if flip_axis:
                hx1, hy1, hx2, hy2 = heads[closest_head][1], heads[closest_head][0], heads[closest_head][3], heads[closest_head][2]
            else:
                hx1, hy1, hx2, hy2 = heads[closest_head][0], heads[closest_head][1], heads[closest_head][2], heads[closest_head][3]

            # If there's a nearby header, this is the start of a schedule; otherwise move on
            if near(sx1, hx1, SCHED_MARGIN) and near(sy1, hy1, SCHED_MARGIN):
                if DEBUG:
                    print(f"MATCH: ({sx1}, {sy1}) {heads[closest_head]}\n")
                sched_num += 1
            else:
                continue
            
            y_list = []
            y_blocks = {}
            sched_blocks = []

            for block in page.get_text("blocks"):
                if flip_axis:
                    x1, y1, x2, y2 = block[1], block[0], block[3], block[2]
                else:
                    x1, y1, x2, y2 = block[0], block[1], block[2], block[3]
                
                # If block is within x bounds of header and close to y of one of the rows in the list
                if x1 > hx1 - SCHED_MARGIN and x1 < hx2 and ((len(y_list) > 0 and closest_match(y1, y_list, SCHED_MARGIN / 2)) or len(y_list) == 0):
                    y_list.append(y1)

                    if y_blocks.get(y1) is None:
                        y_blocks[y1] = [0, []]
                    y_blocks[y1][0] += 1
                    y_blocks[y1][1].append(block)

                    sched_blocks.append(block)

            base_col = y_blocks[max(y_blocks, key=y_blocks.get)][1]
            row_y = [base_col[y][0 if flip_axis else 1] for y in range(len(base_col))]

            output = {}
            out_coords = {}
            out_x2 = {}

            for block in sched_blocks:
                if flip_axis:
                    x1, y1, x2, y2 = block[1], block[0], block[3], block[2]
                else:
                    x1, y1, x2, y2 = block[0], block[1], block[2], block[3]
                
                text = block[4].split("\n")[:-1]
                text = [t for t in text if t != ""]

                # Match y to closest y-coord within threshold
                y_key = closest_match(y1, row_y, line_height / 2 + 1)

                # Set empty arrays so we can store values for this column
                if output.get(y_key) is None:
                    output[y_key] = []
                    out_coords[y_key] = []
                    out_x2[y_key] = []

                # Will usually match "symbol", "key" or "code" at the beginning of the row
                if REMOVE_HEADS and text[0] == heads[closest_head][4].split("\n")[0]:
                    continue

                # If the current cell is in the same row but offset (it's multiline text) and its x is within the previous cell's, merge text
                if JOIN_WRAPPED_TEXT and len(output[y_key]) > 0 and abs(y1 - y_key) > TEXT_MARGIN and (x1 > out_coords[y_key][-1] - TEXT_MARGIN and x2 < out_x2[y_key][-1] + TEXT_MARGIN):
                    output[y_key][-1][-1] += " ".join(text)
                else:
                    output[y_key].append(text)
                    out_coords[y_key].append(x1)
                    out_x2[y_key].append(x2)

            # Sort each row by x-coord (so columns align correctly in each row)
            sorted_array1 = {}
            for y in out_coords:
                sorted_indices = sorted(range(len(out_coords[y])), key=lambda i: out_coords[y][i])
                if sorted_array1.get(y) is None:
                    sorted_array1[y] = []
                for i in sorted_indices:
                    sorted_array1[y] += output[y][i]

            # if DEBUG:
            #     print(sorted_array1)

            # Sort each row by y-coord (so rows are ordered correctly)
            sorted_array2 = [sorted_array1[y] for y in reversed(sorted(sorted_array1.keys(), key=lambda i: i))] if flip_axis else [sorted_array1[y] for y in sorted(sorted_array1.keys(), key=lambda i: i)]

            # Remove any extraneous rows before schedule header (if any accidentally made it in)
            for r, row in enumerate(sorted_array2):
                if sched[4].split("\n")[0] in row:
                    sorted_array2 = sorted_array2[r + 1:] if REMOVE_HEADS else sorted_array2[r:]
                    break

            data[f"Page {page_num + 1}, Schedule {sched_num}" if sched_num > 1 else f"Page {page_num + 1}"] = sorted_array2
        
        load_bar.loading(((page_num - 1) / (len(pdf) - 1)) - 0.1 if len(pdf) > 1 else 0.9)
    
    if DEBUG:
        print(data)
    
    return data

# Process all pages of the doc into an excel file, separated into tabs by each page
def save_excel(data, filename):
    excel_data = BytesIO()
    with pd.ExcelWriter(excel_data, engine='xlsxwriter') as writer:
        for sheet_name, sheet_data in data.items():
            df = pd.DataFrame(sheet_data)
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            
            # Auto-adjust columns' widths
            worksheet = writer.sheets[sheet_name]
            for i, col in enumerate(df.columns):
                column_len = max(df[col].astype(str).map(len).max(), len(str(col)))
                worksheet.set_column(i, i, column_len + 2)
    
    # Save Excel file
    with open(filename, 'wb') as f:
        f.write(excel_data.getvalue())
    
    # return filename

def create_zip(files : list, ext : str = "", prefix : str = ""):
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
        for file in files:
            file = file
            zip_file.write(file, prefix + os.path.basename(file) + ext)
    return zip_buffer.getvalue()


st.title("Schedule Extractor")

# Initialize data storage
set_ls_defaults({
    "sched_keys": "",
    "head_keys": ""
})
set_session_state_defaults({
    "max_pages": 1,
    "sched_keys": [],
    "head_keys": []
})


uploaded_files = st.file_uploader(
    label=" ",
    accept_multiple_files=True,
    type=["pdf"],
    help="Select files to scan.",
    on_change=get_max_page_count,
    key="uploaded_files"
)

with st.expander("Advanced Settings (optional)"):
    SKIP_PAGES = st.multiselect(
        label="Ignore pages",
        placeholder="Select page numbers",
        options=list(range(1, st.session_state.max_pages + 1)),
        max_selections=st.session_state.max_pages - 1,
        disabled=True if st.session_state.max_pages == 1 else False,
        help="Exclude the following pages from being processed on all PDFs. This is useful if there are particular pages with schedule headers that you want to ignore."
    )
    st.markdown(
        body="Automatic Smart-Checking Settings",
        help="Adjusts the smart-checking algorithm as it searches for schedules on a page and maps text."
    )
    SCHED_KEYWORDS = st.multiselect(
        label="Schedule keywords",
        placeholder="Select schedule keywords",
        # options=get_ls_as_list("sched_keys", ["schedule", "legend"]),
        options=["schedule", "legend"],
        default=["schedule", "legend"],
        # key="sched_keys",
        # on_change=set_ls_from_list("sched_keys", ["schedule", "legend"]),
        help="Keywords listed here will be used to determine the presence of a schedule, in conjuction with Header Keywords."
    )
    add_sched_keys = st.text_input(
        label="Additional schedule keywords",
        placeholder="thing 1, item 2, other",
        help="Enter additional schedule keywords separated by commas."
    )
    SCHED_KEYWORDS = [key.strip() for key in add_sched_keys.split(",")] + SCHED_KEYWORDS if add_sched_keys != "" else SCHED_KEYWORDS
    HEAD_KEYWORDS = st.multiselect(
        label="Header keywords",
        placeholder="Select page numbers",
        options=["qty", "quantity", "symbol", "key"],
        default=["qty", "quantity", "symbol", "key"],
        # on_change=set_ls_from_list("head_keys", ["qty", "quantity", "symbol", "key"]),
        help="Keywords listed here will be used to determine the presence of a schedule, in conjuction with Schedule Keywords."
    )
    add_head_keys = st.text_input(
        label="Additional header keywords",
        placeholder="thing 1, item 2, other",
        help="Enter additional header keywords separated by commas."
    )
    HEAD_KEYWORDS = [key.strip() for key in add_head_keys.split(",")] + HEAD_KEYWORDS if add_head_keys != "" else HEAD_KEYWORDS
    TEXT_MARGIN = st.number_input(
        label="Text margin",
        placeholder=3,
        min_value=0,
        max_value=None,
        value=3,
        step=1,
        on_change=ls.setItem("text_margin", ["thing 1", "thing 2"]),
        help="Margin of error (in pixels) for matching text that is roughly on the same axis (used for creating columns/rows if some text isn't *exactly* on the same axis). Applies to all sides of text (so 3px is actually 6 total for a single axis)."
    )
    SCHED_MARGIN = st.number_input(
        label="Schedule margin",
        placeholder=50,
        min_value=0,
        max_value=None,
        value=50,
        step=1,
        on_change=ls.setItem("sched_margin", ["thing 1", "thing 2"]),
        help="Margin (in pixels) for finding & matching text blocks within proximity of a schedule. Any text found within this threshold of other text already linked to the schedule will be added to it."
    )
    JOIN_WRAPPED_TEXT = st.checkbox(
        label="Join wrapped text",
        value=True,
        on_change=ls.setItem("join_wrapped_text", ["thing 1", "thing 2"]),
        help="Attempt to join text that wraps to a new line (row) into the same line. May give mixed results, such as merging some columns to the right into a single column or missing some lines altogether."
    )
    REMOVE_HEADS = st.checkbox(
        label="Remove headers",
        value=False,
        on_change=ls.setItem("remove_heads", ["thing 1", "thing 2"]),
        help="Attempt to remove any header rows from schedule data. Usually overlooks headers that use a different formatting convention than the main (first) one, such as subheaders."
    )

if st.button("Scan PDFs") and not RUNNING:
    START_TIME = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    RUNNING = True
    ERRORS = 0

    # Move all skip page indeces down by 1 (since we start at 0)
    SKIP_PAGES = [page_num - 1 for page_num in SKIP_PAGES]

    if not uploaded_files:
        st.toast("Please upload files.")
    
    if uploaded_files:

        all_excels = []

        proc_container = st.status("Processing PDFs")
        with proc_container:
            proc_output = Terminal(f"[ STARTED PROCESS at {START_TIME} ]\n", "ini")

            for uploaded_file in uploaded_files:
                doc_name = get_filename(uploaded_file.name)
                doc = uploaded_file.read()
                proc_output.update(f"Processing {uploaded_file.name}")
                file_stream = BytesIO(doc)
                pdf = fitz.open(stream=file_stream, filetype="pdf")

                try:
                    sched_data = get_sched_data(pdf, proc_output)
                    save_excel(sched_data, doc_name)
                    all_excels.append(doc_name)
                except Exception as err:
                    proc_output.update(f"Failed to process file:\n{err}\n")
                    ERRORS += 1
                    continue

                proc_output.loading(1)
                proc_output.update("Successfully processed file.\n")
                pdf.close()
            
            END_TIME = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            proc_output.update(f"[ ENDED PROCESS at {END_TIME} ]")
        
        if ERRORS:
            proc_container.update(state="error")
            st.toast(f"{ERRORS} sheet failed to process; see process log for more details." if ERRORS == 1 else f"{ERRORS} sheets failed to process; see process log for more details.")

        RUNNING = False

        if all_excels:
            zip_file = create_zip(all_excels, ".xlsx", "")
            st.download_button(
                label="Download all",
                data=zip_file,
                file_name=f"schedule-extractor_{START_TIME[11:]}.zip",
                mime="application/zip"
            )

            with st.expander("Individual downloads"):
                for doc in all_excels:
                    with open(doc, "rb") as data:
                        st.download_button(
                            label=f"Download {doc}.xlsx",
                            data=data,
                            file_name=f"{doc}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        )
                    os.remove(doc)
