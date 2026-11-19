FROM cgr.dev/chainguard/wolfi-base:latest

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /workspace

COPY pyproject.toml README.md Dockerfile ./
COPY src ./src
COPY tests ./tests
COPY config ./config
COPY .github/workflows/ci.yml ./.github/workflows/ci.yml
COPY .github/workflows/workflow-security.yml ./.github/workflows/workflow-security.yml
COPY .github/workflows/python-repository-sandbox.yml ./.github/workflows/python-repository-sandbox.yml

RUN apk update \
    && apk upgrade --no-cache \
    && apk add --no-cache python-3.12 py3.12-pip ca-certificates-bundle bash \
    && python -m pip install --no-cache-dir --upgrade 'pip==26.3' \
    && python -m pip install --no-cache-dir '.[dev]' \
    && pytest_version="$(python -I -c 'import importlib.metadata as metadata; print(metadata.version("pytest"))')" \
    && python -m venv --without-pip /opt/trusted-runner \
    && python -m pip install --no-cache-dir --target /opt/trusted-runner/lib/python3.12/site-packages "pytest==${pytest_version}" \
    && chown -R 10001:10001 /workspace

COPY src/code_test_lab/trusted_test_runner.py /opt/trusted-runner/trusted_test_runner.py

USER 10001:10001
CMD ["python", "-m", "pytest", "-q"]
