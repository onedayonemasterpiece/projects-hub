# Разговор, офлайн-речь и долговечная память

[Индекс](README.md) · [Центральный Live-агент](12-central-live-agent.md) · [UX](03-product-and-ux.md) · [Проверки](08-reliability.md).

**Ревизия 3, 27 сентября 2026.** Центральная Live-модель сама слышит и понимает как онлайн-, так и накопленную офлайн-речь. Этот документ описывает долговечность источника вокруг агента; он не вводит отдельный ASR/semantic pipeline.

## 1. Главное обещание пользователю

Человек не должен думать: «успело ли это попасть в модель», «не забыла ли модель», «нужно ли повторить всё ещё раз».

Для каждой реплики существует два независимых факта:
1. **источник речи физически сохранён и может быть восстановлен**;
2. **Live-агент услышал источник, понял его и принял/зафиксировал смысловое решение через function calls**.

Первый факт обеспечивает инфраструктура. Второй — центральная Live-модель. Ни один из них не подменяет другой.

## 2. Один источник аудио, два назначения

Микрофонный pipeline один:
- PCM/VAD/pre-roll/hangover для Live;
- параллельно те же исходные кадры попадают в локальный durable capture;
- никакой второй микрофон, второй записывающий стек или конкурирующий VAD не создаётся.

Онлайн сохранённые кадры уходят прямо Gemini Live. Офлайн они остаются в очереди. При восстановлении сети та же запись подаётся **как аудио в Gemini Live**, а не сначала в отдельный распознаватель.

VAD экономит хранение/передачу тишины, но не определяет смысл, проект, ценность или конец разговора. При сомнении детектор сохраняет возможную речь. Пауза на размышление не является командой «закончить разговор».

## 3. Жизненный цикл source

```text
capturing
→ local_durable
→ [если сеть есть: streaming_live]
→ [если сети нет: waiting_connectivity]
→ live_receiving
→ live_turn_complete
→ agent_disposition_pending
→ archived | ephemeral_processed | needs_clarification
→ source_cleanup_eligible
```

Отдельно живут product mutations, вызванные агентом. Например, source уже архивирован, а изменение GitHub-документа ещё `pending`.

### Что означает каждый статус

- `local_durable`: исходные кадры/сегменты восстановимы на устройстве.
- `live_receiving`: центральному Live-агенту ещё передаётся аудио; он не должен считать пакет полностью услышанным.
- `live_turn_complete`: граница пользовательского хода закрыта; agent может отвечать и вызывать tools.
- `agent_disposition_pending`: agent ещё не решил, нужен ли постоянный Markdown/какие project links.
- `archived`: function-call сохранения прошёл readback; source имеет устойчивый Markdown.
- `ephemeral_processed`: Live-агент явно решил, что отдельный постоянный source не нужен; временный recovery source сохраняется до retention.
- `needs_clarification`: агенту нужно продолжить диалог; исходник остаётся.

Ответ голосом без соответствующего receipt не переводит source в `archived`.

## 4. Онлайн: Live сама транскрибирует и понимает

Текущий `live-interaction` уже включает Gemini `inputAudioTranscription`. Поэтому нормальный путь:

```text
PCM → Gemini Live
         ├─ понимание аудио
         ├─ input transcription events → durable source journal
         ├─ function calls → сохранение/изменения
         └─ голосовой ответ
```

Backend может механически собирать `input_transcript` events по `turn_id`, но не прогоняет их через второй LLM для понимания. Сам agent внутри той же Live-сессии слышит звук и принимает semantic decisions.

Input transcription — полезная версия исходного текста, но не абсолютная истина. Аудио остаётся recovery source, пока source policy не разрешит очистку. Голосовая поправка пользователя создаёт корректировку/новую редакцию; старый provenance не подменяется.

## 5. Длинная запись без сети: тот же Live-агент получает звук

### Capture

Без сети пользователь может говорить секунды или минуты. Клиент:
- открывает один logical recording/source;
- пишет восстанавливаемые chunks;
- хранит monotonic sequence, временные диапазоны, checksum и manifest;
- применяет тот же проверенный VAD;
- не запускает semantic обработку;
- не создаёт summary;
- не выбирает проект.

### Return of connectivity

После появления Live:
1. создаётся/возобновляется **обычная центральная Live-сессия Projects Hub**;
2. session context сообщает agent, что следующий пользовательский turn — buffered recording, когда он был записан и какой `source_id` имеет;
3. transport переводится в buffered-turn mode;
4. отключается automatic activity detection именно на этот deliberate replay;
5. отправляется `activityStart`;
6. сохранённый звук технически декодируется/ресэмплируется в PCM16 16 kHz и передаётся Live в исходном порядке;
7. отправляется `activityEnd`;
8. только после полной границы agent отвечает и делает function calls.

Таким образом естественная пауза внутри пятиминутной записи не заставляет модель начать отвечать после первой минуты.

Техническая декодировка AAC/M4A → PCM не является отдельным ASR. Это только формат для Live API.

### Если сеть вернулась ещё во время речи

Нельзя смешивать уже накопленный backlog и текущий микрофон так, чтобы порядок стал неопределённым.

Проектный default:
- текущий logical turn продолжает локально записываться;
- после его seal вся запись воспроизводится центральному Live-агенту как один buffered turn;
- последующий уже live-turn начинается после окончания buffered turn.

Позднее можно оптимизировать переход в live-tail при доказанной строгой очередности. Для MVP сохранность и однозначный порядок важнее нескольких секунд latency.

## 6. Никакого отдельного “ASR fallback”

Если Live transcription оказалась неполной или provider session упала:
- аудио не удаляется;
- source остаётся pending;
- новый экземпляр **того же центрального Live-агента** получает сохранённое аудио заново как recovery turn;
- agent и его input transcription восстанавливают обработку;
- durable command IDs не позволяют повторно выполнить уже подтверждённые business mutations.

Не вводить Flash-Lite/Whisper/другую модель как скрытый fallback только для того, чтобы подготовить текст “настоящему” агенту. Такой путь потребует отдельного продуктового решения, а не технической удобной вставки.

## 7. Как создаётся постоянный Markdown

Постоянную ценность определяет Live-агент по содержанию и разговору. Никакой deterministic classifier перед ним нет.

Типичный tool:

```text
memory.commit_voice_source({
  source_id,
  kind,              // idea | requirement | decision | note | review | ...
  title?,
  project_links?,
  importance?,
  unresolved?,
  semantic_notes?
})
```

Backend tool:
- авторизует actor;
- получает provider transcript journal указанного source;
- сохраняет полный доступный transcript и provenance;
- связывает model-provided semantic metadata;
- делает идемпотентную GitHub materialization/readback;
- возвращает agent receipt.

Agent затем говорит пользователю **после receipt**: что сохранил и куда.

### Что хранится

Markdown разделяет:
- capture metadata;
- исходную provider input transcription;
- подтверждённые пользовательские поправки/нормализации;
- semantic summary агента;
- links на проекты и выполненные actions;
- model/tool/version provenance.

Нельзя писать semantic summary в секцию «дословная речь».

## 8. Что можно не архивировать постоянно

Короткая итерация вроде «повтори», «да», «нет, правее», вопрос без новой проектной информации может остаться только частью conversation history.

Но решение принимает agent в контексте:
- «да» может утверждать важное решение;
- «не пятница, а четверг» — важная корректировка;
- «запомни это» — явный запрос долговременной памяти.

Поэтому **никакого правила “20 секунд = архив, 19 секунд = не архив” нет**.

Если agent вызывает `memory.finish_ephemeral(source_id)`, backend сохраняет disposition и только затем применяет ограниченный recovery retention.

Если agent ничего не решил из-за сбоя, source остаётся `agent_disposition_pending` и автоматически не очищается.

## 9. Как вернуться к прошлому разговору

Agent имеет tools:
- `conversation.search_sources(query, filters?)`;
- `conversation.get_source(source_id)`;
- `conversation.get_thread(conversation_id)`;
- `conversation.get_open_loops(conversation_id)`.

На фразу «вернёмся к тому, что я говорил про фестиваль на прошлой неделе» сам Live-агент ищет источники, читает нужные excerpts/полный source и продолжает разговор.

Search/retrieval может быть полнотекстовым или векторным, но это **retrieval**, а не отдельный агент, решающий смысл запроса вместо Live. Ранжирование возвращает источники; вывод и дальнейшее действие делает Live-модель.

Старые относительные даты интерпретируются с `captured_at/timezone` исходной записи. Просроченное старое намерение не запускает автоматическое внешнее действие без повторной проверки актуальности.

## 10. Новый разговор, Stop и delete

- **Stop**: остановить микрофон/текущий model response. Уже захваченный source сохраняется.
- **Новый разговор**: новый conversation_id. Pending source старого разговора остаётся и может обработаться/архивироваться; его голосовой ответ не проигрывается поверх нового разговора.
- **Отмена ожидания**: не delete.
- **Удалить запись**: отдельная явная операция конкретного source с применимой retention/audit policy.
- **Сбросить** без ясного смысла: безопасно начать новый разговор, не уничтожая source.

## 11. Когда аудио можно удалять

Для `archived` source:
- manifest/source complete;
- agent disposition зафиксирован;
- provider transcript journal и пользовательские corrections сохранены;
- Markdown commit/readback подтверждён;
- связи conversation/source durable;
- backup/retention policy позволяет очистку.

Для `ephemeral_processed`:
- полный Live-turn завершён;
- agent явно выбрал ephemeral disposition;
- нет unresolved tool/mutation;
- истёк опубликованный recovery retention.

Для pending/unknown source автоматической очистки нет.

Невозможно гарантировать восстановление звука, который физически не успел попасть ни в один durable checkpoint до уничтожения единственного устройства. Задача приёмки — измерить и минимизировать этот хвост, а не маскировать его красивым статусом.

## 12. Текущие framework blockers, влияющие на память

Реализация обязана учесть подтверждённые свойства текущего `live-interaction`:

1. Provider уже включает input transcription — это и есть базовая транскрипция центрального агента.
2. Provider сейчас обрезает каждый projected transcript event до 2000 символов. До source archive нужен full-event sink или иная lossless product projection.
3. Session host держит ring на 320 событий. Durable source observer должен писать события до вытеснения.
4. Browser показывает только хвост transcript для UX. Он не источник архива.
5. Provider reconnect сознательно не replay-ит потерянную речь. Projects Hub должен держать durable audio **выше transport** и намеренно feed-ить buffered recording после восстановления.
6. Shared wire пока не имеет product-ready manual `activityStart/activityEnd` buffered mode — это обязательное версионируемое расширение framework.
7. Bootstrap history ограничен; долговечная conversation memory должна читаться agent tools, а не помещаться целиком в provider history.

Эти изменения усиливают центральную Live-архитектуру; они не оправдывают второй ASR/agent pipeline.
