# Development and CI image for Rewire itself.
#
# This is NOT the sandbox image. Repository code under migration is executed in
# a separate, locked-down container (see REWIRE_SANDBOX__IMAGE); this image runs
# Rewire's own toolchain and needs the Docker CLI to drive that sandbox.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git: branch/diff handling. ripgrep: fallback search backend.
# docker-cli: talks to the host daemon via a mounted socket to launch sandboxes.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git ripgrep docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy metadata first so the dependency layer is cached independently of source.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install -e ".[dev]"

COPY . .

# Rewire runs untrusted repository code, but only ever inside the sandbox
# container. Its own process still drops root as a matter of hygiene.
RUN useradd --create-home --uid 1000 rewire && chown -R rewire:rewire /app
USER rewire

ENTRYPOINT ["rewire"]
CMD ["doctor"]
