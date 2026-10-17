# Серверная пара для установленного native APK · 17 октября 2026

## Что исправляет эта поставка

Владелец установил debug APK из native candidate и сообщил: нажатие голосовой
кнопки сразу показывает «не удалось начать». На момент проверки backend/PWA
работает на `adec24c3e2923f44a92eebc68542fab572afe5ab` и ещё использует браузерный
захват. Установленный APK уже передал владение микрофоном native service и
запрещает WebView захватывать аудио. Поэтому старая PWA получает отказ ещё до
HTTP Start. Одной установки этого APK недостаточно: ему нужна согласованная PWA
и серверный `wl-live-v2`.

Эта серверная поставка продолжает исходную работу Claude из PR #175, берёт её
backend/PWA и зависимости из
`e7ff19b8ae11458ac105d43ac79ea5b8814006d8` и сохраняет изменения текущего
production `adec24c3e2923f44a92eebc68542fab572afe5ab`. Они расходятся от
`76bc5f2d84424f26bf3d0056687ae6a5dfcbde81`: native candidate имеет восемь своих
commit, production — семь. В полном rehearsal пять общих изменённых файлов
сливаются без текстовых конфликтов. В серверной поставке пересекаются только
`src/projects_hub/live_adapter.py` и `web/src/App.tsx`; изменения анализа заметок,
проверки доступа, ссылок на результаты и показ результата в PWA сохраняются.

Android source, оба Android workflow и версия приложения остаются из текущего
main. Native Android development продолжается в прежнем PR #175. Это позволяет
проверить уже установленный APK с согласованным сервером через существующий
deploy, не запуская новый обычный Android release. Полный merge native PR меняет
`android/**` и автоматически запускает `android-release.yml`, поэтому он не
является эквивалентом этой серверной поставки.

## Точная пара

- Shared Python и browser: `0.4.0-rc.1`, commit
  `c564f0c44c6fe783fd4932bd4633fada34e05c73`; Python устанавливает extra `audio`.
- Установленный APK: SHA-256
  `8cc466f881f44c68eec7276de94cacd158b2d557b8cd61330e70691042d04c76`,
  `com.kenigevents.projectshub.debug`, version `0.1.1-debug`.
- Его consumer source head:
  `20020927de8ba733922d763146d71fa75872727e`; стандартный PR merge checkout CI:
  `03c3d37ef65dbedc2a8f56aa17b781cb6b5c1988`.
- [Исходный APK artifact](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39027475944/artifacts/12099042605).
- [Ранее проверенный receipt APK](https://github.com/onedayonemasterpiece/projects-hub/blob/e7ff19b8ae11458ac105d43ac79ea5b8814006d8/docs/research/voice-audio-efficiency-20261017/native-candidate/consumer-ci-apk-receipt.json).

`tests/fixtures/native-v2-installed-candidate.json` фиксирует эту пару. Копия
исходного Android Start fixture находится в `tests/fixtures/native-live-start.json`;
её SHA-256 и Git blob ID сверены с точным `2002092…`. Исходный
`NativeSessionRequestTest` проверяет Java producer в Android CI. Серверный тест
проверяет сохранённый запрос по фактической модели `LiveStart` этой поставки.
Он не выдаёт оставшийся main-branch Android source за установленный APK.

Согласованные requirements сохранены: изменены только acceptance у
`CORE-LIVE-WSS` и `CORE-VOICE-SESSION-CONTINUITY`, остальные 21 ID не меняются.
Плановый начальный пик, выбранный владельцем 17 октября: **четыре одновременно
говорящих пользователя**. Это цель последующей совместной нагрузки
Live + captions + Regional Knowledge, ещё не результат нагрузочного теста и не
причина молча менять текущие admission limits.

## Доставка через существующий installer

`deploy/devcoveer_install.py` берётся из актуального main без изменения. Он
сохраняет уже активированный `PROJECTS_HUB_VOICE_ANALYSIS_CONFIG_FILE`, использует
единственный существующий сервис и требует принадлежность точного SHA свежему
`origin/main`. Обход ancestry guard, отдельный gateway, второй production backend
или новый release channel здесь не вводятся.

Перед активацией проверяются точный интегрированный SHA, текущий deployed SHA и
отсутствие нового параллельного изменения main/production. Если основа успела
измениться, нужно интегрировать её до активации, а не устанавливать старый
ancestor поверх новой работы. В отдельной сборке проверяются фактически
установленный shared commit, backend tests, PWA build и native HTTP contract.
После разрешённой активации существующий installer проверяет `/healthz` по
точному deployment SHA; затем проверяется загрузка согласованной PWA и реальный
Start установленного APK. Успешный health сам по себе не является голосовой
приёмкой. Исходный одиночный provider spike уже завершён; эта поставка не
повторяет его как незавершённую задачу.

## Миграция и граница отката

Новая схема добавляет `live_checkpoints`, таблицы `encoded_voice_*` и поля формата,
направления, длительности и проекции в `sources`. Старые источники получают
`pcm16` / `uplink`; их аудиофайлы не перекодируются. В изолированной проверке
синтетическая база прошла путь exact `adec → candidate → adec → candidate`:
старые поля, события и PCM сохранились, сжатые BLOB сохранились при открытии
старым кодом, `PRAGMA integrity_check` вернул `ok`. Это проверка совместимости
хранения, а не полная приёмка downgrade после реального разговора.

Есть конкретное ограничение старого `adec`: он считает незавершённый
`encoded-v2` источник пригодным для PCM replay и включает downlink в список
ожидающих пользовательских источников. Поэтому простой возврат symlink после
первых v2-записей не является корректным rollback голосового продукта.

Перед активацией нужна согласованная SQLite backup вместе с сохранёнными
оригиналами и точными ссылками на текущий release/environment. Нельзя копировать
живой файл SQLite без учёта WAL. Автоматическое восстановление installer после
ошибки health до пользовательских v2-записей сохраняет прежнюю границу. После
первых v2-записей исправление должно сохранять новую базу и все originals;
восстановление старого snapshot поверх неё потеряло бы новые данные.

Для текущего pilot допустима явно ограниченная стратегия совместимого
исправления вперёд. Если требуется рабочий legacy rollback target, его следует
заранее получить отдельным обычным commit: перенести проверки `audio_format`,
запрет reset/append/PCM-read для encoded originals и исключение downlink/encoded
из старого PCM recovery. Такой target сохраняет данные, но не предоставляет
native v2. Ни этот backport, ни новое управление выпуском в данную поставку не
входят.

## Что пока не подтверждено

Исправление pairing и зелёные deterministic tests не закрывают физические
screen-off/background/routes/permissions/Stop, 90 минут разговора, provider
renewal/resume, слышимость промежуточного ответа Миры, качество русского ASR,
неустойчивую мобильную сеть и нагрузку четырёх пользователей вместе с RAG.
Обычный подписанный native release остаётся отдельным этапом исходной работы.
