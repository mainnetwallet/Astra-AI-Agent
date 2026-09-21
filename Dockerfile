# Astra AI Agent — container image.
#
#   docker build -t astra-agent .
#   docker run -p 8787:8787 -v astra-data:/data -e ASTRA_TOKEN=... astra-agent
#
# BIND=0.0.0.0 below is what makes -p 8787:8787 work: the container's own
# namespace is the isolation boundary, so uvicorn must listen on all of its
# interfaces rather than its loopback. Publishing the port (and setting
# ASTRA_TOKEN) is the operator's decision at `docker run`, never a default.
#
# Runtime is Python 3.11 slim. The web server is FastAPI/uvicorn, installed
# from requirements.txt; the optional Playwright browser layer is NOT
# installed (opt-in): browser tools report "unavailable" honestly unless you
# extend this image with `pip install playwright &&
# python -m playwright install --with-deps chromium`.
FROM python:3.11-slim

LABEL org.opencontainers.image.title="Astra AI Agent" \
      org.opencontainers.image.description="Personal AI OS — local AI agent with a provider router" \
      org.opencontainers.image.source="https://github.com/mainnetwallet/Astra-AI-Agent"

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PORT=8787 \
    BIND=0.0.0.0 \
    NO_BROWSER=1 \
    DATA_DIR=/data \
    ASTRA_SCHEDULER=1

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN mkdir -p /data && python3 -m compileall -q -f /app/astra || true

VOLUME /data
EXPOSE 8787

CMD ["python3", "run.py"]
