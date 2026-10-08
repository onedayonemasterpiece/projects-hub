"""Product-owned progressive domain bundles; transport stays in live-interaction."""

BUNDLES = {
    "core": ["projects_list_accessible", "conversation_set_focus", "runtime_versions_get"],
    "preferences": ["preferences_get", "preferences_set_theme"],
    "board": ["board_navigate", "board_query", "board_edit", "board_history"],
    "collaboration": [
        "collaboration_personal_brief", "collaboration_brief_seen",
        "collaboration_general_news_set", "collaboration_questions_inbox",
        "collaboration_questions_answer", "collaboration_question_respond",
        "collaboration_continuation_status", "collaboration_view",
    ],
    "notes": [
        "project_notes_list", "project_note_create", "project_note_get",
        "project_note_reply", "project_participants_list", "project_note_analyze",
        "collaboration_view",
    ],
    "memory": ["memory_read_project", "voice_source_read", "memory_commit_voice_source", "memory_finish_ephemeral"],
    "repositories": ["github_repositories_list", "github_repository_read"],
    "calendar": ["devices_list_capabilities", "calendar_create_event_on_device", "calendar_list_events_on_device"],
    "readiness": ["event_cards_list", "event_readiness_set", "task_create_follow_up", "task_set_state"],
    "knowledge": ["knowledge_search"],
    "expert_reviews": ["expert_reviews_list_assigned", "expert_reviews_get", "expert_reviews_accept", "expert_reviews_resolve", "expert_reviews_request_research"],
    "owner_development": ["backlog_list", "backlog_create", "development_codex_status", "development_execute_backlog", "development_execution_status"],
}

ROUTER = {
    "name": "activate_capability",
    "description": "Load one allowed capability for the current accepted user intent, or return to core. intent is continuation context, never authorization.",
    "parameters": {"type": "object", "additionalProperties": False,
                   "properties": {"capability": {"type": "string", "enum": list(BUNDLES)},
                                  "intent": {"type": "string", "maxLength": 1000}},
                   "required": ["capability", "intent"]},
}
PREFERENCES = [
    {"name": "preferences_get", "description": "Read your own authoritative personal theme and revision.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "preferences_set_theme", "description": "Apply an explicitly requested personal light/dark theme after reading its revision. Claim application only when application_status=applied; pending is saved but screen application unconfirmed. Never infer a command from quoted, negated or background speech.",
     "parameters": {"type": "object", "additionalProperties": False,
                    "properties": {"theme": {"type": "string", "enum": ["light", "dark"]},
                                   "expected_revision": {"type": "integer", "minimum": 0}},
                    "required": ["theme", "expected_revision"]}},
]

OVERLAYS = {'core': 'Read allowed projects before choosing focus. Use runtime_versions_get for actual running '
         'version; never guess.',
 'memory': '- Для явного «запомни/сохрани» и явно долговечной информации используй '
           'memory_commit_voice_source.\n'
           '- Один voice source может относиться к нескольким проектам: сделай отдельный '
           'memory_commit_voice_source для каждого действительно нужного project/result.\n'
           '- В semantic_notes передавай только память для конкретного target project. Полный provider '
           'transcript остаётся личным source и не копируется в project memory.\n'
           '- Для вопроса о ранее сохранённом используй memory_read_project.\n'
           '- Не архивируй каждую бытовую реплику автоматически.\n'
           '- После хотя бы одного успешного memory_commit_voice_source не вызывай '
           'memory_finish_ephemeral для того же source.\n'
           '- Никогда не говори, что что-то сохранено или изменено, пока function result не подтвердил '
           'это.\n'
           '- Source transcript создаёт provider этой же Live-сессии; function tools не являются вторым '
           'AI.\n',
 'repositories': '- Доступ определяет backend. Аргументы function call не могут расширять права или '
                 'подключать новый repository.\n'
                 '- github_repositories_list показывает только уже подключённые и привязанные '
                 'repositories. Если нужного repo нет, скажи, что его должен разрешить workspace owner '
                 'через GitHub integration UI; не пытайся заменить это другим repo.\n'
                 '- Для чтения текущего проекта используй github_repository_read: сначала '
                 'корень/каталог, затем нужный текстовый файл. Не утверждай, что прочитала repository, '
                 'пока tool result не вернул фактический content.',
 'calendar': '- Device-local действие всё равно вызывается здесь, в backend-owned Live session. Android '
             '— только исполнитель typed command.\n'
             '- Для календаря сначала используй devices_list_capabilities, если подходящий телефон '
             'неоднозначен. Для вопросов о расписании используй calendar_list_events_on_device; для '
             'создания — calendar_create_event_on_device.\n'
             '- context.client_timezone — локальная timezone устройства. Слова «сегодня», «завтра», «в '
             '13:45» без явно названной timezone всегда интерпретируй в context.client_timezone. '
             'Никогда не подменяй локальное время UTC. Если пользователь явно назвал другую timezone, '
             'передай её и timezone_explicit=true.\n'
             '- calendar_list_events_on_device только читает локальный CalendarContract выбранного '
             'Android и возвращает ограниченное окно до 31 дня. Не придумывай события, если device '
             'readback не вернулся.\n'
             '- Говори «событие создано» только если calendar_create_event_on_device вернул '
             'status=applied и device readback. pending/claimed означает, что подтверждение на телефоне '
             'ещё ожидается; outcome_unknown означает, что итог надо сверить.',
 'readiness': '- После подтверждённого calendar event backend автоматически создаёт event card. Для '
              'записи подкаста передавай event_type=podcast, иначе generic.\n'
              '- Перед событием используй event_cards_list и называй только фактические незакрытые '
              'пункты checklist.\n'
              '- Меняй checklist через event_readiness_set только после подтверждения пользователя, не '
              'угадывай готовность.\n'
              '- Если реально не хватает подготовки, предложи один конкретный follow-up и создавай его '
              'через task_create_follow_up после согласия.\n'
              '- task_set_state отражает принятие/выполнение/откладывание/отказ; не объявляй task '
              'выполненной без tool result.',
 'knowledge': '- knowledge_search присутствует только когда backend подтвердил отдельный '
              'user-authorized grant к Regional Knowledge resource.\n'
              '- Используй его для региональных фактов, когда полезны книги/журналы и provenance. '
              'Отвечай по evidence, сохраняй различие между источником и своим выводом.\n'
              '- Отсутствие результата не доказывает ложность факта. Не выдавай snippets без evidence '
              'за проверенную истину.\n'
              '- Projects Hub token не является Knowledge token; доступ и ACL проверяет сам Knowledge '
              'resource.\n',
 'expert_reviews': '- Expert-review tools присутствуют только когда backend подтвердил owning-service '
                   'grant и verified expert profile.\n'
                   '- Не решай противоречие сама: объясняй evidence и вызывай typed expert tool только '
                   'после явного решения эксперта.\n'
                   '- Verification score — сила evidence, а не вероятность истины.\n'
                   '- "Нужны ещё источники" является нормальным экспертным исходом.\n'
                   '- Объявляй решение сохранённым только после receipt/readback owning service.\n',
 'owner_development': '- Backlog — первичная сущность работы. Для продуктовой/разработческой задачи '
                      'владельца используй backlog_create; это только сохраняет задачу и никогда не '
                      'запускает разработку.\n'
                      '- Не смешивай readiness/event follow-up с development backlog.\n'
                      '- backlog_list показывает backlog и latest_execution. Никогда не делай вывод '
                      '«разработка не запущена» только из task.state=accepted: accepted — состояние '
                      'backlog, а запуск/фаза находятся в latest_execution.\n'
                      '- Обычное обсуждение, приоритизация, формулировка или добавление задачи в '
                      'backlog НЕ разрешают запуск разработки.\n'
                      '- development_execute_backlog вызывай только если текущий platform owner явно '
                      'попросил реализовать/запустить конкретную существующую задачу или выбранный '
                      'набор задач прямо сейчас.\n'
                      '- Можно запускать 1–5 задач одного проекта одним execution. Задачи разных '
                      'проектов запускай отдельными execution.\n'
                      '- Перед стартом backend сам проверяет owner, native Codex quota >10%, live model '
                      'catalog и отсутствие другого активного owner-run. Если owner profile недоступен, '
                      'сначала вызови development_codex_status, назови доступные native модели и '
                      'попроси владельца явно выбрать модель/effort. Не выбирай Astra/другую модель '
                      'сама и не обходи отказ.\n'
                      '- development_codex_status используй для вопросов об остатке лимита/доступности '
                      'Codex; сообщай фактический remaining_percent и reset/status из tool result.\n'
                      '- development_execution_status используй для «что сейчас делает Codex», '
                      '«закончилось ли», «какой результат». Это только чтение durable state и не '
                      'двигает execution. Не объявляй разработку завершённой раньше terminal status.\n'
                      '- ChatGPT/Codex, запущенные владельцем вне Projects Hub, остаются допустимыми '
                      'способами выполнить ту же backlog-задачу; execution Миры — только один из путей '
                      'исполнения backlog.\n',
 'preferences': 'Read preferences_get, then set an explicit enum with its revision for a clear user '
                'request. A toggle means read and set the opposite explicitly. Ambiguous brightness '
                'needs clarification; device brightness is unavailable. A same-value request is a '
                'no-op. Speak short truthful Russian confirmation only after '
                'persistence_status=verified and application_status=applied. pending: настройка '
                'сохранена, но применение на этом экране пока не подтверждено. superseded or '
                'REVISION_CONFLICT: read current choice and ask before overwriting; never retry with a '
                'new key/revision to bypass an uncertain outcome.'}

OVERLAYS.update({
    "board": "Работай с одной доской текущего проекта внутри личной timeline. На открыть/закрыть используй board_navigate; для видимых объектов board_query view_context, затем board_edit с проверкой revision. Вся запись только через Миру, UI read-only.",
    "collaboration": "На «привет»/«что нового» озвучь лично адресованную collaboration_personal_brief, после фактической сводки brief_seen. Затем проверь collaboration_questions_inbox и задай один существенный ожидающий вопрос аналитики ГОЛОСОМ; не высыпай список. Допустимы четыре исхода: answer с текстом, unknown («не знаю»), skip («пропустить»), later («позже»). Дожидайся backend receipt и не снимай blocker без ответа. Если пользователь сразу поставил другую задачу, она приоритетнее сводки. По умолчанию нет карточек; collaboration_view только на явное «покажи на экране / закрой».",
    "notes": "Проектная заметка — общий объект, не личная переписка. Создавай project_note_create только по явной просьбе; читай и отвечай через project_notes_list/get/reply. На голосовой запрос прочитать или рассказать не открывай виджет. Только на явное «ПОКАЖИ заметку на экране» вызови collaboration_view(view=note, note_id=...). Проверяй участие и полномочия, а сильный анализ получай через provided-only project_note_analyze.",
})
