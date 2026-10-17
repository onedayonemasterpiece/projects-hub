# Native voice: согласование аудиопрофиля WSS

Дата: 17 октября 2026.
Статус: **предложение владельцу; защищённый реестр не изменён**.

## Вопрос

Согласовать ли замену одного критерия `CORE-LIVE-WSS.acceptance[2]`,
чтобы явно разрешить версионированный согласуемый сжатый аудиопрофиль,
сохранив поддержку текущего PCM-профиля и все существующие ограничения?

Сжатое аудио уже является целью [постановки native voice / Android-first](https://github.com/onedayonemasterpiece/projects-hub/blob/docs/native-voice-pwa-delivery-20261016/docs/prompts/projects-hub-native-voice-pwa-delivery-20261016.md).
Конфликт находится в более раннем [защищённом реестре](https://github.com/onedayonemasterpiece/projects-hub/blob/87824b7aa13d0add284e7b1174635dd0c2ffca69/.devcoveer/requirements.json):
он фиксирует PCM как формат передачи. Постановка прямо требует провести
requirements-update path, если такой конфликт обнаружится.

## Точная предлагаемая замена

**Было:**

> PCM uses bounded binary frames with relay ACK/backpressure; model events and audio are pushed over the socket.

**Предлагается:**

> Audio uses an explicitly negotiated, versioned codec profile in bounded binary WSS frames with relay ACK/backpressure and separate encoded-byte, decoded-duration and decode-work limits; model events and output audio are pushed over the socket. The wl-live-v1 PCM16 profile remains supported for compatibility. Codec changes and transport fallback are never silent.

Это изменение не разрешает прямое подключение клиента к провайдеру,
не изменяет same-origin/ticket/ACL границы и не подменяет relay ACK
подтверждением сохранности, понимания или выполнения команды.

`CORE-LIVE-WSS.acceptance[3]` о запрете mutation после разрыва до следующего
чистого хода остаётся в силе. Полнота восстановленного source не возвращает
старому повреждённому ходу права выполнить поручение.

## Что происходит после согласования

Применить только согласованный минимальный diff через штатный
`requirements_update`, сохранив ссылку на явное решение владельца.
Затем продолжить существующие ветки shared v2 + native Android:
сжатый original и timeline сохраняются отдельно от производного PCM;
PCM создаётся на необходимой provider boundary; текущая v1 PWA остаётся совместимой.

Конкретный codec, bitrate, размер порции и очереди выбираются по измерениям
в рамках постановки. Они не требуют от владельца выбора внутренних API.

## Что не ждёт этого решения

Исправления уже начатого continuity R1, сохранения checkpoint, Stop,
damaged-turn fences и приватности внутренних provider-событий можно завершать
на текущем wl-live-v1. Эти исправления не заменяют native Android,
store-and-forward, новый аудиопрофиль или физическую 90-минутную приёмку.

Приоритет свежего Stop и новой речи перед дальнейшим разбором backlog
уже принят в предыдущем обсуждении. Повторно согласовывать его не требуется.

