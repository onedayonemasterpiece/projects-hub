# Полный backend на Go: границы и проверка экономии

17 октября 2026. Дополнение к [исследованию голосового пути](README.md). Проверен текущий кандидат продолжения Claude; нагрузка на сервер и обращения к моделям не запускались. Полный перенос backend рассматривается как вариант при доказанной экономии. Эта записка определяет сравнение и не объявляет перенос выполненным или разрешённым к выпуску.

## Что нужно сравнивать

Целевой сценарий одного пользователя одновременно включает Live, растущие субтитры, поиск evidence, сохранение источника и продуктовые операции. Поэтому оценивать нужно весь обслуживающий runtime и соседние процессы одного сервера, включая состояние покоя и пики. Ускорение отдельного HTTP endpoint не отвечает на этот вопрос.

Сопоставляются оптимизированный Python backend и функционально равный Go backend с общим `live-interaction` runtime. Go-вариант должен исполнять session lifecycle, provider protocol, caption orchestration, source storage adapters, авторизацию и продуктовые операции. Если за Go остаётся прежний Python host, его ресурсы полностью учитываются; такая промежуточная схема ещё не доказывает экономию полного переноса. Публичные wire/storage contracts и границы общего framework сохраняются.

## Фактические владельцы затрат

Пути `src/...` ниже относятся к Projects Hub; `python/live_interaction/...` — к общему репозиторию `live-interaction`.

| Участок | Что существует в проверенном коде | Возможный эффект Go |
|---|---|---|
| HTTP, WSS, продукт | `scripts/run_server.py` запускает Uvicorn/FastAPI; `app.py`, `live_adapter.py`, admission и фоновые services работают в backend. Число workers в entrypoint не задано. | Заменяются Python objects, JSON parsing, scheduling, middleware и domain orchestration. Реальные процессы установленного VPS ещё надо инвентаризировать. |
| Live и captions | `live_runtime.py` вызывает два `run_guarded`; `provider.py` и `transcribe.py` открывают отдельные Google WSS. `CaptionSidecar` — asyncio task внутри процесса, не отдельный Python subprocess. | Можно объединить управление памятью и очередями. Два соединения и две модельные работы сохраняются при том же поведении субтитров; Google inference исполняется вне backend. |
| Resource SDK | Installer закрепляет `ai-resource-control` SHA `6ec1929d`. Каждый обычный `run_guarded` создаёт `Control`, собственный HTTPX client, provider task и heartbeat task; release закрывает client. | Перенос клиентской логики может менять локальные allocations/connection pooling. Lease ledger, RPC и quota authority остаются теми же; экономия не получается удалением guard. |
| Кодеки | `audio_codec.py`: PyAV/FFmpeg contexts, resampler и native encode/decode; Ogg/ADTS framing, CRC и проверки написаны на Python. | Go может заменить обвязку и проверки. Работа native codec, его память и PCM buffers остаются, если используется тот же codec; библиотека и настройки фиксируются в сравнении. |
| Долговечность | `store.py`: SQLite WAL, `synchronous=FULL`, общая `RLock`. `encoded_voice_store.py` хранит compressed payload и receipt metadata транзакционно, без постоянного decoded PCM дубля. | Заменяется orchestration; SQLite, page cache, накопитель и стоимость durable commit сохраняются. Ускорение за счёт более слабого ACK/durability не принимается. |
| Другие функции | `AnalyticsBridgeClient` и `DevCoveerClient` используют отдельные stdio MCP processes; их вызовы сериализуются lock/worker. `note_processing.py` повторно использует HTTPX client; GitHub clients создают его на запрос. | Уменьшение повторных клиентов или дублированных процессов исследуется в обоих вариантах. Переименование вызывающей части в Go само по себе не убирает bridge и выполняемые им задачи. |

Resource SDK проверен по [точному `live.py`](https://github.com/onedayonemasterpiece/ai-resource-control/blob/6ec1929dc3183f4bafb112de96ee0571334c0b40/src/ai_resource_control/live.py) и [HTTP client](https://github.com/onedayonemasterpiece/ai-resource-control/blob/6ec1929dc3183f4bafb112de96ee0571334c0b40/src/ai_resource_control/client.py). Поведение SQLite подтверждается [описанием `synchronous`](https://www.sqlite.org/pragma.html#pragma_synchronous).

### Где находится RAG

В [regional_knowledge.py](../../../src/projects_hub/regional_knowledge.py) есть типизированный `search_evidence` и нормализация результата: до пяти evidence, суммарно до 24 000 символов текста. Там нет embedding model, vector index или HTTP transport. `create_app(regional_knowledge_factory=None)` передаёт factory в adapter; штатный `scripts/run_server.py` её не устанавливает. Capability появляется только при реальном delegated provider. Документ `docs/product/16-wss-multi-user-reliability.md` также оставляет полную интеграцию acceptance gate.

Следовательно, текущий repository не позволяет приписать VPS расход конкретного RAG engine. Для целевой совместной установки в измерение добавляются фактические Knowledge service, embedding/reranker, индекс, DB/object storage и их процессы. Локальные model weights, vector search, OCR/индексация и дисковый cache не исчезают при переносе orchestration на Go. Внешние inference/storage учитываются отдельно: задержка и трафик влияют на продукт, их память не является RSS этого VPS. Обязательная граница — прежние actor/workspace grant, target audience и evidence provenance.

## Какие ограничения уже есть

Кандидат `audio_v2.py` ограничивает chunk 8192 encoded bytes и 240 мс decoded work. Live window — 32 KiB/960 мс, recovery — 16 KiB/480 мс, live age — 2500 мс; recovery получает отдельный бюджет. В `audio_socket.py` общий codec pool имеет два workers и четыре slots; `EncodedVoiceIO` — два workers и восемь slots. Это пределы отдельных очередей, не доказанная вместимость VPS.

Кроме них остаются provider input 768 KiB, caption input 1 200 000 bytes, output relay 256 KiB и ring 320 событий. Число событий не ограничивает их суммарные bytes. Admission default 16 sessions также не является capacity. Проверять нужно сумму очередей, возраст данных и рост памяти при concurrent Live+caption+RAG, включая один разговор.

Оптимизированный Python baseline сохраняет эти гарантии, повторно использует decoded PCM для captions, убирает ненужные внутренние сериализации и выносит блокирующую работу в ограниченные исполнители. Это уже частично начато в кандидате. [Python asyncio](https://docs.python.org/3/library/asyncio-dev.html#running-blocking-code) объясняет, почему синхронная работа задерживает остальные tasks. Go тоже требует ограничения очередей: его GC обменивает CPU на память, а `GOMEMLIMIT` не охватывает всю native/соседнюю память; см. [Go GC guide](https://go.dev/doc/gc-guide).

## Предложенная матрица, ещё не выполненная

| Одновременные разговоры | Обязательная совместная работа |
|---:|---|
| 1 | Live duplex + captions + RAG запросы на том же сервере; сравнение с покоем и каждым компонентом отдельно. |
| 4 | Та же смесь, пересекающиеся tool calls и запись источников. |
| 8 | Та же смесь плюс дозированное восстановление backlog и отдельная проверка свежей речи. |
| 16 | Та же смесь, насыщение и честное admission/backpressure; число не означает обещанный supported tier. |

Для каждого уровня фиксируются одинаковые speech fixtures, codec parameters, corpus/index, запросы, сеть, CPU quota, RAM limit и durable storage. Несколько пользователей имеют отдельные разрешённые scopes; per-actor admission не обходится. Генератор следует capture clock независимо от ACK. Отдельные cold/warm серии и повторения; длительная серия достигает исходного long-session gate. Сначала детерминированный provider boundary для изоляции backend, затем отдельно согласованный интеграционный прогон. Заглушка не доказывает latency модели.

Измеряются whole-host CPU seconds, steady/peak RSS и PSS процессов, cgroup `memory.current/peak`, `cpu.stat`, pressure/OOM, I/O и fsync latency. PSS позволяет учитывать shared pages без полного двойного подсчёта; cgroup включает также page cache/socket memory. Источники: [Linux `/proc`](https://docs.kernel.org/filesystems/proc.html), [cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html). Одновременно снимаются p95/p99 capture→device durable→server durable→provider, caption delivery, tool/RAG response, first audible output и Stop; внешний network/provider time выделяется.

Критерий принятия: равные функции, качество, authority и committed receipts; действующие latency gates выполнены, p95/p99 и fresh Stop не ухудшились. Нет потерянного durable source, duplicate effects, утечки после Stop, неограниченной очереди или возобновления из позднего callback. Проверяются outage/handover, медленный диск, provider failure, исчерпание лимитов и restart. Go должен воспроизводимо снизить заранее выбранный ограничивающий ресурс всего сервера при сохранении остальных бюджетов; экономия и разброс публикуются вместе. До такого результата файлы `measurements/*` остаются локальными function/codec probes, без оценки capacity и без сравнения Python с Go.
