# Astra AI Agent — container image.
#
#   docker build -t astra-agent .
#   docker run -p 8787:8787 -v astra-data:/data -e ASTRA_TOKEN=... astra-agent
#
# BIND=0.0.0.0 below is what makes -p 8787:8787 work: the container's own
# namespace is the isolation boundary, so the server must listen on all of its
# interfaces rather than its loopback. Publishing the port (and setting
# ASTRA_TOKEN) is the operator's decision at `docker run`, never a default.
# NOTE: the variable is BIND — an earlier revision set HOST=127.0.0.1, which
# no code reads, so the container was only reachable from inside itself.
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
    BIND=0.0.0.0 \
    NO_BROWSER=1 \
    DATA_DIR=/data \
    ASTRA_SCHEDULER=1

COPY . /app

# Zero-dependency core — nothing to pip install. Byte-compile as a sanity check.
RUN mkdir -p /data && python3 -m compileall -q -f /app/astra || true

VOLUME /data
EXPOSE 8787

CMD ["python3", "run.py"]