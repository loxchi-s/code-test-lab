FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /workspace

COPY pyproject.toml README.md Dockerfile ./
COPY src ./src
COPY tests ./tests
COPY config ./config
COPY .github/workflows/ci.yml ./.github/workflows/ci.yml
COPY .github/workflows/workflow-security.yml ./.github/workflows/workflow-security.yml
COPY .github/workflows/python-repository-sandbox.yml ./.github/workflows/python-repository-sandbox.yml

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --upgrade 'pip==26.3' \
    && python -m pip install --no-cache-dir '.[dev]' \
    && pytest_version="$(python -I -c 'import importlib.metadata as metadata; print(metadata.version("pytest"))')" \
    && python -m venv --without-pip /opt/trusted-runner \
    && python -m pip install --no-cache-dir --target /opt/trusted-runner/lib/python3.12/site-packages "pytest==${pytest_version}" \
    && useradd --uid 10001 --create-home runner \
    && chown -R runner:runner /workspace

COPY src/code_test_lab/trusted_test_runner.py /opt/trusted-runner/trusted_test_runner.py

USER 10001:10001
CMD ["python", "-m", "pytest", "-q"]
