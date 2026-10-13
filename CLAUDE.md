# Инструкции для Claude

Общие правила репозитория и выбор маршрута находятся в [`AGENTS.md`](AGENTS.md). Индекс общей базы знаний — в [`KnowledgeBase/index.md`](KnowledgeBase/index.md), общие SDD-артефакты — в [`KnowledgeBase/SDD/`](KnowledgeBase/SDD/README.md), действующий процесс — в [`workflow/README.md`](workflow/README.md). Для сырой идеи и проработки до контракта используйте [предварительную проработку](workflow/product-shaping.md) через соответствующий скилл.

При работе с конкретным подпроектом также учитывайте его локальные инструкции, индекс и SDD в `<подпроект>/KnowledgeBase/`, если они есть.

Проектный [Execution Ledger](workflow/ledger.md) настроен получать события Claude Code через `.claude/settings.json`. Он работает пассивно после штатного доверия к проекту; статус «активен» требует подтверждённого события текущей сессии.

[Защита данных](workflow/safety-hooks.md) использует тот же механизм доверия проектным hooks и спрашивает подтверждение для распознанного опасного вызова.

Для изменения самого харнеса используйте [Harness Change](workflow/skills/harness-change/SKILL.md), а для его проверки — [Harness Review](workflow/skills/harness-review/SKILL.md). Все скиллы хранятся в `workflow/skills/` и публикуются через `.claude/skills/`; их список приведён в [`workflow/README.md`](workflow/README.md).
