import streamlit as st
import fitz
import os
from datetime import datetime
import pandas as pd
from utils import Header as head, Authenticator as auth, Tools as tools, UI as ui


# Set headers
head.page_header("Bid Scanner (Keyword Highlighter)")

# Authenticate user
config, authenticator, st.session_state["email"], ls = auth.config_auth("multi-scope-bid-scanner")


try:
    KEYWORDS = [
        "chain", "link", "ornamental", "fenc", "gate", "operator", "wood", "steel", "bollard", "barrier", "wedge", "crash", "turnstile", "temporary", "rail"
    ] if not config["credentials"]["usernames"][st.session_state["username"]]["settings"]["multi-scope_bid_scanner"].get("keywords") \
        else config["credentials"]["usernames"][st.session_state["username"]]["settings"]["multi-scope_bid_scanner"]["keywords"].split(",")
except:
    KEYWORDS = [
        "chain", "link", "ornamental", "fenc", "gate", "operator", "wood", "steel", "bollard", "barrier", "wedge", "crash", "turnstile", "temporary", "rail"
    ]
START_TIME = None
END_TIME = None

RUNNING = False
ERRORS = 0


def save_excel(data, filename):
    df = pd.DataFrame(data, columns=["Filename", "Keyword", "Count", "Pages"])

    # Save to Excel
    with pd.ExcelWriter(filename) as writer:
        df.to_excel(writer, sheet_name='Highlight Results', index=False)
        
        # Auto-adjust columns' widths
        try:
            for j, col in enumerate(df.columns):
                column_len = max(df[col].astype(str).apply(len).max(), len(str(col)))
                writer.sheets['Highlight Results'].set_column(j, j, column_len + 2)
        except:
            pass
    
    return filename

# Find keyword data from PDF text and save as 2d array
def keys_in_pdf(file, doc, pdf_name, keywords, error, load_bar):
    try:
        load_bar.loading(0)
        keyword_data = { keyword: { "count": 0, "pages": [] } for keyword in keywords }
        pdf = fitz.open(stream=doc, filetype="pdf")

        for page_number, page in enumerate(pdf, start=1):
            text = page.get_text()
            for keyword in keywords:
                # Adding keyword data
                if keyword.lower() in text.lower():
                    keyword_data[keyword]["count"] += text.lower().count(keyword.lower())
                    keyword_data[keyword]["pages"].append(page_number)

                # Highlighting keywords in PDF
                for i in page.search_for(keyword):
                    highlight = page.add_highlight_annot(i)
                    highlight.update()
            
            load_bar.loading(((page_number - 1) / (len(pdf) - 1)) - 0.1 if len(pdf) > 1 else 0.9)
        
        pdf.save(f"{pdf_name}")
        pdf.close()

        csv_data = [[pdf_name, keyword, data["count"], data["pages"]]
                    for keyword, data in keyword_data.items() if data["count"] > 0]
        
        load_bar.loading(1)
        load_bar.update("Successfully processed file.\n")
        return csv_data
    except Exception as err:
        load_bar.update(f"Failed to process file:\n{err}\n")
        return error + 1


tools.set_session_state_defaults({
    "max_pages": 1
})

head.title_header("Bid Scanner (Keyword Highlighter)")


uploaded_files = st.file_uploader(
    label=" ",
    accept_multiple_files=True,
    type=["pdf"],
    # help="Select files to scan.",
    label_visibility="collapsed",
    on_change=tools.get_max_page_count,
    key="uploaded_files"
)

use_standard_keys = st.toggle(
    label="Use standard keywords",
    value=True,
    help="Standard set of keywords to highlight.",
    key="use_standard_keys"
)

base_list = [
    "chain", "link", "ornamental", "fenc", "gate", "operator", "wood", "steel", "bollard", "barrier", "wedge", "crash", "turnstile", "temporary", "rail"
]

if use_standard_keys:
    predefined_keys = st.pills(
        label="Select keywords:",
        options=base_list + [keyword for keyword in KEYWORDS if keyword not in base_list],
        default=KEYWORDS,
        label_visibility="collapsed",
        key="predefined_keys",
        selection_mode="multi"
    )

keyword_input = st.text_input(
    label="Additional keywords",
    placeholder="thing 1, item 2, other",
    help="Enter additional keywords separated by commas.",
    key="keyword_input"
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

if st.button("Scan & highlight PDFs", type="primary") and not RUNNING:
    START_TIME = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    RUNNING = True
    ERRORS = 0

    # Save user preferences
    try:
        config["credentials"]["usernames"][st.session_state["username"]]["settings"]["multi-scope_bid_scanner"]["keywords"] = st.session_state["keyword_input"].replace(", ", ",") if not st.session_state["use_standard_keys"] \
            else ",".join(st.session_state["predefined_keys"]) + ("," + st.session_state["keyword_input"].replace(", ", ",") if st.session_state["keyword_input"] else "")
        auth.update_user_data(config)
    except:
        pass

    if not uploaded_files:
        st.toast("Please upload files.", icon="⚠️")
    if not keyword_input and not use_standard_keys:
        st.toast("Please provide keywords.", icon="⚠️")

    tools.log_widget_action(config, "in", tools.state("username"), "MSBS")
    
    # Move all skip page indeces down by 1 (since we start at 0)
    SKIP_PAGES = [page_num - 1 for page_num in SKIP_PAGES]
    
    if uploaded_files and (keyword_input or use_standard_keys):
        cur_keywords = []
        if keyword_input:
            cur_keywords = [keyword.strip() for keyword in keyword_input.split(',')]
        if use_standard_keys:
            cur_keywords.extend(predefined_keys)
        
        all_pdfs = []
        all_csv_data = []

        proc_container = st.status("Processing PDFs")
        with proc_container:
            proc_output = ui.Terminal(f"[ STARTED PROCESS at {START_TIME} ]\n", "ini")

            for uploaded_file in uploaded_files:
                file_name = uploaded_file.name
                file_data = uploaded_file.read()
                proc_output.update(f"Processing {file_name}")

                # Process PDF file
                csv_data = keys_in_pdf(uploaded_file, file_data, file_name, cur_keywords, ERRORS, proc_output)
                if type(csv_data) == int:
                    ERRORS += csv_data
                    continue
                all_csv_data.extend(csv_data)
                all_pdfs.append(file_name)
                uploaded_file.close()

            END_TIME = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            proc_output.update(f"[ ENDED PROCESS at {END_TIME} ]")

        if ERRORS:
            proc_container.update(state="error")
            st.toast(f"{ERRORS} sheet failed to process; see process log for more details." if ERRORS == 1 else f"{ERRORS} sheets failed to process; see process log for more details.", icon="⚠️")

        RUNNING = False
        tools.log_widget_action(config, "out", tools.state("username"), "MSBS")

        if all_csv_data:
            all_pdfs = [save_excel(all_csv_data, f"bid-scanner_{START_TIME[11:]}.xlsx")] + all_pdfs

            zip_file = tools.create_zip(all_pdfs, "", "")
            st.download_button(
                label="Download Excel results & highlighted PDFs",
                data=zip_file,
                file_name=f"bid-scanner_{START_TIME[11:]}.zip",
                type="primary",
                mime="application/zip"
            )

            with st.expander("Individual downloads"):
                for pdf in all_pdfs:
                    with open(pdf, "rb") as data:
                        st.download_button(
                            label=f"Download {pdf}",
                            data=data,
                            file_name=pdf,
                            mime="application/pdf"
                        )
                    os.remove(pdf)
        else:
            st.toast("No keywords found in the uploaded files.", icon="⚠️")


ui.menu(config, authenticator, ls, "Bid Scanner (Keyword Highlighter)", st.session_state["email"])
