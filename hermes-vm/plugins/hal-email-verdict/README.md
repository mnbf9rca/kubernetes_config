# Hal verdict email

A Hal-only outbound platform named `hal_email_verdict`. It reuses the bundled
email adapter's configuration, scoped secret lookup, MIME, TLS and SMTP. The
normal `email` platform remains unchanged. Connecting this alias starts no
IMAP listener, polling task or SMTP probe.

Install this directory at
`/home/hermes/.hermes/profiles/hal/plugins/hal-email-verdict/`. Append
`hal-email-verdict` to Hal's existing `plugins.enabled` list and add
`platforms.hal_email_verdict.enabled: true`; preserve the existing email
configuration and all other plugin entries. The owning profile's native
`platforms.email` configuration must be enabled. No new secret is needed.

The daily mailer uses the native command:

```
hermes -p hal send --to hal_email_verdict:<existing-recipient> --file - --json
```

Standard input is a JSON object with exactly `schema`, `subject` and `body`;
`schema` is `hal.email-verdict.v1`. The subject starts with `House: ` and has
no control characters. The envelope is consumed before native MIME creation.
A per-message subject binding is checked immediately before native SMTP.
Absent plugins, bad envelopes and missing native adapter methods fail before
submission. Errors do not create a fallback send or an application retry.

The installed copy lives outside the Hermes checkout changed by Update Now.
Its native adapter and email target-parser compatibility checks run automatically; do not patch the
checkout or add a manual post-upgrade maintenance step.

Run the tests with a Hermes source tree (the adapter, `gateway/config.py` and
`tools/send_message_tool.py` must be from the same checkout):

```
HERMES_EMAIL_ADAPTER_SOURCE=/path/to/hermes-agent/plugins/platforms/email/adapter.py \
  python3 -B test_verdict.py
```

The tests execute the native target resolver, send_message_tool entry point,
MIME/SMTP and routing functions locally with
framework imports, credentials and network stubbed. They do not contact a mail
server or prove SMTP/inbox delivery. The first scheduled mail is the live
acceptance run; do not send a test email during deployment.
