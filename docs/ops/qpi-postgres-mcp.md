# QPI PostgreSQL MCP

`qpi-pg-prod` is the approved Codex MCP path for read-only QPI production PostgreSQL inspection. It replaces ad-hoc manual SSH tunnels for routine diagnostics, but it does not replace `psql`, `schema_remote.sh`, or private-runner validation for schema changes, writes, and repairs.

## Architecture

```text
Codex local process
  -> ssh -T ubuntu@<bot-public-ip> /usr/local/bin/qpi-pg-mcp
  -> DBHub over MCP stdio on the bot VM jump host
  -> qpi_mcp_readonly@<private-db-ip>:5432/qpi
```

There is no DBHub HTTP listener, no systemd daemon, no public MCP port, and no direct workstation access to PostgreSQL. Codex launches DBHub on demand through SSH, and DBHub exits when the MCP session ends.

Remote files on the bot VM:

- `/usr/local/bin/qpi-pg-mcp`: starts DBHub in stdio mode with `docker run --pull never` and routes DBHub banner/log lines to stderr so stdout stays MCP JSON-RPC only.
- `/etc/qpi/qpi-pg-mcp.env`: read-only database connection details.
- `/etc/qpi/dbhub-qpi.toml`: DBHub source and tool configuration.
- `/etc/qpi/qpi-pg-mcp.image`: pinned DBHub image digest or tag.

## Install Or Refresh

```bash
BOT_VM_HOST="$(terraform -chdir=infra output -raw bot_public_ip)" \
scripts/deploy/qpi_pg_mcp.sh install
```

The install command:

- reads the live app `DATABASE_URL` from `/etc/qpi/bot.env`,
- creates or updates the `qpi_mcp_readonly` PostgreSQL role through the DB VM admin path,
- grants read-only access to current and future `public` objects owned by the app DB user,
- installs Docker CE if the existing VM drifted from the current bot cloud-init package set,
- pulls `bytebase/dbhub:0.22.3` on the bot VM and stores the resolved image reference,
- writes the DBHub config and wrapper on the bot VM.

Rotate the MCP role password without changing the architecture:

```bash
BOT_VM_HOST="$(terraform -chdir=infra output -raw bot_public_ip)" \
scripts/deploy/qpi_pg_mcp.sh rotate-secret
```

## Local Codex Registration

```bash
scripts/dev/qpi_pg_mcp_codex.sh install
scripts/dev/qpi_pg_mcp_codex.sh doctor
```

The local helper registers:

```bash
codex mcp add qpi-pg-prod -- ssh -T -i ~/.ssh/id_rsa -p 22 \
  -o LogLevel=ERROR -o StrictHostKeyChecking=accept-new \
  ubuntu@<bot-public-ip> /usr/local/bin/qpi-pg-mcp
```

It also adds approval gates for DBHub `execute_sql` tool names in the local Codex config.

## Smoke Checks

```bash
BOT_VM_HOST="$(terraform -chdir=infra output -raw bot_public_ip)" \
scripts/deploy/qpi_pg_mcp.sh status

BOT_VM_HOST="$(terraform -chdir=infra output -raw bot_public_ip)" \
scripts/deploy/qpi_pg_mcp.sh smoke

scripts/dev/qpi_pg_mcp_codex.sh doctor
```

The remote smoke uses the installed read-only credentials from the bot VM, checks that a simple catalog query succeeds, and verifies that `CREATE TABLE` is rejected.

## Guardrails

- Use `qpi-pg-prod` only for read-only production diagnostics.
- Never use the global `pg-prod` MCP in this repo.
- Do not use MCP for schema apply, cleanup, production repair writes, or any workflow that needs an auditable mutation path.
- If MCP is unavailable, fall back to the documented SSH tunnel plus `psql` path.
