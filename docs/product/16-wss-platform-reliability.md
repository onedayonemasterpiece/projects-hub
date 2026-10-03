# WSS, multi-user reliability и межпроектная платформа

[Индекс](README.md) · [Live](06-live.md) · [Архитектура](04-architecture.md) · [Надёжность](08-reliability.md) · [Центральный Live-агент](12-central-live-agent.md) · [Экспертные review cases](15-expert-review-cases.md).

**Ревизия 2, 3 октября 2026. Статус: WSS implementation candidate реализован и проходит source/integration regression; public-edge, real-provider concurrency и physical Android acceptance ещё не объявлены пройденными.** Этот документ является каноническим дополнением для WSS, одновременной работы нескольких пользователей, Android self-update и межпроектных интеграций. Где старые документы описывают legacy HTTP Live transport или старый framework pin, применяется эта ревизия.

## 1. Архитектурный инвариант

Projects Hub остаётся одним продуктом и одним backend boundary:

```text
PWA / Android WebView
        │
        │ HTTPS bootstrap + authenticated same-origin WSS
        ▼
Projects Hub backend on DevCoveer
        │
        ├─ ProjectsHubAdmissionMixin
        │       ├─ global bounded admission
        │       ├─ per-actor fairness
        │       └─ duplicate buffered-source exclusion
        │
        ├─ LiveSocketSessionHost / live-interaction
        │       ├─ bounded binary PCM + ACK/backpressure
        │       ├─ pushed provider events/audio
        │       ├─ reconnect generation + fresh one-use ticket
        │       └─ damaged-turn guard after interrupted speech
        │
        ├─ ai-resource-control
        ├─ typed product/cross-project capabilities
        └─ Gemini Live provider session
```

Клиент не подключается к Gemini напрямую. Не добавляется Node gateway, sidecar, второй ASR, второй LLM-router или отдельный «умный» service. Python/FastAPI backend остаётся владельцем auth, conversation scope, provider session и tools.

Центральная Мира остаётся единственным semantic orchestrator. Transport, OAuth adapters, POI adapters, updater и persistence механически обеспечивают доставку/права/надёжность, но не подменяют смысловое решение модели.

## 2. Версия общего Live framework

Projects Hub фиксируется на **точном commit `b6a051a7cf53f84433ebf48a52b623d91fcc6478`**, package version **0.3.7-rc.1** — той линии WSS, на которой построена текущая Street Story integration.

Почему не «самый свежий HEAD»:
- текущий `live-interaction` может содержать последующие незавершённые изменения;
- Projects Hub должен принимать versioned transport как проверяемый dependency;
- переход на следующую версию делается отдельным consumer acceptance, а не live auto-update framework во время пользовательской сессии.

Browser и Python обязаны указывать на один exact commit. Это защищается regression-тестом.

## 3. WSS contract

### 3.1 Bootstrap

`POST /api/live/{conversation_id}/sessions`:
- максимум 4096 bytes bootstrap body;
- неизвестные поля fail-closed;
- transport только `wss`;
- клиент может передать bounded `attempt_id`;
- backend сам выбирает model;
- ответ содержит:
  - `session_id`;
  - `attempt_id`;
  - `transport_protocol = wl-live-v1`;
  - одноразовый short-lived `socket_ticket`;
  - same-origin relative `socket_url`.

Provider credential, resumption handle и resource-control secret в client response не попадают.

### 3.2 Socket authorization

Перед использованием ticket backend:
1. запрещает query string;
2. проверяет `Origin` против `Host`;
3. проверяет Projects Hub login cookie;
4. повторно получает conversation от store как текущий actor;
5. вычисляет resource binding `workspace + subject + conversation`;
6. только затем потребляет одноразовый ticket.

Ticket передаётся как WebSocket subprotocol `wl-ticket.<ticket>` вместе с `wl-live-v1`. URL/query credential не является fallback.

Ticket renewal — отдельный authenticated HTTPS endpoint. Новый ticket инвалидирует предыдущий неиспользованный ticket. Reconnect не переиспользует credential предыдущего socket generation.

### 3.3 PCM и события

Online speech:
- PCM16/16 kHz идёт бинарными WSS frames;
- relay выдаёт `audio_ack`;
- ACK означает только admission на server relay, не «Gemini понял» и не «tool выполнился»;
- unacknowledged queue и возраст PCM ограничены;
- provider events и output PCM push-ятся по тому же socket;
- для обычного realtime режима framework использует automatic speech boundaries; ручные `activity_start/activity_end` относятся к deliberate buffered mode.

Buffered offline replay:
- локальный source остаётся durable до подтверждённой server disposition;
- replay открывает тот же WSS transport;
- `activity_start → ordered binary PCM → activity_end`;
- исторический PCM рассматривается как deliberate buffered turn и не маскируется под stale realtime queue;
- одновременно replay-ить один `client_source_id` в двух сессиях одного actor запрещено.

### 3.4 Disconnect и reconnect

Нельзя «чинить» WSS failure скрытым возвратом к HTTP audio:
- если session уже использовала WSS, HTTP input получает transport mismatch;
- reconnect получает fresh ticket и увеличивает connection generation;
- старые pending/unacknowledged PCM fragments не replay-ятся;
- если transport оборвался внутри открытой activity boundary, turn помечается damaged;
- mutating tool admission блокируется до новой чистой speech boundary;
- Stop остаётся idempotent.

## 4. Одновременная работа нескольких людей

### 4.1 Изоляция

Live resource identity:

```text
issuer subject
+ workspace_id
+ conversation_id
→ ConversationScope.resource_binding()
```

Project focus не является security boundary. Каждый tool независимо работает только через actor/workspace-authorized stores/adapters.

Нельзя по одному session id:
- attach к чужому socket;
- renew чужой ticket;
- читать чужой event stream;
- stop чужую session;
- выполнить tool в чужом workspace.

Regression-тест подтверждает, что второй actor получает 404 на попытку renewal чужой Live-session: сам факт существования session не раскрывается.

Cross-project tokens также не расширяют Projects Hub identity автоматически: target service повторно авторизует user-specific delegated token.

### 4.2 Capacity и fairness

Shared `LiveSocketSessionHost` сохраняет bounded global admission. Projects Hub добавляет поверх него только product-level fairness — transport не форкается.

Параметры:
- global default: **16 активных Live sessions**;
- env: `PROJECTS_HUB_LIVE_MAX_SESSIONS`;
- global допустимый range: 1…64;
- default на одного actor: **2 активные Live sessions**;
- env: `PROJECTS_HUB_LIVE_MAX_SESSIONS_PER_ACTOR`;
- per-actor limit: от 1 до global limit;
- один `client_source_id` не может одновременно replay-иться в двух Live sessions одного actor;
- превышение возвращает visible `LIVE_BUSY` / HTTP 429, а не создаёт бесконечную очередь.

Per-actor admission не реализует свой WebSocket protocol. Ticket, socket lifecycle, reconnect, ACK/backpressure и damaged-turn guard принадлежат shared `live-interaction`.

Это local safety ceiling, а не обещание, что provider quota выдержит 16 полноценных разговоров. Provider/resource capacity дополнительно решает `ai-resource-control`.

Перед production acceptance нужны нагрузочные сценарии минимум 1 / 4 / 8 / configured-max одновременных sessions с:
- параллельным bootstrap;
- PCM ingress;
- tool calls;
- reconnect;
- Stop;
- resource refusal;
- проверкой отсутствия cross-session событий и head-of-line contamination;
- минимум двумя одновременно активными actor;
- проверкой, что один actor не может вытеснить остальных.

## 5. Что переносим из Street Story, а что нет

Переносим общие transport invariants:
- same-origin;
- one-use ticket;
- WSS-only после attach;
- binary PCM;
- ACK pacing;
- bounded ingress/egress;
- ticket renewal;
- reconnect generation;
- event cursor;
- damaged-turn guard;
- no stale replay;
- transport/provider/tool/resource diagnostics.

Не копируем предметную логику Street Story:
- photo/story state;
- publication flow;
- Street Story-specific VAD tuning как безусловный web default;
- Telegram publication contract.

Свежий физический review Street Story, где системный shutter transient открыл ложный speech turn, становится **shared regression requirement**: короткий несмысловой системный transient не должен автоматически считаться пользовательской репликой. Конкретная VAD реализация может различаться между native Android capture и browser AudioWorklet; критерий один — false turn не открывается.

## 6. Regional Knowledge Base как capability Миры

Projects Hub — естественная пользовательская поверхность для чтения региональной базы знаний, но не владелец этой базы.

Целевой путь:

```text
user ↔ Mira / Projects Hub conversation
              │
              └─ progressive capability: regional_knowledge
                       │
                       └─ knowledge_search(query, max_evidence<=3)
                                │ delegated OAuth token for Knowledge audience
                                ▼
                         Regional Knowledge MCP/API
```

Правила:
- active Live tool bundle остаётся маленьким; Knowledge capability не загружается в каждую сессию без необходимости;
- нормальный Live profile базы знаний — один evidence-returning search tool, а не десятки низкоуровневых DB tools;
- результат содержит exact provenance/evidence refs;
- source ACL наследуется;
- Projects Hub token никогда не пересылается как Knowledge token;
- при недоступности Knowledge обычный Projects Hub conversation продолжает работать;
- Mira не выдаёт discovery snippets за verified regional facts.

На 3 октября 2026 эта интеграция **спроектирована, но не объявлена live E2E**: Regional Knowledge delegated OAuth resource/API должен быть фактически развёрнут и принят отдельно.

## 7. Общий OAuth, но не общий authorization

Для продуктовой семьи используется общий Supabase Auth OAuth/OIDC issuer. Stable identity — `issuer + sub`.

Но:
- Projects Hub, Regional Knowledge, Street Story и другие MCP/API остаются отдельными protected resources;
- каждый resource имеет exact audience/client binding;
- каждый сервис сам проверяет ACL/workspace/roles;
- пользователь один раз выдаёт нужный delegated grant;
- refresh grant хранится server-side encrypted;
- Projects Hub получает target-audience access token только для вызова target resource;
- обычный пользовательский flow не использует global service-role impersonation.

Единый OAuth уменьшает количество логинов, но **не** превращает систему в одну общую security boundary.

## 8. POI: одна каноническая модель владения

Чтобы не получить три несовместимые базы фактов:

- **Regional Knowledge Base** владеет книгами, журналами, provenance/page-region graph, author/source verification score и evidence extraction;
- **Street Story** владеет canonical regional POI identity, aliases/external ids, atomic POI claims, evidence sets, contradiction ledger и arbitration state;
- **Projects Hub** владеет пользовательским/экспертным work surface и conversation context, но не канонической POI truth.

Поток:

```text
book/image/source in Regional Knowledge
  → evidence / poi.fact_evidence.v1
  → Street Story resolves canonical poi_id
      → canonical claim / contradiction / unresolved link
          → optional expert review case in Projects Hub
              → expert decision
                  → owning-service receipt + readback
```

Если POI identity неоднозначна, нельзя автоматически создавать дубль или молча merge-ить. Создаётся unresolved review state.

Иллюстрации книг, привязанные к POI, сохраняют source/provenance link и могут позднее использоваться Street Story как historical visual evidence, но Projects Hub не копирует image bytes в отдельный «свой POI store».

## 9. Expert review и будущий экспертный дискурс

Текущие `expert review cases` являются правильным строительным блоком для идеи об асинхронном экспертном дискурсе:
- эксперт видит evidence и контекст;
- принимает typed position/resolution;
- система сохраняет receipt и provenance;
- противоречия могут группироваться;
- позже редакционная Мира может синтезировать материал, честно показывающий позиции экспертов.

Не надо создавать вторую «социальную сеть противоречий» рядом с review cases. Сначала накапливаем реальные review events и telemetry; публичный/editorial слой проектируется по фактическим паттернам.

## 10. Theme и адаптивный onboarding

Свежий owner review добавляет:
- voice commands «переключи на тёмную/светлую тему»;
- короткий адаптивный onboarding;
- не перечислять все возможности при каждом запуске;
- предлагать 1–3 релевантные функции, особенно давно не использовавшиеся;
- пользователь может отказаться от дальнейших подсказок.

Это не причина раздувать default function declarations. Theme — маленькая deterministic UI capability. Onboarding state — metadata/usage signal. Мира формулирует подсказку, но tool surface остаётся progressive.

## 11. Видимый статус Live

В интерфейсе должны различаться:
- подключение/настройка;
- слушаю;
- реальная speech activity;
- думаю/жду model;
- выполняю tool;
- resource/provider/transport error.

Нельзя показывать «слушаю», если socket hello ещё не подтверждён. Нельзя показывать speech pulse только потому, что session существует.

Технические коды — diagnostics; пользователь получает короткую нормальную формулировку. При проблеме нужен incident bundle с `attempt_id/session_id/generation/timings/counters/error class`, но без tickets, provider keys, raw PCM или чужих данных.

## 12. Android self-update — уже продуктовый путь

Self-update **не проектируется заново**.

Текущий Android client:
1. проверяет GitHub Releases при launch/resume;
2. читает `update.json`;
3. сравнивает `versionCode`;
4. показывает «Доступно обновление · …» и notification;
5. по действию пользователя скачивает APK;
6. проверяет SHA-256;
7. при необходимости открывает системное разрешение «install unknown apps»;
8. передаёт APK Android Package Installer.

Нормальный будущий UX:

```text
Новая версия доступна
→ пользователь нажимает «Обновить»
→ verified download
→ Android installer
→ новая версия
```

Пользователю не нужно снова искать APK в GitHub/source. Полностью бесшумную установку без системного installer не обещаем: стандартный Android security boundary сохраняется.

Update failure не ломает установленную версию. Release signing identity должна оставаться стабильной.

Текущее подтверждённое evidence до этой WSS-миграции: hosted Android 14 handoff/in-place update v4→v5 был принят. Новый physical update gate после WSS-кандидата пока не выполнялся. Локальный Android unit job на DevCoveer 3 октября не стартовал из-за отсутствующего `gradlew` в checkout; это environment limitation, а не assertion failure updater.

## 13. Golden corpus и review intake

- Голосовые owner reviews по Projects Hub читаются из `idea-hub/inbox/voice`.
- Telegram thread `https://t.me/c/4488229487/29` используется для screenshots, визуальных замечаний и golden examples/corpus.
- Screenshot/golden artifact не становится продуктовым требованием автоматически: замечание сначала связывается с конкретным acceptance criterion или issue.
- На момент ревизии в thread не было нового screenshot, требующего изменения WSS architecture.

## 14. Фактическое локальное evidence WSS candidate

На 3 октября 2026:
- `live-interaction 0.3.7-rc.1@b6a051a7...` установлен в project `.venv`;
- WSS integration suite: **6 PASS**;
- full backend regression suite: **94 PASS**;
- PWA production build: **PASS**;
- ранее также выполнен clean `npm ci` + production build: **PASS**;
- actor isolation, global capacity, per-actor fairness и duplicate buffered-source admission имеют regression tests;
- requirements guard: `.devcoveer/requirements.json` активен;
- production/public-edge/physical-device acceptance не подменяются локальными тестами.

## 15. Production acceptance gates для этой миграции

Нельзя объявлять «готово как швейцарские часы» только по unit tests.

Минимум:
1. backend suite PASS;
2. clean `npm ci` + production PWA build PASS;
3. WSS integration: bootstrap/ticket/origin/query/hello/binary PCM/ACK/pushed audio/Stop PASS;
4. HTTP fallback после WSS attach отвергается;
5. reconnect получает fresh ticket/generation и не replay-ит stale PCM;
6. bounded offline replay проходит одним deliberate turn;
7. 4+ параллельных actor sessions без cross-talk;
8. configured-capacity admission test;
9. public TLS reverse proxy реально делает WebSocket upgrade;
10. real provider prepared-PCM canary;
11. physical microphone test в Android WebView;
12. transient/noise false-turn regression;
13. Regional Knowledge delegated OAuth + `knowledge_search` E2E;
14. cross-resource token misuse negative test;
15. expert-review write receipt/readback E2E;
16. Android update from previous signed version through in-app button on physical device.

До выполнения конкретного gate его статус — pending, а не inferred from code.

## 16. Rollout

Порядок:
1. source + unit/integration tests на pinned WSS framework — **выполнено локально**;
2. зафиксировать candidate commit/PR — текущий шаг;
3. deploy candidate на существующий single backend;
4. public-edge WebSocket upgrade check;
5. real provider prepared-PCM + owner physical voice acceptance;
6. 4-user concurrency rehearsal;
7. Regional Knowledge capability отдельно включается после OAuth/API readiness;
8. только затем WSS считается production default.

Legacy HTTP endpoints могут временно оставаться compatibility surface, но клиент не использует их как fallback.

Rollback меняет release/framework pin и backend build. Он не должен требовать миграции conversation store, повторного логина всех пользователей или ручного поиска APK пользователем.

## 17. Нежелательные усложнения

Не добавлять без отдельного доказанного требования:
- Redis/broker только «ради WebSocket»;
- отдельный gateway service;
- второй backend language/runtime ради унификации;
- общий database между продуктами;
- service-role cross-project impersonation;
- background semantic classifier;
- HTTP audio fallback;
- unbounded session admission;
- автоматическое расширение Live tool surface всеми межпроектными tools одновременно.

Надёжность здесь достигается малым числом хорошо определённых границ, bounded state, explicit receipts/readback и повторно используемым versioned transport.
