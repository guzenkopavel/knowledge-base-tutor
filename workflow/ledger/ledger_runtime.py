#!/usr/bin/env python3

"""Общее хранение и bounded policy Execution Ledger"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence, TypeVar

from ledger_core import (
    Decision,
    Interaction,
    LedgerState,
    Transition,
    distinct_observation_count,
    reduce_interaction,
    render_view,
)


FIRST_INFORM_OBSERVATIONS = 3
FIRST_INFORM_INTERACTIONS = 3
INFORM_OBSERVATION_STEP = 4
MAX_CONTEXT_CHARACTERS = 1200
MAX_OWNERS = 8
STORE_VERSION = 2
MAX_STATE_BYTES = 2_000_000

Result = TypeVar("Result")


@dataclass(frozen=True)
class ExecutionOwner:
    host: str
    session_id: str
    agent_id: str = ""


class StateStore:
    def __init__(self, root: Path):
        self.root = root

    def path_for(self, _owner: object = None) -> Path:
        return self.root / "state.json"

    def update(
        self,
        owner: ExecutionOwner,
        operation: Callable[[LedgerState], Result],
        reset: bool = False,
    ) -> Result:
        self.root.mkdir(parents=True, exist_ok=True)
        state_path = self.path_for(owner)
        lock_path = state_path.with_suffix(".lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                document = self._load(state_path)
                owners = document["owners"]
                owner_key = _owner_digest(owner)
                saved = owners.pop(owner_key, None)
                state = LedgerState()
                if not reset and isinstance(saved, dict):
                    state = LedgerState.from_dict(saved)
                shared = LedgerState.from_dict(document["shared"])
                state.repository_epoch = shared.repository_epoch
                state.global_revision = shared.global_revision
                state.file_revisions = dict(shared.file_revisions)
                result = operation(state)
                # Shared data carries invalidation only, never another owner's reads.
                shared.repository_epoch = state.repository_epoch
                shared.global_revision = state.global_revision
                shared.file_revisions = dict(state.file_revisions)
                document["shared"] = shared.to_dict()
                owners[owner_key] = state.to_dict()
                document["owners"] = dict(list(owners.items())[-MAX_OWNERS:])
                self._save(state_path, document)
                return result
            except Exception:
                self._discard(state_path)
                raise

    def _load(self, path: Path) -> dict:
        empty = {"storeVersion": STORE_VERSION, "shared": LedgerState().to_dict(), "owners": {}}
        try:
            if path.stat().st_size > MAX_STATE_BYTES:
                return empty
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("storeVersion") != STORE_VERSION:
                return empty
            LedgerState.from_dict(value["shared"])
            if not isinstance(value["owners"], dict):
                return empty
            # Validate the envelope before trusting any stored revision.
            for state in value["owners"].values():
                LedgerState.from_dict(state)
            return value
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
            return empty

    def _save(self, path: Path, value: dict) -> None:
        temporary = path.with_suffix(".tmp")
        payload = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            # Owner insertion order is the bounded LRU order.
            sort_keys=False,
        )
        if len(payload.encode("utf-8")) > MAX_STATE_BYTES:
            raise ValueError("ledger state exceeds bounded storage")
        temporary.write_text(payload + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def _discard(self, path: Path) -> None:
        for candidate in (path, path.with_suffix(".tmp")):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass


def process_interactions(
    owner: ExecutionOwner,
    interactions: Sequence[Interaction],
    store: StateStore,
) -> Optional[str]:
    def apply(state: LedgerState) -> Optional[str]:
        transitions = [
            reduce_interaction(state, interaction)
            for interaction in interactions
        ]
        return _context_after_transitions(state, transitions)

    return store.update(owner, apply)


def process_session_start(
    owner: ExecutionOwner,
    source: str,
    store: StateStore,
) -> Optional[str]:
    reset = source in {"startup", "clear", "fork"}

    def start(state: LedgerState) -> Optional[str]:
        if source not in {"resume", "compact"}:
            return None
        if not state.observations and not state.focus_files:
            return None
        context = render_view(state, max_characters=MAX_CONTEXT_CHARACTERS)
        _mark_informed(state, context)
        return context

    return store.update(owner, start, reset=reset)


def _context_after_transitions(
    state: LedgerState,
    transitions: Sequence[Transition],
) -> Optional[str]:
    nudge = any(item.decision == Decision.NUDGE for item in transitions)
    repository_changed = any(item.repository_changed for item in transitions)
    observation_count = distinct_observation_count(state)
    should_inform = False

    if nudge:
        should_inform = True
    elif state.interaction_count < FIRST_INFORM_INTERACTIONS:
        return None
    elif repository_changed:
        should_inform = True
    elif not state.inform_active and observation_count >= FIRST_INFORM_OBSERVATIONS:
        state.inform_active = True
        should_inform = True
    elif (
        state.inform_active
        and observation_count - state.last_informed_observation_count
        >= INFORM_OBSERVATION_STEP
    ):
        should_inform = True

    if not should_inform:
        return None

    parts = []
    if nudge:
        parts.append(
            "Nudge: эта команда уже выполнялась без зарегистрированного изменения repository. "
            "Повтор не заблокирован; проверьте, действительно ли он нужен"
        )
    if state.observations or state.focus_files:
        parts.append(render_view(state, max_characters=MAX_CONTEXT_CHARACTERS))
    if not parts:
        return None

    context = "\n".join(parts)
    digest = hashlib.sha256(context.encode("utf-8")).hexdigest()
    if digest == state.last_inform_digest and not nudge:
        return None
    _mark_informed(state, context)
    return context[:MAX_CONTEXT_CHARACTERS]


def _mark_informed(state: LedgerState, context: str) -> None:
    state.inform_active = True
    state.last_informed_observation_count = distinct_observation_count(state)
    state.last_inform_digest = hashlib.sha256(context.encode("utf-8")).hexdigest()


def _owner_digest(owner: ExecutionOwner) -> str:
    value = "{}\n{}\n{}".format(owner.host, owner.session_id, owner.agent_id)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
