# Existing verified server image provides Python, LibreOffice and OCR once for
# the shared service. Never distribute this image's payload in a desktop build.
ARG OFFICE_BASE_IMAGE=megmeet-personal-dsh:20260921-r11
FROM ${OFFICE_BASE_IMAGE}
USER root
COPY services/office-service/requirements.txt /opt/office-service/requirements.txt
RUN /opt/office/venv/bin/python -m pip install --no-cache-dir -r /opt/office-service/requirements.txt \
    && mkdir -p /var/lib/megmeet-office/jobs \
    && chown -R 10001:10001 /var/lib/megmeet-office
COPY services/office-service/app.py services/office-service/runner.py /opt/office-service/
ENV PYTHONDONTWRITEBYTECODE=1 OFFICE_JOB_ROOT=/var/lib/megmeet-office/jobs
USER 10001:10001
WORKDIR /opt/office-service
EXPOSE 8093
HEALTHCHECK --interval=10s --timeout=4s --retries=12 CMD /opt/office/venv/bin/python -I -B -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8093/health',timeout=3).read()"
CMD ["/opt/office/venv/bin/python","-m","uvicorn","app:app","--host","0.0.0.0","--port","8093","--workers","1","--no-access-log"]
