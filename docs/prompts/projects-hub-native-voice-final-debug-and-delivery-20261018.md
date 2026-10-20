# Projects Hub — довести нативное голосовое взаимодействие Миры до продуктового результата

> **Исторический снимок 18.10.2026, НЕ текущая точка продолжения.** Актуальный handoff на 20 октября с проверенными релизами, требованиями, Samsung/Slack RCA и незакрытыми P0: [`projects-hub-native-voice-canonical-handoff-20261020.md`](projects-hub-native-voice-canonical-handoff-20261020.md). Старые инструкции о v72/rc3 оставлены ниже для истории, не запускать по ним реализацию без свежей сверки.

**Актуализированная постановка и точка продолжения для нового окна ChatGPT**

Дата снимка: **18 октября 2026**. Владелец разрешил продолжать реализацию и отладку.
**Статус: НЕ ЗАВЕРШЕНО.** Серверная часть одного инцидента исправлена и установлена; новая Android-сборка с исправлением аудиомаршрута НЕ выпущена и НЕ проверена на физическом телефоне. Не выдавать успешный GitHub CI или управляемый emulator fixture за продуктовую приёмку.

## 0. Поручение исполнителю

Ты — основной технический исполнитель, а не автор очередного плана. **Найди и продолжи существующие исходники, ветки Claude Code и уже проделанную отладку; не переписывай проект, не создавай альтернативный backend или новый тестовый фреймворк.** Используй штатные GitHub и Codex DevCoveer MCP-инструменты самостоятельно; OpenCode с иным доступным provider/model или Codex допустим как помощник, но его доступность не должна блокировать прямые операции. Не проси владельца снова проверять каждую мелочь на Samsung. Доведи до проверенного **подписанного Android RC с совпадающим сервером**, затем попроси **одну** содержательную проверку на физическом телефоне. Если окружение/права/физическое оборудование объективно не позволяют закрыть конкретный критерий, честно укажи это и сохрани воспроизводимые доказательства.

**Сначала read-only сверка** актуальных remote main, PR, worktree, deployed SHA, SDK release, CI, связанных инцидентов. Этот документ — checkpoint, а не разрешение слепо откатывать более свежую параллельную работу. Если во время продолжения commits/production изменились, интегрируй без уничтожения чужих изменений.

## 1. Исходная проблема — реальное воспроизведение на Samsung

Пользователь установил подписанный [Projects Hub Android v72](https://github.com/onedayonemasterpiece/projects-hub/releases/tag/android-v72), versionName `0.1.62-rc.72`, versionCode `72`. При разговоре около **17:31–17:32, UTC+02:00, 18 октября**:
- видна реплика «Мира, привет» и текст ответов модели, но пользователь **не слышит голос**;
- на вопрос «Расскажи мне, что свежего, какой бриф?» появляется «Жду ответ Миры…», разговор подвисает;
- ранее были сообщения о сбое записи, «Записываю на устройстве», «Подключаюсь…», запоздалые и неполные субтитры;
- пользователь уже неоднократно вручную выявлял ошибки, которые должны выявлять автоматические проверки. **Продуктово это отказ**, даже при зелёном health и unit tests.

**Сверенные server logs, 15:30:48–15:33:03 UTC 18 октября:** Live-соединение и входная речь работали; первый ответ провайдера содержал **12 audio events / 190080 байт PCM**, но подтверждение фактического воспроизведения на телефоне отсутствовало. При переходе к `collaboration` полная reconfigure setup обходилась примерно в **31 тыс. расчётных единиц UTF-8 JSON**, далее повторные `RESOURCE_TOKEN_BUDGET` отказы `tool_response` (запрос ~5228 ед., повторы каждые ~3 с) оставляли интерфейс в ожидании. Здоровый HTTP ресурсоучёта не означает выданный лимит.

Корневую причину молчания телефона **ещё НЕ доказали**. Обоснованная гипотеза: `AudioTrack` с `USAGE_VOICE_COMMUNICATION` и `MODE_IN_COMMUNICATION` на Samsung направляет аудио в тихий разговорный динамик вместо громкого. Возможны также ошибки WSS/downlink, декодера, аудиофокуса, playhead и обработки Stop. Требуется проверка источника, сохранения, доставки, `AudioTrack` и выбранного Android audio route раздельно.

Основной DevCoveer-инцидент: **`inc_a6d3286ccf0620e09f450bdb`** (sev2, **OPEN**). Связанные незакрытые инциденты: `inc_f33903eeb0eeb826ed7c7a84` (длинные ответы и playback), `inc_66519d591551a6b378dabd44` (resource budget/переключение), `inc_aae28418343acf9cc9ad3812` (потери ASR). Прочитай timeline перед изменениями и дополняй наблюдения; не закрывай до проверки на устройстве.

## 2. Точные состояния и уже выполненное

### 2.1 Projects Hub — backend/PWA (уже в production)

- Репозиторий: [onedayonemasterpiece/projects-hub](https://github.com/onedayonemasterpiece/projects-hub).
- На момент снимка **`origin/main = 7a45193661ca879deced51ee404c2b180d53c368`**.
- Production **`/healthz` HTTP 200**, `release_sha=7a45193661ca879deced51ee404c2b180d53c368`, version `0.1.62`, сервис `projects-hub.service` active, `NRestarts=0`; установлен **штатным** `deploy/devcoveer_install.py --sha ... --preserve-releases`. Сохранены предыдущие релизы.
- [PR #190](https://github.com/onedayonemasterpiece/projects-hub/pull/190) **MERGED**, source `d00c542c84b528a68a3980b8254d47696e37dccf`, merge `7a451936...`.
- Исправлено: при доступной авторизованной `collaboration` инструмент `collaboration_personal_brief` доступен в core и исполняется **напрямую**, без дорогого reconfigure. Соблюдён лимит **9 инструментов**: требующий явного owner grant `development_execute_backlog` по мере необходимости загружается через `owner_development`; при одновременном брифе и ожидающих вопросах реже используемый `runtime_versions_get` освобождает слот. Server authorization проверяет фактически выставленный список функций; самостоятельного расширения прав нет.
- Добавлены privacy-safe лог-события: `live_output_durable` (first/terminal chunk сохранён в downlink store) и `live_output_playhead` (позиция, **сообщённая устройством**). **Playhead ≠ пользователь действительно услышал звук.** Не писать в логи аудио, речь, токены и ключи.
- [GitHub CI backend/PWA](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39143432486) **PASS**. [Android job на том же product SHA](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39143432473): первая попытка **FAIL**, повтор **6/6 PASS** (подробнее ниже).
- Postdeploy canary `job_83c8d420af8ffa390c626b67`: реальный Google WSS `wl-live-v1`, hello, binary PCM ACK, input/output transcript, 6 binary audio frames, `turn_complete`, Stop — **PASS**. **Это не доказательство работы wl-live-v2 на физическом Android и не проверка слышимости.**

Операционная рабочая копия, по которой велась диагностика: `/home/dev/projects/projects-hub-native-pairing-20261017`. В ней после merge исходная ветка `chatgpt/phone-live-budget-and-playback-20261018` (`d00c542...`); есть **старые untracked native-canary-build-14a2e8/**, НЕ удалять/не коммитить автоматически. Исходный native продукт создан Claude и продолжен в [PR #175](https://github.com/onedayonemasterpiece/projects-hub/pull/175) (MERGED). Не заменять его новым проектом.

### 2.2 Shared live-interaction — исправление динамика слито, но **не установлено в клиент**

- Репозиторий: [onedayonemasterpiece/live-interaction](https://github.com/onedayonemasterpiece/live-interaction).
- Текущий опубликованный/установленный в Android v72 SDK — **`0.4.0-rc.2`**, shared source `b8b4ba15cdf1b4cfafc428eb3adec02cb44d92d2`; в Android закреплён через `android/live-interaction-candidate.json` и проверенный архив.
- Исправление Android route: [shared PR #53](https://github.com/onedayonemasterpiece/live-interaction/pull/53), source **`6baafc071a4fa90267d3f43f4af7c422dc0d8786`**, **MERGED** в shared main **`736763a08e66385795607950c5447b5146f30e23`**. Native/Python/Node CI source `6baafc...` **PASS** ([run](https://github.com/onedayonemasterpiece/live-interaction/actions/runs/39158637859)).
- Код `0.4.0-rc.3`: native `CommunicationRoutePolicy`, Android 12+ `setCommunicationDevice`, legacy `setSpeakerphoneOn`, не перехватывать Bluetooth/wired/USB, возвращать исходный маршрут на Stop, не сбрасывать запись при ошибке route, логировать только маршрут/тип устройства (`LiveNativeAudioRoute` и `native_audio_route`). Unit tests на правила маршрутизации. [Описание](https://github.com/onedayonemasterpiece/live-interaction/blob/6baafc071a4fa90267d3f43f4af7c422dc0d8786/docs/native-speaker-route-20261018.md).
- Версия в `package.json`, `pyproject.toml`, `python/live_interaction/__init__.py` синхронизирована на **0.4.0-rc.3**. Ранее на source `cc6efdac` CI падал из-за несинхронной версии Python; это **исправлено** в `6baafc`, и последующий CI зелёный. Не начинать повторное исправление уже исправленного version mismatch.
- Рабочая копия DevCoveer: `/home/dev/projects/live-interaction-queue-budget`, ветка `chatgpt/native-voice-speaker-and-eos-20261018`, HEAD `6baafc...`, tracked clean, но есть не относящиеся к этому untracked `ops-*.sh`. Другая исходная Claude worktree `/home/dev/worktrees/live-interaction-continuity-20261016` может быть старее — **не перезаписывать** без сравнения.
- **На момент снимка НЕ ПОДТВЕРЖДЕНЫ:** существование immutable GitHub SDK release/tarball `v0.4.0-rc.3`, его checksum, **обновление pin consumer**, создание нового подписанного APK с этим SDK, установка на Samsung. Проверить до объявления готовности. Коммит/слияние SDK **не обновляет уже установленное приложение**.

### 2.3 Нестабильный Stop / codec EOS — отдельный открытый дефект

На **одинаковом product SHA `d00c542...`** эмулятор в [Actions run 39143432473](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39143432473) сначала выполнил 5/6 тестов, но шестой `NativeVoiceFlowTest.actualUiVoiceCapturePlaybackOutageRecoveryAndStop` остановился с **`LIVE_CODEC_CAPTURE_SPAN_MISMATCH`** после нажатия Stop. Оба speech fixtures дошли до сервера, обе compressed ответы прошли AudioTrack; ошибка возникла при закрытии длинного input source. Повтор на **том же исходном SHA**: 6/6 **PASS**. Это **intermittent defect, НЕ исправлен повторным тестом**.

Точки чтения:
- `live-interaction/android/platform/src/main/java/org/onedayonemasterpiece/live/platform/NativeEncoder.java`: `finish()`, `drain()`, `emit()`, `validSamples` / `capturedSamples`, trim.
- `.../CodecTimeline.java`: `advance()`, sample clock/preskip, terminal.
- `.../NativeLiveRuntime.java`: `capture()`, `encode()`, `finishSource()`, `stop()`, bounded PCM queue, rotation, thread lifecycle.
- Product `android/app/src/androidTest/.../NativeVoiceFlowTest.java`, `scripts/android_emulator_smoke.sh`.

Нельзя снижать waveform-критерии, скрывать ошибки `CAPTURE_SPAN_MISMATCH`, объявлять silent data loss успешным Stop, выполнять повторное смысловое действие после recovery или просто увеличивать RAM/очереди без измерений. По необходимости добавь детерминированный тест гонки/EOS с точной length/timing telemetry. Сохраняй originals и разделяй `durable`, `delivered`, `decoded`, `played`.

## 3. Что конкретно надо довести сейчас — приоритет

**P0 — закончить голосовую поставку, не масштабировать архитектуру:**

1. **Восстановить точную исходную работу и provenance:** GitHub refs, physical worktrees, PR #175/#190/#53, deployed `/healthz`, текущие SDK releases. Прочитать `AGENTS.md` и защищённые `.devcoveer/requirements.json`; сохранить параллельные изменения. Не делать `git reset --hard`/массовую очистку.
2. **SDK RC3 как настоящий immutable artifact:** проверить, есть ли уже `v0.4.0-rc.3` с пакетом и checksum. Если нет — произвести по штатному release workflow/scripts после контрольных проверок; не создавать вторую библиотеку. Не переиспользовать rc2 под именем rc3. Проверять не только version string, но и SHA/содержимое архива.
3. **Repin Projects Hub** на проверенный SDK (Android platform, Python и web — только если затронуты, согласованно с manifest/lockfiles). Убедиться, что Android действительно включает `CommunicationRoutePolicy`; сервер остаётся совместимым с v2. Если `main` уже продвинулся — сначала интегрировать его. Доставить новый кандидат **без автоматического обновления stable-канала**.
4. **Закрыть flaky input EOS** по воспроизводимому тесту, желательно до выпуска: причину определить по persisted trace, поправить в **shared framework**, а не копировать исходник в product. Несколько полных прогонов Android на *точной паре* pin/source (включая Stop после продолжительного capture, barge-in, outage/reconnect, повтор запуска). Никакого PASS от иной версии. Если не удаётся воспроизвести/устранить, оставить RC blocked/known open, не заявлять полной надёжности.
5. **Подтвердить весь звуковой путь**: реальный Android `AudioRecord → native encode → binary WSS → durable server → Live provider → audio source/downlink → native decode → AudioTrack → route + playback position`. Эмулятор: известные речевые входы, запись звукового выхода, строгая корреляция waveform, outage, Stop; его контролируемый AI runner **не равен** реальному провайдеру. Реальный провайдер: простое приветствие, короткий вопрос, read-only `collaboration_personal_brief` **без ненужного `activate_capability`/31k setup**, инструмент с 9-секундным ожиданием и промежуточным голосовым статусом, честная ошибка budget без бесконечного «Жду…», немедленный Stop.
6. **Проверить физическую маршрутизацию**: на Samsung с динамиком/гарнитурой; route type и решение выбора логируются; если провайдер сгенерировал звук, но нет playhead, это другой класс отказа, чем playhead есть, но звук неслышен. API31 эмулятор сам по себе не доказывает физическую Samsung-route.
7. **Опубликовать подписанный RC** через штатный `android-release.yml`: только при предъявлении Android evidence `source_sha` + `android_run_id`, сохранении production package `com.kenigevents.projectshub`, production signing certificate и повышении `versionCode` **выше 72**. Verify-reference `aapt/apksigner`; вновь скачать и сверить APK, evidence/manifest, SHA256; сделать ссылку владельцу. Не ставить RC в stable auto-updater до физической приёмки.
8. **Довести сервер, если изменения требуются:** `deploy/devcoveer_install.py --sha <verified-main-commit> --preserve-releases` (не mutable working tree), перед переключением проверить CI/хранилище/параллельную работу, после — `/healthz`, public static assets и живой canary. Сохранять backward compatible durable audio; rollback может быть ограничен новым v2 schema/source — не возвращать на старый сервер, который неверно воспринимает encoded blobs как PCM.
9. **Только после автоматической проверки** попросить владельца об одной понятной проверке «Мира, привет; что нового; послушать ответ; Stop» и при необходимости маршрута на Samsung. Если есть удалённый физический device/ADB — используй напрямую. В противном случае честно отмечай последнюю физическую границу и не присваивай статус finished без подтверждения.

**P1 — после P0:** устранить проблемы полноты main ASR (A1/A2: первые слова теряются при успешно записанном и отправленном аудио); дополнительную привязку/тайминг captions (не смешивать interim с authoritative final); 90 минут с renew/reconnect/background и переходами Wi-Fi↔LTE на плохом интернете; совместную измеренную нагрузку **4 одновременно говорящих пользователя** + Live + captions + RAG/Regional Knowledge, p95/p99, RSS/CPU/disk/latency, без изменения политики квот. Анализ голосовых заметок из [my-data-hub PR #48](https://github.com/onedayonemasterpiece/my-data-hub/pull/48) — сохранить существующий pipeline, finish request → owner questions → answers → final document в отдельных ответственных компонентах.

## 4. Неприкосновенные продуктовые решения владельца

- Центральная Мира — **единственный semantic orchestrator**. Не вводить скрытый дополнительный LLM/ASR/router. Прогрессивные bundles и разрешения обязательны; повторный дорогой provider setup ради простой read-only операции — антипаттерн.
- Между **телефоном и сервером обязательно сжатие в обе стороны**; при нестабильном интернете — локально durable capture, recovery отдельно от свежей речи, без двойного исполнения, Stop/новая речь приоритетнее backlog. Не скрывать offline как online.
- Нужны **слышимые короткие промежуточные ответы** при долгом function call, но без спама; нельзя давать ложное подтверждение успешной операции.
- Сервис на одном DevCoveer разделяет RAM/CPU/disks с несколькими проектами. **Пик для первого нагрузочного теста — 4 одновременно говорящих пользователя**; это не уже измеренная ёмкость. Нельзя ради идеальности бесконтрольно поднимать тяжёлые процессы и заполнять диск.
- **Полный переход на Go/Rust не согласован как реализация**: только обоснованное сопоставление при смешанной нагрузке и реальных bottlenecks. Нативные кодеки уже ускоряют CPU-heavy обработку; не переписывать весь backend на языке гипотезы во время P0. См. [оценку](https://github.com/onedayonemasterpiece/projects-hub/blob/955b2ee300cad02da5e3b3d3a43395e0bc0b200f/docs/research/projects-hub-go-rust-assessment-20261018.md).
- Не изменять ключи/provider, не обходить `RESOURCE_TOKEN_BUDGET`, не повышать квоты ради зелёного теста, не раскрывать пользовательское аудио/текст/PII в логах, не ломать доступность других продуктов.
- **Gradle** — локальные build/unit tests; полный Android audio scenario нуждается в эмуляторе с KVM или устройстве. Повторяемый [Android workflow](https://github.com/onedayonemasterpiece/projects-hub/blob/7a45193661ca879deced51ee404c2b180d53c368/.github/workflows/android.yml) и `scripts/android_emulator_smoke.sh` — существующий harness, **не создавать новый**. DevCoveer без подтверждённого KVM не тянуть в тяжёлый emulator; Kaggle не добавлять без доказанной необходимости. Сохранять треки, screenshots, hashes и CI receipts.
- Долговечность и авторизация — критические защищённые требования. Любое принципиальное изменение поведения/UX не из принятой постановки сформулировать **одним верхнеуровневым вопросом** владельцу, а не остановить всю разрешённую отладку.

## 5. Отсылки к исходной работе и управляемому сценарию

- [Изначальная постановка Claude/native voice от 16.10](https://github.com/onedayonemasterpiece/projects-hub/blob/docs/native-voice-pwa-delivery-20261016/docs/prompts/projects-hub-native-voice-pwa-delivery-20261016.md) — отдельная source branch, может отсутствовать в `main`, не утверждать обратного.
- [Единая voice/product delivery](https://github.com/onedayonemasterpiece/projects-hub/blob/7a45193661ca879deced51ee404c2b180d53c368/docs/operations/voice-delivery-integration-20261018.md).
- [Итоговая подписанная Android v72 поставка](https://github.com/onedayonemasterpiece/projects-hub/blob/c02d2188a8b7b6caa710d50aca00871af663b3e6/docs/operations/native-voice-rc-delivery-20261018.md).
- [Результаты старого Android qualified PASS 39132523245](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39132523245); **не заменяют** новый SDK/кандидат.
- [Флаки Android CI — run 39143432473](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39143432473), first FAIL `CAPTURE_SPAN_MISMATCH`, rerun PASS. Диагностика и `voice-flow.json` в evidence artifact.
- [Server PR #190](https://github.com/onedayonemasterpiece/projects-hub/pull/190), [SDK PR #53](https://github.com/onedayonemasterpiece/live-interaction/pull/53), [SDK successful CI 39158637859](https://github.com/onedayonemasterpiece/live-interaction/actions/runs/39158637859).
- [my-data-hub voice-analysis release document](https://github.com/onedayonemasterpiece/my-data-hub/blob/a26797759fe30718fc6a12206c344968dcdc80a9/docs/operations/voice-analysis-release-20261017.md) и [PR #48](https://github.com/onedayonemasterpiece/my-data-hub/pull/48).
- [Общий framework integration contract](https://github.com/onedayonemasterpiece/live-interaction/blob/6baafc071a4fa90267d3f43f4af7c422dc0d8786/docs/integration.md) и repo `AGENTS.md`.

## 6. Доказательство результата / сдача

Подготовь **одну конкретную сдачу** с: ссылками на source PR и immutable SDK artifact, SHA и release versions обеих частей, GitHub Android audio run (вход/выход, outages, Stop, repeated attempts), backend+PWA tests, реальный provider сценарий без ресурсного зависания, exact deployment `/healthz`, подписанный APK link + SHA-256 + прежний cert, screenshots/logs route, перечень **непрошедших** критериев. Документировать без самолюбования и лишней церемонии. «Code merged», «health green», «Gemini сгенерировал PCM» или «эмуль успешно произнёс fixture» отдельно **не равны** «Мира слышно отвечает на реальном Samsung».

**Финальное условие P0:** по нажатию пользователь действительно разговаривает с Мирой, слышит ответ в правильном аудиоустройстве, не зависает на простом брифе, может перебивать и нажимать Stop без потери уже сохранённой речи и ложного повторного действия. Если это ещё не доказано — продолжить или явно оставить открытый физический gate, не объявлять продукт окончательно готовым.
