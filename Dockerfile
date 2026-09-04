FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# git, Node and esbuild are the MCP translation toolchain: /mcp/translate
# clones a server repository, installs its dependencies and bundles it into one
# JavaScript file the phone can evaluate. Without them that endpoint answers
# ToolchainUnavailable rather than failing obscurely, but the feature is off.
#
# esbuild is pinned and installed globally so the bundler never falls back to
# `npx`, which would download a compiler at request time — on a machine that is
# about to run it against a stranger's repository.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git nodejs npm ca-certificates \
    && npm install -g esbuild@0.25.0 \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY sql ./sql

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
