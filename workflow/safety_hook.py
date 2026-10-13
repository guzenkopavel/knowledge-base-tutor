#!/usr/bin/env python3
"""Узкая защита от случайной потери данных без проверок проекта."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import sys


TOOLS = {"Bash", "apply_patch", "Edit", "Write", "MultiEdit", "NotebookEdit"}
GIT_RISK = "Операция может безвозвратно удалить данные или переписать историю Git"
SECRET_RISK = "Запись затрагивает файл секретов или закрытый ключ"
DELETE_RISK = "Рекурсивное удаление затрагивает корень, родительский каталог или данные Git"


def sensitive(path: str) -> bool:
    """Определяет чувствительное имя без чтения содержимого"""
    name = Path(path).name.lower()
    if name in {".env.example", ".env.sample", ".env.template", ".env.dist"}:
        return False
    return (
        name == ".env" or name.startswith(".env.")
        or name in {"id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}
        or Path(name).suffix in {".p8", ".p12", ".pfx", ".key", ".pem"}
    )


def option(args: list[str], long: str, short: str = "") -> bool:
    """Ищет флаг только до разделителя путей"""
    for arg in args:
        if arg == "--":
            break
        if arg == long or arg.startswith(long + "="):
            return True
        if short and arg.startswith("-") and not arg.startswith("--") and short in arg[1:]:
            return True
    return False


def git_risk(args: list[str]) -> str | None:
    """Распознаёт опасные операции с обычными глобальными опциями Git"""
    while args and args[0].startswith("-"):
        flag = args.pop(0)
        if flag in {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}:
            if not args:
                return None
            args.pop(0)
        elif not (flag.startswith(("-C", "-c", "--git-dir=", "--work-tree=", "--namespace="))
                  or flag in {"--no-pager", "--bare", "--no-optional-locks"}):
            return None
    if not args:
        return None
    verb, rest = args[0], args[1:]
    flags = rest[:rest.index("--")] if "--" in rest else rest
    if option(rest, "--help", "h") or verb == "push" and option(rest, "--dry-run", "n"):
        return None
    risky = (
        verb == "reset" and option(rest, "--hard")
        or verb == "clean" and option(rest, "--force", "f") and not option(rest, "--dry-run", "n")
        or verb == "push" and (
            option(rest, "--force", "f") or option(rest, "--mirror")
            or any(arg.startswith("+") for arg in rest)
        )
        or verb == "branch" and (any(arg.startswith("-") and not arg.startswith("--") and "D" in arg[1:] for arg in flags)
                                 or option(rest, "--delete", "d") and option(rest, "--force", "f"))
        or verb == "stash" and bool(rest) and rest[0] in {"drop", "clear"}
        or verb == "restore" and (not option(rest, "--staged", "S") or option(rest, "--worktree", "W"))
        or verb == "checkout" and ("--" in rest or "." in rest or option(rest, "--force", "f"))
    )
    return GIT_RISK if risky else None


def broad_delete(path: str, cwd: Path, root: Path) -> bool:
    """Защищает широкие каталоги, не запрещая удаление обычной сборочной папки"""
    expanded = os.path.expanduser(path)
    resolved = Path(os.path.abspath(cwd / expanded))
    return (
        resolved in {root, Path.home(), Path("/")}
        or resolved in root.parents
        or path in {".", ".."}
        or ".git" in resolved.parts
    )


def shell_risk(command: str, cwd: Path, root: Path, depth: int = 0) -> str | None:
    """Проверяет распространённые формы shell; не является полным интерпретатором"""
    if depth > 2:
        return None
    lexer = shlex.shlex(command, posix=False, punctuation_chars=";&|<>()")
    lexer.whitespace = " \t\r"
    lexer.commenters = "#"
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    if any(token in {"<<", "<<-"} for token in tokens):
        return None
    segment: list[str] = []
    for token in tokens + [";"]:
        if token in {";", "&&", "||", "|", "&", "\n", "(", ")"}:
            risk = segment_risk(segment, cwd, root, depth)
            if risk:
                return risk
            segment = []
        else:
            segment.append(token)
    return None


def segment_risk(tokens: list[str], cwd: Path, root: Path, depth: int) -> str | None:
    """Проверяет одну команду, не исполняя её и не выводя её аргументы"""
    for index, token in enumerate(tokens[:-1]):
        if token in {">", ">>", ">|", "<>"} and sensitive(tokens[index + 1].strip("\"'")):
            return SECRET_RISK
    tokens = [token[1:-1] if len(token) >= 2 and token[0] in "\"'" and token[-1] == token[0] else token for token in tokens]
    while tokens and re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", tokens[0]):
        tokens = tokens[1:]
    if tokens and tokens[0] in {"command", "exec", "env"}:
        return segment_risk(tokens[1:], cwd, root, depth)
    if not tokens:
        return None
    executable, args = Path(tokens[0]).name, tokens[1:]
    if executable == "rtk":
        return segment_risk(args[1:] if args[:1] == ["proxy"] else args, cwd, root, depth)
    if executable == "git":
        return git_risk(args)
    if executable in {"bash", "sh", "zsh"}:
        for index, arg in enumerate(args[:-1]):
            if arg.startswith("-") and "c" in arg[1:]:
                return shell_risk(args[index + 1], cwd, root, depth + 1)
    targets = [arg for arg in args if not arg.startswith("-")]
    if executable == "rm" and (option(args, "--recursive", "r") or option(args, "--recursive", "R")):
        if any(broad_delete(path, cwd, root) for path in targets):
            return DELETE_RISK
    if executable in {"cp", "mv"}:
        targets = targets[-1:] if executable == "cp" else targets
    elif executable in {"sed", "perl"}:
        if not option(args, "--in-place", "i"):
            return None
    elif executable == "dd":
        targets = [arg[3:] for arg in args if arg.startswith("of=")]
    elif executable not in {"rm", "tee", "touch", "truncate"}:
        return None
    return SECRET_RISK if any(sensitive(path) for path in targets) else None


def assess(payload: dict, root: Path) -> str | None:
    """Проверяет поддерживаемый вызов Codex или Claude без записи состояния"""
    tool = payload.get("tool_name")
    if tool not in TOOLS:
        return None
    data = payload.get("tool_input")
    if not isinstance(data, dict):
        raise ValueError("missing tool_input")
    if tool == "Bash":
        command = data.get("command")
        if not isinstance(command, str):
            raise ValueError("missing command")
        cwd = Path(payload.get("cwd") or root)
        return shell_risk(command, cwd, root)
    if tool == "apply_patch":
        patch = data.get("command")
        if not isinstance(patch, str):
            raise ValueError("missing patch")
        paths = re.findall(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", patch, re.M)
    else:
        path = data.get("file_path") or data.get("notebook_path")
        if not isinstance(path, str):
            raise ValueError("missing path")
        paths = [path]
    return SECRET_RISK if any(sensitive(path) for path in paths) else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("codex", "claude"), required=True)
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("invalid payload")
        risk = assess(payload, Path(__file__).resolve().parent.parent)
    except Exception:
        print("Защитный hook не смог проверить вызов. Не повторяй его в обход защиты; сообщи об ошибке", file=sys.stderr)
        return 2
    if risk:
        action = "ask" if args.host == "claude" else "deny"
        reason = risk + ". " + (
            "Нужно подтверждение пользователя для этой операции"
            if action == "ask" else
            "Остановись и объясни риск. Если операция действительно нужна, пользователь выполняет её вручную; не обходи hook"
        )
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": action,
            "permissionDecisionReason": reason,
        }}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
