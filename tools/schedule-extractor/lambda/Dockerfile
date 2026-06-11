# Container image for the schedule-extractor Lambda.
# Image (not zip) because the deps (PyMuPDF, pandas, Pillow, openai, textract) exceed the
# 250 MB zip limit. Build context is the tool dir (see template.yaml DockerContext: ..),
# so main2.py and requirements.txt are at the context root and handler.py under lambda/.
FROM public.ecr.aws/lambda/python:3.12

# Install deps into the Lambda task root.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt -t "${LAMBDA_TASK_ROOT}"

# Extraction core + handler.
COPY main2.py "${LAMBDA_TASK_ROOT}/"
COPY lambda/handler.py "${LAMBDA_TASK_ROOT}/"

CMD ["handler.handler"]
