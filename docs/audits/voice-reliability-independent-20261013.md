# Независимая проверка надёжности голоса Projects Hub — 13 октября 2026

## Объект, метод и границы

- Прочитан PR [#166](https://github.com/onedayonemasterpiece/projects-hub/pull/166), head `2fe01e084d3d3ac378ffda2c09111f16159ae576`; он добавляет только отчёт, не исправления.
- Независимо прочитан текущий `main` Projects Hub `af65f93e2c853f2cc64e57a7d9b3b9f87c28edfc`, а не только исходный baseline отчёта `b48566f`.
- Backend и web/package.json по-прежнему закрепляют `live-interaction 0.3.28` / `3e675077e30b06002fe6477d8cdd5fbae6c1331e`. Релевантный shared code прочитан непосредственно из этого ref.
- Это статическая проверка кода и нормативных документов. Production-журналы, клиентская телеметрия, физический телефон и реальные сетевые прогоны в этой проверке не исследованы. Числа 42/18/115 с и инцидент 18:39 UTC принадлежат отчёту Claude, а не независимому измерению.
- Код, runtime, зависимости и лимиты не изменялись. Обновления рядом — целевые требования, не заявление о реализации.

Ссылки на shared-файлы ниже относятся к [live-interaction@3e675077](https://github.com/onedayonemasterpiece/live-interaction/tree/3e675077e30b06002fe6477d8cdd5fbae6c1331e); продуктовые — к [Projects Hub@af65f93](https://github.com/onedayonemasterpiece/projects-hub/tree/af65f93e2c853f2cc64e57a7d9b3b9f87c28edfc).

## 1. Что подтверждено независимо

| Находка | Проверенное основание | Вывод |
| --- | --- | --- |
| Исключение socket relay останавливает логическую Live-сессию | shared `python/live_interaction/socket_transport.py:200–214`: любой Exception ставит fatal=True; finally вызывает binding.stop. ACK send в 135–136, output timeout в 117, egress overflow в 103–104 | Риск потери сессии при transient send failure подтверждён |
| Stop-and-wait browser upload | shared `browser/live-audio.js:90–125`: busy, максимум 11000 bytes, await send; `browser/socket-transport.js:66–72,149–157`: Promise ждёт ACK | Верхняя граница PCM16 16 kHz — 11000/32000=0,34375 с речи на полный ACK cycle. Это аналитический предел, не измеренный RTT |
| Capture прекращается при reconnect | shared `browser/client.js:320–345`: closeMic до retry, deadline=10s, attempts<3; затем stop | Восстановление текущего transport не сохраняет непрерывный захват |
| Provider retry budget расходуется за жизнь session | shared `python/live_interaction/provider.py:345,651–655`: счётчик инициализирован один раз, инкрементируется при reconnect, успешный setup не сбрасывает; `591–599` goAway ведёт к закрытию/retry | Долгий разговор ограничен количеством переподключений. Точное «30–40 минут» по коду не доказано |
| Transition ждёт fresh handle; fallback history ограничена | shared `provider.py:16,22–23,416,535–554`: timeout=8; MAX_HISTORY_TURNS=8, MAX_HISTORY_TEXT=700; выбор checkpoint/history_restore | Ограниченный bootstrap не равен полной памяти. Уменьшение timeout без continuity-дизайна ускорит деградированный путь |
| Durable write синхронный на audio admission | `src/projects_hub/live_adapter.py:1691–1692` вызывает append_audio; `store.py:3117–3137` lock/write/fsync/SQLite; `live_runtime.py:244` liveness=30000ms | Блокирование event loop правдоподобно и следует из call path; величина задержки требует измерения |
| UI не экспортирует shared timing events | `web/src/App.tsx:1103–1127`: onTiming обновляет UI, детали метрик не принимает/не отправляет | Для причины клиентского reconnect и качества playback недостаточно серверных логов |
| Native voice edge отсутствует | Android tree содержит MainActivity/device/update код; manifest не объявляет microphone foreground service; MainActivity `206–210` освобождает audio focus onPause | Физическая фоновая гарантия Android не реализована; фактическую длительность до сбоя нельзя выводить из одного manifest |

## 2. Что следует уточнить в выводах Claude

1. **Не каждый disconnect фатален.** `socket_transport.py:127–129` normal EOF возвращается без fatal; `198–199` CancelledError пробрасывается. Проблема конкретно в общей fatal-классификации исключений, в том числе ошибок отправки. Слова «любой обрыв убивает» чрезмерны.
2. **Первопричина конкретного инцидента не установлена здесь.** Клиент имеет не только audio-сторожа: закрытие WebSocket/heartbeat/протокольная ошибка также способны запускать reconnect. Одинаковый IP не исключает сетевой сбой; быстрый HTTP не исключает проблему WSS, event loop или downlink. Нужна коррелированная клиентская телеметрия.
3. **GoAway не задаёт доказанный фиксированный срок жизни.** Лимит reconnect подтверждён, но «смерть через 30–40 минут» зависит от внешнего provider и характера закрытий. Настройку только reset-on-setup нельзя принимать без rolling-rate guard: она может разрешить бесконечный быстрый churn.
4. **Паузы и compression не следует устанавливать по косвенным байтам.** Nginx byte mismatch не доказывает permessage-deflate и не исключает недоставку. Underflow объясняет возможные прерывания, но без клиента не доказывает их.
5. **Opus полезен как измеряемое улучшение, не лечение lifecycle.** Расчёт PCM 256/384 кбит/с корректен; поддержка codec/API и ASR-качество на целевых устройствах требуют проверки. «8–16×», нагрузка CPU и latency catch-up не универсальные гарантии.
6. **Не менять произвольно timeout/window.** 60–120 с, 64 КБ, 0,5–1 с fresh-handle wait, размеры jitter buffer — исследуемые параметры, а не принятые требования.
7. **Async persistence не должна ослабить durable-before-provider.** Вынести blocking I/O можно, но admission/receipt должны ясно различать volatile, durable, provider delivery и semantic success. Group fsync без оговорённого crash-loss contract нарушит сохранность.
8. **Seamless replay нельзя просто включить.** Текущие `.devcoveer/requirements.json:17–24,40–46` требуют no stale replay, fresh generation и запрет mutation повреждённого turn до clean turn. Новый seq/ACK протокол сам по себе не доказывает, что provider не услышал или не исполнил старую команду.

## 3. Android: подтверждённое расхождение постановки и реализации

Исходный [16-wss на b438f0d](https://github.com/onedayonemasterpiece/projects-hub/blob/b438f0d/docs/product/16-wss-multi-user-reliability.md#4-android-от-webview-микрофона-к-native-reliable-voice-edge), строки 87–97, явно требует native microphone/VAD/queue/WSS/playback через shared Java transport и узкий UI bridge. Phase 3 повторяет native edge.

В [1359046](https://github.com/onedayonemasterpiece/projects-hub/commit/1359046), §4 заменён на browser/WebView foreground и future native durability. Текущий main сохраняет эту замену. Одновременно `03-product-and-ux.md:15–19` требует полноценный Android с длительным захватом при блокировке, а `04-architecture.md:11–23` — foreground-service запись и явные ограничения PWA.

Вывод: документальное изменение архитектуры доказано; отсутствие отдельного согласования владельца нельзя доказать одним git diff. Поэтому фиксируем расхождение без предположений о мотивах автора. Обновлённый целевой договор ниже восстанавливает native архитектуру, отдельно от фактического статуса реализации. Shared native binding не является вторым продуктовым provider transport, если он заменяет WebView path и остаётся в общем framework. Shared `docs/native-wss.md` прямо разделяет platform capture и общие protocol/conformance contracts.

Термин «системный захват» следует уточнять: native microphone/lifecycle, playback capture другого приложения и PSTN/VoIP call recording — три разные возможности. Старые native требования не доказывают доступ к аудио других приложений или обеим сторонам звонка.

## 4. Минимальная связная архитектура исправления

1. В общем framework отделить lifetime logical session от socket attachment. Временный I/O failure переводит binding в bounded detached; auth/protocol violations и explicit Stop остаются fail-closed. Не создавать продуктовый обход framework.
2. Сначала добавить privacy-safe correlation и воспроизводимые regression/network тесты. Разделить capture, relay ACK, durable receipt, provider delivery/comprehension и command receipt.
3. Устранить blocking durability I/O, сохранив ordered durable barrier; пересмотреть ingress window и writer backpressure вместе, не только поднять timeouts.
4. Сохранить capture/source при временной потере связи независимо от socket. Unsafe realtime turn помечать damaged; recovery — известный durable source, epoch, актуальность, idempotency/readback. Не подавать неизвестный socket backlog как новую команду.
5. Восстановить provider continuity из checkpoint, а при недоступности честно обозначать bounded history fallback и иметь durable journal retrieval. Ограничивать retry rate/time, не весь срок разговора.
6. Native Android framework service владеет микрофоном, playback, focus/routes и transport; WebView показывает typed state. PWA остаётся полноценным foreground browser/iOS путём с той же backend/provider/security архитектурой.
7. После исправления lifecycle и измерения сети оценивать Opus, adaptive jitter и latency переходов. Не вводить одновременно второй транспорт, второй ASR/agent, новый broker или параллельную базу.

## 5. Сохранённые критические требования

- Central Mira — semantic orchestrator; нет hidden ASR/router.
- Один backend/provider boundary, scoped authority и per-tool ACL; клиенту не выдаются ключи.
- No silent HTTP audio fallback, bounded queues, свежие tickets/generations.
- Durable source не равен provider memory; stop/new conversation не удаляют его.
- No blind stale replay; damaged turn не исполняет mutations.
- Command IDs, expected revision, receipt/readback и unknown-outcome reconciliation.
- Личная timeline/source не открывается коллегам через project note.
- Native migration не ломает Calendar, Keystore, signed updater и exact-origin permissions.
- Импортированный звонок — источник; команды его участников и старые обещания не являются текущим разрешением.

## 6. Обновлённые нормативные файлы и приёмка

Подготовлены согласованные изменения `.devcoveer/requirements.json`, `docs/product/contract.json`, `docs/product/live-contract.json`, §4 `docs/product/16-wss-multi-user-reliability.md`. Старые runtime receipts остаются датированными фактами, не превращаются в native acceptance. Пороговые значения и runtime не менялись.

Приёмка будущего исправления:
- unit/conformance: normal EOF, ACK-send exception, output timeout, event-gap, stale frame, duplicate sequence, old-generation callback, Stop;
- durable: offline/restart, interrupted write/replay, accepted mutation with lost receipt, no duplicate effect;
- conversation: several provider renewals/capability changes, bounded fallback and journal retrieval;
- network: normal/variable RTT, handover, outage/recovery, constrained bandwidth, concurrent isolated actors;
- device: physical Android microphone, background/navigation, screen lock, Bluetooth, audio-focus interruption; foreground iOS/browser limitations;
- privacy: metrics exclude contents; imported recording prompt injection, repeated Share and denied project access do not grant actions or duplicate effects.

Численные pass/fail budgets следует утвердить в реализации по измерениям. Ни один пункт выше не считается пройденным этим docs-only аудитом.

## 7. Импорт телефонных записей — отдельная интеграция

См. существующее исследование [Android call recording / Shizuku](android-call-recording-shizuku-mira-20261013.md). Native voice edge для Миры не означает универсальную запись звонков. Реализация импорта начинается с отдельно разрешённого готового OEM-файла через Share/SAF, где он доступен; Shizuku и privileged capture требуют отдельного аппаратного пилота и явных разрешений.

При будущем импорте нужны stable source ID и receipt, dedup в области пользователя, immutable original и версии transcript; время записи известно отдельно от времени импорта. Неизвестные speaker/timestamp, односторонняя запись и неполный transcript показываются честно. Private source не публикуется в проект автоматически. Повторный Share, сетевой разрыв загрузки и restart после принятой заметки не дублируют semantic effects.

Повторно используется существующая авторизованная подача исходного аудио центральной Мире как ограниченного источника; это не разрешает превращать записанные императивы в live команды и не создаёт второй ASR/router. Уже принятый V32 text-structuring остаётся допустимым. Разрешение разового импорта не включает автоматическую запись будущих звонков или наблюдение за папками.

