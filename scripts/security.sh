#!/usr/bin/env sh
set -eu
pip-audit
bandit -r src
