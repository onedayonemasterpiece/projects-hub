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

Projects Hub consumer candidate от 3 октября 2026 использует stable release `live-interaction v0.3.8`. Dependency interface — версия/tag; release tag разрешается в commit `f756a90f864bee16a671b54da7a53189e2e4a94e`, а опубликованный asset имеет SHA-256 `99b8a4ea7c04a81062547a6a63b8e161220fb62a3b6d947ddda1954c2a90ab0`. Commit/digest сохраняются как verification evidence, но межпроектный контракт не привязывается к raw SHA. Следующая смена framework version выполняется только через новый consumer-specific acceptance; moving HEAD не используется.

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

## 4. Android: один foreground WSS, native только для Android-specific durability

Фактический Android Projects Hub — WebView-контейнер общего PWA плюс native возможности: Keystore/device binding, Calendar Provider, notifications и signed self-update. Внутри WebView запускается **тот же PWA `live-interaction v0.3.8` WSS client**, поэтому foreground Android уже не нуждается во втором native WebSocket transport.

Целевая граница:

- foreground UI, microphone capture, WSS framing, ACK/backpressure и playback остаются в общем PWA/shared-browser Live path;
- WebView выдаёт `RESOURCE_AUDIO_CAPTURE` только exact Projects Hub origin; Supabase/Yandex OAuth страницы могут проходить PKCE navigation, но не получают microphone permission;
- Android native слой не открывает параллельную Live-сессию и не создаёт второй WSS transport;
- Gemini/API key/GitHub credentials остаются только на backend;
- Android-specific native развитие относится к **durable/background/offline capture** и системным capabilities. Такой capture должен передавать source в существующий Projects Hub Live/replay contract, а не обходить его альтернативным provider/socket path;
- Stop и UI state остаются согласованы с одной central Live session;
- PWA IndexedDB foreground queue не выдаётся за reboot/background durability Android.

Из Street Story переносим проверенные принципы bounded audio, явного setup/listening state, physical-microphone diagnostics и запрета silent HTTP fallback, но не копируем domain controller и не создаём native transport только ради технологического совпадения. В shared `live-interaction v0.3.8` native Java source сам всё ещё маркирует transport как `0.3.7-rc.1`, поэтому он не является основанием объявлять native Android edge стабильным. Prepared PCM/emulator также не заменяют физическую проверку микрофона.

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
- WSS integration suite: **9 PASS**;
- full backend regression suite: **107 PASS**;
- PWA production build: **PASS**;
- clean `npm ci` + production build: **PASS**;
- four-actor deterministic WSS acceptance PASS: четыре actor одновременно держат WSS handshake, чужой socket-ticket не раскрывается, пятая сессия упирается в global `LIVE_BUSY`;
- global capacity, per-actor fairness и duplicate buffered-source admission защищены regression tests;
- protected product requirements включены через `.devcoveer/requirements.json`;
- exact Projects Hub SHA `720f44d8771043804a5e6e4fe6cb56e38790d79b` с `live-interaction v0.3.8` развёрнут на DevCoveer; service health PASS и restart count 0;
- deployed WSS roundtrip через реальный Gemini Live: binary PCM ACK, no-HTTP-fallback 409, pushed transcript + binary audio + turn_complete + graceful server close — **PASS**;
- deployed concurrency: **2 real-provider sessions одновременно PASS**, третья того же actor получает **429 LIVE_BUSY**;
- постоянный runtime check: `scripts/devcoveer_wss_canary.py --mode roundtrip`, затем отдельно `--mode concurrency`;
- public edge probes: **BLOCKED — DNS name does not resolve**, поэтому TLS/Upgrade ещё не проверены.

Не считать выполненным по этому checkpoint:
- 20-socket/30-minute deterministic soak;
- 3+ independent-user real-provider sessions / live multi-actor soak; four-actor deterministic acceptance это не заменяет;
- public production `wss://` Upgrade; current blocker is unresolved DNS;
- same-conversation multi-device physical/takeover UX acceptance; deterministic single-owner lease is implemented/tested but not yet deployed;
- physical Android WebView microphone over the shared PWA WSS path; public edge must resolve first;
- physical microphone/noise/poor-network acceptance;
- post-candidate physical Android in-app update;
- Android reboot/background durable capture; native implementation must hand off to the existing Live/replay contract rather than open a parallel WSS;
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
- adapter contract покрыт regression tests; после его добавления полный backend suite — **104 PASS**;
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


## 17. Versioned reuse / U05 · 3 октября 2026

Свежий owner review `voice-20261003-083638-766e61b7` добавляет общий инженерный инвариант: перед созданием нового transport/helper/limiter/capability агент разработки обязан выполнить semantic discovery существующих решений, посмотреть их фактический runtime, ретроспективу и связанные инциденты. Цель — не процесс ради процесса, а снижение фрагментарности и повторное использование проверенного кода.

Для shared components:
- consumer зависит от стабильной версии/release/capability availability, а не от raw commit SHA;
- SHA/digest остаются evidence того, что именно было принято и собрано;
- consumer-specific acceptance обязателен после смены версии;
- инцидент общего Live-компонента должен быть доступен всем consumers, которых он касается;
- это не оправдание для нового централизованного gateway, второго semantic agent или тяжёлой regex-бюрократии.

Projects Hub применяет это правило первым к `live-interaction v0.3.8`: Python и browser manifests используют `v0.3.8`, lock разрешает tag в immutable release commit, full backend + clean PWA acceptance выполняются на этой версии, а deployed `720f44d8…` повторно прошёл real-provider WSS roundtrip/concurrency без новых `socket_failed`.
