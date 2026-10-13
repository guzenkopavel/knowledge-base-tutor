# <Название изменения>: план

Status: draft
Spec: spec.md
Design: none
Execution profile: adaptive
Execution scope: change
Evidence profile: normal

## Evidence strategy

- Primary verifier: <один основной сигнал, область, этап и критерий успеха; none с причиной, если исполнение не нужно>
- Early verification: <конкретный риск для раннего сигнала либо none>
- Escalation: <какая новая причина или изменённая область требует повтора/расширения>

## Сверка плана

Не завершена — после составления плана применить `workflow/plan-review.md` и записать «готов — ...» либо «нужны изменения — ...».

## Задачи

### T-001: <Результат>

- Status: pending
- Depends on: none
- Covers: REQ-1
- Knowledge: none — <почему общие или локальные концепты не применяются>
- Boundaries: <важные ограничения и сохраняемое поведение>
- Done when: <наблюдаемый результат>
- Verification: <выбранный полезный сигнал или причина отложить до финала>

#### Completion

- Changed: pending
- Contract: pending
- Boundaries: pending
- Leading risk: pending
- Evidence: не проверено — <почему, либо команда/инструмент, область, версия, результат и exit code>
- verify_now: no — <почему запуск отложен, уже актуален или нужен после этой задачи>

### T-FINAL: Final review

- Status: pending
- Depends on: T-001
- Covers: все USR/REQ, исходные материалы и весь diff
- Knowledge: объединение фактически относящихся концептов задач и изменений
- Boundaries: сохранить согласованные ограничения и не включать соседнюю работу
- Done when: сверены контракт, реализация, границы и достаточность доказательств
- Verification: один основной полезный сигнал, если он нужен; переиспользовать актуальный результат

#### Completion

- Changed: pending
- Contract: pending
- Boundaries: pending
- Leading risk: pending
- Evidence: не проверено — <почему, либо команда/инструмент, область, версия, результат и exit code>
- Verdict: pending

## Проверки

Общий для задач результат или блокер сохранять здесь один раз: сигнал, область, версия, результат, exit code при запуске и условие повтора. Не создавать запись до первого фактического сигнала.

## Приёмка

- Acceptance: pending
- Acceptance evidence: none
- Archive path: none

После готовности итоговой задачи ставьте `Status: awaiting_acceptance`. `Acceptance: accepted` заполняется только по явной приёмке актуального результата; `Status: complete` — после успешного `workflow/check_sdd.py --archive-check <путь-к-change>` и перед перемещением в архив. Архивный путь находится в `changes/archive/` того же KB.
