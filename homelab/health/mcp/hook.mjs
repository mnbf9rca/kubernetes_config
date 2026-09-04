// Registers one extra tool on the upstream InfluxDB MCP server, without a fork.
//
// `influxdb-mcp-server` 0.2.0 runs its source directly and exports nothing, so
// there is no module to import, subclass or wrap from a separate entry point.
// Node's `--import` runs this before the package's own module graph loads, so
// patching the prototype here is in place by the time the package constructs
// its server at module scope.
//
// The patch is on `connect` rather than on the constructor because the server
// is built in two different places upstream: one module-scope instance for
// stdio, and a fresh instance per session in HTTP mode. Both reach `connect`.
//
// THE GUIDE IS NOT IN THIS IMAGE. It arrives as a ConfigMap mount and is read
// FROM DISK ON EVERY CALL, which buys three things: a guide edit needs no image
// rebuild, the ConfigMap's content-hash suffix rolls the Deployment so the
// mounted copy is never stale, and a wrong GUIDE_PATH fails one tool call with
// a readable error instead of wedging the server at boot.
import { readFile } from "node:fs/promises";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

const GUIDE_PATH = process.env.GUIDE_PATH || "/guide/health-data-guide.md";

const DESCRIPTION =
  "Read this FIRST, before writing any Flux against this InfluxDB. Returns " +
  "the guide to this instance: the buckets and the organization, the withings " +
  "measurement's tags and its whole field vocabulary with units, the query " +
  "idioms, how the sibling buckets differ, and the mistakes that return a " +
  "wrong answer rather than an error.";

const connect = McpServer.prototype.connect;

McpServer.prototype.connect = function (...args) {
  // In HTTP mode upstream builds one server per session, so this runs per
  // session; the flag only guards a second connect on the same instance.
  if (!this.__healthGuideRegistered) {
    this.__healthGuideRegistered = true;
    this.tool("how-to-use-health-data", DESCRIPTION, async () => ({
      content: [{ type: "text", text: await readFile(GUIDE_PATH, "utf8") }],
    }));
  }
  return connect.apply(this, args);
};
