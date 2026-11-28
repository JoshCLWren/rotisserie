# Linux test image that matches the CI runners (ubuntu-latest). Build and run it
# with scripts/test-in-docker; the repository is mounted, not copied.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    bash ca-certificates coreutils curl gh git jq nodejs procps python3 ripgrep shellcheck tzdata util-linux \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /uvx /usr/local/bin/
RUN useradd --create-home --uid 1001 runner
USER runner
ENV UV_PYTHON_INSTALL_DIR=/home/runner/.python \
    UV_CACHE_DIR=/home/runner/.cache/uv \
    UV_LINK_MODE=copy
RUN uv python install 3.14 \
 && git config --global user.name "Rotisserie CI" \
 && git config --global user.email "rotisserie-ci@users.noreply.github.com" \
 && git config --global --add safe.directory '*' \
 && mkdir -p /home/runner/.cache/uv /home/runner/venvs/root /home/runner/venvs/factory
WORKDIR /work
