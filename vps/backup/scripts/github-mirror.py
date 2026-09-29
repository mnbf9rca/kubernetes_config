#!/usr/bin/env python3
"""Mirror every repo, wiki and gist of one GitHub account, then export the
non-git data with github-backup. Runs as the init container of the
`github-mirror` CronJob (vps/backup/github-mirror.yaml); the restic container
that follows reads the one-line status file this writes. Design and runbooks:
docs/operations/github-mirror.md.

Fail-safe rules, in order of importance:
  * never delete a directory: a repo that vanishes upstream keeps its mirror;
  * an empty owner list or a failed listing is `enumerate-failed` and touches
    nothing on disk, because a dead account must never read as "delete";
  * the token reaches git only through GIT_CONFIG_* environment as an
    Authorization header (Basic, x-access-token:TOKEN; GitHub rejects Bearer
    for git over HTTPS), never on argv, never in a file, so the mirror's
    `config` holds the plain URL and restic can ship it;
  * always exit 0 after writing the status file, so the restic container runs
    and pushes the verdict; exit 1 only when the status file cannot be written.

Detection: for each default branch, a ref whose old commit is not an ancestor
of its new one is a forced update, and a ref present before and gone after is a
deletion. Other branches are rebased here every day and are not counted.

This file passes through envsubst on its way into a ConfigMap: it must not
name any cluster-prefixed allowlisted variable. Runtime names are MIRROR_DATA,
GITHUB_TOKEN_FILE and GITHUB_LOGIN. See `make check-script-substitution`.
"""
import base64
import json
import os
import subprocess
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
PER_PAGE = 100
EXPORT_FLAGS = [
    "--issues", "--issue-comments", "--issue-events",
    "--pulls", "--pull-comments", "--pull-reviews", "--pull-commits",
    "--releases", "--assets", "--labels", "--milestones",
    "--private", "--fork", "--incremental", "--prefer-ssh",
]


class ApiError(Exception):
    pass


def read_token(path):
    return Path(path).read_text(encoding="utf-8").splitlines()[0].strip()


def api_get_all(path, token=None):
    """Every page of a list endpoint. Raises ApiError on any non-2xx."""
    out = []
    page = 1
    while True:
        sep = "&" if "?" in path else "?"
        req = urllib.request.Request(
            f"{API}{path}{sep}per_page={PER_PAGE}&page={page}",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "github-mirror"},
        )
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                batch = json.load(resp)
        except urllib.error.HTTPError as e:
            raise ApiError(f"HTTP {e.code} on {path}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ApiError(f"{type(e).__name__} on {path}") from None
        if not isinstance(batch, list):
            raise ApiError(f"non-list body on {path}")
        out.extend(batch)
        if len(batch) < PER_PAGE:
            return out
        page += 1


def git_env(token):
    """Auth for github.com and gist.github.com through git's environment
    config, never argv or disk. GIT_CONFIG_COUNT is git 2.31+."""
    env = dict(os.environ)
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        header = f"Authorization: Basic {basic}"
        env.update({
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": header,
            "GIT_CONFIG_KEY_1": "http.https://gist.github.com/.extraheader",
            "GIT_CONFIG_VALUE_1": header,
        })
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def refs(repo):
    """{refname: object id} of a bare repository; {} if it does not exist."""
    if not (repo / "HEAD").exists():
        return {}
    p = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "--format=%(refname) %(objectname)"],
        capture_output=True, text=True, check=False,
    )
    if p.returncode != 0:
        raise RuntimeError("git for-each-ref failed")
    return dict(line.split(" ", 1) for line in p.stdout.splitlines() if " " in line)


def mirror(url, dest, env):
    """Clone --mirror on first sight, else fetch --prune. Never deletes."""
    if (dest / "HEAD").exists():
        cmd = ["git", "-C", str(dest), "fetch", "--quiet", "--prune", "origin",
               "+refs/*:refs/*"]
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", "--quiet", "--mirror", url, str(dest)]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)


def classify(before, after, default_ref, repo):
    """(forced, deleted) for one default branch between two ref snapshots."""
    old = before.get(default_ref)
    new = after.get(default_ref)
    if old is None:
        return (0, 0)
    if new is None:
        return (0, 1)
    if old == new:
        return (0, 0)
    p = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", old, new],
        capture_output=True, check=False,
    )
    return (1, 0) if p.returncode != 0 else (0, 0)


def wiki_absent(proc):
    """GitHub answers a wiki with no pages with exit 128 + 'not found'."""
    return proc.returncode == 128 and "not found" in proc.stderr.lower()


def run_export(data, login, token_file, repo=None):
    """Export one repo, or account data when repo is None. Never clones;
    --prefer-ssh prevents token URLs even in memory."""
    flags = ([*EXPORT_FLAGS, "--repository", repo] if repo is not None else
             ["--starred", "--private", "--fork", "--incremental", "--prefer-ssh"])
    cmd = ["github-backup", login, "--token-fine", f"file://{token_file}",
           "--output-directory", str(data), *flags]
    return subprocess.run(cmd, check=False).returncode


def repository_gone(login, name, token):
    """Only a confirmed 404 makes an export failure informational."""
    req = urllib.request.Request(
        f"{API}/repos/{login}/{name}",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "github-mirror", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60):
            return False
    except urllib.error.HTTPError as e:
        return e.code == 404
    except OSError:
        return False


def write_status(data, **kv):
    line = " ".join(f"{k}={v}" for k, v in kv.items())
    print(f"status: {line}")
    (data / ".status").write_text(line + "\n", encoding="utf-8")


def main():
    data = Path(os.environ.get("MIRROR_DATA", "/data"))
    counts = dict(repos=0, gists=0, forced_default=0, deleted_default=0,
                  mirror_failed=0, export_failed=0, export_gone=0)
    try:
        token_file = os.environ.get("GITHUB_TOKEN_FILE", "/run/secrets/github/token")
        login = os.environ["GITHUB_LOGIN"]
        token = read_token(token_file)
        verdict = "enumerate-failed"
        try:
            repos = api_get_all("/user/repos?affiliation=owner", token)
            gists = api_get_all(f"/users/{login}/gists")  # public endpoint: PAT has no gist scope
        except ApiError as e:
            print(f"enumerate failed: {e}", file=sys.stderr)
            repos = []
        else:
            if not repos:
                print("enumerate failed: empty owner list", file=sys.stderr)

        if repos:
            env = git_env(token)
            for r in repos:
                name = r["name"]
                dest = data / "repositories" / name / "repository"
                before = refs(dest)
                p = mirror(r["clone_url"], dest, env)
                if p.returncode != 0:
                    print(f"mirror failed: {name} rc={p.returncode}", file=sys.stderr)
                    counts["mirror_failed"] += 1
                    continue
                counts["repos"] += 1
                f, d = classify(before, refs(dest), f"refs/heads/{r['default_branch']}", dest)
                counts["forced_default"] += f
                counts["deleted_default"] += d
                if r.get("has_wiki"):
                    wdest = data / "repositories" / name / "wiki"
                    wp = mirror(r["clone_url"].removesuffix(".git") + ".wiki.git", wdest, env)
                    if wp.returncode != 0 and not wiki_absent(wp):
                        print(f"wiki mirror failed: {name} rc={wp.returncode}", file=sys.stderr)
                        counts["mirror_failed"] += 1
            for g in gists:
                dest = data / "gists" / g["id"] / "repository"
                p = mirror(g["git_pull_url"], dest, env)
                if p.returncode != 0:
                    print(f"gist mirror failed: {g['id']} rc={p.returncode}", file=sys.stderr)
                    counts["mirror_failed"] += 1
                    continue
                counts["gists"] += 1

            for r in repos:
                if run_export(data, login, token_file, r["name"]) != 0:
                    if repository_gone(login, r["name"], token):
                        counts["export_gone"] += 1
                        print(f"export gone: {r['name']} (HTTP 404)", flush=True)
                    else:
                        counts["export_failed"] += 1
            if run_export(data, login, token_file) != 0:
                counts["export_failed"] += 1
            if counts["mirror_failed"]:
                verdict = "mirror-failed"
            elif counts["export_failed"]:
                verdict = "export-failed"
            else:
                verdict = "ok"
    except Exception:
        traceback.print_exc(file=sys.stderr)
        verdict = "mirror-failed"

    write_status(data, verdict=verdict, **counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
