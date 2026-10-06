# Projects Hub — PH-BOARD-ANALYTICS-2026-10-05-R1

**Действующая исполнительная постановка. Обновлено 06.10.2026 после закрепления CORE-BOARD-VOICE-LLM-FIRST и CORE-BOARD-ANALYTICS-ISOLATION.**

Продолжай issue #86, draft PR #87 и ветку `docs/board-analysis-20261005`. Не начинай новый общий аудит и не повторяй сохранённую консультацию Kimi K3 с нуля. Перед изменением общих файлов сверяй свежий `main` и сохраняй принятый voice baseline.

Каноническая продуктовая спецификация: [19-board-and-strong-analysis.md](../product/19-board-and-strong-analysis.md). Исторический аудит и консультация остаются источниками контекста, но их ранние рекомендации про классический manual collaborative canvas не имеют силы, если противоречат текущим CORE requirements.

## 1. Нормативный продуктовый контракт

### CORE-BOARD-VOICE-LLM-FIRST

Доска — **voice-first и LLM-first**.

- Пользователь говорит с центральной Мирой Live.
- Пользователь **не** создаёт, не редактирует, не перетаскивает, не ресайзит и не печатает текст прямо в объекты доски.
- Все долговечные board-мутации делает только Мира через typed board-tools от имени аутентифицированного пользователя и в пределах его project ACL.
- Нормальный `execution_origin` для object mutations — `mira`; `analysis_publish` допускается только для публикации сохранённого результата. `direct_ui` — скрытый owner/debug path, не обычный пользовательский UX и не acceptance path.
- UI доски — в основном **read-only Pixi/WebGL scene**. Допустимы минимальные view-controls: pan/zoom/pinch камеры, focus, «показать всё», открытие read-only history/report/share panels.
- Guest всегда view-only.
- Открытие/закрытие доски не создаёт вторую Live-сессию и не ломает microphone/transcript/следующий turn.

### CORE-BOARD-ANALYTICS-ISOLATION

Strong-model analytics — отдельная provided-context-only capability.

- Вход — frozen snapshot явно выбранных board objects + explicit question.
- `consult_model` / `council_run`: `context_mode=provided_only`, `evidence_bundle`, стабильный `request_key`.
- Analytics allowlist не содержит `start_task` и других development-write tools.
- Нельзя читать произвольный project checkout, HOME, логи, connected resources или чужие источники вне evidence bundle.
- `dispatch_unknown` означает неизвестный исход уже сделанного dispatch; blind retry запрещён.
- Результат хранится как durable Markdown/structured artifact и может быть опубликован на доску как `document_card` через отдельный receipt.
- Owner self-development остаётся отдельным явным контуром и не запускается автоматически из анализа.

## 2. Продуктовый результат

Пользователь должен иметь возможность сказать:

- «Открой доску проекта» / «Закрой доску».
- «Добавь красный стикер: проверить доступность зала».
- «Измени этот стикер: …».
- «Сделай его зелёным».
- «Перемести ближе к стикеру про аренду».
- «Разложи эти идеи аккуратнее» / «Сгруппируй похожие».
- «Удалить этот стикер» / «Восстанови удалённый».
- «Найди заметку про аренду» / «Перейди к ней» / «Покажи всё».
- «Кто добавил и кто менял этот стикер?».
- «Проанализируй эти три идеи с Kimi/DeepSeek».
- «Пусть несколько моделей обсудят слабые места».
- «Помести выводы на доску».
- «Поделись доской».

Наблюдаемый результат — authoritative server state + receipt/history/event, а не локальная анимация без сохранения.

## 3. P0 — сначала это

### A. ACL и server authority

- Одна доска на project.
- Один канонический project access resolver.
- Viewer читает сцену/разрешённую историю, но не создаёт доску скрыто и не запускает analysis.
- Editor означает право **Миры** выполнять board-mutations от имени actor; UI не превращается в manual editor.
- `manage_share` отдельно.
- Negative tests: open/snapshot/search/history/command/start_analysis/create_share по чужому project/workspace, guessed IDs и revoked grants.
- Board state, seq, event, receipt/idempotency — одной транзакцией.
- Soft-delete/tombstone + restore/history сохраняются.

### B. Read-only Board UI + sync

- PixiJS/WebGL renderer, сетка, качественный цветной sticky/document_card.
- UI без primary create/edit/delete/move/resize toolbar.
- Без contenteditable/textarea поверх стикера, drag handles и resize handles в normal product mode.
- Pan/zoom/pinch двигают только локальную камеру.
- WebGL context loss → восстановление из server scene; никакого чёрного экрана с потерей данных.
- Отдельный Board WSS: snapshot + bounded tail, one-use ticket, origin check, seq/gap handling, reconnect/resync.
- Presence — максимум минимальный viewer indicator; cursor/drag multiplayer не core.
- Не внедрять CRDT/Yjs/OT/Redis/Kafka ради v1.

### C. Live board overlay — та же Мира

Compact typed surface, без десятков eager tools:

- open_board / close_board;
- create_sticky;
- update_sticky (text/color);
- move_sticky / arrange (семантический layout hint, «ближе», «сгруппируй», «разложи»);
- delete_sticky / restore;
- search_board;
- focus_object / show_all;
- read_history;
- start_analysis / publish_analysis;
- prepare_share / revoke_share.

Допустимо сохранить существующие compact family tools (`board_navigate`, `board_edit`, `board_analysis`, `board_share`), если их closed schemas покрывают те же действия и не превращаются в произвольный JSON executor.

BoardShell не владеет microphone lifecycle. Open/close — UI mode/tool overlay в **той же** Live conversation.

### D. Structural viewport context

Мира должна получать структурный context, а не скриншот как основной механизм:

- actor/conversation/project/board/client instance;
- board_seq/view epoch;
- focus/selected object;
- visible object ids;
- короткий текст/тип/style/revision/bbox видимых/выбранных объектов;
- truncation marker и возможность search/read подробнее.

Клиентский текст не является ACL evidence: backend достаёт canonical object data сам.

Search result содержит `object_id + revision + bbox`. Перед focus клиент сверяет локальную scene/revision. Focus завершён только после client ACK `applied/cancelled/unavailable`.

## 4. P1 — durability и безопасность

### Analytics

- Только provided-only.
- Durable `AnalysisRun`: frozen revisions/hash, exact model/profile, provider task binding, status, result, source_changed.
- Lifecycle: queued/dispatching/running/completed/failed/cancelled + `dispatch_unknown` + `waiting_capacity`.
- NVIDIA не требует пользовательского budget confirmation.
- Free council остаётся OpenCode-only и не делает скрытый NVIDIA fallback.
- `council_pro` = ровно Kimi K3 + DeepSeek V4.1 Flash через direct provided-only NVIDIA transport.
- Два distinct NVIDIA credential/project slots = максимум два concurrent provider inference-вызова; третий ждёт bounded capacity.
- Credential values не попадают в task records/logs/results.
- Live conversation имеет приоритет над аналитической нагрузкой.
- Cancel/late-result/revoked access не должны автоматически публиковать результат.

### Guest/share

- Семидневный high-entropy capability token, server stores hash only.
- Fragment → POST exchange → HttpOnly guest session.
- Guest HTTP/WSS повторно проверяют expiry/revoke; revoke закрывает уже открытый guest socket.
- Guest projection не содержит private document body/reference, members, email, conversation, hidden history.
- Guest UI без microphone/analysis/edit/history.
- Guest routes network-only/no-store/noindex/no-referrer; service worker не выдаёт private cache fallback.
- PWA `navigator.share` только из user gesture.
- Android — только narrow `share.open_chooser`; receipt `chooser_opened=true`, `delivery_confirmed=false`.

### GitHub materialization

Internal Markdown — canonical. GitHub — optional deterministic materialization:

- только same-project repository binding;
- `role=generated_artifacts`;
- `access_mode=app_managed_write`;
- bounded allowed path (например `docs/analysis/<run-id>.md`);
- duplicate content → synced/reused без нового commit;
- update → guarded expected SHA;
- public repo → отдельное явное подтверждение публикации полного текста;
- GitHub outage не удаляет и не обесценивает внутренний report.

## 5. P2 — после P0/P1

- Event retention/pruning: reconnect tail bounded, object history/audit не удалять вместе с transport tail.
- Viewer presence минимальный и опциональный.
- Performance profile на 500 sticky Android / 1000 desktop, но не обещать FPS без измерения.
- Accessibility/read-only semantic representation.
- Не строить Kanban, Figma import, camera digitization, полноценный manual editor или бесконечный council в этой поставке.

## 6. Работа с текущей веткой и voice lane

Продолжай существующие issue #86 / draft PR #87. Не создавай новый board PR без необходимости.

Перед общими файлами (`App.tsx`, `app.py`, `live_adapter.py`, `live_runtime.py`, package/version metadata) всегда сравнивай со свежим `main`.

Свежий voice baseline имеет приоритет. Нельзя восстанавливать старый App/live file целиком и терять PH-VOICE fixes. Board/analytics — отдельные модули + тонкие adapters.

Не трогай локальные dirty/untracked артефакты другого writer’а без доказательства владения. После interruption — readback текущего состояния и продолжение, не повторная реализация постановки.

## 7. Этапы A–F

**A — authority/contracts**  
Project ACL, Mira-only write origin, schemas/migrations, board server authority, negative authorization tests.

**B — read-only renderer/sync/history**  
Pixi scene, view camera, board WSS snapshot/tail, history/restore; никакого normal manual object editor.

**C — voice/Mira integration**  
Open/close same Live session, compact board tools, structural viewport context, search/focus/show-all + client ACK.

**D — isolated analytics**  
Single Kimi/DeepSeek first; durable provided-only runs; bounded council; Markdown/questions/source_changed; no development-write path.

**E — guest/share/materialization**  
Seven-day view-only guest, revoke, PWA/Android share, optional deterministic GitHub sync.

**F — combined acceptance/release**  
Current voice + board + analytics + guest on one baseline; browser/provider/Android/performance evidence; cleanup and acceptance report.

## 8. Обязательные приёмочные сценарии

Каноническая матрица T01–T28 находится в product spec; трактовать её через voice-first contract. Критические сценарии:

1. «Открой доску» / «Закрой доску» — microphone, transcript и следующий turn живы.
2. Мира создаёт/обновляет/перемещает/удаляет sticky → server receipt, canvas event, history.
3. В normal UI нет object create/edit/drag/resize/text input.
4. Два пользователя через независимые Mira sessions меняют разные объекты → обе команды сходятся.
5. Две Миры с одной expected revision меняют один object → один commit + явный conflict/readback; никакого silent LWW.
6. Commit принят, ACK потерян → retry с тем же command_id возвращает прежний receipt без duplicate mutation.
7. Reconnect после неясного board-command → snapshot/tail + receipt; старый intent не пересылается с новой revision.
8. Голосовой search→focus → актуальный bbox/client ACK; stale/deleted object не уводит камеру в пустоту.
9. Structural viewport context отражает visible ids/text/focus/revisions и не требует screenshot OCR.
10. Viewer/guest/direct API не могут мутировать или стартовать analysis.
11. Frozen analysis → completed Markdown; board меняется после freeze → source_changed.
12. Hostile evidence не получает filesystem/start_task/web/admin capability.
13. Duplicate analysis request_key → один provider dispatch; uncertain dispatch → dispatch_unknown, не blind retry.
14. `council_pro` автоматически использует Kimi+DeepSeek без user confirmation; максимум два concurrent NVIDIA inference, третий ждёт capacity.
15. Guest revoke при открытом WSS → сокет закрывается, snapshot/tail больше не выдаются.
16. Guest не получает private document body/reference.
17. Result publication создаёт document_card, но не меняет исходные stickies автоматически.
18. GitHub materialization: create → duplicate reuse → guarded update; public target требует отдельного publish confirmation.
19. Android chooser не заявляет доставку; PWA share требует user gesture.
20. WebGL context loss и reconnect не уничтожают scene.
21. Combined Live+board+analysis не блокирует PCM/turn/tool results.

## 9. Чего не делать

- Не строить manual canvas editor «на всякий случай».
- Не вводить CRDT/Yjs/OT.
- Не смешивать analytics с owner-development.
- Не добавлять второй ASR/LLM/router рядом с Мирой.
- Не выдавать free council за работающий NVIDIA fallback.
- Не просить у пользователя подтверждение «бюджета» NVIDIA: admission — техническая capacity policy.
- Не считать mock/unit заменой real browser/provider/physical evidence.
- Не удалять чужие worktree/untracked/dirty artifacts «для порядка».

## 10. Definition of Done

Задача не завершена красивым mock, созданным issue или зелёным unit-тестом.

Нужен продуктовый результат:

**пользователь говорит с Мирой → Мира меняет server-authoritative доску → read-only UI показывает результат и focus → коллеги/guest видят разрешённую сцену → analysis получает только frozen evidence → результат долговечен и может быть опубликован карточкой → reconnect/revoke/idempotency сохраняют корректность → voice baseline не регрессировал.**

Acceptance report разделяет source, local tests, CI, deployed runtime, provider, browser и physical evidence. Непройденные gates называются прямо.
