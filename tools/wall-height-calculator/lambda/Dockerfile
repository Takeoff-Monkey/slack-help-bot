# Container image for the wall-height-calculator Lambda.
# Image (not zip) because PyMuPDF's native wheel is comfortably packaged this way and it
# keeps the build identical to the other Takeoff Monkey tools. Build context is the tool
# dir (see template.yaml DockerContext: ..), so wall_heights.py and requirements.txt are at
# the context root and handler.py under lambda/.
FROM public.ecr.aws/lambda/python:3.12

# Install deps into the Lambda task root.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt -t "${LAMBDA_TASK_ROOT}"

# Detection + output core + handler.
COPY wall_heights.py "${LAMBDA_TASK_ROOT}/"
COPY lambda/handler.py "${LAMBDA_TASK_ROOT}/"

CMD ["handler.handler"]
