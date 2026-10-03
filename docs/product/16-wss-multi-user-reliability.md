# Projects Hub — WSS, многопользовательская надёжность и следующий продуктовый этап

**Проектное решение от 2 октября 2026; runtime update от 3 октября 2026. Статус: target architecture + deployed backend WSS candidate; public edge / multi-actor / physical acceptance ещё не завершены, поэтому production acceptance не объявлен.**

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

Projects Hub deployed candidate от 3 октября 2026 фиксирует shared `live-interaction 0.3.7-rc.1` на exact commit `b6a051a7cf53f84433ebf48a52b623d91fcc6478` и использует тот же WSS contract, который был отработан в Street Story. Следующая смена framework version выполняется только через новый consumer-specific acceptance; moving HEAD не используется как runtime dependency.

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


## 14. Implementation checkpoint · 3 октября 2026

Целевая архитектура выше материализована в deployed backend candidate Projects Hub на SHA `59b522278a1694dfb930e070149fa0de9224c6b6`, но этот checkpoint **не заменяет** public-edge, multi-actor и physical acceptance.

Реализовано:
- backend использует shared `LiveSocketSessionHost`, а не собственный несовместимый WebSocket engine;
- HTTP session bootstrap ограничен 4096 bytes, transport — WSS-only для нового клиента;
- bootstrap возвращает same-origin `socket_url`, one-use `socket_ticket`, `attempt_id` и `wl-live-v1`;
- socket проверяет query/origin/login/conversation-resource до consumption ticket;
- PCM идёт бинарно, relay ACK и output events/audio обслуживаются shared framework;
- после WSS attach старый HTTP audio input получает `LIVE_TRANSPORT_MISMATCH`;
- PWA normal Live client использует WSS;
- deliberate buffered/offline replay передаёт сохранённый PCM бинарно в WSS и сохраняет manual activity semantics;
- local admission: global default 16 (`PROJECTS_HUB_LIVE_MAX_SESSIONS`, hard range 1…64);
- actor fairness: default 2 active/starting sessions на actor (`PROJECTS_HUB_LIVE_MAX_SESSIONS_PER_ACTOR`, не больше global cap);
- один `client_source_id` нельзя одновременно replay-ить в двух Live sessions одного actor;
- существующий Android signed GitHub Releases updater сохранён без отдельной параллельной update-системы.

Фактическое evidence candidate:
- WSS integration suite: **6 PASS**;
- full backend regression suite: **94 PASS**;
- PWA production build: **PASS**;
- clean `npm ci` + production build: **PASS**;
- cross-actor ticket renewal negative test: чужая session не раскрывается;
- global capacity, per-actor fairness и duplicate buffered-source admission защищены regression tests;
- protected product requirements включены через `.devcoveer/requirements.json`;
- exact SHA `59b522278a1694dfb930e070149fa0de9224c6b6` развёрнут на DevCoveer; service health PASS и restart count 0;
- deployed WSS roundtrip через реальный Gemini Live: binary PCM ACK, no-HTTP-fallback 409, pushed transcript + binary audio + turn_complete + graceful server close — **PASS**;
- deployed concurrency: **2 real-provider sessions одновременно PASS**, третья того же actor получает **429 LIVE_BUSY**;
- постоянный runtime check: `scripts/devcoveer_wss_canary.py --mode roundtrip`, затем отдельно `--mode concurrency`;
- public edge probes: **BLOCKED — DNS name does not resolve**, поэтому TLS/Upgrade ещё не проверены.

Не считать выполненным по этому checkpoint:
- 20-socket/30-minute deterministic soak;
- 3+ independent-user real-provider sessions / live multi-actor soak;
- public production `wss://` Upgrade; current blocker is unresolved DNS;
- same-conversation multi-device takeover lease;
- native Android WSS/capture edge;
- physical microphone/noise/poor-network acceptance;
- post-candidate physical Android in-app update;
- Regional Knowledge delegated OAuth E2E.

## 15. Shared OAuth, Regional Knowledge и canonical POI ownership

Межпроектная синергия строится через **общую identity plane, но отдельные resource authorization boundaries**.

### Shared identity

Продуктовая семья использует общий Supabase Auth OAuth/OIDC issuer. Каноническая identity пользователя — `issuer + sub`.

Это **не** означает общий bearer token:
- Projects Hub, Regional Knowledge, Street Story и другие protected resources имеют собственные exact audiences/client bindings;
- Projects Hub token не пересылается в Regional Knowledge;
- private cross-service access выполняется через user-approved delegated grant к target resource;
- target service заново применяет свои ACL/RLS/roles;
- global service-role credential не используется для impersonation обычного пользователя.

### Regional Knowledge как capability Миры

Projects Hub является пользовательской conversational surface, но не владельцем корпуса Regional Knowledge.

Целевой Live path:

```text
user ↔ Mira / Projects Hub
          │
          └─ progressive capability: regional_knowledge
                  │
                  └─ knowledge_search(query, default=3, hard max_evidence=5)
                          │ user-approved Knowledge-audience token
                          ▼
                    Regional Knowledge
```

Правила:
- capability подключается прогрессивно и не раздувает core tool bundle;
- нормальный Live profile возвращает небольшой evidence pack с точными provenance refs;
- отсутствие Regional Knowledge деградирует только эту capability, а не весь разговор;
- Mira не повышает discovery snippet до verified fact без evidence;
- Projects Hub уже имеет transport-agnostic `RegionalKnowledgeAdapter`: он жёстко связывает provider с текущими `actor_sub + workspace_id`, ограничивает размер evidence/provenance и добавляет единственный `knowledge_search` в Live tool list только когда backend реально вернул user-delegated provider;
- без delegation/provider `knowledge_search` вообще не попадает в конфигурацию Live; неправильная actor/workspace binding, не-HTTPS evidence или сломанный provider fail-closed и не ломают остальные capabilities;
- Projects Hub не реализует service-role или bearer-token fallback для этого пути;
- adapter contract покрыт regression tests; после его добавления полный backend suite — **102 PASS**;
- полная интеграция всё ещё **не реализована E2E**: сам Regional Knowledge проект фиксирует, что отдельный Supabase/S3/OAuth resource ещё не развёрнут, поэтому реальный delegated OAuth grant и network call остаются acceptance gate.

### POI ownership

Чтобы не создать три конкурирующие «истины»:
- **Regional Knowledge** владеет книгами/журналами, page-region provenance, author/source verification и evidence extraction;
- **Street Story** владеет canonical regional `poi_id`, aliases/external identities, atomic POI claims, evidence sets, contradiction ledger и arbitration state;
- **Projects Hub** владеет conversation/expert-review work surface, но не canonical POI/fact store.

Поток evidence:

```text
Regional Knowledge source/evidence
→ typed POI evidence event
→ Street Story canonical poi_id / claim / contradiction
→ optional expert review case in Projects Hub
→ expert typed decision
→ owning-service receipt + readback
```

Ambiguous POI identity создаёт unresolved review state; нельзя молча merge-ить или дублировать POI. Иллюстрация книги, относящаяся к POI, сохраняет source/provenance reference и может позднее использоваться Street Story как historical visual evidence, но Projects Hub не создаёт отдельный media/POI source of truth.

## 16. Android self-update boundary

WSS rollout не заменяет существующий updater.

После первой установки Android:
1. на launch/resume проверяет GitHub Releases;
2. читает `update.json` и сравнивает `versionCode`;
3. показывает пользователю доступность новой версии/notification;
4. по действию пользователя скачивает APK;
5. проверяет SHA-256;
6. сохраняет системный Android package-installer/signature boundary.

Нормальный UX: **«Новая версия доступна → Обновить → verified download → Android installer»**. Пользователю не нужно снова искать APK в GitHub/source. Бесшумную privileged install без Android confirmation продукт не обещает.

Предыдущее hosted Android 14 evidence v4→v5 остаётся валидным для updater-механики; новый physical post-WSS update gate остаётся отдельным acceptance.
