# Agent email: per-agent mailboxes for Hermes

Each Hermes agent has its own mailbox at `cynexia.io`, hosted at
Purelymail. Live since August 23, 2026 for all three agents:
`kairos@cynexia.io` (the `default` profile), `emh@cynexia.io`, and
`hal@cynexia.io`. Agents send and receive through MCP tools; they can
discover and email each other and external parties.

There is no custom code. The system is three pieces of configuration:

1. **Purelymail** hosts the mailboxes — one user per agent. Isolation is
   enforced by the mail host's own logins: an agent's credential reaches
   one mailbox and no other.
2. **[mcp-email-server](https://github.com/wh1isper/mcp-email-server)**
   (BSD-3, pinned at 1.4.2) runs as a stdio MCP server inside each
   Hermes profile, launched with `pipx run`, configured entirely through
   environment variables carrying only that agent's credentials.
3. **The `cynexia.io` Cloudflare zone** carries only DNS records
   pointing at Purelymail. No Email Routing, no Workers.

The full design history (a superseded Cloudflare-native v1 design, the
build-vs-buy survey, and the v2 spec this doc condenses) lives in the
local-only repo `~/git/agent-mail`, kept as an archive.

## DNS reference (cynexia.io)

All records DNS-only (gray cloud). An accidental proxy (orange-cloud)
flip breaks mail silently — nothing monitors for it.

| Record | Value |
|---|---|
| MX `cynexia.io` | `mailserver.purelymail.com`, priority 10 |
| TXT `cynexia.io` | `v=spf1 include:_spf.purelymail.com ~all` and the `purelymail_ownership_proof=…` TXT |
| CNAME `purelymail1._domainkey` … `purelymail3._domainkey` | `key1.dkimroot.purelymail.com` … `key3.dkimroot.purelymail.com` |
| CNAME `_dmarc` | `dmarcroot.purelymail.com` |

Purelymail endpoints: IMAP `imap.purelymail.com:993` (SSL), SMTP
`smtp.purelymail.com:465` (SSL), webmail `inbox.purelymail.com`.

## Credentials

One Login item per agent in the **hermes** 1Password vault, named
`purelymail-<agent>`, with `username` (text, the address) and `password`
(concealed). References read `op://hermes/purelymail-<agent>/username`
and `…/password`. Nothing outside the hermes VM needs these credentials;
the VM's own 1Password integration (`hermes secrets op`) resolves them
at process startup, so profile configs hold only `${VAR}` placeholders.

The agent holds the account password, which also grants webmail
self-service (password change, recovery-method management). The
documented hardening step, verified working but not deployed: give the
agent a Purelymail **app password** instead — full IMAP/SMTP access, no
portal access — by adding the field to the item and remapping
`MAIL_PASSWORD`. Worth doing if an agent's exposure to hostile external
mail grows.

The Purelymail account (admin) credential and its API key stay out of
every agent profile.

## Provisioning a new agent

1. Create the Purelymail user in the console (or `POST
   /api/v0/createUser` with the API key; header `Purelymail-Api-Token`).
   Disable password reset for the user and send no welcome email.
2. Create the 1Password item `purelymail-<agent>` (`username`,
   `password`).
3. If the profile was cloned from another (`hermes profile create
   --clone`), audit the inherited `mcp_servers` block and `.env` and
   strip anything the new agent must not hold — in particular an
   inherited `mail` entry.
4. Map the secrets and add the MCP server. Secret mappings are
   **per-profile** (verified), so every profile uses the same two names:

   ```sh
   hermes -p <profile> secrets op set MAIL_USERNAME "op://hermes/purelymail-<agent>/username"
   hermes -p <profile> secrets op set MAIL_PASSWORD "op://hermes/purelymail-<agent>/password"

   echo Y | hermes -p <profile> mcp add mail \
     --command pipx --connect-timeout 90 \
     --env 'MCP_EMAIL_SERVER_EMAIL_ADDRESS=${MAIL_USERNAME}' \
           'MCP_EMAIL_SERVER_PASSWORD=${MAIL_PASSWORD}' \
           MCP_EMAIL_SERVER_IMAP_HOST=imap.purelymail.com \
           MCP_EMAIL_SERVER_SMTP_HOST=smtp.purelymail.com \
           MCP_EMAIL_SERVER_ACCOUNT_NAME=<agent> \
           MCP_EMAIL_SERVER_CONFIG_PATH=/home/hermes/.hermes/profiles/<profile>/mcp-email-server/config.toml \
     --args run mcp-email-server==1.4.2 stdio
   ```

   For the `default` profile, drop `-p <profile>` and use
   `/home/hermes/.hermes/mcp-email-server/config.toml` as the config
   path — the default profile lives at the `~/.hermes` root, not under
   `profiles/`.
5. Test: `hermes -p <profile> mcp test mail`, then have the agent send a
   message to its own address and read it back.
6. Confirm the profile's `config.yaml` holds the `${MAIL_PASSWORD}`
   placeholder, not a value.

Decommissioning is a couple of clicks: delete the user in the Purelymail
console, `hermes -p <profile> mcp remove mail`, remove the two secret
mappings, delete the 1Password item. Deleting the user permanently
removes the mailbox contents.

### Rules that must not be broken

- **`MCP_EMAIL_SERVER_CONFIG_PATH` is mandatory, per profile.** The
  server always loads a TOML config at that path and keeps a SQLite
  index beside it. The default path (`~/.config/mcp-email-server/`)
  would be shared by every profile under the single `hermes` Unix user,
  and any account that ever lands in a shared store becomes callable
  from every profile.
- **Never run `mcp-email-server config init`, `config select managed`,
  or `ui` on the VM** without first exporting a throwaway
  `MCP_EMAIL_SERVER_CONFIG_PATH`. Flipping the shared default bootstrap
  into managed mode makes every profile's env-configured account
  silently disappear, fleet-wide.
- **`--args` must be the last `mcp add` option.** Hermes parses it as
  "everything that follows"; an `--env` placed after it lands in the
  child's argv, breaking the server and putting values on a visible
  process command line.
- **Keep the version pinned** (`mcp-email-server==1.4.2` in every
  profile). Bump deliberately, all profiles together.
- Two non-TTY gotchas: pipe `Y` into `mcp add` (it asks "Enable all 14
  tools?" interactively), and pre-warm pipx once per VM
  (`pipx run mcp-email-server==1.4.2 --help`) so the first add does not
  time out on the package download.

## Supervisor access

Webmail at `inbox.purelymail.com`, logging in per mailbox with the
credentials from 1Password. There is no all-mailbox admin pane: the
Purelymail admin console manages users but cannot read their mail.

## Limits and behavior worth knowing

- Purelymail costs $10/year plus trivial usage; users are unlimited.
- External sending is capped at roughly **3,000 messages/day for the
  whole account**, shared by all agents. A runaway agent can exhaust the
  budget and block every other agent's external mail until the cap
  clears. Internal (agent-to-agent) mail also counts against usage but
  not reputation.
- Optional per-agent restrictions exist in mcp-email-server
  (`MCP_EMAIL_SERVER_ALLOWED_SENDERS`, `…_ALLOWED_RECIPIENTS`,
  attachment download off by default). No profile sets them yet; apply
  per agent role.
- Inbound mail is unscanned hostile input to an LLM agent. Purelymail
  spam filtering applies, but there is no prompt-injection scanning
  anywhere in this path — agent SOUL/skill hygiene rules are the only
  defense.

## Monitoring and backup: none, deliberately (for now)

**Nothing monitors this system.** A Purelymail outage, an expired
credential, a wedged mcp-email-server process, a DNS record drift, or
send-cap exhaustion all surface only as tool errors inside agent
sessions. The designed-but-unbuilt detector is a round-trip canary (a
cron on the hermes VM: send from a canary mailbox to itself, poll IMAP,
report the outcome) — see the archived spec before building it. Note that
the archived spec says healthchecks.io; since August 26, 2026 new scheduled
work in this estate drives an uptime-kuma **push** monitor instead, and only
four checks remain at healthchecks.io
([monitoring.md](monitoring.md#healthchecksio-checks)). The hermes VM is
off-cluster, so such a canary would push outbound to `uptime.cynexia.com`
through the same Access bypass the in-cluster jobs use.

**There is no backup.** Purelymail's own durability is the only copy of
agent mail, `delete_email` is permanent, and the hermes VM itself has no
backup either ([homelab.md](homelab.md) records its rebuild-not-restore
posture). Accepted deliberately; revisit if agent mail starts carrying
anything worth keeping.

## Migration path

If privacy or volume ever justifies it: self-hosted Stalwart on the VPS,
an MX flip, and `imapsync` per mailbox. The agent-side configuration
barely changes (new hosts, same env variables). The trade: open SMTP
ports on the VPS and own deliverability.
