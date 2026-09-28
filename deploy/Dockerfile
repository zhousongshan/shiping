FROM node:26.5.0-bookworm-slim AS node
FROM python:3.12-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && apt-get update && apt-get install -y --no-install-recommends ffmpeg chromium ca-certificates libstdc++6 fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 video \
 && mkdir -p /app /data && chown video:video /app /data
WORKDIR /app
COPY requirements-lock.txt ./
RUN pip install --no-cache-dir -r requirements-lock.txt
COPY --chown=video:video package.json package-lock.json ./
USER video
RUN npm ci
COPY --chown=video:video app ./app
COPY --chown=video:video static ./static
COPY --chown=video:video agent-tools ./agent-tools
COPY --chown=video:video provider ./provider
COPY --chown=video:video provider-autodl-wan ./provider-autodl-wan
COPY --chown=video:video scripts ./scripts
ENV PYTHONUNBUFFERED=1 VIDEO_AGENT_DATA=/data
CMD ["python", "-m", "uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "4780"]
