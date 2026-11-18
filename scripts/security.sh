#!/usr/bin/env sh
set -u

status=0
pip-audit || status=1
bandit -r src || status=1

exit "$status"
