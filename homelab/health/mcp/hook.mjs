// Registers health tools on the upstream InfluxDB MCP server, without a fork.
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
import { z } from "zod";

const GUIDE_PATH = process.env.GUIDE_PATH || "/guide/health-data-guide.md";

const DESCRIPTION =
  "Read this FIRST, before writing any Flux against this InfluxDB. Returns " +
  "the guide to this instance: the buckets and the organization, the withings " +
  "measurement's tags and its whole field vocabulary with units, the query " +
  "idioms, how the sibling buckets differ, and the mistakes that return a " +
  "wrong answer rather than an error.";

const connect = McpServer.prototype.connect;
const error = (text) => ({ isError: true, content: [{ type: "text", text }] });

function hasLocalGroup(csv, grpid) {
  if (!csv.trim()) return false;
  let columns;
  let found = false;
  for (const line of csv.split(/\r?\n/)) {
    if (!line || line.startsWith("#")) continue;
    const cells = line.split(",");
    if (cells[0] === "" && cells[1] === "result") {
      columns = cells;
      if (!columns.includes("grpid") || !columns.includes("_time")) throw new Error("missing column");
      continue;
    }
    if (!columns || cells.length !== columns.length || cells[0] !== "" ||
        cells[columns.indexOf("grpid")] !== grpid ||
        Number.isNaN(Date.parse(cells[columns.indexOf("_time")]))) {
      throw new Error("malformed query result");
    }
    found = true;
  }
  if (!columns) throw new Error("missing CSV header");
  return found;
}

async function influxPost(path, token, body, contentType) {
  const url = new URL(path, process.env.INFLUXDB_URL);
  url.searchParams.set("org", process.env.INFLUXDB_ORG);
  if (path === "/api/v2/write") url.searchParams.set("bucket", "withings_flags");
  const response = await fetch(url, {
    method: "POST",
    headers: { Authorization: `Token ${token}`, "Content-Type": contentType,
               Accept: "application/csv" },
    body,
    signal: AbortSignal.timeout(30000),
  });
  if (!response.ok) throw new Error("Influx request failed");
  return response;
}

McpServer.prototype.connect = function (...args) {
  // In HTTP mode upstream builds one server per session, so this runs per
  // session; the flag only guards a second connect on the same instance.
  if (!this.__healthToolsRegistered) {
    this.__healthToolsRegistered = true;
    this.tool("how-to-use-health-data", DESCRIPTION, async () => ({
      content: [{ type: "text", text: await readFile(GUIDE_PATH, "utf8") }],
    }));
    this.tool("flag-suspect-withings-group",
      "Flag a locally stored Withings group that looks wrong. Ingest may also remove absent, bracketed siblings at the same time and device.",
      { grpid: z.string() }, async ({ grpid }) => {
        if (typeof grpid !== "string" || grpid.length > 20 ||
            !/^[1-9][0-9]*$/.test(grpid) || BigInt(grpid) > 18446744073709551615n) {
          return error("invalid grpid");
        }
        const flux = `from(bucket:"withings")\n` +
          `  |> range(start: 1970-01-01T00:00:00Z, stop: 2262-04-11T23:47:16Z)\n` +
          `  |> filter(fn: (r) => r._measurement == "withings_measure_group" and r.grpid == "${grpid}")\n` +
          `  |> keep(columns: ["grpid", "_time"])\n` +
          `  |> limit(n: 1)\n`;
        let exists;
        try {
          const response = await influxPost("/api/v2/query", process.env.INFLUXDB_TOKEN,
            flux, "application/vnd.flux");
          exists = hasLocalGroup(await response.text(), grpid);
        } catch {
          return error("Could not check the local group; no flag was written.");
        }
        if (!exists) return error("Group not found locally; no flag was written.");
        try {
          await influxPost("/api/v2/write", process.env.INFLUX_FLAGS_TOKEN,
            `withings_suspect,grpid=${grpid} status="pending"`, "text/plain; charset=utf-8");
        } catch {
          return error("Could not confirm the flag write; retry is safe.");
        }
        return { content: [{ type: "text", text:
          "Queued for the next scheduled Withings ingest, normally within 15 minutes. " +
          "The group remains visible. It stays pending while Withings still returns it; " +
          "the operator must correct the source. Absent, bracketed siblings at the same " +
          "time and device may be removed with it." }] };
      });
  }
  return connect.apply(this, args);
};
