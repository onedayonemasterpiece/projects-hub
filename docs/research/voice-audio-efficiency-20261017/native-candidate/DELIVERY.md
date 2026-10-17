# Native voice candidate — итог передачи 17 октября 2026

Продолжены исходные Claude branches и PR #51 / #175. Интегрированный кандидат собран; обычный выпуск и физическая приёмка ещё не выполнены.

## Проверенная пара и APK

- Shared: `0.4.0-rc.1`, immutable source `c564f0c44c6fe783fd4932bd4633fada34e05c73`.
- Consumer runtime: `20020927de8ba733922d763146d71fa75872727e`; все 82 изменённых/новых файла сверены с опубликованным archive, согласованные requirements полностью сохранены.
- GitHub PR merge checkout сборки: `03c3d37ef65dbedc2a8f56aa17b781cb6b5c1988`. Это обычная проверка PR вместе с актуальным main, а не утверждение, что APK побайтно равен локальной сборке branch head.
- [Backend/PWA contracts CI](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39027475953): **SUCCESS**.
- [Android CI](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39027475944): **SUCCESS** build и emulator-smoke. 3 instrumentation tests подтверждают Activity launch/recreation и theme bridge; они не проверяют 90 минут реального аудио.
- [Скачать CI debug APK artifact](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39027475944/artifacts/12099042605).

APK из этого artifact: 3 157 902 bytes, SHA-256 `8cc466f881f44c68eec7276de94cacd158b2d557b8cd61330e70691042d04c76`. Package `com.kenigevents.projectshub.debug`, versionCode 1, versionName `0.1.1-debug`, minSdk 26 / targetSdk 35. В manifest и DEX проверено наличие NativeVoiceService, NativeSessionApi, NativeLiveRuntime, NativeEncoder и `wl-live-v2`. Это отдельный debug package, не обновление существующей production установки. APK checksum относится к распакованному APK; digest ZIP отдельно записан в [receipt](consumer-ci-apk-receipt.json).

Локальный APK с checksum `1cff757a…` в [исходном verification](verification.json) — другая сборка той же candidate source-пары. Его checksum не приписывается CI artifact.

## Последняя правка подготовки выпуска

После зелёного CI обнаружен конкретный пропуск: `android-release.yml` не получал `liveInteractionAndroidDir`, хотя Gradle native binding требует его. В этом delivery commit добавлены ровно такой же immutable SDK checkout и job environment, как в уже проверенном Android CI. Android runtime, SDK, Gradle build inputs и APK после успешной сборки не менялись; README и release wiring имеют отдельный [проверочный receipt](native-release-source-preparation.json).

YAML и согласованность SHA/environment проверены. После удаления двух новых блоков workflow побайтно совпадает с прежним: signing secrets, версии, build command, apksigner, update manifest, permissions, triggers и publish steps сохранены. Подписанный workflow теперь имеет compile dependency. Это подготовка существующего пути выпуска; workflow **не запускался**.

Его Publish GitHub Release создаёт обычный release, а updater читает releases/latest. Metadata `candidate` не превращает этот workflow в автоматический prerelease publisher. Поэтому запуск для обычного выпуска остаётся отдельным решением после stable SDK promotion и приёмки; смешивать debug candidate с production update нельзя.

## Что уже проверено и что ещё нет

471 backend tests с действительно установленным GitHub shared dependency; 97 web tests и TypeScript/Vite build; shared 127 Python, 98 JavaScript, 9 Java core и 20 Android platform; consumer 31 Android unit tests. Существующий Chromium source smoke прошёл desktop 1280×900 / mobile viewport 390×844; он использует fixtures. Полные команды, scopes, hashes и логи — в [verification.json](verification.json), [CI readback](consumer-ci-readback.json) и [source readback](consumer-remote-verification.json).

Единственный реальный запуск вчерашнего provider runner завершён: realtime/burst получили audio и completed turn; resume skipped, idle TimeoutError. Lease release подтверждён. [Безопасный результат](../provider-spike/spike-all-20261017.summary.json) не позволяет объявить весь provider acceptance успешным и не должен запускаться повторно как незавершённая работа.

До обычного выпуска остаются: реальная слышимость промежуточного ответа Миры при долгом tool call, provider renewal/resume и 90-минутный логический разговор; физический Android screen-off/background/routes/permissions/Stop и source recovery на нестабильной сети; русский звук/ASR и handset CPU/RAM/battery; подписанная цепочка self-update; совместимый migration/rollback с сохранением originals. Текущая PWA сохранена, её legacy PCM v1 не квалифицирует новый compressed profile. Полная installed iOS PWA — следующий milestone.

Новый backend/PWA на production не устанавливался. Этот debug APK требует согласованного backend-кандидата для проверки нового голоса; установленный старый backend не получает v2 от одной установки APK. Исходная постановка допускает передачу release candidate при недоступной физической приёмке, но запрещает объявлять её выполненным релизом.

## Go/Rust и нагрузка

[Основное исследование](../README.md) сохраняет baseline source/probes и ограничения измерений; [полный backend scope](../backend-go-scope.md) учитывает одновременные Live, captions и будущий Regional Knowledge на одном сервере. Нативный codec уже используется. Эквивалентный Go runtime и whole-host сравнительная нагрузка ещё не измерены; перенос языка не объявлен доказанной экономией. Единственный неблокирующий вопрос о capacity — ожидаемый пик одновременно активных голосовых разговоров и рост.
