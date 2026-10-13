#!/usr/bin/env python3

"""Тонкий Codex adapter для Execution Ledger"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from ledger_core import (
    Category,
    Coverage,
    Effect,
    Interaction,
)
from ledger_runtime import (
    ExecutionOwner,
    StateStore,
    process_interactions,
    process_session_start,
)
from ledger_shell import classify_shell, visible_content_command


DISABLED_VALUES = {"0", "false", "no", "off"}


def classify_post_tool(payload: Dict[str, Any], repository_root: Path) -> Interaction:
    tool_name = payload.get("tool_name")
    tool_name = tool_name if isinstance(tool_name, str) else "unknown"
    tool_input = payload.get("tool_input")
    tool_response = payload.get("tool_response")
    working_directory = _working_directory(payload, repository_root)
    execution_group = _execution_group(payload)
    command_scope = str(working_directory)

    if tool_name == "Bash":
        command = _extract_command(tool_input)
        normalized = _normalize_command(command)
        category, effect = classify_shell(normalized)
        success = _response_succeeded(tool_response)
        coverage = None
        if success and effect == Effect.READ_ONLY and not _response_is_truncated(tool_response):
            coverage = _parse_coverage(
                normalized,
                _extract_output(tool_response),
                repository_root,
                working_directory,
            )
        if coverage is None and effect == Effect.READ_ONLY and isinstance(tool_response, str):
            coverage = _plain_read_coverage(normalized, tool_response, repository_root, working_directory)
        if effect == Effect.REPOSITORY_WRITE and not success:
            effect = Effect.UNKNOWN
        return Interaction(
            command_digest=_command_digest(tool_name, normalized, command_scope),
            category=category,
            effect=effect,
            coverage=coverage,
            execution_group=execution_group,
        )

    if tool_name == "apply_patch":
        patch = _extract_command(tool_input)
        paths = tuple(_patch_paths(patch, repository_root, working_directory))
        effect = Effect.REPOSITORY_WRITE if not _response_failed(tool_response) else Effect.UNKNOWN
        return Interaction(
            command_digest=_command_digest(tool_name, patch, command_scope),
            category=Category.MODIFICATION,
            effect=effect,
            modified_paths=paths,
            execution_group=execution_group,
        )

    return Interaction(
        command_digest=_command_digest(tool_name, _stable_json(tool_input)),
        category=Category.UNKNOWN,
        effect=Effect.UNKNOWN,
        execution_group=execution_group,
    )


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
            host="codex",
            session_id=session_id,
            agent_id=_agent_scope(payload),
        )

        if event_name == "PostToolUse":
            interaction = classify_post_tool(payload, repository_root)
            context = process_interactions(owner, [interaction], store)
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


def _hook_output(event_name: str, context: Optional[str]) -> Optional[Dict[str, Any]]:
    if not context:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context,
        }
    }


def _parse_coverage(
    command: str,
    output: Optional[str],
    repository_root: Path,
    working_directory: Path,
) -> Optional[Coverage]:
    if output is None or "\x00" in output:
        return None
    command = visible_content_command(command)
    if not command:
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None

    executable = Path(tokens[0]).name.lower()
    if executable == "cat" and len(tokens) == 2 and not tokens[1].startswith("-"):
        path = _repository_relative_path(tokens[1], repository_root, working_directory)
        return Coverage(path=path, complete=True) if path else None

    if executable != "sed" or len(tokens) != 4 or tokens[1] != "-n":
        return None
    match = re.fullmatch(r"(\d+)(?:,(\d+))?p", tokens[2])
    if not match:
        return None
    start = int(match.group(1))
    requested_end = int(match.group(2) or match.group(1))
    if start < 1 or requested_end < start:
        return None
    line_count = len(output.splitlines())
    if line_count == 0:
        return None
    path = _repository_relative_path(tokens[3], repository_root, working_directory)
    if path is None:
        return None
    actual_end = min(requested_end, start + line_count - 1)
    return Coverage(path=path, start=start, end=actual_end, complete=False)


def _plain_read_coverage(
    command: str, response: str, repository_root: Path, working_directory: Path,
) -> Optional[Coverage]:
    """Some Codex hosts send stdout only, with no exit/truncation metadata.

    Accept only an exact bounded file/range match, never arbitrary stdout as success.
    This proves displayed content, not ordering relative to concurrent writes.
    """
    if len(response) > 256_000:
        return None
    command = visible_content_command(command)
    if not command:
        return None
    coverage = _parse_coverage(command, response, repository_root, working_directory)
    if coverage is None:
        return None
    try:
        path = repository_root / coverage.path
        if not path.is_file() or path.stat().st_size > 1_000_000:
            return None
        with path.open(encoding="utf-8", newline="") as source:
            content = source.read(1_000_001)
        if len(content) > 1_000_000:
            return None
        if coverage.complete:
            expected = content
        else:
            # Use the requested end, not the response-derived end: a prefix is not a full read.
            tokens = shlex.split(command)
            requested_end = int(tokens[2].rstrip("p").split(",")[-1])
            expected = "".join(content.splitlines(keepends=True)[coverage.start - 1:requested_end])
        return coverage if response == expected else None
    except (OSError, UnicodeError, ValueError):
        return None


def _patch_paths(
    patch: str,
    repository_root: Path,
    working_directory: Path,
) -> List[str]:
    result: List[str] = []
    pattern = re.compile(r"(?m)^\*\*\* (?:Add|Update|Delete) File:\s*(.+?)\s*$")
    for match in pattern.finditer(patch):
        path = _repository_relative_path(
            match.group(1),
            repository_root,
            working_directory,
        )
        if path and path not in result:
            result.append(path)
    return result


def _repository_relative_path(
    value: str,
    repository_root: Path,
    working_directory: Path,
) -> Optional[str]:
    candidate = Path(value)
    absolute = candidate if candidate.is_absolute() else working_directory / candidate
    try:
        return absolute.resolve().relative_to(repository_root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _extract_command(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("command", "cmd"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
            if isinstance(candidate, list) and all(isinstance(item, str) for item in candidate):
                return " ".join(candidate)
    return value if isinstance(value, str) else ""


def _working_directory(payload: Dict[str, Any], repository_root: Path) -> Path:
    candidates: List[Any] = []
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        candidates.extend((tool_input.get("workdir"), tool_input.get("cwd")))
    candidates.append(payload.get("cwd"))

    for value in candidates:
        if not isinstance(value, str) or not value:
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = repository_root / candidate
        try:
            return candidate.resolve()
        except OSError:
            continue
    return repository_root.resolve()


def _execution_group(payload: Dict[str, Any]) -> str:
    tool_id = payload.get("tool_use_id")
    if not isinstance(tool_id, str) or not tool_id:
        return ""
    # Identity only; PostToolUse cannot establish read/write ordering.
    return hashlib.sha256(tool_id.encode("utf-8")).hexdigest()[:16]


def _agent_scope(payload: Dict[str, Any]) -> str:
    agent_id = payload.get("agent_id")
    return agent_id if isinstance(agent_id, str) else ""


def _normalize_command(command: str) -> str:
    return command.strip()


def _response_succeeded(response: Any) -> bool:
    if _response_failed(response):
        return False
    codes = _values_for_keys(response, {"exit_code", "exitCode"})
    integer_codes = [value for value in codes if isinstance(value, int)]
    if integer_codes:
        return all(value == 0 for value in integer_codes)
    # A string may be the file itself, including examples of exit codes.
    return False


def _response_failed(response: Any) -> bool:
    if isinstance(response, dict):
        if response.get("isError") is True:
            return True
        status = response.get("status")
        if isinstance(status, str) and status.lower() in {"error", "failed", "failure"}:
            return True
        for key in ("exit_code", "exitCode"):
            code = response.get(key)
            if isinstance(code, int) and code != 0:
                return True
        return any(_response_failed(value) for value in response.values() if isinstance(value, (dict, list)))
    if isinstance(response, list):
        return any(_response_failed(item) for item in response)
    return False


def _response_is_truncated(response: Any) -> bool:
    flags = _values_for_keys(
        response,
        {"truncated", "is_truncated", "output_truncated", "was_truncated"},
    )
    if any(value is True for value in flags):
        return True
    text = _stable_json(response).lower()
    return (
        "output truncated" in text
        or "truncated output" in text
        or "… truncated" in text
        or "... truncated" in text
    )


def _extract_output(response: Any) -> Optional[str]:
    if isinstance(response, dict):
        for key in ("output", "stdout"):
            value = response.get(key)
            if isinstance(value, str):
                return value
        for value in response.values():
            if isinstance(value, (dict, list)):
                nested = _extract_output(value)
                if nested is not None:
                    return nested
    if isinstance(response, list):
        for item in response:
            nested = _extract_output(item)
            if nested is not None:
                return nested
    return None


def _values_for_keys(value: Any, keys: set) -> List[Any]:
    result: List[Any] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in keys:
                result.append(nested)
            if isinstance(nested, (dict, list)):
                result.extend(_values_for_keys(nested, keys))
    elif isinstance(value, list):
        for item in value:
            result.extend(_values_for_keys(item, keys))
    return result


def _command_digest(
    tool_name: str,
    normalized_input: str,
    execution_scope: str = "",
) -> str:
    payload = "{}\n{}\n{}".format(tool_name, execution_scope, normalized_input)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return "<unsupported>"


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


def _set_enabled(enabled: bool) -> int:
    repository_root = repository_root_from(Path.cwd())
    if repository_root is None:
        print("Ledger: Git repository not found", file=sys.stderr)
        return 1
    git_dir = git_directory(repository_root)
    if git_dir is None:
        print("Ledger: Git directory not found", file=sys.stderr)
        return 1
    marker = git_dir / "execution-ledger.disabled"
    if enabled:
        marker.unlink(missing_ok=True)
    else:
        marker.touch()
    print("Ledger: {}".format("enabled" if enabled else "disabled"))
    return 0


def _status() -> int:
    repository_root = repository_root_from(Path.cwd())
    git_dir = git_directory(repository_root) if repository_root else None
    if repository_root is None or git_dir is None:
        print("Ledger: unavailable")
        return 1
    print("Ledger: {}".format("disabled" if ledger_is_disabled(git_dir) else "enabled"))
    print("State: {}".format(git_dir / "execution-ledger"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Execution Ledger Codex adapter")
    parser.add_argument("action", nargs="?", choices=("hook", "status", "enable", "disable"), default="hook")
    arguments = parser.parse_args()
    if arguments.action == "status":
        return _status()
    if arguments.action == "enable":
        return _set_enabled(True)
    if arguments.action == "disable":
        return _set_enabled(False)
    return _run_hook()


if __name__ == "__main__":
    sys.exit(main())
