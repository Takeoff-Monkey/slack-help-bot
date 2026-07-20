import streamlit as st
import re as regex
from datetime import datetime
import fitz
from io import BytesIO
import pandas as pd
import os
import math
from utils import Header as head, Authenticator as auth, Tools as tools, UI as ui


# Set headers
head.page_header("Wall Height Calculator")

# Authenticate user
config, authenticator, st.session_state["email"], ls = auth.config_auth("wall-height-calculator")

START_TIME = None
END_TIME = None

DEBUG = True
RUNNING = False
ERRORS = 0


# Round up to nearest 0.1 (0.05 rounds up)
def sf_round(num):
    return round((num * 10) + 0.1) / 10

# Round up to nearest 0.5
def mf_round(num):
    return math.ceil(num * 2) / 2

def verify_key(key, obj1):
    if key not in obj1:
        obj1[key] = { "tops": [], "bottoms": [] }

# Transpose nested dictionary
def trans_nested_dict(nested_dict):
    trans_dict = {}

    for outer_key, inner_dict in nested_dict.items():
        for inner_key, value in inner_dict.items():
            if inner_key not in trans_dict:
                trans_dict[inner_key] = {}

            trans_dict[inner_key][outer_key] = value

    return trans_dict

def list_annots(pdf, load_bar):
    load_bar.loading(0)
    return [annot.info["content"].replace("\r", "").replace("\n", "") for page in pdf for annot in page.annots()]

def get_wall_data(annots : list, load_bar):
    walls = {}
    
    for i in range(len(annots)):
        info = annots[i]

        # Use wall ids as anchor indices
        if regex.match(r"^[A-Z]?\d+[A-Z]+", info):
            # Find TW/BW & calc height data
            if (i > 1 and regex.match(r"^TW[ ]*\d+", annots[i - 2])) or (i < len(annots) - 1 and regex.match(r"^BW[ ]*\d+", annots[i + 1])):
                id = info.split(",")
                if len(id) > 1:
                    if not regex.search(r"\d", id[0]) and regex.search(r"\d", id[1]):
                        id[0] = regex.findall(r"\d", id[1])[0] + id[0]
                    elif not regex.search(r"\d", id[1]) and regex.search(r"\d", id[0]):
                        id[1] = regex.findall(r"\d", id[0])[0] + id[1]
                    elif not regex.search(r"\d", id[0]) and not regex.search(r"\d", id[1]):
                        continue
                
                for key in id:
                    verify_key(key, walls)

                    # Save TW/BWs for wall
                    try:
                        walls[key]["bottoms"].append(float(regex.sub(r"^BW[ ]*", "", annots[i + 1].strip())) if i < len(annots) - 1 and regex.match(r"^BW[ ]*", annots[i + 1].strip()) else float(regex.sub(r"^BW[ ]*", "", annots[i - 3].strip())))
                        walls[key]["tops"].append(float(regex.sub(r"^TW[ ]*", "", annots[i + 2].strip())) if i < len(annots) - 2 and regex.match(r"^TW[ ]*", annots[i + 2].strip()) else float(regex.sub(r"^TW[ ]*", "", annots[i - 2].strip())))
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
                verify_key(info, walls)
                walls[info]["id"] = info
                walls[info]["length"] = float(regex.match(r"^\d+(\.\d+)?", annots[i - 1])[0]) if i > 0 and regex.match(r"^\d+(\.\d+)?", annots[i - 1]) is not None else None
        
        load_bar.loading(((i - 1) / (len(annots) - 1)) - 0.1 if len(annots) > 1 else 0.9)

    # Calculate rounded height & area for each wall
    for wall in walls:
        # walls[wall]["ht_round"] = round_up(walls[wall].get("ht_calc", 0) * 10) / 10 if walls[wall].get("ht_calc") is not None else None
        walls[wall]["ht_round"] = (sf_round(walls[wall].get("ht_calc", 0)) if project_type == "Single-Family" else mf_round(walls[wall].get("ht_calc", 0))) if walls[wall].get("ht_calc") is not None else None
        walls[wall]["area"] = walls[wall]["ht_round"] * walls[wall].get("length", 0) if walls[wall]["ht_round"] is not None and walls[wall].get("length") is not None else None

    return walls

def save_excel(data, filename):
    excel_data = BytesIO()
    trans_dict = trans_nested_dict(data)
    with pd.ExcelWriter(excel_data, engine='xlsxwriter') as writer:
        df = pd.DataFrame(trans_dict).reindex(columns=["id", "fit", "fib", "ht_calc", "ht_round", "length", "area"]).sort_values(by="id", key=lambda x: x.apply(lambda val: sort_alpha_len(val))).rename(columns={ "id": "Wall Type", "fit": "First Input Top", "fib": "First Input Bottom", "ht_calc": "Wall Height in Feet (Calculated)", "ht_round": "Wall Height (Roundup)", "length": "Wall Length in Feet", "area": "Wall Area (SF)" })
        df.to_excel(writer, sheet_name="Wall Data", index=False)
        
        # Auto-adjust columns' widths
        try:
            worksheet = writer.sheets["Wall Data"]
            for j, col in enumerate(df.columns):
                column_len = max(df[col].astype(str).apply(len).max(), len(str(col)))
                worksheet.set_column(j, j, column_len + 2)
        except:
            pass
    
    # Save Excel file
    with open(filename, 'wb') as f:
        f.write(excel_data.getvalue())

# Sort dataframe alphabetically & by length (1A, 1Z, 1AA, etc.)
# TODO: figure out why sorting rows with starting letter doesn't work
def sort_alpha_len(val):
    val = str(val)
    try:
        return (regex.match(r"^[A-Z]*", val)[0], int("".join(regex.findall(r"\d+", val))), len("".join(regex.findall(r"[A-Z]+$", val))), "".join(regex.findall(r"[A-Z]+$", val))) if regex.match(r"^[A-Z]*\d+[A-Z]+", val) else ("", "", "", "")
    except Exception:
        return ("", "", "", "")


tools.set_session_state_defaults({
    "max_pages": 1
})

head.title_header("Wall Height Calculator")


uploaded_files = st.file_uploader(
    label=" ",
    accept_multiple_files=True,
    type=["pdf"],
    # help="Select files to scan.",
    label_visibility="collapsed",
    on_change=tools.get_max_page_count,
    key="uploaded_files"
)

project_type = st.radio(
    label="Project type",
    index=0,
    options=["Single-Family", "Multi-Family/Commercial"],
    horizontal=True,
    help="This adjusts the wall height rounding in the output spreadsheet."
)

with st.expander("Advanced settings *(optional)*"):
    SKIP_PAGES = st.pills(
        label="Ignore pages",
        # placeholder="Select page numbers",
        options=list(range(1, st.session_state.max_pages + 1)),
        # max_selections=st.session_state.max_pages - 1,
        disabled=True if st.session_state.max_pages == 1 else False,
        help="Exclude the following pages from being processed on all PDFs. This is useful if there are particular pages you always want to ignore (like a title page).",
        selection_mode="multi"
    )

if st.button("Calculate wall heights from PDFs", type="primary") and not RUNNING:
    START_TIME = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    RUNNING = True
    ERRORS = 0

    # Move all skip page indeces down by 1 (since we start at 0)
    SKIP_PAGES = [page_num - 1 for page_num in SKIP_PAGES]

    if not uploaded_files:
        st.toast("Please upload files.", icon="⚠️")
    
    if uploaded_files:
        tools.log_widget_action(config, "in", tools.state("username"), "WALL CALC")
        all_excels = []

        proc_container = st.status("Processing PDFs")
        with proc_container:
            proc_output = ui.Terminal(f"[ STARTED PROCESS at {START_TIME} ]\n", "ini")

            for uploaded_file in uploaded_files:
                doc_name = tools.get_filename(uploaded_file.name)
                doc = uploaded_file.read()
                proc_output.update(f"Processing {uploaded_file.name}")
                file_stream = BytesIO(doc)
                pdf = fitz.open(stream=file_stream, filetype="pdf")

                try:
                    data = get_wall_data(list_annots(pdf, proc_output), proc_output)
                    save_excel(data, doc_name)
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
            st.toast(f"{ERRORS} sheet failed to process; see process log for more details." if ERRORS == 1 else f"{ERRORS} sheets failed to process; see process log for more details.", icon="⚠️")

        RUNNING = False
        tools.log_widget_action(config, "out", tools.state("username"), "WALL CALC")

        if all_excels:
            zip_file = tools.create_zip(all_excels, ".xlsx", "")
            st.download_button(
                label="Download all",
                data=zip_file,
                file_name=f"erw-scanner_{START_TIME[11:]}.zip",
                type="primary",
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

# Sidebar
ui.menu(config, authenticator, ls, "Wall Height Calculator", st.session_state["email"])
