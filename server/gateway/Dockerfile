FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements-linux.lock.txt .
RUN pip install --no-cache-dir -r requirements-linux.lock.txt && pip check
COPY app ./app
COPY migrations ./migrations
COPY maimi_projects ./maimi_projects
USER 10001:10001
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8080","--no-access-log"]
