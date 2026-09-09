FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml README.md LICENSE CHANGELOG.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/phalkmin/trendzeist-mcp" \
      org.opencontainers.image.description="Google Trends MCP server for content ideation" \
      org.opencontainers.image.licenses="MIT"
COPY --from=builder /install /usr/local
RUN useradd --create-home --uid 1000 mcp
USER mcp
ENV TRENDZEIST_CACHE_DIR=/home/mcp/.cache/trendzeist-mcp \
    PYTHONUNBUFFERED=1
# stdio transport: the MCP client attaches to stdin/stdout.
ENTRYPOINT ["trendzeist-mcp"]
