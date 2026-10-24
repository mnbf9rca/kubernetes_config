# Hal verdict email

`hal-email-verdict` registers the Hal-only outbound platform `hal_email_verdict`.
It reuses Hermes's native email configuration, scoped credentials, Message-ID creation, threading, TLS and SMTP.
Connecting this alias starts no inbox poller or connection probe.
The [daily-check runbook](../../../docs/operations/hal-daily-check.md) describes its callers, receipts and reply actions.

## Envelope and verdict subject

The morning runner and assessment publisher use the native send command:

```sh
hermes -p hal send --to hal_email_verdict:<existing-recipient> --file - --json
```

Standard input is JSON with exactly three fields:

```json
{"schema":"hal.email-verdict.v1","subject":"House: 1 thing needs you","body":"## Needs you\n- **Lamp**: check its power."}
```

`parse_envelope` rejects duplicate or extra fields, an incorrect schema and an empty body.
Subjects must begin with `House: ` followed by a verdict, with no control characters, surrounding whitespace or more than 200 characters.
An authorized morning trial may prepend exactly one `[trial] `.
The body cannot contain NUL, `MEDIA:` or `[[as_document]]`; standalone sends also reject thread, media and document options.
The plugin adds no body truncation or message splitting.

## Plain text and HTML

`VerdictAdapter._new_reply` parses the envelope, then calls the native `_new_reply` hook to create the message and its plain-text part.
It replaces the native subject with the verdict subject, sets `multipart/alternative`, and appends the HTML part after the plain-text part.
Both parts come from the same body.
SMTP submission remains in the native adapter.

`render_html` supports `##` headings, `- ` bullets, two-space continuation lines, inline `**bold**` and standalone HTTP or HTTPS links.
Other non-empty lines become paragraphs.
It escapes source HTML and link attributes; a non-HTTP link remains text.
This is a small renderer for the composed emails, without a template engine or general Markdown parser.

## Subject and receipt binding

The expected subject is stored on each MIME message, keeping concurrent sends independent.
Immediately before SMTP, `_smtp_send` requires exactly one Subject header equal to that message's bound verdict.
The inherited send path returns the native Message-ID belonging to that MIME message.

`standalone_send` returns success, platform, recipient and Message-ID after native send success, or an error on failure.
The host `NativeMail` caller additionally requires the expected platform and configured recipient, and refuses skipped or partial-success results.
The plugin does not keep a receipt store or retry a send itself.
The morning runner and batch publisher own their saved receipts and retry behavior; SMTP acceptance does not prove inbox delivery.

## Incoming replies

Replies arrive through Hal's ordinary native `email` platform, not this outbound alias.
That platform applies Hal's authenticated operator sender allowlist and preserves Message-ID and In-Reply-To.
The daily-check plugin binds reply actions to the admitted operator message; the quoted original is context, not a new instruction.
Hal's guide and persona define the reply behavior.

## Installation and checks

The installed directory is `/home/hermes/.hermes/profiles/hal/plugins/hal-email-verdict/`, outside the Hermes checkout.
Hal enables `hal-email-verdict` in `plugins.enabled` and `platforms.hal_email_verdict.enabled`, alongside its enabled native `platforms.email` configuration.
No separate mail credential is needed.
The plugin refuses other profile homes and incompatible native adapter methods or target parsers.

Run the current tests from the repository root:

```sh
python3 -m unittest discover -s hermes-vm/plugins/hal-email-verdict -p 'test_*.py'
```

The parser and MIME tests run locally without sending mail.
The real-native-adapter MIME test runs only where `/home/hermes/.hermes/hermes-agent` is available; it also makes no SMTP connection.
