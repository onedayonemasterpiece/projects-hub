# Native voice: согласованный аудиопрофиль WSS и мобильная сеть

Дата: 17 октября 2026.
Статус: **владелец согласовал; requirements_update и commit guard пройдены**.

## Решение владельца

17 октября владелец подтвердил изменение («так что изменение согласовано»)
и дополнительно потребовал обязательного сжатия между телефоном и сервером
и корректной работы на нестабильном мобильном интернете. Сжатие нового
native/mobile v2 обязательно в обоих направлениях. Старый PCM-профиль
остаётся явной совместимостью и не засчитывается как compressed-mobile acceptance.

Сжатое аудио уже является целью [постановки native voice / Android-first](https://github.com/onedayonemasterpiece/projects-hub/blob/docs/native-voice-pwa-delivery-20261016/docs/prompts/projects-hub-native-voice-pwa-delivery-20261016.md).
Конфликт находится в более раннем [защищённом реестре](https://github.com/onedayonemasterpiece/projects-hub/blob/87824b7aa13d0add284e7b1174635dd0c2ffca69/.devcoveer/requirements.json):
он фиксирует PCM как формат передачи. Постановка прямо требует провести
requirements-update path, если такой конфликт обнаружится.

## Применённая редакция

Изменены только два requirement ID. Все statements и остальные прежние
acceptance criteria сохранены. Длинные формулировки разбиты по установленному
реестром ограничению 500 символов на критерий.

### CORE-LIVE-WSS.acceptance[2]

**Было:**

> PCM uses bounded binary frames with relay ACK/backpressure; model events and audio are pushed over the socket.

**Стало:**

> Audio uses a negotiated, versioned codec profile in bounded binary WSS frames with relay ACK/backpressure and separate encoded-byte, decoded-duration and decode-work limits. Native/mobile v2 requires audio-codec compression in both directions; model events and compressed output audio are pushed over WSS. Explicit wl-live-v1 PCM16 compatibility does not satisfy compressed-mobile acceptance. Codec changes and transport fallback are never silent.

### CORE-VOICE-SESSION-CONTINUITY: три добавленных критерия

> Supported mobile clients are validated on constrained bandwidth, variable latency/jitter, packet loss, transient outages and Wi-Fi/mobile handover; browser background capture limits are stated honestly.

> While recording is active, native capture continues into a bounded on-device durable spool during transport outages. Storage or lifecycle limits are surfaced explicitly without silently discarding committed audio.

> Explicit local Stop stops capture immediately, independently of transport and backlog. Recovery prioritizes fresh speech over deferred processing and resumes source delivery from committed receipts without duplicate semantic effects. Live, delayed and recording-only states remain distinct; an outage never implies zero-delay interaction.

Это изменение не разрешает прямое подключение клиента к провайдеру,
не изменяет same-origin/ticket/ACL границы и не подменяет relay ACK
подтверждением сохранности, понимания или выполнения команды.

`CORE-LIVE-WSS.acceptance[3]` о запрете mutation после разрыва до следующего
чистого хода остаётся в силе. Полнота восстановленного source не возвращает
старому повреждённому ходу права выполнить поручение.

## Формальное применение и readback

Штатный `requirements_update` выполнен: `op_ecf3179c0821aa8269ac6832`.
Полный readback 23 требований совпал с согласованным содержанием.
`git_commit_push` выполнил commit guard и вернул `requirementsChange`
с явной ссылкой на решение владельца: `op_833602a9d3820614e1d01dbb`.

- Предыдущий manifest SHA256: `02e37cf2cc94d6c44a1a6a2414c0fe6a3d43d2a527bd5a32dec73af80c476ea5`.
- Новый manifest SHA256: `1483d58ad9e746354cee50c9c0aedd9dfea4cccde6665c9f570da330dfffea0e`.
- [Защищённый коммит](https://github.com/onedayonemasterpiece/projects-hub/commit/493215046cd75d0f267d612f2277e81846204d1d): единственный изменённый путь — `.devcoveer/requirements.json`.
- Коммит непосредственно продолжает `ffa3b508` и добавлен fast-forward в исходную ветку `claude/voice-continuity-consumer-20261016` / PR #175.

Для штатного guard использован временный tool-addressable worktree ровно на
checkpoint существующей Claude-ветки. Авторизация проверялась при создании
коммита в том же физическом checkout; затем перенесён сам проверенный коммит,
а не неавторизованный diff. Исходные worktree не переименованы и не пересозданы.

Продолжать существующие ветки shared v2 + native Android:
сжатый original и timeline сохраняются отдельно от производного PCM;
PCM создаётся на необходимой provider boundary; текущая v1 PWA остаётся совместимой.

Конкретный codec, bitrate, размер порции и очереди выбираются по измерениям
в рамках постановки. Они не требуют от владельца выбора внутренних API.

## Связь с продолжением реализации

Исправления уже начатого continuity R1, сохранения checkpoint, Stop,
damaged-turn fences и приватности внутренних provider-событий можно завершать
на текущем wl-live-v1. Эти исправления не заменяют native Android,
store-and-forward, новый аудиопрофиль или физическую 90-минутную приёмку.

Приоритет свежего Stop и новой речи перед дальнейшим разбором backlog
уже принят в предыдущем обсуждении. Повторно согласовывать его не требуется.

[Исследование эффективности аудио и Go/Rust](../research/voice-audio-efficiency-20261017/README.md)
не разрешает смену основного WSS-транспорта или большой перенос языка.
WebRTC/QUIC — исследовательские альтернативы; такой переход потребовал бы
отдельного архитектурного решения и соответствующего requirements update.

