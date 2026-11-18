FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /workspace

COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests
COPY config ./config
COPY .github/workflows/ci.yml ./.github/workflows/ci.yml

RUN python -m pip install --no-cache-dir '.[dev]' \
    && useradd --uid 10001 --create-home runner \
    && chown -R runner:runner /workspace

USER 10001:10001
CMD ["python", "-m", "pytest", "-q"]
