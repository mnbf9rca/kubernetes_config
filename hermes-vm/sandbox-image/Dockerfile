FROM nousresearch/hermes-sandbox:v0.21.6-desktop@sha256:669abbd2df186067be9c645eee5fddb8764c9fe8b9edd28625ec3a61e6685443

# Renovate pairs each release with the checksum of its Linux amd64 attachment.
ARG SHELLCHECK_VERSION=v0.11.0
ARG SHELLCHECK_SHA256=8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198
ARG GITLEAKS_VERSION=v8.30.1
ARG GITLEAKS_SHA256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb

RUN set -eu; \
    mkdir -p /opt/hermes-reviewer/bin; \
    curl -fsSL "https://github.com/koalaman/shellcheck/releases/download/${SHELLCHECK_VERSION}/shellcheck-${SHELLCHECK_VERSION}.linux.x86_64.tar.xz" -o /tmp/shellcheck.tar.xz; \
    curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION#v}_linux_x64.tar.gz" -o /tmp/gitleaks.tar.gz; \
    printf '%s  %s\n' "$SHELLCHECK_SHA256" /tmp/shellcheck.tar.xz "$GITLEAKS_SHA256" /tmp/gitleaks.tar.gz | sha256sum -c -; \
    tar -xJf /tmp/shellcheck.tar.xz -C /opt/hermes-reviewer/bin --strip-components=1 "shellcheck-${SHELLCHECK_VERSION}/shellcheck"; \
    tar -xzf /tmp/gitleaks.tar.gz -C /opt/hermes-reviewer/bin gitleaks; \
    rm /tmp/shellcheck.tar.xz /tmp/gitleaks.tar.gz; \
    ln -s /opt/hermes-reviewer/bin/shellcheck /usr/local/bin/shellcheck; \
    ln -s /opt/hermes-reviewer/bin/gitleaks /usr/local/bin/gitleaks; \
    test "$(shellcheck --version | sed -n 's/^version: //p')" = "${SHELLCHECK_VERSION#v}"; \
    test "$(gitleaks version)" = "${GITLEAKS_VERSION#v}"

COPY requirements.txt /opt/hermes-reviewer/requirements.txt
RUN uv venv --python python3 /opt/hermes-reviewer/venv \
    && uv pip install --python /opt/hermes-reviewer/venv/bin/python --require-hashes --only-binary :all: --no-deps -r /opt/hermes-reviewer/requirements.txt \
    && uv pip check --python /opt/hermes-reviewer/venv/bin/python \
    && for tool in bandit semgrep ruff yamllint; do ln -s "/opt/hermes-reviewer/venv/bin/$tool" "/usr/local/bin/$tool"; done

COPY semgrep-rules.yml /opt/hermes-reviewer/rules/semgrep-rules.yml
COPY ruff.toml yamllint.yml test-tools.sh /opt/hermes-reviewer/
ENV SEMGREP_SEND_METRICS=off SEMGREP_ENABLE_VERSION_CHECK=0
RUN /opt/hermes-reviewer/venv/bin/python - <<'PY'
import importlib.metadata as m
import pathlib
import re, subprocess
versions = {name: m.version(name) for name in ('bandit', 'semgrep', 'ruff', 'yamllint')}
versions['shellcheck'] = re.search(r'^version: (.+)$', subprocess.check_output(['shellcheck', '--version'], text=True), re.M)[1]
versions['gitleaks'] = subprocess.check_output(['gitleaks', 'version'], text=True).strip().removeprefix('v')
pathlib.Path('/etc/hermes-reviewer-tools.txt').write_text(''.join(f'{name} {versions[name]}\n' for name in sorted(versions)))
PY
# /etc/profile resets PATH in Hermes's login shell; deliver commands there too.
RUN chmod -R a+rX /opt/hermes-reviewer && chmod 755 /opt/hermes-reviewer/test-tools.sh && chmod 644 /etc/hermes-reviewer-tools.txt
