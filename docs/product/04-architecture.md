# Платформы, архитектура и связность проектов

[Индекс](README.md) · [Центральный Live-агент](12-central-live-agent.md) · [UX](03-product-and-ux.md) · [Память](10-conversation-memory.md) · [Маршрутизация и словари](11-routing-and-vocabulary.md).

**Ревизия 3, 27 сентября 2026.** Центральный интеллект продукта — Gemini Live в пользовательском разговоре. Backend не содержит параллельного смыслового конвейера.

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
        ├─ durable local capture + VAD
        │
        ▼
shared live-interaction
        │ PCM / activity boundaries
        ▼
Gemini Live agent
        │
        ├─ input transcription events ──> source journal
        ├─ audio response ──────────────> client
        └─ function calls
                │
                ▼
        authorized thin tools
                │
     docs / memory / tasks / calendar / notifications / vocabulary
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

Нельзя добавлять скрытые LLM calls внутри tools для предварительной классификации, суммаризации, маршрутизации или переписывания ответа Live.

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

### Словарь
- `vocabulary.read`
- `vocabulary.upsert_grounded`
- `vocabulary.list_candidates`

### Предметные owning products
- Wonderful Lections, KenigEvents, Street Story и другие через свои существующие service contracts/adapters.

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

## ADR-10: долговечные function calls

Provider call id помогает внутри Live-session, но продуктовые операции используют собственный durable command/source identity.

Для mutation:
- agent выбирает tool и semantic args;
- backend добавляет actor/scope;
- command_id/idempotency;
- expected revision;
- execute;
- readback/reconcile;
- tool result возвращается агенту.

Reconnect не превращает function call в новый смысловой запрос. Если outcome unknown, agent получает честный status и может решить следующий шаг, но backend не повторяет mutation слепо.

## ADR-11: стек без космолёта

Целевой backend — Python ASGI/FastAPI, модульный монолит и ограниченный worker той же кодовой базы. Прикладная PostgreSQL + outbox достаточно для MVP.

PWA — TypeScript/React. Android — Kotlin. Общие API schemas, state semantics и acceptance corpus важнее буквального общего UI-кода.

Backend на DevCoveer. Не Fly.io. Не self-hosted GitHub runner.

## ADR-12: связность экосистемы

| Owning project | Projects Hub использует | Не забирает себе |
| --- | --- | --- |
| idea-hub | source/provenance и разрешённые документы | весь личный archive пользователя |
| record-idea-hub | VAD/chunk/durable capture patterns | старое приложение до приёмки замены |
| live-interaction | provider transport, input transcription, audio response, function calls | product semantics |
| ai-resource-control | admission/leases/budgets/fallback | user content |
| wonderful-lections | предметные lecture tools | собственный presentation engine |
| events-bot-new | event/announcement tools | canonical event store |
| street-story | story tools | предметный workflow истории |
| my-data-hub | разрешённые knowledge/data tools | автоматическое раскрытие personal data |

Projects Hub связывает продукты через function calls central agent, а не копирует их business logic.

## Реальные framework изменения до product acceptance

1. deliberate buffered audio turn;
2. manual activityStart/activityEnd wire/setup;
3. lossless input-transcript observer для product source journal;
4. отсутствие 2000-char truncation на archive path;
5. product source persistence до 320-event UI ring;
6. multi-project conversation resource binding;
7. tool contracts для conversation memory/project focus/vocabulary;
8. клиентский durable capture online и offline без второго microphone/VAD stack.

Все эти изменения должны внедряться в owning repositories, версионироваться и иметь real-provider acceptance. Документирование не означает, что они уже работают.
