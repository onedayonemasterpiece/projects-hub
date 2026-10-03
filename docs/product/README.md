# Projects Hub — спецификация живой совместной работы

> **Обновление 3 октября 2026:** WSS/multi-user source candidate реализован поверх shared `live-interaction v0.3.8` release: same-origin one-use tickets, binary PCM/ACK, pushed events, no silent HTTP fallback, bounded global/per-actor admission и duplicate buffered-source exclusion. WSS integration tests и полный backend regression suite (107) проходят; clean `npm ci` + PWA production build проходит. Direct public DNS/TLS/HTTPS/WSS ingress теперь принят на реальном сервере; полный production acceptance всё ещё требует 3+ independent-user real-provider soak и physical Android gates. Канонический design/status: [WSS и многопользовательская надёжность](16-wss-multi-user-reliability.md).

**Ревизия 8 от 3 октября 2026. Статус: central Live/PWA и foreground Android WebView работают через единый WSS consumer `live-interaction v0.3.8`; Android device-command/calendar, signed GitHub Releases, event-readiness и expert-review слой реализованы; полный production acceptance не объявлен из-за незакрытых physical-device/GitHub-App/multi-user/Regional-Knowledge gates.** Имя «Содей / Sodey» остаётся предложением; технический идентификатор `projects-hub` не меняется.

## U05 / 3 октября: versioned shared components

Свежий owner review `voice-20261003-083638-766e61b7` требует перед новой реализацией искать и переиспользовать уже отработанные межпроектные решения вместе с их runtime/incidents. Общий Live-контур подключается по стабильной версии/capability contract, а не по raw SHA. Поэтому Projects Hub consumer переведён на stable `live-interaction v0.3.8`; release commit и asset digest остаются evidence воспроизводимости, но не интерфейсом зависимости.

> **Runtime update · 3 октября:** backend release `074409cc…` с same-conversation Live owner lease развёрнут. Direct public ingress `projects-hub.kenigevents.ru → 78.111.90.230 → local nginx → 127.0.0.1:8196` принят: TLS/HTTPS PASS, public WSS real-provider roundtrip PASS, public two-session concurrency PASS, third same-actor session получает `429 LIVE_BUSY`. Android `android-v6` / `0.1.6` опубликован signed release workflow; PR #31 прошёл emulator-smoke и self-update E2E.

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

> Историческая фиксация аудита 27–28 сентября. В WSS candidate от 3 октября manual activity и socket transport уже есть в pinned shared framework; актуальные незакрытые acceptance gaps перечислены в [16-wss-multi-user-reliability.md](16-wss-multi-user-reliability.md).

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
| [UI: тёмные плавающие острова](13-ui-floating-islands.md) | Визуальная система PWA/Android и основной экран |
| [GitHub connections](14-github-connections.md) | GitHub App installation, repository roles и отсутствие PAT у обычных участников |
| [Экспертные review cases](15-expert-review-cases.md) | Межпроектные экспертные проверки: assignment, evidence ACL, typed resolution и readback |
| [WSS и многопользовательская надёжность](16-wss-multi-user-reliability.md) | Целевой realtime transport, session isolation, concurrency, capability bundles, shared OAuth/Regional Knowledge/POI ownership, implementation evidence, acceptance и rollout |
| [Live contract](live-contract.json) | Машиночитаемые архитектурные правила |

Свежие требования 28 сентября не меняют central-Live архитектуру: V19 добавляет безопасный in-app путь обновления Android после первой установки; V20 добавляет class-specific checklist готовности к приближающимся событиям и создание конкретных follow-up задач по недостающим материалам. Оба пункта включаются последовательно после работающего device-command/calendar E2E, а не расширяют MVP до универсального workflow engine.

## Обновление 2 октября: WSS, несколько пользователей и свежие owner review

Realtime этап зафиксирован в [WSS и многопользовательской надёжности](16-wss-multi-user-reliability.md). Source candidate уже реализует shared WSS transport, one-use tickets, bounded queues/ACK, session generations, запрет stale-audio/mutation replay и actor/workspace/conversation isolation. Deployed production/runtime уже прошёл direct public-ingress и real-provider WSS acceptance; отдельными gate остаются physical-device и broader independent-user acceptance.

Свежий owner review `voice-20261002-174500-c34d45c8` добавляет два базовых UX-направления: light/dark theme с voice control и ненавязчивый adaptive onboarding, который помогает человеку постепенно открывать возможности продукта и может мягко напоминать давно не использованные функции. Preference/usage state должны быть actor-scoped.

Owner review `voice-20261002-173201-0808cd76` фиксируется как исследовательское расширение: асинхронный экспертный дискурс может в будущем становиться evidence-backed редакционным материалом. Это не расширяет текущий MVP автоматически; до реализации нужны participant identity/provenance, privacy/publication consent и отдельная продуктовая приёмка.

## Фактический baseline и статус

Текущий runtime-код — `a6c650e2f0944884dd01b57524ab77211e8db0aa`; он развёрнут на DevCoveer и healthy. Репозиторий `main` уже содержит последующие docs/test-only commits до `20083f29d0c9b8505b47bd59faa0b7e0651fb4b2`, не меняющие backend/runtime: central Gemini Live, resource-control, durable SQLite/WAL, device-command API и readiness/task слой доступны. После деплоя реальный provider canary снова прошёл: `conversation_set_focus` → backend readback → голосовой ответ → `turn_complete`.

Android теперь существует как отдельное устанавливаемое приложение: WebView/PWA сохраняет общий floating-islands Live UX и использует тот же shared WSS transport, а native-слой хранит device credential в Android Keystore, исполняет allowlisted `calendar.create_event` через Calendar Provider, требует подтверждение на телефоне и возвращает `applied` только после readback. GitHub Actions build/unit и Android emulator install+launch прошли на `main`. Signed releases `android-v1`…`android-v6` опубликованы; latest — `android-v6` / `0.1.6`. В текущем Android updater есть retry transient network errors, повторная проверка update на resume и безопасный lifecycle log. Release/feed SHA-256 chain проверена. Hosted Android 14 acceptance теперь PASS для `android-v4 → android-v5`: product обнаружил update, native update button стал готов, verified APK передан Android Package Installer, затем тот же signed APK принят как in-place update `versionCode 4→5` с сохранением UID. Финальный пользовательский тап системной кнопки установки остаётся physical human gate. Foreground Live внутри Android не дублируется native socket-клиентом: WebView запускает тот же PWA WSS. Web microphone permission выдаётся только exact Projects Hub origin; OAuth-страницы не получают audio capture.

После подтверждённого calendar result backend автоматически создаёт event card. Реализованы bounded templates `generic` и `podcast`, checklist готовности и follow-up tasks со статусами proposed/accepted/done/snoozed/rejected; UI показывает это в существующих floating islands. Backend suite после этого слоя: 80 pytest PASS; PWA production build PASS.

Не закрыты и поэтому не подменяются mock/unit evidence: physical Android WebView-microphone/calendar E2E, Android offline/reboot capture, 3+ independent-user real-provider soak и production GitHub App registration/credentials (`github_app_configured=false`). Публичный direct ingress `projects-hub.kenigevents.ru` уже принят по пути DNS → DevCoveer → локальный TLS/SNI → `127.0.0.1:8196`; внешний Yandex edge не используется. Работающий Record Idea Hub не выключается до отдельной физической приёмки нового Android capture/recovery пути.