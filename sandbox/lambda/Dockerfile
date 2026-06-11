# Container image for the run_code sandbox Lambda.
# Build context is the sandbox dir (template.yaml DockerContext: ..), so requirements.txt
# is at the context root and handler.py under lambda/.
FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt -t "${LAMBDA_TASK_ROOT}"

COPY lambda/handler.py "${LAMBDA_TASK_ROOT}/"

CMD ["handler.handler"]
