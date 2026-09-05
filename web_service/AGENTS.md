# Web service

## Компоненты

`app.py` создаёт Flask application, объявляет HTTP API и запускает Waitress.
`store.py` — единственный владелец SQL и переходов состояния SQLite.
`worker.py` — единственный consumer очереди и адаптер к Coordinator.
`settings.py` — все настройки web-service. `static/` — браузерный клиент.

API и UI однопользовательские, без аутентификации; адрес развёртывания находится
только в локальном `.env`. Защита включает same-origin checks для мутаций, JSON-only
requests, CSP, `nosniff`, отсутствие cache. Максимум тела запроса 16 КиБ, текста
задачи 1800 символов.

## Очередь и данные

SQLite хранится в `/opt/agents/data/agents.sqlite3`, WAL включён. request_id
обеспечивает идемпотентную отправку. Один process lock запрещает двух владельцев,
а `BEGIN IMMEDIATE` защищает claim. Во всей системе выполняется одна задача.
Закрытие клиента не влияет на worker. При старте `running` становится
`interrupted`; `queued` сохраняется. Автоповтор running запрещён из-за возможных
частичных внешних эффектов.

Таблица `agent_messages` хранит делегации, компактные ответы агентов и обмен с LLM:
направление, номер попытки, шаг LLM, timestamp и длительность ответа. Worker пишет
события по мере выполнения, поэтому последний сохранённый запрос или tool call
виден и у задачи, завершившейся тайм-аутом. API возвращает журнал в
`task.agent_messages`, включая выполняющуюся задачу. Из журнала исключены system
prompts и скрытая цепочка рассуждений.

Нельзя менять схему без совместимой миграции существующей БД. Persistence failure
завершает process для восстановления systemd; ошибка отдельной задачи даёт failed
без утечки traceback в API.

Production entry point: `venv/bin/python -m web_service`. Шаблон unit находится в
`ops/agents-web.service.example`. После изменений проверяй unit tests, browser smoke при
изменении UI и `/api/health` после restart.
