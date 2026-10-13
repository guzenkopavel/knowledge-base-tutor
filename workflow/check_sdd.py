#!/usr/bin/env python3
"""Небольшая структурная проверка одного общего или локального SDD change."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parent.parent
TASK = re.compile(r"(?m)^###\s+(T-[A-Za-z0-9-]+)\s*[-:]\s*(.+)$")
REQ = re.compile(r"(?m)^#{3,4}\s+(REQ-[A-Za-z0-9-]+)\b[^\n]*$")
LINK = re.compile(r"!?\[[^\]]*\]\((<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)")
PLACEHOLDER = re.compile(r"<[^<>\n]+>")


def value(text: str, name: str) -> str | None:
    match = re.search(rf"(?m)^[ \t]*-?[ \t]*{re.escape(name)}:[ \t]*(.*?)[ \t]*$", text)
    return match.group(1).strip() if match else None


def unfilled(field: str | None) -> bool:
    return not field or field.casefold() == "pending" or bool(PLACEHOLDER.search(field))


def section(text: str, heading: str, level: int = 2) -> str | None:
    marker = "#" * level
    match = re.search(rf"(?m)^{marker}\s+{re.escape(heading)}\s*$", text)
    if not match:
        return None
    tail = text[match.end():]
    stop = re.search(rf"(?m)^#{{1,{level}}}\s+", tail)
    return tail[:stop.start()] if stop else tail


def path_in_repo(raw: str, errors: list[str], label: str) -> Path | None:
    if not raw or raw == "none" or "<" in raw or ">" in raw:
        errors.append(f"{label}: нужен конкретный путь от корня репозитория")
        return None
    if Path(raw).is_absolute():
        errors.append(f"{label}: нужен путь от корня репозитория, без начального /")
        return None
    path = (ROOT / raw).resolve()
    if not path.is_relative_to(ROOT):
        errors.append(f"{label}: путь выходит за корень репозитория")
        return None
    return path


def change_root(change: Path, errors: list[str]) -> Path | None:
    if change.parent.name != "changes" or change.parent.parent.name != "SDD" or change.parent.parent.parent.name != "KnowledgeBase":
        errors.append("change должен находиться в <KB>/SDD/changes/<имя>/")
        return None
    return change.parent.parent


def blocks(text: str, op: str) -> dict[str, str]:
    body = section(section(text, "Contract delta") or "", op, 3) or ""
    found = list(REQ.finditer(body))
    return {
        match.group(1): body[match.start(): found[index + 1].start() if index + 1 < len(found) else len(body)].strip()
        for index, match in enumerate(found)
    }


def target_for(block: str, default: str, errors: list[str], req: str) -> Path | None:
    raw = value(block, "Target spec") or default
    path = path_in_repo(raw, errors, req)
    if path and (path.name != "spec.md" or path.parent.parent.name != "specs" or path.parent.parent.parent.name != "SDD" or path.parent.parent.parent.parent.name != "KnowledgeBase"):
        errors.append(f"{req}: Target spec должен быть в <KB>/SDD/specs/<область>/spec.md")
        return None
    return path


def canonical_block(block: str, source: Path) -> str:
    lines = [line for line in block.splitlines() if not re.match(r"^-?\s*Target spec:", line)]
    if lines:
        lines[0] = re.sub(r"^#{3,4}", "###", lines[0])
    text = "\n".join(lines).strip()
    text = re.sub(r"^(###\s+REQ-[^\n]+)\n+", r"\1\n\n", text)

    def normalize(match: re.Match[str]) -> str:
        raw = match.group(1).strip("<>")
        if raw.startswith(("https://", "http://", "mailto:", "data:", "#")):
            return match.group(0)
        resolved = (source.parent / unquote(raw.split("#", 1)[0])).resolve()
        return match.group(0).replace(match.group(1), str(resolved))

    return LINK.sub(normalize, text)


def validate_links(file: Path, future: Path | None, change: Path, errors: list[str]) -> None:
    content = file.read_text(encoding="utf-8")
    base = future or file
    for match in LINK.finditer(content):
        raw = match.group(1).strip("<>")
        if raw.startswith(("https://", "http://", "mailto:", "data:", "#")):
            continue
        target = Path(unquote(raw.split("#", 1)[0]))
        candidate = (base.parent / target).resolve()
        if target.is_absolute() or not candidate.is_relative_to(ROOT):
            errors.append(f"{file.relative_to(ROOT)}: ссылка выходит за корень репозитория: {raw}")
            continue
        if candidate.exists():
            continue
        if future and candidate.is_relative_to(future.parent):
            staged = (change / candidate.relative_to(future.parent)).resolve()
            if staged.is_relative_to(ROOT) and staged.exists():
                continue
        errors.append(f"{file.relative_to(ROOT)}: ссылка не ведёт к файлу: {raw}")


def validate_tasks(text: str, archive: bool, errors: list[str]) -> None:
    status = value(text, "Status")
    allowed = {"draft", "active", "paused", "awaiting_acceptance"}
    if status not in allowed:
        errors.append(f"tasks.md: Status должен быть одним из {', '.join(sorted(allowed))}")
    if archive and status != "awaiting_acceptance":
        errors.append("archive-check требует Status: awaiting_acceptance")
    if value(text, "Spec") != "spec.md":
        errors.append("tasks.md: Spec должен указывать spec.md")
    if value(text, "Execution profile") not in {"adaptive", "guided"}:
        errors.append("tasks.md: неверный Execution profile")
    if value(text, "Execution scope") not in {"change", "task"}:
        errors.append("tasks.md: неверный Execution scope")
    if value(text, "Evidence profile") not in {"normal", "strict"}:
        errors.append("tasks.md: неверный Evidence profile")
    strategy = section(text, "Evidence strategy")
    for name in ("Primary verifier", "Early verification", "Escalation"):
        if not strategy or unfilled(value(strategy, name)):
            errors.append(f"tasks.md: заполните Evidence strategy / {name}")
    review = section(text, "Сверка плана")
    review_text = review.strip().casefold() if review else ""
    review_text = re.sub(r"^сверка плана:\s*", "", review_text)
    if not re.match(r"^готов(?:\s|$|[—–-])", review_text):
        errors.append("tasks.md: нужна завершённая смысловая сверка плана")
    entries = list(TASK.finditer(text))
    if not entries:
        errors.append("tasks.md: нет задач T-*")
        return
    ids = [entry.group(1) for entry in entries]
    if len(ids) != len(set(ids)):
        errors.append("tasks.md: повторяются идентификаторы задач")
    if ids[-1] != "T-FINAL" or "Final review" not in entries[-1].group(2):
        errors.append("tasks.md: последняя задача должна быть T-FINAL: Final review")
    for index, entry in enumerate(entries):
        body = text[entry.end(): entries[index + 1].start() if index + 1 < len(entries) else len(text)]
        body = body.split("\n## ", 1)[0]
        task_id = entry.group(1)
        for name in ("Status", "Depends on", "Covers", "Knowledge", "Boundaries", "Done when", "Verification"):
            field = value(body, name)
            if unfilled(field):
                errors.append(f"{task_id}: заполните {name}")
        deps = value(body, "Depends on") or ""
        for dep in re.findall(r"\bT-[A-Za-z0-9-]+\b", deps):
            if dep not in ids[:index]:
                errors.append(f"{task_id}: неизвестная или будущая зависимость {dep}")
        if task_id == "T-FINAL" and set(ids[:-1]) - set(re.findall(r"\bT-[A-Za-z0-9-]+\b", deps)):
            errors.append("T-FINAL: зависит не от всех остальных задач")
        task_status = value(body, "Status")
        if task_status not in {"pending", "active", "done", "blocked"}:
            errors.append(f"{task_id}: неверный Status")
        completion = section(body, "Completion", 4)
        if not completion:
            errors.append(f"{task_id}: отсутствует Completion")
        elif task_status == "done" or archive:
            for name in ("Changed", "Contract", "Boundaries", "Leading risk", "Evidence"):
                field = value(completion, name)
                if unfilled(field):
                    errors.append(f"{task_id}: Completion / {name} не заполнен")
            if task_id != "T-FINAL" and unfilled(value(completion, "verify_now")):
                errors.append(f"{task_id}: Completion / verify_now не заполнен")
        if archive and task_status != "done":
            errors.append(f"{task_id}: archive-check требует Status: done")
        if status == "awaiting_acceptance" or archive:
            if task_status != "done":
                errors.append(f"{task_id}: готовый план требует Status: done")
            if task_id == "T-FINAL" and value(completion or "", "Verdict") != "ready":
                errors.append("T-FINAL: готовый план требует Verdict: ready")
    acceptance = section(text, "Приёмка")
    if not acceptance:
        errors.append("tasks.md: отсутствует раздел Приёмка")
    elif archive and (value(acceptance, "Acceptance") != "accepted" or value(acceptance, "Acceptance evidence") == "none" or unfilled(value(acceptance, "Acceptance evidence"))):
        errors.append("tasks.md: archive-check требует явную приёмку актуального результата")


def validate_merge(spec: str, archive_path: Path, errors: list[str]) -> None:
    default = value(spec, "Canonical spec") or ""
    for op in ("ADDED", "MODIFIED"):
        for req, block in blocks(spec, op).items():
            target = target_for(block, default, errors, req)
            if not target or not target.is_file():
                errors.append(f"{req}: целевая спецификация отсутствует после переноса")
                continue
            current = target.read_text(encoding="utf-8")
            matches = [m for m in REQ.finditer(current) if m.group(1) == req]
            if not matches:
                errors.append(f"{req}: требование не перенесено в {target.relative_to(ROOT)}")
                continue
            if len(matches) != 1:
                errors.append(f"{req}: в основной спецификации повторяется идентификатор")
                continue
            match = matches[0]
            following = re.search(r"(?m)^#{1,3}\s+", current[match.end():])
            end = match.end() + following.start() if following else len(current)
            actual = current[match.start():end].strip()
            expected = canonical_block(block, archive_path / "spec.md")
            if canonical_block(actual, target) != expected:
                errors.append(f"{req}: целевой блок отличается от принятой дельты")
    removed_blocks = blocks(spec, "REMOVED")
    for req, block in removed_blocks.items():
        target = target_for(block, default, errors, req)
        if target and not target.is_file():
            errors.append(f"{req}: основная спецификация отсутствует после удаления")
        elif target and any(m.group(1) == req for m in REQ.finditer(target.read_text(encoding="utf-8"))):
            errors.append(f"{req}: удалённое требование осталось в основной спецификации")


def validate_active_count(errors: list[str]) -> None:
    active: list[Path] = []
    for tasks in ROOT.rglob("KnowledgeBase/SDD/changes/*/tasks.md"):
        if tasks.parent.parent.name != "changes" or tasks.parent.parent.parent.name != "SDD":
            continue
        if value(tasks.read_text(encoding="utf-8"), "Status") == "active":
            active.append(tasks.relative_to(ROOT))
    if len(active) > 1:
        errors.append("В рабочем дереве несколько активных планов: " + ", ".join(map(str, active)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-check", action="store_true", help="Проверить приёмку и перенос до перемещения change")
    parser.add_argument("change", type=Path, help="Путь к одному открытому SDD change")
    args = parser.parse_args()
    errors: list[str] = []
    change = (ROOT / args.change).resolve()
    if not change.is_relative_to(ROOT) or not change.is_dir():
        parser.error("change должен быть существующим каталогом внутри репозитория")
    sdd = change_root(change, errors)
    spec_file, tasks_file, design_file = (change / name for name in ("spec.md", "tasks.md", "design.md"))
    for file in (spec_file, tasks_file):
        if not file.is_file():
            errors.append(f"{file.relative_to(ROOT)}: отсутствует")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    spec = spec_file.read_text(encoding="utf-8")
    tasks = tasks_file.read_text(encoding="utf-8")
    if value(spec, "Status") != "accepted":
        errors.append("spec.md: нужен Status: accepted")
    impact = value(spec, "Contract impact")
    if impact not in {"changed", "none"}:
        errors.append("spec.md: Contract impact должен быть changed или none")
    canonical = value(spec, "Canonical spec") or ""
    if impact == "changed" and canonical == "none":
        errors.append("spec.md: для Contract impact: changed нужен Canonical spec")
    if impact == "none" and canonical != "none":
        errors.append("spec.md: для Contract impact: none нужен Canonical spec: none")
    if canonical != "none":
        target_for("", canonical, errors, "Canonical spec")
    for heading in ("Исходные материалы", "Согласованные требования пользователя", "Согласованные изменения исходных материалов", "Намерение и границы", "Приёмка"):
        if not section(spec, heading):
            errors.append(f"spec.md: отсутствует раздел {heading}")
    user_requirements = section(spec, "Согласованные требования пользователя") or ""
    if not re.search(r"\bUSR-[A-Za-z0-9-]+\b", user_requirements):
        errors.append("spec.md: нужны согласованные USR-условия")
    if impact == "changed":
        delta = section(spec, "Contract delta")
        if not delta:
            errors.append("spec.md: отсутствует Contract delta")
        else:
            for op in ("ADDED", "MODIFIED", "REMOVED"):
                if section(delta, op, 3) is None:
                    errors.append(f"spec.md: отсутствует раздел {op}")
            removed = section(delta, "REMOVED", 3) or ""
            if re.search(r"\bREQ-[A-Za-z0-9-]+\b", removed) and not blocks(spec, "REMOVED"):
                errors.append("spec.md: REMOVED требует блоки #### REQ-* с владельцем требования")
            all_ids = [match.group(1) for op in ("ADDED", "MODIFIED", "REMOVED") for match in REQ.finditer(section(delta, op, 3) or "")]
            if len(all_ids) != len(set(all_ids)):
                errors.append("spec.md: один REQ встречается в нескольких операциях")
            if not all_ids and all((section(delta, op, 3) or "").strip() == "none" for op in ("ADDED", "MODIFIED", "REMOVED")):
                errors.append("spec.md: Contract impact: changed требует дельту требований")
    design = value(tasks, "Design")
    if design_file.exists():
        if design != "design.md" or value(design_file.read_text(encoding="utf-8"), "Status") != "accepted":
            errors.append("design.md: файл должен быть accepted и указан в tasks.md")
    elif design != "none":
        errors.append("tasks.md: без design.md требуется Design: none")
    validate_tasks(tasks, args.archive_check, errors)
    validate_active_count(errors)
    archive_path = None
    if args.archive_check and sdd:
        archive_raw = value(tasks, "Archive path") or ""
        archive_path = path_in_repo(archive_raw, errors, "Archive path")
        if archive_path and archive_path.parent != sdd / "changes" / "archive":
            errors.append("Archive path должен находиться в archive/ того же SDD-корня")
        if archive_path and archive_path.exists():
            errors.append("Archive path уже существует")
        if archive_path:
            validate_merge(spec, archive_path, errors)
    for file in (spec_file, tasks_file, design_file):
        if file.is_file():
            future = archive_path / file.name if archive_path else None
            validate_links(file, future, change, errors)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"SDD structure: ok — {change.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
