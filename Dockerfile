FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/pawisoon/chomik-tools" \
      org.opencontainers.image.description="Self-hostable MCP server for chomikuj.pl with a settings panel"

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY core ./core
COPY cloner ./cloner
COPY mcp ./mcp
RUN pip install ./core ./cloner ./mcp && mkdir -p /data
WORKDIR /data
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/health')"
CMD ["chomik-mcp", "--http", "--env", "/data/.env"]
