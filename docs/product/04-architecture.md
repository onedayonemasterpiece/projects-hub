# Платформы, архитектура и связность проектов

> **Дополнение 6 октября 2026:** текущая спецификация коллаборации — [20](20-basic-collaboration-and-personal-timeline.md). Она задаёт одну личную ленту, один mounted BoardViewport, общие Note/Discussion/Question/Task с независимой от private transcript областью доступа и минимальное durable server continuation. Старые схемы source capture ниже сохраняются; новая проектная заметка использует отдельно разрешённое текстовое структурирование.

> **U04 / 2 октября 2026:** network transport, multi-user identity, shared OAuth/resource isolation, Regional Knowledge capability и POI ownership уточнены в [16-wss-multi-user-reliability.md](16-wss-multi-user-reliability.md). Центральный one-agent backend boundary не меняется.

[Индекс](README.md) · [Центральный Live-агент](12-central-live-agent.md) · [UX](03-product-and-ux.md) · [Память](10-conversation-memory.md) · [Маршрутизация и словари](11-routing-and-vocabulary.md).

**Ревизия 4, 28 сентября 2026.** Центральный интеллект продукта — Gemini Live в пользовательском разговоре. Backend не содержит параллельного смыслового конвейера. **Backend Projects Hub один: provider connection к Gemini Live, GitHub App и все server-owned integrations живут на DevCoveer; PWA/Android не подключаются к Gemini/GitHub в обход backend.**

## ADR-01: PWA и Android — два полноценных приложения

Оба клиента реализуют практически единый основной продукт:
- большая голосовая кнопка;
- один живой диалог;
- проекты и история;
- задачи, решения, встречи;
- contextual button choices;
- возвращение к прошлым разговорам.

Android имеет дополнительные platform capabilities: надёжная длительная запись при корректно работающем foreground service, более сильная локальная очередь, device-bound actions и системные интеграции. Это не «тонкий клиент».

PWA имеет тот же core UX и API. Ограничения браузера честно отображаются; нельзя обещать фоновые гарантии Android только потому, что экран похож.

## ADR-02: одно когнитивное звено

```text
PWA / Android microphone
        │
        ├─ durable local capture + shared capture/VAD
        │
        ▼
Projects Hub backend on DevCoveer
        │
        ├─ shared live-interaction session host
        │       │ raw PCM / activity boundaries
        │       ▼
        │   Gemini Live agent
        │       ├─ input transcription ──> backend source journal
        │       ├─ audio response ───────> backend ──> client
        │       └─ typed function calls ─> backend tools
        │
        ├─ GitHub App / project stores / owning service APIs
        │       └─ server-side only; credentials never go to PWA/Android/Live
        │
        └─ device command outbox
                └─ addressed command ──> Android capability
                                      └─ receipt/result ──> backend ──> same Live session
```

Live-agent отвечает за semantic understanding:
- что пользователь сказал;
- к какому проекту это относится;
- нужен ли дополнительный контекст;
- какой вопрос задать;
- что сохранить;
- какое предметное действие предложить/выполнить.

Backend отвечает за механически проверяемое:
- auth/ACL;
- durable source;
- schemas;
- revision checks;
- idempotency;
- retries/reconciliation;
- resource limits;
- transaction/outbox;
- device binding;
- readback.

**Сетевая граница фиксирована:** клиентское приложение не держит provider credential и не создаёт самостоятельную Gemini Live session. Клиент передаёт audio/input в один Projects Hub backend; именно backend поднимает/возобновляет Live provider session и возвращает provider events/audio клиенту.

GitHub работает по той же границе: install/callback metadata приходит в backend, App JWT и installation token создаются только на backend и только на конкретную server operation. Android/PWA видят лишь connection metadata и contextual UI.

Для device-local возможностей направление обратное. Например календарь телефона:
1. Gemini Live вызывает typed `calendar.*` function в своей backend session;
2. backend авторизует actor/workspace/device и создаёт durable device command;
3. конкретное Android-устройство получает только адресованную команду;
4. Android выполняет действие через platform API и возвращает typed receipt/result;
5. backend reconciles outcome и возвращает function result **той же** Live-модели.

Телефон не становится вторым agent/backend и не получает GitHub/provider credentials. Backend не «симулирует» локальный Android Calendar API.

Нельзя добавлять скрытые LLM calls внутри tools для предварительной классификации, суммаризации, маршрутизации или переписывания ответа Live. Разрешённая владельцем операция V32 «структурировать проектную заметку» получает выбранный Мирой готовый текст и возвращает предметный результат; она не входит в путь ASR/Live-routing.

## ADR-03: offline — сохранённый аудио-turn того же агента

Offline capture — не отдельный AI-режим.

Android/PWA сохраняют речь локально. Когда central Live становится доступна, source подаётся **аудио** в Live-сессию. Для длинного buffered source нужен manual activity mode общего framework:
- provider automatic activity detection disabled на этот deliberate replay;
- activityStart;
- последовательный PCM;
- activityEnd.

Так agent не начинает отвечать на естественной паузе внутри длинной записи.

Текущий framework при reconnect намеренно не replay-ит старую речь — это сохраняется для обычного transport recovery. Projects Hub делает deliberate buffered replay из своей durable source queue только после создания готового Live-turn.

Отдельный ASR перед Live не является базовой архитектурой.

## ADR-04: разговор личный для actor, проекты — targets function calls

Пользователь может за один разговор обсуждать несколько разрешённых проектов. Поэтому semantic project focus не должен быть hard-binding всей provider session.

Целевая модель:
- provider/Live session привязана к actor + tenant/workspace + conversation resource;
- доступный project catalogue передаётся server-owned кратким контекстом и доступен через tools;
- current focus хранится как conversation state;
- каждый read/write tool повторно проверяет actor и target project;
- полный личный source не публикуется автоматически ни в одну группу.

Существующий `ProjectScope(tenant_id, subject_id, project_id)` — ранний resource bootstrap, не финальная multi-project модель. Его необходимо эволюционировать к conversation/workspace binding либо эквиваленту, не смешивая resource accounting и object authorization.

Переключение фокуса «теперь про фестиваль» не должно перезапускать микрофон или модель. Если project недоступен — tool возвращает отказ, agent объясняет/уточняет.

## ADR-05: инструменты агента

Минимальный каталог, конкретные имена уточняются реализацией:

### Контекст
- `projects.list_accessible`
- `projects.get`
- `project_docs.search`
- `project_docs.read`
- `conversation.search_sources`
- `conversation.get_source`
- `conversation.set_focus`

### Память
- `memory.commit_voice_source`
- `memory.finish_ephemeral`
- `memory.link_source_to_project`

### Коллаборация
- task/proposal/decision/vote/meeting tools;
- notification tools;
- calendar tools.

Server-owned и device-owned tools различаются исполнителем, но **не когнитивным звеном**. GitHub/project/docs выполняются backend-side. Личный Android calendar, notification permission и другие OS-local actions выполняются device capability после server-issued command; результат возвращается через backend.

### Словарь
- `vocabulary.read`
- `vocabulary.upsert_grounded`
- `vocabulary.list_candidates`

### Предметные owning products
- Wonderful Lections, KenigEvents, Street Story и другие через свои существующие service contracts/adapters.
- External expert review cases остаются объектами owning product. Projects Hub
  хранит assignment/UX/command receipts и вызывает typed review adapter, но не
  копирует канонические claim/evidence данные. Первый такой consumer — Street
  Story POI contradiction journal; подробный договор — [15](15-expert-review-cases.md).

Tool schemas короткие и typed. Agent не получает shell/SQL/credential access. Tool может выполнять несколько детерминированных шагов одной бизнес-операции, но не решает semantic intent отдельной моделью.

## ADR-06: источники и состояния

| Данные | Канонический владелец |
| --- | --- |
| Локально ещё не доставленное аудио | клиентская durable source queue |
| Серверный source/transcript journal и disposition | прикладная PostgreSQL / durable source store |
| Постоянный Markdown разговора/идеи/требования | разрешённый GitHub repo |
| Оперативные задачи/голоса/meeting proposals | прикладная PostgreSQL |
| Project docs | owning GitHub repositories |
| Live quota/lease/grant | общий ai-resource-control |
| Tool command/receipt/outbox | прикладная PostgreSQL |
| Vocabulary snapshots/provenance | project-owned storage + versioned references |

GitHub не используется как транспортный буфер для каждого PCM chunk. Прикладная БД не становится вторым владельцем документации.

## ADR-07: provider transcription — источник той же Live-сессии

Текущий `live-interaction` уже настраивает `inputAudioTranscription`. Product adapter сохраняет provider transcript events для source provenance.

Важные ограничения текущего framework:
- provider projection режет текст события до 2000 символов;
- UI host хранит ограниченный event ring;
- browser transcript preview держит только хвост;
- bootstrap history короткая.

Ни одна из этих UI/transport проекций не должна быть archive source. Нужен lossless product observer/sink до truncation/ring eviction либо соответствующее изменение framework.

Если transcription incomplete, source audio остаётся и заново подаётся тому же central Live agent в recovery turn. Не запускается скрытая другая модель.

## ADR-08: словарь — контекст и tool memory агента

Agent сам читает разрешённые проектные документы и вызывает vocabulary tools. Backend хранит точные source refs/revisions и schema.

В session bootstrap помещается небольшой актуальный terminology snapshot. Если agent обновил словарь в середине разговора:
- tool result делает новые термины доступными самому agent;
- для следующих turn может быть обновлён bounded conversation context;
- old source сохраняет старый snapshot;
- не заявляется, что произвольный custom vocabulary напрямую перенастроил provider speech recognizer, пока это не проверено API/моделью.

## ADR-09: retrieval не является вторым интеллектом

Полнотекстовый или vector search допустим как механический retrieval:
- ACL применяется до выдачи;
- результат — source refs/snippets/metadata;
- agent сам решает, что они означают и что делать.

Нельзя строить отдельный LLM retrieval-router, который переписывает запрос, принимает project decision и передаёт Live только свой summary, если это не отдельное утверждённое продуктовое решение.
## ADR-10: базовая коллаборация без отдельной workflow-платформы

Одна durable личная лента actor+workspace содержит сообщения с устойчивыми message/turn IDs и типизированными блоками. Project focus, provider session и выбранная доска не создают новые ленты. В клиенте один BoardViewport; общая очередь входящих вопросов загружается независимо от открытой доски.

Проектные Note/Discussion/Question/Answer/Task — явно разделяемые ресурсы с author, recipient, revision и ACL. Note публикуется в подключённый GitHub repo как Markdown, store хранит индекс/состояние/receipt; карточка и reader читают тот же объект. Memory/source остаётся личным, пока выбранный фрагмент явно не опубликован. Список ролей автора не заменяет текущие grants.

Принятый ответ и намерение продолжения фиксируются транзакционно. Использовать существующий durable store и один bounded backend pump с lease/retry/readback; очередь переживает рестарт, status read только читает. Существующая MCP transport queue не является durable job queue продукта. Новые Redis/BPM/broker и универсальный event-sourcing слой для этого среза не требуются. Полный контракт и границы реализации — [20](20-basic-collaboration-and-personal-timeline.md).
