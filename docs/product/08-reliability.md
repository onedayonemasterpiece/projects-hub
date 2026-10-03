# Качество: Live понимает, инфраструктура не теряет

> **U04 / 2 октября 2026:** к существующим release gates добавлен WSS/multi-user acceptance checklist: ticket/origin/query, binary PCM/ACK, no HTTP fallback, reconnect/no stale PCM, 4+ actor isolation, public WebSocket edge и physical Android voice/update. Канонический список — [16-wss-platform-reliability.md](16-wss-platform-reliability.md).

[Индекс](README.md) · [Центральный Live-агент](12-central-live-agent.md) · [Память](10-conversation-memory.md).

**Ревизия 3, 27 сентября 2026.** Надёжность оценивается одновременно по сохранности source и по тому, что центральный Live-agent действительно услышал весь материал и выполнил ровно нужные function calls.

## Инварианты

1. Полученная речь имеет durable source независимо от памяти provider.
2. Semantic interpretation делает Live-agent, не deterministic classifier.
3. Offline audio после связи идёт raw audio в тот же тип Live-agent, не в отдельный ASR.
4. Project routing делает Live-agent; backend проверяет target/ACL.
5. Permanent archive disposition делает Live-agent function call.
6. Tools не запускают скрытый semantic LLM.
7. Подтверждённая mutation не повторяется из-за reconnect.
8. UI не говорит «сохранено/сделано» раньше соответствующего receipt/readback.

## Независимые состояния

Source:
`capturing → local_durable → waiting/streaming_live → live_turn_complete → agent_disposition_pending → archived | ephemeral_processed | needs_clarification`.

Command:
`proposed → authorized → executing → verified | failed | outcome_unknown`.

Human obligation:
`proposed → accepted/declined → in_progress/blocked → done/cancelled`.

Provider socket может умереть в любом состоянии и не является source-of-truth.

## Failure matrix

| Событие | Правильное поведение |
| --- | --- |
| Нет сети | Capture продолжается, source durable, AI semantic work не имитируется |
| Сеть появилась после 10 минут речи | Весь sealed source raw audio идёт central Live-agent одним deliberate buffered turn |
| Пауза внутри buffered source | Agent не отвечает до activityEnd всего пакета |
| Network drop во время buffered replay | Source остаётся; recovery epoch продолжает/повторно предъявляет source без duplicate mutation |
| Input transcript неполный | Аудио удерживается; recovery делает тот же central Live-agent |
| Provider transcript event длинный | Archive path не обрезает его до UI-лимита |
| Event ring переполнился | Durable product observer уже сохранил source events |
| Agent не вызвал archive/discard из-за crash | source остаётся agent_disposition_pending |
| Agent выбрал wrong project, user corrected | новая route/mutation revision; backend не скрывает старый подтверждённый effect |
| Project недоступен | tool отказ; agent уточняет/объясняет, личный source не теряется |
| Vocabulary alias ambiguous | agent уточняет; backend не заменяет строку сам |
| Tool timeout after external create | reconciliation/readback до retry |
| Stop | mic/output stop; source и подтверждённые effects сохраняются |
| Новый разговор | старый source не удаляется; поздний старый ответ не проигрывается в новом |
| Quota/authority unavailable | capture работает; provider path следует shared resource contract |

## Обязательные gates

| Gate | Доказательство |
| --- | --- |
| G01 | Provider/data policy допустима для normal и fallback |
| G02 | Две группы/два пользователя не видят чужие objects/search/snippets |
| G03 | Один Live-разговор голосом переключается между 3 проектами без обязательного selector/reconnect |
| G04 | Отзыв доступа блокирует новые tool reads/writes |
| G05 | Параллельные document writes дают controlled revision conflict |
| G06 | Повтор после timeout/reconnect не дублирует business effect |
| G07 | Android offline queue переживает restart/reboot |
| G08 | Android long recording/lock/interruption без ложного recording state |
| G09 | PWA честно проходит supported foreground capture/lifecycle failures |
| G10 | Real mic online: PCM → Live → input transcript → function call → readback → voice response |
| G11 | Shared quota/admission с другими consumers без запрещённого fallback |
| G12 | authority outage и lost acquire дают разные корректные исходы |
| G13 | Calendar timezone/DST/unknown outcome reconciliation |
| G14 | Device-bound commands не уходят другому телефону |
| G15 | Prompt injection из docs/vocabulary не расширяет permissions |
| G16 | Tasks/votes сохраняют proposed/accepted/quorum/deadline semantics |
| G17 | Notifications не включают микрофон и не раскрывают лишнее |
| G18 | DB/source/index restore drill |
| G19 | Нетехнический пользователь проходит цикл полностью голосом + кнопки |
| G20 | Cost/latency telemetry без content/secrets |
| G21 | PWA/Android core UX parity |
| G22 | Multi-project монолог корректно порождает несколько target results |
| G23 | Offline raw audio 3/10/30 минут: agent учитывает контрольные факты начала/середины/конца |
| G24 | Buffered source с длинными паузами не вызывает ранний model response |
| G25 | Lossless input transcript sink не теряет данные из-за 2000-char projection или 320-event ring |
| G26 | Короткая важная реплика архивируется agentом; длинный служебный вопрос может остаться ephemeral |
| G27 | VAD: тихое начало, последний слог, паузы, noise/echo без потери значимой речи |
| G28 | Vocabulary update выполняет central agent через grounded tools |
| G29 | Исторический conversation/source находится и продолжается новой Live-session |
| G30 | Crash на каждой source boundary не требует повторного наговаривания |
| G31 | Current ProjectScope заменён/расширен для multi-project conversation resource binding |
| G32 | Product tools audited: нет hidden LLM routing/classification/summarization |
| G33 | Buffered replay использует manual activityStart/activityEnd real provider |
| G34 | Recovery после provider failure использует same central-agent architecture, не alternate ASR/model |
| G35 | Agent archive function materializes provider transcript/source journal и получает exact readback |

## G23 corpus

В длинную offline-запись специально помещаются:
- имя/термин в начале;
- число и отрицание в середине;
- смена project focus;
- корректировка предыдущей мысли;
- важное действие в конце.

PASS требует:
- source audio coverage;
- input transcript coverage;
- agent ответ учитывает все контрольные факты;
- project/tool receipts соответствуют смыслу;
- ни один факт не появился только в summary отдельного pipeline.

## G32 architecture audit

На каждый function/tool:
- перечислить, какие deterministic операции он выполняет;
- подтвердить, что внутри нет другого LLM call;
- если есть model call — gate FAIL до отдельного owner-approved решения;
- retrieval/search допустим, если он возвращает sources, а semantic вывод делает central Live.

## Framework-specific acceptance

Проверить фактические ограничения текущего `live-interaction`:
- `inputAudioTranscription` включена;
- full trusted transcript не режется 2000-char UI projection;
- product sink пишет события до ring eviction;
- browser preview не используется как archive;
- short provider history не используется как durable memory;
- buffered mode explicit, versioned и tested.

## UX quality

Пользователь видит только полезные состояния:
- слушаю;
- отвечаю;
- сохраняю/выполняю при необходимости;
- нет сети — запись сохранена;
- требуется уточнение.

Транспортные sequence/lease/manifest детали скрыты от обычного UI, но доступны диагностике.

Главный критерий: обычный сетевой/процессный сбой не заставляет человека повторно наговаривать уже подтверждённо сохранённый source.

## Release language

`PASS docs/tests` ≠ `PASS microphone/provider`.

Отдельно сообщать:
- deterministic/unit;
- real provider;
- physical Android/PWA;
- long offline;
- security isolation;
- group pilot.

Пока G01–G35 не имеют evidence, они остаются `not_run`.
