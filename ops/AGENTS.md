# Operations

`agents-web.service.example` — шаблон production unit приложения;
`llama-server.service.example` и `llama-server.env.example` — шаблоны inference.
Локальные копии без суффикса и эксплуатационные отчёты игнорируются Git.

Unit web-service должен запускать package entry point из `/opt/agents`, использовать
venv, абсолютный путь production БД, `Restart=on-failure`, process-wide kill и
закрытый umask. Настройки каждого агента задаются отдельными environment variables.
Production defaults дают Coordinator, Executor и Developer до 300 секунд на ответ;
одна делегация Executor или Developer также ограничена 300 секундами.

После изменения unit-шаблона: установить копию без `.example`, выполнить
`systemctl daemon-reload`,
restart и проверить `ActiveState`, environment и health. Не запускай вторую копию
llama-server; перед изменениями модели прочитай локальные ограничения ресурсов и
учитывай длительную загрузку.
