FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# mcp_client.py is required, not optional: agent.py imports it at module scope
# (specs.md Phase 8), so omitting it fails at uvicorn's import of main:app, not
# at first tool use. observability.py is the same kind of hard dependency --
# main.py, agent.py and mcp_client.py all import it at module scope (specs.md
# Phase 10). Keep this list in sync when adding a backend module: because this
# is an allowlist, a missing module is a container that crash-loops on import
# and a 502 from nginx, not a build error.
COPY main.py agent.py db.py mcp_client.py observability.py ./

EXPOSE 8000

# --proxy-headers/--forwarded-allow-ips=* trust X-Forwarded-For from
# nginx.conf.template's proxy_pass so main.py's per-client-IP rate limiter sees real
# client IPs instead of the nginx container's -- safe only because
# docker-compose.yml exposes this port to the internal network alone (no
# host port mapping), so nginx is the only thing that can ever call it.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
