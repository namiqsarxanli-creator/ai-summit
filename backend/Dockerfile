# FACEMARK backend — Python 3.12 container runtime.
#
# The host machine may have a different Python version installed (see
# ARCHITECTURE.md §4: "Host has 3.14; the container is authoritative").
# This image is the only place Python 3.12 needs to exist.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Dependencies are installed into a venv at /opt/venv rather than the
# system site-packages, and /opt/venv sits OUTSIDE /app. This matters
# for local development: docker-compose.yml bind-mounts the host
# ./backend directory onto /app so code edits are picked up without a
# rebuild. If dependencies lived under /app they would be shadowed by
# that bind mount the instant it was attached.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Install dependencies first (including dev extras — this image is
# used for both serving and running tests/lint/type-checks in this
# phase; a separate slim production target is a later concern, not
# M01 scope) so this layer caches across source-only changes.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install -e ".[dev]"

COPY . .

EXPOSE 8000

# Single worker is an ARCHITECTURE.md invariant (#17: "The backend runs
# exactly one worker until EventBus is backed by a shared transport"),
# not a default to be tuned away — it belongs in the image command, not
# in operator memory.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
