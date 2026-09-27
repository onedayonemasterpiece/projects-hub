# Projects Hub — спецификация живой совместной работы

**Ревизия 3 от 27 сентября 2026. Статус: спроектировано для реализации, не принято как работающее приложение.** Имя «Содей / Sodey» остаётся предложением; технический идентификатор `projects-hub` не меняется.

## Центральная идея

Projects Hub — это **один Live-агент с function calls**, который разговаривает с человеком голосом и работает сразу с множеством разрешённых проектов.

```text
голос пользователя
→ Gemini Live понимает речь
→ сам выбирает/уточняет проект и намерение
→ вызывает typed function
→ backend проверяет права и выполняет действие/readback
→ tool result возвращается той же Live-модели
→ агент отвечает голосом и продолжает разговор
```

Backend не содержит второго смыслового агента. Детерминированный код отвечает за сохранность аудио, transport, ACL, idempotency, revisions, transactions, device binding и readback — но не решает вместо Live-модели, что пользователь имел в виду.

## Что исправлено после архитектурного аудита

Предыдущая ревизия ошибочно описывала длинную офлайн-запись как `отдельный ASR → текст → Live`. Это отменено.

Теперь:
- online PCM идёт непосредственно Gemini Live;
- offline audio надёжно буферизуется и после появления связи **тоже подаётся аудио в центральную Live-модель**;
- для длинного buffered turn нужен manual `activityStart/activityEnd`, чтобы естественная пауза не вызвала ранний ответ;
- input transcription приходит от той же Live-сессии и сохраняется как source provenance;
- Live-agent сам через function call принимает semantic disposition: сохранить source, связать с проектом, обновить документ, создать задачу и т. п.;
- проект выбирает/уточняет agent, а backend лишь проверяет доступ;
- словарь пополняет agent, читая разрешённые документы через tools;
- tools не имеют права скрыто запускать второй LLM для routing/classification/summarization.

## Два приложения, один продукт

PWA и Android — два полноценных приложения с практически одинаковым основным интерфейсом. Android имеет дополнительные platform capabilities и более сильную offline/background capture надёжность, но не является «тонким диктофоном».

Свободного текстового composer в MVP нет. Основной путь — живая речь. Текст показывается для ответа, transcript/document/history; выборы могут появляться кнопками.

## Offline без потери

Если связи нет, пользователь может говорить несколько минут. Один source сохраняется локально с VAD/chunks/manifest. Когда Live возвращается, source целиком воспроизводится центральному agent как один buffered audio turn.

Agent слышит его, отвечает и вызывает tools. Пользователю не нужно повторно наговаривать обычную потерянную из-за сети речь.

Stop, «Новый разговор» и delete — разные действия. Pending source не исчезает при новом разговоре.

## Долговременная память

Не каждый turn обязан становиться отдельным Markdown. Решение принимает Live-agent по смыслу разговора.

При сохранении agent вызывает memory function, которая материализует уже накопленный provider transcript/source journal в Markdown с provenance и project links. Backend не переписывает смысл отдельной моделью.

Если agent не успел принять disposition, source остаётся pending и не очищается.

## Проекты

Один разговор может свободно переходить между проектами:
- «теперь про фестиваль»;
- «это для клуба»;
- «сравни три проекта»;
- «нет, предыдущую мысль отнеси к подкасту».

Ручной selector — удобство, не обязательное условие.

Существующий одно-проектный `ProjectScope` — ранний scaffold и должен быть эволюционирован: Live session должна быть actor/workspace/conversation-scoped, а доступ к конкретным проектам проверяется каждым tool call.

## Словари

Проверенные идеи словарей Record Idea Hub / IdeaHub / Wonderful Lections сохраняются: aliases, misrecognitions, source refs, revisions, acoustic/contextual compatibility.

Но vocabulary semantic work делает центральный Live-agent: он читает разрешённые GitHub docs и вызывает vocabulary tools. Отдельного словарного LLM-worker не требуется.

## Найденные framework gaps

Аудит фактического `live-interaction` выявил важные зависимости:
- input transcription уже включена;
- provider сейчас обрезает projected transcript event до 2000 символов;
- session host ring ограничен 320 events;
- browser transcript preview хранит только хвост;
- reconnect намеренно не queue/replay old speech;
- bootstrap history короткая;
- нет product-ready manual `activityStart/activityEnd` buffered mode.

Это означает: Projects Hub нужен versioned framework increment для durable buffered turn и lossless transcript sink. Это **не** основание вводить отдельный ASR или semantic pipeline.

## Навигация

| Документ | Содержание |
| --- | --- |
| [Центральный Live-агент](12-central-live-agent.md) | Главный архитектурный инвариант, function calls и найденные framework gaps |
| [UX/UI](03-product-and-ux.md) | Live-first интерфейс двух приложений |
| [Память](10-conversation-memory.md) | Online/offline audio source, agent disposition, Markdown, recovery |
| [Проекты и словари](11-routing-and-vocabulary.md) | Multi-project reasoning и agent-owned vocabulary |
| [Архитектура](04-architecture.md) | One-agent dataflow, thin tools, source ownership |
| [Live/resource](06-live.md) | Gemini Live, buffered replay и shared limiter |
| [Безопасность](05-security.md) | ACL, личное/групповое, device actions |
| [MVP](07-delivery.md) | Порядок реализации |
| [Надёжность](08-reliability.md) | Release gates и failure cases |
| [Источники](01-evidence.md) | Голосовые и прямые owner clarifications |
| [Позиционирование](02-positioning.md) | Имя, pitch, пилот |
| [Внешние источники](09-research.md) | Проверенные API/platform constraints |
| [Общий реестр](contract.json) | Sources/integrations/gates |
| [Live contract](live-contract.json) | Машиночитаемые архитектурные правила |

## Фактический baseline и статус

Исходный Projects Hub runtime scaffold содержит только resource adapter к общему `ai-resource-control`; полноценного backend/client/agent adapter ещё нет.

Текущий shared Live framework уже поддерживает прямой audio, input transcription, function calls и tool responses. Однако buffered offline source и lossless transcript archive требуют расширения framework.

Работающий Record Idea Hub не выключается до отдельной реальной приёмки нового capture/recovery пути.

Документация и contract tests не доказывают microphone/provider acceptance. Не выполненные gates остаются `not_run`.
