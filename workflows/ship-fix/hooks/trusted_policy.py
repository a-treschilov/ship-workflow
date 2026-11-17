#!/usr/bin/env python3
"""Approve simple trusted workflow helpers and repository-bound test/lint tools.

Unknown commands fall through to normal permissions. Approved tools execute
trusted repository code and test setup; they are not guaranteed read-only.
"""

import json
import os
from pathlib import Path
import shlex


def resolve_root() -> Path:
    """Find the project root from SHIP_ROOT or the nearest .agents marker, not install depth."""
    override = os.environ.get("SHIP_ROOT")
    if override and Path(override).is_dir():
        return Path(override).resolve()
    start = Path.cwd().resolve()
    for candidate in (start, *start.parents):
        if ((candidate / ".agents/agent-models.yaml").is_file()
                or (candidate / ".agents/workflows/ship-fix/scripts/ship_state.py").is_file()):
            return candidate
    return Path(__file__).resolve().parents[4]


ROOT = resolve_root()
GO_REPOS = {"api"}
NODE_REPOS = {"app", "landing", "mobile", "shared"}
NODE_TOOLS = {"jest": "jest", "eslint": "eslint", "tsc": "typescript", "prettier": "prettier"}
NPM_SCRIPTS = {"lint", "format:check", "build", "test", "sync", "typecheck"}
GENERAL_TOOLS = {"pwd", "cd", "ls", "cat", "head", "tail", "wc", "file", "stat",
                 "which", "date", "echo", "printf", "sort", "uniq", "cut", "tr",
                 "jq", "diff", "find", "grep", "rg", "awk", "sed"}
GIT_READ_ONLY = {"status", "diff", "log", "show", "remote", "ls-remote",
                 "ls-files", "rev-parse", "blame", "stash"}


def inside(path, root):
    return path.resolve().is_relative_to(root.resolve())


def operand(value, repo):
    if not value or value.startswith("-") or ".." in Path(value).parts:
        return False
    return inside(repo / value, repo)


def option_value(kind, value, repo):
    if not value or value.startswith("-"):
        return False
    if kind == "path":
        return operand(value, repo)
    if kind == "number":
        return value.isdigit() and int(value) > 0
    if kind == "boolean":
        return value in {"true", "false"}
    if kind == "colors":
        return value in {"never", "always", "auto"}
    if kind == "standard":
        return operand(value, repo) and (repo / value).is_file()
    if kind == "extensions":
        return all(part.isalnum() for part in value.split(","))
    return kind == "selector"


def arguments(args, repo, flags, values):
    positional = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--" and not positional:
            positional = True
        elif not positional and arg.startswith("-"):
            key, separator, value = arg.partition("=")
            if key in flags and not separator:
                index += 1
                continue
            if key not in values:
                return False
            if not separator:
                index += 1
                if index >= len(args):
                    return False
                value = args[index]
            if not option_value(values[key], value, repo):
                return False
        elif not operand(arg, repo):
            return False
        index += 1
    return True


def go_runner(tool, args, repo):
    if tool == "go":
        if args[:1] not in (["vet"], ["test"]):
            return False
        return arguments(args[1:], repo, set(), {"-run": "selector", "-bench": "selector"})
    return False


def general_runner(argv, cwd):
    # Read-only inspection tools; shell composition/expansion is already rejected.
    if argv[0] == "find":
        return not any(arg in {"-delete", "-exec", "-execdir", "-ok", "-okdir",
                               "-fprint", "-fprintf", "-fls"} for arg in argv[1:])
    if argv[0] == "sed":
        # `-n` only prints explicit ranges; `-i` edits in place.
        return "-n" in argv[1:] and "-i" not in argv[1:]
    return True


def git_runner(argv, cwd):
    if argv[:1] != ["git"] or len(argv) < 2:
        return False
    if argv[1] == "-C":
        if len(argv) < 4:
            return False
        base = Path(argv[2])
        if not base.is_absolute() or not inside(base, ROOT) or base not in {ROOT, *(ROOT / name for name in GO_REPOS | NODE_REPOS)}:
            return False
        argv = ["git", *argv[3:]]
    if argv[1] == "config":
        return len(argv) >= 3 and argv[2] == "--get"
    if argv[1] == "stash":
        return argv[2:] == ["list"]
    if argv[1] in GIT_READ_ONLY:
        # Read-only inspection: flags are free-form (`--oneline`, `-8`, …); operands
        # must stay repo-bound and output redirects/external diffs are rejected.
        deny = {"-o", "--ext-diff", "--no-ext-diff"}
        for arg in argv[2:]:
            if arg.startswith("-"):
                if arg in deny or arg.startswith("--output="):
                    return False
            elif not operand(arg, cwd):
                return False
        return True
    return False


def npm_runner(script, args, repo):
    if script not in NPM_SCRIPTS:
        return False
    # `npm run <script>` plus optional `--` passthrough args.
    return arguments(args, repo, set(), {})


def node_runner(tool, args, repo):
    package = repo / "node_modules" / NODE_TOOLS[tool] / "package.json"
    binary = repo / "node_modules" / ".bin" / tool
    if not package.is_file() or not binary.is_file():
        return False
    if not inside(package, repo) or not inside(binary, repo):
        return False
    metadata = json.loads(package.read_text())
    if not isinstance(metadata, dict) or metadata.get("name") != NODE_TOOLS[tool]:
        return False
    if tool == "jest":
        flags = {"--version", "--help", "--runInBand", "--runTestsByPath", "--ci",
                 "--silent", "--verbose", "--coverage", "--no-coverage", "--passWithNoTests",
                 "--listTests", "--detectOpenHandles", "--forceExit", "--no-cache"}
        values = {"--testNamePattern": "selector", "-t": "selector",
                  "--testPathPattern": "selector", "--testPathPatterns": "selector",
                  "--maxWorkers": "number", "--coverage": "boolean"}
    elif tool == "eslint":
        flags = {"--version", "--help", "--no-warn-ignored", "--quiet"}
        values = {"--max-warnings": "selector", "--ext": "selector"}
    elif tool == "tsc":
        flags = {"--version", "--help", "--noEmit", "--skipLibCheck"}
        values = {"--pretty": "boolean", "--incremental": "boolean", "--project": "path", "-p": "path"}
    else:
        flags = {"--version", "--help", "--check", "--list-different"}
        values = {}
        if not any(a in {"--check", "--list-different", "--version", "--help"} for a in args):
            return False
    return arguments(args, repo, flags, values)


def python_helper(argv, cwd):
    if cwd != ROOT or len(argv) < 4 or argv[:3] != ["python3", "-I", "-B"]:
        return False
    script = argv[3]
    if not script.startswith((".agents/workflows/ship-fix/scripts/", "./.agents/workflows/ship-fix/scripts/")):
        return False
    relative = Path(script)
    if ".." in relative.parts or relative.suffix != ".py":
        return False
    target = ROOT / relative
    return target.is_file() and target.resolve() == target and inside(
        target, ROOT / ".agents/workflows/ship-fix/scripts")


def simple_command(command):
    if any(ord(char) < 32 for char in command):
        return False
    quote = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = None
        elif char == "\\":
            # Only quote/backslash escapes are accepted; other forms fall through.
            if index + 1 >= len(command) or command[index + 1] not in {"'", '"', "\\"}:
                return False
            index += 1
        elif quote == '"':
            if char == '"':
                quote = None
            elif char in "$`!":
                return False
        elif char in {"'", '"'}:
            quote = char
        elif char in ";&|<>$`*?[]{}()!#~":
            return False
        index += 1
    return quote is None


def is_trusted(payload):
    if not isinstance(payload, dict):
        return False
    if payload.get("hook_event_name") != "PreToolUse" or payload.get("tool_name") != "Bash":
        return False
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or tool_input.get("dangerouslyDisableSandbox"):
        return False
    command = tool_input.get("command")
    cwd_text = payload.get("cwd")
    if not isinstance(command, str) or not isinstance(cwd_text, str):
        return False
    # Quoted punctuation is data; shell expansion, composition and redirection are not.
    if not simple_command(command):
        return False
    argv = shlex.split(command)
    if not argv:
        return False
    cwd = Path(cwd_text)
    allowed = {ROOT, *(ROOT / name for name in GO_REPOS | NODE_REPOS)}
    if cwd not in allowed:
        return False
    if not cwd.is_dir() or cwd.resolve() != cwd:
        return False
    trusted = python_helper(argv, cwd)
    if not trusted and argv[0] in GENERAL_TOOLS:
        trusted = general_runner(argv, cwd)
    if not trusted and argv[0] == "env" and argv[1:] == []:
        trusted = True
    if not trusted and argv[0] == "node" and argv[1:2] == ["--check"]:
        trusted = arguments(argv[2:], cwd, set(), {})
    if not trusted and argv[0] == "git":
        trusted = git_runner(argv, cwd)
    if cwd in {ROOT / name for name in GO_REPOS} and argv[0] == "go" and len(argv) >= 2:
        trusted = go_runner(argv[0], argv[1:], cwd)
    if cwd in {ROOT / name for name in NODE_REPOS} and argv[0] == "npm" and len(argv) >= 3 \
            and argv[1] == "run":
        trusted = npm_runner(argv[2], argv[3:], cwd)
    if cwd in {ROOT / name for name in NODE_REPOS} and argv[0] == "npx" and len(argv) >= 2 \
            and argv[1] in NODE_TOOLS:
        trusted = node_runner(argv[1], argv[2:], cwd)
    return bool(trusted)
