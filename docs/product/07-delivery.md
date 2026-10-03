# Насыщенный MVP и порядок реализации

> **U04 / 2 октября 2026:** WSS migration и multi-user acceptance выполняются поверх существующего продукта. Android self-update через signed GitHub Releases сохраняется как обязательный нормальный путь; подробности и rollout gates — [16-wss-platform-reliability.md](16-wss-platform-reliability.md).

[Индекс](README.md) · [Центральный Live-агент](12-central-live-agent.md) · [Архитектура](04-architecture.md) · [Память](10-conversation-memory.md).

**Ревизия 4, 28 сентября 2026.** MVP строится вокруг одной умной Live-модели и её function calls. **Live provider connection и server integrations находятся на одном Projects Hub backend на DevCoveer.** Никакой промежуточный ASR/router/classifier не должен незаметно стать реальным мозгом продукта.

## Кратчайший текущий путь к продуктовому результату

Не реализовывать все идеи из голосовых одновременно. Текущий вертикальный срез: **central Live → `calendar.create_event` function call → durable backend device command → конкретное paired Android-устройство → системный календарь → typed receipt/readback → тот же Live-разговор**. Сначала довести этот путь на реальном телефоне и не потерять уже начатую реализацию device-command outbox.

После этого: минимальный Android updater с обычным системным подтверждением, затем простые collaboration primitives — event/task card, checklist готовности и уведомление. Внешние каналы, библиотека ресурсов, сложные голосования и vector search не блокируют этот E2E.

## M0 — подтвердить внешние условия

- provider/data/region/age policy для заявленной аудитории;
- актуальные версии `live-interaction` и `ai-resource-control`;
- прикладная PostgreSQL, backup/restore;
- identity provider;
- pilot group/use case.

Разработку ядра можно вести синтетически параллельно; реальные закрытые данные не отправляются неподходящему provider path.

## M1 — shared Live framework для Projects Hub

До product adapter закрыть reusable gaps:
- deliberate buffered audio feed;
- manual activity detection mode + `activityStart/activityEnd`;
- lossless trusted input-transcript sink;
- отделение full transcript source от bounded UI projection;
- product observer до 320-event ring eviction;
- durable source feed без второго transport;
- проверка resumption/recovery без replay mutations.

Это изменения owning `live-interaction`, а не локальный форк Projects Hub.

## M2 — capture substrate двух приложений

PWA и Android реализуют единый UX и один source model:
- одна большая кнопка;
- один microphone pipeline;
- VAD/pre-roll/hangover;
- online PCM одновременно durable checkpoint + send **в Projects Hub backend**, который уже отправляет PCM в свою Gemini Live session;
- offline durable chunks/manifest;
- Stop/new conversation/delete имеют разные semantics.

Android получает дополнительные foreground/device capabilities, но core workflow не урезан. Эти capabilities не переносят backend на телефон: server-issued typed command приходит конкретному device, Android выполняет platform API и возвращает receipt.

Для доставки новых Android-сборок достаточно минимального updater path: version manifest с доверенного Projects Hub endpoint/артефакта, подписанный APK, проверка version/signature/hash и системный package installer. Не строить собственный store, MDM или privileged silent installer.

Выход M2: можно записать речь online/offline, пережить restart/network loss и доказать, что источник не потерян. Это ещё не означает, что agent правильно понял содержимое.

## M3 — центральный Live-agent и thin tools

Product adapter:
- actor/workspace/conversation scope;
- server-owned system instruction;
- project catalogue;
- memory functions;
- project docs read/search;
- tasks/decisions/meetings;
- vocabulary tools;
- owning product adapters.
- server-side GitHub App catalogue/operations; GitHub credentials никогда не покидают backend;
- durable device-command outbox для Android-local capabilities.

Acceptance:
- raw online audio → Live input transcription → function call → readback → voice response;
- tool implementations не вызывают второй semantic LLM;
- model сама задаёт столько вопросов, сколько нужно;
- нет формы Send/Edit и обязательного text composer.

## M4 — multi-project conversation

Эволюционировать одно-проектный `ProjectScope`.

Нужно:
- conversation/workspace resource binding;
- per-tool target authorization;
- voice/context project switching;
- unresolved route → agent clarification;
- один монолог может породить результаты в нескольких проектах;
- полный source остаётся личным, пока agent явно не сохраняет разрешённую часть в группу.

Acceptance: три проекта, похожие названия, поздняя поправка target, недоступный project, разговор «по всем проектам» без reconnect.

## M5 — offline buffered Live turn

Длинная запись без сети:
- seal source;
- resumable upload;
- feed raw audio central Live-agent;
- manual activityStart/end;
- agent слышит весь пакет;
- input transcript journal;
- semantic function calls;
- voice response о фактически выполненном.

Сеть может повторно пропасть. Source остаётся и recovery продолжает тот же logical turn/epoch без повторных business effects.

Никакой отдельный transcription model не вставляется перед Live.

## M6 — долговечная conversation memory

Agent tools:
- commit voice source;
- finish ephemeral source;
- search/get historical source;
- resume conversation.

Permanent Markdown создаётся по semantic disposition Live-agent, а не по жёсткому секундному порогу.

Если agent не успел disposition — source pending и не удаляется.

Acceptance: короткое важное, длинный вопрос без новой памяти, explicit «запомни», возврат через неделю, old relative date, source correction.

## M7 — развивающийся словарь

Central agent:
- читает разрешённые GitHub docs;
- сравнивает vocabulary snapshot;
- вызывает grounded upsert;
- использует новый term в дальнейшем разговоре.

Backend хранит schema/source refs/revisions и ACL. Никакого фонового dictionary LLM.

Acceptance: имена, аббревиатуры, алиасы, конфликт, user correction, source revision, закрытый документ.

## M8 — коллаборация

Поверх работающего agent:
- поручения proposed/accepted/blocked/done;
- brainstorm с исходными source refs;
- простое голосование или решение ответственного;
- meeting proposals;
- calendar: server function call → bound Android device command → Android Calendar/provider API → receipt → backend → same Live agent;
- notifications/device actions через тот же bound-device command channel;
- event/project cards;
- class-specific readiness checklist / Definition of Done с lead time;
- follow-up task по реально отсутствующему материалу.

Все эти действия инициирует agent function calls или contextual buttons. Button event входит в тот же conversation/product state и не создаёт отдельный интеллект.

## M9 — связи owning products

Сначала минимальные полезные адаптеры:
- IdeaHub/document memory;
- Wonderful Lections;
- KenigEvents;
- затем Street Story и остальные по нужде.

Не переносить их business logic в Projects Hub. Agent использует service contracts.

## M10 — выпускная проверка

Обязательны:
- multi-project isolation;
- source durability;
- real microphone;
- long buffered audio;
- transcript completeness;
- tool idempotency;
- two devices;
- revocation;
- quota/fallback;
- calendar unknown outcome;
- backup/restore;
- vocabulary;
- prompt injection;
- нет скрытого semantic model внутри tool layer.

Отдельно реальный pilot нетехнических взрослых пользователей должен пройти полный цикл:
**сказали → agent понял → договорились → function calls → подтверждённый результат → позже нашли исходный разговор**.

## Что не делать до доказанной необходимости

- универсальный чат/текстовый редактор;
- второй AI-router;
- отдельный ASR-preprocessor;
- hidden summarizer;
- автоматический фоновый LLM dictionary worker;
- Kafka/Kubernetes/CRDT;
- универсальный local desktop agent;
- сложные weighted voting schemes;
- массовый импорт всех мессенджеров;
- vector DB / embedding pipeline до доказанной нехватки обычного индексированного поиска;
- универсальный workflow/BPM-конструктор для чек-листов;
- privileged silent Android updater/MDM;
- полную библиотеку файлов/медиа до отдельной итерации V06;
- новый presentation/event/story engine.

## Ownership

Projects Hub backend on DevCoveer: единственная Live provider session, conversation semantics, product tools, memory/source, GitHub App, server integrations, device-command outbox и reconciliation.

PWA/Android: capture/UI/local durable queue. Android дополнительно исполняет allowlisted device-local commands; он не является backend и не получает GitHub/provider credentials.

`live-interaction`: shared transport/provider protocol, buffered activity mode, transcript delivery; provider host используется backend-side, capture primitive переиспользуется клиентами.

`ai-resource-control`: provider resource admission.

Record Idea Hub: проверенные capture/VAD/durable queue patterns до извлечения reusable части.

Owning products: свои документы и предметные mutations.

## Договор реализации

Перед каждым крупным решением задавать контрольный вопрос:

> Это инфраструктура, которая помогает Live-agent надёжно работать, или новый слой, который сам начинает понимать пользователя вместо Live-agent?

Если второе — остановиться и обосновать отдельное продуктовое решение. По умолчанию такое усложнение запрещено.
