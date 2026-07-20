# Container image for the DEFAULT run_code sandbox Lambda (tm-sandbox-runcode).
# Build context is the sandbox dir (template.yaml DockerContext: ..), so requirements.txt is at
# the context root and handler.py under lambda/.
#
# This is the extended toolkit: Tesseract OCR + poppler + opencv-headless + PDF/data/doc libs.
# The heavier neural OCR engine lives in the sibling Dockerfile.ocr (tm-sandbox-runcode-ocr).
FROM public.ecr.aws/lambda/python:3.12

# System binaries for OCR. Tesseract is NOT in the default AL2023 repos and there is no EPEL for
# AL2023 — it ships in SPAL (Supplementary Packages for Amazon Linux), enabled via spal-release
# (which is in the base repo). poppler-utils (pdftoppm, for pdf2image) IS in the base repo.
# --setopt=install_weak_deps=0 --nodocs trims the fonts/cairo weak deps; clean in the SAME layer.
# NOTE: SPAL needs an AL2023 base >= 2023.9.20251117 — don't pin an older base-image digest.
RUN dnf install -y spal-release && \
    dnf install -y --setopt=install_weak_deps=0 --nodocs \
        tesseract tesseract-langpack-eng poppler-utils && \
    dnf clean all && rm -rf /var/cache/dnf
# TESSDATA_PREFIX is unnecessary — the SPAL tesseract binary defaults to
# /usr/share/tesseract/tessdata where tesseract-langpack-eng installs eng.traineddata.

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt -t "${LAMBDA_TASK_ROOT}"

COPY lambda/handler.py "${LAMBDA_TASK_ROOT}/"

# Fail the build early if the OCR toolchain isn't actually importable/runnable in the image.
# PYTHONPATH must point at LAMBDA_TASK_ROOT here: deps were installed with `-t`, and /var/task is
# only auto-added to sys.path by the Lambda runtime, not by a bare `python` at build time.
RUN PYTHONPATH="${LAMBDA_TASK_ROOT}" python -c "import fitz, pandas, PIL, openpyxl, pytesseract, cv2, pdfplumber, pdf2image, docx, pptx, reportlab, xlsxwriter, tabulate" && \
    tesseract --list-langs && pdftoppm -v

CMD ["handler.handler"]
