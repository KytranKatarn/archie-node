FROM python:3.11-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl jq sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Python deps
RUN pip install --no-cache-dir flask requests psutil

WORKDIR /app

# Copy node code
COPY entrypoint.sh node_agent.py ./
RUN chmod +x entrypoint.sh

# Data directory (SQLite, config)
RUN mkdir -p /data
VOLUME ["/data"]

ENV HUB_URL=""
ENV TOKEN=""
ENV NODE_NAME=""
ENV OLLAMA_HOST="http://host.docker.internal:11434"

EXPOSE 3001

ENTRYPOINT ["./entrypoint.sh"]
