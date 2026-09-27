# Проверенные внешние первоисточники

Дата сверки: **27 сентября 2026**. Ссылки ниже подтверждают ограниченные технические факты. Проектные решения и цели не выдаются за цитаты источников. Перед rollout изменяемые условия и документация проверяются заново.

## Платформы

| Источник | Проверенный факт и значение |
| --- | --- |
| [MDN: Screen Wake Lock](https://developer.mozilla.org/en-US/docs/Web/API/Screen_Wake_Lock_API) | Lock относится к активному документу и может быть освобождён системой. Не доказательство фоновой записи в PWA |
| [WebKit: Web Push for Web Apps](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/) | iOS/iPadOS web push имеет условия Home Screen web app и пользовательского разрешения. Не обещать push любому открытому табу |
| [Android: foreground service types](https://developer.android.com/develop/background-work/services/fgs/service-types) | Microphone foreground service требует соответствующих разрешений и соблюдения ограничений запуска |
| [Android: Calendar Provider](https://developer.android.com/identity/providers/calendar-provider) | Intent позволяет передать пользователю добавление/редактирование события; запуск UI не доказывает сохранение |

## Live и обработка данных

| Источник | Проверенный факт и значение |
| --- | --- |
| [Gemini: session management](https://ai.google.dev/gemini-api/docs/live-api/session-management) | Время соединения и сессии ограничено; предусмотрены compression, GoAway и resumption. Пользовательские действия должны жить вне сокета |
| [Gemini: rate limits](https://ai.google.dev/gemini-api/docs/rate-limits) | Лимиты зависят от model/tier/project; несколько ключей не означают независимые квоты |
| [Gemini: available regions](https://ai.google.dev/gemini-api/docs/available-regions) | Россия отсутствует в просмотренном списке доступных регионов. Не предполагать разрешение по факту наличия зарубежного сервера |
| [Gemini API terms](https://ai.google.dev/gemini-api/terms) | Условия различают paid/unpaid data handling, доступную аудиторию и возраст; нужен отдельный provider/data preflight |

Операционный вывод, сделанный для этого продукта: ни общий пул ключей, ни их зашифрованная выдача сами по себе не дают права передавать любой класc данных. Это проектный вывод из условий, а не утверждение о текущем billing/status конкретных ключей владельца.

## Изоляция и credentials

| Источник | Проверенный факт и значение |
| --- | --- |
| [PostgreSQL: Row Security](https://www.postgresql.org/docs/current/ddl-rowsecurity.html) | RLS задаёт политики строк; привилегированные роли и владельцы таблиц требуют отдельного учёта |
| [OWASP: Multi-Tenant Security](https://cheatsheetseries.owasp.org/cheatsheets/Multi_Tenant_Security_Cheat_Sheet.html) | Граница tenant касается не только основных SQL-запросов, но и производных данных/сервисов |
| [GitHub: GitHub Apps](https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/about-creating-github-apps) | GitHub App позволяет ограничить permissions и доступные repositories; подходящая основа server-side интеграции |

Этот пакет не заменяет юридическую проверку требований конкретной аудитории и юрисдикции, не утверждает соответствия каким-либо стандартам и не является результатом security audit развёрнутого приложения.

## Короткий конкурентный и нейминговый скрининг

[Asana AI](https://asana.com/product/ai) и [Motion](https://www.usemotion.com/) показывают, что AI-функции в управлении работой уже существуют. Здесь не проводился полный сравнительный тест, не оценивались цены или доли рынка и не заявляется уникальность голосового интерфейса.

[Coacta](https://www.coacta.com/safety) относится к близкой сфере координации клубов/команд. [Confera в App Store](https://apps.apple.com/us/app/confera/id6801247666) представляет продукт для встреч; также существует [Confera](https://www.confera.com.my/). Это достаточное основание не рекомендовать эти варианты без дополнительной проверки, но не правовое заключение о конфликте знаков.

Для Содей/Sodey, Совершим, Ладено и псевдолатинских вариантов предварительного веб-поиска недостаточно: отдельно нужны реестры знаков, домены, магазины, фонетический/поисковый тест. Отсутствие очевидного результата поиска не доказывает свободное имя.

## Ревизия 2: речь, порядок событий и словари

Проверено 27 сентября 2026 по первичной документации; это не замена теста конкретного установленного SDK и устройства.

| Источник | Ограниченный факт и проектное следствие |
| --- | --- |
| [Gemini Live API reference](https://ai.google.dev/api/live) | Input transcription приходит независимо от других сообщений; промежуточная версия меняется. Ответ/turnComplete не служит квитанцией полноты архивной расшифровки |
| [Gemini Live API reference](https://ai.google.dev/api/live) | clientContent поддерживает накопление контекста с отдельным завершением хода; смешивание с realtimeInput не имеет гарантированного порядка. Длинный старый буфер нельзя произвольно чередовать с новой речью |
| [Gemini Live API reference](https://ai.google.dev/api/live) | Setup не меняется произвольно на открытом соединении; предусмотрено изменение конфигурации через resumption. AudioTranscriptionConfig описывает customVocabulary, но совместимость конкретной разговорной модели/SDK ещё проверяется |
| [MDN MediaRecorder dataavailable](https://developer.mozilla.org/en-US/docs/Web/API/MediaRecorder/dataavailable_event) | timeslice не точный таймер; поведение браузера/блокировка может задерживать данные. Интервал 1000 мс не является доказательством секундной crash-safety |
| [Gemini Live capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities) | Доступны VAD и взаимодействие с прерыванием ответа; требуется согласовать клиентские и серверные activity semantics, а не переносить batch-форму отправки |

Отдельно прочитаны Record Idea Hub ARCHITECTURE/IDEA_HUB_CONTRACT, IdeaHub voice-terminology.yaml и Wonderful Lections continuous-slide-review/terms.ru.json. Точные commit/blob IDs закреплены в [документе 11](11-routing-and-vocabulary.md) и [Live-контракте](live-contract.json). Их существующие batch-ограничения не становятся UX-ограничениями нового Live-продукта.
