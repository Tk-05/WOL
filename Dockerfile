FROM python:3.12-slim

# iputils-ping: wol_daemon/status.py checks a machine's state via ICMP ping.
# openssh-client: wol_daemon/ssh_shutdown.py shells out to `ssh` for SSH-based shutdown.
# Using the system ssh client instead of a Python SSH library (e.g. paramiko) avoids
# depending on prebuilt wheels for cryptography's native extension on 32-bit ARM (Pi).
# tzdata: lets the TZ variable from docker-compose.yml take effect. Without it the container
# silently runs in UTC and every schedule fires hours off. noninteractive keeps tzdata's
# install from waiting for a region prompt during the build.
# Runs as root in the container so ping works without extra capability handling.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       iputils-ping openssh-client tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wol_daemon ./wol_daemon

EXPOSE 9090
VOLUME ["/config"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:9090/', timeout=3).status==200 else 1)"

CMD ["python", "-m", "wol_daemon.daemon", "/config/config.yaml"]
