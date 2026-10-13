#!/usr/bin/env python3

"""Детерминированное состояние выполнения без привязки к runtime"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple


SCHEMA_VERSION = 2
MAX_OBSERVATIONS = 200
MAX_COMMAND_RECEIPTS = 32
MAX_FOCUS_FILES = 8
MAX_FILE_REVISIONS = 200


class Category(str, Enum):
    INSPECTION = "inspection"
    SEARCH = "search"
    TEST = "test"
    MODIFICATION = "modification"
    ENVIRONMENT = "environment"
    UNKNOWN = "unknown"


class Effect(str, Enum):
    READ_ONLY = "read_only"
    REPOSITORY_WRITE = "repository_write"
    TRANSIENT_WRITE = "transient_write"
    UNKNOWN = "unknown"


class Decision(str, Enum):
    ALLOW = "allow"
    NUDGE = "nudge"


@dataclass(frozen=True)
class Coverage:
    path: str
    start: Optional[int] = None
    end: Optional[int] = None
    complete: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "start": self.start,
            "end": self.end,
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Coverage":
        path = value.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("coverage path is required")
        start = value.get("start")
        end = value.get("end")
        if start is not None and not isinstance(start, int):
            raise ValueError("coverage start must be an integer")
        if end is not None and not isinstance(end, int):
            raise ValueError("coverage end must be an integer")
        return cls(path=path, start=start, end=end, complete=bool(value.get("complete")))


@dataclass(frozen=True)
class Observation:
    coverage: Coverage
    repository_epoch: int
    global_revision: int
    file_revision: int
    ordering_known: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "coverage": self.coverage.to_dict(),
            "repositoryEpoch": self.repository_epoch,
            "globalRevision": self.global_revision,
            "fileRevision": self.file_revision,
            "orderingKnown": self.ordering_known,
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Observation":
        return cls(
            coverage=Coverage.from_dict(_mapping(value.get("coverage"))),
            repository_epoch=_non_negative_int(value.get("repositoryEpoch")),
            global_revision=_non_negative_int(value.get("globalRevision")),
            file_revision=_non_negative_int(value.get("fileRevision")),
            ordering_known=value.get("orderingKnown") is True,
        )


@dataclass(frozen=True)
class CommandReceipt:
    digest: str
    category: Category
    condition: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "digest": self.digest,
            "category": self.category.value,
            "condition": self.condition,
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "CommandReceipt":
        digest = value.get("digest")
        condition = value.get("condition")
        if not isinstance(digest, str) or not isinstance(condition, str):
            raise ValueError("invalid command receipt")
        return cls(
            digest=digest,
            category=Category(value.get("category", Category.UNKNOWN.value)),
            condition=condition,
        )


@dataclass(frozen=True)
class Interaction:
    command_digest: str
    category: Category
    effect: Effect
    coverage: Optional[Coverage] = None
    modified_paths: Tuple[str, ...] = ()
    execution_group: str = ""
    # Post-only hooks cannot prove that a read did not overlap a write.
    ordering_known: bool = False


@dataclass(frozen=True)
class Transition:
    decision: Decision
    repository_changed: bool
    observation_added: bool


@dataclass
class LedgerState:
    repository_epoch: int = 0
    global_revision: int = 0
    file_revisions: Dict[str, int] = field(default_factory=dict)
    observations: List[Observation] = field(default_factory=list)
    command_receipts: List[CommandReceipt] = field(default_factory=list)
    focus_files: List[str] = field(default_factory=list)
    nudged_conditions: Dict[str, str] = field(default_factory=dict)
    interaction_count: int = 0
    inform_active: bool = False
    last_informed_observation_count: int = 0
    last_inform_digest: str = ""

    def execution_condition(self) -> str:
        return "{}:{}".format(self.repository_epoch, self.global_revision)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "repositoryEpoch": self.repository_epoch,
            "globalRevision": self.global_revision,
            "fileRevisions": dict(sorted(self.file_revisions.items())),
            "observations": [item.to_dict() for item in self.observations],
            "commandReceipts": [item.to_dict() for item in self.command_receipts],
            "focusFiles": list(self.focus_files),
            "nudgedConditions": dict(self.nudged_conditions),
            "interactionCount": self.interaction_count,
            "informActive": self.inform_active,
            "lastInformedObservationCount": self.last_informed_observation_count,
            "lastInformDigest": self.last_inform_digest,
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "LedgerState":
        if value.get("schemaVersion") != SCHEMA_VERSION:
            raise ValueError("unsupported ledger schema")

        parsed_file_revisions = {
            path: _non_negative_int(revision)
            for path, revision in _mapping(value.get("fileRevisions")).items()
            if isinstance(path, str)
        }
        file_revisions = dict(
            list(parsed_file_revisions.items())[-MAX_FILE_REVISIONS:]
        )
        observations = _parse_items(value.get("observations"), Observation.from_dict)
        receipts = _parse_items(value.get("commandReceipts"), CommandReceipt.from_dict)
        focus_files = [
            path for path in _list(value.get("focusFiles")) if isinstance(path, str)
        ][:MAX_FOCUS_FILES]
        active_receipt_digests = {
            receipt.digest for receipt in receipts[-MAX_COMMAND_RECEIPTS:]
        }
        nudged_conditions = {
            digest: condition
            for digest, condition in _mapping(value.get("nudgedConditions")).items()
            if (
                isinstance(digest, str)
                and isinstance(condition, str)
                and digest in active_receipt_digests
            )
        }

        return cls(
            repository_epoch=_non_negative_int(value.get("repositoryEpoch")),
            global_revision=_non_negative_int(value.get("globalRevision")),
            file_revisions=file_revisions,
            observations=observations[-MAX_OBSERVATIONS:],
            command_receipts=receipts[-MAX_COMMAND_RECEIPTS:],
            focus_files=focus_files,
            nudged_conditions=nudged_conditions,
            interaction_count=_non_negative_int(value.get("interactionCount")),
            inform_active=bool(value.get("informActive")),
            last_informed_observation_count=_non_negative_int(
                value.get("lastInformedObservationCount")
            ),
            last_inform_digest=(
                value.get("lastInformDigest")
                if isinstance(value.get("lastInformDigest"), str)
                else ""
            ),
        )


def reduce_interaction(state: LedgerState, interaction: Interaction) -> Transition:
    condition = state.execution_condition()
    repeat = any(
        receipt.digest == interaction.command_digest and receipt.condition == condition
        for receipt in state.command_receipts
    )
    decision = Decision.ALLOW

    nudge_categories = {Category.INSPECTION, Category.SEARCH, Category.TEST}
    if repeat and interaction.category in nudge_categories:
        if state.nudged_conditions.get(interaction.command_digest) != condition:
            decision = Decision.NUDGE
            state.nudged_conditions[interaction.command_digest] = condition

    repository_changed = _apply_effect(state, interaction)
    observation_added = False
    if (
        interaction.coverage is not None
        and interaction.effect == Effect.READ_ONLY
    ):
        path = interaction.coverage.path
        state.observations.append(
            Observation(
                coverage=interaction.coverage,
                repository_epoch=state.repository_epoch,
                global_revision=state.global_revision,
                file_revision=state.file_revisions.get(path, 0),
                ordering_known=interaction.ordering_known,
            )
        )
        state.observations = state.observations[-MAX_OBSERVATIONS:]
        observation_added = True

    state.command_receipts.append(
        CommandReceipt(
            digest=interaction.command_digest,
            category=interaction.category,
            condition=condition,
        )
    )
    state.command_receipts = state.command_receipts[-MAX_COMMAND_RECEIPTS:]
    active_receipt_digests = {receipt.digest for receipt in state.command_receipts}
    state.nudged_conditions = {
        digest: condition
        for digest, condition in state.nudged_conditions.items()
        if digest in active_receipt_digests
    }
    state.interaction_count += 1

    return Transition(
        decision=decision,
        repository_changed=repository_changed,
        observation_added=observation_added,
    )


def observation_freshness(state: LedgerState, observation: Observation) -> str:
    if observation.repository_epoch != state.repository_epoch:
        return "may_be_stale"
    if observation.global_revision != state.global_revision:
        return "may_be_stale"
    current_file_revision = state.file_revisions.get(observation.coverage.path, 0)
    if observation.file_revision != current_file_revision:
        return "may_be_stale"
    if not observation.ordering_known:
        return "freshness_unknown"
    return "current"


def render_view(
    state: LedgerState,
    max_observations: int = 12,
    max_characters: int = 1600,
) -> str:
    lines = ["[Execution Ledger: состояние выполнения]"]
    if state.focus_files:
        lines.append("Фокус: {}".format(", ".join(state.focus_files[:MAX_FOCUS_FILES])))

    visible = _latest_observations(state)
    if visible:
        lines.append("Наблюдения:")
        for observation in visible[: max(0, max_observations)]:
            status = observation_freshness(state, observation)
            status_text = {
                "current": "актуально",
                "may_be_stale": "возможно устарело",
                "freshness_unknown": "прочитано; актуальность не подтверждена",
            }[status]
            lines.append(
                "- {} — {}".format(
                    _coverage_label(observation.coverage),
                    status_text,
                )
            )

    rendered = "\n".join(lines)
    if len(rendered) <= max_characters:
        return rendered
    return rendered[: max(0, max_characters - 2)].rstrip() + " …"


def distinct_observation_count(state: LedgerState) -> int:
    return len(
        {
            (
                item.coverage.path,
                item.coverage.start,
                item.coverage.end,
                item.coverage.complete,
            )
            for item in state.observations
        }
    )


def _apply_effect(state: LedgerState, interaction: Interaction) -> bool:
    if interaction.effect == Effect.REPOSITORY_WRITE:
        state.global_revision += 1
        paths = _unique_paths(interaction.modified_paths)
        if paths:
            for path in paths:
                revision = state.file_revisions.pop(path, 0) + 1
                state.file_revisions[path] = revision
            state.file_revisions = dict(
                list(state.file_revisions.items())[-MAX_FILE_REVISIONS:]
            )
            state.focus_files = _merge_focus(paths, state.focus_files)
        else:
            state.repository_epoch += 1
        return True

    if interaction.effect == Effect.UNKNOWN:
        state.repository_epoch += 1
        state.global_revision += 1
        return True

    return False


def _latest_observations(state: LedgerState) -> List[Observation]:
    latest: Dict[
        Tuple[str, Optional[int], Optional[int], bool],
        Tuple[int, Observation],
    ] = {}
    for index, observation in enumerate(state.observations):
        coverage = observation.coverage
        key = (coverage.path, coverage.start, coverage.end, coverage.complete)
        latest[key] = (index, observation)
    ordered = sorted(
        latest.values(),
        key=lambda item: item[0],
        reverse=True,
    )
    return [observation for _, observation in ordered]


def _coverage_label(coverage: Coverage) -> str:
    if coverage.complete:
        return "{} (целиком)".format(coverage.path)
    if coverage.start is None or coverage.end is None:
        return coverage.path
    return "{}:{}-{}".format(coverage.path, coverage.start, coverage.end)


def _unique_paths(paths: Iterable[str]) -> List[str]:
    result: List[str] = []
    for path in paths:
        if path and path not in result:
            result.append(path)
    return result


def _merge_focus(changed: List[str], existing: List[str]) -> List[str]:
    return (changed + [path for path in existing if path not in changed])[:MAX_FOCUS_FILES]


def _mapping(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("mapping expected")
    return value


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _non_negative_int(value: Any) -> int:
    if not isinstance(value, int) or value < 0:
        return 0
    return value


def _parse_items(value: Any, parser: Any) -> List[Any]:
    result = []
    for item in _list(value):
        if not isinstance(item, dict):
            continue
        try:
            result.append(parser(item))
        except (TypeError, ValueError):
            continue
    return result
