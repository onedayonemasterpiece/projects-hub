# Эффективность голосового пути: сжатие, очереди и гипотеза Go/Rust

Дата: 17 октября 2026. Статус: исследование для продолжения существующей работы Claude Code; перенос языка не реализован и не выбран как обязательный шаг.

## Решение по имеющимся данным

Продолжать общий `live-interaction` и начатый native/mobile v2: обязательное аудиосжатие в обе стороны, сохранение сжатого источника, ограниченные очереди, подтверждённая доставка и приоритет свежей речи. Для первого сжатого пути использовать существующий Python host с нативным кодеком; конкретный поддерживаемый профиль квалифицировать на телефоне и с провайдером. Выбирать Go или Rust для дополнительного переноса после измерения остаточного ограничения всего пути.

Это инженерная рекомендация, а не доказательство превосходства Python. Проверенные base64/JSON функции занимают около **0,95 мс CPU на секунду входного аудио** в коротком локальном тесте. Для их ускорения не требуется переписывать управление сессией. Синхронное сохранение каждого фрагмента перед ACK, последовательное ожидание ACK, второй provider-поток субтитров и лишняя передача серверного источника через телефон имеют более существенные архитектурные последствия. Их фактический вклад в задержки и вместимость рабочего VPS ещё не измерен.

Владелец 17 октября явно согласовал изменение требования аудиопрофиля и добавил обязательную работу на нестабильном мобильном интернете. Формальный `requirements_update` и commit guard пройдены; [коммит 4932150](https://github.com/onedayonemasterpiece/projects-hub/commit/493215046cd75d0f267d612f2277e81846204d1d) добавлен в исходную Claude-ветку. Точная редакция и receipts находятся в [решении об аудиопрофиле](../../decisions/native-voice-audio-profile-proposal-20261017.md). Совместимый `wl-live-v1` сохраняется; он не засчитывается как выполнение нового требования сжатого мобильного пути.

## Границы и доказательства

Проверено именно продолжение вчерашней работы:

- shared framework: ветка `claude/voice-continuity-v0.4-20261016`, исходный Claude commit `599dc1c`, продолжение исходников `3b85224962c396e1a52977d6b81eee659db0cacc`, [PR #51](https://github.com/onedayonemasterpiece/live-interaction/pull/51);
- Projects Hub: ветка `claude/voice-continuity-consumer-20261016`, исходный Claude commit `87824b7`, продолжение исходников `4a4d658cc5209429c7ed584ed0c1a8b51700743d`, документация на момент исследования `ffa3b508`, [PR #175](https://github.com/onedayonemasterpiece/projects-hub/pull/175);
- [исходная постановка Android-first / native voice](https://github.com/onedayonemasterpiece/projects-hub/blob/docs/native-voice-pwa-delivery-20261016/docs/prompts/projects-hub-native-voice-pwa-delivery-20261016.md), приложенный владельцем Claude transcript и межпроектный отчёт о Go.

Указанные candidate commits не объявляются действующим production. Локальные пробы ниже не вызывают провайдера, не читают частные записи, не создают нагрузку на рабочий сервис и не доказывают физическую работу микрофона. Версии среды проб отличаются от среды рабочего VPS.

| Вид вывода | Что установлено |
|---|---|
| Наблюдение по исходникам | Default WSS передаёт binary PCM; внутри backend есть base64/NDJSON; durable append синхронный; caption — отдельная provider session |
| Измерение | Затраты выбранных существующих функций и нативного libopus на синтетических данных в локальном контейнере |
| Расчёт | Объёмы PCM, размер base64, условный предел stop-and-wait ACK и отношение целевых bitrate |
| Гипотеза для следующей проверки | Вклад синхронного I/O, ACK и scheduling в p95/p99 под многопользовательской нагрузкой |
| Не установлено | Число пользователей на VPS, экономия денег, качество русской речи после Opus, батарея телефона, победитель Go против Rust |

## 1. Где на самом деле преобразуется аудио

### Телефон → сервер → провайдер

Обычный Projects Hub явно выбирает `transport: 'wss'`. В телефонном WSS-пути base64 нет: PCM16 mono 16 кГц передаётся бинарным кадром с 12-байтовым заголовком. Android consumer пока использует WebView; имеющийся Java transport общего framework ещё не подключён как полный native capture/playback путь. [Product defaults](https://github.com/onedayonemasterpiece/projects-hub/blob/4a4d658cc5209429c7ed584ed0c1a8b51700743d/web/src/App.tsx#L1085-L1102)

В backend цепочка следующая:

1. Shared relay получает binary PCM и кодирует его в base64 для существующего `binding.input`.
2. Product adapter немедленно декодирует строку обратно в PCM для хранилища.
3. `append_audio` под общей блокировкой открывает файл, дописывает PCM, делает `fsync`, затем SQLite UPDATE/SELECT. SQLite использует WAL и `synchronous=FULL`. Это синхронный вызов внутри async input, до provider queue и relay ACK.
4. Shared host сериализует объект с base64 в NDJSON, кладёт bytes во внутрипроцессную очередь; provider снова читает, декодирует UTF-8 и разбирает JSON.
5. Provider создаёт JSON сообщения Google с `realtimeInput.audio.data`. На этой последней границе base64 следует из используемого JSON-протокола.
6. Caption sidecar получает тот же вход, после чего relay возвращает ACK.

Это не отдельный subprocess между Projects Hub host и provider: NDJSON преобразования выполняются между корутинами одного процесса. Значит, общий framework может иметь путь передачи объекта/bytes внутри процесса, сохранив внешний NDJSON-интерфейс там, где он действительно нужен. [Relay](https://github.com/onedayonemasterpiece/live-interaction/blob/3b85224962c396e1a52977d6b81eee659db0cacc/python/live_interaction/socket_transport.py#L153-L164), [adapter](https://github.com/onedayonemasterpiece/projects-hub/blob/4a4d658cc5209429c7ed584ed0c1a8b51700743d/src/projects_hub/live_adapter.py#L1859-L1895), [durable append](https://github.com/onedayonemasterpiece/projects-hub/blob/4a4d658cc5209429c7ed584ed0c1a8b51700743d/src/projects_hub/store.py#L3167-L3187), [queue reader](https://github.com/onedayonemasterpiece/live-interaction/blob/3b85224962c396e1a52977d6b81eee659db0cacc/python/live_interaction/session_host.py#L36-L60)

### Субтитры

Для обычной realtime session код пытается открыть ещё одну Live session — `gemini-3.5-transcribe-live` — с отдельным resource lease и соединением. Она выдаёт текст, а не вторую озвучку. Primary настроен на VERBATIM, captions — на SMART; caption failure не должен останавливать основную Миру. Отдельного product flag для отключения captions в проверенном обычном realtime пути нет. Buffered/recovery-only сессии sidecar не создают. [Runtime](https://github.com/onedayonemasterpiece/projects-hub/blob/4a4d658cc5209429c7ed584ed0c1a8b51700743d/src/projects_hub/live_runtime.py#L136-L177)

Caption повторно декодирует base64, копит PCM в `bytearray`, режет его по 3200 байт / 100 мс, снова кодирует base64 и проходит собственные NDJSON parse и provider JSON dump. Телефон отправил звук один раз; дублирование трафика и модельной сессии начинается на backend. Устранение одних Python-копий не устраняет эту вторую provider session. [Caption slicing](https://github.com/onedayonemasterpiece/projects-hub/blob/4a4d658cc5209429c7ed584ed0c1a8b51700743d/src/projects_hub/live_transcription.py#L170-L215)

Быстрый растущий transcript уже является заданной продуктовой целью: первый model-derived фрагмент во время разборчивой непрерывной речи не позднее 5 секунд, p95 доставки полученного backend event до DOM не более 750 мс. Поэтому удаление sidecar допустимо после доказательства, что основной поток выполняет эти цели; заново спрашивать, нужны ли пользователю промежуточные субтитры, не требуется. Это цели приёмки продукта, не гарантированный SLA Google. [Прежняя постановка, §5](https://github.com/onedayonemasterpiece/projects-hub/blob/4a4d658cc5209429c7ed584ed0c1a8b51700743d/docs/prompts/voice-long-input-fix-20261005.md)

### Провайдер → телефон

Google JSON содержит base64 PCM. Shared host хранит audio event и при WSS encoding декодирует его в binary PCM; телефон преобразует Int16 samples во Float32 для воспроизведения. На участке сервер → телефон base64 также нет. Ring ограничен 320 events, исходящая очередь — 256 KiB; это разные ограничения. Лимит числа событий не даёт точного лимита аудиобайт без размеров events. Shallow dict copies не означают, что на каждом шаге копируется вся base64 строка. Output audio на серверный PCM-диск не пишется. [Output encoding](https://github.com/onedayonemasterpiece/live-interaction/blob/3b85224962c396e1a52977d6b81eee659db0cacc/python/live_interaction/socket_transport.py#L71-L150), [playback](https://github.com/onedayonemasterpiece/live-interaction/blob/3b85224962c396e1a52977d6b81eee659db0cacc/browser/client.js#L86-L117)

## 2. Расход, который стоит убрать прежде переноса языка

### ACK и сохранение нужно исправлять вместе

Browser sender ждёт завершения `send`, а WSS send завершается после ACK. Следующий batch не отправляется, пока предыдущий ждёт подтверждения. Target batch — 40 мс, фактический batch может увеличиться из-за очереди; максимум PCM — 11000 байт / 343,75 мс. Не следует переносить сюда параметры Java transport, у которого другая ACK window: продукт его пока не использует. [Sender pump](https://github.com/onedayonemasterpiece/live-interaction/blob/3b85224962c396e1a52977d6b81eee659db0cacc/browser/live-audio.js#L90-L125)

Условный потолок такой передачи — размер максимального batch, делённый на полный ACK round trip. При 500 мс получается 11000 / 0,5 = **22000 байт/с**, что ниже поступающих 32000 байт/с PCM. Это расчёт ограничителя протокола, не измеренный инцидент. Добавление durable I/O увеличивает время подтверждения; смена Python на Go при сохранении stop-and-wait не устраняет зависимость от RTT.

Направление исправления: ограниченное окно нескольких порций, подтверждения непрерывного committed prefix, управляемый writer и допустимая группировка durable commit. Нельзя получить ускорение, преждевременно объявляя данные сохранёнными или убирая подтверждённую сохранность. Синхронный I/O в event loop способен задерживать другие сессии даже при невысоком CPU; официальная [документация asyncio](https://docs.python.org/3/library/asyncio-dev.html#running-blocking-code) описывает необходимость отделять блокирующую работу.

### Online durability и лишняя доставка через телефон

Обычный online client не передаёт `persistAudio`; IndexedDB существует в отдельной offline capture ветке. В ней `batchMs=256` не объединяет записи в IndexedDB: `persist` вызывается на fragments до send batching. Native durable compressed spool должен стать частью непрерывного capture lifecycle, а не включаться лишь после потери уже не сохранённого online audio. [Offline setup](https://github.com/onedayonemasterpiece/projects-hub/blob/4a4d658cc5209429c7ed584ed0c1a8b51700743d/web/src/App.tsx#L1271-L1294), [local persist](https://github.com/onedayonemasterpiece/projects-hub/blob/4a4d658cc5209429c7ed584ed0c1a8b51700743d/web/src/offlineSources.ts#L149-L194)

В существующем server recovery весь raw PCM источник скачивается на телефон в ArrayBuffer и оттуда отправляется обратно на backend порциями по 8000 байт в buffered session. Это не обычный Live, но для слабой сети такой круг увеличивает трафик, время и память телефона. Серверный источник должен восстанавливаться на сервере по авторизованному source ID, с сохранением idempotency, damaged-turn fence и запрета превращать старую запись в новый исполнимый приказ. [Recovery](https://github.com/onedayonemasterpiece/projects-hub/blob/4a4d658cc5209429c7ed584ed0c1a8b51700743d/web/src/serverRecovery.ts#L66-L134)

## 3. Обязательное сжатие: выигрыш и границы

| Направление | Текущий PCM payload | Исследовательский Opus target | Отношение bitrate |
|---|---:|---:|---:|
| Телефон → сервер, mono 16 кГц PCM16 | 256 кбит/с | 24 кбит/с | 10,7× |
| Сервер → телефон, mono 24 кГц PCM16 | 384 кбит/с | 32 кбит/с | 12× |

Это арифметика целевых payload bitrate, **не измерение сетевой экономии и не обещание качества**. VBR, container headers, WSS/TLS/IP, подтверждения и повторная передача меняют фактические байты. 16 кГц raw PCM занимает 1,92 MB на минуту принятого аудио; при target 24 кбит/с codec payload — 0,18 MB/мин. Отсечение тишины означает, что минута открытой сессии не всегда равна минуте передаваемого звука.

На 40 мс текущего PCM приходится 1280 bytes звука, 1292 bytes телефонного binary WSS payload и 1708 bytes base64 ASCII. Пример основного Google JSON занимает 1786 bytes; второй caption-путь при 100 мс порциях добавляет около 43460 bytes/с. Вместе получается около 88110 bytes/с application JSON на два provider соединения при непрерывно принятой речи. Возможное negotiated WebSocket deflate здесь не измерялось; его нельзя смешивать с аудиокодеком или считать отсутствующим только по исходникам.

Сжатое исходное аудио следует сохранять как source, не декодируя и не перекодируя весь backlog повторно. PCM нужен только короткому активному участку перед текущим Google API и другим потребителям, которым он действительно требуется. Если primary и caption остаются, они могут использовать один короткий decoded PCM buffer; base64 создаётся при подготовке соответствующего provider сообщения.

### Кодек уже исполняется как машинный код

`libopus` — нативная библиотека с C API. Её вызов из Python не превращает алгоритм Opus в Python-цикл. Она поддерживает stateful encoder/decoder и rates 8/12/16/24/48 кГц. Не нужен обязательный путь decode до 48 кГц → Python resample до 16 кГц: decoder может сразу отдавать необходимую поддерживаемую частоту. Один state нельзя одновременно использовать для независимых потоков; его не следует создавать заново на каждый кадр. [Encoder API](https://opus-codec.org/docs/opus_api-1.6/group__opus__encoder.html), [Decoder API](https://opus-codec.org/docs/opus_api-1.6/group__opus__decoder.html)

На Android системное Opus encoding гарантируется с Android 10, decoding — с Android 5.0. В текущем проекте minSdk 26, поэтому нельзя предположить наличие системного Opus encoder на каждом поддерживаемом устройстве. Нужна проверка capabilities и либо bundled software Opus для нужных версий, либо явно согласованный альтернативный сжатый профиль. Повышение минимальной Android-версии не является автоматической частью оптимизации. [Supported media formats](https://developer.android.com/media/platform/supported-formats)

Opus — lossy codec. Выбор bitrate/application/complexity подтверждается на русской речи, именах, числах, тихом голосе, шуме и обрывах; текущая синтетика этого не проверяет. Кодек сам по себе не улучшает распознавание, если выбранные настройки удаляют полезную речевую информацию.

## 4. Короткие локальные измерения

Среда: Linux x86_64, AMD EPYC 9V74, Python 3.12.14, Node 24.19.0, libopus 1.4. Shared host без закрепления ядра; это не Android и не рабочий VPS. Исходные результаты, source hashes и переносимые offline scripts находятся в [measurements](measurements/) и [probes](probes/).

### Существующие функции

Пять timed samples с warmup; показана median process CPU. Код импортирует существующие source functions, provider send заменён in-memory sink; внешняя сеть и модель не вызываются.

| Измеренный участок | Размер | CPU на секунду аудио |
|---|---|---:|
| WSS decode → base64 encode → adapter decode | 40 мс входа | 0,120 мс |
| Main NDJSON queue → parse → provider JSON | 40 мс входа | 0,409 мс |
| Caption feed/slice/queue → parse → provider JSON | 40 мс входа | 0,422 мс |
| Downlink base64 decode → binary frame | 100 мс ответа | 0,090 мс |
| JS resample + RMS + Worklet-style copy, Node x86 | 128 samples при 48 кГц | 1,061 мс |

Сумма первых трёх строк — около 0,95 мс CPU/аудиосекунду, арифметика отдельных serial stages. Не измерены resource guards, SQLite/диск, TLS, реальное WSS scheduling, browser capture/playback, GPU/model inference и конкуренция пользователей. Python allocation peaks в осушаемой синтетической очереди нельзя объявлять RAM на сессию. RSS этого маленького harness нельзя сравнивать с RSS полноценного Go/Rust host.

### Нативный Opus через Python ctypes

Три пробы по 20 секунд синтетического аудио для каждого профиля; mono, 20 мс packets, VBR, FEC/DTX выключены. В таблице complexity 5; JSON содержит также 0 и 10. CPU включает ctypes loop и instrumentation.

| Профиль | Encode CPU/аудиосекунду | Decode CPU/аудиосекунду |
|---|---:|---:|
| 16 кГц / target 24 кбит/с / VOIP | 5,897 мс | 0,632 мс |
| 24 кГц / target 32 кбит/с / AUDIO | 2,444 мс | 0,925 мс |

Для предполагаемой серверной роли decode входа + encode ответа сумма двух подходящих клеток составляет около **3,08 мс CPU на секунду одновременно активного входного и выходного аудио**. Это сумма независимых проб, не тест полного duplex host и не прогноз числа сессий. Размер mono encoder state 43580 bytes, decoder state 18228 bytes относится только к данной версии libopus; это не память процесса, очередей или сессии. Синтетический signal приводил к разным SILK/CELT режимам, поэтому complexity 10 не всегда был медленнее 5; заключение о качестве из этого делать нельзя.

Вывод проб узкий: нативный кодек доступен внутри нынешнего host с небольшими измеренными локальными затратами. Они не обосновывают перенос всего framework и не отменяют проверку интегрированной нагрузки.

## 5. Выбор между Go, Rust и нынешним host

| Вариант | Когда оправдан | Что остаётся проверить / цена |
|---|---|---|
| **Python host + native codec** | Первый compressed v2 с сохранением уже исправленных Stop/checkpoint/session semantics | Отделить blocking I/O, ограничить queues и native work, измерить event-loop lag |
| **Небольшой Rust module через FFI/PyO3** | Измерен дорогой kernel: sample processing, framing/copy, иной CPU-bound участок вне готового кодека | Packaging/ABI, cancellation, владение state; отсутствие GIL в native call само по себе не делает вызов асинхронным |
| **Go implementation общего transport/session host** | После предыдущих исправлений ограничителем остаются обслуживание соединений, allocations/GC Python, orchestration CPU/RAM | Эквивалентность lifecycle, SDK/resource boundary, интеграция продукта, стоимость дополнительного процесса при сохранении Python |
| **Go proxy перед неизменённым Python audio path** | Только если доказана самостоятельная нужная функция этой границы | Иначе остаётся прежняя обработка каждого кадра плюс ещё один hop/process/очередь |

Go подходит для сетевой конкурентности и способен улучшить вместимость; это не гарантирует сокращение внешнего model latency. У него тоже есть GC. `GOMEMLIMIT` — мягкий runtime limit, а не жёсткая граница RSS вместе с C-библиотеками; чрезмерное ограничение может увеличить GC CPU. Подключение libopus через cgo требует учитывать нативную память, pointer rules и C cross-toolchain. [Go GC guide](https://go.dev/doc/gc-guide), [cgo](https://pkg.go.dev/cmd/cgo)

Rust предоставляет управление памятью без tracing GC и предотвращает data races в safe code, но не устраняет общие логические race conditions. Поздний checkpoint после Stop, неверный cumulative ACK или повторный бизнес-эффект останутся ошибками и в Rust, если перенести неверный протокол. [Ownership](https://doc.rust-lang.org/book/ch04-01-what-is-ownership.html), [Rustonomicon](https://doc.rust-lang.org/nomicon/races.html)

Для FFI недостаточно сказать «освободим GIL»: вызывающий event-loop thread всё равно занят до возврата синхронной функции. Выбор bounded worker scheduling зависит от измеренной длительности работы и конкуренции; запуск отдельной threadpool job для каждого 20-мс packet не является бесплатной оптимизацией. [PyO3 parallelism](https://pyo3.rs/main/parallelism), [asyncio development](https://docs.python.org/3/library/asyncio-dev.html)

В приложенном межпроектном отчёте полезен порядок A → B → C: нынешний путь, обоснованно оптимизированный нынешний путь, затем эквивалентный Go. Для Projects Hub вступительная рекомендация Go пока сильнее приведённых измерений: доклад не выполнял новый audio benchmark. Наши локальные пробы уточняют это место; выводы о других проектах здесь не пересматриваются. Чужие результаты миграций и HTTP RPS не являются коэффициентом ускорения этого сервиса.

## 6. Что означает корректная работа при нестабильном интернете

Нужны раздельные гарантии: сохранить источник, доставить его, восстановить контекст и поддержать живое взаимодействие. При полном отсутствии связи удалённая Мира не может отвечать online. Приложение должно продолжать разрешённую запись в пределах durable spool, честно показать режим и после восстановления сначала обслуживать свежую речь. Local Stop сразу останавливает capture независимо от сети. Уже сохранённый источник остаётся доступным; он не превращается в новый executable owner turn.

Компрессия уменьшает необходимую полосу, но WSS/TCP сохраняет ordered delivery: потерянные старые bytes способны задержать новые. Приоритет внутри приложения помогает до записи в socket buffer; уже отправленный backlog нельзя просто обогнать новым сообщением в том же ordered потоке. [RFC 6455](https://www.rfc-editor.org/rfc/rfc6455), [RFC 9000, §13](https://www.rfc-editor.org/rfc/rfc9000.html#section-13)

Начальный вариант — сжатый binary WSS в существующем framework, короткая live window и дозированная recovery. Если квалификация плохой сети покажет недопустимую latency именно из-за transport HOL, следующий кандидат — WebRTC media с надёжной доставкой source отдельно. Он добавляет ICE/STUN/TURN и DTLS/SRTP; TCP fallback также может возвращать HOL. QUIC/WebTransport — отдельное архитектурное решение: независимые streams устраняют блокирование между streams, но не внутри одного reliable stream; datagrams требуют собственной политики потерь/восстановления. Ни одно из этих изменений не получается автоматически из смены языка. [RFC 8835](https://www.rfc-editor.org/rfc/rfc8835.html), [RFC 9221](https://www.rfc-editor.org/rfc/rfc9221.html)

WebRTC/QUIC здесь рассматриваются как исследовательские альтернативы за пределами текущего согласованного WSS-пути. Смена основного транспорта требует отдельного архитектурного решения и изменения соответствующего требования; текущее согласование codec profile этого не разрешает.

Если доступная скорость доставки `C` не превышает скорость появления новых данных `R`, backlog не сокращается. При `C > R` на его очистку остаётся не больше `C − R`, ещё до остальных расходов. Поэтому «весь backlog сначала, потом снова Live» противоречит свежему приоритету и может не завершиться. Нужны отдельные bounded live/recovery бюджеты и честный delayed/recording-only режим.

### Уточнения draft, влияющие на надёжность

- `fsync` раз в 250 мс не доказывает, что можно потерять не больше 250 мс captured audio: есть codec frame, container, очередь writer и сам commit. Измеряется возраст самого старого ещё volatile sample; Stop/закрытие обязаны корректно сохранить финальный хвост.
- Durable receipt относится к payload и достаточным recovery metadata. Cumulative ACK N означает непрерывно сохранённый prefix до N, а не максимальный увиденный номер через пропуск.
- Source codec descriptor закрепляется за epoch. Новый reconnect profile не делает pending material предыдущей epoch декодируемым новым state и не разрешает незаметное transcoding.
- Ogg page не обязательно самостоятельный декодируемый фрагмент: необходимы headers, правила continued packets и restart/preroll. В Ogg Opus granule clock всегда 48 кГц, независимо от PCM rate decoder 16/24 кГц. [RFC 7845](https://www.rfc-editor.org/rfc/rfc7845)
- Маленькие packets имеют заметные headers: при 20 мс / 24 кбит/с target payload 60 bytes, однопакетная Ogg page добавляет 28 bytes, то есть 11,2 кбит/с при 50 pages/s. Три packets на page уменьшают этот расчёт до 4 кбит/с, но первый packet ждёт до 40 мс дополнительно. Это расчёт container trade-off, не выбранный окончательный packetization. [RFC 3533](https://www.rfc-editor.org/rfc/rfc3533)
- RAM capture tail, disk spool, live window, provider queue и recovery pace ограничиваются отдельно. Маленький compressed packet может декодироваться в существенно большее PCM; нужны ограничения bytes, audio duration/age и decode work.

Эти уточнения сохраняют утверждённую цель. Они не требуют нового решения владельца о внутреннем API и не разрешают ослабить долговечность ради скорости.

## 7. Следующая проверка и вопросы владельцу

Продолжение реализации идёт в существующих shared/consumer ветках. Порядок проверки: сначала сжатый путь и устранение лишних переходов с прежними authority boundaries, затем интегрированный профиль, затем небольшой эквивалентный Go/Rust pilot только при измеренном остаточном ограничителе.

В интегрированном профиле нужны CPU-seconds на audio-second, RSS/cgroup всей поставляемой цепочки, event-loop lag, p95/p99 capture → local durable → server durable → provider, возраст очередей, время свежей речи после reconnect и освобождение ресурсов после Stop. Источник аудио подаётся по времени независимо от ACK, иначе генератор скрывает накопление очередей. Отдельно проверяются ограниченная полоса, jitter/loss, outage, handover, насыщение диска и параллельные пользователи. Качество речи и физические Android/background/Bluetooth проверки не заменяются этим профилем.

Новых решений по разрешению компрессии, приоритету Stop или смене OpenCode-провайдера не требуется. Для выбора запаса остаётся один вопрос верхнего уровня:

**Какой пик одновременно активных голосовых разговоров нужен первому рабочему релизу и какой рост ожидается?** Текущий admission default 16 — защита, не доказанная вместимость и не ответ владельца о целевой нагрузке. До ответа использовать его как исходную точку измерений и не выдавать за capacity.

Вопрос не блокирует обязательную компрессию, исправление source recovery detour и продолжение уже начатого continuity. Субтитры сохраняют существующие latency/quality gates; если измерения выявят конкретный неразрешимый компромисс стоимости и задержки, он будет вынесен отдельно с числами. Результат исследования не является разрешением переписать проект целиком или объявить native v2 выпущенным.
