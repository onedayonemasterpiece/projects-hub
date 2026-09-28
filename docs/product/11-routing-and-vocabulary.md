# Проекты в разговоре и развивающийся словарь

[Индекс](README.md) · [Центральный Live-агент](12-central-live-agent.md) · [UX](03-product-and-ux.md) · [Память](10-conversation-memory.md).

**Ревизия 3, 27 сентября 2026.** Project routing и развитие словаря — часть рассуждения центрального Live-агента. Backend предоставляет каталоги, чтение, поиск, ACL и запись, но не запускает отдельный semantic router.

## 1. Один разговор о многих проектах

Пользователь не обязан вручную выбирать проект перед каждой фразой.

Примеры:
- «Теперь про фестиваль».
- «Это уже для клуба».
- «Сравни, что осталось по всем моим активным проектам».
- «Нет, предыдущую мысль отнеси к подкасту».
- «Эта часть личная, в группу не клади».

Live-агент:
1. знает краткий список доступных проектов и current focus;
2. при необходимости вызывает `projects.list_accessible`, `projects.get` или document search;
3. сам делает semantic inference;
4. если уверенности недостаточно — задаёт столько уточняющих вопросов, сколько нужно;
5. только затем вызывает tool нужного target project.

Backend не содержит “if keyword X → project Y”. Он проверяет, что выбранный моделью project_id действительно доступен actor.

## 2. Личный разговор и групповые результаты

Conversation source по умолчанию личный для actor. Если в одном монологе затронуты три проекта, agent может вызвать три отдельных project operations, но весь исходный transcript не копируется трем группам.

Каждый project result содержит:
- source_id/fragment refs;
- actor;
- target project;
- semantic kind;
- applied revision/receipt.

Agent может позже исправить route новой function call. Уже подтверждённое внешнее действие не стирается фразой «я имел в виду другое»; agent видит receipt и выполняет корректирующее действие, если оно разрешено.

## 3. Current focus — помощь, а не ограничение

`conversation.set_focus(project_id)` сохраняет удобный focus, чтобы не повторять название проекта в каждой реплике. Focus не выдаёт новых прав и не делает следующие слова автоматически частью этого проекта навсегда.

Явное пользовательское указание сильнее focus. Разговор о “всех проектах” временно снимает узкий focus без создания отдельной новой AI-сессии.

## 4. Multi-project scope и безопасность

Live-session персональна actor. Это позволяет одной модели понимать связи между доступными человеку проектами.

Project isolation обеспечивается на tool boundary:
- каждый project read/write проверяет membership/role;
- search выполняется по разрешённым источникам;
- write target всегда явен;
- tool result не возвращает скрытые данные соседней области;
- shared notifications/publication имеют отдельную аудиторию.

Существующий Projects Hub `ProjectScope` — resource bootstrap, не финальная semantic session scope. Он должен быть заменён/расширен conversation/workspace binding, а project authorization перенесён в product tools.

## 5. Проверенные предшественники словарей

Принципы берём из:
- Record Idea Hub source/terminology provenance;
- `idea-hub/config/voice-terminology.yaml`;
- Wonderful Lections `recognition_context`;
- Wonderful Lections `assets/review/terms.ru.json`.

Сохраняем:
- canonical names;
- aliases/common misrecognitions;
- entity kind;
- project/source refs;
- revision/digest;
- правило акустической и контекстной совместимости;
- неопределённость вместо угадывания.

Не переносим batch-ASR UX или отдельную AI-модель.

## 6. Как агент сам строит словарь

Central Live agent имеет tools:
- `vocabulary.read(scope)`;
- `project_docs.search(project_id, query)`;
- `project_docs.read(project_id, ref)`;
- `vocabulary.upsert_grounded(entries, source_refs, expected_revision)`;
- `vocabulary.list_candidates(scope)`.

При первом входе в проект или по голосовой команде «обнови словарь проекта» agent:
1. смотрит текущий vocabulary snapshot;
2. ищет/читает разрешённые документы;
3. замечает названия людей, продуктов, аббревиатуры и устойчивые domain terms;
4. вызывает `vocabulary.upsert_grounded`;
5. получает revision receipt;
6. продолжает разговор с этим знанием.

Backend **не вызывает другой LLM**, чтобы сделать это заранее.

## 7. Автоматическое обновление без постоянного reread

Документальные revisions можно отслеживать детерминированно. Если source SHA не изменился, agent не нужно перечитывать его ради словаря.

Если изменился:
- system может пометить dictionary source stale;
- Live-agent при следующем подходящем разговоре/явном refresh читает delta и обновляет vocabulary;
- для критичных проектных rename можно подать agent короткий source-change notice;
- semantic решение о новом alias/canonical term остаётся у Live.

Не требуется фоновый LLM-worker.

## 8. Голосовые поправки

«Это называется Виштынецкий парк, запомни».

Agent слышит коррекцию, при необходимости сверяет проектные документы и вызывает vocabulary tool. Короткая фраза может одновременно быть source memory и словарной правкой.

Если correction противоречит каноническому документу:
- agent объясняет расхождение/уточняет;
- не перезаписывает canonical автоматически.

## 9. Собственная транскрипция не должна усиливать ошибку

Provider input transcription принадлежит той же Live-сессии, но ошибочная строка не является внешним доказательством правильного написания.

Grounded update требует:
- разрешённого документа;
- либо явной поправки пользователя;
- либо другой уже проверенной записи/метаданных.

Agent может использовать контекст, чтобы понять, что “Виштенец” вероятно означает “Виштынец”, но archival normalization фиксирует основание и не выдаёт догадку за исходную буквальную речь.

## 10. Словарь и сама Live-модель

Небольшой актуальный terminology snapshot передаётся в server-owned context центрального agent. Более широкий словарь доступен через tool.

Это помогает **одной Live-модели** понимать предметную речь и нормализовать source.

Нельзя обещать custom speech-adaptation параметр, если конкретный Gemini Live model/API его не поддерживает. Даже без отдельного provider vocabulary API agent может использовать контекст и документы для правильной интерпретации.

## 11. Поиск по проектам и прошлым голосовым

Поиск — инструмент Live-agent:
- lexical/vector retrieval возвращает refs/snippets;
- ACL проверяется до выдачи;
- agent сам выбирает нужные источники и делает вывод.

Нельзя вставлять между voice и Live скрытый “router model”, который сначала решает, какие проекты релевантны, и передаёт центральному agent только свою выжимку.

## 12. Приёмка

Обязательные сценарии:
- три проекта в одном монологе;
- одинаковые/похожие имена проектов;
- переключение focus без остановки Live;
- позднее «нет, это в другой проект»;
- материал одновременно личный и частично проектный;
- недоступный project target;
- поиск старого voice source и продолжение;
- dictionary refresh по изменившемуся GitHub document;
- новая аббревиатура;
- неоднозначный alias;
- явная голосовая поправка;
- отказ прав на vocabulary source;
- отсутствие скрытых дополнительных LLM calls.

Успех измеряется правильным смыслом и правильным target/receipt, а не только валидным JSON словаря.
