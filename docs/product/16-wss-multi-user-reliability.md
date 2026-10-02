# Projects Hub — WSS, многопользовательская надёжность и следующий продуктовый этап

**Проектное решение от 2 октября 2026. Статус: target architecture / implementation plan, не production acceptance.**

Этот документ фиксирует следующий обязательный архитектурный переход Projects Hub: realtime-голос переводится на общий WSS-контракт `live-interaction`, а одновременно вводятся явные инварианты многопользовательской изоляции, bounded backpressure, recovery, capability routing и измеримой приёмки.

Цель не в том, чтобы заменить несколько HTTP-запросов одним WebSocket. Цель — сделать голосовой Projects Hub предсказуемым как продукт: пользователь говорит с одной Мирой, несколько людей могут работать параллельно, сбой одного соединения не повреждает чужую сессию, а ни одно действие не объявляется выполненным раньше authoritative readback.

Этот документ дополняет [центрального Live-агента](12-central-live-agent.md), [Live/resource](06-live.md), [надёжность](08-reliability.md), [UX](03-product-and-ux.md) и [runtime](../runtime-devcoveer.md). Общая transport/provider механика остаётся в versioned `live-interaction`; Projects Hub не создаёт собственный несовместимый WSS-протокол.

## 1. Что переносим из Street Story и общего Live framework

Аудит текущего Street Story WSS и `live-interaction` показал набор решений, которые являются общими инфраструктурными улучшениями, а не особенностями Street Story:

- существующий Python backend Projects Hub сохраняется; отдельный Node gateway/sidecar ради WebSocket не вводится;
- HTTP остаётся bootstrap/auth/control plane, realtime media/events переходят на persistent WSS;
- bootstrap выдаёт same-origin относительный `socket_url`, одноразовый короткоживущий `socket_ticket`, `attempt_id` и версию transport protocol;
- ticket связывается с actor/workspace/conversation/session/resource/generation и используется только один раз;
- credential не передаётся в URL; origin, resource, generation и ticket проверяются до admission аудио;
- готовность наступает после protocol `hello_ack`, а не после одного TCP/WebSocket open;
- PCM идёт короткими binary frames с sequence и исходным capture age; server отбрасывает stale/out-of-order frames вместо «догоняющего» воспроизведения старой речи;
- sender имеет bounded ACK window и bounded total queued/unacknowledged audio;
- ACK означает только **relay admission**, не provider comprehension, не tool success и не durable mutation;
- server events и output PCM push-ятся по socket, а не читаются event poller;
- application ping/pong отделяет protocol liveness от TCP-факта;
- после WSS attach session не может молча переключиться на старый HTTP-audio путь;
- gap внутри открытого speech turn помечает input как damaged; новые product mutations блокируются до следующей чистой границы речи;
- уже принятые mutation никогда не replay-ятся после reconnect; unknown outcome сначала reconciles/readback;
- transport, provider, resource, capability transition, tool и authorization failures остаются разными классами ошибок;
- Stop локально немедленный и не ждёт provider shutdown;
- transport/capture generation и playback generation различаются: закрытие provider не должно обрезать уже полученный ответ;
- исправления WSS/framework выпускаются как immutable versioned release с consumer-specific acceptance, а не «подменяются на лету».

Текущий shared candidate Street Story использует рамку порядка `live-interaction 0.3.7-rc.1`. Projects Hub не должен механически копировать номер версии: перед внедрением нужно выбрать актуальный release/candidate, сверить SHA/archive integrity и прогнать собственные regression gates.

## 2. Целевой transport flow

Нормальный online turn:

```text
PWA/Android capture
→ authenticated HTTP session bootstrap
→ one-use WSS ticket + attempt_id + socket_url
→ WSS hello / hello_ack
→ activity_start
→ ordered bounded PCM frames
→ activity_end
→ Projects Hub backend-owned Live session
→ Gemini Live
→ optional typed function call
→ authorized domain/device executor
→ authoritative readback
→ function response to the same Live conversation
→ pushed transcript/audio/events
→ client playback/UI
```

Offline/durable turn остаётся отдельной продуктовой возможностью и не превращается в транспортный reconnect-buffer:

```text
durable local capture
→ sealed local source + chunk hashes/manifest
→ network recovery
→ explicit buffered Live turn
→ manual activity boundary
→ same central Live agent
→ terminal source disposition/readback
→ only then local cleanup
```

Старое правило «не replay stale realtime speech» и продуктовая гарантия «не потерять длительную offline запись» совместимы именно потому, что это **два разных механизма**.

## 3. PWA: WSS first, durable source отдельно

PWA использует общий browser transport/AudioWorklet из `live-interaction`; Projects Hub не создаёт второй `getUserMedia`/AudioContext/WebSocket engine.

При миграции:

1. realtime HTTP input + event polling заменяются shared WSS binding;
2. normal session считается listening только после `hello_ack`;
3. `captureDuringStart` выключается до отдельной приёмки безопасного startup-catchup механизма: нельзя делать вид, что старая очередь пригодна, увеличивая capture-age limit;
4. локальная IndexedDB/offline capture остаётся app-owned durable source layer;
5. при потере realtime WSS последние неподтверждённые frames не выдаются за сохранённые;
6. pending offline source не удаляется до terminal server disposition/readback;
7. старые HTTP Live endpoints могут временно существовать как explicit compatibility/canary path, но **не являются автоматическим fallback** активной WSS-сессии.

## 4. Android: от WebView-микрофона к native reliable voice edge

Текущий Android Projects Hub в основном является WebView-контейнером поверх PWA, плюс уже имеет важные native возможности: Keystore/device binding, Calendar Provider, notifications и signed self-update.

Для устойчивого голосового продукта целевая Android-архитектура следующая:

- UI и продуктовые состояния могут продолжать использовать общий floating-islands frontend;
- microphone capture, native VAD/admission, audio queue, WSS transport и playback переходят в native Android слой через **shared Java LiveSocketTransport** из digest-verified `live-interaction` archive;
- WebView ↔ native bridge остаётся узким typed boundary: start/stop, state/events, transcript projection, playback/status; provider credentials через него не проходят;
- Gemini/API key/GitHub credentials остаются только на backend;
- native transport не является вторым agent/backend;
- Stop останавливает hardware capture немедленно;
- VAD pulse показывает реально admitted speech, а не просто открытую сессию;
- source audio для offline/reboot recovery хранится product-owned durable способом и не смешивается с realtime WSS resend queue.

Из Street Story следует важный урок физического телефона: prepared PCM и emulator не доказывают качество реального микрофона. Источник AudioRecord, VAD, suppression/AEC/NS и playback tail должны диагностироваться на настоящем устройстве. Конкретная Street Story калибровка VAD не копируется вслепую: Projects Hub принимает её как исходную гипотезу и калибрует на собственных trace.

## 5. Многопользовательская модель — обязательный архитектурный инвариант

Projects Hub изначально предназначен не только одному владельцу. Нельзя сначала сделать singleton realtime state, а потом «добавить пользователей».

### 5.1 Изоляция

Минимальный ключ runtime isolation:

```text
actor_id
+ workspace_id
+ conversation_id
+ live_session_id
+ connection_generation
```

Project focus не является правом доступа. Каждый tool call заново проверяется server-side по actor/workspace/project policy.

Запрещены process-global mutable значения вида current user/current project/current session/current tool result. Кэш provider call/result, transition state, queue watermarks, source binding и playback generation принадлежат конкретной сессии.

### 5.2 Одновременные разговоры

Разрешены:

- разные пользователи в одном workspace — независимые личные Live conversations;
- один пользователь в разных conversations — независимые sessions;
- разные workspaces — независимые sessions;
- параллельные read/write actions — только через обычные ACL/idempotency/revision/readback boundaries.

Для **одной и той же conversation** одновременно с двух устройств нужен явный ownership lease:

- normal case — один active microphone owner;
- второе устройство видит, что conversation active;
- takeover — отдельное явное действие с generation bump;
- старый socket после takeover больше не может доставлять audio или mutations;
- никаких двух микрофонов, смешанных в один provider session без отдельной будущей multi-speaker feature.

### 5.3 Backpressure и fairness

Каждая сессия имеет собственные bounded audio/output queues и watermarks. Slow/noisy client не может заполнить глобальную очередь и задержать остальных.

Shared resource controller остаётся отдельным trust boundary:

- lease/key sticky после provider readiness;
- нет key hopping после старта;
- provider/resource denial не обходится локальным ключом;
- concurrency cap и fair admission должны быть измеримыми и явными;
- optional work можно yield/drop по policy, но уже принятая write mutation не исполняется повторно.

## 6. Durable writes, concurrency и хранилище

Для пилота на одном DevCoveer допускается существующий SQLite при соблюдении строгих условий:

- WAL;
- `synchronous=FULL` для критичных данных;
- bounded busy timeout;
- короткие транзакции;
- никакого network/provider ожидания внутри DB transaction;
- serialisation конфликтующих writes на уровне resource/revision;
- idempotency key для команд;
- optimistic revision/expected state там, где несколько участников могут менять общий объект;
- authoritative readback после mutation;
- unknown outcome — reconciliation, а не retry mutation.

SQLite не объявляется бесконечной multi-user архитектурой. Перед multi-instance rollout source-of-truth переносится за `DurableStore` на PostgreSQL/другой принятый shared DB; transport migration не должна одновременно вынуждать такой перенос, если single-host concurrency acceptance проходит.

## 7. Progressive capabilities вместо одного большого tool-каталога

Текущий Projects Hub Live adapter исторически собирает много функций в одну плоскую конфигурацию. С ростом продукта это ухудшает tool selection, увеличивает setup/context cost и усложняет права.

Целевой Live contract следует shared `live-agent-architecture`:

### Core/router

Небольшой стабильный core:
- identity/language/dialogue;
- truthfulness/readback;
- project/workspace context;
- `activate_capability` / release;
- минимальный read-only context.

### Базовые capability bundles

Ориентир: 3–6 functions, обычно <10.

- **memory/context** — read memory, commit grounded source, terminal disposition;
- **calendar/readiness** — devices, calendar command, event cards, checklist/tasks;
- **GitHub/project docs** — connected repositories, bounded reads/search, allowed writes;
- **expert review** — review case/profile/actions/readback;
- **preferences/onboarding** — user theme/preferences, usage/exposure state;
- будущий **shared resources** — files/images/docs available to a group;
- будущий **expert discourse/editorial** — discussion evidence, positions, contradictions, editorial draft.

Capability switch сохраняет **один пользовательский разговор**. Для Gemini конфигурация меняется через безопасную resumption boundary по общему framework; mutation не replay-ится, а пользователь не обязан повторять уже принятое намерение.

## 8. Свежие owner review 2 октября

### 8.1 Light/dark theme

Voice packet `voice-20261002-174500-c34d45c8` прямо требует поддержки светлой и тёмной темы Projects Hub. Основной путь может быть голосовым:

- «Мира, переключи на тёмную тему»;
- «Мира, переключи на светлую тему».

Решение:

- preference actor-scoped, не process/global и не workspace-wide по умолчанию;
- typed tool `ui_theme_set(theme=light|dark|system)`;
- readback возвращает сохранённое preference;
- UI применяет значение без перезапуска разговора;
- optional GUI toggle не является обязательным условием voice-first MVP.

### 8.2 Adaptive onboarding

Тот же review требует ненавязчиво учить человека возможностям продукта.

Целевое поведение:

- при запуске максимум одна короткая полезная подсказка по умолчанию;
- она не перебивает уже начатую пользовательскую задачу;
- hints выбираются из разрешённых capabilities пользователя;
- хранится actor-scoped `first_seen / last_hint / use_count / last_used`;
- давно не использованную функцию допустимо мягко напомнить повторно;
- «расскажи, что ты умеешь» открывает полноценный voice-driven capability tour;
- telemetry оценивает не количество показанных подсказок, а discovery → последующее использование;
- onboarding не должен превращать каждый старт в длинную рекламную реплику.

### 8.3 Expert discourse → editorial publication

Voice packet `voice-20261002-173201-0808cd76` формулирует исследовательское направление: асинхронные мнения нескольких экспертов могут накапливаться, выявлять сильные расхождения/противоречия и затем становиться редакционным материалом.

Это **не обязательный текущий MVP**. Сохраняем как следующий capability track с предварительными условиями:

- отдельная identity/role каждого участника;
- provenance каждой позиции;
- private/shared/public ACL;
- различие «сказал эксперт» и «редакционный синтез модели»;
- participant/editor consent на публичное цитирование/пересказ;
- contradiction detection остаётся semantic/model-owned, deterministic слой лишь хранит evidence/relations;
- draft всегда reviewable;
- publish требует отдельной явной confirmation/readback;
- до реализации провести интервью с потенциальными экспертами.

Эта ветка естественно расширяет уже появившийся expert-review слой, но не должна загрязнять core Live tool list.

## 9. UX-инварианты realtime

Интерфейс должен показывать реальное состояние, а не оптимистичную догадку:

- «Подключаюсь» до `hello_ack`;
- «Слушаю» только когда capture готов;
- pulse — только admitted speech;
- «Думаю/работаю с инструментом» — служебная строка, не фальшивая реплика Миры и не часть model history;
- transport/provider/resource/tool/authorization errors имеют разные human-friendly сообщения;
- «сохранено/создано/изменено» появляется только после readback;
- Stop немедленный;
- восстановление transport не создаёт новый видимый разговор;
- capability transition не заставляет пользователя повторять намерение.

## 10. Observability без нарушения приватности

Default production telemetry хранит идентификаторы и измерения, а не содержимое личной беседы.

Обязательные correlations:

- `attempt_id`, `session_id`, `conversation_id`;
- actor/workspace as opaque IDs;
- connection/provider generation;
- framework/protocol/config digest;
- active capability/transition ID;
- model/provider;
- device/client kind;
- tool name + lifecycle/error code;
- resource grant/denial;
- capture/queue/playback counters.

Обязательные metrics/events:

- session requested/hello/ready/fail/stop;
- WSS ticket issue/consume/reuse rejection;
- socket buffer and capture-age high-water marks;
- admitted/dropped/stale/out-of-order frames;
- first input transcript and first output audio timestamps;
- provider GoAway/resume/close;
- capability transition requested/ready/fail;
- tool start/success/error/readback;
- resource wait/deny;
- playback drain/interrupt;
- disconnect/recovery reason.

По умолчанию не логируются API keys, tickets, resumption handles, raw PCM/images, full transcripts, tool args/results и full system prompt. Временный transcript diagnostic mode возможен отдельно и только как явное диагностическое решение с bounded retention; Street Story 7-day transcript policy не переносится автоматически в Projects Hub.

## 11. Acceptance matrix

Production-ready нельзя вывести из наличия кода или одного provider canary.

### Protocol/contract

Должны проходить:
- wrong/reused/expired ticket;
- wrong origin/resource/generation;
- frame sequence/age/size limits;
- `hello_ack` readiness;
- bounded ACK window/queue;
- slow consumer isolation;
- ping/pong timeout;
- damaged turn → mutation blocked → clean-turn recovery;
- Stop;
- no automatic HTTP fallback;
- duplicate provider call ID does not duplicate transition/write.

### Deterministic concurrency

До public pilot:
- минимум 20 concurrent synthetic WSS sessions в transport/load test;
- минимум 30 минут soak;
- independent actor/workspace/conversation streams;
- mixed read/write workload;
- no cross-session audio/event/state leak;
- no duplicate mutations;
- bounded memory/queue growth;
- slow/failing client does not stall healthy clients.

20 — test headroom, а не обещание бизнесовой ёмкости.

### Real provider concurrency

До заявления, что «несколько человек могут пользоваться одновременно»:
- минимум 3 реальные независимые Live sessions одновременно;
- несколько последовательных turns;
- хотя бы один read + один permitted write/readback;
- одного клиента можно остановить/оборвать без деградации двух других;
- resource accounting/leases проверены по каждой session.

### Same-workspace collaboration

Отдельные тесты:
- два разных actor в одном workspace;
- общий project object с optimistic revision;
- private conversation/source не виден второму actor;
- shared result виден только после разрешённой materialization;
- конфликт edits не silently last-write-wins.

### Same-user multi-device

- второе устройство не смешивает microphone stream;
- takeover bump generation;
- старый connection лишается authority;
- accepted write receipt остаётся единственным.

### Public edge

Нужен настоящий `wss://` TLS Upgrade через production reverse proxy. `/healthz = 200` не является доказательством WSS.

### Physical Android

Нужны:
- реальный микрофон;
- несколько непрерывных turns;
- interruption;
- шум/короткие transients;
- poor network;
- Stop/restart;
- offline/reboot durable capture;
- calendar command confirmation/readback;
- playback drain;
- update после разговора без потери identity/pending source.

### Failure drills

- WSS disconnect mid-turn;
- provider GoAway;
- resource budget denial;
- backend restart;
- slow client;
- lost device receipt/unknown write outcome;
- DB contention;
- capability transition failure.

Критерий: источник не теряется там, где обещана durability; mutation не дублируется; ошибки атрибутируемы; следующий чистый turn способен восстановить работу.

## 12. Rollout без «большого взрыва»

### Phase 0 — защищённые product requirements
В новом рабочем окне создать `.devcoveer/requirements.json` через новый `requirements_update` и зафиксировать критические цели продукта. Изменение этих требований — только по явному owner intent.

### Phase 1 — shared framework pin
Выбрать актуальный accepted `live-interaction` WSS candidate/release, pin exact source/archive digest, добавить backend socket binding и conformance tests. Доменная логика не переписывается.

### Phase 2 — PWA WSS
Перевести realtime capture/events на WSS; оставить durable offline source отдельно; `captureDuringStart=false` до отдельной приёмки. HTTP Live path — только explicit compatibility/rollback, не silent fallback.

### Phase 3 — Android native voice edge
Подключить shared Java WSS transport, native capture/VAD/playback и typed bridge к floating-islands UI. Не трогать уже принятые Calendar/Keystore/update boundaries.

### Phase 4 — capability decomposition
Разбить flat tool catalog на core/router + bounded capability bundles с safe Gemini resumption.

### Phase 5 — multi-user gates
Прогнать concurrency, same-workspace ACL/revision, same-user takeover и failure drills. Исправлять contention/state leaks до public pilot.

### Phase 6 — public edge + physical pilot
Закрыть DNS/TLS, GitHub App, real browser/Android microphone и минимум несколько одновременных пользователей. До этого production acceptance остаётся false.

### Phase 7 — product growth
Theme/onboarding входят в базовый product UX. Shared resources и expert discourse/editorial развиваются отдельными capabilities после core reliability.

## 13. Неприкосновенные границы

Во время WSS-перехода запрещено без отдельного продуктового решения:

- вводить второй semantic agent/ASR-router перед центральной Live-моделью;
- переносить provider/GitHub credentials в PWA/Android;
- заменять durable offline source WebSocket resend-очередью;
- делать automatic retry unknown write;
- смешивать личные conversations пользователей;
- превращать project focus в ACL;
- расширять один flat tool list по мере появления каждой новой функции;
- обещать physical reliability по emulator/prepared PCM;
- выключать работающий Record Idea Hub до физической приёмки Projects Hub capture/recovery;
- считать expert editorial track обязательным MVP без отдельного исследования/согласования.

Главный критерий: транспорт, модель, инструменты и UI могут эволюционировать, но пользователь всегда получает один непрерывный понятный разговор, надёжную сохранность обещанных данных, строгую изоляцию и проверяемый результат действий.
