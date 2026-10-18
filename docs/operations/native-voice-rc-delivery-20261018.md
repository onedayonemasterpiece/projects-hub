# Native voice: поставка RC от 18 октября 2026

**Выпущен подписанный Android RC 0.1.62-rc.72; совместимые backend/PWA развёрнуты и проверены.**

[Скачать APK](https://github.com/onedayonemasterpiece/projects-hub/releases/download/android-v72/projects-hub.apk)
· [Релиз android-v72](https://github.com/onedayonemasterpiece/projects-hub/releases/tag/android-v72).

Продолжена исходная работа над голосовым framework и Android-клиентом.
[PR #175](https://github.com/onedayonemasterpiece/projects-hub/pull/175) слит в
`1c718e4527abc86ff4559c25fca875ba6599761c`. Это release candidate с перечисленными
ниже открытыми ограничениями; успешный эмуляторный сценарий не означает полную
приёмку установленного приложения и реального провайдера.

## Результат и идентичность поставки

Проверенный source — `28a85be68aaec1215562866f36dddc22d1898214`.
Его дерево `1038978e8638d9a7d8394abb737feccd54e4cb40` совпадает с деревом merge
commit. Android использует опубликованный shared SDK
[`v0.4.0-rc.2`](https://github.com/onedayonemasterpiece/live-interaction/releases/tag/v0.4.0-rc.2),
release commit `b8b4ba15cdf1b4cfafc428eb3adec02cb44d92d2`, архив SHA-256
`8ff9df3888a0db10367878545b526c370118d262a50bb75c4b4ba63f63ee243a`.
Идентичность архива проверяется общим build helper; SDK остаётся prerelease.

| Этап | Подтверждённый результат |
| --- | --- |
| Contracts и PWA | **PASS**: [39132523164](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39132523164), 739 тестов за 88,47 с; PWA-проверки пройдены |
| Android audio | **PASS**: [39132523245](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39132523245), все 6 instrumentation tests и все 8 audio checks; [числовой receipt](evidence/native-voice-emulator-39132523245.json) |
| Backend/PWA deployment | **PASS**: `job_4342ffb74edad58c2038492a`, 53,604 с; завершён **14:24:36 UTC**. Установлен проверенный source `28a85be`; service active/running, restarts=0, health подтверждает revision |
| Публичный WSS с настоящим provider | **PASS**: `job_b1492211666bc579b0b56907`, 5,040 с. Совместимость `wl-live-v1`: binary PCM ACK/output, turn_complete, Stop/server close; HTTP fallback не использовался |
| Подписанный Android RC | **PASS**: [запуск №72, 39138486269](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39138486269); build **1 мин 23 с**, опубликован **15:02:06 UTC**. Source/evidence 6+8, signing, package/certificate и publication readback пройдены |
| Установленный native v2 клиент | **Открытая физическая приёмка RC**. Публичный WSS v1 smoke не заменяет Start/Stop и приёмку native v2 на телефоне |

Deployment сохранил четыре каталога версий, включая текущий, без удаления. В установленном
окружении проверены точные shared `b8b4ba15` и resource SDK `7c9b9ec1` pins,
а также metadata PyAV **18.1.0**. Обычные публичные GET index, JS и CSS дали
**HTTP 200**; размеры **747 / 658 239 / 21 514 байт** и SHA-256 каждого файла
совпали с установленными PWA assets. Повторный health в **15:02:50 UTC**:
**HTTP 200**, release `28a85be`, live/resource/static ready. Данные deployment,
runtime и WSS сохранены в [receipt](evidence/native-voice-deployed-28a85be-20261018.json).

Опубликованный APK: **2 333 682 байта**, SHA-256
`887956029daad60c50ad864f945080d0b2b1d2f589448abc5557ab178cc8b067`.
APK, `update.json` и `release-evidence.json` скачаны с публичных URL с **HTTP 200**;
размеры и SHA-256 совпали с публикацией. Build Tools 35 (`aapt`, `apksigner`)
и исправленный validator независимо подтвердили package
`com.kenigevents.projectshub`, versionCode **72**, versionName **0.1.62-rc.72**,
не-debuggable APK и тот же production certificate SHA-256
`936a216fa547c6eebc9d2e9428c20b94cc6057285342f78418ff683b5e9cb691`.
Readback tag подтвердил application source `28a85be`; release **416513611** —
`prerelease=true`, `draft=false`, stable latest остаётся **android-v63**.
Подробности — в [signed release receipt](evidence/native-voice-signed-android-v72-20261018.json)
и [опубликованном release evidence](https://github.com/onedayonemasterpiece/projects-hub/releases/download/android-v72/release-evidence.json).

Publisher `ed3cba28175ccc94c8c8c0e10c71d4620f29e16b` отличается от application
source. Его узкие правки устранили две ошибки доставки — job env и Accept для
Actions ZIP ([#186](https://github.com/onedayonemasterpiece/projects-hub/pull/186),
[#187](https://github.com/onedayonemasterpiece/projects-hub/pull/187)) — и дефект
парсера `aapt`, ошибочно принимавшего `compileSdkVersionCodename` за package name
([#188](https://github.com/onedayonemasterpiece/projects-hub/pull/188)).
Последняя правка: **53 focused checks PASS**,
[CI 39138304064](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39138304064)
— **750 tests PASS за 67,99 с**, PWA **PASS**. Проверенный код приложения не менялся.

Android artifact **12138921330**: **5 596 347 байт**, **84 entries**, SHA-256
`fc7c047990d2ea527310e3bced1b23dcb0b38e1e5ccd32297414d78285bc740e`.
Проверены receipt-файлы и instrumentation XML; `evidence_from_zip` — **PASS**.
Скриншоты live, outage и final просмотрены. Это тот же run, а не перенос
положительного результата с прежней сборки.

## Что доказал Android run

Проверен настоящий путь **AudioRecord → codec → бинарные сжатые данные → серверное
хранилище → compressed downlink → AudioTrack** с управляемым AI runner.
Пройдены захват двух известных речевых фрагментов, слышимый ответ, разрыв связи,
сохранение offline-речи без её смыслового исполнения, свежая речь после reconnect
и Stop. Корреляция входных фрагментов — **0,91943 / 0,98476**, speaker-ответа —
**0,9945**. Пороговые условия не снижались.

Использован штатный API31 Google APIs guest: `guest-background.collected=true`,
`package_mutations=false`; GMS/Search не отключались. Запись screen video была
выключена, сохранены PNG и аудиодоказательства. Это описание условий прогона,
без вывода о величине экономии CPU или единственной причине прежнего сбоя.

После Stop: `idle`, `capturing=false`, `playing=false`, `code=null`; сохранены
три input sources. Остаток не нулевой: UI показывает **60 мс**, receipt
`pending_after_stop_us` — **112 187 мкс**. PASS подтверждает проверенные границы
Stop/recovery, но **не полное server commit/source-seal всех хвостов**.

## Воспроизведение штатным способом

Сборка и версии инструментов — в [Android README: Candidate build](../../android/README.md#candidate-build).
Команда Gradle запускает `testDebugUnitTest assembleDebug`; это локальная сборка,
а не microphone acceptance. Полный сценарий использует существующий
[Actions Android workflow](../../.github/workflows/android.yml) и тот же harness,
описанный в [Repeatable local and Actions voice checks](../../android/README.md#repeatable-local-and-actions-voice-checks).
Для нового запуска нужны свежие evidence/state directories и тот же SDK/image
pin; старый артефакт не заменяет проверку нового дерева.

Подписанная поставка выполняется только существующим
[Controlled signed release candidate](../../android/README.md#controlled-signed-release-candidate):
явные `source_sha` и `android_run_id`, прежние package/certificate, возрастающая
версия и immutable prerelease вне stable update feed. Release gate принимает
данный формат без обязательного MP4 или disabled-package predicates.

Этот RC устанавливается по прямой APK-ссылке: stable updater его автоматически
не предлагает. Production package и ключ сохранены для обновления установленного
v63. Отдельный `.debug` package остаётся отдельным приложением: RC не обновляет
его на месте и не переносит его локальные данные. Debug-сборку можно сохранить
для диагностики; установка RC не требует её удаления.

## Известные ограничения RC

- **Основная русская транскрипция A1/A2 — FAILED.** В обеих проверках первая
  canonical final-фраза сократилась до «Слышишь меня?», во второй «соединения»
  заменено на «соединений». [A2 evidence](evidence/native-voice-primary-send-a2-20261018.json)
  подтверждает полные успешные клиентские WebSocket writes исходного PCM;
  это не recognition ACK. Ответы аудиодиалога получены, но строгая проверка
  обеих целых фраз остаётся проваленной. Это отдельный дефект от optional captions.
- **Промежуточный голосовой ответ:** [pending-a3 — PASS](evidence/native-voice-pending-a3-20261018.json).
  Настоящий provider выдал звук до завершения обычного read tool с тестовой
  задержкой 9 с, затем итоговый ответ. Проверка использовала синтетическую
  английскую речь; слышимость этого progress на физическом Android отдельно
  не подтверждена.
- **Captions:** `clock-a3` — **FAILED**, production clock остаётся `unverified`.
  Optional captions явно деградированы; наблюдение накопительных offsets
  не доказывает полную привязку речи и finals без offsets.
- **Открытая приёмка:** 90-минутный разговор с renewal/recovery; физический
  Android, background/screen-off, permissions, audio routes и signed update;
  нестабильный мобильный интернет и Wi-Fi/mobile handover. Управляемый outage
  эмулятора не закрывает эти условия. **Четыре одновременных голоса вместе
  с реальной региональной базой знаний/RAG** на одном сервере не квалифицированы.

Границы и дальнейшая связанная работа сохранены в
[общем delivery record](voice-delivery-integration-20261018.md).
Поставка RC и её публичный readback завершены. Подтверждения владельца об
установке и работе native v2 на физическом телефоне ещё нет; эта приёмка
сохранена как открытое ограничение разрешённого RC.
