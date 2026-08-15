# syntax=docker/dockerfile:1
FROM python:3.13-slim

# git is needed to install the private alarmdecoder pin.  Authenticate the
# build with a BuildKit secret:
#   docker build --secret id=git_token,src=.git-token .
# or hand the wheel in from the GitHub release and pip install it instead.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=secret,id=git_token \
    set -e; \
    if [ -s /run/secrets/git_token ]; then \
        git config --global \
            url."https://x-access-token:$(cat /run/secrets/git_token)@github.com/".insteadOf \
            "https://github.com/"; \
    fi; \
    pip install --no-cache-dir -r requirements.txt; \
    rm -f /root/.gitconfig

COPY ad_mqtt ./ad_mqtt/
COPY run.py ./

# By default, docker instances log only to screen.
ENV ADMQTT_LOG_SCREEN=true
ENV ADMQTT_LOG_FILE=
# Mount the zone config at /etc/ad-mqtt/zones.yaml (see zones.yaml.example).
ENV ADMQTT_DEVICES_FILE=/etc/ad-mqtt/zones.yaml
# Liveness heartbeat backing the container health check (handoff G14).
ENV ADMQTT_HEARTBEAT_FILE=/tmp/ad-mqtt-heartbeat

RUN useradd --system --uid 1000 admqtt
USER admqtt

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD \
    python -c "import os, sys, time; p = os.environ['ADMQTT_HEARTBEAT_FILE']; \
sys.exit(0 if os.path.exists(p) and time.time() - os.path.getmtime(p) < 90 else 1)"

CMD ["python", "/app/run.py"]
