# First-session checklist

Three one-off items.
Work them at the start of the first `/update-estate` session, before Step 1 of the main runbook, then **delete this file** in the session's commit.
`SKILL.md` tests for its existence, so deleting it is how the checklist retires.

Some items may already be satisfied.
Each one starts with an assertion: run it, and act only when it fails.
Report the outcome of all three to the operator before moving on.

## 1. Omni automatic etcd backups

**Assert:** both clusters have automatic backups enabled with a non-zero interval, and the last backup is recent.

```bash
omnictl get etcdbackupoverallstatus -o yaml
omnictl get etcdbackupstatus -o yaml
```

Expect `configurationname: s3` and an empty `configurationerror`.

`lastbackuptime.seconds` is raw Unix seconds, so convert it before you judge its age:

```bash
omnictl get etcdbackupstatus -o json | jq -r '"\(.metadata.id) \(.spec.lastbackuptime.seconds | todate)"'
```

Expect a timestamp inside the last day for each cluster.

**Never run `omnictl get etcdbackups3configs`** — it prints the storage access key and secret in plaintext.

**If backups are disabled or the interval is `0`:** enable them — [estate-updates.md](../../../docs/operations/estate-updates.md#omni-etcd-backups) carries both mechanisms, for a cluster template and for a raw resource.

Confirmed enabled at a 1-hour interval on both clusters on August 26, 2026, so expect this item to pass.

## 2. The FreshRSS `security` category

**Assert:** FreshRSS has a category named `security` holding the five advisory feeds.

The user is `ruined0346` — the single account on this instance, recorded in [vps.md](../../../docs/operations/vps.md).
It is an ordinary identifier, so you do not need to run `list-users.php` to find it.

```bash
kubectl --context cynexia-vps -n vps exec deployment/freshrss -c freshrss -- \
  php /var/www/FreshRSS/cli/export-opml-for-user.php --user ruined0346 | grep -i security
```

**If the category is absent:** import it.
FreshRSS has no add-a-feed CLI script, so an OPML import is the deterministic path.
Write this file into **this session's scratchpad directory** rather than `/tmp` on the operator's machine, and call that path `$OPML` in the commands below:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>security</title></head>
  <body>
    <outline text="security" title="security">
      <outline type="rss" text="Kubernetes CVE feed" title="Kubernetes CVE feed"
        xmlUrl="https://kubernetes.io/docs/reference/issues-security/official-cve-feed/feed.xml"
        htmlUrl="https://kubernetes.io/docs/reference/issues-security/official-cve-feed/"/>
      <outline type="rss" text="Talos Linux releases" title="Talos Linux releases"
        xmlUrl="https://github.com/siderolabs/talos/releases.atom"
        htmlUrl="https://github.com/siderolabs/talos/releases"/>
      <outline type="rss" text="cert-manager releases" title="cert-manager releases"
        xmlUrl="https://github.com/cert-manager/cert-manager/releases.atom"
        htmlUrl="https://github.com/cert-manager/cert-manager/releases"/>
      <outline type="rss" text="Traefik releases" title="Traefik releases"
        xmlUrl="https://github.com/traefik/traefik/releases.atom"
        htmlUrl="https://github.com/traefik/traefik/releases"/>
      <outline type="rss" text="cloudflared releases" title="cloudflared releases"
        xmlUrl="https://github.com/cloudflare/cloudflared/releases.atom"
        htmlUrl="https://github.com/cloudflare/cloudflared/releases"/>
    </outline>
  </body>
</opml>
```

Copy it in, import it and clean up in **one** `exec`.
`freshrss` is a keel `:latest` Deployment with `strategy: Recreate`, so a poll-triggered restart between two separate `exec` calls loses the file from the pod's `/tmp`:

```bash
kubectl --context cynexia-vps -n vps exec -i deployment/freshrss -c freshrss -- \
  sh -c 'cat > /tmp/security-feeds.opml \
    && php /var/www/FreshRSS/cli/import-for-user.php --user ruined0346 \
         --filename /tmp/security-feeds.opml; \
    rc=$?; rm -f /tmp/security-feeds.opml; exit $rc' < "$OPML"
```

**Verify each feed actually fetches**, which an import does not prove.
Do this in the web interface: open `https://rss.cynexia.com`, select the `security` category, and confirm all five feeds carry articles.
A feed with zero articles or an error marker did not fetch — fix it before ticking this item.

**Do not run `cli/actualize-user.php` to check this.**
It refreshes every feed on the account and prints a full feed URL in its per-domain retry warnings.
One subscription on this instance carries an API key inside its URL, logged in `secrets-to-rotate.md` on August 20, 2026 and still unrotated, so running that script re-discloses a known secret into your transcript and obliges you to stop and file another honesty-box row.
The web interface proves the same thing and discloses nothing.

The feed URLs and what each one really is are in [estate-updates.md](../../../docs/operations/estate-updates.md#advisory-feeds).
Four of the five are release feeds, not advisory feeds.
That is deliberate and documented; do not "improve" it by hunting for advisory feeds that do not exist.

## 3. The `estate-update` healthchecks.io check

**Assert:** the ping UUID resolves.

```bash
op read 'op://Homelab/estate-update/healthcheck-uuid' >/dev/null && echo "resolves"
```

It should resolve: the update-machinery build item created this check and stored its UUID before this skill was written.

**If it does not resolve**, stop and ask the operator to create the check and store its UUID at that path.
Do not create the check yourself, and do not create a second one: healthchecks.io is capped at 20 checks, `estate-update` took the last slot on August 26, 2026, and a duplicate is a check nobody pings.

Do not paste the UUID into this session, into a commit, or into any file.
It is a spam-target identifier: holding one lets a stranger mask a real failure, which is why it stays out of this public repository.
It needs no honesty-box row if it does appear in a transcript.

**Then confirm the ping path works end to end**, using the session's closing ping in Step 6 of `SKILL.md`.
A first ping that returns HTTP 200 is the proof.

## Retire this file

When all three items pass, delete this file and commit it with the session's other work:

```bash
git rm .claude/skills/update-estate/first-session.md
```

`SKILL.md` Step 0 checks for this path.
Once it is gone, later sessions skip the checklist.
