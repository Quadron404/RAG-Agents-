# The sandbox image: the separate browser, the gateway and the shell.
#
# Built by codespace/restricted-sandbox.sh from the repository root:
#
#   docker build -f codespace/restricted.Dockerfile -t rag-restricted:latest .
#
# selenium/standalone-chrome already carries Chrome and chromedriver, which is
# the fiddly part to install, so this only adds Python and the two libraries the
# restricted services import.  The application code is copied in, so the exact
# policy, guard and gateway a deployment runs are the ones in the checkout.
FROM selenium/standalone-chrome:latest

USER root

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/rag
COPY backend /opt/rag/backend
RUN pip3 install --no-cache-dir --break-system-packages -r /opt/rag/backend/requirements-restricted.txt

# PYTHONPATH so `python -m app.restricted.<service>` resolves, and no bytecode so
# the read-only-ish image stays predictable.
ENV PYTHONPATH=/opt/rag/backend \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python3", "-m", "app.restricted.gateway", "--host", "0.0.0.0", "--port", "9111"]
