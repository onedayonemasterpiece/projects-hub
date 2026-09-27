# UI-концепция: тёмные плавающие острова

[Индекс](README.md) · [Live UX](03-product-and-ux.md) · [Центральный агент](12-central-live-agent.md).

**Ревизия 4, 27 сентября 2026.** Визуальное направление первой реализации PWA и Android.

## Образ

Интерфейс — не dashboard и не чат-клон. Основа — спокойный почти чёрный холст с несколькими **плавающими островами**. Каждый остров появляется только когда несёт актуальный смысл.

Главный принцип: один экран должен ощущаться как рабочее пространство вокруг живого разговора, а не как панель управления проектами.

## Dark-first

MVP проектируется тёмным по умолчанию: почти чёрный canvas, графитовые islands, слабый светлый edge, мягкий белый foreground, спокойный серый secondary text. Listening использует холодный cyan/blue accent; offline/pending — restrained amber; destructive/error — restrained red.

Точные цвета утверждаются после OLED/LCD и contrast review. Не превращать интерфейс в cyberpunk.

## Главный экран

Постоянно видны максимум три зоны.

**Context island** сверху: личный/workspace контекст, current project focus либо «несколько проектов», при необходимости audience/privacy indicator. Это индикатор, не обязательный selector. Agent сам меняет focus голосом. Tap открывает Projects.

**Work island** в центре показывает только актуальный материал: краткий результат/ответ, документ, варианты решения, время, task/result, confirmation или source refs. Это не бесконечная transcript-лента. Если визуальный материал не нужен, центр может быть почти пустым.

**Voice island** снизу и пространственно стабилен. Большая voice button/orb ориентировочно 96–112 dp + короткий статус. Состояния: ready, listening, answering, reconnecting, offline recording, buffered transfer, stopped/error. Геометрия кнопки не меняется; меняются свет, pulse/waveform и короткая подпись.

## Пульсация

Listening связан с реальным уровнем речи. Waiting/thinking — минимальная breathing-анимация. Answering — ритм output. Offline recording — capture pulse плюс тёплый indicator. Idle — без постоянной декоративной анимации. Reduced motion обязателен.

## Contextual actions

Кнопочные ответы появляются отдельным небольшим action island: проект при неоднозначности, слоты времени, «Беру / Не могу / Нужна помощь», подтверждение публикации/приглашения, найденные источники.

После выбора island исчезает или становится result. Это ответ внутри разговора, не Submit workflow.

## Projects, Mine и History

Нет тяжёлого постоянного sidebar.

Project switcher открывается floating sheet/island: recent/active projects, outcome/next step, group, connected resources. На desktop — боковой остров; на mobile — нижний sheet. Выбор project меняет focus, но не обязан завершать Live.

Mine/Changes показывает только то, что требует внимания: мои задачи, запросы решения/времени, важные изменения, pending source только когда нужен человек.

History содержит даты/темы, project links, archived voice sources, unresolved loops и подтверждённые actions. Голосовой поиск по истории остаётся основным быстрым способом.

## Integration UI

Connections находятся в Project/Workspace settings.

GitHub island показывает account/organization, выбранные repositories, их role, read/write policy и access state. Главное действие: «Подключить GitHub» / «Изменить доступ». Поля «вставьте token» нет.

Если во время разговора нужен недоступный repo, agent показывает компактный integration island «Нужен доступ к repository» + кнопку «Разрешить в GitHub». После callback разговор продолжается без повторного наговаривания.

## Первый вход

Войти → личное пространство создаётся автоматически → сразу видна большая voice button → agent может спросить, над чем работаем. GitHub/calendar/notifications подключаются по потребности.

Не требовать GitHub или заполнение project form до первого разговора.

## Mobile / Desktop

Mobile: context сверху, work по центру, action над voice, voice внизу safe-area, Projects/History как sheets.

Desktop: больше свободного canvas, крупнее work island, auxiliary islands по краям, voice остаётся нижним центром. Не создавать отдельный desktop-dashboard.

## Accessibility

Контраст, accessible labels, targets >=44 dp, keyboard в PWA, screen reader, reduced motion, scalable type. Цвет не единственный носитель критического состояния.

## Не делать

Не делать sidebar+header+dashboard cards, постоянную transcript-ленту как чат, десятки badges, всегда открытое дерево проектов, text composer, декоративные islands без функции, тяжёлый glassmorphism или onboarding wizard.

## UI acceptance

Без обучения пользователь должен: начать разговор; видеть реальную запись; понять offline capture; голосом перейти в другой проект; выбрать предложенный вариант кнопкой; найти прошлый разговор; открыть Projects/Mine; подключить GitHub без поиска токенов.

PWA и Android проходят один scenario corpus.
