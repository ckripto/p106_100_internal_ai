# Security

Do not commit deployment addresses, credentials, access tokens, private keys,
production databases, user workspaces or operational reports. Store local values in
ignored `.env`, `AGENTS.local.md` and deployment files without the `.example`
suffix. Tracked examples must contain placeholder hostnames and dummy values only.

Before every commit, inspect the staged file list and scan staged contents for
secrets. If a secret is committed, remove it from the complete Git history before
pushing and rotate the credential when applicable.
