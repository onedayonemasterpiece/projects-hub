# Projects Hub

## Продуктовая проработка 27 сентября 2026

[Полная спецификация](docs/product/README.md): 18 голосовых, имя «Содей» как предложение, питч, насыщенный PWA + Android MVP, изоляция данных и 20 выпускных проверок. Это план реализации, не готовое приложение; технический идентификатор `projects-hub` сохранён.

Голосовой диалог по проектам, идеям и документации с итеративным уточнением и правками. Будущая замена `record-idea-hub`, а не переименование работающего диктофона.

Основание: голосовое `idea-hub/inbox/voice/2026/09/voice-20260926-134715-8a684154.md` и прямое поручение владельца от 26 сентября 2026 создать этот репозиторий. [Продуктовое видение](docs/vision.md).

## Что уже есть

`src/projects_hub/live_resources.py` — исполняемая серверная граница подключения общего `ai-resource-control`: фиксированный consumer `projects-hub`, обязательная авторизованная область проекта и отдельная привязка ресурсов для пользователя/проекта. Normal Live использует central authority; при её транспортной недоступности trusted backend может взять только назначенный Projects Hub source alias `GOOGLE_API_KEY4` и передать его shared SDK как `AI_RESOURCE_CONTROL_FALLBACK_KEY` по общему bounded fallback-контракту. Unit tests проверяют изоляцию привязки, передачу управления именно общему SDK и корректный отказ при отсутствии пакета.

Это **не готовое Android-приложение и не готовый production backend**. В этом checkpoint нет микрофонного UI, GitHub write tools, runtime deployment и реальной Live-приёмки. Работающий Record Idea Hub не менялся.

## Общий ресурс

Общий пакет: private `onedayonemasterpiece/ai-resource-control`. Транспорт: `onedayonemasterpiece/live-interaction`. Одна существующая Supabase authority; проект не создаёт собственный quota ledger. Подробности и текущий blocker: [resource-rollout.md](docs/resource-rollout.md).

Runtime устанавливает private wheel авторизованным deployment-процессом и совместимый pinned transport. Сам package scaffold не подменяет частный dependency одноимённым публичным PyPI-пакетом. Без SDK возвращается `RESOURCE_PACKAGE_MISSING`; старый транспорт SDK отклоняет до вызова Google.

## Проверка

```sh
python -m pip install -e '.[test]'
PYTHONPATH=src python -m pytest -q
```

Тесты offline, без API key, Supabase и расходов провайдера.
