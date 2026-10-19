# E5 device diagnostic — 2026-10-04

Две opt-in страницы, без изменения рабочего поиска и без APK:

- `/labs/embedding-benchmark.html`: вычисление E5 на устройстве и экспорт JSON;
- `/labs/embedding-report.html`: локальное сравнение нескольких файлов результатов.

Они размещены на `https://projects-hub.kenigevents.ru`. HTML исходники находятся в `web/public/labs/`. Runtime получил только два новых статических файла; backend не перезапускался, существующие страницы и авторизация не изменялись. До merge этой ветки следующий полный deploy может убрать runtime-копии.

## Реально выполнено

Desktop headless Chromium на DevCoveer, НЕ физический телефон:

| Метрика | Наблюдение |
| --- | --- |
| run_id | bb5ff2e4-0978-4c20-8d68-3bbe88992569 |
| App/runtime | diagnostic 1.0.1 / Transformers.js 3.8.1, WASM, 1 thread |
| Состояние | completed_review |
| Векторы вопросов | 30/30 |
| Warm p95 | 112.61 ms |
| Queued4 p95 | 234.06 ms |
| Token IDs | 20/20 совпали |
| Строгие vector checks | 11/20 прошли |
| Минимальный cosine | 0.9983935998445772 |
| Максимальное отклонение компонента | 0.00982438400387764 |
| RAM всего приложения / phone stability | не измерены |
| Retrieval quality именно browser-векторов | ещё не оценена |

Оригинальный JSON скачан через browser bridge и прочитан локальным report viewer. SHA-256: `ec073bafd12a2565568c68f9e82bfd8289a7134e4ca5df93f5f5586dd6481e56`.

Это не PASS совместимости. Reference tolerances .999 cosine / .002 component не ослаблялись. Нельзя включать client encoder автоматически до оценки влияния отклонений на ranking. Первоначальная ошибка standalone ES-module bundle исправлена в 1.0.1; это ошибка стенда, не телефона.

## Запуск владельцем устройства

Откройте diagnostic на телефоне через Wi-Fi. Укажите метку модели/браузера, подтвердите загрузку примерно 150–190 МБ публичных статических файлов и нажмите «Запустить тест». До завершения оставьте вкладку видимой. Сохраните JSON; затем перезагрузите страницу и повторите ещё дважды без удаления кэша. Сохраните каждый результат отдельно. Отдельно проверьте прерывание при сворачивании: ожидается `interrupted_hidden`, а не PASS.

«Удалить кэш только этого теста» удаляет только его CacheStorage namespace. Голосовая очередь, проекты и вход не трогаются. Результаты автоматически не отправляются. Если download/share не поддерживается WebView, есть «Показать JSON». Для завершённого теста пакет содержит query-векторы, а не текст книги.

Первый стенд — CPU/WASM E5-small INT8, без WebGPU. Полная RAM через portable browser API не измеряется: `deviceMemory` — capacity hint. Chrome, WebView приложения и Safari должны иметь отдельные результаты. Не выдавать desktop/browser smoke за Android или универсальную поддержку.

## Оценка извлечения без нового inference

`evaluate_device_run.py` сравнивает клиентские query-векторы с той же приватной E5 document matrix. Нужны существующий retained lab и fixtures из Regional Knowledge commit `f77f78427984fd970c85339c7de4f53a0f253004`.

```sh
LAB=/home/dev/artifacts/regional-knowledge-base/20261004T001513Z-small-embeddings-20261004
REPO=/home/dev/projects/regional-knowledge-base
"$LAB/venv/bin/python" evaluate_device_run.py /path/to/device.json \
  --lab "$LAB" --benchmark-root "$REPO" --output /path/to/new-evaluation.json
```

Проверяются версии/hashes, все question IDs, конечные значения, размерность и L2. Рассчитываются required-evidence Recall@1/3/5/10, Hit, MRR, all-evidence@10, top10 overlap; vector/fused режимы и native reference показаны отдельно. Новый файл не затирает предыдущий. API, модель и production DB не вызываются.

Оценку и device JSON можно открыть вместе в report viewer; связь проверяется по run_id и SHA-256. Не публикуйте corpus.jsonl или документные векторы. Snapshot имеет известное ограничение полноты: обрезка native_blocks до 1000 символов, 200 чанков ровно такой длины и отсутствие текстовых чанков на последних 30 страницах. Текущие qrels не доказывают Precision или точность ответа Миры.

## Тесты и принятие

`python -m pytest -q test_evaluate_device_run.py`: 10 локальных арифметических/контрактных тестов прошли. Synthetic fixtures — только тесты кода, не device evidence. Общий CI проекта/Android этим не объявляется пройденным.

Для пилота собрать минимум три прогона на бюджетном, среднем и современном Android, отдельно Chrome и фактический WebView. Учитывать все отказы/прерывания, не только лучшие измерения. Результаты версионировать по модели, runtime и браузеру; повторять после обновлений. Серверный encoder остаётся основным резервом, клиент — опцией. Тест совместно с голосом и ADB memory profile пока не проведены.

Продолжение проверки качества: [постановка для Codex](https://github.com/onedayonemasterpiece/regional-knowledge-base/blob/4d69ff80e135533df1ff2f0c009db6443c781ce0/docs/prompts/embedding-retrieval-validation-followup-2026-10-04.md).
