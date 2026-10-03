# Live, общий лимиттер и центральный агент

> **U05 / 3 октября 2026:** primary client↔backend transport — authenticated same-origin WSS через `LiveSocketSessionHost`; Projects Hub consumer использует stable `live-interaction v0.3.8`. Release commit/digest — verification evidence, а не dependency interface. После WSS attach silent HTTP audio fallback запрещён; ticket/reconnect/backpressure contract — в [16-wss-multi-user-reliability.md](16-wss-multi-user-reliability.md).

[Индекс](README.md) · [Центральный Live-агент](12-central-live-agent.md) · [Архитектура](04-architecture.md) · [Память](10-conversation-memory.md).

**Ревизия 4, 28 сентября 2026.** Live — не один из сервисов обработки Projects Hub, а центральная интерактивная модель продукта. **Gemini Live provider session существует на единственном Projects Hub backend на DevCoveer; PWA и Android не подключаются к Gemini напрямую.** Общий resource controller ограничивает её ресурс; tools дают ей руки; durable capture не даёт потерять речь.

## 1. Текущий фактический baseline

В актуальном `live-interaction`:
- PCM16 16 kHz отправляется Gemini Live напрямую;
- setup включает `inputAudioTranscription` и `outputAudioTranscription`;
- provider выдаёт `tool_call`, input/output transcript, audio и turn events;
- session host выполняет product adapter tools и возвращает function responses той же Live-сессии;
- resource guard защищает provider connect/send/receive.

Это подтверждает правильную архитектурную ось: **client audio → Projects Hub backend → Live model → function calls → backend/device executor → readback → та же Live model → backend → client**.

Projects Hub уже имеет ранний resource adapter к `ai-resource-control`, но не готовый product adapter.

## 2. Что resource limiter делает и чего не делает

`ai-resource-control`:
- выбирает допустимый provider scope/credential;
- admission/lease/budget;
- guards provider traffic;
- управляет bounded outage fallback.

Он не:
- распознаёт речь;
- выбирает проект;
- суммаризирует;
- решает, архивировать ли разговор;
- генерирует agent reply.

Нельзя использовать limiter/ledger как product state или semantic router.

## 3. Normal Live session

Порядок:
1. клиент authenticate actor в Projects Hub backend;
2. backend создаёт conversation/workspace scope;
3. backend собирает server-owned system instruction и небольшой разрешённый bootstrap context;
4. backend получает resource lease;
5. **backend** подключает Gemini Live и владеет provider session;
6. PWA/Android передают raw PCM в backend; shared `live-interaction` на backend отправляет его provider;
7. backend сохраняет provider input-transcript/source events;
8. backend исполняет Live function calls через product adapter или durable device-command channel;
9. backend возвращает tool/device receipts модели;
10. model продолжает голосовой ответ, backend доставляет audio/events клиенту.

Provider key/resumption handle и GitHub credentials не выдаются клиенту. Android не создаёт отдельную Live session для локальных platform actions.

Project access проверяется **на каждом tool**, а не один раз через project_id provider session.

## 4. Multi-project conversation

Один actor может говорить о нескольких разрешённых проектах без нового provider connection при каждом переключении.

Текущий `ProjectScope(..., project_id)` в Projects Hub resource scaffold не является финальной моделью. Целевой Live resource binding — actor + tenant/workspace/conversation. Project focus — state агента, а права на target project — server-side tool authorization.

Это изменение не ослабляет изоляцию: Live-session личная для actor. Общий проект получает только то, что agent записал туда разрешённым function call.

## 5. Длинная offline-речь

Ключевое исправление: **никакого separate ASR → text → Live**.

Offline client хранит audio. После соединения central Live-model получает этот audio.

Для buffered replay нужен режим:
- setup/realtimeInput config с automatic activity detection disabled;
- `activityStart`;
- весь buffered PCM;
- `activityEnd`.

Это официально поддерживаемая семантика Live API для client-controlled activity boundaries. Она позволяет передать несколько минут записи как один логический user turn несмотря на внутренние паузы.

Current `audioStreamEnd` с default server VAD недостаточен для гарантии единого длинного buffered turn: model может определить end-of-speech раньше, чем будет отправлен весь backlog.

## 6. Deliberate replay против transport replay

В текущем provider есть правило: при потере WebSocket не queue/replay old speech. Оно остаётся правильным для **неопределённого realtime reconnect**, чтобы случайно не повторять уже услышанную команду.

Projects Hub решает другую задачу:
- durable source находится **выше** transport;
- есть manifest и source_id;
- после восстановления создаётся deliberate buffered turn;
- он помечается recovery epoch;
- повторные business effects fenced durable command IDs.

То есть мы не заставляем transport слепо replay-ить потерянный socket backlog. Product намеренно повторно предъявляет agent известный source, когда это нужно для понимания/восстановления.

## 7. Input transcription — та же Live-модель

Текущий provider включает:
```text
inputAudioTranscription: {}
```

Поэтому отдельный транскрипционный model call в базовой архитектуре не нужен.

Provider `input_transcript` events используются для:
- отображения;
- source provenance;
- Markdown;
- поиска разговоров;
- восстановления контекста.

Agent не обязан получать transcript через tool, чтобы понять собственный audio turn — модель уже слышит audio. Но для позднего поиска и exact source backend сохраняет provider transcription journal.

## 8. Найденные gaps shared framework

### Transcript truncation

Сейчас `provider.py` проецирует `text[:2000]` для input/output transcription event.

Это подходит как защитный UI projection, но **не подходит source archive**. Нужен lossless product observer/event path либо изменение event shape: full text для trusted adapter + bounded preview для UI.

### 320-event ring

Session host ring ограничен 320 events. Product source persistence должна происходить до eviction. Event polling client не является archive database.

### Browser preview

Browser собирает только хвост input transcript. Это UX state, не источник.

### Short bootstrap history

Host передаёт ограниченную recent history. Старые разговоры agent получает через conversation tools/source retrieval. Нельзя увеличивать prompt до всего архива.

### Buffered activity markers

Shared wire нужно расширить typed events `activity_start/activity_end` и setup option manual activity detection для deliberate buffered turn.

### Durable source feeding

Нужен adapter-level механизм читать сохранённый source и bounded/paced отправлять PCM через shared provider без создания второго transport implementation.

Все эти gaps исправляются в `live-interaction` как reusable capability, а Projects Hub только использует versioned release.

## 8.1. Device-local function execution

Часть function calls имеет server-owned executor (memory, GitHub, project docs, owning service API), а часть — device-owned executor (личный календарь Android, notification permission, другие явно разрешённые platform capabilities).

Для device-owned call central Live-model всё равно вызывает function **в backend session**. Backend:
- авторизует actor + workspace + concrete device;
- записывает durable command/idempotency state;
- доставляет command только связанному Android device;
- получает typed result/receipt;
- reconciles unknown outcome/retry;
- возвращает подтверждённый result той же Live-модели.

Android исполняет только allowlisted platform command и не получает provider/GitHub credentials. Device channel — исполнитель, не второй semantic agent.

## 9. Function calls: одна модель, много инструментов

Live configuration содержит только нужный набор typed functions. Примеры:
- project/document read/search;
- conversation memory;
- task/decision/meeting;
- vocabulary;
- owning product adapters.

Function implementation не вызывает скрытую вторую модель для semantic work.

Паттерн:
```text
user audio
→ Live understands
→ Live calls tool
→ deterministic authorized execution/readback
→ tool response
→ Live explains/continues
```

Если tool не знает semantic choice, он возвращает data/options/error агенту. Он не выбирает вместо модели.

## 10. Vocabulary context

Scoped terminology может входить в bounded server-owned context и быть доступен tool. Agent умеет самостоятельно пополнять словарь по разрешённым GitHub documents.

Нельзя обещать, что этот словарь является отдельной “speech recognition vocabulary API” Gemini Live, пока такая поддержка не доказана для конкретной модели. Он прежде всего помогает **той же Live-модели** понимать/нормализовать domain speech.

## 11. Resource fallback

Сохраняется текущий общий договор:
- normal central authority;
- Projects Hub source alias `GOOGLE_API_KEY4` преобразуется trusted backend в generic fallback;
- fallback только при исходной read-only authority unavailable до mutating acquire;
- не при 429/quota/no-capacity/lost acquire/после provider ready;
- не брать keys других consumers;
- bounded emergency admission/expiry.

Fallback меняет credential/resource path, **не интеллект и не model pipeline**.

## 12. Stop, reconnect и memory

Stop:
- немедленно прекращает local capture/playback current turn;