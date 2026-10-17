ARG DSH_HOST_BASE_IMAGE=megmeet-personal-dsh:20260918-r8
FROM python@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e AS python-office
COPY office-engine/requirements-linux.lock.txt /opt/office/requirements.lock.txt
RUN python -m venv /opt/office/venv && /opt/office/venv/bin/pip install --no-cache-dir -r /opt/office/requirements.lock.txt

FROM ${DSH_HOST_BASE_IMAGE}
USER root
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
COPY --from=python-office /usr/local/bin/python3.13 /usr/local/bin/python3.13
COPY --from=python-office /usr/local/lib/libpython3.13.so.1.0 /usr/local/lib/
COPY --from=python-office /usr/local/lib/python3.13 /usr/local/lib/python3.13
COPY --from=python-office /opt/office /opt/office
RUN ldconfig && apt-get update && apt-get install -y --no-install-recommends libreoffice-writer libreoffice-calc libreoffice-impress fonts-noto-cjk fonts-dejavu-core libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
RUN ln -s /usr/local/bin/python3.13 /usr/local/bin/python && ln -s /usr/local/bin/python3.13 /usr/local/bin/python3
COPY office-engine/engine.py office-engine/ocr-config.json office-engine/ocr-models.json /opt/office/
COPY office-engine/ocr-models /opt/office/ocr-models
COPY dev-local/dsh/office-adapter /opt/dsh/office-adapter
COPY services/personal-cloud/worker.mjs /opt/personal-cloud/worker.mjs
USER node
WORKDIR /workspace
CMD ["node", "/opt/personal-cloud/worker.mjs"]
