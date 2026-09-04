# Container image for the arazoza-formatter Lambda.
# Image (not zip) to keep the build identical to the other Takeoff Monkey tools. Build
# context is the tool dir (see template.yaml DockerContext: ..), so formatter.py, the
# groupings list and requirements.txt are at the context root and handler.py under lambda/.
FROM public.ecr.aws/lambda/python:3.12

# Install deps into the Lambda task root.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt -t "${LAMBDA_TASK_ROOT}"

# Formatting core + the hardcoded takeoff groupings it matches against + handler.
COPY formatter.py "${LAMBDA_TASK_ROOT}/"
COPY takeoff-groupings.txt "${LAMBDA_TASK_ROOT}/"
COPY lambda/handler.py "${LAMBDA_TASK_ROOT}/"

CMD ["handler.handler"]
