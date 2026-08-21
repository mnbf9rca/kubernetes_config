#!/usr/bin/env python3
"""Lint every script the clusters actually run, plus the repo's Python.

WHY THIS EXISTS
---------------
Sixteen shell and Python scripts drive this repo's backups, snapshots and
ingest jobs, and until this check landed **nothing the repo could run** looked
at any of them. There was no shellcheck in the Makefile, no ruff, no pyflakes,
no test runner, no CI workflow and no pre-commit hook. Every shellcheck result
that ever appeared in a review came from an agent typing the command by hand,
which is a process that works right up until nobody types it.

That is the same defect `check-job-ttl` and `check-script-substitution` were
each created to fix: a rule everybody agrees with, enforced by nothing. Those
two are now preflight prerequisites of `diff-*` and `apply-*`; so is this.

The bugs this catches are the ones this repo has actually been bitten by --
`set -e` silently not applying inside an AND-OR list, a pipeline whose exit
status is never checked, `local` used in POSIX `sh`, `du` writing to stderr
inside a numeric capture. `check-script-substitution` already stops a script
leaking a secret into a ConfigMap; nothing stopped a script simply being wrong.

LINTS THE RENDER, NOT THE SOURCE
--------------------------------
What gets linted must be what the cluster runs. Several scripts are not files
at all: `homelab/backup/restic-cronjob.yaml` carries roughly 430 lines of shell
inline in a YAML block scalar, and a source-tree lint would walk straight past
it. So this runs `kustomize build <cluster>` and pulls the shell back out of
the rendered stream, from two places:

  * ConfigMap `data:` keys -- what a `configMapGenerator` produces from a file
    under a cluster's `scripts/` directory. Keys ending `.sh`, and keys whose
    value opens with a shell `#!` line, are shell; `.py` keys and Python
    shebangs are Python.
  * Block scalars inside a container's `args:` / `command:` list -- the inline
    case above. The language comes from the interpreter the SAME container
    names in its `command:`/`args:` plain scalars (`sh`, `-c`). If no
    interpreter can be identified the snippet is NOT quietly skipped: that is
    reported as "could not run", because a block of shell nobody linted is
    exactly the hole this check exists to close.

Upstream bases are linted too, for the same reason -- `local-path-config`'s
`setup`/`teardown` keys really do run on the node -- but their findings are
ADVISORY and do not fail the check. A snippet counts as ours when it can be
located verbatim in a repo file; anything else came from a remote base and
cannot be fixed here, only forked. Failing every apply on somebody else's style
warning would make the gate something people route around, and a routed-around
gate protects nothing. The advisories are still printed, and the upstream
snippets are still named in the OK output, so a snippet of YOUR OWN that stops
resolving to a repo file is visible rather than silently downgraded.

Findings are reported against the SOURCE file wherever the snippet can be
located there -- exact contiguous match allowing a constant indent, which is
unambiguous at these sizes -- so `path:line` is somewhere you can actually go
and edit. Snippets with no source match are reported against the render.

`shellcheck -s sh`, NEVER `-s bash`
-----------------------------------
These scripts run under busybox ash (`restic/restic`, `alpine/k8s`) and dash,
not bash. `-s bash` would suppress SC3040 and the whole SC3xxx portability
family -- the most valuable findings here, because a bashism in an ash
container is a runtime failure in a backup job at 03:00, not a style nit.
Shebangs do not override this: `-s` is authoritative.

Deliberate `# shellcheck disable=` directives in the scripts are honoured, as
they must be -- SC3040 for `set -o pipefail` under a busybox ash that supports
it, SC2016 for single quotes that must survive into another container, SC2125
and SC2086 for a glob that is deliberately left unexpanded, SC1091 for a
library sourced from a sibling ConfigMap key.

PYTHON
------
The Python phase is repo-wide and runs whichever cluster is named: it needs no
render and no cluster, so scoping it per-cluster would buy nothing and would
leave the repo's own tooling scripts unguarded.

  * Every `*.py` under `homelab/`, `vps/` and `scripts/` is compiled -- a
    syntax floor that is always available because it is stdlib.
  * Every `test_*.py` beside them is executed. They are stdlib `unittest` by
    design (see the header of `test_cloudflare_analytics_ingest.py`) precisely
    so that running them needs no toolchain.
  * A real linter runs only if one is genuinely installed. `ruff`, `pyflakes`
    and `flake8` are probed, as executables and as `python3 -m`. If none is
    present this prints an explicit SKIP naming what it looked for. It does
    NOT pass silently and it does NOT claim to have linted.

`shellcheck` itself is treated as REQUIRED, not optional: skipping it would
recreate the exact defect this check exists to remove. It is listed in
`make check-tools`. `brew install shellcheck`.

Usage:
  scripts/check-script-lint.py [cluster ...]      default: homelab vps

Exit status:
  0  everything linted clean, every test passed
  1  at least one finding (each reported as `path:line:col: SCxxxx message`)
  2  the check itself could not run -- shellcheck missing, kustomize missing
     or a build failed, or a snippet whose language could not be determined.
     Kept distinct for the same reason `check-job-ttl` keeps it distinct: a
     build that never rendered tells you NOTHING about the scripts in it, and
     a network blip must never be able to read as a clean run or as a rule
     violation.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CLUSTERS = ("homelab", "vps")

# Roots searched both for the Python phase and for resolving a rendered
# snippet back to the file it came from.
SOURCE_ROOTS = ("homelab", "vps", "scripts")
SOURCE_SUFFIXES = (".sh", ".py", ".yaml", ".yml")
PRUNE_DIRS = {".git", "__pycache__", ".venv", "node_modules"}

# Interpreter basenames. `busybox` appears because `busybox sh -c` is a real
# spelling; the `sh` token in the same list settles it either way.
SHELL_NAMES = {"sh", "ash", "dash", "bash", "busybox"}
PYTHON_NAMES = {"python", "python3"}

# `kustomize build` emits normalised block YAML: one resource per document,
# documents separated by a bare `---` at column 0, top-level keys at column 0,
# two-space indent steps, no anchors, no merge keys, no multi-line flow
# scalars. Block scalars are the only multi-line construct, and their content
# is always indented past their header. Every pattern below relies on exactly
# that, and on nothing else -- the same ground `check-job-ttl.py` stands on.
DOC_SEP = "---"
BLOCK_HEADER = r"\|[0-9+-]*\s*$"
# `  some-key.sh: |` -- a ConfigMap data key.
DATA_BLOCK = re.compile(r"^ {2}([A-Za-z0-9_.\-]+): " + BLOCK_HEADER)
# `            - |` -- a block scalar as a list item.
ITEM_BLOCK = re.compile(r"^( *)- " + BLOCK_HEADER)
# A mapping key, either bare (`command:`) or opening a list item (`- args:`).
BARE_KEY = re.compile(r"^( *)([A-Za-z0-9_.\-]+):\s*$")
LIST_KEY = re.compile(r"^( *)- ([A-Za-z0-9_.\-]+):\s*$")
# A plain scalar list entry: `            - sh`.
LIST_SCALAR = re.compile(r"^( *)- (?!\|)(\S.*)$")
# `file:line:col: level: message [SCxxxx]` -- shellcheck's gcc format.
GCC_LINE = re.compile(r"^([^:]+):(\d+):(\d+):\s*(\w+):\s*(.*)$")

SHEBANG_SHELL = re.compile(r"^#!\s*\S*/(?:env\s+)?(sh|ash|dash|bash|busybox)\b")
SHEBANG_PYTHON = re.compile(r"^#!\s*\S*/(?:env\s+)?(python[0-9.]*)\b")


class CheckUnrunnable(Exception):
    """The check could not run at all. Exit 2, never 0 and never 1."""


class Snippet(object):
    """One extracted script, and where it came from.

    `first_render_line` is the 1-based line number, in the `kustomize build`
    output, of the snippet's own first line -- so a finding at snippet line L
    is at render line `first_render_line + L - 1`. `source` is the same thing
    resolved back to a repo file, when one matches.
    """

    def __init__(self, cluster, label, language, text, first_render_line):
        self.cluster = cluster
        self.label = label
        self.language = language
        self.text = text
        self.first_render_line = first_render_line
        self.source = None          # (relative path, 1-based line of line 1)

    def where(self, line):
        """Render a finding's location as the most actionable string we have."""
        if self.source:
            path, start = self.source
            return "%s:%d" % (path, start + line - 1)
        return "%s render line %d (%s)" % (
            self.cluster, self.first_render_line + line - 1, self.label)


# --------------------------------------------------------------------------
# Rendering and extraction
# --------------------------------------------------------------------------

def build(cluster):
    """`kustomize build <cluster>`, with every failure mode as CheckUnrunnable.

    Needs NETWORK: both clusters' bootstrap layers pull remote bases and
    kustomize fetches them. A slow fetch that trips kustomize's git timeout is
    reported as "could not run", never as a lint finding.
    """
    try:
        out = subprocess.run(["kustomize", "build", cluster], check=False,
                             capture_output=True, text=True, timeout=180,
                             cwd=REPO_ROOT)
    except FileNotFoundError:
        raise CheckUnrunnable("kustomize not on PATH")
    except subprocess.TimeoutExpired:
        raise CheckUnrunnable(
            "`kustomize build %s` timed out after 180s" % cluster)
    if out.returncode != 0:
        raise CheckUnrunnable("`kustomize build %s` failed:\n%s"
                              % (cluster, out.stderr.strip()[:2000]))
    return out.stdout


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def capture_block(lines, header):
    """Read the block scalar whose header is at index `header`.

    Returns `(body_lines, first_body_index)`. Content indent is taken from the
    first non-blank line, per the YAML spec, and the block ends at the first
    non-blank line indented less than that. Blank lines are kept in place, so
    index arithmetic against the render stays exact.
    """
    header_indent = indent_of(lines[header])
    body, content_indent, first = [], None, None
    index = header + 1
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            body.append("")
            index += 1
            continue
        if content_indent is None:
            if indent_of(line) <= header_indent:
                break
            content_indent = indent_of(line)
            first = index
        if indent_of(line) < content_indent:
            break
        body.append(line[content_indent:])
        index += 1
    while body and not body[-1].strip():
        body.pop()
    if first is None:
        return [], None
    # Drop leading blanks so body[0] is the first real line, and report that
    # line's render position.
    lead = first - (header + 1)
    return body[lead:], first


def classify(key, body):
    """`shell`, `python` or None, from a ConfigMap data key and its content."""
    if key.endswith(".sh"):
        return "shell"
    if key.endswith(".py"):
        return "python"
    if body:
        if SHEBANG_SHELL.match(body[0]):
            return "shell"
        if SHEBANG_PYTHON.match(body[0]):
            return "python"
    return None


def enclosing_key(lines, index):
    """The mapping key that owns the list item at `index`, or None.

    A list entry at column N belongs to the nearest preceding key whose own
    name starts at column N -- either `^ {N}name:` or `^ {N-2}- name:`, the
    latter being how kustomize writes the first key of a list item.
    """
    column = indent_of(lines[index])
    for back in range(index - 1, -1, -1):
        line = lines[back]
        if not line.strip():
            continue
        bare = BARE_KEY.match(line)
        if bare and len(bare.group(1)) == column:
            return bare.group(2)
        listed = LIST_KEY.match(line)
        if listed and len(listed.group(1)) + 2 == column:
            return listed.group(2)
        if indent_of(line) < column - 2:
            return None
    return None


def interpreter_language(lines, index):
    """Language of the inline block at `index`, from its container's argv.

    Scans the enclosing list item -- from its `- ` marker to the next line at
    or below that indent -- for plain scalar entries at the block's own column.
    Those are the `command:`/`args:` tokens. Returns `shell`, `python`,
    `"other"` for an interpreter we recognise as neither, or None when no
    interpreter could be identified at all (which the caller escalates).
    """
    column = indent_of(lines[index])
    marker = column - 2
    if marker < 0:
        return None
    start = None
    for back in range(index - 1, -1, -1):
        line = lines[back]
        if line.strip() and indent_of(line) == marker and \
                line[marker:marker + 2] == "- ":
            start = back
            break
        if line.strip() and indent_of(line) < marker:
            break
    if start is None:
        return None
    end = len(lines)
    for forward in range(start + 1, len(lines)):
        line = lines[forward]
        if line.strip() and indent_of(line) <= marker:
            end = forward
            break

    found_any = False
    for line in lines[start:end]:
        scalar = LIST_SCALAR.match(line)
        if not scalar or len(scalar.group(1)) != column:
            continue
        token = scalar.group(2).strip().strip("'\"")
        if not token or token.startswith("-"):
            continue
        name = os.path.basename(token)
        found_any = True
        if name in SHELL_NAMES:
            return "shell"
        if name in PYTHON_NAMES:
            return "python"
    return "other" if found_any else None


def extract(cluster, render):
    """Every script snippet in one cluster's rendered stream."""
    lines = render.split("\n")
    boundaries = [0] + [i + 1 for i, line in enumerate(lines)
                        if line == DOC_SEP] + [len(lines)]
    snippets = []

    for start, stop in zip(boundaries, boundaries[1:]):
        doc = lines[start:stop]
        kind, name, namespace, section = None, "<unnamed>", None, None
        for line in doc:
            if line.startswith("kind: "):
                kind = line[6:].strip()
            elif line and not line.startswith(" ") and line.endswith(":"):
                section = line[:-1]
            elif section == "metadata" and line.startswith("  name: "):
                name = line[8:].strip()
            elif section == "metadata" and line.startswith("  namespace: "):
                namespace = line[13:].strip()
        where = "%s/%s" % (namespace, name) if namespace else name

        in_data = False
        for index, line in enumerate(doc):
            if line and not line.startswith(" ") and line.endswith(":"):
                in_data = line == "data:"

            if in_data and kind == "ConfigMap":
                key = DATA_BLOCK.match(line)
                if key:
                    body, first = capture_block(doc, index)
                    language = classify(key.group(1), body)
                    if language and body:
                        snippets.append(Snippet(
                            cluster,
                            "ConfigMap %s key %s" % (where, key.group(1)),
                            language, "\n".join(body) + "\n",
                            start + first + 1))
                    continue

            if ITEM_BLOCK.match(line):
                owner = enclosing_key(doc, index)
                if owner not in ("args", "command"):
                    continue
                body, first = capture_block(doc, index)
                if not body:
                    continue
                language = interpreter_language(doc, index)
                if language is None:
                    raise CheckUnrunnable(
                        "%s: inline %s block in %s %s (render line %d) names no "
                        "interpreter, so its language cannot be determined.\n"
                        "Refusing to guess: an unlinted block of shell is "
                        "exactly what this check exists to prevent. Give the "
                        "container an explicit `command:`."
                        % (cluster, owner, kind, where, start + index + 1))
                if language == "other":
                    continue
                snippets.append(Snippet(
                    cluster, "%s %s inline %s" % (kind, where, owner),
                    language, "\n".join(body) + "\n", start + first + 1))

    return snippets


# --------------------------------------------------------------------------
# Mapping a rendered snippet back to the file it came from
# --------------------------------------------------------------------------

def source_candidates():
    paths = []
    for root in SOURCE_ROOTS:
        base = os.path.join(REPO_ROOT, root)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
            for filename in sorted(filenames):
                if filename.endswith(SOURCE_SUFFIXES):
                    paths.append(os.path.join(dirpath, filename))
    return paths


def read_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().split("\n")
    except OSError as exc:
        raise CheckUnrunnable("cannot read %s: %s" % (path, exc))


def locate(snippet, candidates):
    """Find the snippet verbatim in a repo file, allowing a constant indent.

    A snippet embedded in YAML is indented; a snippet that came from a file is
    not. Requiring the same extra indent on every line is what makes this a
    match rather than a guess, and at these sizes a false positive would need
    two files to share dozens of consecutive identical lines.
    """
    body = snippet.text.rstrip("\n").split("\n")
    anchor = next((i for i, line in enumerate(body) if line.strip()), None)
    if anchor is None:
        return None
    needle = body[anchor]

    for path in candidates:
        lines = read_lines(path)
        for index, line in enumerate(lines):
            # Cheap prefilter, then the exact constant-indent test.
            if line.strip() != needle.strip():
                continue
            extra = indent_of(line) - indent_of(needle)
            if extra < 0 or line != " " * extra + needle:
                continue
            start = index - anchor
            if start < 0:
                continue
            if all(_line_matches(lines, start + offset, extra, text)
                   for offset, text in enumerate(body)):
                return (os.path.relpath(path, REPO_ROOT), start + 1)
    return None


def _line_matches(lines, index, extra, text):
    if index >= len(lines):
        return False
    if not text.strip():
        return not lines[index].strip()
    return lines[index] == " " * extra + text


# --------------------------------------------------------------------------
# Shell phase
# --------------------------------------------------------------------------

def run_shellcheck(snippets):
    """shellcheck every shell snippet as POSIX sh.

    Returns `[(snippet, message), ...]`. One invocation over all of them:
    shellcheck's own exit code is not used to decide pass/fail (it conflates
    "found something" with "could not read a file"), the parsed findings are.
    """
    if shutil.which("shellcheck") is None:
        raise CheckUnrunnable(
            "shellcheck not on PATH.\nThis gate is not optional -- skipping it "
            "would restore the exact hole it was\nadded to close. Install it: "
            "`brew install shellcheck`.")

    findings = []
    with tempfile.TemporaryDirectory(prefix="check-script-lint-") as tmp:
        names = {}
        for number, snippet in enumerate(snippets):
            slug = re.sub(r"[^A-Za-z0-9._-]+", "-", snippet.label).strip("-")
            filename = "%03d-%s" % (number, slug)
            if not filename.endswith(".sh"):
                filename += ".sh"
            names[filename] = snippet
            with open(os.path.join(tmp, filename), "w",
                      encoding="utf-8") as handle:
                handle.write(snippet.text)

        if not names:
            return findings

        # -s sh, never -s bash. See the module docstring.
        command = ["shellcheck", "-s", "sh", "-f", "gcc"] + sorted(names)
        try:
            out = subprocess.run(command, check=False, capture_output=True,
                                 text=True, timeout=300, cwd=tmp)
        except subprocess.TimeoutExpired:
            raise CheckUnrunnable("shellcheck timed out after 300s")

        for line in out.stdout.splitlines():
            match = GCC_LINE.match(line.strip())
            if not match:
                continue
            filename, number, column, level, message = match.groups()
            snippet = names.get(os.path.basename(filename))
            if snippet is None:
                continue
            findings.append((snippet, "%s:%s: %s: %s"
                             % (snippet.where(int(number)), column, level,
                                message)))

        if not findings and out.returncode != 0:
            raise CheckUnrunnable(
                "shellcheck exited %d with no parseable findings:\n%s"
                % (out.returncode, (out.stderr or out.stdout).strip()[:2000]))
    return findings


# --------------------------------------------------------------------------
# Python phase
# --------------------------------------------------------------------------

PYTHON_LINTERS = (
    ["ruff", "check"],
    ["python3", "-m", "ruff", "check"],
    ["pyflakes"],
    ["python3", "-m", "pyflakes"],
    ["flake8"],
    ["python3", "-m", "flake8"],
)


def python_files():
    sources, tests = [], []
    for root in SOURCE_ROOTS:
        base = os.path.join(REPO_ROOT, root)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                sources.append(path)
                if filename.startswith("test_"):
                    tests.append(path)
    return sources, tests


def compile_check(paths, extra_snippets):
    """Syntax floor. Always available -- `compile` is a builtin."""
    findings = []
    units = [(path, None) for path in paths]
    units += [(snippet.where(1), snippet) for snippet in extra_snippets]
    for label, snippet in units:
        if snippet is None:
            try:
                with open(label, encoding="utf-8") as handle:
                    text = handle.read()
            except OSError as exc:
                raise CheckUnrunnable("cannot read %s: %s" % (label, exc))
            display = os.path.relpath(label, REPO_ROOT)
        else:
            text, display = snippet.text, label
        try:
            compile(text, display, "exec")
        except SyntaxError as exc:
            findings.append("%s:%s: SyntaxError: %s"
                            % (display, exc.lineno, exc.msg))
    return findings


def find_python_linter():
    for command in PYTHON_LINTERS:
        if shutil.which(command[0]) is None:
            continue
        probe = subprocess.run(command + ["--version"], check=False,
                               capture_output=True, text=True, timeout=60)
        if probe.returncode == 0:
            return command
    return None


def lint_python(paths):
    """Run a Python linter if one is installed. Returns (findings, note)."""
    command = find_python_linter()
    if command is None:
        return [], ("SKIP: no Python linter installed (probed ruff, pyflakes, "
                    "flake8 as commands and as `python3 -m`).\n"
                    "      Python was syntax-checked and its tests were run; "
                    "it was NOT linted.")
    try:
        out = subprocess.run(command + paths, check=False, capture_output=True,
                             text=True, timeout=300, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        raise CheckUnrunnable("%s timed out after 300s" % command[0])
    findings = []
    if out.returncode != 0:
        for line in (out.stdout or out.stderr).splitlines():
            if line.strip():
                findings.append(line.rstrip())
    return findings, "OK: %s clean across %d Python file(s)" % (
        " ".join(command), len(paths))


def run_tests(paths):
    """Execute each stdlib-unittest suite. Returns (findings, count)."""
    findings = []
    for path in paths:
        try:
            out = subprocess.run([sys.executable, path], check=False,
                                 capture_output=True, text=True, timeout=600,
                                 cwd=REPO_ROOT)
        except subprocess.TimeoutExpired:
            raise CheckUnrunnable("%s timed out after 600s"
                                  % os.path.relpath(path, REPO_ROOT))
        if out.returncode != 0:
            tail = (out.stderr or out.stdout).strip().splitlines()[-30:]
            findings.append("%s FAILED:\n    %s"
                            % (os.path.relpath(path, REPO_ROOT),
                               "\n    ".join(tail)))
    return findings, len(paths)


# --------------------------------------------------------------------------

def main(argv):
    clusters = argv[1:] or list(DEFAULT_CLUSTERS)
    notes, advisories = [], []
    try:
        snippets = []
        for cluster in clusters:
            snippets.extend(extract(cluster, build(cluster)))

        candidates = source_candidates()
        for snippet in snippets:
            snippet.source = locate(snippet, candidates)

        shell = [s for s in snippets if s.language == "shell"]
        rendered_python = [s for s in snippets if s.language == "python"]
        vendored = [s for s in snippets if s.source is None]

        findings = []
        for snippet, message in run_shellcheck(shell):
            # Snippets this repo did not author cannot be fixed here, only
            # forked. They are still linted and still reported -- silence would
            # be a lie -- but they do not fail the gate, because a gate that
            # blocks every apply on somebody else's style warning is a gate
            # that gets bypassed, and a bypassed gate protects nothing.
            (advisories if snippet.source is None else findings).append(message)
        notes.append("OK: shellcheck -s sh clean across %d repo-authored shell "
                     "script(s) rendered from %s"
                     % (len(shell) - len(vendored), ", ".join(clusters)))

        sources, tests = python_files()
        findings += compile_check(sources, rendered_python)
        notes.append("OK: %d Python file(s) + %d rendered Python script(s) "
                     "compile" % (len(sources), len(rendered_python)))

        lint_findings, lint_note = lint_python(sources)
        findings += lint_findings
        notes.append(lint_note)

        test_findings, suites = run_tests(tests)
        findings += test_findings
        notes.append("OK: %d Python test suite(s) pass" % suites)

        if vendored:
            # Named, not just counted: this list is how you notice that a
            # snippet YOU wrote stopped resolving to a repo file and quietly
            # became advisory-only.
            notes.append(
                "NOTE: %d snippet(s) come from an upstream base, not this "
                "repo, so their\n      findings are advisory. Their sources: "
                "%s" % (len(vendored),
                        ", ".join(sorted({s.label for s in vendored}))))
    except CheckUnrunnable as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    if findings:
        print("Script lint findings:\n")
        for finding in findings:
            print("  %s" % finding)
        print("\nEach location is the SOURCE file where the snippet could be "
              "located, else\nits position in `kustomize build` output. "
              "Shell is checked as POSIX sh because\nthese run under busybox "
              "ash and dash -- SC3xxx findings are real portability\nbugs, not "
              "style. If a warning is genuinely wrong, add a narrow\n"
              "`# shellcheck disable=SCxxxx` WITH a stated reason, as the "
              "existing ones do.\nSee the header of scripts/check-script-lint.py.")
        _print_advisories(advisories)
        return 1

    for note in notes:
        print(note)
    _print_advisories(advisories)
    return 0


def _print_advisories(advisories):
    if not advisories:
        return
    print("\nAdvisory (upstream bases, not fixable in this repo — not failing "
          "the check):")
    for advisory in advisories:
        print("  %s" % advisory)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
