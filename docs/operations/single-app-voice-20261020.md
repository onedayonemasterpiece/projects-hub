# Projects Hub: одна версия приложения и проверяемый голос

Решение владельца, 20 октября 2026, продолжение native voice handoff.

## Единственный пользовательский путь

Доводим один пакет `com.kenigevents.projectshub` с новым shared native SDK. После первой установки все следующие сборки приходят через существующий in-app updater (`releases/latest` → `update.json` → SHA-256 → системный установщик Android). Сохраняются сертификат, package ID и возрастающий versionCode. Метка GitHub latest означает канал доставки, **не завершённую продуктовую приёмку**. Нельзя называть прошлые незавершённые сборки готовым официальным продуктом.

Это прямое решение заменяет прежнее указание выпускать APK только как prerelease вне updater и просить владельца/dot устанавливать их вручную. `release_candidate` в evidence остаётся статусом неполной приёмки, а не вторым пользовательским каналом. Android debug разрешён только для изолированных CI/эмуляторных тестов; на телефон владельца его больше не поставляем.

Дубликат `.debug` не удалять автоматически: его локальные .wvs могут содержать неотправленные оригиналы. Удаление приложения и данных — отдельное действие владельца. Существующие аудиоисточники, авторизация, история и основной пакет сохраняются.

## Версии без догадок

Приложение сообщает собственные versionName/versionCode, package ID, исходный SHA и версию/SHA включённого SDK из BuildConfig. NativeSessionApi использует эти же значения в Start; MainActivity передаёт их UI. Backend сообщает свой реально запущенный release SHA и версию загруженного SDK. Мире запрещено называть backend_version версией приложения или брать running version из release feed. Старый клиент, не приславший метаданные, остаётся с неизвестными полями.

В раскрываемом контексте UI есть «Версии приложения и сервера». Сервер проверяется заново при открытии и возврате онлайн; при ошибке нельзя показывать устаревшее подтверждение. Эти поля — только диагностика, никогда не полномочия.

## Отладка и порядок поставки

Использовать текущий PR и канонический live-interaction, не переписывать transport. После backend/PWA/Android тестов на точном кандидате включить его в main, проверить ancestry относительно работающего релиза, выполнить штатный exact SHA installer с сохранением releases, проверить process/health/SDK. Подписанная сборка той же линии публикуется существующим workflow с проверкой аудио, package/cert и manifest; readback обязан доказать, что именно она стала `releases/latest`. Нельзя снижать waveform threshold ради публикации.

Готовность к тестированию не означает уже пройденный Samsung long soak. Фиксировать отдельно: CI, реальный provider, пакет/версия на телефоне, слышимость, речь/текст/перебивание/Stop, восстановление и 90 минут. Сравнение делается по паре app+backend, а не по слову debug/official.

## Проверенные внешние рекомендации (20.10.2026)

- [Google Live best practices](https://ai.google.dev/gemini-api/docs/live-api/best-practices): небольшие аудиопакеты; не задерживать передачу секундной предбуферизацией; прерывание отменяет очередь воспроизведения, но не удаляет durable original; длительность обеспечивают compression/resumption/GoAway, а не бесконечное создание новых сессий. Формат провайдера не меняет согласованный Opus/WSS телефон↔наш backend.
- [Android AudioTrack](https://developer.android.com/reference/android/media/AudioTrack): учитывать реальные short writes, start threshold, underrun и played position; Stop/flush не должен ждать долгой блокирующей записи под общим монитором. Точный played cursor не равен акустическому доказательству.
- [Android AcousticEchoCanceler](https://developer.android.com/reference/android/media/audiofx/AcousticEchoCanceler): эффект привязан к конкретному AudioRecord session; проверить поддержку, создание, успешное включение и actual state, а не предполагать качество по факту вызова create.
- [Google Live capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities): не смешивать manual activityStart/activityEnd с правилами automatic/hybrid VAD; проверять отправку начала/конца тихой речи и перебивания. Порог/паузы не менять вслепую по одному форумному примеру.

Форумные кейсы — гипотезы для проверки на нашем устройстве, не доказательство нашей первопричины. Повторяемый CI speaker mismatch разбирается по оригинальному WAV и локальным лагам, а не скрывается повторными прогонами или ослаблением корреляции.

## Samsung v78: независимый критический аудит Opus 5.5, 20.10.2026

Физический тест v78 на Samsung показал регрессию. [Отчёт устройства](https://mira-projects.slack.com/archives/C0C872SJFA7/p1792506803193439). Opus 5.5 проверил исходники и выявил три **независимые** цепочки отказа; результаты — предложения для проверки, не автоматически принятый диагноз.

1. Android LIVE_NATIVE_STORAGE_BACKPRESSURE: захват обгонял кодирование/долговременную запись при одновременном проигрывании; один synchronized NativeVoiceStore закрывал и открывал разные журналы. Продукт ограничил LRU четырьмя дескрипторами без отключения fsync. Shared SDK добавил ограниченное ожидание 100 мс и audio-приоритет кодировщику. ACK по-прежнему только после сохранения.
2. Серверный LIVE_AUDIO_BACKPRESSURE: в логах было 31 из 32 элементов очереди при нескольких килобайтах данных. Для 10-мс Opus-пакетов такой лимит меньше согласованного окна 960 мс. SDK rc15 сделал ограниченные слоты по длительности (104 live, 56 recovery) при прежних byte/time/age квотах.
3. После разрыва и reconnect — LIVE_AUDIO_OUTPUT_SEQUENCE. SDK rc14 изолирует ошибку одного восстанавливаемого output, rc15 не допускает fatal от запоздалого ACK уже несвязанного источника, rc16 закрывает codec epoch на generation_complete и отдельно сохраняет A/B после пропущенного или позднего terminal. Выходные оригиналы не удаляются и не считаются проигранными автоматически.

Ранее 90-минутная непрерывная запись 20-мс пакетами упиралась в 250 000 чанков примерно на 83-й минуте. Лимит продукта увеличен до 400 000; общие ограничения (4 млн чанков, длительность, байты) неизменны. Это исправление верхней границы, не доказательство 90-минутной производительности.

**Приёмка:** Start/Stop, 10+ полноценных реплик; длинные ответы/перебивание; A→generationComplete→B; A→потерянный terminal→B→поздний terminal A; входной 10/20-мс Opus при ограниченном IO; многократный reconnect/GoAway; 90 мин на Samsung (тишина, диалог, Wi-Fi↔LTE/BT); до четырёх реально независимых голосовых сессий и RKB-нагрузка. Нельзя называть продукт полностью принятым по одному успешному CI. Отдельные captions пока CAPTION_CLOCK_UNVERIFIED.

**Запрет:** drop захваченной речи, выключение fsync, ACK/played до подтверждения, снятие порогов waveform, бесконечные очереди, повторное семантическое выполнение после replay, автоматическая очистка .wvs или установка debug на устройство владельца.

## v79: фактическая поставка и незавершённая физическая приёмка — 20.10.2026

**Опубликовано:** [один Android v79](https://github.com/onedayonemasterpiece/projects-hub/releases/tag/android-v79) 0.1.62+79, `com.kenigevents.projectshub`, прежняя подпись; release workflow [39466068930](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39466068930) SUCCESS, [встроенное обновление 39466390713](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39466390713) SUCCESS. APK SHA-256 `36c9571b5291b20a2e28c2832da78a3a3a6a54a0fe8e7b719aac231bc268bcc8`. Backend установлен штатным installer на `aacce79d010a91a694cdd0a033953d87c9ae0141`, SDK `0.4.0-rc.16` source `f3c6831ebd2d1fba1381e16a06a8911eedf2a4fd`; `/healthz` HTTP 200, restart count 0, прежние релизы сохранены. [Android emulator audio 39463173877](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39463173877) SUCCESS — 6 instrumentation / 12 audio checks; это **управляемый fixture, не Samsung**.

**Реальный Google Live, wl-live-v2, источник ровно из Android CI:** неизменяемые синтетические 816 Opus чанков / 16.30 с на каждую попытку; проверено две WSS generation в одном logical Live session. В обеих каждый пакет `816/816` server-durable, source sealed и 2 input final / 2 output terminal; проверены реальные аудиоответы и stop без фатального `OUTPUT_SEQUENCE`. Эти сведения получены из изолированного отчёта `.dependencies/native-v79-provider-20261020-run3.json` (SHA-256 `2f5531160652cfed7533ccd3bed4163dce1a8219d8ccbccd4d500bed9962bd94`) и отдельного read-only SQLite snapshot: два complete source, 816 chunks, 229760 PCM projection bytes каждый. Тест **primary_passed=false**: Google дважды сократил первую ожидаемую русскую фразу; вторая совпала. Испытание `diagnostic_continue_on_transcript_mismatch` строго отделяет получение вторых WSS receipts от авторитетной текстовой приёмки, никогда не превращает несовпадающий текст в PASS. `CAPTION_CLOCK_UNVERIFIED` остается; sidecar отключён для native до проверенной шкалы времени. Не создаётся скрытый ASR или semantic replay.

**Физический blocker:** dot [отчитался в Samsung-треде](https://mira-projects.slack.com/archives/C0C872SJFA7/p1792393120356429), что в 17:40:06 UTC `adb devices` видел **0 устройств**. Установленный на Samsung versionCode/подпись v79 не прочитаны. Ручная установка, clearing/reinstall старой `.debug`, microphone action и сетевые переключения не выполнялись. Следующие реальные проверки: установить v79 через **существующий** in-app updater; сверить точные APK+backend SHA; 15–20 минут разносторонней беседы и barge-in; managed transport reconnect/GoAway и корректная транскрипция; затем полноценные 90 минут на Samsung и mixed load 4 одновременно (с RKB), с метриками задержек/потерь, без снижения порогов. Пока их нет, итог **P0 открыт, не принято**, несмотря на GitHub latest и успешный эмулятор.

**Отдельное замечание по Google:** в [официальных Live Transcription рекомендациях](https://ai.google.dev/gemini-api/docs/live-api/live-transcribe) dedicated model `gemini-3.5-transcribe-live` поддерживает режим `VERBATIM`, а conversational Live Agent и отдельный Transcription — разные режимы. Нельзя объявлять truncated canonical Live final полной дословной записью или заменять semantic input сторонней строкой без доказанной attribution. VAD/low-latency на Android менять только после измерений и исходного PCM: произвольное смягчение порога может создать эхо и ложные speech turns.
