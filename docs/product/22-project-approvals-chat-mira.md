# Проектные разрешения dot: чат, Мира и уведомления

Статус: **TO-BE, проектирование; не реализация и не выдача разрешений**. Дата: 2026-10-14.
Проверенный срез AS-IS: [main b44a1f58](https://github.com/onedayonemasterpiece/projects-hub/tree/b44a1f58c6fcf2de8b24b1fdd5ef96ca29c5b90a).
Этот документ не меняет ACL, OAuth grants, bindings, подписки, код или deployment.

## 1. Решение в одном абзаце

Основная единица согласования — максимально широкий, понятный **набор разрешённых действий в конкретном проекте**, действующий для последующих задач в согласованных границах. dot создаёт один typed approval request. Он сразу становится долговечным запросом в чате; push и Мира ускоряют ответ, но не являются условием доступности. Ответ реального аутентифицированного владельца сохраняется как проверяемый receipt. dot получает событие, перечитывает текущее разрешение и продолжает только допустимую работу. Чат, Мира, push и обязательная форма показывают один request_id и одно актуальное решение.

Нельзя обещать, что человек обязательно увидит уведомление при полном отключении связи/уведомлений. Гарантируем отсутствие **тихой потери**: запрос и причина остановки сохраняются; доставка, просмотр и решение различимы; сбой виден; после восстановления происходит сверка.

## 2. Основание и границы

Прямые уточнения владельца прочитаны в полном Slack-треде, включая окончание пагинации:
- [MCP → push → Мира → реальный ответ](https://app.slack.com/archives/C0C872SJFA7/p1791964236102509).
- [Не дробить на микродействия](https://app.slack.com/archives/C0C872SJFA7/p1791964267182789).
- [Максимально широкие границы проекта](https://app.slack.com/archives/C0C872SJFA7/p1791964339768299).
- [Чат обязателен, Мира может не работать; поручение проектировать](https://app.slack.com/archives/C0C872SJFA7/p1791964491151789).

Это проектирование механизма, а не согласие на перечисленные в примерах операции. Обычный аналитический ответ, согласие обсудить вопросы или кнопка «Продолжить разговор» не создают разрешений. Проектный grant выражает намерение владельца, но не создаёт технические credentials и не заменяет права внешних систем.

## 3. AS-IS: что действительно найдено

| Слой | Проверенный факт | Что ещё отсутствует |
| --- | --- | --- |
| MCP | [mcp_api.py](../../src/projects_hub/mcp_api.py) публикует start_question_session, get_project_request, record_request_result и legacy test tools | Нет typed project approval request/grant/decision API |
| Question sessions | [collaboration_analysis.py](../../src/projects_hub/collaboration_analysis.py): durable ответы/command receipts, отдельный accept/pause/resume/complete, проверка адресата и source/binding revision | Эти receipts относятся к аналитическим ответам, не к полномочиям dot |
| Startup Миры | [PR #170](https://github.com/onedayonemasterpiece/projects-hub/pull/170), merged 2026-10-14; версия исходников 0.1.62: question tools доступны при pending sessions в core, бюджет менее 10 tools | Наличие question tools не доказывает approval flow, физическое голосовое согласование или push |
| Identity | [collaboration_analysis_api.py](../../src/projects_hub/collaboration_analysis_api.py) получает actor из authenticated request; MCP service actor отделён от context actor | Нет отдельного доверенного owner approval receipt и voice-to-decision проверки |
| Events | [mcp_events.py](../../src/projects_hub/mcp_events.py), [контракт MCP](../mcp-events.md): request/outbox, подписки, корреляция, retries, expiry | HTTP acceptance callback не означает обработку dot, уведомление человека или согласие |
| Slack | [mcp_bindings.py](../../src/projects_hub/mcp_bindings.py): exact route binding, claim/receipt, reconciliation неизвестной отправки | Нельзя превращать утверждение service client «владелец согласился» в owner decision |
| Результаты анализа | В данном срезе result-ready Mira event создаётся после confirmed Slack receipt | Отдельный result-notifications fix в [chatgpt/question-result-greeting-20261014](https://github.com/onedayonemasterpiece/projects-hub/tree/chatgpt/question-result-greeting-20261014), проверенный head c3e78c53, не входит в этот документ; его код/ветка здесь не меняются |
| Android | [Notifier.java](../../android/app/src/main/java/com/kenigevents/projectshub/Notifier.java): общий канал projects_hub, default importance; запуск MainActivity без approval deep link | Нет отдельного approval channel/sound/deep link |
| Background | [DevelopmentCompletionJob.java](../../android/app/src/main/java/com/kenigevents/projectshub/DevelopmentCompletionJob.java): периодический JobScheduler 15 минут, device credential, dedup | Это не realtime push; его first-sync suppression непригоден для сокрытия старых pending approvals |
| PWA | [sw.js](../../web/public/sw.js): install/activate/fetch и offline shell | Нет push/notificationclick handler или подтверждённой Web Push доставки |

Версия 0.1.62 и PR #170 подтверждены исходниками; фактический production health в этом задании не опрашивался. Сообщённый production baseline 0.1.62 не является новым runtime acceptance. Датированный [question-session checkpoint](../reports/question-sessions-delivery-20261014.md) содержит историческую версию и не должен читаться как текущий статус.

## 4. TO-BE: модель разрешения и минимизация запросов

Разделить три домена:
1. **AnalysisQuestion**: знания, требования, варианты; не разрешение.
2. **ProjectApproval**: намерение владельца и разрешённая область автономной работы.
3. **TechnicalConfirmation**: обязательная внешняя форма, action-time approval или действие самим владельцем.

Перед запросом resolver проверяет актуальные уже выданные разрешения. Если операция покрыта, новый запрос не нужен. Предлагаемый широкий профиль объединяет редактирование, тесты, исправления, retries, коммиты, PR, merge и deployment **только если соответствующие действия явно включены владельцем**, для названных репозиториев/окружений. Новые задачи внутри этого профиля не требуют нового согласования.

Не спрашивать на каждый tool call. Повторный вопрос нужен при материальном расширении области, назначения, данных, лимита, обязательств или риска; истечении/отзыве; обязательном подтверждении конкретного действия. Объединять прогнозируемые исключения в один понятный пакет, где правила это допускают.

### Canonical scope envelope

- workspace_id, project_id и стабильная project/repository identity; имена только для показа.
- owner_actor_id, requester service principal, dot conversation/task identity.
- allowed_action_categories и exclusions: что dot вправе выполнять самостоятельно, включая новые задачи и retries.
- resources/destinations: конкретные repos, ветки, environments, сервисы; для communications — аудитория и ограниченная категория информации.
- data_categories и recipient boundaries; чувствительные данные требуют конкретного получателя, а секреты не входят в голосовой текст/чаты/receipt.
- side_effects и risk class, financial limits/currency/recurrence, если применимо; отсутствие лимита не означает unlimited spend.
- valid_from, expires_at либо явно выбранное owner «до отзыва»; последнее только для допустимой категории. Request decision deadline и grant lifetime — разные поля. Не навязывать короткий TTL проектному grant ради технического TTL webhook.
- scope_revision, canonical digest, schema/policy version, owner-visible summary и полный текст условий.
- change/revoke history; проверка существования проекта и полномочий владельца на момент решения и исполнения.

В первом запросе показывать один рекомендуемый широкий пакет; детали раскрываются. Сужение по ответу владельца формирует новую точную редакцию, без скрытого расширения. Если смысл неоднозначен, уточнить только неоднозначную часть.

## 5. Архитектура и контракт API

Все имена ниже предлагаемые, ещё не существующие.

**Approval service** владеет requests, project grants, decisions, delivery projections и outbox. Авторизация выполняется на сервере; аргументы инструмента не назначают владельца и не предоставляют права.

MCP для dot:
- approval_request_create(project_id, scope_proposal, origin_binding_id, command_id, blocked_work_refs). Не принимает произвольный callback или чат-получателя.
- approval_request_get(request_id) и approval_inbox(project_id, cursor).
- approval_delivery_claim/receipt для уже разрешённого chat route, по существующему паттерну uncertain-send reconciliation.
- approval_resume_ack(request_id, decision_id, task_id, command_id): подтверждает получение решения исполнителем, не выполнение работы.

Owner-facing authenticated REST/UI и Live capability:
- approval_get/inbox.
- approval_decide(request_id, expected_revision, decision, command_id, evidence_ref); реального actor определяет auth adapter.
- approval_revoke(grant_id, expected_revision, command_id); amendment с расширением проходит новое согласование.
- approval_seen отдельно от decide.

Не давать MCP service principal общий «approve as owner». Для Slack нужен доверенный ingestion adapter: проверенный workspace/user mapping, подпись события или серверная верификация оригинального сообщения через доверенный connector, exact parent request/revision и immutable message ID. Переданный dot текст/actor_id, скриншот, display name или его пересказ не достаточны. Если такой adapter недоступен, Slack остаётся местом самого вопроса и ссылки на authenticated approval page; voice тоже не обход.

Повторно использовать transactional outbox и identity/ACL primitives, но не расширять purpose аналитического question_package до permissions. В существующем projects_hub.request.created можно добавить явно discriminated request_kind=approval_decided, если совместимость и подписка это допускают; иначе новый versioned event с отдельным разрешённым setup. Существующая подписка не считается автоматически разрешённой для новой смысловой области.

Событие содержит IDs, revision и безопасный summary, без секретов и полного sensitive scope. Получатель аутентифицированно перечитывает запись. New subscription/credential/permission setup выполняется только отдельным требуемым подтверждением; этот дизайн ничего такого не создаёт.

### State и атомарность

Request: pending → approved / denied / expired / cancelled / superseded.
Промежуточное awaiting_required_form означает, что голосовой/чат-ответ сохранён, но разрешение ещё не действует.
Grant: active → expired / revoked / superseded.
Work: waiting_approval → resumable → running / blocked_elsewhere / completed; approved не равно resumed и не равно completed.

Одна транзакция сохраняет решение, owner receipt, grant (если допустим) и continuation outbox. CAS по revision предотвращает два разных решения. Unique command_id + canonical payload digest: одинаковый retry возвращает тот же receipt; другие данные с тем же ID — conflict.

Delivery хранится отдельно по destination: queued / accepted / presented / seen / failed / unconfirmed. Failure Миры не задерживает чат и наоборот. Перенять исправленный независимый result-delivery pattern после его отдельного review, не соединять два релиза.

### Concurrent approvals

Два разных request для одного проекта могут сосуществовать по разным scope; UI всегда называет проект и различающий краткий предмет. Эквивалентные pending scope deduplicate и добавляют blocked_work_refs. Несовместимые/перекрывающиеся amendments показывают delta и base grant revision. Нельзя склеить два grant так, чтобы получить не согласованную владельцем комбинацию назначения/данных/риска.

Одновременные «да» из чата и голоса дают один decision; второй канал получает актуальный статус. «Да» против «нет» не перезаписывают историю: первый CAS фиксирует решение, поздний конфликт показывается владельцу; явное последующее «отзываю» — отдельная revoke-команда. Revocation проверяется перед каждым новым внешним side effect; уже завершённые действия не откатываются автоматически. Reservation/CAS на execution admission задаёт границу гонки revoke/start; уже начатое действие помечается in-flight и безопасно прерывается, если сервис поддерживает это.

## 6. Реальный owner receipt и голос

Receipt: decision_id, request_id, scope_revision/digest, authenticated owner identity, channel, source message/utterance ID, session/device binding, presented scope revision, server timestamp, decision и ограничения, authentication assurance, required-form receipt при наличии, immutable audit reference.

Не считать голос биометрической идентификацией. Аутентифицированная сессия сама по себе также не доказывает, что любую фоновую речь произнёс владелец. Решение разрешено лишь в активном явном approval turn, после предъявления точного scope и с проверяемым evidence от input path. Playback Миры, tool arguments, replay старой речи, синтетический transcript и фоновое «да» не являются решением.

Мира кратко называет проект, широкий набор действий, существенные назначения/лимиты/последствия/срок и спрашивает решение. «Открой», «прочитай», «давай обсудим» не approve. При нескольких запросах несвязанное «да» требует уточнения. При ненадёжном распознавании/прерванном объяснении — уточнение или кнопка authenticated UI. Минимально необходимый transcript/evidence хранится приватно с retention, raw audio не сохраняется по умолчанию; в логи идут IDs и digest.

Capability approvals: 3–6 functions с router; небольшой core содержит только pending count/ID и вход в capability. Следовать [общей Live-архитектуре](https://github.com/onedayonemasterpiece/live-interaction/blob/main/docs/live-agent-architecture.md). Не добавлять второй semantic agent/ASR и не расширять eager startup сверх бюджета. Pending approvals не вытесняют controls question sessions; deep link может сразу выбрать approvals overlay с работающими tools. Транспортный reconnect не воспроизводит mutation.

## 7. Обязательные формы и внешняя авторизация

Project approval никогда не:
- вводит/передаёт пароль, API key и другие особо чувствительные credentials вместо владельца;
- обходится вместо mandatory action-time approval для необратимого удаления, persistent-access/security изменения и прочих обязательных случаев;
- заменяет обязательную форму провайдера, OAuth consent, платёжное подтверждение или handoff;
- превращает медицинские/финансовые/юридические обязательства в обычную фоновую операцию;
- автоматически создаёт или меняет правила разрешений в dot.

Для каждого обязательного шага linked TechnicalConfirmation хранит provider/action ID, exact scope/amount/destination, expiry и verified terminal receipt. Чат и Мира объясняют, что и где требуется; по голосовому «да» можно открыть/показать правильную форму, но не отметить её принятой. Проверяется доверенный provider callback/readback, а не пересказ агента. Смена суммы/адресата/условий инвалидирует старую корреляцию. Истечение формы не создаёт новую автоматически принятую форму.

Grant в Projects Hub не обязывает dot host считать его достаточным подтверждением. До production нужен документированный trusted receipt ingress/consumer contract с host. Если host не принимает такое evidence или требует собственную форму, задача остаётся waiting_required_form, а пользователь получает точную причину и доступный чат-путь. Это обязательный integration gate, не ошибка, которую можно скрыть успешным голосовым ответом.

## 8. Обязательный чат и надёжная доставка

1. Create атомарно сохраняет request, pending block в личной ленте и intent отправки в исходный проверенный чат dot. При недоступности Projects Hub dot всё равно спрашивает прямо в существующем чате и честно отмечает, что Мира пока недоступна.
2. Исходный чат может быть ChatGPT или Slack: approval не должен зависеть от наличия Slack binding или Slack confirmed receipt. Для ChatGPT требуется свой проверенный conversation/owner adapter, без фиктивного Slack route. Chat send не зависит от запуска Миры, push registration или наличия активной Live-сессии. Чат содержит проект, понятный scope, последствия/лимиты, статус «жду решения» и способ ответить без голоса.
3. Неизвестный исход отправки — unconfirmed; прочитать exact thread/request marker, затем reconcile. Не дублировать запрос вслепую. При отсутствии подтверждённого destination не выбирать новый канал наугад.
4. Push ведёт к этому же request. При неработающем голосе открывается текстовая карточка с кнопками и полными условиями. Звук/открытие push/seen не дают согласия.
5. Внутри Projects Hub pending approvals остаются в inbox и личной ленте, включая первый sync после установки. В исходном чате сохраняется видимая причина блокировки. Список сортируется по сроку/последствиям, одинаковые напоминания группируются.
6. Предлагаемый delivery SLO: при здоровых сервисах chat attempt ≤10 секунд, устойчивый retry после известных временных ошибок с backoff; отсутствие подтверждённой отправки более 60 секунд создаёт operational alert. Эти цифры — целевые требования, не измеренный AS-IS.
7. Напоминание при отсутствии owner seen/decision — только по допустимым notification settings, с quiet hours и ограниченной частотой; важный deadline может требовать более раннего напоминания. Seen прекращает напоминания «не доставлено», но не снимает блокировку.
8. Decision deadline истёк: request=expired, grant не создаётся. Просроченный ответ требует новой актуальной редакции. Ни timeout, ни silence, ни недоступность сервиса не дают auto-approval.
9. Continuation event retries используют тот же eventId; dot deduplicates decision_id/task_id, читает live scope/ACL и фиксирует resume ack. При недоступной/истёкшей подписке решение не теряется: pending continuation виден в inbox и восстанавливается после разрешённого renewal. Accepted callback без resume ack остаётся awaiting_resume; UI не говорит «dot продолжил».
10. Если оба канала недоступны, сохранять delivery failure и blocked work, показывать при восстановлении; не объявлять delivered/seen. Доставка в другой канал допустима только по подтверждённому заранее fallback route.

### Android и PWA: отдельный звук

Android: отдельный notification channel «Разрешения проектов», собственный default sound при первоначальной настройке, deep link с request_id; пользователь сохраняет полный контроль звука/importance/DND. После создания channel приложение не может насильно изменить его поведение; показывать системные настройки. Это поддерживаемая архитектура, но слышимый звук на реальном устройстве требует приёмки. [Android channels](https://developer.android.com/develop/ui/compose/notifications/channels).

Android 13+ требует POST_NOTIFICATIONS для обычных уведомлений; отказ означает текстовый/in-app путь без навязчивого повторного запроса. [Android runtime permission](https://developer.android.com/develop/ui/compose/notifications/notification-permission).

PWA: стандарт NotificationOptions не определяет custom sound URL; поэтому отдельный звук не является переносимой гарантией. Использовать различимые title/icon/tag и deep link, а доступность звука определяет браузер/OS. [WHATWG Notifications API](https://notifications.spec.whatwg.org/#dictdef-notificationoptions). Foreground audio не выдавать за background push. Нужны отдельные Push subscription/service-worker transport и проверка consent; создание credentials/доступа не входит в этот дизайн.

Фоновый 15-минутный Android polling нельзя считать мгновенным push. Минимальный первый релиз честно работает через обязательный чат и foreground inbox; native/Web Push transport — отдельный этап с разрешённой настройкой. Не использовать call/alarm bypass ради звука.

## 9. Минимальные стадии

**A. Contract + chat-first MVP.** Отдельный approval domain/schema; scope resolver; trusted owner chat/UI decisions; required-form adapter contract; CAS/idempotency/revoke; durable chat + inbox; continuation/outbox + resume ack. Без Миры продукт уже завершает допустимое согласование.

**B. Мира как второй интерфейс.** Маленькая capability, exact request/revision, input evidence, совместный статус, deep link и мгновенный текстовый fallback. Проверить concurrent chat/voice и provider interruption.

**C. Platform notifications.** Android channel/sound + request deep link, выбранный push transport; Web Push отдельно. Обязательная реальная device/browser acceptance, deny/DND/offline и first-sync pending. Не блокировать A/B ради звука.

**D. Controlled rollout.** Shadow resolve без выдачи grants; один явно выбранный проект/профиль; журнал decisions и delivery health; host evidence acceptance; затем расширять по доказанным результатам. Откат отключает новое создание/исполнение, не удаляя pending requests и receipts; UI объясняет, куда отвечать.

Изменения реализации/развёртывание потребуют отдельного поручения. Никакие новые внешние исполнители или платные тесты в рамках подготовки документа не запускались.

## 10. Матрица приёмки

Все строки ниже — будущие инженерные тесты и критерии приёмки для разработчика; **не выполненные тесты и не 28 обязательных подтверждений пользователя**. Пользователь согласует широкий проектный пакет, а не проходит эту матрицу как процедуру. Реальное участие владельца требуется лишь для необходимых голосовых/device-проверок и предусмотренных обязательных подтверждений.

| ID | Сценарий | Ожидаемый результат / evidence |
| --- | --- | --- |
| A01 | Один grant, 20 новых задач и retries внутри scope | Один owner decision, 0 повторных вопросов; execution audit с grant revision |
| A02 | Новый repo/destination/data/risk/spend | Delta request, старый grant не расширен |
| A03 | Только аналитический ответ/accept question session | Ни grant, ни execution authorization не созданы |
| A04 | Мира выключена, quota/provider/WSS failure | Исходный чат + text decision/UI работают; pending block видим |
| A05 | Projects Hub API недоступен при запросе | dot спрашивает в чате; не сообщает о созданном backend request без receipt |
| A06 | Push denied/DND/offline/первый sync | Запрос не скрыт, sound не обещан, inbox восстанавливает pending |
| A07 | Chat send timeout после фактической отправки | Readback/reconcile, ровно одно сообщение, сохранён message ID |
| A08 | Chat и voice отвечают одновременно | Один CAS decision/receipt, второй видит тот же terminal state |
| A09 | Одновременные конфликтующие approve/deny | Нет last-write-wins; конфликт виден, последующий revoke отдельный |
| A10 | Два проекта/запроса и неоднозначное «да» | Никакого согласия без точной корреляции |
| A11 | Поддельный actor/tool receipt/ботовое сообщение | Decision rejected; service principal не может impersonate owner |
| A12 | Фоновая речь/playback/replay/плохой transcript | Нет grant; уточнение/кнопка, актуальный input evidence |
| A13 | Request/grant expired, cancelled или revoked | Исполнение запрещено; поздний callback не оживляет |
| A14 | Revoke на границе external action | Admission barrier проверен; in-flight явно отмечен; новые effects остановлены |
| A15 | Повтор command/event после restart | Один receipt, один continuation, никаких повторных side effects |
| A16 | Ответ approved, callback 2xx, dot не resume | awaiting_resume, truthful UI, durable recovery; не «работа продолжается» |
| A17 | Required external form/handoff | Голос не принимает форму; только trusted provider receipt разблокирует |
| A18 | Host не принимает Projects Hub evidence | Чат объясняет host-required confirmation, bypass отсутствует |
| A19 | OAuth/subscription/binding revoked | Каждый read/decide/execute rechecks access, delivery не раскрывает scope |
| A20 | Scope edit после предъявления владельцу | Старый digest не approve новую редакцию; explicit delta |
| A21 | Secret/sensitive payload на lock screen/logs | Без секретов и лишних данных; owner-only scope read, audit sanitization |
| A22 | Android channel fresh/existing/user-muted | Проверенный deep link, respect settings, реальный звук отдельно от delivery |
| A23 | PWA foreground/background/unsupported push | Работает текстовый fallback; custom sound не заявлен |
| A24 | Result-notifications fix развивается параллельно | Старые question sessions/receipts/аналитика не меняют семантику |
| A25 | Много pending requests + restart/capability switch | Рабочие tools при offer, budget <10, не replay approval mutation |
| A26 | Утрата ownership/проект удалён/ACL изменён | Grant больше не допускает действие, historical receipt сохранён |
| A27 | Backend решение committed, ответ потерян | Retry с тем же command возвращает exact receipt; не повторный вопрос |
| A28 | Невозможна доставка ни в один разрешённый канал | durable failed/unconfirmed + blocked work; никакого auto-approval |

Приёмочные evidence: exact release SHA, request/scope/decision IDs, source owner identity reference, command/event IDs, actual chat message ID, per-channel timestamps, resume ack и readback эффекта. Секреты/сырой голос не включать в публичный отчёт. Fixture PASS не заменяет физическую голосовую/device acceptance.

## 11. Критерий готовности документа и открытые решения

Архитектура не зависит от обязательного наличия Миры и не производит micro-approval на tool calls. До реализации требуется выбрать конкретный trusted owner evidence adapter для каждого канала, подтвердить контракт host, выбрать push transport и согласовать проектный scope profile/срок. Эти решения не должны превращаться в разрешение «всё во всех проектах».

Ссылки: [индекс спецификации](README.md), [безопасность](05-security.md), [базовая коллаборация](20-basic-collaboration-and-personal-timeline.md), [MCP events](../mcp-events.md).

Связанный отдельный трек: [21. Проектная информация: онтология и единый доступ](https://github.com/onedayonemasterpiece/projects-hub/blob/7036e16eea6bebc0603e99588056d7a81dbb6f5b/docs/product/21-project-information-unified-access.md). Его вопрос — единый доступ Миры к проектной информации; данный документ 22 определяет разрешения на действия. Ссылка закреплена на проверенный commit, пока треки находятся в отдельных ветках.
