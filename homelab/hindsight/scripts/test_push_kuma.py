"""Exercise retry, redaction and nonfatal delivery in both real push helpers."""
from pathlib import Path
import re
import subprocess

for name in ("hindsight-pg-dump.sh", "hindsight-canary.sh"):
    source = Path(__file__).with_name(name).read_text()
    helper = re.search(r"(?m)^push_kuma\(\) \{.*?^\}", source, re.S).group()
    for failures, attempts, pauses, warning in ((0, 1, 0, ""), (1, 2, 1, ""),
            (2, 2, 1, "kuma: push not delivered\n")):
        for status in ("up", "down"):
            # Replace only external delivery and the delay: no real push or wait.
            script = r"""
set -eu
MSG_FILE=/dev/null
PUSH_URL=https://invalid.example/secret-token
calls=0
pauses=0
reset=0
request() {
  calls=$((calls + 1))
  echo "$PUSH_URL" >&2
  [ "$calls" -gt "$1" ]
}
wget() { request "$failures"; }
curl() { request "$failures"; }
sleep() { pauses=$((pauses + 1)); }
msg_reset() { reset=1; }
"""
            result = subprocess.run(["sh", "-c", script + helper +
                f"\nfailures={failures}\npush_kuma {status}\n" +
                'printf "%s %s %s\\n" "$calls" "$pauses" "$reset"'],
                capture_output=True, text=True, check=True)
            assert result.stdout == f"{attempts} {pauses} 1\n", (name, failures, result.stdout)
            assert result.stderr == warning, (name, failures, result.stderr)
print("OK: both push helpers retry once, redact diagnostics and preserve success")
