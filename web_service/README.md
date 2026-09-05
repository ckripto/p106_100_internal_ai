# Веб-интерфейс

URL задаётся локальным `.env`. Один пользователь, без аутентификации.

Сессии и задачи сохраняются в `/opt/agents/data/agents.sqlite3`. Очередь общая и
обрабатывается одним worker. Закрытие браузера не останавливает выполнение. При
возврате UI загружает историю и текущий progress. Workspace агентов не удаляется
вместе с сессией.

Под ответом Coordinator находится спойлер «Журнал агентов и LLM». В нём показываются
подзадачи, компактные ответы и каждый наблюдаемый запрос агента к модели с ответом,
номером шага, временем сообщения и продолжительностью ожидания. Ошибка или тайм-аут
LLM записываются отдельным ответом. Журнал обновляется во время выполнения и
хранится в БД; system prompts и скрытая цепочка рассуждений в него не входят.

После рестарта ожидающие задачи сохраняются, а выполнявшаяся становится
`interrupted`, поскольку инструменты могли уже дать внешний эффект. request_id
предотвращает дублирование при повторе HTTP-запроса. Для backup работающей БД
используй SQLite backup API, а не копирование только основного файла при WAL.

## Настройки

Все web settings находятся в `settings.py`: `AGENTS_DB`, `WEB_HOST`, `PORT`,
`WEB_THREADS`, `WEB_CHANNEL_TIMEOUT`, `WEB_MAX_REQUEST_BODY_SIZE`,
`WEB_QUEUE_POLL_INTERVAL`. Agent settings находятся в пакетах соответствующих
агентов и задаются локальным `.env`. Production unit использует
`EnvironmentFile=/opt/agents/.env`.

## Эксплуатация

```bash
systemctl status agents-web.service
journalctl -u agents-web.service -n 60 --no-pager
systemctl restart agents-web.service
curl http://localhost/api/health
```

Production entry point: `venv/bin/python -m web_service`.

Проверки:

```bash
venv/bin/python -m pytest -q
venv/bin/python tests/browser_smoke.py
```

Browser smoke использует временную БД и имитацию Coordinator, проверяет мобильный
экран, фоновую задачу, уточнение, reload, безопасный текст и удаление сессии.
