#!/usr/bin/env python3
"""Assert no configMapGenerator script names an envsubst-allowlisted variable.

WHY THIS EXISTS
---------------
Files delivered by a kustomize `configMapGenerator` are part of the rendered
stream, so they pass through `envsubst` exactly like a Secret manifest does.
`envsubst` substitutes the BARE `$NAME` form as well as `${NAME}` — verified,
not assumed:

    $ FOO=REALVALUE envsubst '${FOO}' <<<'a=$FOO b=${FOO} c=$BAR'
    a=REALVALUE b=REALVALUE c=$BAR

The homelab allowlist contains `RESTIC_REPOSITORY`, `RESTIC_PASSWORD`,
`B2_ACCOUNT_ID` and `B2_ACCOUNT_KEY` — precisely the environment variable names
restic itself reads. A backup script that writes the perfectly ordinary line

    echo "Initializing restic repo at $RESTIC_REPOSITORY"

therefore does not ship that line. It ships the resolved B2 repository URL,
baked into a **ConfigMap**, which is stored unencrypted and readable by anyone
with namespace read access. `$RESTIC_PASSWORD` would put the repository
password there in plaintext.

`make check-placeholder-coverage` cannot catch this. That check looks for
placeholders which SURVIVE the render; this failure mode is a placeholder that
was substituted when it should never have been a placeholder at all. The two
checks are mirror images and neither substitutes for the other.

A second, quieter failure mode: an allowlisted name that happens to be unset at
render time substitutes to the EMPTY STRING silently, so a script variable
would simply vanish mid-line.

THE RULE
--------
No file under a cluster `scripts/` directory may mention any name in
`ENVSUBST_VAR_NAMES` or `VPS_ENVSUBST_VAR_NAMES`, in either the `${NAME}` or
the bare `$NAME` form. Both lists are checked against every script regardless
of cluster: a name that is safe under `vps/` today becomes a live hazard the
moment somebody shares that script with `homelab/`, and sharing is exactly what
a refactor reaches for.

If a script genuinely needs such a value, indirect it through a differently
named environment variable set in the YAML — a `secretKeyRef` where the value
is a secret, a literal where it is not — and use the new name in the script:

    env:
      - name: REPO_DISPLAY
        valueFrom: { secretKeyRef: { name: restic-b2, key: RESTIC_REPOSITORY } }

Usage:
  scripts/check-script-substitution.py [dir ...]   default: the cluster trees

Exit status:
  0  no script mentions an allowlisted variable
  1  at least one does (each reported as `path:line: $NAME`)
  2  the check itself could not run (Makefile unreadable, a list missing or
     empty, a scan directory that is not a directory)
"""
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAKEFILE = os.path.join(REPO_ROOT, "Makefile")

# The allowlists to enforce. Both are applied to every script; see the module
# docstring for why the cluster a script currently lives under is not a defence.
ALLOWLIST_VARS = ("ENVSUBST_VAR_NAMES", "VPS_ENVSUBST_VAR_NAMES")

# Roots scanned for `scripts/` directories. The repo-level `scripts/` directory
# is deliberately NOT scanned: nothing in it is rendered by kustomize, so
# nothing in it ever meets envsubst. Only files that ride the manifest stream
# are at risk, and those live under a cluster tree.
SCAN_ROOTS = ("homelab", "vps")


class CheckUnrunnable(Exception):
    """The check could not run at all.

    Given its own exit code because "I could not look" must never be reported
    as "I looked and everything is fine" — the same reasoning the VPS backup
    gate applies to a `find` that errors.
    """


def read_makefile_vars(path):
    """Return {name: value} for the Makefile's simple assignments.

    Only what this check needs: `NAME := value` / `NAME = value` with backslash
    continuations joined, comments and rules ignored. Values may reference
    other variables as `$(OTHER)` or `${OTHER}`; those are expanded below.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise CheckUnrunnable("cannot read %s: %s" % (path, exc))

    # Join continuation lines before parsing, so a multi-line list assignment
    # is seen whole.
    text = re.sub(r"\\\n\s*", " ", text)

    assignment = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:?\??=\s*(.*)$")
    variables = {}
    for line in text.splitlines():
        if line.startswith("\t") or line.lstrip().startswith("#"):
            continue
        match = assignment.match(line)
        if match:
            variables[match.group(1)] = match.group(2).strip()
    return variables


def expand(name, variables, seen=None):
    """Expand `$(OTHER)` / `${OTHER}` references inside a Makefile value."""
    seen = seen or set()
    if name in seen:
        raise CheckUnrunnable("recursive Makefile variable %s" % name)
    if name not in variables:
        raise CheckUnrunnable(
            "%s is not defined in %s — this check cannot be trusted until it "
            "is" % (name, MAKEFILE))
    seen = seen | {name}

    def replace(match):
        return expand(match.group(1) or match.group(2), variables, seen)

    return re.sub(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)|\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
                  replace, variables[name])


def allowlisted_names():
    """The union of both envsubst allowlists, as a sorted list."""
    variables = read_makefile_vars(MAKEFILE)
    names = set()
    for list_name in ALLOWLIST_VARS:
        entries = expand(list_name, variables).split()
        if not entries:
            # An empty list would make this check pass vacuously against every
            # script in the repo, which is worse than not having it.
            raise CheckUnrunnable("%s resolved to an empty list" % list_name)
        names.update(entries)
    return sorted(names)


def script_files(roots):
    """Yield every file under a `scripts/` directory beneath the given roots."""
    for root in roots:
        base = root if os.path.isabs(root) else os.path.join(REPO_ROOT, root)
        if not os.path.isdir(base):
            raise CheckUnrunnable("not a directory: %s" % base)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
            if os.path.basename(dirpath) != "scripts":
                continue
            for filename in sorted(filenames):
                yield os.path.join(dirpath, filename)


def build_pattern(names):
    """One regex matching `${NAME}` or a bare `$NAME` for any allowlisted name.

    The bare form needs a trailing boundary: without it `$RESTIC_PASSWORD`
    would also be reported for the innocent `$RESTIC_PASSWORD_FILE`, and more
    importantly `$B2_ACCOUNT_ID` must not be missed inside `$B2_ACCOUNT_IDX`
    only because the longer name sorted first. Alternating on the sorted names
    with an explicit boundary makes the match exact either way.
    """
    alternation = "|".join(re.escape(n) for n in names)
    return re.compile(r"\$\{(%s)\}|\$(%s)(?![A-Za-z0-9_])"
                      % (alternation, alternation))


def main(argv):
    roots = argv[1:] or list(SCAN_ROOTS)
    try:
        names = allowlisted_names()
        pattern = build_pattern(names)
        hits, scanned = [], 0
        for path in script_files(roots):
            scanned += 1
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    lines = handle.readlines()
            except OSError as exc:
                raise CheckUnrunnable("cannot read %s: %s" % (path, exc))
            for number, line in enumerate(lines, 1):
                for match in pattern.finditer(line):
                    hits.append((os.path.relpath(path, REPO_ROOT), number,
                                 match.group(0)))
    except CheckUnrunnable as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    if hits:
        print("Script(s) naming an envsubst-allowlisted variable:\n")
        for path, number, token in hits:
            print("  %s:%d: %s" % (path, number, token))
        print("\nThese files pass through envsubst on their way into a "
              "ConfigMap, and envsubst\nsubstitutes the bare $NAME form too. "
              "Each token above would be replaced by the\nREAL secret value "
              "and stored, in plaintext, in a ConfigMap.\n"
              "\nFix by giving the script a differently named variable and "
              "setting it in the\nworkload's `env:` — a secretKeyRef for a "
              "secret, a literal for anything else.\nSee the header of "
              "scripts/check-script-substitution.py.")
        return 1

    print("OK: %d script(s) under %s name none of the %d allowlisted vars"
          % (scanned, ", ".join(roots), len(names)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
