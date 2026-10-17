# Постановка: анализ модели коллаборации projects-hub

**Дата:** 14 октября 2026.
**Заказчик:** владелец projects-hub. Текст подготовлен Claude Code по итогам сессии 14 октября.
**Тип задачи:** аналитика. Это не разрешение на реализацию, выдачу прав, установку Slack-приложения, новые подписки или deploy.
**Язык результата:** русский. Продукт называется **projects-hub**, не «Hub».

## 1. Цель

Владелец строит в projects-hub уверенную систему ведения проектов. В ней люди и агенты в нескольких проектах и инструментах делают разные действия ради общих целей: от «поделиться идеей» до вопросов, поручений, голосований и встреч. Требуемый уровень надёжности — «швейцарские часы», при этом без космолёта.

По итогам сессии есть реестр из 77 пользовательских историй и предложенная модель, в центре которой процесс (workflow) и дело. Нужно критически проверить модель на историях и коде и превратить её в реализуемую структуру хранения и минимальный срез. Главные вопросы владельца:

1. Какой список пользовательских сценариев to-be реально нужен.
2. Какую структуру данных нужно сделать, чтобы их поддержать: лаконично и в связке со Slack.
3. Где и как в каждой истории участвуют dot, Slack и другие инструменты, с указанием каких.

## 2. Что прочитать

Документы этой сессии зафиксированы на коммите `f6b073f`, это ветка PR [#172](https://github.com/onedayonemasterpiece/projects-hub/pull/172). После слияния они будут в `main` по тем же путям.

| Документ | Зачем |
| --- | --- |
| [Реестр пользовательских историй](https://github.com/onedayonemasterpiece/projects-hub/blob/f6b073f934a1e0324aa5dd5f65790f48de96454f/docs/product/user-stories.md) | 77 историй `PH-US-NN`: кто и зачем, успех, где участвуют Мира, dot, Slack и другие инструменты, реальность по словам владельца, источники, принятые решения, открытые вопросы |
| [24. Онтология и структура данных](https://github.com/onedayonemasterpiece/projects-hub/blob/f6b073f934a1e0324aa5dd5f65790f48de96454f/docs/product/24-ontology-from-user-stories.md) | Предлагаемая модель: вид процесса, дело, обращение на основе реализованного вопроса, объекты, записи, каталог процессов, донастройка процессов, связка со Slack, кандидаты в MVP |
| [23. Коллаборация уровня «швейцарские часы»](https://github.com/onedayonemasterpiece/projects-hub/blob/f6b073f934a1e0324aa5dd5f65790f48de96454f/docs/product/23-collaboration-swiss-watch-proposal.md) | Правила надёжности, неблокирующая синхронизация со Slack на бесплатном тарифе, сторож сроков и инвариантов. Модель §3–§4 заменена документом 24 |
| [Ревью операционной надёжности](https://github.com/onedayonemasterpiece/projects-hub/blob/f6b073f934a1e0324aa5dd5f65790f48de96454f/docs/audits/project-information-operational-reliability-20261014.md) | Девять мест в коде, где работа застревает или молча исчезает, и порядок исправлений |
| [21. Проектная информация](https://github.com/onedayonemasterpiece/projects-hub/blob/f6b073f934a1e0324aa5dd5f65790f48de96454f/docs/product/21-project-information-unified-access.md) | Исходный трек: общий доступ Миры и dot, Slack ↔ projects-hub, принятые решения владельца в §9 |
| [22. Проектные разрешения dot](https://github.com/onedayonemasterpiece/projects-hub/blob/4bfee3aba91c5fe3a2f8927f45151e053ea4735a/docs/product/22-project-approvals-chat-mira.md) | Отдельная ветка: согласования для dot. В модели 24 согласование — вид обращения |

Источники историй:

| Источник | Ссылка |
| --- | --- |
| Шесть голосовых владельца от 14 октября | [В1](https://github.com/onedayonemasterpiece/idea-hub/blob/9286e0792b90b3b63482fa0d994eb86574ded071/inbox/voice/2026/10/voice-20261014-141233-17af2511.md), [В2](https://github.com/onedayonemasterpiece/idea-hub/blob/9286e0792b90b3b63482fa0d994eb86574ded071/inbox/voice/2026/10/voice-20261014-143420-8c0cd9f2.md), [В3](https://github.com/onedayonemasterpiece/idea-hub/blob/9286e0792b90b3b63482fa0d994eb86574ded071/inbox/voice/2026/10/voice-20261014-150134-d0a7d273.md), [В4](https://github.com/onedayonemasterpiece/idea-hub/blob/9286e0792b90b3b63482fa0d994eb86574ded071/inbox/voice/2026/10/voice-20261014-154204-47dd63a4.md), [В5](https://github.com/onedayonemasterpiece/idea-hub/blob/9286e0792b90b3b63482fa0d994eb86574ded071/inbox/voice/2026/10/voice-20261014-160944-d9f86ec3.md), [В6](https://github.com/onedayonemasterpiece/idea-hub/blob/9286e0792b90b3b63482fa0d994eb86574ded071/inbox/voice/2026/10/voice-20261014-170555-07d461d5.md) |
| Ранние голосовые V01–V33 | [продуктовый контракт](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/docs/product/contract.json), поле `sources` |
| Подкаст в projects-hub | [анализ совместной работы 11 октября](https://github.com/onedayonemasterpiece/idea-hub/blob/9286e0792b90b3b63482fa0d994eb86574ded071/docs/projects-hub-podcast-collaboration-analysis-20261011.md), [программа воркфлоу 13 октября](https://github.com/onedayonemasterpiece/idea-hub/blob/9286e0792b90b3b63482fa0d994eb86574ded071/docs/podcast-workflow-programme-20261013.md), [истории POD-US](https://github.com/onedayonemasterpiece/idea-hub/blob/9286e0792b90b3b63482fa0d994eb86574ded071/docs/podcast-user-stories-20261013.md) |
| Методология историй, только как справка | [продуктовая модель events-bot-new](https://github.com/onedayonemasterpiece/events-bot-new/blob/d431b9f86b87999cf27f74b99857d23aedc28f16/docs/product-model/README.md) |

Код, на котором основана модель, зафиксирован на `main` `76bc5f2`. Перед выводами сверить с актуальным `main`.

| Что | Где |
| --- | --- |
| Вопрос, пакет вопросов, анализ | [collaboration_analysis.py](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/src/projects_hub/collaboration_analysis.py) |
| Личные задачи и шаблоны готовности события | [readiness.py](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/src/projects_hub/readiness.py) |
| Доработка: фазы и стадии Codex | [development.py](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/src/projects_hub/development.py) |
| Заметки, обсуждения, журнал | [collaboration.py](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/src/projects_hub/collaboration.py) |
| Доска | [board.py](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/src/projects_hub/board.py) |
| Привязки к Slack и доставка через dot | [mcp_bindings.py](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/src/projects_hub/mcp_bindings.py), [mcp_events.py](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/src/projects_hub/mcp_events.py), [MCP events](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/docs/mcp-events.md) |
| Участники, права, внешние учётки | [store.py](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/src/projects_hub/store.py) |
| Работа и критерии поставки в DevCoveer | [product_delivery.py](https://github.com/onedayonemasterpiece/codex-devcoveer-mcp/blob/1771fdfdd083e64e89ae5c26cd6e6f553b70e458/runtime/product_delivery.py), [incident_registry.py](https://github.com/onedayonemasterpiece/codex-devcoveer-mcp/blob/1771fdfdd083e64e89ae5c26cd6e6f553b70e458/runtime/incident_registry.py) |

## 3. Решения владельца, которые не пересматриваются

1. Из документа 21 §9: хранить полное обсуждение и отдельно выделять решения; обсуждения связанного канала доступны проектной аудитории; канал принадлежит одному проекту, проект может иметь несколько каналов, тема — несколько тредов; у вопроса ровно один источник с версией.
2. Из Slack берётся полное хранение обсуждений, если проект включён в projects-hub. Каналы остальных проектов не захватываются.
3. Используются только бесплатные возможности Slack. Платные группы пользователей, Workflow Builder, гостевые аккаунты и Slack Connect не используются.
4. Процесс (workflow) — центральное понятие. В проекте может быть несколько настраиваемых процессов, их задача — ничего не забыть.
5. Процессы настраивают не профессионалы, а участники своими историями: «я хочу …, сделав …, в итоге получив …, чтобы …». Пока настройка полностью у dot. Запросить изменение могут владелец проекта и владелец системы. Изменение обсуждается, dot задаёт вопросы по стандартному потоку вопросов, после итераций выходит новая версия процесса.
6. Изменения процессов поставляются обновлением бэкенда. Приложение обновляется только при реальной необходимости.
7. Регулярные публикации пока не поручаются, только разовые.
8. Система работает без dot. Мира от него не зависит.
9. Мира не является пультом запуска разработки: доработка созревает в треде Slack, запуск отдельно.
10. Подкаст ведётся в projects-hub и является первым реальным пилотом.

## 4. Предложения, которые ещё не приняты и могут быть оспорены

- Пять сущностей и вид процесса: участник, проект, дело как экземпляр процесса, объект, запись, обращение (документ 24 §2–§6).
- Обращение как расширение реализованного вопроса видами «сделать», «выбрать», «напомнить», «рассмотреть», адресатом-ролью или группой и правилом ответа (§5).
- Определение процесса как версионированный файл в репозитории проекта, проверяемый бэкендом по схеме; дело помнит свою версию; разовые пункты; откат (§3).
- Универсальная отрисовка дела из стандартных видов полей и общие инструменты Миры, чтобы новые версии процесса не требовали обновления приложения (§3).
- Собственный Slack-бот projects-hub для захвата, команд и отправки, dot — поверх сохранённых данных (документ 23 §5, документ 24 §9).
- Кандидаты в MVP: аналитика с вопросами, подготовка выпуска подкаста с подпроцессами гостя и встречи, доработка, сигнал о сбое, донастройка процесса через dot (документ 24 §10).

## 5. Ограничения

- Не строить универсальный BPM-движок, язык процессов, ветвления и скрипты. Это запрещено в [ADR-10](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/docs/product/04-architecture.md), [07](https://github.com/onedayonemasterpiece/projects-hub/blob/76bc5f2d84424f26bf3d0056687ae6a5dfcbde81/docs/product/07-delivery.md) и `contract.json`. Процесс — данные из фиксированного набора элементов.
- Не вводить новый оркестратор, второй семантический маршрутизатор или отдельного поискового агента (документ 21 §10).
- Live-агент: маленькое ядро промпта и наборы по 3–6 инструментов по [архитектуре live-interaction](https://github.com/onedayonemasterpiece/live-interaction/blob/main/docs/live-agent-architecture.md).
- Изменение содержания требует expected revision и устойчивого command id.
- Тесты на заглушках не заменяют реальную проверку голосом, через MCP или в Slack.
- В этой задаче ничего не реализовывать и не развёртывать, не выдавать права и не менять подписки, привязки и Slack-приложения.

## 6. Что нужно сделать

1. **Проверить модель на историях.** Каждая история с реальностью «было» или «нужно» должна пройти через модель документа 24. Показать, где не проходит и что добавить. Показать, что в модели лишнее, и упростить, где можно.
2. **Предложить структуру хранения поверх существующих таблиц.** Дать таблицу «сущность модели → существующие таблицы → новые поля и таблицы → миграция без потери данных». Учесть `collaboration_analyses`, `collaboration_questions`, `owner_development_questions`, `tasks`, `task_executions`, `event_cards`, `project_notes`, `board_objects`, `collaboration_events`, привязки и outbox.
3. **Набросать схему определения процесса.** Этапы, пункты, стандартные виды полей, правила и закрытый список их действий, подпроцессы, уведомления, канал Slack. Описать проверку, версии, перевод дел и откат. Отдельно отметить, что потребует кода projects-hub.
4. **Описать путь донастройки через dot.** Нужные операции MCP, путь вопросов через существующий поток, права владельца проекта и владельца системы, связь с согласованиями из документа 22. Показать, как история владельца превращается в изменение процесса и Gherkin-пример.
5. **Проверить Slack на бесплатном тарифе.** События, чтение истории, упоминания, кнопки, нужные права приложения. Учесть, что в workspace сейчас занято 3 из 10 мест для приложений. Описать, какие сообщения пишет бот, как устроены треды дел, сверка и неблокирующая отправка.
6. **Уточнить MVP.** Подтвердить или изменить срез, дать порядок работ и критерии приёмки в виде Gherkin-сценариев, включая отказы: Slack недоступен, dot молчит, источник изменился.
7. **Учесть дефекты из ревью.** Отметить, какие из них блокируют MVP. Например, вопросы молча исчезают при правке заметки-якоря, а уведомление Миры зависит от доставки в Slack.
8. **Сформулировать вопросы владельцу.** Только блокирующие, немного и продуманно. Каждый вопрос — с вариантами и рекомендацией. Они пойдут владельцу через стандартный поток вопросов.

## 7. Формат результата

Один Markdown-документ в `docs/product/` репозитория projects-hub, в отдельной ветке с PR, с индексом в `docs/product/README.md`. Документ проходит тест `tests/test_product_spec.py`: начинается с `# ` и содержит только существующие локальные ссылки.

Разделы:

1. Вывод в пяти–семи пунктах.
2. Исправления модели.
3. Структура хранения и миграция.
4. Схема определения процесса.
5. Донастройка через dot.
6. Slack на бесплатном тарифе.
7. MVP, порядок и Gherkin-критерии.
8. Риски.
9. Вопросы владельцу.

В каждом утверждении различать: «проверено в коде на SHA …», «предложение», «не проверено». Не объявлять реализованным то, что есть только в документации.
