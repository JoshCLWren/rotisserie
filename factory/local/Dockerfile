# Build from the factory directory: docker build -f local/Dockerfile -t rotisserie-local-worker .
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends git jq curl ca-certificates python3 python3-venv python3-pip \
    nodejs npm bash coreutils procps xz-utils \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --break-system-packages --quiet uv
COPY .github/scripts/install-opencode-v2.sh /opt/install-opencode-v2.sh
COPY .github/scripts/opencode-factory-quota-retry.mjs /opt/opencode-factory-quota-retry.mjs
RUN RUNNER_TEMP=/opt GITHUB_PATH=/opt/opencode-path HOME=/root bash /opt/install-opencode-v2.sh \
 && ln -s "$(cat /opt/opencode-path)/opencode" /usr/local/bin/opencode \
 && opencode --version
