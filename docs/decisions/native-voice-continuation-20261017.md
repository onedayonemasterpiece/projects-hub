# Native voice: продолжение работы Claude от 16 октября

Дата: 17 октября 2026.
Статус: **R1 source checkpoint; зависимый draft, не выпуск и не завершение native voice**.

## Какая работа продолжена

Сверены переданный владельцем журнал Claude Code, GitHub и обе физические
worktree DevCoveer. Работа продолжена в исходных ветках без пересоздания проекта:

| Репозиторий | Ветка | Вчерашний checkpoint | Проверенный код продолжения |
|---|---|---|---|
| live-interaction | `claude/voice-continuity-v0.4-20261016` | `599dc1caad8e0995ab65dc6b3fe2f684073af8f6` | `3b85224962c396e1a52977d6b81eee659db0cacc` |
| projects-hub | `claude/voice-continuity-consumer-20261016` | `87824b7aa13d0add284e7b1174635dd0c2ffca69` | `4a4d658cc5209429c7ed584ed0c1a8b51700743d` |

[Shared PR #51](https://github.com/onedayonemasterpiece/live-interaction/pull/51)
остаётся тем же PR. Оба новых source-коммита непосредственно продолжают указанные
вчерашние HEAD. Последующие изменения этого файла не меняют проверенный код.

Физические worktree:

- `/home/dev/worktrees/live-interaction-continuity-20261016`;
- `/home/dev/worktrees/projects-hub-voice-continuity-20261016`.

При проверке они были чистыми и совпадали с исходными SHA по всем 55 и 267
tracked files соответственно. Новые коммиты записаны в GitHub; fast-forward этих
физических worktree ещё не подтверждён. Перед следующим запуском сверить их
HEAD и локальные изменения, затем обновить именно эти worktree. Основной
`/home/dev/projects/projects-hub` на посторонней docs-ветке не является целью.

## Исправленные ошибки

### Потерянная речь и право выполнить команду

Поздний `input_dropped` мог приходить после постановки `activity_end` в очередь,
когда флаг открытой реплики уже сброшен. Это оставляло возможность выполнить
команду по неполной речи. Теперь внутренний `audio_turn_generation` связывает
потерю, границу реплики и provider receipt. Подтверждённая потеря повреждает
свой ход независимо от причины — reconnect, capability transition или budget.
Снять блокировку может только более поздняя чистая граница, достигшая provider.

Ошибка отправки последнего кадра теперь сама сообщает о потере и закрывает
старый сокет; появление следующего кадра для обнаружения ошибки не требуется.
Кадры, оставшиеся в очереди к моменту готовности нового provider-сокета,
отбрасываются с точной причиной и идентификатором хода.

### Stop, captions и приватность

Caption reader продолжает принимать Stop во время переподключения.
Ожидающее подключение отменяется; речь из разрыва не переигрывается. Принятый
до самой первой готовности стартовый участок сохраняется в упорядоченной
очереди с пределом 768 KiB и передаётся один раз.

В Python provider появился внутренний `resumption_checkpoint`. Legacy Node
host теперь отфильтровывает его до event ring, polling и WSS: сырой handle не
становится клиентским событием.

### Жизненный цикл checkpoint в Projects Hub

Автоматический reclaim отсоединённой сессии раньше проходил как явный Stop
и удалял checkpoint перед восстановлением. Shared host теперь принимает
доверенную внутреннюю причину `detached_reclaim`; обычный Stop остаётся
`explicit_stop` и имеет приоритет при конкуренции этих действий.

После resume новая сессия помнит ID исходного checkpoint. Явный Stop
инвалидирует текущий и унаследованный checkpoint, а поздние callback закрытой
сессии не возвращают его в usable и не заменяют checkpoint преемника.
Причина reclaim не принимается из HTTP/WSS запроса пользователя.

Первоначальная гипотеза о потере checkpoint при обычном graceful shutdown
**не подтвердилась**: существующий `stop_all()` его сохранял. Это поведение
сохранено и проверено. Текущий product saver синхронный; исправление не
объявляет гарантий отмены произвольного уже выполняющегося async saver
в других потребителях shared API.

## Проверка

| Проверка | Результат | Область доказательства |
|---|---:|---|
| Исходный полный consumer pytest | 430 passed | Существующий baseline |
| Итоговый shared Python unittest | 95 passed | Provider/host/caption contract regressions |
| Итоговый Node/browser suite | 98 passed | Shared JavaScript contracts |
| Итоговый consumer pytest с candidate framework | 439 passed, 1 warning | Admission, adapter, SQLite и остальные product fixture tests |
| Обычный GitHub CI shared PR | success | `contracts` и `native` jobs |

[GitHub run на shared SHA 3b852249](https://github.com/onedayonemasterpiece/live-interaction/actions/runs/39012102826)
выполнил обычные npm/Python проверки и `gradle --no-daemon -p android test`.
Native job проверяет библиотечные контракты; физическое Android-устройство
в этом прогоне не участвовало.

Локальная consumer-проверка использовала Python 3.12.14 и явный `PYTHONPATH`
на приведённую выше пару candidate исходников. Она не доказывает установку
кандидата из текущих dependency manifests. Предупреждение — внешнее
`Pydantic TypedDictExtraConfigWarning` в MCP `RequestParamsMeta`.
После фиксации GitHub blob SHA всех изменённых source/test файлов сверены
с локально проверенными байтами.

## Почему выпуск ещё не готов

`pyproject.toml`, `web/package.json`, `web/package-lock.json` и release-pin tests
сохраняют released `live-interaction 0.3.28` / `3e675077…`. Новый consumer код
зависит от API shared candidate. Его draft **нельзя сливать или выпускать
с текущим stable dependency graph**.

CI не получает временного overlay и не пропускает новые регрессии. Возможное
падение consumer CI на старом framework — реальная незавершённая зависимость;
зелёный локальный прогон кандидатов не заменяет этот gate. После настоящего
версионированного shared выпуска обновить обе зависимости, lockfile,
release metadata и pin tests одним согласованным изменением, затем пройти
обычный consumer CI и продуктовую приёмку без подмены импорта.

Наблюдавшийся production baseline: `0.1.62`, release
`76bc5f2d84424f26bf3d0056687ae6a5dfcbde81`, health 200. Merge, deploy,
переключение production dependency и публикация APK здесь не выполнялись.

## Незавершённый реальный provider spike

Вчерашний журнал указывает существующий runner и материалы:
`/home/dev/artifacts/projects-hub/20261016T134802Z-voice-continuity-impl-20261016/`.
В нём находятся `provider_spike.py`, `run_spike.sh`, синтетический `speech.pcm`
и прежние `spike-idle` / `spike-pacing` результаты. Продолжать этот runner.

Вчера подтверждены только подключение и получение checkpoint. Pacing
сорвался из-за отсутствующих `queued_at` у activity boundaries в runner;
продуктовый host эти метки ставил. Счётчик лимита 150000 units также требует
проверки: старый подсчёт `resource_budget.granted_units` не доказывает общий
расход. Shared resource guard не заменяет проверку этого отдельного cap.

Сегодня OpenCode task `dvt_43845b37065747bfabb255e917cdfde0` трижды завершился
до выполнения команд с `503 Upstream error from Nvidia: Service temporarily
overloaded`, включая повтор после паузы. Модель/provider не переключались.
Последний evidence SHA256:
`13baaaa1216e77cf5c260dc6befde6a3f2519c6bd583c6e929672783d0a0ec2d`.
Это недоступность provider, а не отказ автоматической проверки безопасности.
Наличие текущих файлов runner на devstand пока не перечитано.

Следующий реальный прогон: проверить точный импорт candidate framework,
исправить метки в существующем runner и подтвердить bounded accounting,
затем через общий resource control и закреплённый ключ проверить pacing,
renewal/GoAway, resume/history restore и Stop. Прежнее разрешение владельца
на Live-тесты через контроль лимитов остаётся в силе; повторное согласование
самих этих расходов не требуется.

## Решение владельца и следующий этап

Один точный вопрос вынесен в
[предложение аудиопрофиля](native-voice-audio-profile-proposal-20261017.md):
разрешить согласуемый версионированный codec profile в
`CORE-LIVE-WSS.acceptance[2]`, сохранив v1 PCM и существующие защитные границы.
Защищённый `.devcoveer/requirements.json` не изменён. После явного решения
применить только согласованный diff через `requirements_update`.

В shared draft уточнены уже принятые решения: Stop завершает capture/Live,
сохраняя timeline и источники; полнота восстановленного source не возвращает
старой повреждённой команде право на mutation; свежая речь и Stop имеют
приоритет перед дальнейшим разбором backlog. Это не новые вопросы владельцу.

Дальше остаются shared v2 codec/durable protocol, native Android service,
владеющий capture/VAD/WSS/playback/recovery независимо от WebView, и физические
проверки. Нынешняя native библиотека — транспорт wl-live-v1; она не является
готовым автономным микрофонным service. Android-first порядок сохраняется;
полная installed iOS PWA квалификация остаётся следующим этапом. Ни 90 минут
на телефоне, ни background/process-death recovery, ни подписанный native
candidate APK этим checkpoint не закрыты.
