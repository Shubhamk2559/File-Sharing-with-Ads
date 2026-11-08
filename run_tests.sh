#!/usr/bin/env sh
# Python (unittest) + JS (node --test). Real-FastAPI test runs automatically when fastapi+httpx are installed.
set -e
python3 -m unittest discover -s tests -t . -v 2>&1 | grep -v '^{"ev"'
node --test tests/*.test.js
