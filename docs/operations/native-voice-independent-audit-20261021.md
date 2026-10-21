# Projects Hub / Мира — независимый критический аудит native voice (21.10.2026)

**Автор:** Claude Opus 5.5, прямое чтение исходников обоих репозиториев (не `provided_only`).
**Ответ на:** [комплексное досье 21.10](native-voice-comprehensive-audit-handoff-20261021.md).
**Статус продукта не меняется: НЕ ПРИНЯТ.** Этот документ не является физической приёмкой; приватные аудио, logcat и Slack-вложения я **не** видел.

## 0. Итог

1. **rc18 / PR207 корректны** (receipt строго после одного fsync, сбой записи помечает store непригодным, OVERRUN-проверка привязки добавлена). Мержить можно. Но они чинят только конкуренцию fsync на Android, а найденные ниже блокеры находятся **на сервере** и в учёте памяти телефона.
2. **Новый P0, подтверждён измерением:** каждый входящий 20-мс кадр выполняет на сервере **три SQL-запроса со стоимостью O(длина источника)**. Один источник живёт от подключения до переподключения. На 90-й минуте это **~150 мс чистого SQL на кадр** при бюджете 20 мс, всё под **глобальным RLock всего продукта**. Звонок в 90 минут без переподключений невозможен даже на идеальной сети, а четыре одновременных пользователя упираются в потолок примерно через 3 минуты непрерывного источника. Патч и регрессионный тест прилагаются (§3.1, приложение A).
3. **Новый P0, подтверждён кодом:** голосовой store использует **тот же SQLite-коннект и тот же `threading.RLock`, что и весь продукт** (около 320 мест с `with …_lock`). При этом event-loop синхронно вызывает store из `on_event` (каждый фрагмент транскрипта пишется с FULL fsync) и из **больше чем 50 `async def` HTTP-маршрутов**, включая `/healthz`. Медленный коммит голоса замораживает event-loop всех сессий: ping, ACK и чтение WSS. Метрики lag event-loop нет нигде.
4. **Подтверждено кодом:** сервер отправляет `server_durable` ACK, **а потом** повторно проверяет возраст кадра и может бросить `LIVE_AUDIO_STALE`. Сокет падает. Остаток живого источника становится recovery-only, а recovery-лейн **никогда не проецируется в Google**. Кроме того, `close()` при открытой реплике шлёт провайдеру `activity_end`. В итоге модель отвечает на половину вопроса, а хвост фразы сохранён, но не услышан. Это кодовый механизм того, что досье описывает как «Мира ответила на микропаузе», но атрибуцию к конкретным событиям v79 нужно ещё доказать.
5. **Подтверждено кодом:** якорь часов источника на сервере ставится **после** `await store.open_source()`. Задержка store превращается в постоянное отрицательное смещение возраста всех кадров этого источника, а диагностика `capture_age_ms=max(0, …)` **прячет знак**. Правка в две строки.
6. **Подтверждено кодом:** Android держит **не больше 32 неподтверждённых кадров на live и recovery вместе**. Сервер в rc15 поднял ту же границу до 104/56, а клиент нет. При 10-мс пакетах это 320 мс аудио: любая задержка ACK дольше этого останавливает живую речь, а выгрузка recovery отнимает у неё слоты. Это кодовый механизм reconnect storm (§3.7).
7. **Подтверждено кодом:** на телефоне **ни один продакшн-путь не вызывает `release()`**. Входные и выходные журналы `.wvs` копятся через все звонки и дни. Лимит 128 MiB / 4096 источников общий и накопительный. По оценке, его хватает на ~2,5 ч суммарных разговоров. Перед любым 90-минутным тестом нужно считать текущий объём на S21. Требуется одно решение владельца (§6).

## 1. Что проверено

| Объект | SHA / версия |
| --- | --- |
| Projects Hub `origin/main` | `c1133029ae2693c1e240ab1ccb6c22c82162ad56` (досье) |
| Projects Hub PR207 head | `d6ab8158b738d1d36db14b6a1988bc9aeec6e80d`, **все 4 проверки SUCCESS** (build, backend, emulator-voice API 31, pwa), на 21.10 ~12:00 UTC **не смержен** |
| live-interaction `main` = `v0.4.0-rc.18` | `e710e8e8143a3724d16250a6e439ccd178c279ca` (PR70 merged 10:44 UTC) |
| rc17 → rc18 diff | `404b013..e710e8e`: `NativeEncoder`, `NativeLiveRuntime`, `DurableSourceStore` |

**Классы доказательства:** **[код]** — механизм следует из исходника. **[измерено]** — воспроизведено мной на devserver. **[гипотеза]** — согласуется с цифрами досье, но не доказано по конкретному event/response ID.

**Ограничения:** journald сервиса мне недоступен (DevCoveer MCP не авторизован, у локального пользователя нет `systemd-journal`). Поэтому цифры v79 взяты только из досье. Бенчмарк SQL синтетический: горячий page cache, `synchronous=OFF`, без конкурентной нагрузки. Значит, реальные цифры с двумя FULL fsync на кадр **хуже**, а не лучше. Скрипты и вывод сохранены на devserver: `/home/dev/artifacts/projects-hub/20261021T120052Z-voice-indep-audit-sql-bench-20261021/` (retained).

## 2. Сверка досье с кодом

| Утверждение досье | Вердикт | Уточнение |
| --- | --- | --- |
| §1: PR207 Android CI «ещё выполнялся» | **Устарело** | Все 4 проверки на `d6ab815` SUCCESS. Не смержен, не установлен, физически не проверен. |
| §5.1: «SQLite connection единая, RLock, WAL, FULL» | **Верно, но сильно недооценено** | Это не голосовой store, а **весь продуктовый `DurableStore`** ([store.py#L47-L51](https://github.com/onedayonemasterpiece/projects-hub/blob/d6ab8158b738d1d36db14b6a1988bc9aeec6e80d/src/projects_hub/store.py#L47-L51)). Его лок разделяют timeline, проекты, MCP, HTTP и Live. |
| §5.1: «2 worker threads, pending=8» | **Верно, но вводит в заблуждение** | Оба потока берут один RLock, поэтому фактически параллелизм равен 1. Второй поток только держит слот семафора ([live_runtime.py#L101](https://github.com/onedayonemasterpiece/projects-hub/blob/d6ab8158b738d1d36db14b6a1988bc9aeec6e80d/src/projects_hub/live_runtime.py#L101)). |
| §5.1: «лимит 250 000 → 400 000, но это не проверяет дисковые квоты» | **Верно, но настоящий 90-минутный ограничитель другой** | Стоимость кадра растёт линейно и упирается в 20-мс бюджет примерно на 12-й минуте одного источника (§3.1), задолго до 400 000. |
| §5.1: «server-durable ACK на кадр, уже признанный STALE» | **Подтверждено [код]** | [audio_socket.py#L415-L426](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/python/live_interaction/audio_socket.py#L415-L426): commit → ACK → `_age()` → raise. Ещё одна проверка стоит после decode ([L446](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/python/live_interaction/audio_socket.py#L446)). |
| §5.1: «повторно отправленный committed chunk не проходит decode» | **Подтверждено [код]** | [L423](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/python/live_interaction/audio_socket.py#L423): `if source.recovery or not commit.newly_committed: return`. Recovery-лейн в принципе не проецирует. |
| §5.1: device STORE_LIMITS 128 MiB / 4096 / 120 мин | **Подтверждено, и это хуже, чем описано** | Лимиты считаются по **всем** `.wvs` всех scope при открытии ([NativeVoiceStore.java#L139-L144](https://github.com/onedayonemasterpiece/projects-hub/blob/d6ab8158b738d1d36db14b6a1988bc9aeec6e80d/android/app/src/main/java/com/kenigevents/projectshub/NativeVoiceStore.java#L139-L144)) и никогда не уменьшаются (§3.6). |
| §4.1: CAPTION_CLOCK_UNVERIFIED — детерминированный gate | **Подтверждено [код]** | `live_transcription.py#L116-L117`. |
| §5.2: rc18 — «flush по EOS/turn/idle» | **Верно** | Flush срабатывает при 6 страницах, ≥100 мс аудио, флагах `PORTION_START/END/EPOCH_END`, дедлайне 80 мс и в `finish()`. Нюанс описан в §4.5. |
| §3.1: STALE в 18:43:07.554 / 19:39:21.540 как причина detach | **Скорее следствие [гипотеза]** | См. §5: в обоих тестах Android `LIVE_NATIVE_LIVE_BACKLOG_RECOVERY` стоит на 0,37–0,52 с **раньше** серверного STALE. |

## 3. P0 — новые находки и точные исправления

### 3.1. P0-SQL: три запроса O(n) на каждый кадр [измерено + код]

**Где** (PR207 head):
- [encoded_voice_store.py#L359](https://github.com/onedayonemasterpiece/projects-hub/blob/d6ab8158b738d1d36db14b6a1988bc9aeec6e80d/src/projects_hub/encoded_voice_store.py#L359): `SELECT MIN(chunk_seq),MAX(chunk_seq) … WHERE source_id=? AND epoch_id=?`. SQLite сводит к index seek только **одиночный** MIN или MAX. Оба в одном SELECT сканируют весь диапазон индекса.
- [#L364](https://github.com/onedayonemasterpiece/projects-hub/blob/d6ab8158b738d1d36db14b6a1988bc9aeec6e80d/src/projects_hub/encoded_voice_store.py#L364): `… chunk_seq BETWEEN low AND high AND epoch_id<>? LIMIT 1`. Совпадений нет, поэтому `LIMIT 1` не срабатывает и сканируется вся эпоха.
- [#L480](https://github.com/onedayonemasterpiece/projects-hub/blob/d6ab8158b738d1d36db14b6a1988bc9aeec6e80d/src/projects_hub/encoded_voice_store.py#L480) (`claim_projection`, второй COMMIT того же кадра): `MIN(capture_start_us)`. Индекс `(source_id,epoch_id,chunk_seq)` не покрывает `capture_start_us`, поэтому это тоже скан.

**Почему растёт:** `NativeEncoder` создаёт **одну** codec epoch на источник ([NativeEncoder.java#L31](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/android/platform/src/main/java/org/onedayonemasterpiece/live/platform/NativeEncoder.java#L31)). Источник ротируется только при переподключении. Кадры идут непрерывно, в том числе в тишине, по 50 в секунду.

**Измерение** (точная схема `initialize_encoded_voice_schema`, SQLite 3.46.1, одна эпоха):

| Кадров в источнике (≈ минут) | `MIN,MAX`+`BETWEEN`, мс | `MIN(capture_start_us)`, мс | Итого на кадр | После патча |
| --- | --- | --- | --- | --- |
| 10 000 (3,3) | 2,7 | 1,7 | **4,4 мс** | ~0,02 мс |
| 50 000 (16,7) | 16,9 | 11,1 | **28 мс** | ~0,02 мс |
| 150 000 (50) | 50,4 | 35,6 | **86 мс** | ~0,02 мс |
| 270 000 (90) | 94,3 | 59,1 | **153 мс** | ~0,02 мс |

Индексные seek'и после патча стоят ~0,005 мс каждый, без роста. Детерминированная метрика (инструкции SQLite VM на commit+claim одного кадра): **2 → 7 602** сотен инструкций между 2-м и 40 003-м кадром до патча и практически без роста после.

**Следствие [код + арифметика]:**
- Один пользователь: 20-мс бюджет исчерпывается примерно на **36 000 кадрах, это ~12 минут** непрерывного источника. Дальше ACK отстаёт, Android достигает порога в 2 с (`LIVE_NATIVE_LIVE_BACKLOG_RECOVERY`), происходит переподключение, источник обнуляется, и цикл начинается заново. Система «самоизлечивается» обрывом примерно каждые 10–15 минут.
- Четыре пользователя под одним RLock делят бюджет: ≤5 мс на кадр, потолок около **3 минут** непрерывных источников.
- Под тем же локом всё время, пока идёт запрос, ждут коммиты выходного архива, timeline и HTTP (§3.2).

**Для v79 это не основная причина:** на 2-й минуте стоимость ~2,5 мс на кадр. Это заметный джиттер, но не насыщение. Блокер касается 90 минут и четырёх пользователей.

**Исправление:** [приложение A](#приложение-a-патч-p0-sql-и-регрессионный-тест). Семантика инварианта сохранена: эпоха занимает непрерывный интервал `chunk_seq`, а ближайшие соседи `previous`/`following` уже выбираются. Новая проверка строже старой в одном краевом случае: старый код пропускал первую строку новой эпохи внутри чужого интервала. Все 21 существующих теста `test_encoded_voice_store.py` проходят, а новый тест на исходном коде падает с `(2, 7602)`.

### 3.2. P0-LOCK: глобальный RLock продукта и синхронный store на event-loop [код; величина не измерена]

**Где:**
- `adapter.on_event` вызывается синхронно на event-loop ([session_host.py#L347-L360](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/python/live_interaction/session_host.py#L347-L360)). В продукте он делает `store.append_voice_timeline_event(...)` / `append_source_event(...)` для каждого `input_transcript`, `output_transcript`, `interim_input_transcript`, `turn_complete`, `interrupted` ([live_adapter.py#L2087-L2139](https://github.com/onedayonemasterpiece/projects-hub/blob/d6ab8158b738d1d36db14b6a1988bc9aeec6e80d/src/projects_hub/live_adapter.py#L2087-L2139)). Это SQLite с FULL fsync под тем же RLock.
- В `app.py` больше 50 маршрутов объявлены как `async def`, и многие из них вызывают store напрямую, без `to_thread`/threadpool. Среди них `/healthz` → `store.ping()` ([app.py#L505](https://github.com/onedayonemasterpiece/projects-hub/blob/d6ab8158b738d1d36db14b6a1988bc9aeec6e80d/src/projects_hub/app.py#L505)) и timeline, который WebView читает во время звонка. В `app.py` и `live_adapter.py` нет ни одного `to_thread`/`run_in_threadpool`.

**Механизм:** поток `voice-source` держит RLock во время коммита. Досье фиксирует output-archive commit до 869/1148 мс, плюс §3.1. В это время event-loop блокируется на том же RLock. Останавливаются все сессии и все сокеты: `pong`, `audio_ack`, `output_*`, чтение WSS. Это ровно та картина «ACK не приходит, поэтому backlog растёт», которая приводит к `BACKLOG_RECOVERY`/STALE.

**Исправления (минимальные, без новой архитектуры):**
1. **P0, сейчас:** у HTTP-хендлеров без `await` поменять `async def` на `def`. FastAPI сам унесёт их в threadpool. Это механическая правка, начать с `/healthz`, timeline GET/PUT, `bootstrap`, `conversation`, `sources`.
2. **P0, сейчас (измерение):** обёртка `InstrumentedRLock` вокруг `DurableStore._lock` с `acquire_wait_ms` и `hold_ms`, имя операции брать из стека/контекста. Плюс sampler event-loop lag: задача `sleep(0.1)` и разница со временем; логировать p50/p99/max раз в минуту и любой выброс ≥100 мс. PR207 `live_voice_io_contention` меряет только семафор и полное время, но **не** ожидание RLock внутри потока и не lag event-loop.
3. **P1, после метрик:** `on_event`. Нельзя просто вернуть awaitable: хост сделает `create_task` и спроецирует событие клиенту раньше записи, а в продукте `event["timeline_message"]` нужно до проекции. Правильно делать это в SDK: поддержать *упорядоченный* awaitable `on_event`, когда хост ждёт его перед проекцией в одном per-session порядке. В продукте тогда пишем через `io.run`. Если после §3.1 и §3.5 RLock держится ≤10 мс, это может оказаться P2 — решать по метрике п.2.
4. **P1:** вынести голосовые таблицы в **отдельный файл SQLite со своим коннектом и локом**. Схема, FULL и WAL не меняются, это существующая БД, разделённая на два файла. Голос перестаёт конкурировать с остальным продуктом и RKB-подобными соседями. Делать только если метрика п.2 покажет, что после §3.1/§3.5 конкуренция остаётся.

### 3.3. P0-STALE-ACK: STALE после durable ACK рвёт сокет и навсегда отрезает хвост реплики от модели [код]

**Цепочка:**
1. `_chunk`: `commit_chunk`, затем `audio_ack server_durable`, затем `_age()` выбрасывает STALE ([audio_socket.py#L415-L426](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/python/live_interaction/audio_socket.py#L415-L426)). `_worker` переводит `failed` в результат, и сокет закрывается.
2. `SocketBinding.close()`: если реплика открыта, провайдеру уходит `activity_end`/`audio_stream_end` ([socket_host.py#L404-L422](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/python/live_interaction/socket_host.py#L404-L422)). **Google получает конец хода на середине фразы и отвечает на услышанную часть.**
3. Android: после переподключения прежний источник становится recovery ([NativeLiveRuntime.java#L693](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/android/platform/src/main/java/org/onedayonemasterpiece/live/platform/NativeLiveRuntime.java#L693)). Сервер для recovery-лейна только сохраняет ([L423](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/python/live_interaction/audio_socket.py#L423)). **Хвост фразы плюс весь backlog плюс разрыв (0,5–10 с бэкоффа) сохранены побайтно, но модель их не слышит никогда.**

Это кодовое объяснение пользовательского симптома из досье §3.2 («реагирует на микропаузу… затем уступает продолжившейся речи»). **[гипотеза]** для конкретных событий v79: нужна корреляция по `response_id`/`turn_serial` (§7).

**Исправление (SDK rc19, обе стороны, согласуется через hello-capability `live_source_close: 1`, старые клиенты сохраняют текущее поведение):**
- В `_chunk` после ACK не выбрасывать STALE. Вместо этого вызвать `_close_live_projection(source, reason)`. Он закрывает открытую portion провайдеру ровно один раз, делает `decoder.close()`, `decoder=None`, `decoder_ended=True`, `portions.clear()`, освобождает `live_ref` и помечает `source.projection_closed=True`. Затем пишет диагностику `socket_live_projection_closed` (знаковый возраст, `projected_through`, generation) и отправляет клиенту `{"type":"source_live_closed","source_id":…,"projected_through":N,"code":"LIVE_AUDIO_STALE"}`.
- `audio()`: для `projection_closed` источника кадры с live-флагом принимаются **только в store** (commit + ACK, без `_age`, без decode). Иначе Android, который ещё не обработал `source_live_closed`, получит `GENERATION`.
- Android: на `source_live_closed` выставить `run.liveSourceId=null` и `run.rotation.incrementAndGet()`. Энкодер штатно ротирует источник, старый дописывается как recovery. **WSS и Google-сессия остаются живы, reconnect storm не возникает.**
- Тесты: матрица §7 №1, №12, №17 из досье плюс новый: «STALE на кадре k после ACK → нет detach, k..n durable, `projected_through=k-1`, следующий live-источник проецируется».

Что делать с непроецированным хвостом, решает продукт. См. вопрос владельцу №2 (§6) и P1-GRACE (§4.6).

### 3.4. P0-CLOCK: якорь часов источника ставится после медленного store, знак возраста скрыт [код]

- Android отправляет `source_time_now_us` = сэмпловые часы в момент `openSource` ([NativeLiveRuntime.java#L693](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/android/platform/src/main/java/org/onedayonemasterpiece/live/platform/NativeLiveRuntime.java#L693), [L749](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/android/platform/src/main/java/org/onedayonemasterpiece/live/platform/NativeLiveRuntime.java#L749)).
- Сервер ставит `registered_at=time.monotonic()` **после** очереди read-ahead, очереди lane 0 (за кадрами прежнего источника) и `await self.store.open_source()`, а это два FULL-коммита под общим RLock ([audio_socket.py#L318-L327](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/python/live_interaction/audio_socket.py#L318-L327)).
- Пусть D — эта задержка. Тогда `age = истинный_возраст − D` для **всех** кадров источника. Пока идёт backlog, ошибка маскируется. Когда очередь опустела, свежие кадры (истинный возраст ~100–200 мс) получают `age < −250 мс`, срабатывает STALE «часы клиента впереди». Это верно при D ≳ 0,4 с, что достижимо при задержках, которые фиксирует досье.
- Диагностика `capture_age_ms=max(0, age_us // 1000)` ([L270](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/python/live_interaction/audio_socket.py#L270)) превращает этот случай в «0». Поэтому по текущим логам «опоздал» и «впереди» неразличимы.

**Исправление (rc19, ~10 строк):**
```python
# socket_transport.read_loop, ветка v2 control:
if message.get("type") == "source_open":
    message["server_received_monotonic"] = time.monotonic()   # перезаписывает клиентское значение
stage_v2_input(message, len(frame.encode("utf-8")))

# audio_socket._control("source_open"):
received = message.get("server_received_monotonic")
anchor = received if isinstance(received, float) else time.monotonic()
source = _Source(bound, recovery, source_time, anchor, ...)

# audio_socket._age, диагностика:
capture_age_ms=age_us // 1000,            # знаковое
clock_ahead=age_us < 0,
```
Остаточное смещение равно односторонней сетевой задержке. Оно положительное и малое, поэтому допустимо. Тест: `open_source` с искусственной задержкой 900 мс, затем поток свежих кадров. Сейчас ожидается STALE, после правки STALE быть не должно.

### 3.5. P0-UPLINK-COMMIT: на сервере два FULL-коммита на каждый 20-мс кадр [код]

Сейчас на каждый кадр выполняется `commit_chunk`, то есть `BEGIN IMMEDIATE…COMMIT` ([#L390](https://github.com/onedayonemasterpiece/projects-hub/blob/d6ab8158b738d1d36db14b6a1988bc9aeec6e80d/src/projects_hub/encoded_voice_store.py#L390)), а затем `claim_projection`, ещё один `BEGIN IMMEDIATE…COMMIT` ([#L486](https://github.com/onedayonemasterpiece/projects-hub/blob/d6ab8158b738d1d36db14b6a1988bc9aeec6e80d/src/projects_hub/encoded_voice_store.py#L486)). Это **100 fsync в секунду на одного говорящего**, четыре пользователя дают 400/с, и всё это сериализовано RLock. rc18 уже шлёт кадры с телефона пачками по ≤6, так что пачки приходят на сервер естественно сгруппированными.

**Исправление (rc19 + продукт, по образцу rc17 output batch):**
- SDK `_worker(lane 0)`: после `get()` неблокирующе добрать в локальную очередь до 5 следующих `AudioFrame` того же `stream_ref/epoch_ref` с непрерывным `chunk_seq`. Control-сообщения не перепрыгиваются: на первом не-кадре добор останавливается. Затем `store.commit_chunks(bound, epoch, chunks)`, опциональный метод с fallback на цикл `commit_chunk`.
- Продукт `_commit_chunks`: один `BEGIN IMMEDIATE`, для каждого кадра `_commit_chunk(..., in_transaction=True)` (параметр уже есть), один `COMMIT`. ROLLBACK выполняется под `if db.in_transaction`, как в PR207.
- ACK отправляется **один раз**, на максимальный `server_durable_seq`, и только после COMMIT. Контракт «ACK ≤ durable» не меняется.
- Затем decode и `_project` по каждому кадру по порядку. `claim_projections` для группы одной транзакцией.
- Пейсинг (`next_work_at`) считать по суммарной длительности группы.
- Тесты: EIO на COMMIT → ни одного ACK; смешанная группа «дубликат + новый хвост»; control между кадрами не переупорядочивается; группа на границе `EPOCH_END`.

### 3.6. P0-DEVICE-QUOTA: журналы на телефоне никогда не освобождаются [код + оценка]

- `NativeVoiceStore.release()` существует ([#L446-L470](https://github.com/onedayonemasterpiece/projects-hub/blob/d6ab8158b738d1d36db14b6a1988bc9aeec6e80d/android/app/src/main/java/com/kenigevents/projectshub/NativeVoiceStore.java#L446-L470)), но его не вызывает ни один продакшн-путь ни в продукте, ни в SDK. `disposition()` тоже никто не вызывает. Выходные источники `release` отвергает в принципе.
- При открытии `journalBytes`/`sourceCount` суммируются по **всем** `.wvs` всех scope ([#L139-L144](https://github.com/onedayonemasterpiece/projects-hub/blob/d6ab8158b738d1d36db14b6a1988bc9aeec6e80d/android/app/src/main/java/com/kenigevents/projectshub/NativeVoiceStore.java#L139-L144)). Лимит 128 MiB / 4096 ([NativeVoiceService.java#L41-L42](https://github.com/onedayonemasterpiece/projects-hub/blob/d6ab8158b738d1d36db14b6a1988bc9aeec6e80d/android/app/src/main/java/com/kenigevents/projectshub/NativeVoiceService.java#L41-L42)) **накопительный за всё время жизни данных приложения**.
- **Оценка (не измерение):** запись CHUNK = 2×UUID (sourceId, epochId) + 28 байт метаданных + Opus 24 кбит/с × 20 мс ≈ 60 байт + 36 байт обвязки, итого **~200 байт на 20 мс, ~36 МБ на час** микрофона, который пишет и тишину. Выход и PLAYED добавляют ещё 10–20 МБ/ч. **128 MiB хватает примерно на 2,3–2,8 ч суммарных разговоров**, после чего `VOICE_STORE_QUOTA` и `LIVE_NATIVE_SOURCE_STORE_FAILED`. На S21 уже накоплены тесты v74–v80, а `pm clear` запрещён. **90-минутный тест может упасть по квоте, не дойдя до сети.**

**Исправление:**
1. **До любого длинного теста:** штатный in-app diagnostics readback `journalBytes`, `sourceCount`, число input/output источников. Без удаления.
2. По решению владельца (§6, вопрос 1) освобождать **входной** источник, только когда выполнено всё сразу: `sealed && serverSealed && committed == chunks`, сервер вернул `complete=true` и SHA-256 всего оригинала совпал с локальным. После этого записать `disposition("server_verified")` и вызвать `release()`. Это соответствует правилу devserver «оригинал хранится до проверенного receipt и SHA-256».
3. **Выходной** источник — это кэш серверного оригинала. Освобождать после `completed` playback и серверного receipt played-курсора. Риск повторного воспроизведения отсутствует, потому что сервер уже фильтрует по сессии (rc13).
4. Тест: 50 циклов «звонок 2 мин → seal → verify → release», после них `journalBytes` возвращается к исходному. Отдельно: квота не может уменьшиться для источника без серверного SHA.

### 3.7. P0-CLIENT-WINDOW: Android держит не больше 32 неподтверждённых кадров на все источники сразу [код]

- [LiveSocketTransportV2.java#L158-L160](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/android/src/main/java/org/onedayonemasterpiece/live/LiveSocketTransportV2.java#L158-L160): `|| pending.size() >= 32) return false;`. Это тот же класс дефекта, который rc15 исправил **на сервере** («32 слота при 10-мс Opus»), но на клиенте лимит остался. `pending` общий для live и recovery.
- При 20-мс пакетах окно равно 640 мс аудио, при 10-мс пакетах (досье §2.3 относит их к uplink) — **320 мс**. Пропускная способность uplink ≤ 32 кадра за время «RTT + серверный commit». Любая задержка ACK дольше 320 мс сразу останавливает отправку живой речи, backlog начинает расти в реальном времени, на 2 с срабатывает `BACKLOG_RECOVERY`.
- После переподключения recovery-кадры занимают те же 32 слота. Живой источник идёт первым в каждом тике `pump` (40 мс), но получает только освобождённые слоты. Это **кодовый механизм reconnect storm**: живая речь стареет, пока выгружается backlog, затем снова recovery. Ср. досье §2.2: 12 WSS за 88 с.

**Исправление (rc19, Android):** брать ёмкость из согласованных `audio_limits` по той же формуле, что на сервере (`_audio_item_capacity`: `min(128, max(32, duration_us/10_000 + 8))`), **раздельно** для live (104) и recovery (56). Учитывать `pending` по лейнам. Байтовые и временные лимиты не меняются, как и правило «ACK ≤ durable». Тест: RTT 400 мс на 10-мс пакетах — сейчас ожидается `BACKLOG_RECOVERY`, после правки его быть не должно. Параллельная выгрузка recovery не должна задерживать live дольше одного тика `pump`.

**Связанная проверка (P0, только данные):** реальная длительность uplink-пакета на S21 по `encoded_voice_chunks.duration_us` для источников v79/v80. Если пакет 10 мс, то: (а) 90 минут — это **540 000** кадров, и `source_chunks=400_000` ([encoded_voice_store.py#L104](https://github.com/onedayonemasterpiece/projects-hub/blob/d6ab8158b738d1d36db14b6a1988bc9aeec6e80d/src/projects_hub/encoded_voice_store.py#L105)) даёт `SOURCE_STORAGE_FULL` на **66:40**; (б) все «минуты» в таблице §3.1 делятся пополам; (в) серверу нужно 100 кадров/с, то есть 200 FULL-коммитов в секунду на одного говорящего без §3.5. Расчёт досье «90 мин = 270 000 chunks» верен только для 20-мс пакетов.

## 4. P1

### 4.1. P1-PACING: пейсинг 1,5× применяется к commit/ACK, а не только к декодированию [код]
`_worker` спит до `next_work_at` **до** `_chunk` ([audio_socket.py#L280-L285](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/python/live_interaction/audio_socket.py#L280-L285)), а `live_decode_us_per_second=1_500_000`. После серверной паузы S backlog рассасывается с чистой скоростью 0,5 с за секунду, то есть ещё ~2S секунд, всё это время с повышенным возрастом. Вместе с окном 960 мс un-ACKed на Android и порогом 2 с одной паузы ~1,3–1,5 с достаточно для `BACKLOG_RECOVERY`. **Правка:** ограничивать скорость только стадии `_project` (декодирование и подачу в провайдер), а commit/ACK не тормозить, их уже ограничивает store. Это делается вместе с §3.5. Сначала измерить, допускает ли Google подачу быстрее реального времени при догонке. Не поднимать 1,5 с / 2 с / 2,5 с таймауты.

### 4.2. P1-ARCHIVE-STOP: `OUTPUT_ARCHIVE_STALL` останавливает весь разговор [код]
`admit()` ждёт 5 с, затем `_fail()` и `host.stop()` ([audio_output.py#L133-L140](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/python/live_interaction/audio_output.py#L133-L140), [L167-L181](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/python/live_interaction/audio_output.py#L167-L181)). Под конкуренцией за RLock (§3.1/§3.2) это прямой путь к «самопроизвольному Stop». **Правка (в духе rc14):** помечать сбойным только текущий ответ: `output_failed(response_id)`, закрыть его выходной источник, честный diagnostic. Входная сторона и логическая Live-сессия продолжают работать. Durable-receipt не выдаётся. Тест: commit выхода заблокирован на 6 с, ожидается сбой одного ответа, сессия жива, следующий ответ играет.

### 4.3. P1-ADMITTED: нет доказательства, что кадр дошёл до провайдера (вопрос досье №3)
Добавить в ACK стадию `provider_admitted`: `{"type":"audio_ack","stage":"provider_admitted","source_id","chunk_seq"}` после успешного `binding.input_pcm` последнего фрагмента кадра. Коалесцировать раз в 100 мс и обязательно отправлять на `PORTION_END`. Android хранит его отдельно от `serverDurable` (только для диагностики и UI, не для удаления). Тогда «сохранено, но не услышано» видно численно на каждом участке.

### 4.4. P1-METRICS: обязательные числа до следующего физического теста
Сервер: `store_lock_wait_ms` / `store_lock_hold_ms` по операции, `commit_ms` по направлению, `event_loop_lag_ms` p50/p99/max, high-water read-ahead и lane-очередей, знаковый `capture_age_ms`. Android: время `monitor_wait` и `fsync` в `NativeVoiceStore` (p50/p95/p99/max) отдельно для input/output/PLAYED, `pcm` queue high-water. Всё числовое, без текста и аудио.

### 4.5. P1-RC18-REVIEW: rc18/PR207 — замечания, которые не блокируют merge
- Корректно: receipt выдаётся только после единого `getFD().sync()`, `failed=true` ставится при любом сбое записи/apply, валидации `appendBatch` эквивалентны `appendChunk`, повтор идемпотентен, `LIVE_NATIVE_SERVER_DURABLE_OVERRUN` строго отвергает ACK больше device-durable (P0-6 досье).
- `NativeEncoder.finish()` вызывает `flushPending()` **после** проверок `ENCODER_TAIL_SHORT/OVERRUN` ([NativeEncoder.java#L163-L166](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/android/platform/src/main/java/org/onedayonemasterpiece/live/platform/NativeEncoder.java#L163-L166)). Если проверка не прошла, до 100 мс уже закодированных валидных оригиналов теряются из памяти. Ложного receipt при этом нет. Правка: вызвать `flushPending()` перед проверками хвоста. P2.
- Батчинг добавляет до 80–100 мс к задержке uplink до WSS и к `nativeTiming(speech_start)`. Это до 5 % 2-секундного бюджета. Локальный barge-in (`speechStarted`) не затронут. Учитывать при сравнении таймингов v80 и rc18.

### 4.6. P1-GRACE: что делать с хвостом фразы при обрыве (нужно решение владельца №2)
Сейчас `close()` сразу шлёт `activity_end` (§3.3). Альтернатива такая. При открытой реплике сервер ждёт переподключения той же логической сессии до N секунд (предлагаю ≤5 с), не закрывая ход. После переподключения он проецирует в провайдер durable-хвост *этого* источника (recovery-кадры с `seq > projected_through` и возрастом ≤ N + 2 с) с ускорением, затем `activity_end`. Если переподключения нет, всё как сейчас. Это продуктовое поведение («дослушать» или «ответить на услышанное»), поэтому без решения владельца не реализовывать.

## 5. Как читать события v79 с учётом кода [гипотеза]

| Пара событий | Android `BACKLOG_RECOVERY` | Сервер `socket_detached STALE` | Δ |
| --- | --- | --- | --- |
| Тест 1, recovery 1 | 18:43:07.170 | 18:43:07.554 | +0,38 с |
| Тест 1, recovery 2 | 18:46:02.495 | 18:46:02.865 | +0,37 с |
| Тест 2, recovery 1 | 19:39:21.018 | 19:39:21.540 | +0,52 с |

- Во всех трёх парах Android первым фиксирует, что неподтверждённый live-backlog старше 2 с ([NativeLiveRuntime.java#L712-L715](https://github.com/onedayonemasterpiece/live-interaction/blob/e710e8e8143a3724d16250a6e439ccd178c279ca/android/platform/src/main/java/org/onedayonemasterpiece/live/platform/NativeLiveRuntime.java#L712-L715)). Серверная метка STALE тогда **последствие**, а не первопричина: устаревшие кадры из очереди обработаны раньше, чем read-loop увидел закрытие. **Оговорка:** смещение часов телефона и сервера не измерено, и стабильные +0,4–0,5 с могут оказаться им. Различить можно по `socket_audio_stale_frame.capture_age_ms` (если в логе 0, это кандидат §3.4) и по причине закрытия в logcat.
- Первопричина в обеих интерпретациях одна: **ACK uplink задерживался больше чем на ~1 с подряд**. Тест 1 согласуется с конкуренцией за store: `archive_admission_wait` растёт 15 → 27 → 68 → 175 мс в 18:42:36–18:43:05.9, то есть выходной архив во время ответа модели коммитится всё медленнее, и через 1,3 с наступает recovery. Тест 2 имеет пик 1132 мс в 19:38:16, на 65 с раньше. Прямой связи нет, и это честно отмечено в досье.
- `max output-archive commit` 869/1148 мс по коду означает, что RLock продукта был занят примерно столько же. Всё это время event-loop стоял, если в этот момент выполнялся `on_event` или HTTP-хендлер (§3.2).

## 6. Вопросы владельцу (продуктовые, по одному разу)

1. **Память телефона.** Сейчас телефон хранит копию каждой записи речи и каждого ответа Миры навсегда. По оценке, место кончится примерно через 2,5 часа разговоров суммарно, и запись остановится. **Можно ли телефону удалять свою копию записи после того, как сервер подтвердил, что сохранил её полностью и побайтно совпадающей?** На сервере оригинал остаётся.
2. **Обрыв посреди фразы.** Если связь рвётся, пока вы говорите, Мира сейчас сразу отвечает на ту часть, которую успела услышать, а окончание фразы сохраняется, но до неё не доходит. **Лучше, чтобы Мира подождала до ~5 секунд и дослушала сохранённое окончание, прежде чем отвечать?**

## 7. План до реальной 90-минутной приёмки (по порядку, у каждого шага свой gate)

1. **Сегодня, без телефона:** смержить PR207 (CI зелёный; по процессу владельца). Отдельный PR с P0-SQL (приложение A) поверх него. P0-LOCK п.1–2 (`def`-хендлеры, InstrumentedRLock, loop-lag). Метрики §4.4.
2. **rc19 SDK:** P0-CLIENT-WINDOW, P0-CLOCK, P0-STALE-ACK, P0-UPLINK-COMMIT, P1-PACING, P1-ARCHIVE-STOP, P1-ADMITTED. Продукт: `commit_chunks`/`claim_projections`. Матрица досье §7 плюс тесты, перечисленные выше.
3. **Серверный soak без телефона и без Google** (новый gate, дешевле любого физического теста): 4 одновременных синтетических v2-клиента из существующей Android CI fixture или Python-клиента. Непрерывный 20-мс Opus uplink, раз в 20–40 с выходные bursts по 5 с, контролируемая AI-граница. Работа на копии продуктовой БД на том же хосте, параллельно с обычной нагрузкой RKB. **30 минут, затем 90 минут.** Gate: `commit_ms` p99 < 50 мс, `store_lock_wait` p99 < 20 мс, `event_loop_lag` p99 < 20 мс и max < 200 мс, **0** STALE / BACKPRESSURE / BACKLOG_RECOVERY / ARCHIVE_STALL. Без этого gate физический 90-минутный тест не имеет смысла.
4. **Телефон:** readback квоты (§3.6 п.1), затем решение владельца №1 и реализация освобождения журналов.
5. **Этап B досье (реальный Google):** без изменений плюс проверка `provider_admitted` против `server_durable` на каждой реплике.
6. **Samsung 15–20 мин**, затем **90 мин**, по этапам C/D досье, через штатный in-app update, с readback версии APK/SDK/backend.

**Что проверить в journald и БД сразу** (через `host_probe log_search`, окна v79 ±5 с вокруг каждого detach; распределение `duration_us` — read-only запросом к копии БД):
`encoded_voice_chunks.duration_us` uplink-источников v79/v80 (10 или 20 мс, §3.7); `socket_audio_stale_frame` (`capture_age_ms`: 0 означает кандидата §3.4, большое значение — backlog); `socket_audio_admission_wait` и `socket_audio_read_ahead_full` перед detach; `live_voice_io_contention` (если уже на PR207); `archive_event_committed` с `commit_ms ≥ 250`; `socket_gap` с `audio_turn_open=true` (это реплики, оборванные `activity_end`, §3.3); за ним `input_transcript` того же хода (насколько короче сказанного).

## Приложение A. Патч P0-SQL и регрессионный тест

Применяется поверх PR207 `d6ab815`. Все 21 существующих тестов `tests/test_encoded_voice_store.py` проходят, итого 22 passed. Новый тест на исходном коде падает: `AssertionError: (2, 7602)`. Падения `tests/test_native_voice_canary.py` в моём окружении (44) одинаковы с патчем и без него, их причина в отсутствии нативного Opus-кодека в системном Python (`compressed downlink profile is unavailable`), к патчу они не относятся. Нужен прогон в CI.

```diff
--- a/src/projects_hub/encoded_voice_store.py
+++ b/src/projects_hub/encoded_voice_store.py
@@ -356,16 +356,25 @@ class ProductEncodedAudioStore:
                 raise StoreError("SOURCE_EPOCH_SEALED", "Codec epoch has already ended")
             if chunk.flags & EPOCH_END and descriptor["end_seq"] not in (None, chunk.chunk_seq):
                 raise StoreError("SOURCE_EPOCH_CONFLICT", "Codec epoch has a different terminal chunk")
-            epoch_extent = self.store.db.execute("SELECT MIN(chunk_seq),MAX(chunk_seq) FROM encoded_voice_chunks WHERE source_id=? AND epoch_id=?", (source["id"], epoch.epoch_id)).fetchone()
+            # SQLite seeks a lone MIN()/MAX() on the index; both in one SELECT
+            # scan the whole epoch. Two ordered seeks keep this O(log n).
+            first = self.store.db.execute("SELECT chunk_seq FROM encoded_voice_chunks WHERE source_id=? AND epoch_id=? ORDER BY chunk_seq LIMIT 1", (source["id"], epoch.epoch_id)).fetchone()
+            last = self.store.db.execute("SELECT chunk_seq FROM encoded_voice_chunks WHERE source_id=? AND epoch_id=? ORDER BY chunk_seq DESC LIMIT 1", (source["id"], epoch.epoch_id)).fetchone()
+            epoch_extent = (first[0], last[0]) if first else (None, None)
             if epoch_extent[0] is not None:
                 if chunk.flags & EPOCH_END and epoch_extent[1] > chunk.chunk_seq:
                     raise StoreError("SOURCE_EPOCH_CONFLICT", "Codec end precedes already committed epoch audio")
-                low, high = min(epoch_extent[0], chunk.chunk_seq), max(epoch_extent[1], chunk.chunk_seq)
-                interleaved = self.store.db.execute("SELECT 1 FROM encoded_voice_chunks WHERE source_id=? AND chunk_seq BETWEEN ? AND ? AND epoch_id<>? LIMIT 1", (source["id"], low, high, epoch.epoch_id)).fetchone()
-                if interleaved:
-                    raise StoreError("SOURCE_EPOCH_CONFLICT", "Codec epochs cannot interleave within one captured source")
             previous = self.store.db.execute("SELECT chunk_seq,epoch_id,flags,capture_start_us,duration_us FROM encoded_voice_chunks WHERE source_id=? AND chunk_seq<? ORDER BY chunk_seq DESC LIMIT 1", (source["id"], chunk.chunk_seq)).fetchone()
             following = self.store.db.execute("SELECT chunk_seq,epoch_id,capture_start_us FROM encoded_voice_chunks WHERE source_id=? AND chunk_seq>? ORDER BY chunk_seq ASC LIMIT 1", (source["id"], chunk.chunk_seq)).fetchone()
+            # Every committed epoch occupies one contiguous run of chunk_seq. The
+            # nearest neighbours decide whether this row keeps that invariant in
+            # O(log n); a BETWEEN scan over the epoch grows with call duration.
+            joins_run = ((previous is not None and previous["epoch_id"] == epoch.epoch_id)
+                         or (following is not None and following["epoch_id"] == epoch.epoch_id))
+            splits_other = (previous is not None and following is not None
+                            and previous["epoch_id"] == following["epoch_id"] != epoch.epoch_id)
+            if (epoch_extent[0] is not None and not joins_run) or splits_other:
+                raise StoreError("SOURCE_EPOCH_CONFLICT", "Codec epochs cannot interleave within one captured source")
             if ((previous and previous["capture_start_us"] + previous["duration_us"] > chunk.capture_start_us)
                     or (following and end_us > following["capture_start_us"])):
                 raise StoreError("SOURCE_TIMELINE_CONFLICT", "Chunk order overlaps the original capture timeline")
@@ -477,7 +486,9 @@ class ProductEncodedAudioStore:
-            origin = self.store.db.execute("SELECT MIN(capture_start_us) FROM encoded_voice_chunks WHERE source_id=? AND epoch_id=?", (source["id"], commit.epoch_id)).fetchone()[0]
+            # capture_start_us is monotone in chunk_seq (timeline checks above), so
+            # the first row of the epoch is its origin: an index seek, not a scan.
+            origin = self.store.db.execute("SELECT capture_start_us FROM encoded_voice_chunks WHERE source_id=? AND epoch_id=? ORDER BY chunk_seq LIMIT 1", (source["id"], commit.epoch_id)).fetchone()[0]
```

```python
# tests/test_encoded_voice_store.py
def test_per_chunk_commit_and_projection_cost_does_not_grow_with_source_length(voice):
    """A 90-minute 20-ms source holds 270,000 chunks; every live chunk must
    stay O(log n) under the shared store lock, or ACK latency grows per minute."""
    store, encoded, *_ = voice

    async def run():
        bound = await opened(encoded)
        await encoded.commit_chunk(bound, epoch(), chunk(1))
        steps = [0]

        def count():
            steps[0] += 1
            return 0

        def measured(seq):
            steps[0] = 0
            store.db.set_progress_handler(count, 100)
            try:
                commit = encoded._commit_chunk(bound, epoch(), chunk(seq, payload=b"\7" * 20))
                encoded.claim_projection(DurablePCMProjection(commit, b"\0\0" * 1600, 16000, (seq - 1) * 100_000))
            finally:
                store.db.set_progress_handler(None, 0)
            return steps[0]

        short = measured(2)
        filler = chunk(3, payload=b"\7" * 20)
        rows = [(bound.server_source_id, seq, "epoch-1", (seq - 1) * 100_000, 100_000, 0,
                 filler.payload_sha256, filler.payload) for seq in range(3, 40_003)]
        with store._lock:
            store.db.execute("BEGIN IMMEDIATE")
            store.db.executemany("INSERT INTO encoded_voice_chunks(source_id,chunk_seq,epoch_id,capture_start_us,duration_us,flags,payload_sha256,payload) VALUES(?,?,?,?,?,?,?,?)", rows)
            store.db.execute("UPDATE encoded_voice_sources SET contiguous_seq=? WHERE source_id=?", (40_002, bound.server_source_id))
            store.db.execute("COMMIT")
        long = measured(40_003)
        assert long < short * 3, (short, long)

    asyncio.run(run())
```

## Приложение B. Воспроизведение бенчмарка

Скрипты `bench.py` (текущие запросы), `bench4.py` (каждый запрос отдельно, до и после) и их вывод лежат в каталоге артефактов из §1. Методика: схема импортируется из `projects_hub.encoded_voice_store.initialize_encoded_voice_schema`, одна эпоха, 60-байтный payload (Opus 24 кбит/с × 20 мс), медиана 20–50 повторов на точках 1k/10k/50k/150k/270k кадров. Абсолютные мс зависят от хоста. Существенен **линейный рост** при отсутствии роста у индексных seek'ов.
