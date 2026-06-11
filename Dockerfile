FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for faster dependency management
RUN pip install --no-cache-dir uv

COPY . .

# Install Python dependencies using uv sync
RUN uv sync --no-dev

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app

# Give read and write access to the store_creds volume
RUN mkdir -p /app/store_creds \
    && chown -R app:app /app/store_creds \
    && chmod 755 /app/store_creds

USER app

# Expose port (use default of 8000 if PORT not set)
EXPOSE 8000
# Expose additional port if PORT environment variable is set to a different value
ARG PORT
EXPOSE ${PORT:-8000}

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD sh -c 'curl -f http://localhost:${PORT:-8000}/health || exit 1'

# Stream stdout/stderr unbuffered so Render's log view sees output in real
# time, and lock encoding to UTF-8 so the startup banner's emoji don't trip
# encoding errors on the slim base image.
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8

# Set environment variables for Python startup args
ENV TOOL_TIER=""
ENV TOOLS=""

# Don't re-sync the environment at container start: dependencies were already
# installed at build time with `uv sync --no-dev`. A plain `uv run` would
# implicitly re-sync (including dev deps), needing network access and slowing
# every cold start.
ENV UV_NO_SYNC=1

# Use entrypoint for the base command and CMD for args
ENTRYPOINT ["/bin/sh", "-c"]
CMD ["uv run main.py --transport streamable-http ${TOOL_TIER:+--tool-tier \"$TOOL_TIER\"} ${TOOLS:+--tools $TOOLS}"]
