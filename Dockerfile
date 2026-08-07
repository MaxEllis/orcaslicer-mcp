# Lets MCP directory crawlers (e.g. Glama) build and run the server to enumerate
# its tools for a quality score. The server starts and answers tools/list with NO
# token; ORCA_API_TOKEN is only needed at runtime to reach a live OrcaSlicer, so
# introspection works in a bare container.
FROM python:3.12-slim
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir .
ENTRYPOINT ["orcaslicer-mcp"]
