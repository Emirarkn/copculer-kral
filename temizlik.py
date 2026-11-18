"""Local review and approval gate for deletion-only Git patches."""

import argparse
import ast
import datetime
import difflib
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import tokenize
import unicodedata


SOURCE = set(".c .cc .cpp .cs .css .go .h .hpp .html .java .js .jsx .kt .md .mjs .cjs .php .py .rb .rs .scss .sh .sql .svelte .ts .tsx .vue .ps1".split())
CONFIG = set(".cfg .ini .json .toml .txt .xml .yaml .yml".split())
BUILD_NAMES = {"dockerfile", "makefile", "procfile", "jenkinsfile"}
TEMPLATE = set(".jinja .j2 .hbs .ejs .erb .njk .twig .liquid .cshtml .razor".split())
PROSE = {".md", ".txt"}
BLOCKED_DIRS = set("build coverage dist vendor node_modules data datasets migrations fixtures generated out target".split())
SENSITIVE_DIRS = set("secrets credentials private dumps .aws .ssh .kube .docker .gnupg".split())
SENSITIVE_FILES = set(".npmrc .pypirc .netrc .git-credentials .htpasswd .pgpass .dockercfg".split())
SENSITIVE_EXT = set(".pem .key .p12 .pfx .jks .keystore .tfstate .tfvars".split())
SENSITIVE_REASONS = {"secret_content", "sensitive_name", "sensitive_dir"}
SEARCHABLE_REASONS = {"blocked_dir", "extension", "dotfile", "size"}
BLOCKED_NAME = re.compile(r"(^|[._-])(secret[s]?|credentials?|tokens?|passwords?|passwd|private[-_]?key|env|dump|id_rsa|id_ed25519)([._-]|$)", re.I)
BLOCKED_TEXT = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:gh[pousr]_|github_pat_|sk-(?:proj-|live-)?|AKIA)[A-Za-z0-9_-]{16,}|(?:password|api[_-]?key|client[_-]?secret|access[_-]?token)\s*[:=]\s*['\"][^'\"\r\n]{8,}['\"]", re.I)
MAX_BYTES = 1_000_000
REFS_MAX_BYTES = 5_000_000
ENV = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
ENV.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)


class Refusal(Exception):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(root, *args, payload=None):
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull, "-c", "apply.ignoreWhitespace=no", *args],
        cwd=root, env=ENV, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=60, check=False,
    )
    if result.returncode:
        raise Refusal(f"Git command failed ({args[0]}, exit {result.returncode}).")
    return result.stdout


def default_state_path(system, environ, home):
    if system == "nt":
        base = environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    else:
        base = environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
    return os.path.join(base, "copculer_kral")


def state_dir():
    """Directory holding receipts and journals. Never inside the target project."""
    override = os.environ.get("COPCULER_STATE_DIR")
    if override:
        return Path(override)
    return Path(default_state_path(os.name, os.environ, str(Path.home())))


def is_inside(child, parent):
    child = os.path.normcase(str(Path(child).resolve()))
    parent = os.path.normcase(str(Path(parent).resolve()))
    return child == parent or child.startswith(parent.rstrip("\\/") + os.sep)


def prepare_state(root):
    """Validate and create the state directory; refuse locations inside the project."""
    base = state_dir()
    if is_inside(base, root):
        raise Refusal("The state directory cannot be inside the target project.")
    base.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(base, 0o700)
    return base.resolve()


def reject_inside_project(root, path, what):
    if is_inside(path, root):
        raise Refusal(f"{what} cannot be inside the target project.")


COMMAND_CONFIG = re.compile(r"^(filter\..+\.(clean|smudge|process)|diff\..+\.(textconv|command)|diff\.external)$", re.I)


def check_repo_config(root):
    """Refuse repositories whose local config makes Git run commands on files (filter drivers, textconv).

    System and global config are already disabled, but .git/config is under the project's control and
    `git apply` can run clean filters. Review such a repository by hand instead.
    """
    listing = git(root, "config", "--local", "--list", "-z").decode("utf-8", "replace")
    keys = sorted({entry.split("\n", 1)[0] for entry in listing.split("\0") if entry})
    bad = [key for key in keys if COMMAND_CONFIG.match(key)]
    if bad:
        raise Refusal("The repository-local Git config defines settings that run commands on files ("
                      + ", ".join(bad) + "). Remove them or review this repository by hand; "
                      "the tool will not run commands configured by a repository.")


def project(value):
    root = Path(value).resolve(strict=True)
    if not root.is_dir() or Path(git(root, "rev-parse", "--show-toplevel").decode("utf-8").rstrip("\r\n")).resolve() != root:
        raise Refusal("Provide the root directory of a Git project.")
    check_repo_config(root)
    return root


def eligibility_reason(name):
    """None when the name is in review scope, otherwise the exclusion reason. Looks at the name only."""
    if not isinstance(name, str) or any(not char.isprintable() for char in name) or any(char in name for char in '\\:"'):
        return "invalid_name"
    parts = name.split("/")
    if any(not part or part in {".", ".."} or part.endswith((".", " ")) for part in parts):
        return "invalid_name"
    lower = name.lower()
    lowered = [part.lower() for part in parts]
    if any(part in SENSITIVE_DIRS for part in lowered):
        return "sensitive_dir"
    for part, low in zip(parts, lowered):
        if BLOCKED_NAME.search(part) or low in SENSITIVE_FILES or low.startswith(".env"):
            return "sensitive_name"
    if PurePosixPath(lowered[-1]).suffix in SENSITIVE_EXT:
        return "sensitive_name"
    for number, part in enumerate(lowered):
        if part.startswith(".") and not (number == 0 and lower.startswith(".github/workflows/")):
            return "dotfile"
    if any(part in BLOCKED_DIRS for part in lowered):
        return "blocked_dir"
    path = PurePosixPath(lower)
    if path.suffix not in SOURCE | CONFIG and path.name not in BUILD_NAMES:
        return "extension"
    return None


def eligible(name):
    return eligibility_reason(name) is None


def safe_path(root, name):
    if not eligible(name):
        raise Refusal("File is out of scope.")
    current = root
    parts = name.split("/")
    for number, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if number == len(parts) - 1:
                return current
            raise Refusal("The file's parent directory was not found.") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise Refusal("Links and reparse points are out of scope.")
        if number < len(parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                raise Refusal("The file's parent path must be a directory.")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
            raise Refusal("File type or size is out of scope.")
    return current


def read_bytes(root, name):
    path = safe_path(root, name)
    with path.open("rb") as source:
        data = source.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise Refusal("File size is out of scope.")
    return data


def read_source(root, name):
    data = read_bytes(root, name)
    if b"\0" in data or BLOCKED_TEXT.search(data):
        raise Refusal("File is excluded by the content filter.")
    data.decode("utf-8")
    return data


def physical_state(root, name):
    """(reason, size) for a tracked path, walking components without following links."""
    current = root
    parts = name.split("/")
    for number, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return "missing", 0
        except OSError:
            return "not_regular", 0
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return "link", 0
        if number < len(parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                return "not_regular", 0
        else:
            if not stat.S_ISREG(info.st_mode):
                return "not_regular", 0
            if info.st_nlink != 1:
                return "link", 0
            return None, info.st_size
    return "missing", 0


def load_file(root, name, max_bytes):
    """(reason, data). Only called for names that are not sensitive by name."""
    reason, size = physical_state(root, name)
    if reason:
        return reason, None
    if size > REFS_MAX_BYTES:
        return "too_large", None
    if size > max_bytes:
        return "size", None
    try:
        with (root / name).open("rb") as source:
            data = source.read(REFS_MAX_BYTES + 1)
    except OSError:
        return "not_regular", None
    if len(data) > REFS_MAX_BYTES:
        return "too_large", None
    if b"\0" in data:
        return "binary", None
    if BLOCKED_TEXT.search(data):
        return "secret_content", None
    try:
        data.decode("utf-8")
    except UnicodeError:
        return "non_utf8", None
    return None, data


def inventory(root):
    """(snapshot, files {path: sha}, excluded {path: reason}). Sensitive names are never opened."""
    paths = git(root, "ls-files", "--cached", "-z").split(b"\0")
    files = {}
    excluded = {}
    for raw in sorted(set(paths) - {b""}):
        name = raw.decode("utf-8", "backslashreplace")
        reason = eligibility_reason(name) if raw.decode("utf-8", "ignore").encode("utf-8") == raw else "invalid_name"
        if reason is None:
            reason, data = load_file(root, name, MAX_BYTES)
            if reason is None:
                files[name] = digest(data)
                continue
        excluded[name] = reason
    snapshot = digest(encoded({"root": str(root), "files": files, "excluded": excluded, "tracked": digest(b"\0".join(paths))}))
    return snapshot, files, excluded


def excluded_report(excluded):
    return [{"path": name, "reason": reason} for name, reason in sorted(excluded.items())]


SYMBOL = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")  # refs: at least 3 characters
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")  # symbols field: short names are allowed too


def reference_scan(root, files, excluded, symbols, overrides=None):
    """Word-bounded symbol search.

    Searches in-scope files (or their post-edit content from overrides) and non-sensitive excluded files.
    Sensitive or unreadable files are never searched; they come back as not_reviewed (path, reason).
    """
    overrides = overrides or {}
    patterns = {symbol: re.compile(r"(?<![A-Za-z0-9_])" + re.escape(symbol) + r"(?![A-Za-z0-9_])") for symbol in symbols}
    in_scope = {symbol: {} for symbol in symbols}
    outside = {symbol: {} for symbol in symbols}
    not_reviewed = []
    for name in sorted(files):
        if name in overrides:
            data = overrides[name]
        else:
            data = read_source(root, name)
            if digest(data) != files[name]:
                raise Refusal("The file changed after the scan.")
        if data is None or not patterns:
            continue
        for number, line in enumerate(data.decode("utf-8").split("\n"), 1):
            for symbol, pattern in patterns.items():
                if pattern.search(line):
                    in_scope[symbol].setdefault(name, []).append(number)
    for name, reason in sorted(excluded.items()):
        if reason not in SEARCHABLE_REASONS:
            not_reviewed.append((name, reason))
            continue
        reason, data = load_file(root, name, REFS_MAX_BYTES)
        if reason:
            not_reviewed.append((name, reason))
            continue
        if not patterns:
            continue
        text = data.decode("utf-8")
        for symbol, pattern in patterns.items():
            count = sum(1 for line in text.split("\n") if pattern.search(line))
            if count:
                outside[symbol][name] = count
    return {"in_scope": in_scope, "excluded_matches": outside, "not_reviewed": not_reviewed}


def refs(root, symbol):
    if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
        raise Refusal("The symbol must be an identifier of at least 3 characters ([A-Za-z_][A-Za-z0-9_]).")
    snapshot, files, excluded = inventory(root)
    scan = reference_scan(root, files, excluded, [symbol])
    if not scan["in_scope"][symbol]:
        raise Refusal("The symbol does not appear in any in-scope file.")
    return {
        "symbol": symbol, "snapshot": snapshot,
        "in_scope": [{"path": name, "lines": lines} for name, lines in sorted(scan["in_scope"][symbol].items())],
        "excluded_matches": [{"path": name, "count": count} for name, count in sorted(scan["excluded_matches"][symbol].items())],
        "not_reviewed": [{"path": name, "reason": reason} for name, reason in scan["not_reviewed"]],
    }


def candidate_symbols(item):
    symbols = item.get("symbols", [])
    if (not isinstance(symbols, list) or len(symbols) > 50
            or any(not isinstance(symbol, str) or not IDENT.fullmatch(symbol) for symbol in symbols)):
        raise Refusal("symbols must be a list of identifier strings.")
    return list(dict.fromkeys(symbols))


def code_line_numbers(data):
    """Line numbers that hold code tokens (None if the source cannot be tokenized)."""
    skip = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
            tokenize.ENDMARKER, tokenize.ENCODING}
    lines = set()
    try:
        for token in tokenize.tokenize(io.BytesIO(data).readline):
            if token.type not in skip:
                lines.update(range(token.start[0], token.end[0] + 1))
    except (tokenize.TokenError, SyntaxError):
        return None
    return lines


def deletes_code(name, before, item):
    suffix = PurePosixPath(name.lower()).suffix
    if suffix in PROSE:
        return False
    if item.get("action") == "delete_file" and "lines" not in item:
        return True
    if suffix == ".py":
        code = code_line_numbers(before)
        if code is not None:
            removed = {number for first, last in item["lines"] for number in range(first, last + 1)}
            return bool(removed & code)
    return True


def removed_numbers(before, item):
    if item.get("action") == "delete_file" and "lines" not in item:
        return set(range(1, len(lines_for(before)) + 1))
    return {number for first, last in item["lines"] for number in range(first, last + 1)}


UNVERIFIED_BINDING = "deleted bindings could not be fully verified"
ATTRIBUTE_BINDING = "attribute or index assignment cannot be verified"
_BINDER_NAMES = ("FunctionDef", "AsyncFunctionDef", "ClassDef", "alias", "ExceptHandler", "Name", "MatchAs",
                 "MatchStar", "MatchMapping", "TypeVar", "ParamSpec", "TypeVarTuple")
KNOWN_BINDERS = tuple(getattr(ast, item) for item in _BINDER_NAMES if hasattr(ast, item))
MATCH_BINDERS = tuple(getattr(ast, item) for item in ("MatchAs", "MatchStar", "MatchMapping") if hasattr(ast, item))
STATEMENT_UNITS = tuple(item for item in (ast.stmt, ast.excepthandler, getattr(ast, "match_case", None)) if item)


def node_span(node):
    """(first, last) line of a statement-like node, including decorators; match_case has no lineno of its own."""
    if hasattr(ast, "match_case") and isinstance(node, ast.match_case):
        return node.pattern.lineno, max(item.end_lineno for item in node.body)
    first = min([item.lineno for item in getattr(node, "decorator_list", [])] + [node.lineno])
    return first, node.end_lineno


def collect_bindings(node, names, issues, local=False, comprehension=False):
    """Collect every name a removed statement binds (or deletes) in the enclosing scope.

    Anything that binds a name through a path not listed here ends up in `issues`: unknown means unverified.
    `local` is set inside removed function, class and lambda bodies (their names cannot be reached from outside,
    except through global/nonlocal). `comprehension` hides loop variables but not walrus targets.
    """
    visible = not local and not comprehension
    if isinstance(node, ast.Name):
        if isinstance(node.ctx, (ast.Store, ast.Del)) and visible:
            names.add(node.id)
        return
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        names.update(node.names)
        return
    # Unknown binding paths: any other node that carries a bound name as a plain string.
    if not isinstance(node, KNOWN_BINDERS) and not isinstance(node, (ast.Import, ast.ImportFrom)):
        for field in ("name", "asname", "rest", "id"):
            if isinstance(getattr(node, field, None), str):
                issues.add(UNVERIFIED_BINDING)
        if isinstance(getattr(node, "names", None), list) and any(isinstance(i, str) for i in node.names):
            issues.add(UNVERIFIED_BINDING)
    if isinstance(node, (ast.Attribute, ast.Subscript)):
        if isinstance(node.ctx, (ast.Store, ast.Del)) and visible:
            issues.add(ATTRIBUTE_BINDING)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            if alias.name == "*":
                issues.add(UNVERIFIED_BINDING)
            elif visible:
                names.add((alias.asname or alias.name).split(".")[0])
        return
    elif isinstance(node, ast.ExceptHandler):
        if node.name and visible:
            names.add(node.name)
    elif isinstance(node, ast.NamedExpr):
        if isinstance(node.target, ast.Name) and not local:
            names.add(node.target.id)
        collect_bindings(node.value, names, issues, local, comprehension)
        return
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        if visible:
            names.add(node.name)
        for child in ast.iter_child_nodes(node):
            collect_bindings(child, names, issues, local or any(child is item for item in node.body), comprehension)
        return
    elif isinstance(node, ast.Lambda):
        collect_bindings(node.args, names, issues, local, comprehension)
        collect_bindings(node.body, names, issues, True, comprehension)
        return
    elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        for child in ast.iter_child_nodes(node):
            collect_bindings(child, names, issues, local, True)
        return
    else:
        if visible and isinstance(node, MATCH_BINDERS):
            for field in ("name", "rest"):
                value = getattr(node, field, None)
                if isinstance(value, str):
                    names.add(value)
    for child in ast.iter_child_nodes(node):
        collect_bindings(child, names, issues, local, comprehension)


def defined_names(before, removed):
    """(names, issues): names bound by statements lying entirely inside the removed lines.

    issues lists reasons the result cannot be trusted as complete. None when the source cannot be parsed.
    Partially removed statements are descended into; only fully removed ones are collected.
    """
    try:
        tree = ast.parse(before)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    names, issues = set(), set()

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, STATEMENT_UNITS):
                first, last = node_span(child)
                if set(range(first, last + 1)) <= removed:
                    collect_bindings(child, names, issues)
                    continue
            visit(child)

    visit(tree)
    return names, sorted(issues)


def symbol_problems(name, before, item, listed):
    """Reasons why the listed symbols cannot vouch for the deletion (empty list means they can)."""
    reasons = []
    suffix = PurePosixPath(name.lower()).suffix
    removed = removed_numbers(before, item)
    lines = lines_for(before)
    text = b"".join(lines[number - 1] for number in sorted(removed)).decode("utf-8", "replace")
    for symbol in listed:
        if not re.search(r"(?<![A-Za-z0-9_])" + re.escape(symbol) + r"(?![A-Za-z0-9_])", text):
            reasons.append(f"{symbol}: does not appear in the deleted lines")
    if not deletes_code(name, before, item):
        return reasons
    if suffix != ".py":
        reasons.append("deleted definitions cannot be verified in this language")
        return reasons
    result = defined_names(before, removed)
    if result is None:
        reasons.append("Python file could not be parsed")
        reasons.append(UNVERIFIED_BINDING)
        return reasons
    names, issues = result
    reasons.extend(issues)
    missing = sorted(names - set(listed))
    long_missing = [value for value in missing if SYMBOL.fullmatch(value)]
    short_missing = [value for value in missing if not SYMBOL.fullmatch(value)]
    if long_missing:
        reasons.append("deleted names missing from symbols: " + ", ".join(long_missing))
    if short_missing:
        reasons.append("short names not listed in symbols, so they could not be searched: " + ", ".join(short_missing))
    return reasons


STAR_IMPORT_REASON = "the origin of names cannot be resolved because of from … import *"


def star_imports(root, files, overrides):
    """[(path, module, level)] for every `from x import *` that remains after the selection is applied."""
    found = []
    for name in sorted(files):
        if PurePosixPath(name.lower()).suffix != ".py":
            continue
        data = overrides[name] if name in overrides else read_source(root, name)
        if data is None:
            continue
        try:
            tree = ast.parse(data)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
                found.append((name, node.module or "", node.level))
    return found


def star_affects(name, stars):
    """A star import makes name sources unresolvable for its own file and for the module it imports from."""
    path = PurePosixPath(name)
    module = path.parent.name if path.stem == "__init__" else path.stem
    for star_path, star_module, level in stars:
        if star_path == name:
            return True
        if star_module.split(".")[-1] == module or (not star_module and level and PurePosixPath(star_path).parent == path.parent):
            return True
    return False


DYNAMIC_IMPORT = re.compile(r"importlib|__import__|import_module|sys\.modules")


def word_pattern(text):
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(text) + r"(?![A-Za-z0-9_])")


def module_name(path):
    posix = PurePosixPath(path)
    return posix.parent.name if posix.stem == "__init__" else posix.stem


def identifier_counts(tree, symbol):
    """Per line: how many AST identifiers are named `symbol` (uses, bindings, attributes, parameters, imports)."""
    counts = {}

    def add(line):
        counts[line] = counts.get(line, 0) + 1

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == symbol:
            add(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == symbol:
            add(node.end_lineno)
        elif isinstance(node, (ast.arg, ast.keyword)) and node.arg == symbol:
            add(node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol:
            add(node.lineno)
        elif isinstance(node, ast.alias):
            for part in node.name.split("."):
                if part == symbol:
                    add(node.lineno)
            if node.asname == symbol:
                add(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            for part in (node.module or "").split("."):
                if part == symbol:
                    add(node.lineno)
        elif isinstance(node, ast.ExceptHandler) and node.name == symbol:
            add(node.lineno)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for item in node.names:
                if item == symbol:
                    add(node.lineno)
    return counts


def file_unrelated(data, symbol, module):
    """True when a file's matches of `symbol` are provably its own identifiers, not the candidate module's.

    Conservative on purpose. All of these must hold: the file parses as Python, it never mentions the candidate
    module's name (not even in a string or comment), it has no dynamic-import machinery, and every textual match
    of the symbol is an identifier in the AST (so no match hides in a string or comment).
    """
    try:
        tree = ast.parse(data)
        text = data.decode("utf-8")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    if word_pattern(module).search(text) or DYNAMIC_IMPORT.search(text):
        return False
    counts = identifier_counts(tree, symbol)
    pattern = word_pattern(symbol)
    for number, line in enumerate(text.split("\n"), 1):
        found = len(pattern.findall(line))
        if found and counts.get(number, 0) != found:
            return False
    return True


def hit_content(root, files, excluded, overrides, path):
    if path in files:
        return overrides[path] if path in overrides else read_source(root, path)
    if path in excluded:
        return load_file(root, path, REFS_MAX_BYTES)[1]
    return None


def review_statuses(root, files, excluded, selected, ops):
    """Per-candidate review status and notes, computed against the post-edit state of the whole selection."""
    overrides = {name: after for name, _, after in ops}
    wanted = {name: candidate_symbols(item) for item, (name, _, _) in zip(selected, ops)}
    symbols = sorted({symbol for values in wanted.values() for symbol in values})
    scan = reference_scan(root, files, excluded, symbols, overrides)
    unreviewed = [name for name, _ in scan["not_reviewed"] if PurePosixPath(name.lower()).suffix in SOURCE | TEMPLATE]
    stars = star_imports(root, files, overrides)
    statuses, notes = {}, []
    for item, (name, before, _) in zip(selected, ops):
        reasons = []
        is_python = PurePosixPath(name.lower()).suffix == ".py"
        module = module_name(name)
        if deletes_code(name, before, item) and not wanted[name]:
            reasons.append("code is deleted but symbols is empty")
        reasons.extend(symbol_problems(name, before, item, wanted[name]))
        if is_python and deletes_code(name, before, item) and star_affects(name, stars):
            reasons.append(STAR_IMPORT_REASON)
        for symbol in wanted[name]:
            relevant_scope, relevant_outside, ignored = False, False, []
            hits = [(path, True) for path in scan["in_scope"][symbol]] + \
                   [(path, False) for path in scan["excluded_matches"][symbol]]
            for path, in_scope in hits:
                unrelated = False
                if is_python and path != name and PurePosixPath(path.lower()).suffix == ".py":
                    data = hit_content(root, files, excluded, overrides, path)
                    unrelated = data is not None and file_unrelated(data, symbol, module)
                if unrelated:
                    ignored.append(path)
                elif in_scope:
                    relevant_scope = True
                else:
                    relevant_outside = True
            if relevant_scope:
                reasons.append(f"{symbol}: matches outside the deleted range")
            if relevant_outside:
                reasons.append(f"{symbol}: matches in out-of-scope files")
            if ignored:
                notes.append(f"{item['id']}: {symbol}: ignored matches in {len(ignored)} Python file(s) that never "
                             f"reference module '{module}': " + ", ".join(sorted(ignored)[:5])
                             + (" ..." if len(ignored) > 5 else ""))
        if unreviewed:
            reasons.append(f"unreviewable source or template files exist ({len(unreviewed)})")
        statuses[item["id"]] = "REVIEW_COMPLETE" if not reasons else "REVIEW_INCOMPLETE(" + "; ".join(reasons) + ")"
    return statuses, notes


def load_json(path):
    with Path(path).open(encoding="utf-8-sig") as source:
        return json.load(source)


def sha_or_none(data):
    return None if data is None else digest(data)


def current_bytes(root, name):
    """Current content of a tracked in-scope file, or None when it does not exist."""
    path = safe_path(root, name)
    if not os.path.lexists(path):
        return None
    return read_bytes(root, name)


def file_mode(root, name):
    path = Path(root) / name
    if not os.path.lexists(path):
        return "100644"
    info = path.lstat()
    return "100755" if info.st_mode & stat.S_IXUSR and os.name != "nt" else "100644"


def base_commit(root):
    try:
        return git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    except Refusal:
        return None


def selected_plan(root, plan_path, ids):
    reject_inside_project(root, plan_path, "The plan file")
    plan = load_json(plan_path)
    snapshot, files, excluded = inventory(root)
    if not isinstance(plan, dict) or plan.get("snapshot") != snapshot:
        raise Refusal("The snapshot digest does not match. Review the project again.")
    if not isinstance(plan.get("candidates"), list):
        raise Refusal("The plan needs a candidates list.")
    candidates = {}
    for candidate in plan["candidates"]:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("id"), str):
            raise Refusal("Each candidate needs a string id.")
        key = candidate["id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", key) or key in candidates:
            raise Refusal("Candidate ids must be unique.")
        candidates[key] = candidate
    if len(set(ids)) != len(ids) or set(ids) - candidates.keys():
        raise Refusal("The selection has duplicate or unknown ids.")

    selected = [candidates[key] for key in ids]
    paths = set()
    ops = []
    spans = {}
    modes = {}
    for item in selected:
        name = item.get("path")
        if not isinstance(name, str) or name not in files or PurePosixPath(name.lower()).suffix not in SOURCE:
            raise Refusal("A selected file is outside the editable scope.")
        path_key = os.path.normcase(name)
        if path_key in paths:
            raise Refusal("Combine edits to the same file into a single candidate.")
        paths.add(path_key)
        before = read_source(root, name)
        if digest(before) != files[name]:
            raise Refusal("The file changed during the scan.")
        after = edited_content(item, before)
        ops.append((name, before, after))
        spans[name] = None if after is None else [tuple(span) for span in item["lines"]]
        modes[name] = file_mode(root, name)

    check_parent_directories(root, ops)
    status, notes = review_statuses(root, files, excluded, selected, ops)
    return {"snapshot": snapshot, "selected": selected, "ops": ops, "spans": spans, "modes": modes,
            "status": status, "notes": notes}


def check_parent_directories(root, changes):
    removed = {root / name for name, _, after in changes if after is None}
    for parent in {path.parent for path in removed} - {root}:
        if not any(entry not in removed for entry in parent.iterdir()):
            raise Refusal("The selection would empty a directory. Git would remove the directory too, so the selection was stopped.")


def lines_for(data):
    chunks = data.split(b"\n")
    return [chunk + b"\n" for chunk in chunks[:-1]] + ([chunks[-1]] if chunks[-1] else [])


def edited_content(item, data):
    if item.get("action") == "delete_file" and "lines" not in item:
        return None
    ranges = item.get("lines")
    if item.get("action") != "remove_lines" or not isinstance(ranges, list) or not ranges:
        raise Refusal("The action must be remove_lines or delete_file.")
    lines = lines_for(data)
    kept = []
    cursor = 0
    for span in ranges:
        if not isinstance(span, list) or len(span) != 2 or any(type(number) is not int for number in span):
            raise Refusal("A line range needs two integers.")
        first, last = span
        if not cursor < first <= last <= len(lines):
            raise Refusal("Line ranges must be valid, sorted and non-overlapping.")
        kept.extend(lines[cursor:first - 1])
        cursor = last
    return b"".join(kept + lines[cursor:])


def file_patch(name, before, after, mode="100644"):
    """Patch for one file. before None creates the file, after None deletes it."""
    path = name.encode("utf-8")
    old, new = b"a/" + path, b"b/" + path
    patch = [b"diff --git " + old + b" " + new + b"\n"]
    if before is None:
        patch.append(b"new file mode " + mode.encode("ascii") + b"\n")
    elif after is None:
        patch.append(b"deleted file mode " + mode.encode("ascii") + b"\n")
    diff = difflib.diff_bytes(
        difflib.unified_diff, lines_for(before or b""), lines_for(after or b""),
        (b"/dev/null" if before is None else old) + b"\t", (b"/dev/null" if after is None else new) + b"\t",
    )
    for line in diff:
        patch.append(line if line.endswith(b"\n") else line + b"\n\\ No newline at end of file\n")
    return b"".join(patch)


DIRECTIVE_PATTERNS = [
    r"^#!",
    r"type:\s*ignore", r"noqa", r"pylint:", r"mypy:", r"pyright:", r"fmt:\s*(off|on|skip)", r"isort:",
    r"eslint-", r"@ts-(ignore|expect-error|nocheck)", r"prettier-ignore", r"istanbul ignore", r"c8 ignore",
    r"jshint", r"@flow", r"@jsx",
    r"//go:", r"\+build", r"nolint", r"#\[allow", r"#!\[",
    r"NOSONAR", r"swiftlint:", r"rubocop:", r"pragma", r"@license", r"@preserve", r"SPDX-License-Identifier",
    r"<!--\s*\[if",
]
DIRECTIVES = [re.compile(pattern, re.I) for pattern in DIRECTIVE_PATTERNS]
# PEP 263: encoding declaration is only a comment on the first two lines.
CODING_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+", re.I)


def syntax_status(name, data):
    """PASSED, ERROR or UNVERIFIED. Only structural checks; nothing is executed."""
    suffix = PurePosixPath(name.lower()).suffix
    try:
        if suffix == ".py":
            ast.parse(data)
        elif suffix == ".json":
            json.loads(data.decode("utf-8-sig"))
        elif suffix == ".toml":
            try:
                import tomllib
            except ImportError:
                return "UNVERIFIED"
            tomllib.loads(data.decode("utf-8-sig"))
        else:
            return "UNVERIFIED"
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return "ERROR"
    return "PASSED"


def removed_line_numbers(before, after, spans):
    """1-based line numbers of `before` that are not in `after`."""
    if spans:
        return {number for first, last in spans for number in range(first, last + 1)}
    old, new = lines_for(before), lines_for(after)
    if len(old) <= len(new):
        return set()
    removed = set()
    for tag, first, last, _, _ in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in {"delete", "replace"}:
            removed.update(range(first + 1, last + 1))
    return removed


def directive_violations(before, removed):
    lines = lines_for(before)
    found = []
    for number in sorted(removed):
        text = lines[number - 1].decode("utf-8", "replace")
        if number <= 2 and CODING_COOKIE.search(text):
            found.append((number, CODING_COOKIE.pattern))
            continue
        for pattern in DIRECTIVES:
            if pattern.search(text):
                found.append((number, pattern.pattern))
                break
    return found


def docstring_violations(before, removed):
    """Docstring lines being removed while their owner definition stays."""
    try:
        tree = ast.parse(before)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return []
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if not (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            continue
        doc = set(range(body[0].lineno, body[0].end_lineno + 1))
        if not doc & removed:
            continue
        if not isinstance(node, ast.Module):
            first = min([item.lineno for item in node.decorator_list] + [node.lineno])
            if set(range(first, node.end_lineno + 1)) <= removed:
                continue
        found.append(body[0].lineno)
    return sorted(found)


def check_change(name, before, after, spans):
    """Pre-write validation. Raises Refusal on a detectable break; returns the syntax result text."""
    if after is None:
        return "file deletion, no syntax check"
    result = syntax_status(name, after)
    if before is None:
        return result
    removed = removed_line_numbers(before, after, spans)
    if removed:
        hits = directive_violations(before, removed)
        if hits:
            raise Refusal(f"{name}: deleted lines contain a directive or protection pattern (line "
                          + ", ".join(str(number) for number, _ in hits) + "). Candidate rejected.")
        if PurePosixPath(name.lower()).suffix == ".py":
            docs = docstring_violations(before, removed)
            if docs:
                raise Refusal(f"{name}: a docstring is deleted but its owner stays (line "
                              + ", ".join(map(str, docs)) + "). Candidate rejected.")
    if syntax_status(name, before) == "PASSED" and result == "ERROR":
        raise Refusal(f"{name}: the change introduces a syntax error. Candidate rejected.")
    return result


def prepare_commit(root, ops, label, extra=None, modes=None, spans=None, verified=False):
    """Validate ops and compute patches and the approval digest. Shared by review, apply, undo, recover.

    verified=True is for writes that restore content from a hash-verified state-dir backup (undo, recover):
    syntax and directive checks are skipped, because the original content must always be restorable.
    """
    extra = extra or {}
    modes = modes or {}
    spans = spans or {}
    names = [name for name, _, _ in ops]
    if not ops or len(set(map(os.path.normcase, names))) != len(names):
        raise Refusal("The operation list is empty or has duplicate files.")
    patches = {}
    report = []
    for name, current, target in ops:
        if current is None and target is None:
            raise Refusal("Invalid operation.")
        if verified:
            report.append((name, "restored from verified backup, checks skipped"))
        else:
            report.append((name, check_change(name, current, target, spans.get(name))))
        patches[name] = file_patch(name, current, target, modes.get(name) or file_mode(root, name))
    patch = b"".join(patches[name] for name in names)
    approval = digest(encoded({
        "action": label, "root": str(root), "base_commit": base_commit(root),
        "ops": [[name, sha_or_none(current), sha_or_none(target)] for name, current, target in ops],
        "status": extra.get("status", {}), "mode": extra.get("mode"), "patch": digest(patch),
    }))
    apply_patch(root, patch, check=True)
    return {"patches": patches, "patch": patch, "approval": approval, "report": report}


def confirm(action, approval):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise Refusal("Enter the confirmation in your own interactive terminal.")
    phrase = f"{action} {approval}"
    print(f"To confirm this operation, enter this line:\n{phrase}")
    if input("> ") != phrase:
        raise Refusal("Confirmation did not match.")


def apply_patch(root, patch, *, reverse=False, check=False):
    flags = ["--whitespace=nowarn"]
    if reverse:
        flags.append("--reverse")
    if check:
        flags.append("--check")
    git(root, "apply", *flags, "-", payload=patch)


# --- State: receipts, journal, index -------------------------------------------------------

RECEIPT_ID = re.compile(r"[0-9a-f]{32}")


def receipts_dir():
    return state_dir() / "receipts"


def index_path():
    return state_dir() / "index.jsonl"


def write_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def read_index():
    path = index_path()
    if not path.exists():
        return []
    entries = []
    for line in path.read_bytes().splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def same_root(left, right):
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


class Journal:
    """Per-receipt write journal. Status: pending, partial, complete, reverted."""

    def __init__(self, directory, direction, creator=None):
        self.directory = Path(directory)
        self.receipt_id = self.directory.name
        self.direction = direction
        self.creator = creator
        self.files = {}
        self.status = "pending"
        self.resume = False
        self.success_state = "done"

    @classmethod
    def load(cls, directory):
        try:
            data = json.loads((Path(directory) / "journal.json").read_bytes())
            journal = cls(directory, data["direction"])
            journal.status = data["status"]
            journal.files = dict(data["files"])
        except (OSError, ValueError, KeyError, TypeError):
            raise Refusal("The journal could not be read. The receipt is not trustworthy.") from None
        if journal.direction not in {"apply", "undo"} or journal.status not in {"pending", "partial", "complete", "reverted"}:
            raise Refusal("The journal is invalid.")
        return journal

    def save(self):
        write_atomic(self.directory / "journal.json", encoded({
            "receipt_id": self.receipt_id, "direction": self.direction, "status": self.status, "files": self.files,
        }))

    def start(self, ops):
        publish = self.creator(ops) if self.creator else None
        if not self.resume:
            self.files = {name: "todo" for name, _, _ in ops}
        self.status = "pending"
        self.save()
        if publish:
            publish()

    def mark(self, name, state):
        self.files[name] = state
        self.save()

    def finish(self):
        states = set(self.files.values())
        if "failed" in states:
            self.status = "partial"
        elif states == {"done"}:
            self.status = "complete"
        elif states == {"todo"}:
            self.status = "reverted"
        else:
            self.status = "partial"
        self.save()


def ensure_unlocked(root):
    """Refuse new work while a pending/partial journal exists for this project."""
    for entry in read_index():
        if not same_root(entry.get("root", ""), root) or not RECEIPT_ID.fullmatch(str(entry.get("receipt_id"))):
            continue
        directory = receipts_dir() / entry["receipt_id"]
        if not (directory / "journal.json").exists():
            print(f"Warning: the journal of receipt {entry['receipt_id']} was not found (it may have been deleted); not counted as a lock.")
            continue
        journal = Journal.load(directory)
        if journal.status in {"pending", "partial"}:
            raise Refusal(
                f"An interrupted operation exists (receipt {entry['receipt_id']}). "
                f"Resolve it first with `recover {root} {entry['receipt_id']} --forward` or `--backward`."
            )


def commit_changes(root, ops, label, journal, *, expected=None, extra=None, modes=None, spans=None,
                   preflight=None, verified=False):
    """The only write path. ops: [(name, current_bytes_or_None, target_bytes_or_None)]."""
    prepared = prepare_commit(root, ops, label, extra, modes, spans, verified)
    for name, result in prepared["report"]:
        print(f"Syntax | {name} | {result}")
    print("Patch:")
    show_patch(prepared["patch"])
    approval = prepared["approval"]
    print(f"Approval digest {approval}")
    if expected is not None and expected != approval:
        raise Refusal("The digest does not match. Re-check the review output.")
    confirm(label, approval)
    if preflight:
        preflight()
    for name, current, _ in ops:
        if sha_or_none(current_bytes(root, name)) != sha_or_none(current):
            raise Refusal(f"The file changed during confirmation: {name}")
    apply_patch(root, prepared["patch"], check=True)
    journal.start(ops)
    print(f"Receipt id {journal.receipt_id}", flush=True)
    for name, _, target in ops:
        try:
            apply_patch(root, prepared["patches"][name])
            if sha_or_none(current_bytes(root, name)) != sha_or_none(target):
                raise Refusal("Content after writing is not as expected.")
        except (Refusal, OSError) as error:
            journal.mark(name, "failed")
            journal.finish()
            raise Refusal(
                f"Writing stopped at {name} ({error}). Journal status: {journal.status}. "
                f"To resolve: recover {root} {journal.receipt_id} --forward or --backward"
            ) from None
        journal.mark(name, journal.success_state)
    journal.finish()


# --- Receipts and undo -----------------------------------------------------------------------

def create_receipt(root, receipt_id, directory, ops, modes):
    base = base_commit(root)
    entries = []
    for number, (name, current, target) in enumerate(ops):
        if current is None:
            raise Refusal("A receipt can only be created for existing files.")
        blob = None
        if base:
            try:
                candidate = git(root, "rev-parse", f"{base}:{name}").decode("ascii").strip()
                if digest(git(root, "cat-file", "blob", candidate)) == digest(current):
                    blob = candidate
            except Refusal:
                blob = None
        entries.append({
            "index": number, "name": name, "before_sha": digest(current), "after_sha": sha_or_none(target),
            "base_blob": blob, "mode": modes.get(name) or file_mode(root, name),
        })

    def create(final_ops):
        for number, (_, current_data, target_data) in enumerate(final_ops):
            write_atomic(directory / "before" / f"{number}.bin", current_data)
            if target_data is not None:
                write_atomic(directory / "after" / f"{number}.bin", target_data)
        created = datetime.datetime.now(datetime.timezone.utc).isoformat()
        receipt = encoded({
            "version": 1, "receipt_id": receipt_id, "root": str(root), "base_commit": base,
            "files": entries, "created": created,
        })
        write_atomic(directory / "receipt.json", receipt)

        def publish():
            line = encoded({"receipt_id": receipt_id, "root": str(root),
                            "receipt_sha256": digest(receipt), "created": created}) + b"\n"
            with open(index_path(), "ab") as output:
                output.write(line)
                output.flush()
                os.fsync(output.fileno())
        return publish
    return create


def load_receipt(root, receipt_id):
    """Load a receipt from the state directory only, verifying it against index.jsonl."""
    if not isinstance(receipt_id, str) or not RECEIPT_ID.fullmatch(receipt_id):
        raise Refusal("The receipt id must be 32 lowercase hexadecimal characters.")
    matches = [entry for entry in read_index() if entry.get("receipt_id") == receipt_id]
    if len(matches) != 1:
        raise Refusal("The receipt was not found in the state directory, or its id is duplicated.")
    directory = receipts_dir() / receipt_id
    try:
        raw = (directory / "receipt.json").read_bytes()
        receipt = json.loads(raw)
    except (OSError, ValueError):
        raise Refusal("The receipt file could not be read.") from None
    if matches[0].get("receipt_sha256") != digest(raw):
        raise Refusal("The receipt digest does not match the index digest. The receipt is not trustworthy.")
    if (not isinstance(receipt, dict) or receipt.get("receipt_id") != receipt_id or receipt.get("version") != 1
            or not same_root(receipt.get("root", ""), root) or not same_root(matches[0].get("root", ""), root)):
        raise Refusal("The receipt does not belong to this project.")
    if not isinstance(receipt.get("files"), list) or not receipt["files"]:
        raise Refusal("The receipt file list is invalid.")
    return receipt, directory


def load_backups(receipt, directory):
    """Read before/after backups from the state dir and verify them against the receipt hashes."""
    items = []
    try:
        for number, entry in enumerate(receipt["files"]):
            name = entry["name"]
            if not eligible(name) or entry.get("index") != number:
                raise Refusal("A path in the receipt is invalid.")
            before = after = None
            if entry["before_sha"] is not None:
                before = (directory / "before" / f"{number}.bin").read_bytes()
                if digest(before) != entry["before_sha"]:
                    raise Refusal(f"The backup of the previous content is corrupted: {name}")
            if entry["after_sha"] is not None:
                after = (directory / "after" / f"{number}.bin").read_bytes()
                if digest(after) != entry["after_sha"]:
                    raise Refusal(f"The backup of the resulting content is corrupted: {name}")
            items.append({"name": name, "before": before, "after": after, "mode": entry.get("mode", "100644"),
                          "base_blob": entry.get("base_blob")})
    except (KeyError, TypeError, OSError):
        raise Refusal("Receipt backups could not be read or are missing.") from None
    return items


def check_base_blobs(root, receipt, items):
    """Second cross-check against the pinned commit. The state-dir backup stays the source of truth.

    Returns True only when every file was verified against the commit. The check is skipped (False) when the
    receipt has no base_commit, the commit is unreachable, or a file has no recorded blob. Anyone who can
    rewrite the receipt can also force the skip, so use `undo --require-git-check` when that matters.
    """
    base = receipt.get("base_commit")
    if not base:
        print("Git cross-check: not possible (no base_commit in the receipt). Using the backup only.")
        return False
    try:
        git(root, "cat-file", "-e", f"{base}^{{commit}}")
    except Refusal:
        print("Git cross-check: not possible (commit unreachable). Using the backup only.")
        return False
    complete = True
    for item in items:
        if not item["base_blob"]:
            complete = False
            continue
        found = git(root, "rev-parse", f"{base}:{item['name']}").decode("ascii").strip()
        if found != item["base_blob"] or git(root, "cat-file", "blob", found) != item["before"]:
            raise Refusal(f"Git cross-check mismatch: {item['name']}")
    if complete:
        print("Git cross-check: OK.")
    else:
        print("Git cross-check: OK for files with a recorded blob; some files had none (uncommitted changes).")
    return complete


def undo(root, receipt_id, require_git_check=False):
    prepare_state(root)
    ensure_unlocked(root)
    receipt, directory = load_receipt(root, receipt_id)
    journal = Journal.load(directory)
    if journal.status in {"pending", "partial"}:
        raise Refusal(f"This receipt is interrupted. Use recover {root} {receipt_id}.")
    if not ((journal.direction == "apply" and journal.status == "complete")
            or (journal.direction == "undo" and journal.status == "reverted")):
        raise Refusal("This receipt cannot be undone (already undone, or never applied).")
    items = load_backups(receipt, directory)
    verified_by_git = check_base_blobs(root, receipt, items)
    if require_git_check and not verified_by_git:
        raise Refusal("--require-git-check: the Git cross-check could not verify every file, so undo was refused.")
    ops, changed = [], []
    for item in items:
        current = current_bytes(root, item["name"])
        if sha_or_none(current) != sha_or_none(item["after"]):
            changed.append(item["name"])
        ops.append((item["name"], current, item["before"]))
    if changed:
        raise Refusal("Files changed after apply (user changes): " + ", ".join(changed))
    print("Files to undo:\n" + "\n".join(name for name, _, _ in ops))
    commit_changes(root, ops, "UNDO", Journal(directory, "undo"), modes={i["name"]: i["mode"] for i in items},
                   verified=True)
    print("The selected cleanup was undone.")


def recover(root, receipt_id, mode):
    prepare_state(root)
    receipt, directory = load_receipt(root, receipt_id)
    journal = Journal.load(directory)
    if journal.status not in {"pending", "partial"}:
        raise Refusal("This receipt does not need recovery.")
    items = load_backups(receipt, directory)
    apply_direction = journal.direction == "apply"
    ops, user_changes = [], []
    for item in items:
        origin, goal = (item["before"], item["after"]) if apply_direction else (item["after"], item["before"])
        current = current_bytes(root, item["name"])
        if sha_or_none(current) == sha_or_none(goal):
            journal.files[item["name"]] = "done"
            if mode == "backward":
                ops.append((item["name"], goal, origin))
        elif sha_or_none(current) == sha_or_none(origin):
            journal.files[item["name"]] = "todo"
            if mode == "forward":
                ops.append((item["name"], origin, goal))
        else:
            user_changes.append(item["name"])
    if user_changes:
        raise Refusal("Some files have user changes in between; no file was touched: " + ", ".join(user_changes))
    journal.resume = True
    journal.success_state = "done" if mode == "forward" else "todo"
    if not ops:
        journal.finish()
        print(f"No files to recover. Journal status: {journal.status}.")
        return
    print("Files to recover:\n" + "\n".join(name for name, _, _ in ops))
    commit_changes(root, ops, "RECOVER", journal, extra={"mode": mode}, modes={i["name"]: i["mode"] for i in items},
                   verified=True)
    print(f"Recovery finished. Journal status: {journal.status}.")


# --- Command line ------------------------------------------------------------------------------

LIMITS_TEXT = (
    "Limits of these results: the syntax check only catches certain structural errors in supported languages (.py, .json, .toml); "
    "in other languages the result is UNVERIFIED, which does not mean passed. REVIEW_COMPLETE only says "
    "that no reference outside the deleted range was found in any searchable file; it is not evidence about dynamic access, reflection or "
    "consumers outside the project. Behavior tests that you run provide additional evidence; "
    "uncertainty remains for uses the tests do not cover. That a file can be parsed does not prove that the deletion "
    "preserves behavior."
)


def show(plan):
    for item, (name, _, _) in zip(plan["selected"], plan["ops"]):
        lines = item.get("lines")
        detail = "file deletion" if lines is None else "lines " + ", ".join(f"{first}-{last}" for first, last in lines)
        print(f"{item['id']} | {name} | {detail} | {plan['status'][item['id']]}")
    for note in plan.get("notes", []):
        print("Note | " + note)


def safe_text(data):
    """Display-safe text for a patch: control, format (bidi) and other invisible characters are shown escaped.

    Only the display changes; the approval digest and the written patch use the real bytes.
    """
    text = data.decode("utf-8", "replace").replace("\r\n", "\n")
    out = []
    for char in text:
        if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"}:
            out.append(char)
        else:
            code = ord(char)
            if code < 0x100:
                out.append("\\x%02x" % code)
            elif code < 0x10000:
                out.append("\\u%04x" % code)
            else:
                out.append("\\U%08x" % code)
    return "".join(out)


def show_patch(data):
    sys.stdout.write(safe_text(data))
    sys.stdout.flush()


def write_out(data):
    """Write bytes to stdout even when stdout has no binary buffer (tests)."""
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        sys.stdout.flush()
        buffer.write(data)
        buffer.flush()
    else:
        sys.stdout.write(data.decode("utf-8", "replace"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Approved code cleanup")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("snapshot", "read", "refs", "review", "apply", "undo", "recover"):
        command = commands.add_parser(name)
        command.add_argument("project")
        if name == "read":
            command.add_argument("file")
        if name in {"review", "apply"}:
            command.add_argument("plan")
            command.add_argument("--ids", nargs="+", required=True)
        if name == "refs":
            command.add_argument("symbol")
        if name == "review":
            command.add_argument("--diff", action="store_true")
        if name == "apply":
            command.add_argument("--digest", required=True)
        if name in {"undo", "recover"}:
            command.add_argument("receipt_id")
        if name == "undo":
            command.add_argument("--require-git-check", action="store_true",
                                 help="refuse unless the pinned Git commit verifies every restored file")
        if name == "recover":
            group = command.add_mutually_exclusive_group(required=True)
            group.add_argument("--forward", action="store_true")
            group.add_argument("--backward", action="store_true")
    args = parser.parse_args(argv)
    root = project(args.project)
    if args.command in {"snapshot", "read"}:
        snapshot, files, excluded = inventory(root)
        if args.command == "snapshot":
            print(json.dumps({"snapshot": snapshot, "files": sorted(files), "excluded": excluded_report(excluded),
                              "excluded_count": len(excluded)}, ensure_ascii=False, indent=2))
        else:
            if args.file not in files:
                raise Refusal("The file is not in the review scope.")
            data = read_source(root, args.file)
            if digest(data) != files[args.file]:
                raise Refusal("The file changed after the scan.")
            write_out(data)
        return
    if args.command == "refs":
        print(json.dumps(refs(root, args.symbol), ensure_ascii=False, indent=2))
        return
    if args.command == "undo":
        undo(root, args.receipt_id, args.require_git_check)
        return
    if args.command == "recover":
        recover(root, args.receipt_id, "forward" if args.forward else "backward")
        return

    plan = selected_plan(root, args.plan, args.ids)
    show(plan)
    if args.command == "review":
        prepared = prepare_commit(root, plan["ops"], "APPLY", {"status": plan["status"]}, plan["modes"], plan["spans"])
        for name, result in prepared["report"]:
            print(f"Syntax | {name} | {result}")
        print(f"Approval digest {prepared['approval']}")
        if args.diff:
            show_patch(prepared["patch"])
        print(LIMITS_TEXT)
        return
    incomplete = [key for key, value in plan["status"].items() if value.startswith("REVIEW_INCOMPLETE")]
    if incomplete:
        raise Refusal("REVIEW_INCOMPLETE candidates cannot be applied (" + ", ".join(incomplete)
                      + "). Complete the evidence (symbols, refs), then regenerate the plan and review again.")
    prepare_state(root)
    ensure_unlocked(root)
    receipt_id = secrets.token_hex(16)
    directory = receipts_dir() / receipt_id
    ops = plan["ops"]

    def preflight():
        if inventory(root)[0] != plan["snapshot"]:
            raise Refusal("The project changed during confirmation. Review again.")
        check_parent_directories(root, ops)

    journal = Journal(directory, "apply", create_receipt(root, receipt_id, directory, ops, plan["modes"]))
    commit_changes(root, ops, "APPLY", journal, expected=args.digest, extra={"status": plan["status"]},
                   modes=plan["modes"], spans=plan["spans"], preflight=preflight)
    print("The approved cleanup was applied.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        main()
    except (Refusal, OSError, ValueError, KeyError, IndexError, TypeError, EOFError, subprocess.TimeoutExpired) as error:
        print(f"Stopped. {error}", file=sys.stderr)
        sys.exit(1)
