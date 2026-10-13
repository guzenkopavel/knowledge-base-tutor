#!/usr/bin/env python3

"""Общая консервативная классификация shell-команд для Ledger"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Sequence, Tuple

from ledger_core import Category, Effect


def classify_shell(command: str) -> Tuple[Category, Effect]:
    if not command or _contains_shell_control(command):
        return Category.UNKNOWN, Effect.UNKNOWN
    try:
        tokens = shlex.split(command)
    except ValueError:
        return Category.UNKNOWN, Effect.UNKNOWN
    if not tokens:
        return Category.UNKNOWN, Effect.UNKNOWN

    if tokens[0] == "rtk":
        inner = tokens[2:] if len(tokens) > 1 and tokens[1] == "proxy" else tokens[1:]
        if not inner:
            return Category.UNKNOWN, Effect.UNKNOWN
        return classify_shell(shlex.join(inner))

    executable = Path(tokens[0]).name.lower()
    if tokens[0] != executable:
        return Category.UNKNOWN, Effect.UNKNOWN
    lowered = command.lower()
    if _is_test_command(lowered, executable, tokens):
        return Category.TEST, Effect.TRANSIENT_WRITE
    if executable in {"rg", "grep"} and not any(
        token == "--pre" or token.startswith("--pre=") for token in tokens
    ):
        return Category.SEARCH, Effect.READ_ONLY
    if executable == "git":
        subcommand = tokens[1].lower() if len(tokens) > 1 else ""
        writes_output = any(
            token == "--output" or token.startswith("--output=")
            for token in tokens[2:]
        )
        if (
            subcommand in {"status", "diff", "log", "show", "rev-parse"}
            and not writes_output
        ):
            return Category.INSPECTION, Effect.READ_ONLY
        return Category.UNKNOWN, Effect.UNKNOWN
    if executable == "sed":
        if _is_safe_sed_read(tokens):
            return Category.INSPECTION, Effect.READ_ONLY
        return Category.MODIFICATION, Effect.UNKNOWN
    if executable in {
        "cat",
        "head",
        "tail",
        "ls",
        "pwd",
        "wc",
        "stat",
        "file",
        "which",
    }:
        return Category.INSPECTION, Effect.READ_ONLY
    return Category.UNKNOWN, Effect.UNKNOWN


def visible_content_command(command: str) -> str:
    """Return a raw content command only when no output-filtering wrapper is used."""
    if not command or _contains_shell_control(command):
        return ""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    if not tokens:
        return ""
    if tokens[0] == "rtk":
        if len(tokens) < 3 or tokens[1] != "proxy":
            return ""
        return shlex.join(tokens[2:])
    return command


def _is_test_command(
    lowered: str,
    executable: str,
    tokens: Sequence[str],
) -> bool:
    if executable == "pytest":
        return True
    if executable in {"python", "python3"}:
        return "pytest" in tokens or "unittest" in tokens
    return False


def _is_safe_sed_read(tokens: Sequence[str]) -> bool:
    return (
        len(tokens) == 4
        and tokens[1] == "-n"
        and re.fullmatch(r"\d+(?:,\d+)?p", tokens[2]) is not None
        and not tokens[3].startswith("-")
    )


def _contains_shell_control(command: str) -> bool:
    quote = ""
    escaped = False
    index = 0
    while index < len(command):
        character = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if character == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if character == "'" and quote != '"':
            quote = "" if quote == "'" else "'"
            index += 1
            continue
        if character == '"' and quote != "'":
            quote = "" if quote == '"' else '"'
            index += 1
            continue
        if quote != "'" and (
            character == "`"
            or (character == "$" and command[index : index + 2] == "$(")
        ):
            return True
        if not quote and character in ";&|><\n":
            return True
        index += 1
    return bool(quote or escaped)
