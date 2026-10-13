#!/usr/bin/env python3

"""Тонкий Claude Code adapter для Execution Ledger"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ledger_core import Category, Coverage, Effect, Interaction
from ledger_runtime import (
    ExecutionOwner,
    StateStore,
    process_interactions,
    process_session_start,
)
from ledger_shell import classify_shell


DISABLED_VALUES = {"0", "false", "no", "off"}
READ_ONLY_TOOLS = {"Glob", "Grep", "WebFetch", "WebSearch"}
TRANSIENT_TOOLS = {
    "AskUserQuestion",
    "EnterPlanMode",
    "ExitPlanMode",
    "Skill",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "TodoWrite",
}
WRITE_TOOLS = {"Edit", "MultiEdit", "NotebookEdit", "Write"}
MAX_READ_RESPONSE_CHARACTERS = 256_000


def classify_batch(
    payload: Dict[str, Any],
    repository_root: Path,
) -> List[Interaction]:
    tool_calls = payload.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    calls = [item for item in tool_calls if isinstance(item, dict)]
    working_directory = _working_directory(payload, repository_root)
    execution_group = _batch_group(calls)
    return [
        _classify_tool(
            call,
            repository_root,
            working_directory,
            execution_group,
        )
        for call in calls
    ]


def process_payload(
    payload: Dict[str, Any],
    repository_root: Path,
    store: StateStore,
) -> Optional[Dict[str, Any]]:
    try:
        event_name = payload.get("hook_event_name")
        session_id = payload.get("session_id")
        if not isinstance(event_name, str) or not isinstance(session_id, str):
            return None
        owner = ExecutionOwner(
            host="claude",
            session_id=session_id,
            agent_id=_agent_scope(payload),
        )

        if event_name == "PostToolBatch":
            if not isinstance(payload.get("tool_calls"), list):
                return None
            context = process_interactions(
                owner,
                classify_batch(payload, repository_root),
                store,
            )
            return _hook_output(event_name, context)

        if event_name == "SessionStart":
            source = payload.get("source")
            source = source if isinstance(source, str) else ""
            context = process_session_start(owner, source, store)
            return _hook_output(event_name, context)
    except Exception:
        return None

    return None


def repository_root_from(start: Path) -> Optional[Path]:
    current = start.resolve()
    for candidate in (current,) + tuple(current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def git_directory(repository_root: Path) -> Optional[Path]:
    marker = repository_root / ".git"
    if marker.is_dir():
        return marker
    if marker.is_file():
        try:
            first_line = marker.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, UnicodeError, IndexError):
            return None
        if not first_line.startswith("gitdir:"):
            return None
        value = first_line.split(":", 1)[1].strip()
        path = Path(value)
        return path if path.is_absolute() else (repository_root / path).resolve()
    return None


def ledger_is_disabled(git_dir: Path) -> bool:
    configured = os.environ.get("TUTOR_EXECUTION_LEDGER", "").strip().lower()
    return configured in DISABLED_VALUES or (git_dir / "execution-ledger.disabled").exists()


def _classify_tool(
    call: Dict[str, Any],
    repository_root: Path,
    working_directory: Path,
    execution_group: str,
) -> Interaction:
    tool_name = call.get("tool_name")
    tool_name = tool_name if isinstance(tool_name, str) else "unknown"
    tool_input = call.get("tool_input")
    tool_response = call.get("tool_response")
    digest = _command_digest(tool_name, tool_input, str(working_directory))

    if tool_name == "Read":
        path = _tool_path(tool_input, repository_root, working_directory)
        coverage = _read_coverage(path, tool_response)
        return Interaction(
            command_digest=digest,
            category=Category.INSPECTION,
            effect=Effect.READ_ONLY,
            coverage=coverage,
            execution_group=execution_group,
        )

    if tool_name in READ_ONLY_TOOLS:
        return Interaction(
            command_digest=digest,
            category=Category.SEARCH,
            effect=Effect.READ_ONLY,
            execution_group=execution_group,
        )

    if tool_name in TRANSIENT_TOOLS:
        return Interaction(
            command_digest=digest,
            category=Category.ENVIRONMENT,
            effect=Effect.TRANSIENT_WRITE,
            execution_group=execution_group,
        )

    if tool_name in WRITE_TOOLS:
        effect, path = _native_write_target(
            tool_input,
            repository_root,
            working_directory,
        )
        return Interaction(
            command_digest=digest,
            category=Category.MODIFICATION,
            effect=effect,
            modified_paths=(path,) if path else (),
            execution_group=execution_group,
        )

    if tool_name == "Bash":
        command = _extract_command(tool_input)
        category, effect = classify_shell(command)
        return Interaction(
            command_digest=digest,
            category=category,
            effect=effect,
            execution_group=execution_group,
        )

    return Interaction(
        command_digest=digest,
        category=Category.UNKNOWN,
        effect=Effect.UNKNOWN,
        execution_group=execution_group,
    )


def _read_coverage(
    path: Optional[str],
    tool_response: Any,
) -> Optional[Coverage]:
    if path is None:
        return None
    text = _response_text(tool_response)
    if text is None or "\x00" in text or _response_is_truncated(text):
        return None

    line_numbers: List[int] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.match(r"^\s*(\d+)(?:\t|→|\|)", line)
        if match is None:
            return None
        line_numbers.append(int(match.group(1)))
    if not line_numbers:
        return None
    if line_numbers != list(range(line_numbers[0], line_numbers[-1] + 1)):
        return None

    return Coverage(
        path=path,
        start=line_numbers[0],
        end=line_numbers[-1],
        complete=False,
    )


def _response_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value if len(value) <= MAX_READ_RESPONSE_CHARACTERS else None
    if not isinstance(value, list):
        return None
    parts: List[str] = []
    total_characters = 0
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "text":
            return None
        text = item.get("text")
        if not isinstance(text, str):
            return None
        parts.append(text)
        total_characters += len(text)
        if total_characters > MAX_READ_RESPONSE_CHARACTERS:
            return None
    return "\n".join(parts)


def _response_is_truncated(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "output truncated",
            "truncated output",
            "... truncated",
            "… truncated",
            "[truncated",
            "has been truncated",
        )
    )


def _tool_path(
    tool_input: Any,
    repository_root: Path,
    working_directory: Path,
) -> Optional[str]:
    value = _tool_path_value(tool_input)
    if value is None:
        return None
    candidate = Path(value)
    absolute = candidate if candidate.is_absolute() else working_directory / candidate
    try:
        return absolute.resolve().relative_to(repository_root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _native_write_target(
    tool_input: Any,
    repository_root: Path,
    working_directory: Path,
) -> tuple[Effect, Optional[str]]:
    value = _tool_path_value(tool_input)
    if value is None:
        return Effect.UNKNOWN, None
    candidate = Path(value)
    absolute = candidate if candidate.is_absolute() else working_directory / candidate
    try:
        repository = repository_root.resolve()
        lexical = Path(os.path.abspath(absolute))
        resolved = absolute.resolve()
    except (OSError, RuntimeError, ValueError):
        return Effect.UNKNOWN, None

    for path in (lexical, resolved):
        try:
            relative = path.relative_to(repository).as_posix()
        except ValueError:
            continue
        return Effect.REPOSITORY_WRITE, relative
    return Effect.TRANSIENT_WRITE, None


def _tool_path_value(tool_input: Any) -> Optional[str]:
    if not isinstance(tool_input, dict):
        return None
    for key in ("file_path", "notebook_path", "path"):
        candidate = tool_input.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _working_directory(payload: Dict[str, Any], repository_root: Path) -> Path:
    value = payload.get("cwd")
    if isinstance(value, str) and value:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = repository_root / candidate
        try:
            return candidate.resolve()
        except OSError:
            pass
    return repository_root.resolve()


def _batch_group(calls: Sequence[Dict[str, Any]]) -> str:
    tool_ids = [
        item.get("tool_use_id")
        for item in calls
        if isinstance(item.get("tool_use_id"), str)
    ]
    if tool_ids:
        value = "\n".join(sorted(tool_ids))
    else:
        value = _stable_json(
            [
                {
                    "tool_name": item.get("tool_name"),
                    "tool_input": item.get("tool_input"),
                }
                for item in calls
            ]
        )
    return hashlib.sha256(("claude-batch\n" + value).encode("utf-8")).hexdigest()[:16]


def _agent_scope(payload: Dict[str, Any]) -> str:
    agent_id = payload.get("agent_id")
    return agent_id if isinstance(agent_id, str) else ""


def _extract_command(value: Any) -> str:
    if isinstance(value, dict):
        command = value.get("command")
        return command if isinstance(command, str) else ""
    return value if isinstance(value, str) else ""


def _command_digest(tool_name: str, tool_input: Any, scope: str) -> str:
    value = "{}\n{}\n{}".format(tool_name, scope, _stable_json(tool_input))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return "<unsupported>"


def _hook_output(event_name: str, context: Optional[str]) -> Optional[Dict[str, Any]]:
    if not context:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context,
        }
    }


def _run_hook() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        cwd = payload.get("cwd")
        start = Path(cwd) if isinstance(cwd, str) else Path.cwd()
        repository_root = repository_root_from(start)
        if repository_root is None:
            return 0
        git_dir = git_directory(repository_root)
        if git_dir is None or ledger_is_disabled(git_dir):
            return 0
        output = process_payload(
            payload,
            repository_root,
            StateStore(git_dir / "execution-ledger"),
        )
        if output is not None:
            json.dump(output, sys.stdout, ensure_ascii=False, separators=(",", ":"))
            sys.stdout.write("\n")
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(_run_hook())
