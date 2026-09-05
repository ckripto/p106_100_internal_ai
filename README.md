# Local multi-agent system

The project runs a Coordinator, an Executor for user workspaces, a Developer for
maintaining the system, and a durable single-user web interface. Detailed module
contracts are documented in the hierarchical `AGENTS.md` files.

## Configuration

Copy the configuration template and fill deployment-local hostnames:

```bash
cp .env.example .env
cp AGENTS.local.md.example AGENTS.local.md
```

`.env` and `AGENTS.local.md` are intentionally ignored. Never commit addresses,
credentials, tokens, databases, workspace contents or operational reports.

Install the web unit from `ops/agents-web.service.example`, then run:

```bash
venv/bin/python -m pytest -q
venv/bin/python -m web_service
```

More details: `agents/README.md` and `web_service/README.md`.
