FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FIX_DATA_DIR=/data
ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN pip install --no-cache-dir uv==0.9.9 && uv sync --frozen --no-dev --no-editable && useradd --create-home --uid 10001 iris && mkdir /data && chown iris:iris /data
USER iris
VOLUME ["/data"]
EXPOSE 8000
CMD ["iris-code-fix-agent"]
