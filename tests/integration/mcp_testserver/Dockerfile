FROM python:3.12-slim
ARG MCP_VERSION=1.30.0
RUN pip install --no-cache-dir "mcp==${MCP_VERSION}"
COPY server.py /srv/server.py
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/srv/server.py"]
