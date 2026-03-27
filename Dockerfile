# Freshservice Managed MCP Server
# A managed fork of effytech/freshservice_mcp with controlled tool access
FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/TeeJS/freshservice_mcp_managed"
LABEL org.opencontainers.image.description="Freshservice Managed MCP Server - controlled tool access via allowlists"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# Install system dependencies for building Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy project definition
COPY pyproject.toml ./
COPY src ./src

# Install project and dependencies
RUN pip install --no-cache-dir .

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf -o /dev/null -w '%{http_code}' -H 'Accept: text/event-stream' http://localhost:${MCP_PORT:-8080}/mcp | grep -q '200\|405\|406' || exit 1

ENTRYPOINT ["python", "-m", "freshservice_mcp.server"]
