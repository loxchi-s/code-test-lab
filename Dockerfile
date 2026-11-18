FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /workspace

COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests
COPY config ./config
COPY .github/workflows/ci.yml ./.github/workflows/ci.yml
COPY .github/workflows/workflow-security.yml ./.github/workflows/workflow-security.yml
COPY .github/workflows/python-repository-sandbox.yml ./.github/workflows/python-repository-sandbox.yml

RUN python -m pip install --no-cache-dir --upgrade 'pip==26.3' \
    && python -m pip install --no-cache-dir '.[dev]' \
    && useradd --uid 10001 --create-home runner \
    && chown -R runner:runner /workspace

USER 10001:10001
CMD ["python", "-m", "pytest", "-q"]
