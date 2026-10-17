# Native voice: продолжение работы Claude от 16 октября

17 октября 2026. Статус: **интегрированный кандидат для проверки; выпуск и физическая приёмка ещё не выполнены**.

## Продолжена именно исходная работа

Переданный владельцем журнал Claude Code, GitHub и физические worktree DevCoveer подтвердили одну цепочку работы. Новые изменения продолжают её в прежних ветках и draft PR:

| Репозиторий | Ветка | Исходный checkpoint Claude | Текущий кандидат |
|---|---|---|---|
| live-interaction | `claude/voice-continuity-v0.4-20261016` | `599dc1caad8e0995ab65dc6b3fe2f684073af8f6` | `0.4.0-rc.1`, `c564f0c44c6fe783fd4932bd4633fada34e05c73`, [PR #51](https://github.com/onedayonemasterpiece/live-interaction/pull/51) |
| projects-hub | `claude/voice-continuity-consumer-20261016` | `87824b7aa13d0add284e7b1174635dd0c2ffca69` | этот commit поверх `955576734e97288634bbe2afb55c19dda39e51b3`, [PR #175](https://github.com/onedayonemasterpiece/projects-hub/pull/175) |

Физические worktree — `/home/dev/worktrees/live-interaction-continuity-20261016` и `/home/dev/worktrees/projects-hub-voice-continuity-20261016`. Они обновляются только fast-forward после проверки HEAD и отсутствия локальных изменений. Shared worktree уже проверена чистой на `c564f0c`; итоговый receipt consumer fast-forward сохраняется в work checkpoint. Посторонний checkout `/home/dev/projects/projects-hub` не является целью этой работы.

Python dependency с extra `[audio]`, web dependency/lockfile и Android source metadata/оба CI checkout закреплены на одном immutable shared SHA. Это **согласованный граф зависимостей feature-кандидата**. Stable потребители, production и release/signing workflow не переключались. npm записал lockfile при установке из GitHub; точные байты source archive дополнительно сверены по SHA-256. npm не выполняет обычную integrity-проверку git dependency, поэтому наличие поля integrity отдельно не выдаётся за такую проверку.

## Поведение кандидата

### Нативный Android и сжатие

Общий Android SDK теперь содержит runtime для AudioRecord, VAD, MediaCodec, WSS, AudioTrack и восстановления. Product foreground service владеет сессией независимо от WebView. Bridge передаёт команды, состояние и transcript; аудиобайты через JavaScript/base64 не ходят. Stop имеет отдельный приоритетный путь. Закрытие представления не подменяет намерение пользователя остановить разговор, а смена аккаунта завершает прежний голосовой контекст.

Новый профиль `wl-live-v2` передаёт сжатые оригиналы в обе стороны бинарными кадрами. Opus — основной вариант; AAC допускается при подтверждённых свойствах конкретного encoder, включая priming. Неизвестная возможность кодека означает явное неподдерживаемое состояние. Минимальный Android API 26 сохранён; наличие системного Opus encoder на старых версиях не предполагается.

На сервере PyAV вызывает нативные codec/resampler contexts в ограниченном пуле. Один декодированный PCM projection используется основным Live и caption sidecar. Новый путь не записывает второй постоянный PCM оригинал и не выполняет прежнюю внутреннюю цепочку base64 decode/encode перед sidecar. Сериализация, требуемая внешним provider API, сохраняется на его границе.

Существующий браузерный/PWA путь сохранён совместимым. Его legacy v1 PCM **не засчитывается** как новый обязательный сжатый профиль. Полная установленная iOS PWA остаётся следующим этапом по исходному Android-first порядку.

### Долговечность и нестабильная сеть

Источник, доставка и выполненное действие имеют разные подтверждения: device durable → server durable → provider forwarded → semantic commit; для ответа отдельно учитывается фактически проигранная позиция. ACK о записи не объявляет, что модель поняла всю реплику или пользователь услышал ответ.

Encoded store сохраняет payload и receipt транзакционно в SQLite WAL/FULL. Повторная доставка сопоставляется по стабильной идентичности и hash. Пропуск в последовательности не скрывается максимальным увиденным номером: чтение и подтверждение ограничены непрерывным префиксом. Исходящие оригиналы переживают WSS detach; отменённый или прерванный ответ остаётся доступным источником и не начинает автоматически звучать заново.

Очереди ограничены одновременно байтами, продолжительностью и возрастом. Есть отдельные live/recovery бюджеты и ограничение decode work. Поздняя речь сохраняется как `delayed`/`recording_only`; она не возвращается в Live как новая команда. Восстановление полного источника не возвращает повреждённой старой команде право на mutation. Свежая речь и Stop сохраняют приоритет перед backlog.

Для сервера установлены пределы encoded payload 64 MiB на источник и 2 GiB на store, ограничения длительности, числа chunks/epochs и очередей. Это предел payload, а не обещание, что вся SQLite DB с WAL/индексами занимает ровно столько же. Полный диск/очередь отображается явно; автоматического удаления оригиналов для получения зелёного статуса нет.

Неопределённый результат HTTP Start сохраняет тот же attempt id и не создаёт вторую provider-сессию. Stop оставляет terminal tombstone. Поздний callback, повтор Start или caption task не воскрешают закрытую сессию. Завершение host закрывает codec/IO ресурсы и ограниченно ожидает потоки; зависший native вызов не выдаётся за успешно убитый поток.

Сохранены и исправления первого checkpoint: корреляция потерянной речи по `audio_turn_generation`, запрет mutation по неполной реплике, отказ от replay старого provider backlog, Stop во время caption reconnect, приватность resumption handle и различение `detached_reclaim` от explicit Stop. Исходный `stop_all()` сохраняет checkpoint; terminal `close()` отдельно освобождает ресурсы.

### Долгие функциональные вызовы Миры

Для длительных внешних функций включён provider contract `NON_BLOCKING`. Если результат не пришёл через 2,5 секунды, shared host выдаёт одно коррелированное pending-событие и промежуточный FunctionResponse `willContinue=true`, `scheduling=WHEN_IDLE`. Инструкция Мире позволяет коротко сказать, что она ждёт результат, затем продолжить работу. Повторяющейся искусственной болтовни и выдуманного процента готовности нет.

Настоящий результат/ошибка завершает тот же вызов. Новая речь, Stop и отмена имеют приоритет; устаревшее завершение не перебивает следующую реплику. Промежуточный ответ не считается результатом инструмента, не заменяет receipt побочного эффекта и не попадает в его dedup cache. UI отдельно показывает ожидание инструмента и не сбрасывает его от окончания короткой фразы Миры.

Это проверено на уровне реального provider setup, shared scheduling и product/UI контрактов. **Фактическая слышимость короткого ответа выбранной Live-моделью ещё требует R1 provider/handset проверки.** Модель не заменялась ради этого незаметно. Основание протокола: [FunctionResponse API](https://ai.google.dev/api/generate-content#FunctionResponse) и [Live thinking/tool behavior](https://ai.google.dev/gemini-api/docs/live-api/thinking).

### Transcript без зависимости от живого WebView

Нативные input/output события атомарно обновляют источник и каноническую conversation timeline на сервере. Исчезновение WebView не теряет обязательную запись transcript. Источники и turn ids не переатрибутируются новой реплике при позднем callback старой сессии.

Большой transcript хранится полностью. Событие, превышающее 16 KiB JSON, передаёт компактную ссылку; клиент получает одну каноническую запись через авторизованный endpoint. Unicode и JSON escaping учитываются до отправки. Клиент ограничивает pending-загрузки, частоту попыток и память последних revisions, дедуплицирует их, отменяет при смене контекста и применяет результат к исходному placeholder. Ответ старого аккаунта/разговора/сессии не меняет новый экран. Запрос чужой записи возвращает непрозрачный 404.

LRU хранит последние 256 watermark identities, не вытесняя pending. Повторный hint обновляет recentness, включая failed revision, и не запускает горячий retry. После вытеснения давно не запрашиваемого id новый входящий hint может вызвать одно новое чтение; само вытеснение ничего не запускает. Этот компромисс обеспечивает конечную память долгой сессии.

## Проверка и воспроизводимость

Основные receipts, точные команды и логи: [native-candidate/verification.json](../research/voice-audio-efficiency-20261017/native-candidate/verification.json).

| Проверка | Результат | Что проверено |
|---|---:|---|
| Consumer backend, framework установлен из GitHub SHA | 471 passed, 1 внешнее предупреждение | Весь pytest; installed `live_interaction`, без shared PYTHONPATH overlay |
| Shared Python | 127 passed | Реальный PyAV codec, framing/store/progress/shutdown и прежние контракты |
| Shared JavaScript | 98 passed | 12 test files; полный Node runner с individual case reporting |
| Shared Java core | 9 passed | Wire/durable protocol и прежние core контракты |
| Shared Android platform | 20 passed | Codec timeline, playback/recovery и native platform contracts |
| Consumer Android | 31 passed; debug APK собран | Service/bridge/Start/Stop и product integration contracts |
| Java native Start → Python HTTP model | 2 passed | Реальная сериализация native запроса совместима с backend |
| Product v2 + PyAV + captions | passed | Codec path подключён к product adapter/sidecar |
| Web Node/TypeScript/Vite | 97 passed; TypeScript/Vite build passed | Проверки actual candidate dependency и сборка |
| Existing browser smoke | desktop + mobile viewport passed | Chromium 151.0.7922.34; Vite source с fixtures, не реальный provider/Android |
| Shared GitHub CI на `c564f0c` | success | [Run 39026083247](https://github.com/onedayonemasterpiece/live-interaction/actions/runs/39026083247) |

Локальная среда backend — Python 3.12.14, PyAV 18.1.0; shared установлен с extra `[audio]` в отдельный venv. Метаданные wheel нормализуют версию в `0.4.0rc1`, runtime экспортирует `0.4.0-rc.1`. Web — Node 24.19.0. Внешнее предупреждение Pydantic относится к MCP `RequestParamsMeta`; Vite сообщает о существующем крупном bundle. Эти предупреждения не скрыты и не подавлены.

Browser smoke первоначально проверял enabled Stop как признак готовности сокета. Stop по контракту доступен уже в `starting`. Исправлено ожидание существующего сценария на `state-listening`, которое следует за WSS connect; runtime ради теста не менялся. Повтор прошёл для desktop 1280×900 и mobile viewport 390×844. Это эмуляция viewport с fixtures, не физическая mobile network проверка.

APK: 3 399 365 bytes, SHA-256 `1cff757af14b04de9350ab88a12f20564aa3f3885b30ed87c37f663a75e1527f`. Сборка: JDK 17.0.20.1+1, Gradle 8.10.2, AGP 8.7.3, SDK 35, minSdk 26. Это отдельный debug package `0.1.1-debug`, а не подписанное production обновление `0.1.62`. Все Android source/build inputs сверены с опубликованным shared RC; последующая правка README отражена отдельно и не меняет APK.

## Реальный provider spike: выполнен один раз, приёмка не закрыта

Продолжен существующий `provider_spike.py` и `run_spike.sh all` из вчерашнего artifact directory. Оригинал сохранён побайтно; offline suite дала 14 passed. Реальный запуск `81957d0e35dc4d6b9e1f6a62536e8f21` начался 09:11:23 UTC и завершил запись результата 09:25:06 UTC. Импортированные источники совпали с проверенными hash; runtime host — Python 3.14.4.

Realtime и burst передали одни и те же 134 chunks / 13,3 секунды синтетической английской речи. Оба получили model audio и completed turn. В realtime передача заняла 13,53 с, от фактического audio end до turn complete — 3,22 с; в burst передача заняла 0,01 с, завершение — 6,13 с. Это измерение runner/provider на одном сервере, без телефона, и оно не доказывает, что burst лучше.

В обоих режимах не наблюдался свежий resumable checkpoint после audio end. Resume проверка поэтому **skipped**; idle renewal закончился **TimeoutError**, GoAway/resume не наблюдались. Завершившийся runner не означает PASS всех gates. Одна lease, три connection attempts при cap шесть; 4 147 reserved units при cap 150 000, unknown reservations — ноль, release подтверждён. Это authority accounting, не независимо проверенный vendor bill.

[Полный безопасный итог](../research/voice-audio-efficiency-20261017/provider-spike/spike-all-20261017.summary.json) и [handoff](../research/voice-audio-efficiency-20261017/provider-spike/HANDOFF.md) сохраняют отрицательные результаты. Запуск не повторяется как будто он ещё не выполнен. [Session resumption updates](https://ai.google.dev/gemini-api/docs/live-api/session-management) периодические, а не обязательные после каждой реплики; отсутствие такого update само по себе не доказывает поломку всего provider resume. Следующий конкретный R1 план должен различать пригодность последнего checkpoint, подтверждённую границу и ещё не принятую речь.

## Производительность и полный backend на Go

[Основное исследование](../research/voice-audio-efficiency-20261017/README.md) содержит реальные function/codec probes и их ограничения. [Отдельная оценка всего backend](../research/voice-audio-efficiency-20261017/backend-go-scope.md) проверяет именно одновременные Live + captions + RAG и соседние процессы VPS.

Текущий вывод: сначала общий ограниченный runtime, сжатые оригиналы, одна decode-проекция и удаление лишних внутренних копий. Нативный codec уже исполняет тяжёлую работу вне интерпретатора. Оставшийся bottleneck надо измерить. Rust модуль имеет смысл для доказанного горячего участка; полный Go backend — если равнофункциональная реализация воспроизводимо снижает расход всего сервера при тех же durability, ACL, качестве, p95/p99, Stop и свежей речи.

Go proxy перед прежним Python host не доказывает такой перенос. Regional Knowledge в этом репозитории пока представлен delegated evidence boundary, а не установленным embedding/vector engine. Реальное сравнение должно включать выбранный Knowledge service, индекс, models и storage на том же сервере. Матрица 1/4/8/16 одновременных разговоров предложена, но не запускалась; default admission 16 не является обещанной capacity. Никакой коэффициент ускорения/экономии Go и число поддерживаемых пользователей здесь не заявлены.

Единственный оставшийся неблокирующий вопрос владельцу: целевой пик одновременных активных голосовых разговоров и план роста. Обязательное сжатие, нестабильная сеть, приоритет Stop/свежей речи и source-only recovery уже согласованы.

## Что остаётся до продуктовой приёмки

1. Исходный 90-минутный Android сценарий на физическом устройстве: погашенный экран, background/process death, permissions, Bluetooth/гарнитура/динамик, Stop и непрерывность timeline/источников.
2. Реальная mobile network матрица: latency, jitter, потери, малая полоса, outage и handover, восстановление без повторных semantic effects, явные состояния очереди/диска и приоритет свежей речи. При полосе ниже capture rate backlog не может исчезнуть математически; нужен честный delayed режим.
3. R1 provider acceptance: audible long-tool pending, основной transcript и обязательные captions, длительная сессия/renewal/resume, capability transitions и Stop. Уже выполненный spike эти gates не закрыл.
4. Качество русского ASR и ответного звука с выбранными codec settings: шум, тихая речь, собственные имена; на телефоне также battery/CPU/RAM. Номинальный bitrate не доказывает сохранение качества распознавания.
5. Подписанный APK и реальная цепочка self-update; синхронная release-пара shared artifacts/consumer, затем обычная rollout/rollback проверка. Добавленная storage schema сохраняет legacy PCM; старый binary не получает автоматически право трактовать новые encoded sources как PCM. Нужен проверенный migration и backup/rollback сценарий, без reset/удаления источников.
6. Совместная нагрузка с настоящим Regional Knowledge provider и измерение capacity при выбранном целевом пике. Успешные unit tests не устанавливают ресурсные бюджеты production VPS.

У этих пунктов есть основания в исходной постановке, согласованном сжатии и фактических неподтверждённых свойствах. Дополнительный перенос языка не сделан условием завершения уже согласованного голоса. Production, merge main, подписание, release/tag и deployment этой работой не выполнялись.

Согласованная редакция требований закреплена commit `493215046cd75d0f267d612f2277e81846204d1d`: изменены только два requirement ID, остальные statements/criteria сохранены. Receipt и точная редакция: [native-voice-audio-profile-proposal-20261017.md](native-voice-audio-profile-proposal-20261017.md).
