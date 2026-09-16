# Astra AI Agent — container image.
#
#   docker build -t astra-agent .
#   docker run -p 8787:8787 -v astra-data:/data -e ASTRA_TOKEN=... astra-agent
#
# Runtime is Python 3.11 slim (core is stdlib-only). The optional Playwright
# browser layer is NOT installed (opt-in): browser tools report "unavailable"
# honestly unless you extend this image with `pip install playwright &&
# python -m playwright install --with-deps chromium`.
FROM python:3.11-slim

LABEL org.opencontainers.image.title="Astra AI Agent" \
      org.opencontainers.image.description="Personal AI OS — plugin-based local agent" \
      org.opencontainers.image.source="https://github.com/mainnetwallet/Astra-AI-Agent"

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PORT=8787 \
    HOST=127.0.0.1 \
    NO_BROWSER=1 \
    DATA_DIR=/data \
    ASTRA_SCHEDULER=1

COPY . /app

# Zero-dependency core — nothing to pip install. Byte-compile as a sanity check.
RUN mkdir -p /data && python3 -m compileall -q -f /app/astra || true

VOLUME /data
EXPOSE 8787

CMD ["python3", "run.py"]