FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Optional, locally supplied DSH wheels. The repository ships an empty
# directory so the default image remains small and DSH-disabled. Linux test
# setup may copy already-verified wheels here before building with DSH enabled.
COPY vendor-wheels /tmp/dsh-wheels
RUN set -eux; \
    set -- /tmp/dsh-wheels/*.whl; \
    if [ -e "$1" ]; then pip install --no-cache-dir --no-index "$@"; fi; \
    rm -rf /tmp/dsh-wheels

COPY app ./app
RUN mkdir -p /var/lib/megmeet/workspaces /var/lib/megmeet/dsh /var/lib/megmeet/iam \
    && chown -R 10001:10001 /var/lib/megmeet
USER 10001:10001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
