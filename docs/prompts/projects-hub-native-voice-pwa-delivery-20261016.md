# Projects Hub — нативный голос Android и полноценная PWA

Дата: 16 октября 2026. Постановка для Claude на следующую реализацию.

## Что нужно получить

Продолжи работу над Projects Hub и доведи новую голосовую схему до интегрированного продукта.
Нужна работающая версия приложения, а не отдельный стенд, исследование или набор тестов.
Короткая квалификация рисков входит в реализацию; после неё переходи к коду, интеграции и проверке выпуска.

В Android микрофон, воспроизведение, транспорт и жизненный цикл разговора должны работать нативно,
независимо от WebView. Сам WebView может остаться интерфейсом: переписывать весь UI на native не требуется.
Приоритет первого этапа — Android native: максимально используй его возможности, не ограничивай их
наименьшим общим знаменателем браузеров. PWA сохраняется как самостоятельный клиент без регрессий.
Устанавливаемая на iOS PWA — важный следующий этап, но её полная приёмка не блокирует первый Android-релиз.
Универсальность означает общие смысловые контракты и совместимый backend, а не одинаковые платформенные API.
Для iOS/PWA допустим собственный механизм capture/playback/lifecycle через общий контракт.
Общая цель — логический разговор не менее 90 минут, включая продления provider-сессии и восстановление.
Это цель первой Android-приёмки и общего session layer; браузерные профили проверяются отдельно.
Не обещай iOS PWA фоновую непрерывность на уровне Android или уже достигнутую надёжность.

Действуй самостоятельно в выборе минимального технического решения. Применяй бритву Оккама:
сначала используй существующий shared framework и продуктовые контракты, убирай дублирование,
не строй второй агент, gateway или систему обновлений. Не создавай параллельную инфраструктуру вместо
развития существующей; необходимые native capture/codec/transport доработки входят в задачу.
Не своди работу к механическому исполнению старого аудита: проверяй его выводы по текущему коду.

## Контекст и исходная точка

Перед изменениями проверь актуальные ветки, незавершённую работу, требования и фактический deployment.
На момент подготовки постановки main Projects Hub: `76bc5f2d84424f26bf3d0056687ae6a5dfcbde81`.
Обе зависимости в `pyproject.toml` и `web/package.json` указывают shared
`live-interaction` 0.3.28, commit `3e675077e30b06002fe6477d8cdd5fbae6c1331e`.
Это проверенная исходная точка репозитория, не утверждение о версии на каждом устройстве.

Прочитай нужные разделы, не начинай ещё один полный аудит:
- [Договор Android/PWA и continuity, §§2–4.4](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/docs/product/16-wss-multi-user-reliability.md).
- [Критический аудит и уточнения решений, особенно §§12–18](https://github.com/onedayonemasterpiece/projects-hub/blob/68ff7ebe61b148c522b0671db4a5b2371f8c6abe/docs/audits/voice-plan-critical-audit-20261015.md), [PR #174](https://github.com/onedayonemasterpiece/projects-hub/pull/174).
- [Независимый аудит](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/docs/audits/voice-reliability-independent-20261013.md).
- [Отдельная постановка по импорту звонков](https://github.com/onedayonemasterpiece/projects-hub/blob/docs/call-recording-claude-handoff-20261015/docs/audits/call-recording-claude-handoff-20261015.md).
- Текущие `.devcoveer/requirements.json`, `docs/product/contract.json`, `docs/product/live-contract.json`.

В аудите есть исторические числа, гипотезы и рекомендации, а не только обязательные решения.
Лимиты provider connection/context/resumption, форматы и браузерную поддержку проверь по актуальной
официальной документации и измерениям на выбранной модели. Не зашивай прежние числа как вечные константы.
Если защищённый реестр противоречит целевому сжатию или recovery, явно покажи точный конфликт,
подготовь минимальную правку и пройди установленный requirements-update path.
Не ослабляй требования и не считай этот файл обходом требуемого согласования.

## Границы общего кода

Канонический framework: [onedayonemasterpiece/live-interaction](https://github.com/onedayonemasterpiece/live-interaction).
Проверенные точки входа: `python/live_interaction/provider.py`, `node/sessions.mjs`,
`android/src/main/java/org/onedayonemasterpiece/live/LiveSocketTransport.java` и browser binding.
Наличие Android transport ещё не доказывает наличие готового native audio lifecycle.

В Projects Hub начни с `src/projects_hub/live_runtime.py`, `live_adapter.py`, `live_transcription.py`,
`store.py`; `web/src/App.tsx`, `offlineSources.ts`, `bufferedReplay.ts`, `serverRecovery.ts`;
`android/app/src/main/java/com/kenigevents/projectshub/MainActivity.java` и Android manifest.
Shared transport, codec framing/decoding, reconnect и общая interruption-механика принадлежат framework.
Продукт сохраняет права, domain tools, durable source integration, UI и Android permissions/service wiring.
Платформенные адаптеры и разные механизмы допустимы и ожидаемы; domain semantics, авторизация и recovery
не должны расходиться. Границу уточняй по существующему коду, не копируй общую реализацию в Projects Hub.

Изменения framework выпускай версионированно, с совместимостью и conformance-проверками.
Обнови обе consumer-зависимости и lockfiles согласованно, сохрани version + immutable SHA/digest evidence.
Смена shared версии требует приёмки именно Projects Hub; зелёный framework CI её не заменяет.

## Android и PWA

Android:
- Один native microphone foreground service обслуживает явно начатый пользователем разговор.
- Capture, VAD/admission, bounded queue, WSS, playback и recovery не зависят от живого JavaScript/WebView.
- Typed bridge передаёт команды start/stop и проекции состояния/transcript; UI не становится владельцем аудио.
- Не допускай одновременно browser/native capture, двух transport owners или параллельных provider-разговоров.
- Покажи реальный статус и Stop в приложении/уведомлении. Stop немедленно прекращает локальный захват.
- Сохрани уже записанный источник при Stop/ошибке; остановка не означает разрешение стереть его.
- Учти permission denial/revocation, audio focus, звонок, Bluetooth route, screen-off, background и возврат UI.
- Не обещай автоматический старт микрофона после reboot/force-stop вопреки правилам Android.
- Сохрани Keystore/device binding, Calendar, ACL, exact-origin bridge и действующий signed updater.

PWA сейчас и устанавливаемая iOS PWA следующим этапом:
- Сохрани обычный live-разговор, playback, interruptions, offline source, recovery, transcript и domain tools.
- Используй общий browser binding, без нового частного getUserMedia/WebSocket engine.
- Сейчас проведи smoke/regression текущей PWA на поддержанных desktop/mobile профилях и укажи точные версии.
- Отдельным следующим milestone проверь установленную на iOS PWA: установку, запуск, capture/playback,
  разрешения, сохранность источника, suspend/return и recovery на физическом iPhone с указанной iOS.
- Полная физическая Safari/iOS installed-PWA квалификация не является gate первого Android-релиза.
  При этом новые backend/wire/codec решения уже сейчас не должны ломать текущую PWA или закрывать этот путь.
- Зафиксируй поддержанные профили; неподдержанный codec/режим должен давать понятное состояние или согласованный fallback.
- Не выдавай service worker за гарантию фонового микрофона. Suspend/lock должен честно отражаться в UI.
- При возвращении вкладки восстанавливай логический разговор и сохранённые источники без повторных effects.
- Разные platform-specific механизмы допустимы, но смысл ACK, source recovery, прав и результата команды общий.

## Один контракт разговора и восстановления

До основной реализации опиши короткую машину состояний и versioned wire contract.
Это рабочая часть задачи, не отдельный многонедельный проект документации.
Раздели logical conversation, device capture, socket binding, provider session, durable source и semantic command.

Минимальные наблюдаемые стадии:
`captured/volatile → device-durable → relay-admitted → server-durable → provider-forwarded → turn-complete → semantic-outcome`.
Порядок и границы конкретных ACK уточни по реализации; ни одна стадия не подменяет другую.
Relay ACK не означает понимание моделью, сохранность после crash или выполнение инструмента.
Перед передачей provider соблюдай определённый durable barrier; обозначь сбойное окно volatile capture.

Временный разрыв:
- Продолжай сохранять собственную речь локально, показывай «сохраняю, доставлю после восстановления».
- Стабильные source/chunk ID, original timestamps, sequence, hashes и recovery epoch переживают retry/restart.
- После сети доставляй известные порции с проверяемым прогрессом и порядком, а не неизвестный хвост realtime queue.
- Различай исходное время речи и время доставки; live и отложенная речь не должны перемешиваться без политики.
- Повреждённый/неполный ход не получает права на mutation только потому, что соединение восстановлено.
- Устаревшее или отменённое поручение не исполняется автоматически; неоднозначное решение уточняет Мира.
- Unknown tool outcome сначала reconcile/readback, затем повтор только доказанно невыполненного шага.
- Command idempotency отделена от дедупликации байтов и попыток анализа.
- Не удаляй единственную локальную копию до подтверждённого durable результата/terminal disposition.

Сохрани LLM-семантику: смысл обращения, актуальность поручения и решение о действии не подменяются
списками разрешённых фраз, regex-командами или whitelist слов «да/нет/стоп».
Детерминированными остаются права, состояние, idempotency, resource bounds и safety fences.
Отмена пользователем и новая команда должны корректно менять судьбу отложенного материала.

Backend restart, provider renewal и capability transition не создают новый разговор для пользователя.
Нужны сохраняемый журнал/контекст, безопасная resumption при поддержке provider и восстановление без handle.
Проверь память о начале разговора после нескольких переходов, а не только последние короткие реплики.
Reconnect ограничивай частотой, backoff/jitter и recovery budget, не пожизненным числом попыток.
Предотвращай reconnect storm и бесконечные retries при auth/protocol failure.
Включи caption-sidecar в lifecycle/budget; его отказ не должен незаметно ломать основной разговор.
Определи bounded idle/dormant policy и сохранение начала речи при выходе из простоя.
Deployment/drain/restart policy должна учитывать активные разговоры, а не только успешный healthcheck.

## Сжатие и ограниченные ресурсы

Цель: сжатый клиентский transport и сжатое durable хранение; PCM на необходимой provider boundary.
Не хранить новые длинные разговоры как постоянную несжатую PCM-копию.
Общий decoder обслуживает provider, captions, recovery и будущий source analysis без независимых несовместимых путей.

Определи по измерениям пригодные Opus и AAC профили для Android и текущей PWA.
Сохрани negotiation/adapter путь для Safari; его полная физическая квалификация относится к iOS milestone.
Не ухудшай Android профиль ради обязательного совпадения codec API на всех платформах.
Codec и container/framing — разные решения: не объявляй fMP4 в целом непригодным.
Проверь latency chunk emission, init/fragment handling, decoder restart, playback и восстановление timeline.
Явно согласуй protocol/codec, sample rate, channels, framing и fallback; не меняй формат молча.
Не отдавай Opus/AAC прямо provider, если его текущий договор требует PCM.
Проверь также downlink; если нужен временный compatibility profile, назови его ограничение и оставшийся gate.

Ограничь encoded/decoded bytes, duration, queue, frame count, decode work, disk и CPU.
Защити decoder от повреждённых и чрезмерно дорогих payloads.
Сохраняй исходные сжатые байты и timeline; производные не заменяют original.
Legacy PCM должен оставаться читаемым; не делай разрушительную миграцию без разрешения.
Задай retention/cleanup и поведение при заполнении диска, не удаляя неполученный материал молча.
Выбор bitrate обоснуй качеством речи/имён/чисел, размером, задержкой и нагрузкой, а не только экономией.

## Перебивания и возобновление ответа

Сохрани возможность перебить Миру и начать говорить, включая новую реплику после восстановления.
Раздели platform AEC, detection кандидата на перебивание, смысловое подтверждение и playback cancellation.
Измеряй ложные срабатывания; шум/эхо/поддакивание не должны систематически обрывать полезный ответ.
Не заменяй смысловой слой списком фраз. При ложном перебивании нужен проверяемый resume/restart ответа.
При истинном перебивании не проигрывай устаревшие куски после reconnect.
Согласуй playback generation, фактически услышанный пользователем фрагмент и контекст агента.
Не обещай sample-perfect continuation, если provider её не поддерживает; выбери честный UX восстановления.
Проверь повторный start после Stop, смену аудиомаршрута и speech во время начала ответа.

## Импорт звонков и отдельные аппаратные проверки

Общая схема должна быть готова к импорту завершённой записи, но не превращай этот этап в форк рекордера.
Зафиксируй минимальный контракт: immutable private original, source ID/hash/provenance,
chunked analysis с durable cursor, исходные интервалы, версии transcript/analysis и coverage/gaps.
Импортированный разговор — source-only; на пути анализа технически нет mutation-инструментов.
Записанные слова участников не являются текущими поручениями, даже если это голос владельца.
Извлечённые задачи/договорённости остаются кандидатами до отдельного актуального принятия.
Короткий разрешённый fixture может подтвердить границу; реальные записи требуют разрешений на передачу.
Не заявляй speaker identity, точные таймкоды или полноту без evidence.

Прямая запись двух сторон телефонного звонка, Shizuku/OEM и автомобильное эхо требуют отдельных физических пилотов.
Они не блокируют основной выпуск native voice + PWA, если их неподтверждённые возможности не обещаются.
Проверка обычного Android микрофона, background и screen-off при этом остаётся обязательной для такого заявления.

## Как работать и что считать готовностью

1. Коротко сверь код/контракты, проверь самые рискованные codec/recovery/lifecycle предположения.
   Зафиксируй измеримые пороги latency, recovery window, offline capacity, потерь, battery/traffic и budget.
   90 минут уже заданы как цель; остальные пороги обоснуй baseline и пометь неутверждённые продуктовые trade-offs.
2. Реализуй Android-first вертикаль в shared framework и Projects Hub; согласованно обнови текущую PWA
   без регрессий. Отдельные платформенные адаптеры предпочтительнее искусственно одинаковой реализации.
   Устраняй выявленные дефекты до интеграционного результата; не заканчивай на отдельном harness.
3. Проведи regression, conformance и end-to-end проверки на точных сборках.
   Включи сеть с latency/loss, Wi-Fi↔mobile, offline speech, provider renewal, backend restart,
   interrupted playback, unknown tool result, multi-user isolation и восстановление источника.
4. Проведи 90-минутный Android-разговор с несколькими renewal/recovery, idle/wake и воспоминанием ранних фактов.
   Сверь принятые durable chunks, timeline/coverage и результаты команд: нет потерянной сохранённой речи
   и повторных business effects. Промежутки, когда OS не позволяла capture, видимы, не скрыты словом PASS.
5. Проверь настоящий Android handset: screen-off, background/навигатор, возврат WebView,
   permission/audio-focus changes, route change и signed update. Emulator/prepared PCM не заменяют handset.
   Для текущей PWA приложи smoke/regression и честные границы background. Полную приёмку установленной
   iOS PWA выдели следующим milestone с конкретными сценариями; не задерживай ею первый Android-релиз.
6. Подготовь совместимый выпуск framework, backend/PWA и signed APK через существующий release path.
   При наличии действующих полномочий доведи deployment и post-release smoke до verified результата.
   Если merge/release или устройство требуют отдельного доступа/разрешения, запроси только конкретное недостающее,
   продолжая независимую работу. Не обозначай ожидающий gate как выполненный релиз.

Эта постановка описывает следующий результат реализации; её публикация сама по себе не запускает код,
не поручает внешнему агенту работу и не выдаёт новых прав на production, merge или реальные записи.
Используй действующие полномочия задачи реализации и ограничения репозиториев.

## Итоговый отчёт

Дай короткий продуктовый итог и ссылки:
- Что реально работает в Android и текущей PWA; отдельно следующий iOS installed-PWA milestone,
  платформенные ограничения и непроверенные возможности. Не смешивай готовность этих этапов.
- PR/commits, shared release version, backend/PWA deployment SHA и APK/version/checksum.
- Воспроизводимые команды и evidence: CI, conformance, 90 минут, handset, браузеры и post-release smoke.
- Измеренные задержки, reconnect/recovery, traffic/storage, CPU/battery и ошибки на описанных условиях.
- Migration/rollback, судьба старых источников и точные оставшиеся blockers с нужным действием владельца.

Отчёт «тесты зелёные» без новой интегрированной сборки не закрывает задачу.
Если физическая приёмка недоступна, честно выдай release candidate и конкретный незакрытый gate,
а не объявляй продукт полностью проверенным.
