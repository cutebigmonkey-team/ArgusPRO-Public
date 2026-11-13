FROM node:24-bookworm-slim AS comments-ui-check
WORKDIR /checks
COPY ops/validation/comments-ui /checks
RUN node verify.mjs && node --test frontend/cloud/*.test.mjs \
    && node --check frontend/cloud/workspace.mjs \
    && cp manifest.json /comments-ui-verified.json

FROM python:3.12-slim-bookworm AS worker-source
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 ARGUS_COLLECTOR_RUNTIME=hostless PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
COPY ops/drive/requirements.txt /app/drive-requirements.txt
COPY ops/collect/requirements-enhanced-engines.txt /app/enhanced-engine-requirements.txt
RUN pip install --no-cache-dir -r drive-requirements.txt -r enhanced-engine-requirements.txt tzdata==2025.2 beautifulsoup4==4.14.3 feedparser==6.0.12 soupsieve==2.9.0 playwright==1.55.0 yt-dlp==2026.10.26 youtube-comment-downloader==0.1.84 trafilatura==2.3.1 urllib3==2.8.0 'psycopg[binary]==3.3.8' \
    && pip install --no-cache-dir 'https://github.com/cutebigmonkey-team/ARGUS-Collectors/archive/54af59c52e5da9d64069dd31eabab3a2ff198fe9.tar.gz' \
    && python -m playwright install --with-deps chromium \
    && useradd --uid 10001 --create-home collector
COPY ops/collect /app/ops/collect
COPY ops/drive /app/ops/drive
COPY ops/local-storage /app/ops/local-storage
COPY ops/hostless /app/ops/hostless
COPY ops/ai /app/ops/ai
COPY backend/src/platforms/*Adapter.cpp /app/backend/src/platforms/
ARG ARGUS_SOURCE_SHA=""
RUN python -c 'import os,re,pathlib; value=os.environ.get("ARGUS_SOURCE_SHA", ""); assert not value or re.fullmatch("[0-9a-f]{40}",value); pathlib.Path("/app/release-sha").write_text(value)'
RUN python /app/ops/hostless/check_image.py
RUN python /app/ops/hostless/memory.py
USER 10001:10001

# A failed check prevents creation of the final worker image. PostgreSQL exists
# only in this disposable build stage and never connects to the production DB.
FROM worker-source AS comments-backend-check
USER root
RUN apt-get update && apt-get install -y --no-install-recommends postgresql \
    && rm -rf /var/lib/apt/lists/*
RUN sh /app/ops/hostless/check_comments_build.sh

FROM worker-source AS production
COPY --from=comments-backend-check /comments-backend-verified.json /app/comments-backend-verified.json
COPY --from=comments-ui-check /comments-ui-verified.json /app/comments-ui-verified.json
CMD ["python","/app/ops/hostless/worker.py"]
