FROM cgr.dev/chainguard/wolfi-base:latest@sha256:28c39be5a5e1e9b85081c10740e8495f6518a865b23bc6a486637c4b21e74b43

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /workspace

COPY pyproject.toml README.md ./
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
    && printf 'runner:x:10001:10001:CI Runner:/tmp:/sbin/nologin\\n' >> /etc/passwd \
    && printf 'runner:x:10001:\\n' >> /etc/group \
    && python -c "import pwd, grp; assert pwd.getpwuid(10001).pw_name == 'runner'; assert grp.getgrgid(10001).gr_name == 'runner'" \
    && chown -R 10001:10001 /workspace

USER 10001:10001
CMD ["python", "-m", "pytest", "-q"]
