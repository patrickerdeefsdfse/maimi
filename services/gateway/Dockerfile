FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt requirements-linux.lock.txt ./
RUN pip install --no-cache-dir -r requirements-linux.lock.txt && pip check

# Optional wheels are only for the historical Python DSH adapter. The current
# native Node worker runtime is assembled independently of this gateway image.
COPY vendor-wheels /tmp/dsh-wheels
RUN set -eux; \
    set -- /tmp/dsh-wheels/*.whl; \
    if [ -e "$1" ]; then pip install --no-cache-dir --no-index "$@" && pip check; fi; \
    rm -rf /tmp/dsh-wheels

COPY app ./app
COPY maimi_projects ./maimi_projects
RUN mkdir -p /var/lib/megmeet/workspaces /var/lib/megmeet/dsh /var/lib/megmeet/iam \
    && chown -R 10001:10001 /var/lib/megmeet
USER 10001:10001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
