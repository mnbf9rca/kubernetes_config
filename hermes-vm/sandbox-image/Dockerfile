FROM ghcr.io/astral-sh/uv:0.12.24@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a AS uv
FROM nousresearch/hermes-sandbox:v0.21.7-desktop@sha256:c7d8607d076f26a64e0bd6828f74e1764af43fe3281368188459bb693bfa7567

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
    tar --no-same-owner -xJf /tmp/shellcheck.tar.xz -C /opt/hermes-reviewer/bin --strip-components=1 "shellcheck-${SHELLCHECK_VERSION}/shellcheck"; \
    tar --no-same-owner -xzf /tmp/gitleaks.tar.gz -C /opt/hermes-reviewer/bin gitleaks; \
    rm /tmp/shellcheck.tar.xz /tmp/gitleaks.tar.gz; \
    ln -s /opt/hermes-reviewer/bin/shellcheck /usr/local/bin/shellcheck; \
    ln -s /opt/hermes-reviewer/bin/gitleaks /usr/local/bin/gitleaks; \
    test "$(shellcheck --version | sed -n 's/^version: //p')" = "${SHELLCHECK_VERSION#v}"; \
    test "$(gitleaks version)" = "${GITLEAKS_VERSION#v}"

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock /opt/hermes-reviewer/
RUN UV_PROJECT_ENVIRONMENT=/opt/hermes-reviewer/venv UV_PYTHON_DOWNLOADS=never uv sync --project /opt/hermes-reviewer --python python3 --locked --no-dev --no-build \
    && uv pip check --python /opt/hermes-reviewer/venv/bin/python \
    && for tool in bandit semgrep ruff yamllint; do ln -s "/opt/hermes-reviewer/venv/bin/$tool" "/usr/local/bin/$tool"; done

COPY semgrep-rules.yml /opt/hermes-reviewer/rules/semgrep-rules.yml
COPY ruff.toml yamllint.yml test-tools.sh /opt/hermes-reviewer/

ARG CODEQL_VERSION=codeql-bundle-v2.27.2
ARG CODEQL_SHA256=f002864be6dd8d5d7bdb123aaf7291ec8e291012bd9f70b6d2362f730db52aeb
RUN set -eu; \
    curl -fsSL "https://github.com/github/codeql-action/releases/download/${CODEQL_VERSION}/codeql-bundle-linux64.tar.gz" -o /tmp/codeql.tar.gz; \
    printf '%s  %s\n' "$CODEQL_SHA256" /tmp/codeql.tar.gz | sha256sum -c -; \
    mkdir -p /opt/hermes-reviewer/codeql; \
    tar --no-same-owner -xzf /tmp/codeql.tar.gz -C /opt/hermes-reviewer/codeql --strip-components=1; \
    rm /tmp/codeql.tar.gz; \
    for language in actions cpp csharp go java javascript ruby rust swift; do \
        rm -rf "/opt/hermes-reviewer/codeql/$language" /opt/hermes-reviewer/codeql/qlpacks/codeql/"$language"-*; \
    done; \
    chmod -R a+rX /opt/hermes-reviewer; \
    chmod 755 /opt/hermes-reviewer/test-tools.sh; \
    ln -s /opt/hermes-reviewer/codeql/codeql /usr/local/bin/codeql; \
    test "$(codeql version --format=json | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')" = "${CODEQL_VERSION#codeql-bundle-v}"

ENV SEMGREP_SEND_METRICS=off SEMGREP_ENABLE_VERSION_CHECK=0
RUN /opt/hermes-reviewer/venv/bin/python - <<'PY'
import importlib.metadata as m
import json, pathlib, re, subprocess
versions = {name: m.version(name) for name in ('bandit', 'semgrep', 'ruff', 'yamllint')}
versions['shellcheck'] = re.search(r'^version: (.+)$', subprocess.check_output(['shellcheck', '--version'], text=True), re.M)[1]
versions['gitleaks'] = subprocess.check_output(['gitleaks', 'version'], text=True).strip().removeprefix('v')
versions['codeql'] = json.loads(subprocess.check_output(['codeql', 'version', '--format=json'], text=True))['version']
pathlib.Path('/etc/hermes-reviewer-tools.txt').write_text(''.join(f'{name} {versions[name]}\n' for name in sorted(versions)))
PY
# /etc/profile resets PATH in Hermes's login shell; deliver commands there too.
RUN chmod 644 /etc/hermes-reviewer-tools.txt
