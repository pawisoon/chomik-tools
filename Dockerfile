FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY core ./core
COPY cloner ./cloner
COPY mcp ./mcp
RUN pip install ./core ./cloner ./mcp && useradd -m -u 1000 chomik && mkdir -p /data && chown chomik /data
USER chomik
WORKDIR /data
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/health')"
CMD ["chomik-mcp", "--http", "--env", "/data/.env"]
