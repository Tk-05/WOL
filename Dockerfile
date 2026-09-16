FROM python:3.12-slim

# iputils-ping: wol_daemon/status.py checks the node's state via ICMP ping.
# Runs as root in the container so this works without extra capability handling.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wol_daemon ./wol_daemon

EXPOSE 8080
VOLUME ["/config"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/', timeout=3).status==200 else 1)"

CMD ["python", "-m", "wol_daemon.daemon", "/config/config.yaml"]
