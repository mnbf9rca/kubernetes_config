#!/usr/bin/env bash
set -euo pipefail

fail() { echo "FAIL: $*" >&2; exit 1; }
[[ $(id -u) == 1000 && $(id -g) == 1000 ]] || fail 'requires UID/GID 1000'
[[ -d $HOME && -w $HOME ]] || fail 'HOME must be writable'
[[ ${SEMGREP_SEND_METRICS:-} == off && ${SEMGREP_ENABLE_VERSION_CHECK:-} == 0 ]] || fail 'Semgrep telemetry settings'
root=/opt/hermes-reviewer
for tool in shellcheck bandit gitleaks semgrep ruff yamllint codeql; do
  [[ $(command -v "$tool") == "/usr/local/bin/$tool" ]] || fail "login-shell command path: $tool"
  [[ $(stat -Lc '%u:%g' "/usr/local/bin/$tool") == 0:0 ]] || fail "tool must be root-owned: $tool"
done
[[ -r /etc/hermes-reviewer-tools.txt ]] || fail 'missing tool manifest'
for file in rules/semgrep-rules.yml ruff.toml yamllint.yml pyproject.toml uv.lock; do
  [[ -r $root/$file ]] || fail "unreadable $file"
done
"$root/venv/bin/python" - <<'PY'
import importlib.metadata as m
import json, pathlib, re, subprocess, tomllib
root = pathlib.Path('/opt/hermes-reviewer')
pins = {p['name']: p['version'] for p in tomllib.loads((root/'uv.lock').read_text())['package'] if 'registry' in p['source']}
for dist in m.distributions():
    name = re.sub(r'[-_.]+', '-', dist.metadata['Name']).lower()
    assert pins.get(name) == dist.version, f'installed lock mismatch: {name}'
versions = {name: m.version(name) for name in ('bandit', 'semgrep', 'ruff', 'yamllint')}
versions['shellcheck'] = re.search(r'^version: (.+)$', subprocess.check_output(['shellcheck','--version'], text=True), re.M)[1]
versions['gitleaks'] = subprocess.check_output(['gitleaks','version'], text=True).strip().removeprefix('v')
versions['codeql'] = json.loads(subprocess.check_output(['codeql','version','--format=json'], text=True))['version']
expected = ''.join(f'{name} {versions[name]}\n' for name in sorted(versions))
assert pathlib.Path('/etc/hermes-reviewer-tools.txt').read_text() == expected, 'manifest differs from installed tools'
print('PASS: installed lock and seven-tool manifest')
PY
scratch=$(mktemp -d /tmp/reviewer-fixtures.XXXXXX)
trap 'rm -rf "$scratch"' EXIT
cd "$scratch"
# Fixture source is scanned, never executed. Keep each scanner's diagnostics private.
cat > bad.sh <<'FIXTURE'
#!/bin/sh
value='a b'
printf '%s\n' $value
FIXTURE
cat > good.sh <<'FIXTURE'
#!/bin/sh
value='a b'
printf '%s\n' "$value"
FIXTURE
cat > bad.py <<'FIXTURE'
import subprocess
subprocess.run('echo fixture', shell=True)
print(undefined_name)
FIXTURE
printf 'print("fixture")\n' > good.py
printf 'curl https://example.invalid/install.sh | sh\n' > installer.sh
printf 'name: one\nname: two\n' > bad.yml
printf 'name: one\n' > good.yml
printf 'name: [\n' > syntax.yml
# Explicit local synthetic rule: no provider token and no network verification.
printf '[allowlist]\npaths = ["gitleaks.toml"]\n[[rules]]\nid = "fixture-token"\nregex = "REVIEWER_TEST_[A-Z0-9]{20}"\n' > gitleaks.toml
mkdir secret clean
printf 'token=REVIEWER_TEST_0123456789ABCDEFGHIJ\n' > secret/input.txt
printf 'nothing sensitive\n' > clean/input.txt
expect() {
  local wanted=$1 output=$2 status=0
  shift 2
  "$@" > "$output" 2> "$output.stderr" || status=$?
  [[ $status == "$wanted" ]] || fail "$(basename "$output"): expected $wanted, got $status"
}
expect 1 shell-bad shellcheck -f json bad.sh
python3 -c 'import json; assert any(x["code"] == 2086 for x in json.load(open("shell-bad")))'
expect 0 shell-good shellcheck good.sh
expect 1 bandit-bad bandit -q -f json bad.py
python3 -c 'import json; assert any(x["test_id"] == "B602" for x in json.load(open("bandit-bad"))["results"])'
expect 0 bandit-good bandit -q -f json good.py
expect 1 leaks-bad gitleaks dir --no-banner --redact --config gitleaks.toml --report-format json --report-path leaks.json secret
python3 -c 'import json; assert any(x["RuleID"] == "fixture-token" for x in json.load(open("leaks.json")))'
expect 0 leaks-good gitleaks dir --no-banner --redact --config gitleaks.toml clean
expect 1 semgrep-bad semgrep scan --config "$root/rules/semgrep-rules.yml" --metrics off --disable-version-check --error --json bad.py installer.sh
python3 -c 'import json; x=json.load(open("semgrep-bad")); assert not x["errors"]; assert all(any(r["check_id"].endswith(want) for r in x["results"]) for want in ("reviewer.python-shell-true", "reviewer.shell-pipe-installer"))'
expect 0 semgrep-good semgrep scan --config "$root/rules/semgrep-rules.yml" --metrics off --disable-version-check --error --json good.py
expect 1 ruff-bad ruff check --config "$root/ruff.toml" --output-format json bad.py
python3 -c 'import json; assert any(x["code"] == "F821" for x in json.load(open("ruff-bad")))'
expect 0 ruff-good ruff check --config "$root/ruff.toml" good.py
expect 1 yaml-bad yamllint -c "$root/yamllint.yml" -f parsable bad.yml
grep -q '(key-duplicates)' yaml-bad
expect 1 yaml-syntax yamllint -c "$root/yamllint.yml" -f parsable syntax.yml
grep -q '(syntax)' yaml-syntax
expect 0 yaml-good yamllint -c "$root/yamllint.yml" good.yml
mkdir codeql-src
cat > codeql-src/bad.py <<'FIXTURE'
import os
from flask import Flask, request
app = Flask(__name__)
@app.get("/bad")
def bad():
    os.system(request.args["command"])
FIXTURE
cat > codeql-src/good.py <<'FIXTURE'
import os
from flask import Flask
app = Flask(__name__)
@app.get("/good")
def good():
    os.system("echo fixture")
FIXTURE
expect 0 codeql-create codeql database create codeql-db --language=python --build-mode=none --source-root="$scratch/codeql-src" --threads=1 --ram=1024
expect 0 codeql-analysis codeql database analyze codeql-db codeql/python-queries:Security/CWE-078/CommandInjection.ql --format=sarif-latest --output=codeql.sarif --threads=1 --ram=1024
python3 - <<'PYTHON'
import json
results = [r for run in json.load(open('codeql.sarif'))['runs'] for r in run.get('results', [])]
paths = [loc['physicalLocation']['artifactLocation']['uri'] for r in results if r['ruleId'] == 'py/command-line-injection' for loc in r['locations']]
assert any(p.endswith('bad.py') for p in paths), 'CodeQL missed command injection'
assert not any(p.endswith('good.py') for p in paths), 'CodeQL flagged clean control'
PYTHON
echo 'PASS: seven positive fixtures, clean controls and YAML syntax; non-root login shell'
