# Насыщенный MVP и порядок реализации

[Индекс](README.md) · [Центральный Live-агент](12-central-live-agent.md) · [Архитектура](04-architecture.md) · [Память](10-conversation-memory.md).

**Ревизия 3, 27 сентября 2026.** MVP строится вокруг одной умной Live-модели и её function calls. Никакой промежуточный ASR/router/classifier не должен незаметно стать реальным мозгом продукта.

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
- online PCM одновременно durable checkpoint + Live send;
- offline durable chunks/manifest;
- Stop/new conversation/delete имеют разные semantics.

Android получает дополнительные foreground/device capabilities, но core workflow не урезан.

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
- meeting proposals/calendar;
- notifications;
- event/project cards.

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
- новый presentation/event/story engine.

## Ownership

Projects Hub: conversation semantics, product tools, memory/source, UI, collaboration.

`live-interaction`: transport, provider protocol, buffered activity mode, transcript delivery.

`ai-resource-control`: provider resource admission.

Record Idea Hub: проверенные capture/VAD/durable queue patterns до извлечения reusable части.

Owning products: свои документы и предметные mutations.

## Договор реализации

Перед каждым крупным решением задавать контрольный вопрос:

> Это инфраструктура, которая помогает Live-agent надёжно работать, или новый слой, который сам начинает понимать пользователя вместо Live-agent?

Если второе — остановиться и обосновать отдельное продуктовое решение. По умолчанию такое усложнение запрещено.
