# Tests

`test_agents.py` проверяет транспорт, общий tool runtime, Coordinator и маршруты
Executor/Developer. `test_webapp.py` проверяет Store, worker и Flask API на
временной SQLite. `browser_smoke.py` запускается отдельно и проверяет UI настоящим
Chromium с имитацией Coordinator.

Тесты не должны обращаться к production БД, реальному inference API или systemd.
Используй `tmp_path`, mock transport/runners и конечные ожидания потоков.
`pytest.ini` ограничивает discovery каталогом `tests/`, чтобы пользовательские
файлы и тесты удалённого Executor не выполнялись вместе с тестами системы.
Основной запуск: `venv/bin/python -m pytest -q`; браузерный:
`venv/bin/python tests/browser_smoke.py`.
