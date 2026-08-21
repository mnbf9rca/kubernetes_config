#!/usr/bin/env python3
"""Assert no healthchecks.io ping body is built from a command's output.

WHY THIS EXISTS
---------------
A ping body leaves the estate. healthchecks.io is a third-party SaaS; the body
is stored in their object storage and stays there until the ping log rotates.
So an `emit` call is a line in a public file, and the rule that keeps it safe is
in the spec as section 9.2:

    NEVER BUILD A BODY FROM A COMMAND'S OUTPUT.

healthchecks.io's own documentation teaches the opposite (`m=$(certbot renew
2>&1); curl --data-raw "$m" ...`) and for this estate that pattern leaks:

  - `restic` error messages quote the repository URL;
  - `kubectl exec` into the influxdb pod returns a stream produced by scripts
    that pass the InfluxDB OPERATOR token on argv (influx-native-backup.sh:21,
    influx-export-lp.sh:36), so anything echoing argv puts that token in a body,
    repeated nightly;
  - `wget`/`curl` errors quote the full URL, which for a ping IS the check's
    write credential;
  - and none of it is bounded.

The obvious enforcement - "grep every emit for a `$(` in review" - is defeated
by one intermediate assignment:

    M=$(restic backup /data 2>&1); emit "error=$M"

which passes that grep cleanly and posts the repository URL to a third party.
So this check refuses BOTH: command substitution inside a sink argument, AND a
sink argument naming any variable known to hold captured output or a credential.

THE RULE
--------
A body sink (`emit`, `say_err`, `fatal` in shell; `hc_emit`, `hc_summary` in
Python) may only be called with:

  * literal text, and
  * variables the script itself computed - a count, an age, a byte size, a path
    built from a literal glob, or a classified verdict from a fixed enum.

Arithmetic expansion `$(( ... ))` is fine and is stripped before scanning: it
cannot run a command.

This is not a proof. Human review still reads spec section 9. It catches the
three vectors an adversarial review actually found, which a grep did not.

Usage:
  scripts/check-ping-bodies.py [dir ...]     default: the cluster trees

Exit status:
  0  every sink call is clean
  1  at least one is not (each reported as `path:line: reason`)
  2  the check itself could not run (a required target file missing, the
     Makefile unreadable, a scan directory that is not a directory)
"""
import ast
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAKEFILE = os.path.join(REPO_ROOT, "Makefile")

SCAN_ROOTS = ("homelab", "vps")
TEXT_SUFFIXES = (".sh", ".yaml", ".yml")
PY_SUFFIXES = (".py",)

# Files that MUST exist and MUST be scanned. If one is renamed or moved out of a
# scanned root, this check would silently stop covering it - and "I could not
# look" must never be reported as "I looked and everything is fine". Same
# reasoning as the VPS backup gate applies to a `find` that errors.
REQUIRED_TARGETS = (
    "homelab/backup/restic-cronjob.yaml",
    "homelab/health/scripts/influx-backup.sh",
    "homelab/health/scripts/ingest-freshness.sh",
    "homelab/health/scripts/cloudflare-analytics-ingest.py",
    "vps/backup/scripts/restic-backup.sh",
)

# Shell/YAML sinks. `say_err` and `fatal` are checked too because they FEED
# `emit`: one sink per diagnostic is the convention (spec 7.2), so the argument
# that reaches the pod log is the same string that reaches the third party.
SHELL_SINKS = ("emit", "say_err", "fatal")

# Python sinks.
PY_SINKS = ("hc_emit", "hc_summary")

# Variables that hold captured output, a credential, or something spec 9.1
# forbids outright. Checked in both `${NAME}` and bare `$NAME` form. The
# envsubst allowlists are added to this at run time.
DENY_VARS = (
    # ping UUIDs - a write credential, and in scope in every one of these scripts
    "HC_UUID", "HC_APPLE", "HC_GARMIN",
    # captured command output
    "OUT", "BODY", "RESP", "OUTPUT", "STDERR",
    # credentials read from a Secret into a differently-named variable
    "TOKEN", "PASSWORD", "SECRET", "KEY",
    # spec 9.3: pod names are excluded with no exceptions
    "POD",
)

# Python: names a sink argument may reference. Adding one is a deliberate review
# act - it asserts that the value is a count, a timestamp, an age, or a
# classified verdict, and never a response body or an exception message.
PY_VALUE_ALLOWLIST = frozenset({
    "chunks_done", "MAX_CHUNKS", "RETENTION_HOURS",
    "rows_total", "series_total",
    "watermark", "start", "now", "committed_through",
    "g_start", "g_end", "missing_hours", "gap_marker",
    "warnings", "rc", "lag_minutes",
})

# Python: calls a sink argument may make. `iso` formats a datetime; `len`, `int`
# and `str` are shape-preserving. `type` is allowed ONLY as `type(x).__name__`,
# enforced separately below - the class name of an exception is safe, its repr
# is not, because the exception in hand may be a QueryFailed carrying a zone tag
# or up to 800 bytes of raw response.
PY_CALL_ALLOWLIST = frozenset({"iso", "len", "int", "str", "type"})

ARITH = re.compile(r"\$\(\([^()]*\)\)")
# `NAME=` in command position, taking the rest of the line as the right-hand
# side. Deliberately over-approximate: a false taint costs one `untaint` comment
# with a written reason, a missed taint costs a secret.
ASSIGN = re.compile(r"(?:^|[;&|(]|\s)([A-Za-z_][A-Za-z0-9_]*)=(?P<rhs>\S.*)$")
READ_INTO = re.compile(r"(?:^|[;&|(]|\s)read\s+(?P<names>(?:-\w+\s+)*[A-Za-z_].*)$")
UNTAINT = re.compile(
    r"#\s*check-ping-bodies:\s*untaint\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?P<why>\S.*)$")
COMMENT_ONLY = re.compile(r"^\s*(#|//)")
# A sink name in COMMAND POSITION: start of line, or after ; && || { ( or a pipe.
SINK_CALL_TMPL = (r"(?:^|[;&|{(]|\|\||\b(?:do|then|else)\s)"
                  r"\s*(%s)\s+(?P<arg>\S.*)$")
SINK_DEF_TMPL = r"^\s*(?:%s)\s*\(\s*\)"


def _names_any(text, names):
    """True if `text` references any of `names` as $NAME or ${NAME}."""
    for name in names:
        if re.search(r"\$\{?%s(?![A-Za-z0-9_])" % re.escape(name), text):
            return True
    return False


class CheckUnrunnable(Exception):
    """The check could not run at all. Given its own exit code (2)."""


def read_makefile_vars(path):
    """Return {name: value} for the Makefile's simple assignments."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise CheckUnrunnable("cannot read %s: %s" % (path, exc))
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
    seen = seen or set()
    if name in seen:
        raise CheckUnrunnable("recursive Makefile variable %s" % name)
    if name not in variables:
        raise CheckUnrunnable(
            "%s is not defined in %s - this check cannot be trusted until it is"
            % (name, MAKEFILE))
    seen = seen | {name}

    def replace(match):
        return expand(match.group(1) or match.group(2), variables, seen)

    return re.sub(
        r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)|\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
        replace, variables[name])


def denied_names():
    """DENY_VARS plus both envsubst allowlists."""
    variables = read_makefile_vars(MAKEFILE)
    names = set(DENY_VARS)
    for list_name in ("ENVSUBST_VAR_NAMES", "VPS_ENVSUBST_VAR_NAMES"):
        entries = expand(list_name, variables).split()
        if not entries:
            raise CheckUnrunnable("%s resolved to an empty list" % list_name)
        names.update(entries)
    return sorted(names)


def build_var_pattern(names):
    """`${NAME}` or a bare `$NAME` for any denied name, with a right boundary."""
    alternation = "|".join(re.escape(n) for n in names)
    return re.compile(r"\$\{(%s)\}|\$(%s)(?![A-Za-z0-9_])"
                      % (alternation, alternation))


def scan_text(path, lines, var_pattern):
    """Yield (line_number, reason) for every unsafe shell/YAML sink call.

    Tracks TAINT within the file: a variable assigned from a command
    substitution, from a backtick, from `read`, or from another tainted
    variable, is itself tainted, and naming a tainted variable in a sink
    argument is refused. That closes the hole a plain `$(`-grep leaves open:

        M=$(restic backup /data 2>&1); emit "error=$M"

    LIMITATION, stated rather than hidden: positional parameters are not
    tracked, so a tainted value passed into a function and emitted as `$1` is
    not caught. In this estate those are resolved artifact paths, which spec
    section 9.3 accepts.

    To clear a taint, write the marker comment on its own line, WITH A REASON:

        # check-ping-bodies: untaint _zs - gated to digits by the case above
    """
    call = re.compile(SINK_CALL_TMPL % "|".join(SHELL_SINKS))
    definition = re.compile(SINK_DEF_TMPL % "|".join(SHELL_SINKS))
    tainted = set()
    for number, line in enumerate(lines, 1):
        clean = ARITH.sub("", line)
        untaint = UNTAINT.search(clean)
        if untaint:
            tainted.discard(untaint.group(1))
            continue
        if COMMENT_ONLY.match(line):
            continue
        if not definition.match(line):
            for assign in ASSIGN.finditer(clean):
                rhs = assign.group("rhs")
                if "$(" in rhs or "`" in rhs or _names_any(rhs, tainted):
                    tainted.add(assign.group(1))
            reader = READ_INTO.search(clean)
            if reader:
                # Stop at the end of the `read` command; `while read -r x; do
                # emit ...` must not taint `do` and `emit` as well as `x`.
                names = re.split(r"[;&|<>]", reader.group("names"))[0]
                for name in names.split():
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                        tainted.add(name)
        if definition.match(line):
            continue
        match = call.search(line)
        if not match:
            continue
        arg = ARITH.sub("", match.group("arg"))
        for name in sorted(tainted):
            if re.search(r"\$\{?%s(?![A-Za-z0-9_])" % re.escape(name), arg):
                yield number, ("$%s in a %s argument holds captured output "
                               "(assigned from a command substitution, a "
                               "backtick or `read` earlier in this file). "
                               "Emit a value the script computed, or clear it "
                               "with a `# check-ping-bodies: untaint %s "
                               "<reason>` line."
                               % (name, match.group(1), name))
        if "$(" in arg:
            yield number, "command substitution in a %s argument" % match.group(1)
        if "`" in arg:
            yield number, "backtick substitution in a %s argument" % match.group(1)
        for hit in var_pattern.finditer(arg):
            yield number, ("%s in a %s argument - it holds captured output or a "
                           "credential" % (hit.group(0), match.group(1)))


def _py_reason(node):
    """None if this expression is a safe sink argument, else why not.

    `type(exc).__name__` is the one shape that may name an out-of-allowlist
    variable. An exception's CLASS NAME is safe; its `repr` is not, because the
    exception in hand may be a QueryFailed whose message splices in `zone_tag`
    (from the CF_ZONE_TAGS Secret) or up to 800 bytes of raw response body.
    """
    permitted = set()
    for child in ast.walk(node):
        if (isinstance(child, ast.Attribute) and child.attr == "__name__"
                and isinstance(child.value, ast.Call)
                and isinstance(child.value.func, ast.Name)
                and child.value.func.id == "type"
                and len(child.value.args) == 1
                and isinstance(child.value.args[0], ast.Name)):
            permitted.add(id(child.value.args[0]))
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name) and func.id in PY_CALL_ALLOWLIST:
                continue
            return "call to %s is not on the sink allowlist" % ast.unparse(func)
        if isinstance(child, ast.Attribute):
            if child.attr == "__name__":
                continue
            return "attribute .%s is not on the sink allowlist" % child.attr
        if isinstance(child, ast.Name):
            if id(child) in permitted:
                continue
            if child.id in PY_VALUE_ALLOWLIST or child.id in PY_CALL_ALLOWLIST:
                continue
            return ("name %r is not on the sink value allowlist in "
                    "check-ping-bodies.py" % child.id)
        if isinstance(child, ast.JoinedStr):
            return "f-string in a sink argument"
    return None


def scan_python(path, source):
    """Yield (line_number, reason) for every unsafe Python sink call."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise CheckUnrunnable("cannot parse %s: %s" % (path, exc))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id in PY_SINKS):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            reason = _py_reason(arg)
            if reason:
                yield node.lineno, "%s(): %s" % (func.id, reason)


def files_to_scan(roots):
    for root in roots:
        base = root if os.path.isabs(root) else os.path.join(REPO_ROOT, root)
        if not os.path.isdir(base):
            raise CheckUnrunnable("not a directory: %s" % base)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "__pycache__")]
            for filename in sorted(filenames):
                if filename.endswith(TEXT_SUFFIXES + PY_SUFFIXES):
                    yield os.path.join(dirpath, filename)


def assert_targets_present(roots):
    """Every REQUIRED_TARGET under a scanned root must exist."""
    for target in REQUIRED_TARGETS:
        if not any(target.startswith(r.rstrip("/") + "/") for r in roots):
            continue
        full = os.path.join(REPO_ROOT, target)
        if not os.path.isfile(full):
            raise CheckUnrunnable(
                "required target is missing: %s - if it moved, update "
                "REQUIRED_TARGETS in scripts/check-ping-bodies.py in the same "
                "commit, or this check silently stops covering it" % target)


def main(argv):
    roots = argv[1:] or list(SCAN_ROOTS)
    try:
        assert_targets_present(roots)
        var_pattern = build_var_pattern(denied_names())
        hits, scanned, calls = [], 0, 0
        call_re = re.compile(SINK_CALL_TMPL % "|".join(SHELL_SINKS))
        def_re = re.compile(SINK_DEF_TMPL % "|".join(SHELL_SINKS))
        for path in files_to_scan(roots):
            scanned += 1
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    source = handle.read()
            except OSError as exc:
                raise CheckUnrunnable("cannot read %s: %s" % (path, exc))
            rel = os.path.relpath(path, REPO_ROOT)
            if path.endswith(PY_SUFFIXES):
                for number, reason in scan_python(path, source):
                    hits.append((rel, number, reason))
                calls += sum(source.count("%s(" % s) for s in PY_SINKS)
            else:
                lines = source.splitlines()
                for number, reason in scan_text(path, lines, var_pattern):
                    hits.append((rel, number, reason))
                for line in lines:
                    if not COMMENT_ONLY.match(line) and not def_re.match(line) \
                            and call_re.search(line):
                        calls += 1
    except CheckUnrunnable as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    if hits:
        print("Unsafe healthchecks.io ping-body sink call(s):\n")
        for path, number, reason in hits:
            print("  %s:%d: %s" % (path, number, reason))
        print("\nA ping body leaves the estate and is retained by a third party."
              "\nNever build one from a command's output: restic quotes the "
              "repository URL,\nthe exec'd influx scripts carry the operator "
              "token on argv, and a failing\nwget quotes the ping URL, which IS "
              "the check's write credential.\n"
              "\nEmit a count, an age, a byte size, a path the script built from "
              "a literal\nglob, or a verdict from a fixed enum. See spec section "
              "9.2 and the header of\nscripts/check-ping-bodies.py.")
        return 1

    print("OK: %d file(s) under %s, %d ping-body sink call(s), none unsafe"
          % (scanned, ", ".join(roots), calls))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
