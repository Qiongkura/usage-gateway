FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONIOENCODING=utf-8

# 先装依赖（利用层缓存）
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
COPY web ./web
COPY pricing.yaml zen_prices.json ./
COPY config.example.yaml ./config.example.yaml

# 首次启动没有 config.yaml 时自动用示例配置
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 9000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD ["python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:9000/api/ready', timeout=4)"]

ENTRYPOINT ["docker-entrypoint.sh"]
