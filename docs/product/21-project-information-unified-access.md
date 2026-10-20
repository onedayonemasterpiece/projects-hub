# Проектная информация: онтология, общий доступ и Slack ↔ Hub

[Индекс продукта](README.md) · [Доска и анализ](19-board-and-strong-analysis.md) · [Коллаборация](20-basic-collaboration-and-personal-timeline.md) · [Question sessions](../prompts/projects-hub-question-sessions-delivery-20261013.md) · [MCP Events](../mcp-events.md)

**14 октября 2026 · revision 2 · решения после реальных ответов владельца; аналитический проектный трек, не реализация и не разрешение на rollout.**
Canonical track ID: `project-information-unified-access-20261014`.

## 1. Основание и границы

[Поручение владельца в исходном Slack-треде](https://app.slack.com/archives/C0C872SJFA7/p1791962574970919?thread_ts=1791919589.989969&cid=C0C872SJFA7):
«Для Миры то доступ к информации через единый фреймворк нужен, ей не нужно знать непосредственно о slack».
«Выдели это как отдельный проектный трек требующий аналитики, оформи её в документации, возможно в отдельном .md документе и корректный роутинг документации обеспечь.»

Это один трек с тремя частями:
1. Какие проектные сведения существуют и как связаны.
2. Как Мира и dot одинаково находят, читают и изменяют разрешённые сведения.
3. Как Slack и Hub согласованно обмениваются обсуждениями, материалами и результатами.

Новый Slack-тред не создаётся. Реальный пакет вопросов этого трека завершён владельцем: четыре содержательных ответа и два явных пропуска; решения и открытые части сведены в §9. Применяется существующий consent/priority/pause/result-delivery контракт. Вопросные сессии и предмет этого документа взаимосвязаны: временная note-centric привязка помогла провести пилот, но не определяет целевую структуру информации.

Архитектурные документы и код принадлежат `onedayonemasterpiece/projects-hub`; `projects-hub-notes` предназначен для пользовательского проектного содержания. Этот документ не переносится в notes-репозиторий ради доступности Мире. Доступ к нему должен обеспечить общий разрешённый read contract и правильно связанный пользовательский аналитический объект.

Эта revision меняет только документацию в её отдельной ветке, не main, deploy, код, subscriptions, bindings, grants или credentials. Отдельно разрешённый пилот использовал временную заметку и точную привязку исходного треда (§5C). Принятая проектная аудитория и хранение обсуждений — целевая политика; подключение конкретных каналов и расширение действующих доступов требуют самостоятельной авторизованной операции. Личные разговоры и посторонний контент не становятся разрешённым импортом автоматически.

## 2. AS-IS: на что уже можно опереться

Проверенный снимок: docs commit `89c5f9b97168f3a92edb8d72e73f1ec8e736b21b`, parent code `d2cad6f6c171ae3eb54ca80ae4eb18aff003b82c`. Это факт чтения кода, не проверка текущего production или live acceptance. Датированные отчёты остаются свидетельствами своего снимка.

| Область | Найдено в коде/контракте | Граница |
| --- | --- | --- |
| Проектная заметка | [collaboration.py](../../src/projects_hub/collaboration.py): project_notes, note intents, discussion entries, collaboration events; create/get/list/reply, repository readback, personal brief | Не универсальный каталог материалов и не Slack-архив |
| Доска | [board.py](../../src/projects_hub/board.py): одна board на project, versioned objects/events/commands; типы sticky/document_card, ссылки analysis_run/document/https | Имеющуюся доску развивать ссылками на канонические объекты; из её наличия не следует готовность всей модели задач/канбана |
| Анализы и вопросы | [collaboration_analysis.py](../../src/projects_hub/collaboration_analysis.py): durable analyses/questions/answer commands/jobs; dot package, consent/control, receipt, source/binding revision | Внешний source требует checkpoint; source_verified выставляется непосредственно для project_note, не любого внешнего source |
| Live tools | [live_capabilities.py](../../src/projects_hub/live_capabilities.py): project_notes_list/project_note_get и collaboration inbox/answer/request в capability bundles | Мира умеет предметные операции, но единого межисточникового lookup здесь ещё нет |
| MCP | [mcp_api.py](../../src/projects_hub/mcp_api.py): start_question_session, get_project_request, record_request_result и два legacy test tools | Пять request-scoped tools; не выдавать предполагаемые search/read за уже доступные tools |
| Маршрут | [mcp_bindings.py](../../src/projects_hub/mcp_bindings.py): owner-approved project/note → workspace/channel/thread, revision/history, questions_allowed | UNIQUE(project_id,note_id) и UNIQUE(workspace,channel,thread) ограничивают текущую связь; это не готовая модель project↔несколько channels или note↔несколько threads |
| Доставка | [mcp_events.py](../../src/projects_hub/mcp_events.py), [MCP contract](../mcp-events.md): durable request/outbox, correlated result, Slack claim/receipt/reconciliation | Callback accepted ≠ результат сохранён; available Мире ≠ она уведомила человека |
| Соседняя работа | [delivery checkpoint](../reports/question-sessions-delivery-20261014.md), [действующая постановка](../prompts/projects-hub-question-sessions-delivery-20261013.md) | Снимок отчёта перечисляет незакрытую реальную приёмку. Этот трек её не объявляет завершённой |

Ранний [MCP analysis prompt](../prompts/projects-hub-analysis-questions-mcp-20261013.md) описывал active-session read как достаточный первый срез. Более поздняя delivery-постановка требует idle-dot continuation, новой версии, исходного Slack и уведомления Миры; она имеет приоритет для полного цикла. Не возвращать старое упрощение через этот трек.

### Три конкретных пробела

- Нет доказанного общего bounded каталога/поиска/чтения источников для Live и MCP с одинаковыми ACL, provenance и coverage.
- Привязка обсуждения сейчас точечная и note-centric; список каналов проекта, выбор подходящего треда по описанию и самостоятельные материалы требуют явного контракта.
- Существующие event/result tools решают корреляцию конкретного запроса. Они не выполняют historical Slack backfill, edit/delete reconciliation и не подтверждают полноту архива.

## 3. TO-BE: простая модель информации

Одна небольшая доменная модель поверх нынешних store/services. Это **логические роли**, не требование новой таблицы для каждой строки, graph database, universal workflow engine или второго агента.

| Сущность/роль | Значение и минимальные связи | Каноническое владение |
| --- | --- | --- |
| Project | Стабильный ID, имя/aliases, текущие участники/grants, repo bindings | Текущий Hub store; каталог может иметь несколько channel links |
| Channel link | Связь project с внешним channel и назначением/областью поиска | Авторизованная интеграционная конфигурация; не право отправки в любой thread |
| Topic / Discussion | Тема объединяет несколько тредов; полное обсуждение хранится отдельно от выделенных решений | Hub identity + native thread/message identities; одна тема не равна одному Slack-треду |
| Information item | Заметка, идея, гипотеза, материал, аналитика или решение; title, kind, author, revision, source refs | Один выбранный canonical location на объект, без второй редактируемой копии |
| Source reference | Slack message/thread, GitHub document@commit, project note@revision, иной разрешённый источник | Native ID, observed revision/hash, author/time, ACL scope, coverage, readback evidence |
| Analysis | Результат по конкретным версиям evidence, assumptions, unanswered questions и used answer receipts | Существующий analysis domain; полный архитектурный документ в code/docs repo |
| Question / answer | Самостоятельный адресованный вопрос с reason/priority, одним породившим typed source и durable disposition/ответом | Существующая question-session модель; task/document/note, без обязательной заметки-посредника |
| Decision | Явно принятое решение с автором/полномочием, датой, основанием, supersedes | Канонический item либо типизированная запись существующего домена; summary не заменяет evidence |
| Task | Поручение человеку или агенту: requester, assignee human/agent, состояние, результат, evidence и blocker; срок при наличии | Существующий task domain; канбан является представлением статусов задач |
| Board card | Представление item/task/analysis с canonical ID и revision | Существующая одна доска проекта; карточка не создаёт новую копию текста |

Обычная заметка, гипотеза или материал **не обязаны становиться задачей**. Полезные связи: `supports`, `contradicts`, `derived_from`, `discussed_in`, `answers`, `decides`, `requires_task`, `supersedes`. Начать с нужных этому сценарию типов связи, не строить заранее полный семантический граф.

Не смешивать статусы: гипотеза «не проверена» не означает task «todo»; analysis «устарел относительно source» не означает task «done»; discussion «пауза» не завершает вопросы. Продвижение на канбане меняет связанную задачу лишь при соответствующем разрешённом действии.

Принятая кардинальность: один Slack-канал принадлежит одному проекту; проект может иметь несколько каналов; тема может иметь несколько тредов. Хранить topic → thread references, не склеивать тему с физическим thread. Ответ на конкретное обращение идёт в его исходный сохранённый тред. Для нового обращения к теме без назначения правило выбора ещё не определено: нужен явный маршрут либо уточнение, а не рассылка во все треды. Нынешние UNIQUE ограничения binding — ограничение реализации, а не целевая кардинальность.

Каждый вопрос имеет ровно один породивший источник: source_kind/source_id/source_revision для задачи, документа или заметки. Дополнительные evidence refs допустимы как контекст, но не заменяют единственный исходный объект. Поведение открытых вопросов при изменении источника ещё не выбрано; хранить provenance и показывать конфликт версий, не приписывать владельцу политику автоматической замены пакета.

### Люди и агенты как исполнители

Модель должна поддерживать совместную работу живых пользователей и агентов, включая dot. Поручение человеку и поручение агенту — одна задача существующего task domain, а не два независимых workflow.

Минимальный контракт:
- `requester`: кто поставил задачу, его проверенная identity и основание полномочий.
- `assignee`: тип `human | agent` и стабильный ID конкретного исполнителя; имя/alias служит отображению, не авторизации.
- Цель, проект, исходные материалы/версии, ожидаемый результат и критерий готовности; при необходимости срок и зависимости.
- Состояние работы, сохранённый результат/ссылка, доказательства выполнения и проверки; явный blocker, кто и какое действие должен выполнить для разблокировки.
- История назначения, ответов, передачи и завершения с авторством. Сообщение агента «готово» не заменяет проверяемый результат; ответ человека также сохраняется со своим авторством.
- Агент действует от собственной service/agent identity в делегированном scope: разрешённые действия, источники, назначения и ограничения. Он не подменяет личность владельца; requester, owner, исполнитель, автор решения и publisher различимы. Передача задачи агенту сама по себе не расширяет доступ.
- Человек может получить вопрос/поручение и вернуть ответ/результат; агент может получить задачу анализа/исполнения и вернуть artifact, evidence либо blocker. Это не превращает каждое обсуждение или вопрос в задачу автоматически.

Реализовать недостающие поля/связи поверх существующих tasks, participants, questions и receipts. Приёмка включает одну задачу человеку и одну dot в одном проекте: назначение различимо, результаты проверяемы, blocker адресуется нужному участнику, попытка агента выйти за scope отклоняется. Новый orchestrator или универсальная workflow-платформа для этого не нужны.

### Авторство и версии

- Human author, integration principal, semantic producer (Мира/dot), publisher и исходный Slack actor различимы.
- GitHub canonical document адресуется repo + path + commit/blob SHA; Hub связывает его логической revision. Slack revision отражает observed edit timestamp/hash; Hub revision — монотонная domain revision. Эти числа не взаимозаменяемы.
- Запись использует expected revision и command ID + payload hash в scope principal/project/object. Повтор того же содержимого возвращает прежний receipt; другой payload с тем же ID даёт conflict.
- Concurrent edit не разрешается last-write-wins. Сначала перечитать новую версию; независимые изменения можно согласованно объединить, смысловой конфликт возвращается автору/пользователю.
- Accepted decision изменяется новой записью с supersedes и основанием. Удаление Slack-источника не притворяется отменой отдельно принятого решения, но дальнейшее хранение решения/цитат подчиняется согласованной retention/privacy policy.
- Доска, search snippets и краткие сводки — производные проекции. Source invalidation помечает их устаревшими; прежний summary не выдаётся как актуальное доказательство.

## 4. Shared lookup contract для Миры и dot

Framework предоставляет единый предметный контракт, а provider adapters знают Slack/GitHub/Hub. Мира не получает Slack API, токены, cursor plumbing или правила каждого источника в prompt. dot использует тот же доменный слой через MCP. Совпадают source identities, ACL и write receipts; различаются лишь transport/authenticated principal и доступные capability bundles.

Следующие имена — **предложения логических операций**, не существующие tools и не обязательные публичные названия:

1. `resolve_context(query, project_hint?, object_hint?, cursor?, budget?)`: найти разрешённые project/discussion/item кандидаты по человеческому описанию. Вернуть короткое название, тип, источник, дату, project и признаки совпадения. При неоднозначности Мира задаёт один уточняющий вопрос; confidence не даёт права выбрать адресата записи.
2. `search_information(context_ref, query, kinds?, cursor?, budget?)`: ACL-filtered snippets и refs, scoped coverage; отсутствие результата в неполной выдаче не означает отсутствия факта.
3. `read_information(ref, revision?, section?, cursor?, budget?)`: короткий summary или необходимые страницы полного материала, связанные decisions/questions/tasks и состояние источника.
4. `write_information(action, target_ref, expected_revision, command_id, content, source_refs)`: reuse существующих create/reply/update/task commands. Route выбирается сервером из одобренного binding, а не из свободного текста модели.
5. `get_operation(receipt_or_command)`: состояние canonical save, GitHub materialization, Slack delivery и Mira notification по отдельности.

Первый срез может реализовать resolve/search/read одним небольшим tool с discriminated operation; не нужен tool на каждый kind. Shared framework владеет versioned envelope/capability discovery/bounded paging, Hub — доменными сервисами и ACL, Slack adapter — source fetching/edit/delete/checkpoints. Нельзя скрытно добавить semantic router/classifier: намерение понимает центральная Live-модель, retrieval возвращает данные.

Общий envelope ответа:

- `object_id, kind, project_id, revision, canonical_ref`;
- `source_refs` с native IDs, observed revision/hash, verified permalink, author/time и provenance;
- `scope, checked_at, coverage_state, gaps`;
- `has_more, next_cursor, truncation_reason` и фактический размер страницы;
- `permissions` только допустимых действий, без чужих названий/сниппетов;
- `operation_id/receipt` для изменений и отдельные destination states.

Cursor opaque, привязан к principal/scope/snapshot и не даёт прав. При изменении ACL следующая страница переавторизуется; при изменении snapshot выдача сообщает invalidated/restart-required, а не смешивает версии молча. Объём ответа ограничен независимо от числа совпадений; full read доступен постранично.

### Live context

Сохранён контракт [06-live §13](06-live.md#13-ограниченный-контекст-миры-проверенный-baseline-14-октября-2026) и [memory §14](10-conversation-memory.md#14-slack-история-и-восстановление-рабочего-набора): configured Gemini 3.8 Live, input ceiling 131072 tokens, runtime model ID не подтверждён; compression/resumption включены. Fallback 8 реплик ×700 символов и continuation ≤1200 символов не являются долговременной памятью. ARC byte-based admission units не равны provider tokens.

Bootstrap содержит текущую цель, durable decisions/answer receipts, незакрытые вопросы, ограничения и refs. Большой каталог проектов и Slack-история читаются по запросу. Предложенные numeric budgets в 06-live остаются гипотезами испытаний. Не добавлять второй транспорт, локальную compression policy или новый большой prompt. До голосового подтверждения запись ответа должна иметь durable receipt; reconnect перечитывает state.

## 5. Основные write flows

### A. Телефон → найденное обсуждение → осмысленный ответ в исходном треде

1. Человек говорит: «Вернёмся к обсуждению общей информации для Миры».
2. Мира вызывает общий lookup; получает доступные candidates и короткий контекст. Если совпадений несколько — уточняет проект/тему, не требует Slack ID.
3. Читает актуальные evidence/решения/незакрытые вопросы в пределах рабочего набора.
4. Пользователь формулирует ответ и просит передать в обсуждение. Мира готовит смысловой текст, сохраняя содержание, оговорки и авторство. Не публикует сырое аудио/транскрипт и не отправляет личные побочные фразы.
5. Общий write сохраняет discussion entry + command receipt + authorized delivery intent. Проверяет текущие grants, source audience и exact thread binding.
6. Уже существующий delivery исполнитель выполняет claim → отправку → receipt/readback. Мире доступно отдельно «сохранено» и «доставлено»; при неизвестном исходе она не обещает доставку.

Если текущий binding отсутствует или устарел, поиск/чтение продолжаются, отправка ждёт точной авторизации маршрута. Этот документ сам binding не создаёт.

### B. Slack → разрешённый source → Hub

Ingestion сохраняет native identity и наблюдённую версию сообщения; не превращает каждое сообщение в заметку/задачу. Полезный материал связывается с существующим information item либо создаётся по явному действию пользователя. Source backfill сам не является поручением, не запускает анализ и не пересылает историю Мире целиком.

Slack-edit уже связанного сообщения сначала обновляет source revision и инвалидирует производные summaries/analysis. Изменять canonical Hub/GitHub документ из Slack допустимо только явной edit-командой в будущем опциональном сценарии, с expected revision и правами. Просто редактирование текста Slack не переписывает документ.

### C. dot → вопросы Мире → новый анализ → исходный Slack + краткое уведомление

Применяется действующая [delivery-постановка](../prompts/projects-hub-question-sessions-delivery-20261013.md), без копии её state machine. dot анализирует актуальные источники и составляет приоритетные небольшие packages. Мира предлагает обсудить, ждёт consent, поддерживает important-only/pause/answer/unknown/skip/later. Только разрешённое завершение сохраняет continuation intent.

dot использует сохранённые ответы для новой **содержательной версии** исходного анализа с expected revision, evidence refs и used receipts. Финал — смысловой результат и ссылка в исходном одобренном Slack-треде. Мира получает durable brief notification, подробности читает по просьбе. Частичные ответы и pause не запускают анализ; external dot cycle не запускает повторный Kimi/DeepSeek анализ Hub.

Для этого трека канонический предмет анализа — данный документ. По отдельному разрешению владельца текущий пилот использовал временный anchor `note_d1613b1a2042b9fa32d49a0cbb6c0c7d`, revision1, SHA `f5939c83b13b843897fb66c761df48b1a97ab0e9`; binding `mcp_binding_dfeef222bca1a5722908e4134fc42438`, revision2, исходный root `1791919589.989969`. Реальный пакет: `ca_f2e76645f945618a60478b01274c8380`. Anchor содержит ссылку на документ, не его конкурирующую копию; прежний тестовый binding не используется. Целевая модель допускает прямой typed source документа/задачи без такой заметки. [Сохранённый результат по ответам](https://github.com/onedayonemasterpiece/projects-hub-notes/blob/7795ae6f8ec7555227609800ec4217dda74e14d4/docs/notes/d1/note-d1613b1a2042b9fa32d49a0c.question-result-ca_f2e76645f945618a60478b01274c8380.md) является основанием этой revision; его сохранение само по себе не доказывает Slack delivery или озвучивание Мирой.

## 6. Slack history: backfill, полнота, edits/deletes, ACL

### Проверенные внешние ограничения, 14 октября 2026

- Slack Free предоставляет доступ к последним 90 дням истории. Данные старше года удаляются; retention может быть настроен на 90 дней либо год. Наличие ссылки/пина не доказывает доступность старого текста. Настройки именно этого workspace не проверены. [Free limits](https://slack.com/help/articles/115002422943-Usage-limits-for-free-workspaces), [retention](https://slack.com/help/articles/203457187-Customize-data-retention-in-Slack).
- `conversations.history` и `conversations.replies` имеют разные режимы лимитов по классу приложения: текущие docs сохраняют Tier 3 для Marketplace/internal customer-built; для новых коммерчески распространяемых non-Marketplace apps указан 1 request/minute и 15 объектов. Класс нашей установки нужно проверить, нельзя объявлять 1 rpm универсальным. [history](https://docs.slack.dev/reference/methods/conversations.history/), [replies](https://docs.slack.dev/reference/methods/conversations.replies/).
- History и replies читаются с пагинацией. История канала не заменяет полный обход ответов тредов; неполная/короткая страница сама по себе не доказывает конец. Доступ зависит от токена/scopes/membership. [history](https://docs.slack.dev/reference/methods/conversations.history/), [replies](https://docs.slack.dev/reference/methods/conversations.replies/).
- Events API поддерживает события изменений и удалений сообщений; обработка требует dedup и последующей сверки при пробелах. События не дают отсутствующую историю задним числом. [Events API](https://docs.slack.dev/apis/events-api/), [message_changed](https://docs.slack.dev/reference/events/message/message_changed/), [message_deleted](https://docs.slack.dev/reference/events/message/message_deleted/).

Не устанавливать новые Slack subscriptions/scopes в рамках аналитики. Проверка текущих возможностей и политика архива предшествуют реализации.

### Минимальная стратегия

1. Зафиксировать allowlist разрешённых project/channel/thread scopes, principal, аудиторию и retention policy. Read scope не равен publish scope.
2. Получить bounded snapshot каждой разрешённой области с верхней временной границей; пройти cursors и replies, сохраняя checkpoint после каждой страницы.
3. Хранить coverage ledger: requested interval, actually observed interval, fetched pages/roots/replies, oldest available, last success, gaps/reasons, retention/access unknown. Статусы `partial`, `complete_within_accessible_window`, `access_blocked`, `retention_gap` различимы. «Вся история» допустимо только для доказанного ограниченного интервала.
4. Накладывать incremental events на тот же source key `workspace/channel/message_ts`; event ID — dedup доставки, source key + revision/hash — dedup содержания. Затем сверять небольшой перекрывающийся временной диапазон, чтобы события во время backfill не потерялись.
5. Edit хранит новую наблюдённую revision; delete создаёт tombstone и удаляет/закрывает доступ к индексированному тексту по политике. Старые chunks/summaries не остаются в retrieval как current. Не заявлять восстановление edit history, которого Slack не предоставил.
6. Сверять доступ на search/read и перед отправкой. В выбранной целевой модели обсуждения связанного канала доступны участникам проекта Hub, даже без membership в Slack-канале. Доступ коннектора к исходному каналу и разрешение опубликовать импорт в конкретной проектной аудитории проверяются отдельно. Не требовать пересечения двух memberships как целевую политику. При отзыве проектного доступа инвалидировать caches/working set; последствия source revoke для уже импортированной истории ещё требуют решения.
7. На 429 соблюдать Retry-After; на access failure не пытаться альтернативным токеном. Недоступные старые сообщения остаются gap. Отдельно разрешённый импорт архива возможен только с проверкой источника, аудитории и полноты, не как обход Free retention.
8. Начать с SQLite metadata + существующего source storage/простого полнотекстового поиска. Векторный индекс, новый broker и отдельный indexing fleet — только после измеренного недостатка.

Владелец выбрал хранение полного обсуждения с отдельным выделением решений, а не только curated-выжимки. Полное хранение не означает загрузку всего архива в Live context. Конкретный срок хранения, последствия удаления в Slack, revoke и исключения для чувствительного/постороннего содержания остаются открытыми. Сначала определить разрешённые каналы, проектную аудиторию и границы первого импорта; недоступная история остаётся gap.

## 7. Sync и failure matrix

| Событие/сбой | Durable результат и восстановление | Что видит пользователь / guard |
| --- | --- | --- |
| Повтор ingestion или event | Тот же source/event key; повтор не создаёт item/анализ | Нет дубля и самопроизвольного поручения |
| Slack edit во время чтения | Новая observed revision; invalidation производных; reread | Анализ помечен source changed |
| Slack delete / retention gap | Tombstone либо явный недоступный interval, политика удаления cache | Старый snippet не показывается как актуальный |
| Project ACL / source access revoke | Проектный доступ переавторизуется; source ingestion останавливается при потере доступа; судьба ранее импортированной истории требует политики | Нет раскрытия вне разрешённого проекта; source revoke не подменяется молча новым правилом хранения |
| Неоднозначный lookup | Список разрешённых candidates, без mutation | Один короткий вопрос об уточнении |
| Concurrent Hub/GitHub edit | Expected revision/SHA conflict, сохранение обоих намерений | Перечитать/согласовать, без тихого overwrite |
| GitHub write timeout | Сохранён intent; readback точного path/content/SHA, только затем retry | Не заявлять canonical save по одному запросу |
| Hub save успешен, Slack ошибся | Сохранён результат; destination pending/error отдельно | Можно прочитать в Hub; доставка ещё не подтверждена |
| Slack send outcome неизвестен | Claim остаётся unconfirmed; readback thread по response marker | Нет повторной отправки вслепую |
| Binding сменён/отозван | Старый intent не перенаправляется на новый thread; explicit conflict | Нужен проверенный маршрут, прежние данные сохранены |
| Question pause/partial answers | Consent/filter/answers/receipts durable; continuation отсутствует | Нет преждевременного анализа |
| Compression/reconnect/restart | State по IDs/revisions перечитывается, mutation receipt переиспользуется | Нет потери ответа или повторного вопроса |
| Callback accepted, dot не продолжил | Outbox transport и domain completion различимы | Незакрытый delivery gate, не «готово» |
| Result readable, Mira не уведомила | Notification/seen state отдельно от availability | Не считать read endpoint доставкой |
| Budget/429/temporary outage | Bounded paging/backoff/checkpoint, без повторного business write | Явная неполнота/ожидание, не потеря решения |

Outbox и receipts остаются нынешними механизмами. Требуется небольшой integration слой и совместимые расширения существующих записей; distributed transaction между Slack, SQLite и GitHub не обещается. Надёжность достигается intent → idempotent effect → verified readback/receipt.

## 8. Маленькие этапы и критерии приёмки

**Пилот и надёжность.** Базовый реальный question cycle позволил провести текущую разрешённую аналитическую сессию. Interruption/resume/restart и отдельные отказы не объявляются пройденными по одному успешному циклу; проверять их адресно в затрагиваемом срезе. Не превращать полную матрицу будущего rollout в обязательную церемонию перед каждым небольшим аналитическим пакетом. Source/route/authority проверяются для каждого пакета.

**Этап 1. Инвентаризация и read-only lookup.** Один проект, минимум два разрешённых канала/обсуждения, связанный документ и нетасковая заметка. Сопоставить нынешние IDs/roles; добавить общий bounded read envelope поверх существующих services.
Приёмка: Мира и dot находят одинаковые разрешённые объекты/версии по описанию; неоднозначность уточняется; ACL deny не раскрывает snippets; большой результат корректно дочитывается. Никакой отправки или миграции всех данных.

**Этап 2. Семантический reply.** Одна правильно связанная тема: голос → summary discussion reply → исходный Slack.
Приёмка: правильное авторство/содержание, точный root, current grants; raw transcript/audio не публикуются; retry/restart/unknown-send не дают дубля. Подтверждены отдельные save/delivery states.

**Этап 3. Ограниченный history coverage.** Одна согласованная область и интервал; backfill + incremental reconciliation.
Приёмка: roots/replies/pagination, edit/delete, revoke, 429, пропуск события и restart. Отчёт показывает недоступную Free историю как gap. Сверка проверяет полноту в выбранном доступном окне, не только count.

**Этап 4. Информация ↔ задачи/доска.** Связать нетасковые материалы, решения и существующие task/status views.
Приёмка: материал остаётся материалом; задача явно создаётся/изменяется; board card читает canonical revision; обновление source не создаёт новый task и не меняет статус без команды.

**Этап 5. Аналитический цикл.** Текущий разрешённый пакет завершён; его ответы сведены в эту revision2. Следующие пакеты нужны только для оставшихся существенных решений. Ссылка и краткий итог возвращаются в исходный тред; сохранение, доставка и уведомление Миры подтверждаются раздельно.
Приёмка: evidence/used receipts/expected revision, сохранение blockers, отсутствие чужого тестового note ID, duplicate analysis/delivery, новых subscriptions/grants.

Каждый этап имеет independent stop condition и отчёт «проверено / не проверено / blocker». Реальные голосовые, MCP и Slack проверки не заменяются mocks или schema PASS. Перед реализацией перепроверить свежий code/deployed SHA; исторические тестовые числа из соседних отчётов не переносить на этот трек.

## 9. Решения и открытые вопросы после реальной сессии

Источник: [результат анализа @7795ae6](https://github.com/onedayonemasterpiece/projects-hub-notes/blob/7795ae6f8ec7555227609800ec4217dda74e14d4/docs/notes/d1/note-d1613b1a2042b9fa32d49a0c.question-result-ca_f2e76645f945618a60478b01274c8380.md), blob `a452df9c21e2a9ffbea3fa6ae8946fc98f40120c`. Backend completion_scope=full означает явное завершение сессии, не ответы на все продуктовые вопросы.

### Принято владельцем

1. Хранить полное обсуждение и отдельно выделять решения. Обсуждение, гипотеза, предложение и принятое решение остаются различимыми.
2. Обсуждения связанного канала доступны проектной аудитории Hub, даже если участник не состоит в Slack-канале. Эта запись не меняет действующие ACL.
3. Канал → один проект; проект → несколько каналов; тема → несколько тредов. Конкретная отправка всегда имеет однозначное назначение.
4. Вопрос → один породивший источник (задача, документ или заметка), с сохранённой версией. Вопрос имеет свою структуру; заметка-посредник не является целевым требованием.

Эти ответы заменяют прежние предложения only-curated archive, intersection ACL и одного primary thread на тему; не оставлять их альтернативным действующим контрактом.

### Не решено

- `pi-first-scenario-01`: **skip**, первый сценарий с телефона не выбран.
- `pi-information-kinds-01`: **skip**, категории/метки первого экрана не выбраны.
- Ответ о единственном источнике вопроса **не отвечает** на вторую часть: что делать с открытым вопросом при изменении источника. Переоценка, замена пакета или предупреждение пока не согласованы.
- Retention, delete/revoke, чувствительные исключения и границы первого импорта.
- Выбор назначения для нового обращения к теме с несколькими тредами.
- Ранее предложенные voice summary review, Slack editing, task linking и history window остаются непринятыми предложениями, а не выводами из этой сессии. Не публиковать повторный пакет автоматически.

### Подтверждения

Четыре содержательных ответа и два пропуска представлены receipts:
- `cmd_f0f238c8080fd3e7cb8bbecb7bee2659958d366a`
- `cmd_56cd982cc0935c608670be406384859de21c9502`
- `cmd_c7513a116cc7fc9c3efc7bf55e8e08fa9b3f5043`
- `cmd_1c980eea3ac46b0b7bff0c81f609d518fc40d81a`
- `cmd_d3fd7fd504a8079627a3c648854defb57bcbff97`
- `cmd_0bbc7f6893d216cd2c2339cb57a29ee9c7279aed`

Явное завершение: `cmd_303e039185bfc6a1b06c7a28928ca4d9409a3b67`. Пропуск не означает согласие; открытая часть составного вопроса остаётся открытой независимо от терминального статуса пакета.

## 10. Следующий аудит и минимальная реализация

Требование о людях и агентах-исполнителях добавлено по отдельному прямому уточнению владельца после голосовой сессии; оно не приписывается шести receipts выше.

Этот документ — единая актуальная постановка для Claude и других агентов; результат в notes служит evidence, а не второй конкурирующей спецификацией.

Короткий аудит текущего кода должен дать:
1. Таблицу «требование → существующий service/domain → минимальный недостающий участок». Переиспользовать project, discussion, analysis, question/answer receipts, task, board, repository gateway и outbox.
2. Самый маленький рабочий срез: один проект, два канала, два треда одной темы, документ или задача как прямой источник вопроса. Не строить универсальную платформу заранее.
3. Список временных ограничений для удаления: искусственная note-прослойка, ручное связывание каждого шага, повторные подтверждения уже разрешённого действия и технические IDs в голосовом UX. Сохранить точный scope, идемпотентность и обязательные подтверждения; убрать только ненужные церемонии.
4. Ясную приёмку: Мира и dot находят одно решение из разных тредов; полное обсуждение не смешивается с решением; один источник вопроса сохраняется; stale revision виден; ответ идёт в выбранный исходный тред без дубля; нет раскрытия за пределами разрешённого проекта.
5. Раздельные факты «реализовано / проверено / ещё открыто» и только блокирующие вопросы владельцу. Порядок первого UX-сценария не выбран, поэтому предлагаемый срез обозначать предложением.

Не вводить новый оркестратор, второй semantic router, отдельного search-agent, новую систему задач или распределённый workflow engine. Миграция истории, выдача прав и rollout не следуют автоматически из этой docs-only revision. Следующий агент сначала сверяет актуальный код и источники, затем предлагает небольшой проверяемый patch.
