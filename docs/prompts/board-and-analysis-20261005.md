# Projects Hub — исполнительное окно доски и сильной аналитики

**Task ID: PH-BOARD-ANALYTICS-2026-10-05-R1.** Дата постановки: 5 октября 2026.

Это задача на реализацию, не на новую концепцию. Нужен работающий продуктовый результат: одна коллаборативная доска на проект, управляемая Мирой, и ограниченная аналитика сильными моделями с долговечными документами. Действуй по этой постановке и [спецификации](../product/19-board-and-strong-analysis.md). [Аудит](../audits/board-and-analysis-20261005.md) уже выполнен, [реальная консультация Kimi K3](../audits/kimi-board-analysis-20261005.md) проведена и критически оценена. Не начинай всё с нуля и не повторяй консультацию без конкретного нового вопроса.

Используй GitHub и Codex DevCoveer/OpenCode. ChatGPT самостоятельно читает исходники, принимает содержательные/архитектурные решения и пишет точные изменения кода/документов; OpenCode и прямые инструменты применяют подготовленные изменения, запускают проверки и доставку. Не отправляй весь документ агенту с просьбой независимо переписать проект. Native Codex без отдельного явного выбора владельца не запускать.

## 1. Начни с восстановления фактического состояния

Проверь свежий main, текущий runtime, открытый связанный PR/issue, writer tasks, worktrees и требования. Ветка этого направления: **docs/board-analysis-20261005**. Продолжай её и связанный draft PR; не делай вторую копию задачи/документации. В начале там только четыре Markdown-файла, реализация ещё не запускалась.

Аудит закреплён на main f5155a4834e1f6a3c1983d5c4466a24a6a0e7f93, но это исторический baseline, не целевая версия для отката. В runtime при аудите была 0.1.26 / 3747b87d9b6154d80c5808b3550e7e260367401e. Эти значения перечитать; не выравнивать их reset/deploy старого main.

Параллельная работа по голосу: **PH-VOICE-2026-10-05-R1**, на момент аудита PR #84, branch chatgpt/projects-hub-voice-long-input-20261005, physical project **projects-hub-owner**, HEAD 64872cc69d4e5784d14a23273b1e0cb8dac17af8. Определи её свежий статус. Не используй этот checkout для новых изменений, не сбрасывай/не чисти его, не запускай параллельные тесты с записью в те же каталоги. Если voice PR уже merged, включи принятые изменения в baseline и продолжай собственную задачу.

Прочитай AGENTS и .devcoveer/requirements.json, а также актуальные shared live-interaction контракты. Сохраняй единую Миру, WSS, private conversation isolation, explicit first-party project grants, owner-only development, долговечность голоса и штатное Android self-update. Не ослабляй критические требования для прохождения тестов. Board/analytics требования добавляй как проверяемое расширение существующего контракта, не как его замену.

## 2. Продуктовые цели

Пользователь говорит «Открой доску проекта» — Мира переключает UI в канвас текущего разрешённого проекта. «Закрой доску» возвращает разговор. В обоих случаях работает та же голосовая сессия, микрофон и транскрипция не сбрасываются.

На доске — качественные разноцветные стикеры с редактируемым текстом и реалистичным ощущением бумажного объёма; не PNG с запечённой надписью и не плоский черновой placeholder. Векторная каноническая модель, WebGL-рендерер PixiJS 8, DOM-редактор текста. Малоконтрастная масштабируемая сетка, touch pan/pinch, мышь/trackpad, плавный отменяемый focus, «показать всё», соблюдение reduced motion. Один board на project, два типа v1: sticky и document_card. Не внедряй Three.js или собственный Skia/WASM renderer.

Мира ищет объекты по всей доступной доске, уточняет неоднозначность, фокусирует по актуальному ID/revision на исходном устройстве и может ответить, кто создал и кто изменял объект. Камеры коллег не двигаются. История содержит человека-инициатора и способ выполнения, а не только «автор — AI». Персональная полная расшифровка не становится общей историей проекта.

Сильная модель или ограниченный консилиум анализируют явно выбранные материалы: риски, пограничные случаи, варианты и уточняющие вопросы. Мира кратко обсуждает результат; полный Markdown открывается из беседы/списка результатов и может быть карточкой на доске. В GitHub он материализуется только по разрешённой binding/publication policy. Принятое решение человека и предложение модели различаются.

«Поделись доской» готовит **живую view-only ссылку на семь дней**, с явным предупреждением о видимости будущих правок. Гость открывает без аккаунта, pan/zoom/fullscreen доступны, редактирование/аналитика/личный диалог/приватные связанные документы недоступны. Отзыв работает и для уже открытого WebSocket. В Android — системный chooser через узкую native capability; в PWA голос готовит кнопку для user gesture, если navigator.share нельзя вызвать сразу. Не утверждай доставку сообщения по факту открытия chooser.

Камера→оцифровка рисунка, полноценный Kanban, сложные группы, полноценный offline merge, посимвольный CRDT, произвольные виджеты, бесконечный council и auto-development не входят. Не расширяй scope ими «для архитектурной завершённости».

## 3. Технические обязательства

### A. Права проекта

В проверенном Store bootstrap/list_projects/_project_row использовали workspace membership, а не явный actor→project grant. Проверь, не исправлено ли это в свежем main; переиспользуй один канонический resolver. Если нет — добавь минимальную project-authorized границу и отрицательные тесты для snapshot/search/history/WS/analytics/share. Не выдавай всем workspace members доступ ко всем проектам миграцией. Не переписывай identity/auth систему целиком.

### B. Долговечная совместная доска

Board WSS отделён от voice PCM. Drag/presence — эфемерные типы того же board-сокета, не отдельная инфраструктура. Сервер — authority; per-object expected_revision; command_id + payload hash; state + seq + audit + receipt одной транзакцией; ACK/broadcast после commit. Snapshot + bounded retained tail закрывает gap загрузки и reconnect. Повтор с прежним id не создаёт дубль. Разные объекты не конфликтуют из-за глобальной revision. Тот же объект при stale revision даёт явный конфликт с сохранением draft, а не silent last-write-wins.

Не подставляй свежую expected_revision в старую конфликтную команду автоматически. Tombstone и условный undo не отменяют чужие последующие правки. Проверяй single-process authority до in-memory broadcast. Ограничивай payload/очереди/частоту presence; медленный клиент не забивает сервер. SQLite/WAL переиспользуй; не добавляй Redis/PostgreSQL/CRDT без установленной необходимости.

### C. Live и UI context

Используй shared live-interaction lifecycle, компактный mode/capability overlay и typed product tools. BoardShell не владеет microphone lifecycle и не создаёт вторую provider session. Structured viewport/selection привязаны к actor, conversation, project, board, client instance и interaction/view epoch. Поиск возвращает объект/revision/bbox; клиент подтверждает applied/cancelled. Не считай выполнением только произнесённый ответ Миры.

### D. Изолированная аналитика

Не подключай аналитику к start_codex_task/DevelopmentService: существующий вызов имеет access=write. Новый read-only facade не получает права owner-development. Нельзя дать обычному участнику administrative DevCoveer project/path/task lookup. **Read-only файловый доступ не tenant isolation.** Нужен provided-context-only evidence bundle и технически запрещённое чтение вне него.

Если текущий bridge не обеспечивает этот режим, сделай минимальное дополнение в owning codex-devcoveer-mcp, согласовав фактическую текущую реализацию новой consult_model; не создавай параллельный gateway. Изменения bridge — отдельный узкий PR/патч с тестами и без перезаписи чужой consultant/council работы. До безопасной изоляции обычные участники не получают capability; owner canary не считается завершённой общей аналитикой.

AnalysisRun долговечен, имеет frozen sources/revisions, budget, exact model/effort, provider task binding и статусы. Текущая consult_model schema не имеет request_key: проверь свежую версию; для lost-dispatch добавь bridge-level idempotency/readback либо сохраняй dispatch_unknown без автоматического повтора. Локальный mutex не означает exactly-once через сеть. Результат сохраняется независимо от доступности GitHub; sync и board insertion — отдельные receipts.

Одна консультация — первая рабочая поставка; далее существующий council_run с ограниченным составом/rounds. Free не использует NVIDIA. Paid extended/pro требуют показать точный план и дождаться явного подтверждения, прежде чем вернуть одноразовый токен. Не подменяй недоступную выбранную модель и не запускай native Codex fallback. Каталог/effort брать из реальной capability; не обещать работоспособность всех перечисленных моделей по одному Kimi success.

### E. Share

Отдельный scoped guest grant; hash токена, expiry/revoke на сервере. Guest projection не раскрывает private references, members/emails/audit/conversation. Повторная проверка доступа перед выдачей событий, закрытие действующего сокета при revoke. Исключить guest/private API из service worker cache. Голосовая команда не обходит browser user activation и не выбирает получателя сообщения вместо пользователя.

## 4. Как работать параллельно

Сначала реализуй новые board/* и analytics/* модули, собственные schemas/styles/tests. Общие App.tsx, app.py, store.py, live_adapter.py и package lock уже меняются в voice lane: касайся их минимальными интеграционными патчами после свежего rebase, не переписывай целиком. Не трогай VAD, PCM routing, recovery/replay, transcription truncation, voice budgets, SDK pins, release version/signing в промежуточных поставках.

Новые модули и тесты можно делать до merge voice PR. Финальный merge/release и combined acceptance проводи последовательно на объединённом проверенном baseline. Если пересечение блокирует один адаптер, продолжай независимую часть; не замораживай всю разработку и не обходи конфликт вторым Live framework.

Работай в одном свободном проверенном checkout этого направления либо штатном bounded handoff. Не создавай новый checkout/venv на каждый этап. После interruption сначала readback текущего состояния и продолжение, не повторная постановка. После доставки убери свои завершённые временные артефакты/worktree, сохрани отчёты; чужую активную работу не удаляй.

## 5. Порядок поставок и DoD

Следуй этапам A–F спецификации: ACL/contracts → renderer/sync/history → Live mode/search/focus → isolated single model и bounded council → guest/share → combined acceptance/release. После каждого этапа фиксируй что реально изменено, выполненные тесты и следующий незакрытый пункт. Не ограничивайся планом, созданным issue или mock-демонстрацией.

Обязательны проверки **T01–T28** из спецификации. Особое внимание: concurrent edits; commit-before-ACK crash; reconnect mid-drag; stale object focus; одна учётная запись в двух вкладках; длинная речь и barge-in при открытии/закрытии; hostile input в analytics bundle; double dispatch; отмена/поздний результат; guest expiry/revoke на открытом WS; private MD preview; Markdown XSS; PWA user gesture; combined Live+canvas+analysis нагрузка.

Минимальные доказательства результата: backend tests, frontend build/tests, два независимых браузерных контекста, реальный WSS reconnect, реальная single-model консультация через продуктовый маршрут, реальный разрешённый council, guest без аккаунта, физический Android с микрофоном и штатным обновлением при изменении native слоя. Performance профили — 500 стикеров Android / 1000 desktop как проверяемая стартовая нагрузка, а не готовый benchmark. Запиши frame-time, задержки ACK/focus, память и влияние на Live; не обещай FPS по выбору WebGL.

В итоге обнови документацию и product index, оформи concise acceptance report со ссылками на evidence. Отделяй source, CI, deployed, provider, browser и physical acceptance. Если физического устройства/явного paid approval нет, назови конкретный gate и подготовь воспроизводимую проверку; не помечай его пройденным и не заменяй mock.

**Задача завершена, когда доской и аналитикой реально можно пользоваться, права/история/сохранность корректны, семидневный guest просмотр работает, а текущие исправления голоса и updater сохранены.**
