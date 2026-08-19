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

# Drop root: run the server as an unprivileged user
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080

# /healthz is deliberately outside the auth gate. The previous check hit /mcp,
# which returns 401 once OAuth is enabled and would mark the container unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:${MCP_PORT:-8080}/healthz || exit 1

ENTRYPOINT ["python", "-m", "freshservice_mcp.server"]
