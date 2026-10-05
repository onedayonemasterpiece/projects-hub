# Аудит: доска Projects Hub и сильная аналитика

**Задача:** PH-BOARD-ANALYTICS-2026-10-05-R1. **Дата:** 05.10.2026.

**Итог:** направление имеет самостоятельную продуктовую ценность и может разрабатываться параллельно голосовому исправлению. Это новая подсистема, а не включение уже существующей доски. До публичной/многопользовательской поставки нужно закрыть явный project ACL, изоляцию аналитического контекста, durability и безопасный guest grant. Полную архитектуру приложения переделывать не требуется.

[Спецификация](../product/19-board-and-strong-analysis.md) · [реальная консультация Kimi](kimi-board-analysis-20261005.md) · [промпт разработки](../prompts/board-and-analysis-20261005.md).

## 1. Объём и границы проверки

Прочитаны свежие голосовые записи IdeaHub, актуальный исходный код Projects Hub через GitHub, requirements/AGENTS и состояние checkout через прямые probes DevCoveer, metadata открытых PR, runtime health и конфигурация backend-сервиса. Изучены первичные материалы Miro, Figma/FigJam, Penpot, PixiJS и MapLibre/MDN. Реально выполнен один вызов consult_model с Kimi K3 через OpenCode/NVIDIA, получен terminal result и штатный Markdown artifact.

В этом аудите не выполнялась реализация доски, не запускалась новая source-development задача, не переключались/очищались рабочие checkout, не перезапускался backend, не менялись voice transport/VAD/лимиты. Документация сохраняется отдельно через GitHub, без нового локального worktree. Графические и многопользовательские тесты будущей доски ещё не могли быть пройдены: такой реализации в проверенной ветке нет.

### 1.1. Зафиксированный срез

| Поверхность | Факт наблюдения | Чего это не доказывает |
| --- | --- | --- |
| Исходный main | f5155a4834e1f6a3c1983d5c4466a24a6a0e7f93; свежий remote_head совпадал с tracking ref в 11:48:17 UTC | Это не обязательно текущая загруженная версия процесса/телефона. |
| Physical project projects-hub | Чистый main checkout при первоначальном git_state | Его нельзя считать свободным для записи без новой проверки активных задач. |
| Voice lane | projects-hub-owner; chatgpt/projects-hub-voice-long-input-20261005; HEAD 64872cc69d4e5784d14a23273b1e0cb8dac17af8; PR #84 открыт при чтении | Это не разрешение менять этот checkout и не конечный статус PR спустя часы. |
| Runtime health | В 11:54:24 UTC HTTP 200, version 0.1.26, release_sha 3747b87d9b6154d80c5808b3550e7e260367401e, sqlite-wal, static_ready=true | Не phone acceptance и не доказательство всех realtime-сценариев. |
| Runtime service | systemd active/running, NRestarts=0 на момент probe, Python scripts/run_server.py из current/source | Само по себе не доказывает число внутренних workers. Это требуется проверить перед in-memory board broadcast. |
| Runtime app file | Зарегистрированный installed-file probe вернул файл release 3747b87…; loaded_process_state=not_observable | Нельзя утверждать точное совпадение файла на диске с импортированным модулем процесса. |
| Consultant | dvt_b09ede2cf805448c9be2f4f353bc6a6c; completed; nvidia/moonshotai/kimi-k3; read; no fallback | Один успех не статистика надёжности провайдера и не гарантия качества модели для всех задач. |

Разница main и runtime фиксируется как факт, не автоматически как ошибка: параллельно идёт voice rollout. Новое окно должно сохранить принятые изменения голоса, а не «выравнивать версии» откатом runtime к старому main.

## 2. Что действительно сказано в голосовых

Основные записи:

**V1** — [voice-20261005-130620-ffe0764d.md](https://github.com/onedayonemasterpiece/idea-hub/blob/7ff18e94d324314aa59cfd27b7d8e988759e9eb8/inbox/voice/2026/10/voice-20261005-130620-ffe0764d.md). В полном тексте прямо назван Projects Hub. Начинать со стикеров; канвас почти на весь экран при остающейся голосовой кнопке; touch pan/pinch; документы Markdown и ссылки на них; Мира понимает текущую видимую область как структуру/вектор. Сильные модели выполняют содержательный анализ вместо попытки делать всю глубокую аналитику Live-моделью. Один консультант или консилиум: риски, пограничные случаи, уточняющие вопросы, письменные отчёты, краткое голосовое изложение. Камера/оцифровка рисунков и полноценный Kanban названы перспективой.

**V2** — [voice-20261005-133011-41d66a29.md](https://github.com/onedayonemasterpiece/idea-hub/blob/7ff18e94d324314aa59cfd27b7d8e988759e9eb8/inbox/voice/2026/10/voice-20261005-133011-41d66a29.md). Гость открывает ссылку в браузере, перемещается/масштабирует, при необходимости включает fullscreen; редактирование недоступно. После размышления о сутках пользователь выбирает **неделю**. По команде «поделись» желателен системный выбор приложения и получателя. Назван Miro.com, прежнее название RealtimeBoard; не следует исследовать unrelated mira.com вместо досок.

Третья свежая запись voice-20261005-101612-a27ac2cb.md касается лекции «Все на завод»; в scope Projects Hub не включена. Автоматические выжимки V1/V2 местами называют IdeaHub и смешивают разрешённые/отложенные решения, поэтому требования извлечены из полного transcript, а не из списка generated tasks.

Прямое дополнение владельца к этому аудиту: открыть/закрыть голосом, WebGL, реалистичная цветная бумага с объёмом, лёгкая сетка, плавный фокус, поиск, creator/editor history. Разделение «одна доска на каждый project» — инженерная интерпретация существующей проектной модели; глобальной общей доски для всех пользователей не предлагается.

## 3. Подтверждённые находки в исходном коде

Все ссылки ниже закреплены на main f5155a4. Последующие изменения требуют нового сравнения, не повторного проектирования с нуля.

### F1. Настоящей канвас-доски ещё нет

Литеральный поиск board по src и web/src дал классы event-board/backlog-board в App.tsx и CSS. Это существующие карточки событий/backlog, не infinite canvas, не scene graph и не board protocol. Поиск analysis дал инженерный prompt внутри development.py, а не сервис пользовательской аналитики. Проверены список модулей, schema Store и Live functions. Отсутствие не утверждается на основании одного пустого GitHub search: это совместный результат инвентаризации и чтения кода.

Следствие: нельзя заявить «подключим уже готовую доску» или считать общий task backlog аналогом новой поверхности. При этом event/task/document primitives полезно переиспользовать как ссылки, не создавать вторые канонические задачи.

### F2. Явный project ACL в проверенных методах отсутствует — высокий приоритет

[store.py](https://github.com/onedayonemasterpiece/projects-hub/blob/f5155a4834e1f6a3c1983d5c4466a24a6a0e7f93/src/projects_hub/store.py), методы _membership, bootstrap, _project_row, list_projects, set_focus: членство проверяется для workspace, список проектов выбирается по workspace_id; _project_row проверяет project_id + workspace_id, без actor grant. get_conversation отдельно сохраняет приватность разговора владельцу actor.

Нельзя распространять workspace-only проверку на guest/board/search/analytics и утверждать, что выполнен protected контракт явных прав проекта. Нужен один project access resolver и отрицательные тесты на все новые входы. Полный rewrite identity plane не нужен. Миграция не должна выдавать доступ всем workspace members по умолчанию.

### F3. Существующий DevCoveer клиент предназначен для owner-development

[devcoveer_client.py](https://github.com/onedayonemasterpiece/projects-hub/blob/f5155a4834e1f6a3c1983d5c4466a24a6a0e7f93/src/projects_hub/devcoveer_client.py): allowlist — codex_status, list_models, start_task, continue_task, read_task. start_codex_task передаёт access=write и provider=codex; status читает Codex catalog. Новые consult_model/council_run в клиенте продукта отсутствуют.

Следствие: нельзя подключить аналитическую кнопку к существующей функции запуска разработки. Нужен отдельный capability facade AnalyticsService, хотя общий низкоуровневый transport можно сохранить. Эта находка передана Kimi заранее и подтверждена им, не является его независимым открытием.

### F4. read-only не равняется tenant-safe — высокий приоритет интеграции

Реальный consultant task использовал read project context для архитектурного аудита владельца. Такой режим подходит этой сессии аудита, но сам по себе не ограничивает чтение только выделенными стикерами обычного участника.

Риск: передача Projects Hub project_id в административный DevCoveer project resolver способна дать модели контекст файлового checkout/других материалов, недоступных инициатору. Запрет записывать код не предотвращает утечку при чтении. Требуется предоставленный immutable evidence bundle и технически запрещённый доступ к произвольным file/shell/MCP/network инструментам. Подмена задачи «изоляция» фразой в prompt недопустима. Kimi отдельно эту проблему не раскрыл; она добавлена в итоговую постановку ChatGPT.

### F5. Разрыв идемпотентности на внешнем dispatch

В текущей опубликованной схеме consult_model аргументы — project, model, purpose, question; request_key не предусмотрен. Локальная уникальность AnalysisRun предотвращает двойной tap, но не отвечает на вопрос «bridge успел запустить inference до сетевого timeout?». Такой же риск необходимо проверить для council_run.

Решение: bridge-level idempotency key/readback или честное dispatch_unknown без blind retry. Это не новая платформа оркестрации, а необходимая граница повторов. Kimi предложил readback по ключу, но не проверил наличие такого ключа во внешнем API; его совет дополнен конкретной недостающей capability.

### F6. Смена режима доски затрагивает уже чувствительный Live UI

[live_adapter.py](https://github.com/onedayonemasterpiece/projects-hub/blob/f5155a4834e1f6a3c1983d5c4466a24a6a0e7f93/src/projects_hub/live_adapter.py), _functions: 15 базовых функций, до 26 с optional knowledge/expert/development наборами. Специальных board capabilities нет. [app.py](https://github.com/onedayonemasterpiece/projects-hub/blob/f5155a4834e1f6a3c1983d5c4466a24a6a0e7f93/src/projects_hub/app.py) содержит выделенный Live socket endpoint с Origin/cookie/ticket проверками.

Новые функции нельзя просто приклеить десятками к постоянному prompt. Board mode — компактный domain overlay через принятый shared Live mechanism; аудиоконтроллер, transcript, источник и очередь tool results должны переживать открытие/закрытие UI. Нет второй Live-сессии и нет regex-router речи.

### F7. SQLite — переиспользуемая база, не готовая досочная транзакционность

[store.py, начало файла](https://github.com/onedayonemasterpiece/projects-hub/blob/f5155a4834e1f6a3c1983d5c4466a24a6a0e7f93/src/projects_hub/store.py#L29-L54): WAL, synchronous=FULL, foreign_keys=ON, threading.RLock. Это полезная существующая основа. Настройки SQLite не делают будущие многошаговые операции атомарными автоматически: state, audit, seq и receipt должны быть в одной явной транзакции.

Для первого выпуска достаточно текущей модели хранилища, коротких сериализованных записей и snapshot + bounded tail. Не нужен PostgreSQL/Redis/CRDT только потому, что появилась совместная доска. Однако in-memory broadcast нельзя применять без проверки, что приложение не обслуживается несколькими независимыми workers.

### F8. Авторство, поиск, камера и доступ — разные сущности

created_by/updated_by не заменяют историю. board_seq не заменяет object_revision. Общий документ не означает общую камеру. «Мира видит экран» не означает, что ей нужно отправлять скриншоты и распознавать уже структурный текст. Общая view-only ссылка не разрешает раскрывать private document previews.

Это проектные риски нового направления, а не найденные уже эксплуатируемые дефекты ещё не написанной доски. Решения: append-only audit, локальная camera, context epochs, ID-based focus/readback, ACL-ограниченная guest projection. Они проверяются сценариями T01–T28 спецификации.

## 4. Что взято из других продуктов

Публичные источники проверены 05.10.2026. Ниже сравнительная интерпретация, не утверждение, что у нас уже такие же производительность или масштаб.

| Источник | Полезный принцип | Что не копировать |
| --- | --- | --- |
| [Miro: мышь/trackpad/touch](https://help.miro.com/hc/en-us/articles/360017731053-Using-Miro-with-a-mouse-trackpad-or-touchscreen) | Разделение pan/select, pinch, восстановление ориентации через fit, понятные controls | Весь toolbar и миникарту до реальной необходимости. |
| [Miro: поиск](https://help.miro.com/hc/en-us/articles/360012140959-How-to-search-in-Miro) | Результат ведёт к конкретному объекту/месту, а не просто открывает доску | Притворяться, что список проектов или LIKE уже обеспечил надёжное смысловое нахождение. |
| [Miro: visitors](https://help.miro.com/hc/en-us/articles/360012524559-Collaboration-with-Visitors) | Простой вход по ссылке, отдельная роль view-only | Автоматически делать связанные документы публичными или обещать непереотправляемую bearer-ссылку. |
| [FigJam: рабочая доска](https://help.figma.com/hc/en-us/articles/14942424871575-Create-your-first-meeting-board-in-FigJam) | Стикеры, лёгкая сетка, компактные инструменты, понятный collaborative canvas | Полноценный Figma-дизайн-редактор и наследование чужого визуального стиля. |
| [Figma: multiplayer, 2019](https://www.figma.com/blog/how-figmas-multiplayer-technology-works/) | Отдельная модель документа, object IDs/properties, server authority и WebSocket; сложность должна соответствовать задаче | Копировать исторический LWW без учёта потери чужого текста; принимать статью 2019 за полный текущий runtime Figma. |
| [Figma: reliability, 2022](https://www.figma.com/blog/making-multiplayer-more-reliable/) | Одних периодических снимков недостаточно для честного «сохранено»; важна долговечность изменений | Переносить распределённую инфраструктуру Figma в небольшой SQLite-продукт. |
| [Penpot: WebGL renderer](https://penpot.app/blog/say-hello-to-background-blur/) | Визуально сложные векторные объекты могут выводиться GPU-рендерером; современный Penpot уже не только старый SVG pipeline | Собственный Rust/WASM/Skia engine ради первых стикеров. |
| [PixiJS Application](https://pixijs.com/8.x/guides/components/application), [Text](https://pixijs.com/8.x/guides/components/scene-objects/text/canvas) | Готовый WebGL renderer; управление density/culling; отдельные канонические строки и производные texture | Перерастеризация всего текста каждый кадр и обещание автоматически плавного UI. |
| [MapLibre camera](https://maplibre.org/maplibre-gl-js/docs/API/classes/Map/) | Плавный совместный pan/zoom, ориентирование, reduced motion | Географические tiles/projections и зависимости для простой доски. |
| [MDN Web Share](https://developer.mozilla.org/en-US/docs/Web/API/Navigator/share) | Системное «Поделиться» с реальным user gesture | Обещание, что произвольный voice callback откроет browser share без нажатия. |

Выбор этой постановки: PixiJS/WebGL + векторная каноническая модель + DOM text editing + видимый 2.5D-объём бумаги. Это компромисс между прямым пожеланием владельца о WebGL, контролем дизайна и отказом от собственного движка. Альтернатива SVG/Canvas2D технически возможна, но её преимущество на нашем устройстве не измерено. Замена стека допустима только с явным объяснением и данными прототипа, не по уверенной фразе консультанта.

## 5. Консультация Kimi: фактический результат и полезность

### 5.1. Как выполнена

Использована **новая consult_model**, не обычная source-development задача. Выбрана Kimi K3, purpose=architecture, access=read. Backend OpenCode, provider NVIDIA. Task dvt_b09ede2cf805448c9be2f4f353bc6a6c завершился без fallback; read_task вернул полный итог и Markdown artifact. [Ответ сохранён здесь](kimi-board-analysis-20261005.md).

Kimi получил уже сформированный дизайн с точными source/voice references, предупреждением о write-only owner path и активном PR #84. Поэтому результат — критика предложенного решения, а не независимое слепое соревнование. Полезность оценивается по тому, что изменилось после ответа, а не по числу абзацев и не по самооценке модели.

### 5.2. Оценка замечаний

| Совет Kimi | Оценка | Изменение постановки |
| --- | --- | --- |
| Отделить analytics от owner-development | Верно, но эта находка уже была в исходном вопросе | Закреплён отдельный capability facade. Новая копия MCP transport не обязательна; важна граница прав. |
| Добавить политику роста/retention | Самое полезное дополнение | Разделены audit и replay tail; квоты/пагинация/наблюдение роста, без тайного удаления истории. |
| Ограничить стоимость и параллелизм council | Полезное усиление | Explicit budget/concurrency, сохранение правил free/paid и Live priority. Готовность общей resource authority не предполагается без проверки. |
| Проверить mode/tool bundle при длинном голосовом turn | Полезный конкретный regression case | Добавлен T11, неизменность микрофона/transcript и отсутствие потерянного следующего turn. |
| Отметить изменение исходной доски после анализа | Полезное UX-уточнение к уже предложенному frozen snapshot | Добавлен source_changed; точное число изменённых объектов только при реальном diff. |
| Отказаться от WebGL, SVG/Canvas2D покроет сотни стикеров | Не принято как вывод без замеров | Сохраняется WebGL/PixiJS; perf gate на устройстве. Альтернатива остаётся гипотезой, не запретом. |
| Упростить replay до snapshot + N последних событий | По сути совпадает с уже предложенным bounded resync | Спецификация формулирует один механизм snapshot + bounded tail, не две системы. |
| Не делать отдельный канал для drag | Полезное снятие неоднозначности, не отказ от отдельного board WSS | Drag/presence — сообщения одного board socket; voice socket остаётся отдельным. |
| Reconnect commit по свежей revision | Требует исправления | Запрещено просто переписать expected_revision; сначала receipt/conflict reconciliation, иначе теряется чужая работа. |
| Revoke проверять на каждом N-м событии, «мгновенно» | Недостаточно строго | Проверка до выдачи данных + push-close при revoke + bounded heartbeat; каждая N-я проверка не гарантирует отсутствие утечки между проверками. |
| Readback аналитики по idempotency key | Правильный принцип, API не проверен | Добавлен явный gap request_key в consult_model и состояние dispatch_unknown. |
| Гостевой доступ как исключение из authenticated isolation | Принимается только уточнённо | Это отдельный scoped capability grant, не отмена проверки прав и не ослабление critical contract. |
| Стикер-диктофон с transcript | Не первая поставка | Уже есть голосовая фиксация в стикер; новый recorder/source pipeline раздвоил бы PH-VOICE. |
| «Скрыть доску» защищает приватность screen sharing | Частично полезно, но защита сформулирована чрезмерно | При close очищается активный viewport; ранее переданный контекст модели или экран уже увидевшего гостя этим не исчезает. |

**Общая оценка: консультация полезна как дополнительный reviewer/checklist, но не как технический арбитр.** Она усилила retention, capacity и проверку voice mode boundary. Значительная часть ответа повторила исходную гипотезу, а renderer/reconnect/revoke рекомендации потребовали коррекции. Два наиболее важных API/security пробела — provided-context isolation и отсутствие внешнего request_key — итоговая ChatGPT-постановка раскрывает глубже ответа Kimi.

Нельзя по одному вызову утверждать, что Kimi надёжнее другой модели или статистически часто доступна. Нет сравнительного бенчмарка, GPU/frame-time измерений, стоимости вызова либо проверки всех доступных моделей; таких цифр в отчёте не придумывается.

## 6. Параллельная работа и риск конфликтов

На момент просмотра PR #84 менял: deploy/devcoveer_install.py, pyproject.toml, voice canary scripts, src/projects_hub/app.py, live_adapter.py, live_runtime.py, logging_config.py, store.py, version.py, voice/backend tests, web/package.json, package-lock.json, web/src/App.tsx, live-interaction.d.ts, serverRecovery.ts, app-voice-lifecycle.test.mjs.

Новое направление должно сначала владеть только новыми board/analytics modules и tests. Входные точки App, router, store migration и Live capability registry — минимальные адаптеры с отдельным сравнением при интеграции. Не брать целый App.tsx/store.py из устаревшего main и не писать новый голосовой controller ради режима доски.

Изолированная разработка и тесты могут идти немедленно; изменение общего runtime/release и combined physical acceptance выполняются последовательно после сведения проверенного voice baseline. База/модели/контракты доски не должны зависеть от незавершённых source mutations в чужом checkout. Версия/подпись/self-update сохраняют существующий release path.

Ветка документации — начало одного нового направления. Новое окно продолжает её и единственный связанный draft PR/issue, а не создаёт десять копий постановки. Перед записью — проверить текущие writer tasks и worktrees; после завершения убрать свои сохранённые временные артефакты. Нельзя «для порядка» удалять активный voice worktree.

## 7. Приоритеты без космолёта

**Неустранимый минимум:** project ACL; один typed document; per-object revision + durable receipt/audit; отдельный board WSS; локальная camera и verified focus; сохранённый Live lifecycle; ограниченный аналитический context и budget; view-only guest projection/expiry/revoke.

**Не нужно заранее:** CRDT/Yjs/OT, отдельная распределённая БД, Redis/Kafka, Three.js/физика бумаги, собственный растеризатор, сложные группы и Kanban, отдельный ASR/recorder, бесконечный council, импорт Figma/Penpot, автоматическое исполнение рекомендаций.

Самый быстрый корректный маршрут — новые модули с простыми контрактами, затем узкая интеграция и настоящая приёмка. Упрощение означает отказ от лишних механизмов, а не отказ от проверки прав и сохранности данных.

## 8. Передача в разработку

Исполнительная задача — [board-and-analysis-20261005.md](../prompts/board-and-analysis-20261005.md), продуктовый контракт — [19-board-and-strong-analysis.md](../product/19-board-and-strong-analysis.md). В нём описаны этапы A–F и 28 обязательных проверок.

На момент завершения этого документа поставлены **анализ, реальная консультация и проектная документация**, не runtime-функциональность. Ожидаемая работа следующего окна — реализация до проверяемого результата, без нового общего аудита с нуля и без вмешательства в незавершённое исправление голоса.
