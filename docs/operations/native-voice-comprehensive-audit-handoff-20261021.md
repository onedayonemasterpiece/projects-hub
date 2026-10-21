# Projects Hub / Мира — комплексное техническое досье голосового движка для независимого аудита Opus 5.5

> **Ответ независимого аудита (Opus 5.5, прямое чтение кода, 21.10):** [native-voice-independent-audit-20261021.md](native-voice-independent-audit-20261021.md) — новые P0 на сервере (O(n) SQL на кадр, общий RLock продукта на event-loop, STALE после ACK, якорь часов), окно клиента 32 кадра, накопительная квота журнала телефона; патч и тест приложены.

**Версия досье:** 21 октября 2026 года. **Статус продукта: НЕ ПРИНЯТ.** Этот документ самодостаточен в части известных отказов, основных численных событий, исходных выводов трёх аудитов Opus, применённых и не применённых исправлений, гипотез, вопросов и сценариев проверки. **Для чтения содержания доступ к Slack НЕ требуется.**

**Критическая оговорка:** здесь собраны обезличенные технические события из доступного серверного journald, доказательства CI и сводные результаты физических Android-логов, проанализированных dot. Полные двоичные logcat, личные M4A, JSON/SRT транскриптов и необработанные файлы из Slack **в публичный GitHub не переносились**. Там, где отсутствует первичная поминутная трасса Android, это прямо указано. Не заявлять, что документ содержит побайтную копию всех логов. Для постановки проблем и разработки можно пользоваться только этим документом, исходниками GitHub и указанными открытыми GitHub evidence.

## 0. Место в документации, как читать и какой результат требуется

**Документная иерархия:**

1. [Видение продукта](../vision.md) и [центральная Мира](../product/12-central-live-agent.md) — зачем существует продукт.
2. [Защищённые требования](../../.devcoveer/requirements.json), [каноническая постановка и общий handoff](../prompts/projects-hub-native-voice-canonical-handoff-20261020.md) — функциональный контракт, источник решений, приоритеты и запреты.
3. [Единственный Android-пакет и процедура релиза](single-app-voice-20261020.md), [интеграция голоса и ресурсов](voice-delivery-integration-20261018.md) — технические правила исполнения.
4. **Настоящее досье** — исследовательский слой: физические тесты 18–20 октября, первичные события, конкретные результаты предыдущих аудитов, незакрытые проблемы и новые кандидаты.
5. [Shared live-interaction: architecture](https://github.com/onedayonemasterpiece/live-interaction/blob/main/docs/live-agent-architecture.md) и [voice continuity](https://github.com/onedayonemasterpiece/live-interaction/blob/main/docs/voice-continuity-contract.md) — подробности SDK.
6. [Историческая постановка от 18.10](../prompts/projects-hub-native-voice-final-debug-and-delivery-20261018.md) — исходные факты раннего v72, **не** актуальная команда к откату. Числовые JSON-артефакты эмулятора хранятся в [каталоге evidence](evidence).

**Суть требуемого результата:** одна центральная семантическая Live-Мира на Samsung S21 Ultra. Нативные AudioRecord / VAD / MediaCodec / WSS / AudioTrack живут независимо от WebView; WebView — наблюдение и единая личная переписка. До **90 минут** полноценного двухстороннего разговора с длинными ответами, естественными микропаузами, активным перебиванием и сохранением контекста. Нестабильная мобильная сеть, переход Wi-Fi ↔ LTE, Bluetooth, Google provider GoAway и reconnection не приводят к потере исходной речи, дублированию уже выполненной команды, воспроизведению вчерашнего ответа, ложному played/ACK, зависанию или самопроизвольному Stop. До четырёх голосовых помощников работают параллельно на общем DevCoveer вместе с RKB и другими сервисами — ёмкость надо **измерить**, а не предположить.

**Не путать уровни доказательства:**
- Android CI/эмулятор с контролируемой AI-границей ≠ физическая Samsung акустика.
- Два WSS поколения и server-durable 816/816 Opus пакетов ≠ 90 минут без потерь ≠ транскрипция всех слов моделью.
- Сервер сохранил original ≠ Google получил этот PCM в том же логическом ходу.
- AudioTrack reported playhead ≠ человек действительно слышал всё, а «interrupt callback» ≠ ошибочно обрезанное слово.
- GitHub latest / подписанная версия ≠ установленная на телефоне версия ≠ продуктовая приёмка.

## 1. Контрольные версии и источники истины

| Линия | Фактический выпуск / исходник | Что известно |
| --- | --- | --- |
| 18.10, v72 | Подписанный Android v72; SDK rc2 → исправление маршрута динамика в общем SDK rc3 | На Samsung были текст и аудиоответ от Google, но человек не слышал звук. Позднее обнаружена отдельная проблема с token budget / большим reconfigure. Физическая причина неслышимости в тот момент не доказана. |
| 19–20.10, v76/отдельный debug и v77 | Основной signed v76/v77 и отдельный старый debug-пакет | **Тест 19.10 проходил в debug, не в официальной v76**. Не утверждать «signed v76 деградировала» на основании этого теста. Журналы сервера при этом действительные. |
| 20.10, v78 | Исходник Projects Hub f25493884c037e85536b97e8fbaba5212ab68cc5; SDK rc12 | Реальная регрессия: архивные ответы, некорректные переходы после reconnect, недоступные native captions. Отдельные ошибки описаны ниже. |
| 20.10, v79 | Основной пакет com.kenigevents.projectshub, 0.1.62+79, product aacce79d010a91a694cdd0a033953d87c9ae0141, SDK rc16 f3c6831ebd2d1fba1381e16a06a8911eedf2a4fd | **Эта пара фактически проверялась на Samsung в двух физических сессиях** 20.10. User Stop в конце обоих. Ещё имеются STALE и локальные backlog recoveries. |
| 20–21.10, **поставленный v80** | Основной подписанный [v80](https://github.com/onedayonemasterpiece/projects-hub/releases/tag/android-v80) 0.1.62+80, product 59c71a635f5aff9a98150d861053977f72191fb0, SDK **rc17** 404b013a66546a1020f3a547fd978c89571be53e | Backend /healthz ранее подтвердил указанную пару, service active, 0 restarts. Android [CI 39497666513](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39497666513), [release 39499061755](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39499061755) и [self-update E2E 39499322865](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39499322865) SUCCESS. **НЕТ доказанной физической 15–20/90-мин приёмки v80.** |
| 21.10, **ещё кандидат rc18** | [Shared SDK PR70](https://github.com/onedayonemasterpiece/live-interaction/pull/70) merged и [rc18](https://github.com/onedayonemasterpiece/live-interaction/releases/tag/v0.4.0-rc.18) опубликован: SDK e710e8e8143a3724d16250a6e439ccd178c279ca. [Product PR207](https://github.com/onedayonemasterpiece/projects-hub/pull/207), head d6ab8158b738d1d36db14b6a1988bc9aeec6e80d | Добавлен **входной** group fsync ≤100 мс; продуктовые unit и PWA/backend проверки проходили. На момент последней проверки [Android CI 39587321439](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39587321439) ещё выполнялся — **проверить фактический итог перед merge**. Не считать этот PR установленным или принятым на Samsung. |

**Идентичность поставки:** точные app versionCode/versionName, package и подпись, SDK SHA, product source SHA, backend /healthz release_sha и версия AI provider обязаны фиксироваться отдельно. **Не переносить события с v78 на v80 как доказанный диагноз.** Для повтора использовать единственный штатный in-app updater; не устанавливать дополнительный debug-пакет, не выполнять pm clear/uninstall, не трогать локальные .wvs.

## 2. Хронология физических отказов до двух последних тестов

### 2.1. 18 октября — v72: текст модели есть, звука не слышно

В физическом тесте Samsung пользователь видел ответы на приветствие и вопрос о брифе, но голос не был слышен. В backend-окне 15:30:48–15:33:03 UTC сохранился факт генерации **12 provider audio events, суммарно 190 080 байт PCM** в первом ответе. Звуковой маршрут AudioTrack/AudioFocus/communication speaker был гипотезой, не доказанной первопричиной. При попытке collaboration reconfigure setup оценивался примерно в **31 тысячу единиц UTF-8 JSON**, вслед за чем шли RESOURCE_TOKEN_BUDGET для tool_response; UI продолжал показывать ожидание. Не смешивать неслышимость звука, нехватку токенов и отсутствие ответа по смыслу.

На том же исходнике один Android UI-voice/EOS CI прогон дал **5/6**, сбой LIVE_CODEC_CAPTURE_SPAN_MISMATCH после Stop; следующий прогон без правки дал **6/6**. Повторное PASS **не исправило** intermittent defect. Эти исторические факты изложены в [исходной постановке от 18.10](../prompts/projects-hub-native-voice-final-debug-and-delivery-20261018.md).

### 2.2. 19 октября — отдельный debug: reconnect storm / бюджет / архив

В прежнем Debug-тесте, где source SHA самого APK был не вполне установлен, backend-сессия за **88 секунд** осуществила порядка **12 WSS подключений**, с повторными LIVE_SOCKET_IO, LIVE_AUDIO_STALE и завершением LIVE_AUDIO_OUTPUT_ARCHIVE_STALL. Другая сессия длилась около **10,2 минуты**, около 5 поколений подключения, в ней встречались **3 LIVE_AUDIO_BACKPRESSURE** и 1 LIVE_AUDIO_STALE. Есть события RESOURCE_TOKEN_BUDGET, Playback/AudioTrack underrun, оценочные задержки NativeVoiceStore **110–322 мс**. Основной исторический handoff содержит IDs инцидентов inc_a6d3286ccf0620e09f450bdb (звук), inc_f33903eeb0eeb826ed7c7a84 (длинные ответы), inc_66519d591551a6b378dabd44 (budget), inc_aae28418343acf9cc9ad3812 (ASR). Статусы этих incident IDs следует проверить заново.

Не переносить эту статистику на v79 как «повторился тот же код»: ранняя версия могла иметь другие capture/codec/recovery ветви. Однако **общий класс узкого участка**, конкуренция durable store и realtime audio, существовал до текущего патча.

### 2.3. 20 октября — v78: тяжёлая реальная регрессия

Три независимых механизма:
1. **Старое аудио:** product pending_outputs выбирал все незавершённые output исходники одной беседы; Android в новом Start мог автоматически пытаться возобновить ответы предыдущего дня. Ошибка session-scope была доказана кодом, а не предположением о поведении провайдера.
2. **Серверное переполнение:** 10-мс Opus пакеты заполняли прежнюю очередь из **32 элементов** при небольшом количестве байтов, до истечения согласованного 960-мс окна. Иногда LIVE_AUDIO_BACKPRESSURE приводил к reconnect.
3. **Последовательность после восстановления:** поздние terminal/interrupted события A могли закрывать codec epoch ответа B либо неверно отменять B; новый ответ при открытом архиве A вызывал LIVE_AUDIO_OUTPUT_SEQUENCE, после чего server fail мог перевести логический разговор в Stop.

**Первичные выборки journald (сводка обезличена):**

~~~text
UTC                      событие                     данные
2026-10-20 14:12:15.063  socket_detached             LIVE_AUDIO_BACKPRESSURE, generation=1, queue_bytes=4618
2026-10-20 14:12:41.199  socket_detached             LIVE_AUDIO_BACKPRESSURE, generation=2, queue_bytes=0
2026-10-20 14:14:23.766  socket_detached             LIVE_AUDIO_BACKPRESSURE, generation=4, queue_bytes=4396
2026-10-20 14:14:27.299  socket_failed               LIVE_AUDIO_OUTPUT_SEQUENCE, generation=5, queue_bytes=0
2026-10-20 14:15:54.804  socket_detached             LIVE_AUDIO_STALE, generation=2, queue_bytes=212
2026-10-20 14:16:16.177  socket_detached             LIVE_AUDIO_BACKPRESSURE, generation=4, queue_bytes=1313
~~~

Первые четыре строки относятся к одной конкретной проблемной сессии в районе 14:12–14:14; последние две — к **другой** session. Запрещено склеивать разные session_id в одну цепочку. Прежний подробный аудит зафиксировал ещё последовательность около 14:02: backpressure → stale output → OUTPUT_SEQUENCE → Stop примерно через 57 мс; для атрибуции конкретной причины нужен точный response_id/event_seq, одна лишь близость по времени недостаточна.

**Эмуляторное воспроизведение отдельного дефекта:** [CI 39447837713](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39447837713) завершался LIVE_NATIVE_STORAGE_BACKPRESSURE в момент первого ответа, когда downlink уже имел contiguous_seq=19 и played_us=376000 мкс, uplink имел contiguous_seq=290, а аудиоочередь была ~620 мс. Это данные **контролируемого эмулятора**, а не того же физического разрыва.

## 3. Два физических Samsung v79 теста 20 октября — числовая корреляция

**Привязка версии:** Dot подтвердил с телефона именно подписанную **0.1.62+79 / versionCode79**, основной package com.kenigevents.projectshub, .debug при readback отсутствовал. Сервер был на product aacce79d… / SDK rc16, HTTP200. Эти два теста **не** относятся к v80/rc17.

| Характеристика | Тест 1 | Тест 2 |
| --- | --- | --- |
| Логическая беседа | около 18:40:59.997–18:46:53.902 UTC | около 6 мин 19 с, конец примерно 19:43:31 UTC |
| Суммарная продолжительность | **5:54** | **6:19** |
| socket recovery | **2** | **2** |
| LIVE_AUDIO_STALE | **2** | **1** |
| Другой WSS gap без установленной причины | не подтверждён отдельно | **1** |
| Android LIVE_NATIVE_LIVE_BACKLOG_RECOVERY | 18:43:07.170 и 18:46:02.495 UTC | 19:39:21.018 и 19:41:28.664 UTC |
| Max backend output-archive commit из сводного аудита | **869 мс** | **1 148 мс** |
| Финальный Stop | **18:46:52.701 UTC, user-initiated** | **19:43:31.089 UTC, user-initiated** |
| Active-source playback interruptions (не равны ошибочным barge-in) | **7** | **8** |
| Native partial text | CAPTION_CLOCK_UNVERIFIED | CAPTION_CLOCK_UNVERIFIED |

### 3.1. Первичные backend журналы (прочитаны из journald 21.10; поля обезличены)

~~~text
UTC                      event                    code/metric                            queue
2026-10-20 18:38:38.754  live_caption_event       CAPTION_CLOCK_UNVERIFIED              text_length=0
2026-10-20 18:41:02.240  live_caption_event       CAPTION_CLOCK_UNVERIFIED              text_length=0
2026-10-20 18:42:36.245  archive_admission_wait   wait=15ms                              102400 bytes / 7 items
2026-10-20 18:43:05.198  archive_admission_wait   wait=27ms                              110080 bytes / 4 items
2026-10-20 18:43:05.634  archive_admission_wait   wait=68ms                               99840 bytes / 6 items
2026-10-20 18:43:05.913  archive_admission_wait   wait=175ms                              79360 bytes / 2 items
2026-10-20 18:43:07.554  socket_detached          LIVE_AUDIO_STALE, generation=1         138 bytes
2026-10-20 18:46:02.865  socket_detached          LIVE_AUDIO_STALE, generation=2         1134 bytes
2026-10-20 19:38:16.133  archive_admission_wait   wait=1132ms                             66560 bytes / 3 items
2026-10-20 19:39:21.540  socket_detached          LIVE_AUDIO_STALE, generation=1         0 bytes
2026-10-20 19:39:23.303  archive_admission_wait   wait=132ms                              92160 bytes / 4 items
2026-10-20 19:41:19.192  archive_admission_wait   wait=17ms                               87040 bytes / 6 items
2026-10-20 19:41:57.912  archive_admission_wait   wait=30ms                               66560 bytes / 2 items
~~~

**Происхождение этих строк:** непосредственная ограниченная выборка приложения из DevCoveer/journald, нормализована до UTC, event, code, wait и queue; source/session/actor IDs и тексты убраны. Это подлинные числовые поля наблюдений, но **не сплошной журнал**.

**Важно о причинности:** большой archive wait и STALE находятся близко в первом тесте (до 2 с в группе 18:43:05–07), но близость сама по себе не доказывает единственную корневую причину. Второй тест имеет явный пик archive wait=1132мс в 19:38:16 и STALE в 19:39:21 — **больше минуты между ними**; нельзя утверждать, что именно этот единственный пик немедленно вызвал detach. Нужно проверить конкуренцию mic/output и очерёдность накопления backlog, source clock и ping.

### 3.2. Что действительно слышно, а что только следует из callbacks

- Финальные завершения **обоих** тестов произошли по пользовательскому Stop. Нельзя называть их самопроизвольным закрытием.
- Dot в корреляции Android/playback назвал **7 и 8** случаев interrupt при наличии активного output source в двух основных сессиях. Счётчики 73, 44 или 46 callbacks относятся к более широкому окну и **не** количеству слышимых обрывов.
- Во втором тесте явные акустические кандидаты окончания звука: **19:39:58.571 UTC** (прибл. F1 offset 03:36.33, энергия до ~03:36.36), **19:40:04.491 UTC** (~03:42.25, энергия до ~03:42.38), **19:41:50.573 UTC** (F2 ~00:04.83; голосовая атрибуция сомнительна). Это подтверждает остановку буфера, **не обязательно неверный barge-in или потерю слова**.
- Первый recovery второго теста приходится на ~02:59 первого записанного сегмента: **между вопросом и началом ответа**, а не однозначно посреди звучащего слова. Второй recovery пришёлся на отсутствующий в аудиозаписи участок продолжительностью примерно **23–24 секунды**. Пауза ~**7.9 секунды** во втором файле была после предложения завершить разговор и не совпала с recovery: нельзя автоматически назвать её багом.
- Пользовательский опыт: Мира иногда реагирует на **микропаузу в длинном вопросе** как на конец мысли, формирует короткий ответ, затем уступает продолжившейся речи; иногда UI показывает более полный текст, чем произнесённая часть. Надёжного независимого дословного эталона пользовательских реплик и общей временной шкалы обоих аудиофайлов не было. **Число/доля реально потерянных слов не установлены.**

## 4. Известные проблемы текста и реального Google Live, отдельно от transport

1. **CAPTION_CLOCK_UNVERIFIED** — **не случайное падение**. В product CaptionSidecar.start native clock по умолчанию unverified, sidecar отказывает до запуска дополнительной модели. Поэтому отсутствие streaming partial captions ожидаемо на текущей конфигурации. Нельзя просто убрать gate: source/sample clock, offset, scope и attributed utterance сначала должны быть доказаны.
2. **Canonical final ≠ verbatim.** На изолированном воспроизводимом wl-live-v2 с реальным Google Live из Android CI fixture (два WSS поколения одной логической сессии), в каждом отправлены и подтверждены **816 из 816** исходных Opus chunks, длительность входа ~**16.30 с**; получены две входные финальные транскрипции и завершённые выходные аудиоисточники. Первая русская ожидаемая фраза сократилась **в обоих прогонах**, вторая совпала целиком; строгая проверка имела **primary_passed=false**. Это не packet loss, а проблема семантического/ASR результата; нельзя «чинить» транспортным ACK или объявлять такой тест зелёным.
3. **Текст UI.** В раннем тесте пользователь не видел часть своих сообщений на экране, но затем находил их после ручного скролла. Гипотезы: timeline reconciliation/scroll/render, revision/role/turn mapping, асинхронная доставка; **нельзя** назвать все эти события потерей серверной транскрипции без полного event→timeline→viewport readback.
4. **Микропаузная сегментация и barge-in.** SDK NativeEncoder.observeVoice использует RMS относительно шума, quietSamples и примерно **480 мс** тишины для переходов; продукт имеет дополнительные Live/manual-activity semantics. Нужно выяснить, когда возникает PORTION_END, когда Google выдаёт response, когда Android его прерывает и когда пользователь ещё говорит. Произвольное изменение порога или 2-х независимых VAD одновременно может ухудшить естественность и эхо.
5. **Краткие ответы** не доказаны отдельным дефектом модели: в сценариях dot неоднократно явно просил отвечать одной короткой фразой. Длинные ответы нужно тестировать отдельно без такого ограничения.

## 5. Структурный кодовый аудит: где именно возникают задержки

### 5.1. Два направления и общая критическая секция

~~~text
Samsung AudioRecord (20-ms PCM)
  -> bounded 100-frame PCM queue (~2sec)
  -> NativeEncoder / MediaCodec Opus
  -> product NativeVoiceStore (.wvs, source/epoch/chunk IDs, fsync)
  -> only then device-durable ACK -> wl-live-v2 WSS
  -> server AudioSocket._worker(live/recovery)
  -> ProductEncodedAudioStore / EncodedVoiceIO
  -> SQLite WAL synchronous=FULL under one RLock
  -> server-durable ACK
  -> decode -> projection -> Google Live provider  [ВАЖНО: отдельная стадия]
Provider audio response
  -> shared AudioOutputArchive (Opus encoded)
  -> product encoded_voice_store SQLite output commit
  -> WSS downlink/recovery
  -> product NativeVoiceStore output .wvs + AudioTrack
  -> actual played cursor/Stop/cancel
~~~

**Code-confirmed:** продуктовый EncodedVoiceIO использует **2 worker threads, pending=8** для всех голосовых сессий и обоих направлений. SQLite connection единая, **threading.RLock**, **journal_mode=WAL**, **synchronous=FULL**. Ни отключать FULL, ни переносить receipt перед COMMIT нельзя. Product-owned NativeVoiceStore ранее делал **FileDescriptor.sync под общим synchronized монитором на каждый input, output и PLAYED/COMMIT record**. Поэтому output архив мог заставлять encoder ожидать монитор и наоборот. LRU четырёх открытых journal handles уменьшил повторное open/close, но **не** устранил длительные fsync.

**Opus 5.5 F0–F5 по v78/rc13 (независимая консультация dvt_fe2d2d35edf0440fb4699627b75ea879):**

- **F0:** Android STORAGE_BACKPRESSURE, серверный BACKPRESSURE и output OUTPUT_SEQUENCE — *три независимых класса*, не один баг.
- **F1:** в неудачном controlled Android CI первый output совпал с конкурирующими input commit/output/PLAYED, а PCM queue overflow завершался безусловным fail. Доказан факт конкуренции за один store; конкретные ms Android на физическом S21 в данном окне требовали измерения.
- **F2:** очереди server ingress и HOL: один блокирующий до **1.5с** admission frame задерживает последующие, read-ahead накапливает пакеты; общая SQLite/IO очередь может задерживать uplink. Дополнительно синхронные store calls из event-loop способны блокировать ping и audio, но loop lag тогда ещё не измерялся. Установление прямой причины каждого detach требовало queue/lock timestamps.
- **F3:** response-owner races: поздний terminal A закрывает codec epoch B, недостающий A terminal вызывает sequence fault, interrupted может отменить не тот response. При output failure возможен server stop. **Высокая уверенность в дефекте кода, средняя в атрибуции конкретного v78 14:02.**
- **F4:** server session-scoped autoplay был сломан, старые исторические output предлагались новой Live-сессии; очередь Android played и cancelled могла переживать Start. Внутрисессионный недозакрытый output также мог автоматически возвращаться.
- **F5:** captions отключены из-за отсутствия verified source clock; смешивание temporal clock может неправильно связать слова с репликами.

**Проверять не только общую очередь:**
- В SDK Android поле run.pcm = ArrayBlockingQueue(100) и fallback offer 100ms, затем системная ошибка при длительном заполнении. Если encoder отстаёт, источник получает fresh=false при >**0.5с** sample lag и может уйти в recovery-only, где Google не слышит актуальную речь вживую.
- Сервер после fsync может вернуть server-durable ACK на frame, который уже признан STALE и **никогда не проецируется в Google**. Durable bytes целы, но смысл реплики потерян. Повторная отправка committed chunk может также не пройти decode path из-за not-newly-committed. Это отдельный семантический P0, не «исчезла запись».
- Android ввод/вывод продолжает совместно использовать один монитор, PLAYED курсоры могут порождать дополнительные fsync; AudioTrack underrun и реальные короткие записи надо коррелировать, а не считать playhead акустикой.
- Когда Google возвращает A/B, terminal/EOS и resumption identities должны принадлежать конкретному response_id, а не только mutable session.response_seq.
- На длительном источнике при 20мс страницах **90 мин = 270 000 chunks**. Старый лимит **250 000** срабатывал около **83мин20с**; поднят до **400 000**, но это **не проверяет** реальные дисковые квоты и долгую стабильность.
- Product device STORE_LIMITS в NativeVoiceService: **128 MiB journal bytes**, **4096 источников**, 120 минут source duration. Частая source rotation/reply outputs могут исчерпать source-count или размер записи; бесконтрольная автозачистка не разрешена. Требуется 90-мин профиль с исходниками, sealed/retained без удаления неотправленного.

### 5.2. Устранено по версиям, статус доказательства

| Изменение | Где | Примечание |
| --- | --- | --- |
| Session-scoped pending output и отсутствие автоматического replay вчерашнего | SDK rc13 + product PR202 | Оригиналы остаются доступны явным archival read. |
| Изоляция сломанного output recovery от полного Stop | rc14 | Строгие durable receipts сохраняются. |
| Пакетное число слотов для 10-мс Opus (например live 104/recovery 56) и stale unbound ACK | rc15 | Не убраны ограничения по bytes/duration/age. |
| Response A/B, generation_complete, late terminal и обработка потерянного EOS | rc16 | Нужно проверить valid cancellation semantics и provider GoAway, а не считать выходной Stop невозможным. |
| Atomic downlink output burst до 6 страниц в SQLite | rc17 + product PR205/206, **поставлено v80** | Уменьшает конкуренцию за lock; по двум новым v79 физическим тестам эту оптимизацию **не измеряли**, потому что они до v80. |
| Native source LRU, 90m per-source chunk upper bound 400k | product PR202, поставлено | Сохраняет оригиналы, уменьшает файловые переключения, но не микрофонные per-page fsync. |
| Atomic **uplink microphone** journal burst <=6 страниц / <=100мс с **одним fsync**, flush по EOS/turn/idle; защита bind cursor overrun | **rc18 + product PR207, кандидат** | Архив и тесты кода собраны 21.10. Пока **не считать физически установленным и проверенным**. Один глобальный монитор и серверный uplink пофрагментный commit остаются отдельными пунктами. |

## 6. Что Opus 5.5 рекомендовал и что осталось открытым

Первый аудит и две дополнительные критические консультации (dvt_fe2d2d35edf0440fb4699627b75ea879; dvt_4e81b45c267b4d60a026e9f8b1acb48c; dvt_1dad040d036149948f04063c3ece7003) запускались **provided_only**, по переданным выдержкам кода, а не через произвольное самостоятельное чтение репозитория. Это ограничение должно быть восполнено **внешним независимым аудитором с прямым доступом к GitHub**. Не ссылаться на эти консультации как на «доказательство полной проверки всего репозитория».

**P0-1, output epoch ownership (в основном реализовано rc16):** поздний turn_complete от A не закрывает epoch B; response меняется только с закрытием прежнего оригинала; interruption отменяет правильный response; stale old audio не переинтерпретируется. Повторить инъекции на GoAway и изолированном реальном provider.

**P0-2, output batch (реализовано rc17):** одна SQLite транзакция для 1–6 оригинальных выходных фрагментов, ACK только после COMMIT. Важный край: failed COMMIT может уже rollback-нуть transaction, нельзя заслонить первоначальное исключение вторым ROLLBACK failure.

**P0-3, uplink batch (кандидат rc18):** на Android commit 4–6 фрагментов / <=100ms одним fsync, без изменения .wvs framing. На сервере uplink ещё может иметь один COMMIT на кадр; Opus рекомендовал аналогичный короткий group commit под неизменными durable ACK и строгой очередностью projection. Не реализовывать невидимое drop.

**P0-4, measurement first:** реальные io_queue_wait_ms, store_lock_wait_ms, commit_ms по direction, event-loop lag p99/max, ingress high-water, отличие локального backlog от provider lag, AudioTrack underrun и Android monitor/fsync contention. Само появление numeric diagnostics в rc17 не означает, что все метрики реализованы. Нельзя «лечить» просто увеличением 1.5с/2с таймаутов.

**P0-5, full-duplex Android:** общий synchronized monitor на input/output/played остаётся. Перенос fsync наружу без source pinning, close/fd lifetime, rollback, global quota reservation опасен. Opus предложил сначала bounded group sync, затем измерить. PLAYED записи допустимо коалесцировать по согласованной семантике не более 250–500ms с обязательным flush при Stop/pause/complete; риск повторного воспроизведения должен быть явно измерен и согласован.

**P0-6, unbound ACK:** опоздавший ACK отвязанного output после reconnect не должен убивать WSS, но ACK seq > фактически server-durable должен **оставаться ошибкой**. Это не разрешение игнорировать любые нарушения последовательности.

**Незакрытые вопросы сверх пачек (P0/P1):**
- Server live STALE **после** durable commit: как без потери семантики сообщить, что исходник сохранён, но не доставлен модели, и корректно открыть recovery с source/portion boundaries?
- Ввод Mира на микропаузах: 480ms quiet у NativeEncoder + manual/automatic Google VAD, преждевременный response, active-source interrupt. Нужно выяснить последнюю безопасную точку фактического user speech, а не просто поднимать RMS.
- Возможен source sample clock drift за 90 мин. Opus оценил: +50 ppm приведёт к 250мс смещения примерно за **83 мин**, +100 ppm — за ~42мин; это модельная **оценка**, не измеренный дрейф S21. Часы captions/ASR сопоставить по source/sample/monotonic/UTC.
- Google context window compression / GoAway / resumption и tool idempotency в одной логической сессии. Без provider state нельзя объявлять долгую семантическую continuity.
- Four concurrent voice users + RKB на одной машине: shared 2 workers / 8 slots, SQLite FULL и другие соседние нагрузки требуют stress + fair-share профиля, не переписывания на Go/Rust по гипотезе.
- Браузерная единая timeline: final input/role/turn/revision, partial→canonical replacement, auto-scroll, stale UI, воспроизводимость «есть в истории, не видно на экране».

**Не делать (Opus no-go и инварианты владельца):** выключать fsync или PRAGMA synchronous=OFF, выдавать receipt/played до реальной фиксации/плейбека, дропать uplink кадры, бесконечно расширять очереди или timeout, ресетить output_next и повторно запускать семантические tool calls, открывать новую логическую Live session после terminal, включать captions с непроверенным wall clock, терять source/epoch IDs, удалять .wvs и отдельное debug-приложение без прямого разрешения, снижать waveform threshold, выключать другие сервисы ради теста.

## 7. Матрица 18 детерминированных граничных испытаний (прежний Opus, скорректирована)

| № | Arrange / инъекция | Инвариант результата |
| --- | --- | --- |
| 1 | Audio A → закрыть WSS → stale gap → поздний terminal A → audio B | Одна логическая сессия, B новый codec epoch, нет OUTPUT_SEQUENCE, нет лишнего autoplay A. |
| 2 | Audio A → audio B без EOS | A не подмешивается к B, новый source B, зафиксированный diagnostic без server Stop. |
| 3 | Audio A → B → поздний turn_complete(A) | Terminal A не завершает открытый B encoder, нет OUTPUT_IDENTITY. |
| 4 | session.response_seq уже B, но interrupted(response_id=A) | Отменяется именно A; B продолжает играть. |
| 5 | Recovery A + cancel A + live B параллельно | После cancel не доставлять A; B не задерживать за старой очередью. |
| 6 | Новый WSS hello с played offset >0 и durable source A | Полный точный source prefix доступен, сыгранное не дублируется, output_next непрерывен. |
| 7 | Клиент прежней v78 пытается playback_resume вчерашнего source | Server фильтрует по текущему session; исторический played_us не меняется сам по себе. |
| 8 | 90 минут тишины, heartbeat, reconnect 10-мин интервал | Нет fsync/store quota fault, источники/журнал в лимите, нет отключённого навсегда liveSourceId. **Physical обязателен для Doze.** |
| 9 | 4 сессии одновременно, provider выдаёт 5с audio bursts, mic uplink непрерывен | Fairness, no starvation, p99 io/wait bounded, 0 BACKPRESSURE от нормального нагрузки. |
| 10 | Google GoAway между ответами A и B | Никаких повторных tool effects, контекст и source continuity, разрешённый resumption. |
| 11 | +50/100/150 ppm sample clock drift, потеря Android AudioTimestamp | Корректный source timeline; captions verified или безопасно disabled. **Физический drift отдельно.** |
| 12 | Сервер uplink commit пауза 1.4с, затем 1.6с | Recovery ровно по контракту, без фальшивого ACK и reconnect storm. |
| 13 | Device-durable output ACK задержан 4.9с и 5.1с | Recoverable WSS detach по timeout, но не произвольный session Stop; сохранён output source и cursor. |
| 14 | Android fsync 700–1200мс одновременно с incoming/outgoing и PLAYED | Нет silent PCM drop, false receipt, indefinite starvation; проверить high-water и восстановление. **Физический UFS отдельно.** |
| 15 | Blocking synchronous product tool/store call на event-loop 800мс | Event-loop lag измерен и устраняется без нарушения idempotent command_id. |
| 16 | Поздний unbound output ACK, затем seq > durable | Первый игнорируется с diagnostic; второй **строго** отвергается. |
| 17 | Encoder отстаёт на 600мс перед source rotation | Не возникает вечная recovery-only глухота; известны fresh/delayed transitions и реальные provider-admitted frames. |
| 18 | Bluetooth device route сменился посреди длинного ответа | Сохраняются route/AudioTrack/played, нет underrun storm или «слышен в неверном динамике». **Физический S21 обязателен.** |

**Добавить для rc18:** torn .wvs tail после записи 1..6 кадров перед fsync, EIO на force (нет выдачи receipts), смешанный duplicate prefix+new tail, partial group при Stop/epoch transition, source count/quota в 90мин, нет ложного сообщения device durable до force, незавершённый provider portion после уже server-durable STALE.

## 8. 12 сложных вопросов, ответы которых должны изменить код или план испытаний

1. Для **каждого** физического detach: это read-ahead full, expiration admission 1.5с, device 2с lag, codec/EOS, provider GoAway или иной WSS gap? Нужны generation, source, lane и первая сработавшая ошибка, не последний Stop.
2. При **OUTPUT_SEQUENCE**: какой event_seq/response_id пришёл, что было self.response_id/session.response_seq, какой original terminal был последним и какой не пришёл?
3. Как можно доказать, что после server-durable ACK пакет поступил **в provider**, а не был отброшен при STALE после commit? Какие source flags теряются при реконнекте?
4. Какова реальная стоимость 1 кадра Android: MediaCodec, monitor_wait, .wvs write, fsync (p50,p95,p99,max) одновременно с output PLAYED и Bluetooth route? Когда 100 кадров PCM =2с исчерпываются?
5. Почему при STALE воспроизводится несколько смен WSS generation за короткое время? Что ограничивает retrial rate и не создаёт новый Google turn/tool effect?
6. Измерено ли shared backend io_queue_wait, SQLite lock wait, commit by direction, p99 event loop lag и starvation при 4 users+RKB? Какой ресурс ограничивает первым?
7. Где lifetime mapping исходника A/B, can late terminal A cancel B, и что считается hardware played при output cancellation?
8. Не исчерпается ли 128MiB/4096 source quota раньше 90 минут из-за множества output sources и частых rotations? Кто вправе retire source, а какие .wvs обязаны пережить Stop и переустановку?
9. Что именно обрывает длинную реплику: NativeEncoder PORTION_END после ~480мс тишины, provider end-of-turn, cancelOutput, AudioTrack underrun, manual Stop или UI truncation? Как получить независимый эталон без приватного аудио в GitHub?
10. Откуда CAPTION_CLOCK_UNVERIFIED: какие sample/mono/UTC anchors есть на каждом участке и как доказать scope/provenance для partial и canonical replacement без второго семантического помощника?
11. Как обеспечивается 90-минутная provider continuity при GoAway/context limit: resumption token, compression, replay только durable оригиналов без повторного смыслового вызова?
12. Какие из текущих проверок являются именно физической приёмкой, а какие controlled fixture/коротким WSS тестом? Где evidence точной пары phone APK SHA/backend SHA/SDK SHA?

## 9. Обязательный порядок продуктовой приёмки

**Этап A, сервер/детерминированные контракты:** exact SHA SDK + consumer, 100% unit/Python/Node/Android CI; injection для 700–1200ms fsync, 10/20ms Opus, torn-tail, separate A/B, late ACK, source quota; главный check — ACK строго после durable fsync/COMMIT, нет silent loss. Автоматический эмулятор со строго прежним waveform threshold. Ни один flaky 5/6 не выдавать за «почти успех».

**Этап B, реальный Google Live:** последовательность 10+ содержательных turn, два-три WSS поколения одной logical session, explicit provider admission, audio output/seal/cancel, реальные RU input/output finals, точные source IDs, без OUTPUT_SEQUENCE и без provider/tool duplicate. Отдельно фиксировать mismatch canonical RU, не отменяя gate.

**Этап C, Samsung (по согласованию владельца):** только один signed main package через штатный in-app update; readback apk/version/signature и точный backend; нормальная работа при длинных вопросах/ответах/паузах, 15–20 минут; запись акустического результата + logcat и backend event scope; 1–2 управляемых reconnect, Stop/barge-in, Wi-Fi↔LTE/BT/route. Не стирать данные.

**Этап D, длительная работа:** минимум одна реальная непрерывная 90-минутная беседа как необходимый gate, затем повторные 90-минутные прогоны с другими профилями (тихо, плотно, нестабильная сеть), 4 параллельных сессии на фоне RKB, стабильные queue/CPU/mem/fsync p99/max, без ложных Stop и silently dropped provider admission, с retention исходников. Предыдущее предложение Opus было сильнее: **20×20мин с сетевыми сменами + 3×90мин**; это рекомендация аудитора, а не автоматически утверждённая владельцем норма.

**Условия безусловного NO-GO:** OUTPUT_SEQUENCE/OUTPUT_IDENTITY/ARCHIVE_STALL на нормальной речи, самопроизвольный Stop, lost durable source, false receipt/played, бесконечный reconnect/Start, пропуск целых пользовательских реплик, stale autoplay прошлого разговора, пробой actor scope, отсутствие точной версии телефона/сервера. CAPTION_CLOCK_UNVERIFIED означает отдельный незавершённый UI/ASR gate; честно показывать disabled, не подменять финальный текст.

## 10. Внешние первоисточники — только дополнительная навигация, не обязательны Opus

Они подтверждают происхождение настоящих физических аудио и логов, но **критически необходимые цифры и выводы уже внесены в разделы 2–9**. У Opus может не быть доступа к Slack — это не препятствие чтению досье. Ничего из приватных вложений *не следует придумывать*.

- Отчёт о физической регрессии [v78](https://mira-projects.slack.com/archives/C0C872SJFA7/p1792506803193439?thread_ts=1792393120.356429).
- Основной [тред двух v79 прогонов](https://mira-projects.slack.com/archives/C0C872SJFA7/p1792354032050759): файл comprehensive-v79-two-runs-20261020.md; backend-archive projects-hub-v79-two-runs-20261020.zip; отдельно полный Android logcat projects-hub-v79-android-two-tests.zip; android-backend-correlation.md; audio-log-addendum.zip; active-output-interrupts-audio-candidates.md и две личные звуковые записи.
- Проверенный [Android/backend таймлайн](https://mira-projects.slack.com/archives/C0C872SJFA7/p1792527317593929?thread_ts=1792354032.050759) и [выравнивание 8 playback interrupts](https://mira-projects.slack.com/archives/C0C872SJFA7/p1792527573453339?thread_ts=1792354032.050759). Полных текстов частных реплик в публичном досье нет.
- [Каноническое состояние прошлых выпусков](../prompts/projects-hub-native-voice-canonical-handoff-20261020.md), [одна основная Android-линия](single-app-voice-20261020.md), [исторические numeric emulator evidence](evidence).

**Задание независимому Opus 5.5:** *самостоятельно* прочитать текущий код обоих репозиториев и изменения SDK PR70/product PR207, сверить их с цифрами разделов 2–5, классифицировать «доказано кодом / логом / гипотеза», предложить **минимальные точные P0/P1 патчи и тесты**, проверить происхождение ACK и provider admission, сохранить ресурсную надёжность без новой архитектуры, затем выдать приоритетный технический план, приводящий к реальной 90-минутной физической приёмке. **Не сообщать, будто проверено неизвестное приватное аудио.**
