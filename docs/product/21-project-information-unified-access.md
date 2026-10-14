# Проектная информация: онтология, общий доступ и Slack ↔ Hub

[Индекс продукта](README.md) · [Доска и анализ](19-board-and-strong-analysis.md) · [Коллаборация](20-basic-collaboration-and-personal-timeline.md) · [Question sessions](../prompts/projects-hub-question-sessions-delivery-20261013.md) · [MCP Events](../mcp-events.md)

**14 октября 2026 · revision 1 · аналитический проектный трек, не реализация и не разрешение на rollout.**
Canonical track ID: `project-information-unified-access-20261014`.

## 1. Основание и границы

[Поручение владельца в исходном Slack-треде](https://app.slack.com/archives/C0C872SJFA7/p1791962574970919?thread_ts=1791919589.989969&cid=C0C872SJFA7):
«Для Миры то доступ к информации через единый фреймворк нужен, ей не нужно знать непосредственно о slack».
«Выдели это как отдельный проектный трек требующий аналитики, оформи её в документации, возможно в отдельном .md документе и корректный роутинг документации обеспечь.»

Это один трек с тремя частями:
1. Какие проектные сведения существуют и как связаны.
2. Как Мира и dot одинаково находят, читают и изменяют разрешённые сведения.
3. Как Slack и Hub согласованно обмениваются обсуждениями, материалами и результатами.

Новый Slack-тред не создаётся. Вопросы ниже подготовлены для этого трека; сейчас они не опубликованы в Hub, Мире или Slack. Голосовой проход начинается **после доказанной готовности соседней question-session системы**, по её действующему consent/priority/pause/result-delivery контракту. Это не новая постановка реализации question sessions.

Архитектурные документы и код принадлежат `onedayonemasterpiece/projects-hub`; `projects-hub-notes` предназначен для пользовательского проектного содержания. Этот документ не переносится в notes-репозиторий ради доступности Мире. Доступ к нему должен обеспечить общий разрешённый read contract и правильно связанный пользовательский аналитический объект.

Изменения этого трека пока только документационные. Нет изменения main, deploy, кода, subscriptions, bindings, grants или credentials. Наличие доступа к репозиторию/Slack не разрешает новую аудиторию, архив всех каналов или публикацию личного разговора.

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
| Discussion | Тема, участники/аудитория, исходный thread и связанные объекты | Hub identity + source identity; Slack сообщения остаются источниками Slack |
| Information item | Заметка, идея, гипотеза, материал, аналитика или решение; title, kind, author, revision, source refs | Один выбранный canonical location на объект, без второй редактируемой копии |
| Source reference | Slack message/thread, GitHub document@commit, project note@revision, иной разрешённый источник | Native ID, observed revision/hash, author/time, ACL scope, coverage, readback evidence |
| Analysis | Результат по конкретным версиям evidence, assumptions, unanswered questions и used answer receipts | Существующий analysis domain; полный архитектурный документ в code/docs repo |
| Question / answer | Адресованный вопрос с reason/priority и durable disposition/ответом | Существующая question-session модель |
| Decision | Явно принятое решение с автором/полномочием, датой, основанием, supersedes | Канонический item либо типизированная запись существующего домена; summary не заменяет evidence |
| Task | Обязательство/действие, ответственный, статус, срок при наличии | Существующий task domain; канбан является представлением статусов задач |
| Board card | Представление item/task/analysis с canonical ID и revision | Существующая одна доска проекта; карточка не создаёт новую копию текста |

Обычная заметка, гипотеза или материал **не обязаны становиться задачей**. Полезные связи: `supports`, `contradicts`, `derived_from`, `discussed_in`, `answers`, `decides`, `requires_task`, `supersedes`. Начать с нужных этому сценарию типов связи, не строить заранее полный семантический граф.

Не смешивать статусы: гипотеза «не проверена» не означает task «todo»; analysis «устарел относительно source» не означает task «done»; discussion «пауза» не завершает вопросы. Продвижение на канбане меняет связанную задачу лишь при соответствующем разрешённом действии.

Один проект может иметь несколько Slack-каналов. Канал может охватывать несколько обсуждений; отдельное обсуждение имеет точный root thread. Возможность одного канала обслуживать несколько проектов остаётся решением §9: пока project hint не выбирается по каналу автоматически при неоднозначности. Для первого среза сохраняется одна primary delivery route на обсуждение. Будущие дополнительные routes не должны молча ослаблять нынешние UNIQUE/ACL инварианты.

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

Для этого трека source — именно данный canonical документ и связанная с ним аналитическая работа, а не старая тестовая заметка. Runtime project/note/analysis/binding IDs **не назначены и не выдуманы**. Если существующий adapter требует note anchor, перед использованием нужен правильный track-specific anchor с ссылкой на code/docs SHA и подтверждённым маршрутом; отдельное согласование persistent binding остаётся обязательным. Нельзя отправить эти вопросы в старый тестовый binding ради прохождения схемы.

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
6. Сверять доступ на search/read и перед отправкой. Slack member может не быть Hub reader и наоборот; нужен пересекающийся разрешённый audience. При revoke инвалидировать caches/working set, не только скрыть ссылку.
7. На 429 соблюдать Retry-After; на access failure не пытаться альтернативным токеном. Недоступные старые сообщения остаются gap. Отдельно разрешённый импорт архива возможен только с проверкой источника, аудитории и полноты, не как обход Free retention.
8. Начать с SQLite metadata + существующего source storage/простого полнотекстового поиска. Векторный индекс, новый broker и отдельный indexing fleet — только после измеренного недостатка.

Hub не является скрытым способом бессрочно хранить Slack. Нужно выбрать, какие **самостоятельно принятые проектные материалы** живут по собственной согласованной политике, а какие являются временным Slack cache. Это P0-вопрос, а не вывод из технической возможности экспортировать.

## 7. Sync и failure matrix

| Событие/сбой | Durable результат и восстановление | Что видит пользователь / guard |
| --- | --- | --- |
| Повтор ingestion или event | Тот же source/event key; повтор не создаёт item/анализ | Нет дубля и самопроизвольного поручения |
| Slack edit во время чтения | Новая observed revision; invalidation производных; reread | Анализ помечен source changed |
| Slack delete / retention gap | Tombstone либо явный недоступный interval, политика удаления cache | Старый snippet не показывается как актуальный |
| Project/Slack ACL revoke | Проверка каждой операции; closed access, invalidation working set | Не раскрываются даже названия/сниппеты |
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

**Gate 0. Соседняя система.** Проверить на актуальном exact SHA её полный голосовой цикл: предложение/согласие → ответ → interruption/resume → явное завершение → idle dot → новая версия → исходный Slack receipt → Mira brief → restart. Никакие вопросы этого трека не публиковать до её готовности и проверки track-specific source/route/permission.

**Этап 1. Инвентаризация и read-only lookup.** Один проект, минимум два разрешённых канала/обсуждения, связанный документ и нетасковая заметка. Сопоставить нынешние IDs/roles; добавить общий bounded read envelope поверх существующих services.
Приёмка: Мира и dot находят одинаковые разрешённые объекты/версии по описанию; неоднозначность уточняется; ACL deny не раскрывает snippets; большой результат корректно дочитывается. Никакой отправки или миграции всех данных.

**Этап 2. Семантический reply.** Одна правильно связанная тема: голос → summary discussion reply → исходный Slack.
Приёмка: правильное авторство/содержание, точный root, current grants; raw transcript/audio не публикуются; retry/restart/unknown-send не дают дубля. Подтверждены отдельные save/delivery states.

**Этап 3. Ограниченный history coverage.** Одна согласованная область и интервал; backfill + incremental reconciliation.
Приёмка: roots/replies/pagination, edit/delete, revoke, 429, пропуск события и restart. Отчёт показывает недоступную Free историю как gap. Сверка проверяет полноту в выбранном доступном окне, не только count.

**Этап 4. Информация ↔ задачи/доска.** Связать нетасковые материалы, решения и существующие task/status views.
Приёмка: материал остаётся материалом; задача явно создаётся/изменяется; board card читает canonical revision; обновление source не создаёт новый task и не меняет статус без команды.

**Этап 5. Этот аналитический цикл.** После Gate 0 и решений P0 активировать только разрешённый track-specific package. Ответы приводят к revision 2 данного анализа/связанного результата; ссылка и краткий итог возвращаются в тот же Slack-тред, Мира сообщает готовность.
Приёмка: evidence/used receipts/expected revision, сохранение blockers, отсутствие чужого тестового note ID, duplicate analysis/delivery, новых subscriptions/grants.

Каждый этап имеет independent stop condition и отчёт «проверено / не проверено / blocker». Реальные голосовые, MCP и Slack проверки не заменяются mocks или schema PASS. Перед реализацией перепроверить свежий code/deployed SHA; исторические тестовые числа из соседних отчётов не переносить на этот трек.

## 9. Подготовленные вопросы для голосового обсуждения

Состояние всех пакетов: **draft, not published**. Общий источник: track ID этого документа + его immutable GitHub commit. IDs ниже — proposed external question IDs, не созданные runtime records. Адресат — владелец; actor ID разрешается через существующую verified identity. Пакеты небольшие, Мира задаёт по одному после согласия; после ответа dot проверяет, не исчезла ли необходимость следующего.

### Пакет A · P0 · доступ и долговечность

1. `pi-archive-policy-01` · critical / blocking  
   **Вопрос:** Что должно оставаться самостоятельной проектной памятью после исчезновения сообщения Slack: только явно сохранённые заметки/решения или также полные обсуждения выбранных каналов?  
   **Почему:** Free history и cache retention нельзя приравнивать к разрешению на постоянный архив.  
   **Влияет:** retention, purge/tombstones, storage и объём backfill. Предложение: source cache по согласованной политике; durable материалы только с явным проектным назначением.

2. `pi-audience-policy-01` · critical / blocking  
   **Вопрос:** Если участник видит проект в Hub, но не входит в связанный Slack-канал, какие материалы этого канала ему разрешено читать?  
   **Почему:** Project membership само по себе не переносит Slack audience.  
   **Влияет:** intersection ACL либо явная публикация выбранного материала в project audience. До решения доступ к Slack-source закрыт.

3. `pi-route-policy-01` · high / blocking для writes  
   **Вопрос:** У обсуждения сохраняем один основной тред для ответов, даже если у проекта несколько каналов? А один канал может относиться сразу к нескольким проектам?  
   **Почему:** Сейчас binding точечный; неоднозначный маршрут опаснее неудобного уточнения.  
   **Влияет:** channel mapping и resolution; предложен один primary route и уточнение при неоднозначности.

### Пакет B · P1 · повседневный голосовой сценарий

1. `pi-first-scenario-01` · high  
   **Вопрос:** Что первым должно работать с телефона: найти старое обсуждение, ответить в него голосом или найти все решения по теме?  
   **Почему:** Определяет самый короткий полезный read/write срез.  
   **Влияет:** порядок этапов 1–3 и первая реальная приёмка; не требует автоматического архива всех каналов.

2. `pi-summary-review-01` · high  
   **Вопрос:** Перед обычной отправкой смыслового ответа Мира должна прочитать подготовленный текст или достаточно кратко подтвердить, что она передаёт, когда ты прямо просишь отправить?  
   **Почему:** Нужно сохранить смысл без лишней диктовки и не выдавать пересказ за дословную речь.  
   **Влияет:** voice UX; чувствительные/существенные действия всё равно проходят применимое подтверждение. Это обсуждение дизайна, не новое standing разрешение на отправку.

3. `pi-information-kinds-01` · normal  
   **Вопрос:** Какие различия действительно нужны на первом экране: заметка/материал/идея/гипотеза/решение или достаточно общего материала с простыми метками?  
   **Почему:** Не каждый вид требует своей формы и workflow.  
   **Влияет:** минимальная ontology/UI, без превращения всего в задачи.

### Пакет C · P2 · изменения и связь с работой

1. `pi-slack-editing-01` · normal  
   **Вопрос:** Slack пока оставляем для обсуждений и ссылок или нужен явный способ редактировать канонический документ из треда?  
   **Почему:** Двусторонний редактор значительно расширяет конфликты и права; voice-first путь уже основной.  
   **Влияет:** optional edit command, expected revision и приёмка; предложение отложить editing до надёжного reply.

2. `pi-task-linking-01` · normal  
   **Вопрос:** Когда из материала появляется задача: только по твоей прямой просьбе или Мира может предложить конкретное действие для подтверждения?  
   **Почему:** Материал и обязательство имеют разную семантику.  
   **Влияет:** создание task и связи с канбаном; никакого автоматического превращения всей аналитики в backlog.

3. `pi-history-window-01` · normal  
   **Вопрос:** С какого конкретного проекта, каналов и периода начать проверку истории, чтобы результат был полезен сразу?  
   **Почему:** Полнота проверяема в ограниченном доступном окне; старые данные могут отсутствовать.  
   **Влияет:** первый corpus, latency и критерии backfill; настройки retention и текущий access ещё нужно прочитать.

## 10. Рекомендуемое решение до ответов

Начать с общей read модели, двух разрешённых обсуждений одного проекта и одного голосового semantic reply. Сохранить существующие note/analysis/question/task domains, одну доску, command receipts/outbox и тонкие source adapters. Не вводить новый оркестратор, обязательный Slack-редактор, бесконечный архив, новую систему задач или отдельный search-agent.

Непринятые решения §9 остаются открытыми; предложения не считаются согласованными. После голосового обсуждения обновить этот же canonical документ новой revision, указать реальные answer receipts и принятое/отложенное, не размножать конкурирующие спецификации.
