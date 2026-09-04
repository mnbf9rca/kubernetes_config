# ghcr.io/mnbf9rca/influxdb-mcp-server — upstream 0.2.0 plus one tool.
#
# There is no fork. hook.mjs patches the server's connect step; the upstream
# package is unmodified and unpatched, and is installed from npm by digest
# through the committed lockfile.
#
# amd64 only: both clusters are amd64, and a second architecture is a build
# nobody runs.
FROM node:lts-alpine@sha256:e67514e5d0f6c46656005e1b693b2ec9d52e80b641307de684d4a015ba7a4eaf

WORKDIR /app

# npm ci, NOT npm install: it installs exactly the lockfile's tree and fails if
# package.json and the lockfile disagree. The exact 0.2.0 pin fixes one package;
# the lockfile is what fixes @modelcontextprotocol/sdk, which the upstream
# package declares by caret range and which hook.mjs patches.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY hook.mjs ./

# The guide is deliberately NOT copied in. It arrives as a ConfigMap mount at
# /guide, so guidance edits and image builds move independently.
ENV GUIDE_PATH=/guide/health-data-guide.md

# The upstream bin, from its own package.json `bin` field: src/index.js. Node
# ignores the file's shebang. Deployment `args` append to this.
ENTRYPOINT ["node", "--import", "/app/hook.mjs", \
            "/app/node_modules/influxdb-mcp-server/src/index.js"]
