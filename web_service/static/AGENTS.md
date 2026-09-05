# Browser client

Статический UI использует только HTML/CSS/vanilla JavaScript без CDN и сборки.
`app.js` опрашивает API раз в три секунды, хранит выбранную сессию, draft и
идемпотентный pending request в localStorage. Пользовательский текст вставляется
только через `textContent`.

`task.agent_messages` отображается в сохраняющем открытое состояние details.
Каждая запись показывает sender/recipient, тип обмена, попытку или шаг LLM,
локальное время и для response его продолжительность. JSON форматируется только
после `JSON.parse`, а затем также вставляется через `textContent`.

Сохраняй mobile-first layout, доступные labels, работу после reload и закрытия
вкладки. Не добавляй секреты, implementation details агентов или внешние ресурсы.
Изменения проверяй `tests/browser_smoke.py` на мобильном и desktop viewport.
