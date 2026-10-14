# Проектные разрешения dot: чат, Мира и уведомления

Статус: **TO-BE, проектирование; не реализация и не выдача разрешений**. Дата: 2026-10-14.
Проверенный срез AS-IS: [main b44a1f58](https://github.com/onedayonemasterpiece/projects-hub/tree/b44a1f58c6fcf2de8b24b1fdd5ef96ca29c5b90a).
Этот документ не меняет ACL, OAuth grants, bindings, подписки, код или deployment.

## 0. Существующий tunnel и проверенный автор решения · revision 5, 2026-10-14

**Второе MCP-подключение не является обязательным условием голосового согласования. Минимальный целевой путь сохраняет существующий личный Secure MCP Tunnel: Мира/UI сохраняет решение от аутентифицированного участника Hub, а dot читает серверную запись через тот же tunnel. Отдельный Personal OAuth остаётся возможным расширением, только если обнаружится конкретная потребность, которую нельзя закрыть действующим transport и надёжным account binding.**

Основание: [уточнение о личном tunnel и передаче проверенной информации о пользователе в методах](https://app.slack.com/archives/C0C872SJFA7/p1791969743313839), [Hub — коллаборация нескольких пользователей](https://app.slack.com/archives/C0C872SJFA7/p1791968363167599). Предыдущая revision 4 преждевременно превратила два подключения из возможного варианта в target. Эта редакция исправляет именно архитектурную обязательность, не объявляет уже работающим отсутствующий approval API.

### Что означает личное подключение

Личное владение подключением определяет, кому оно доступно в клиенте. Авторизация transport подтверждает вызывающую интеграцию. Авторство конкретного решения подтверждается отдельно источником, который сохранил человеческое действие. Это разные сведения, и все они могут передаваться через одно MCP-подключение.

В текущем Hub credential tunnel представляет service principal. Это не опровержение того, что подключение принадлежит пользователю: backend просто не получает из него автоматически доказательство, что конкретную фразу произнёс этот человек. Добавить в read-method сведения о пользователе правильно, если они вычислены сервером из проверенной сессии/источника и защищённой привязки к получателю; произвольные user_id/actor_id в аргументах инструмента такого доказательства не дают.

### Конкретный AS-IS, проверенный read-only

Проверенный код: [main 76bc5f2d](https://github.com/onedayonemasterpiece/projects-hub/tree/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81). Это проверка репозитория, не production health или live-приёмка.

- [mcp_auth.py](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/src/projects_hub/mcp_auth.py): provision_tunnel переиспользует service principal; grant возвращает principal_type=service. project_owner для service допускает active project, поэтому этой общей видимости недостаточно для персональных approvals.
- [mcp_api.py](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/src/projects_hub/mcp_api.py): authorize_service, отключённый personal consent, token exchange service_only=True. Инструкция сервера прямо отделяет contextual actor_id от authenticated response author. Это ограничивает текущую семантику, но не требует нового OAuth для чтения серверного человеческого решения.
- [auth.py](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/src/projects_hub/auth.py) проверяет подпись и срок session; [app.py](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/src/projects_hub/app.py) получает actor из cookie, проверяет conversation access и передаёт subject/tenant в Live. [live_adapter.py](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/src/projects_hub/live_adapter.py) берёт actor для tools из session.state. Эти механизмы нужно переиспользовать, а не создавать второй identity store.
- store.py уже содержит external_identities(provider, subject) → actor, memberships и project_grants. Коллаборация требует проверки конкретного участника и ресурса; platform owner не становится согласующим по умолчанию.
- [mcp_events.py](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/src/projects_hub/mcp_events.py), get_project_request, делегирует live read в [mcp_bindings.py](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/src/projects_hub/mcp_bindings.py). Там access сверяет client_id, активный binding/revision и доступ к note; ответ уже возвращает answer_receipts с actor_id/command_id/timestamp из сохранённых collaboration_answer_commands. Следовательно, service transport уже способен переносить серверные actor-bound записи. Но question answers не являются разрешениями.
- Существующий каталог пяти tools **не содержит** approval request/decision receipt. Нельзя просто читать answer_receipts и трактовать ответ как grant. Нужна новая typed approval семантика с отдельными ACL и доказательствами.

### Минимальная delta: одно подключение, два проверяемых субъекта

1. **Сохранить transport:** существующий endpoint, tunnel credential, subscriptions и старые tools не менять. Для approvals добавить read contract через этот же service access; не переопределять principal_type=service как human.
2. **Сохранить человеческий источник:** Mira/UI approval_decide получает actor исключительно из аутентифицированной Hub/Live session. Сервер проверяет адресованного approver, полномочия на конкретное действие/ресурс, предъявленный scope/revision и actual input evidence. Tool call или session login сами по себе не являются человеческим «да». Решение и receipt создаются атомарно; MCP service не умеет fabricate/approve as owner.
3. **Добавить доверенную привязку получателя:** серверный binding связывает service client/connection, конкретного владельца dot, Hub actor, workspace/project и разрешённый chat/task/subscriber route. Создание/изменение этой привязки — отдельная авторизуемая операция; этой документацией она не выполняется. Existing discussion binding полезен как pattern, но его platform-owner-only и discussion_reply semantics нельзя выдать за готовый collaborator approval binding. Если один service client обслуживает несколько dot, обязательна проверенная per-recipient привязка; один общий bearer + caller-supplied user_id не даёт isolation.
4. **Вернуть доказательства через read:** approval_request_get(request_id) вычисляет effective recipient из проверенного server binding, проверяет current ACL и возвращает service_principal, authenticated_decision_actor, actor_binding/evidence provenance, request/scope/revision, decision, timestamp и current grant status. Не добавлять user_id как способ назначить автора. Достаточно серверной записи, прочитанной через доверенный authenticated connector; переносимая криптографическая подпись нужна лишь при появлении отдельной границы проверки, не как обязательный новый credential.
5. **Продолжить в нужной dot:** событие — сигнал перечитать запись, не разрешение само по себе. Dot проверяет, что connector удостоверяет автора именно как её authenticated owner, совпадают binding/адресат, проект, exact scope и срок, права не отозваны. Если Hub actor ↔ владелец этой dot пока не доказан, state=identity_unverified и чат-fallback; нельзя автоматически довериться совпадающему имени, email или «мой tunnel».
6. **Минимальный API выбор:** предпочтителен отдельный typed approval_request_get через то же подключение. Допустимо совместимо расширить get_project_request новым discriminated request_kind и receipt, если это не меняет старую семантику/права/подписку. Ни вариант не требует сам по себе второго connection; оба требуют реализации и приёмки.

Для общего персонального каталога проектов Personal OAuth может оказаться удобным отдельным решением. Однако наличие OAuth также не доказывает конкретное человеческое согласие, а общая поддержка нескольких accounts не доказывает нужность второго подключения. Решать по проверенным требованиям и tenant isolation, а не вводить ещё один login заранее.

### Ограниченное read-only сравнение существующих решений

Проверены исходники/документация и доступные tool schemas; новые auth flows, логины, credentials или внешние тестовые операции не запускались.

| Проект | Проверенный контракт | Что можно переиспользовать без замены Hub connection |
| --- | --- | --- |
| [my-browser-bridge](https://github.com/onedayonemasterpiece/my-browser-bridge/blob/9db29261a4ff8c316a1c4671c54068f4bff25c7b/deploy/tunnel/README.md) | Auth на edge/tunnel; отдельная revocable service identity; origin loopback. Browser profile alias — сохранённая browser session, не удостоверение автора MCP-инструкции | Принцип сохранения рабочего service transport и отдельной прикладной проверки. Нельзя выдать browser alias за owner consent |
| [my-data-hub](https://github.com/onedayonemasterpiece/my-data-hub/blob/e7972f7339b0abe28634bd1782c0d517c65ab06e/src/my_data_hub/mcp/oauth.py), [CIMD](https://github.com/onedayonemasterpiece/my-data-hub/blob/e7972f7339b0abe28634bd1782c0d517c65ab06e/docs/operations/chatgpt-cimd-oauth.md) | AccessIdentity с subject/client/scopes, exact issuer/audience/resource, revocation; CIMD дополняет static clients | Контракт principal и revoke/revision checks. Не копировать токены или без причины менять рабочий Hub на CIMD |
| [events-bot](https://github.com/onedayonemasterpiece/events-bot-new/blob/d431b9f86b87999cf27f74b99857d23aedc28f16/private_events_mcp/oauth.py) | Owner/partner subjects, client/resource-bound scopes; tools prepare/commit/review отделяют proposal от owner decision | Frozen scope/revision, current-rights recheck и idempotent receipt. Event-specific approve tool не является универсальным доказательством человеческого согласия для dot |
| [Kaliningrad / Regional Knowledge](https://github.com/onedayonemasterpiece/regional-knowledge-base/blob/fd78310f0e91d59b605f737f90c1d5b3d3508f2c/docs/auth.md) | Stable platform UUID, resource-specific token, собственная identity registry. Read-only profile реально успешно вернул stable account ID | Identity/profile pattern можно добавить там, где уже есть правдивое credential-to-person binding. Чужой resource token/профиль не переносится в Hub |
| [Wonderful Lections](https://github.com/onedayonemasterpiece/wonderful-lections/blob/eb9503b03acbf8f22142201f0cabc6d87017e2d3/docs/operations/chatgpt-connection.md) | Existing browser identity → tenant-bound OAuth, PKCE/revocation; owner не превращается в system_owner через MCP. Есть browser regression history | Поведение consent/identity tests как donor, не обязательный новый auth stack. Его документированные browser/HTTP тесты не заменяют текущую live-проверку connection |
| [Projects Hub P0](../reports/mcp-events-p0-20261013.md), [stable contract](../reports/mcp-events-mvp0-20261013.md) | Реальный idle-dot read/write и stable events прошли через Secure MCP Tunnel и service principal. Public OAuth form failure в отчёте остался unexplained | Использовать этот работающий transport/outbox/bindings. Не диагностировать старую ошибку заново по аналогии с другим продуктом |

У Wonderful Lections отдельно исправлялись Origin:null из no-referrer и CSP на callback; events-bot также имеет [CSP redirect incident](https://github.com/onedayonemasterpiece/events-bot-new/blob/d431b9f86b87999cf27f74b99857d23aedc28f16/docs/reports/incidents/INC-2026-08-08-private-mcp-oauth-csp-redirect.md). **Это возможные классы проверок, не установленная причина сбоя Hub.** В этой задаче возвращаться к ремонту публичного OAuth не нужно.


### D0 fallback и проверяемый следующий шаг

**D0:** широкий запрос в исходном проверенном чате; Мира объясняет pending и даёт ссылку. Пользователь отвечает там, dot проверяет автора/correlation и обновляет projection. Это fallback при недоказанной voice identity или недоступном голосе, не выбранная вместо голоса окончательная архитектура.

**Spike после отдельного поручения на реализацию, сейчас без setup:** (1) fixtures: два участника/два проекта, per-recipient bindings, запрет service-authored decisions, stale/revoke/replay; (2) Mira/UI authenticated input → exact server approval receipt → чтение через существующий tunnel → проверка owner binding в dot; (3) одно безопасное ordinary action без независимо заранее выданного разрешения на само тестируемое действие → exact effect readback; (4) чужой actor/recipient, фоновая речь, поддельный receipt и stale scope не допускают effect. Callback 2xx, JSON с user_id и успешный OAuth не PASS. Модель доверия должна принимать проверенную серверную атрибуцию автора, а не только удобный для агента текст.

Сначала проверить, какую достоверную connection-owner информацию уже даёт существующий tunnel/client и какие trusted bindings доступны Hub. В коде, изученном здесь, не доказана привязка service credential к authenticated owner именно текущей dot. Если её можно добавить или подтвердить в текущем transport, новый connection не нужен. Если нельзя, описать конкретный недостающий trust claim и минимальный способ его получить; только затем сравнивать дополнительный Personal access. Не возвращаться к ремонту старого public OAuth failure без соответствующей задачи.

**Вердикт:** одно существующее подключение достаточно как транспорт серверного решения; источник авторства находится в authenticated Hub/Mira session и проверенной привязке к владельцу dot. Read-method уже переносит actor-bound question receipts, но approval semantics, voice input evidence и полная owner/recipient isolation пока TO-BE. Живая цепочка voice → authorized action ещё не принята; mandatory формы всегда остаются отдельными.

## 1. Решение в одном абзаце

Основная единица согласования — максимально широкий, понятный **набор разрешённых действий в конкретном проекте**, действующий для последующих задач в согласованных границах. dot создаёт один typed approval request. Он сразу становится долговечным запросом в чате; push и Мира ускоряют ответ, но не являются условием доступности. В D0 ответ проверяется в исходном чате, а Hub хранит его ссылку и статус; в минимальном target решение сохраняется от конкретного уполномоченного участника и читается через существующий tunnel с проверенной привязкой автора и получателя. dot получает событие, перечитывает текущее разрешение и продолжает только допустимую работу. Чат, Мира, push и обязательная форма показывают один request_id и одно актуальное решение.

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
- approver_actor_id/approval policy, requester service principal, initiating user, dot conversation/task identity. Термин owner в receipts ниже означает конкретного уполномоченного владельца аккаунта/ресурса, не глобального platform owner.
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

Для обычного предварительного согласия нужен доказанный source-author/account binding и достаточный scope, а не вымышленная универсальная форма host: см. проверенный verdict и spike в разделе 0. При валидном owner instruction повторное «да» в чате не требуется. Отсутствие подтверждённого автора — состояние identity_unverified с чат-fallback; обязательная форма конкретного действия — отдельное awaiting_required_form. Их нельзя смешивать. Решение Hub не отменяет действующие ограничения host/connector.

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

**A. D0 fallback: текстовое согласование + Мира как уведомление.** Сохранять существующие auth/connection. Pending projection с exact chat reference, scope resolver, общий статус, CAS/idempotency/revoke, durable chat + Mira inbox/link. Решение проверяется в исходном чате; Hub не создаёт самостоятельное voice permission. Без Миры согласование работает.

**B. Same-tunnel target: typed approval receipt и доказанная user/tenant/recipient isolation. Мира как второй интерфейс.** Маленькая capability, exact request/revision, input evidence, совместный статус, deep link и мгновенный текстовый fallback. Проверить concurrent chat/voice и provider interruption.

**C. Platform notifications.** Android channel/sound + request deep link, выбранный push transport; Web Push отдельно. Обязательная реальная device/browser acceptance, deny/DND/offline и first-sync pending. Не блокировать A/B ради звука.

**D. Controlled rollout.** Shadow resolve без выдачи grants; один явно выбранный проект/профиль; журнал decisions и delivery health; host evidence acceptance; затем расширять по доказанным результатам. Откат отключает новое создание/исполнение, не удаляя pending requests и receipts; UI объясняет, куда отвечать.

Изменения реализации/развёртывание потребуют отдельного поручения. Никакие новые внешние исполнители или платные тесты в рамках подготовки документа не запускались.

## 10. Матрица приёмки

Все строки ниже — будущие инженерные тесты и критерии приёмки для разработчика; **не выполненные тесты и не обязательные подтверждения пользователя**. Пользователь согласует широкий проектный пакет, а не проходит эту матрицу как процедуру. Реальное участие владельца требуется лишь для необходимых голосовых/device-проверок и предусмотренных обязательных подтверждений.

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
| A29 | Один tunnel, два участника и два проекта | У каждого только адресованные receipts; service reader не impersonates human; cross-recipient read/event rejected |
| A30 | Tunnel принадлежит пользователю, но owner mapping отсутствует | identity_unverified; не approve по caller user_id, имени или одному факту подключения |
| A31 | Серверный receipt доступен через текущий tunnel | Authenticated actor, owner binding и exact scope проверены; ordinary action не требует второго connection или повторного «да» |
| A28 | Невозможна доставка ни в один разрешённый канал | durable failed/unconfirmed + blocked work; никакого auto-approval |

Приёмочные evidence: exact release SHA, request/scope/decision IDs, source owner identity reference, command/event IDs, actual chat message ID, per-channel timestamps, resume ack и readback эффекта. Секреты/сырой голос не включать в публичный отчёт. Fixture PASS не заменяет физическую голосовую/device acceptance.

## 11. Критерий готовности документа и открытые решения

Архитектура не зависит от обязательного наличия Миры и не производит micro-approval на tool calls. Для D0 нужны exact chat route, pending/status projection и допустимый широкий scope/срок. Для same-tunnel target нужны typed server-authored decision receipt, reuse actors/sessions/ACL, проверенная привязка владельца dot и изоляция получателей. Второе подключение не является prerequisite; оно рассматривается только при доказанной отдельной потребности. Push transport выбирается отдельно. Эти решения не должны превращаться в разрешение «всё во всех проектах».

Ссылки: [индекс спецификации](README.md), [безопасность](05-security.md), [базовая коллаборация](20-basic-collaboration-and-personal-timeline.md), [MCP events](../mcp-events.md).

Связанный отдельный трек: [21. Проектная информация: онтология и единый доступ](https://github.com/onedayonemasterpiece/projects-hub/blob/7036e16eea6bebc0603e99588056d7a81dbb6f5b/docs/product/21-project-information-unified-access.md). Его вопрос — единый доступ Миры к проектной информации; данный документ 22 определяет разрешения на действия. Ссылка закреплена на проверенный commit, пока треки находятся в отдельных ветках.
