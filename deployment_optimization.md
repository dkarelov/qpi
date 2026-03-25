# Deployment / Testing Optimization Plan

## Goal

Reduce time lost to waiting, environment drift, and repeated infrastructure discovery during DB-backed test runs and live deployments, while moving toward a cleaner long-term production operating model.

This plan is intentionally focused on:

- test execution architecture,
- runtime / function deployment architecture,
- operational runbooks and documentation,
- removing fragile assumptions discovered in the previous implementation cycle.

Explicitly deferred for now:

- object-storage-backed function deploy path for oversized archives.

That should only be added if inline YC Function zip uploads become a real blocker again.

## Updated Decisions From Discussion

The optimization target is now:

1. A dedicated self-hosted GitHub runner VM, separate from the production bot VM.
2. The runner VM is preemptible and normally powered off.
3. GitHub-hosted bootstrap jobs start the runner VM on demand, wait for the runner to come online, then hand off DB-backed tests and deploy work to that runner.
4. After work completes, the runner shuts down automatically after a short idle window.
5. A lightweight weekly keepalive start is scheduled so the runner stays registered and observable even while mostly powered off.

Rationale:

- this keeps the production bot VM clean and single-purpose,
- keeps cost low because the runner is on only when needed,
- accepts preemptible tradeoffs because deployments are not urgent and waiting is acceptable,
- preserves a clear future path if the runner later needs to move to a larger or non-preemptible VM.

## What Went Wrong In Practice

The previous cycle spent much more time on waiting than on implementation. The main causes were:

1. Local workstation was treated as a primary executor for DB-heavy work.
   - `qpi_test` was often reached over an SSH tunnel to the private DB VM.
   - `psqldef` and long DB-backed test runs over that tunnel were slow and brittle.

2. Test DB lifecycle ownership was unclear.
   - The normal app role (`qpi`) could not reliably recreate `qpi_test` / `qpi_test_scratch`.
   - The actual admin-capable path existed, but only through DB VM access as `postgres`.
   - That capability was not encoded into the standard workflow.

3. DB-backed isolation was not strong enough.
   - A single long shared-DB run allowed cross-file contamination.
   - Some tests mutate schema shape and rely on repairing it later.
   - Failed or interrupted sessions left open backends and deadlocks behind.

4. CI and deploy execution did not match the intended operating model.
   - DB-backed jobs ran on remote GitHub-hosted runners, not in the private network.
   - Deploy wrappers had to rediscover environment facts instead of consuming a stable internal path.

5. Documentation did not encode enough operational truth.
   - It was not clear which work should run locally, on GitHub-hosted runners, on the private runner, or on the bot VM.
   - The DB-admin reprovision path was not part of the standard documented workflow.

## Design Principles

1. The safest path should also be the default path.
2. DB-backed tests must run inside the private network.
3. The live bot VM should not double as the CI/deploy executor.
4. Disposable test DB recreation is preferable to healing a dirty shared DB.
5. GitHub-hosted runners should bootstrap private work, not replace private execution.
6. Preemptible runner interruptions are acceptable for this stage; simplicity and cost matter more than uninterrupted capacity.
7. Every privileged operational path must be explicitly scripted and documented.

## Target Architecture

### 1. Canonical Execution Split

Use four execution tiers:

1. `fast`
   - Runs on the developer workstation or GitHub-hosted runner.
   - No private DB dependency.
   - Includes non-DB unit-style suites and deterministic Telegram harness.

2. `db-integration`
   - Runs on the dedicated private self-hosted runner VM.
   - Reaches the private DB cheaply.
   - Uses a freshly recreated disposable `qpi_test` per test file.

3. `schema-compat-and-migration`
   - Runs on the same private self-hosted runner VM.
   - Uses fresh disposable DBs per file/batch.
   - Keeps schema-mutating and migration smoke tests isolated from ordinary DB-backed app tests.

4. `deploy`
   - Runtime deploy and function deploy jobs run on the dedicated private self-hosted runner VM after validation succeeds.
   - Terraform remains reserved for actual infra mutations.

Workstation tunnel access remains available for manual inspection and spot checks, but it is not the primary path for full DB-backed validation.

### 2. Runner VM Lifecycle

Introduce one dedicated self-hosted runner VM with these rules:

- separate from the production bot VM,
- preemptible,
- normally powered off,
- started on demand by a GitHub-hosted bootstrap job,
- auto-starts the GitHub runner service on boot,
- shuts down automatically after an idle window,
- receives a weekly keepalive boot.

Lifecycle flow:

1. A workflow needing private execution starts on a GitHub-hosted runner.
2. The bootstrap job starts the runner VM in the correct YC folder.
3. The bootstrap job waits until the self-hosted runner is `online`.
4. DB-backed test and/or deploy jobs run on the self-hosted runner using labels such as `self-hosted`, `linux`, `qpi-private`.
5. A final cleanup job schedules shutdown, or a runner-local idle-shutdown mechanism powers the VM off after 30 minutes of inactivity.

Notes:

- waiting for preemptible capacity is acceptable,
- no attempt should be made to use the bot VM as a fallback execution target,
- workflow logic should target a generic private runner label so the backing VM can later change without rewriting CI.

### 3. Weekly Keepalive

Because the runner VM is mostly powered off, add a small weekly workflow that:

1. starts the runner VM,
2. waits until the runner service is online,
3. records a minimal health signal,
4. powers the runner off again.

Purpose:

- keep the runner registration fresh,
- detect broken boot or registration drift before a real deployment is needed,
- preserve observability for a mostly-off host.

This workflow should do no DB work and no deploy work.

### 4. Test DB Lifecycle

Make the DB VM the owner of test DB provisioning.

Canonical rule:

- `qpi_test` is disposable,
- `qpi_test_scratch` is disposable,
- both are recreated from scratch before DB-backed runs,
- the runner VM never tries to reuse a possibly dirty DB from a prior run.

Supported lifecycle:

1. From the private runner, connect to the DB VM admin path.
2. Recreate `qpi_test` and `qpi_test_scratch`.
3. Apply schema using `psqldef` against the fresh DBs.
4. Run DB-backed tests from the private runner against the private DB address.

This removes:

- tunnel latency,
- dependence on app-role `CREATEDB`,
- contamination from local aborted runs.

### 5. DB-Backed Isolation Model

Adopt file-level reprovisioning instead of one long shared-database run.

Recommended model:

- before each ordinary DB-backed test file:
  - recreate `qpi_test`,
  - apply current repo schema,
  - run that file,
- before each schema-mutating compatibility file:
  - recreate a fresh disposable DB,
  - run only that file or tightly coupled batch,
- before migration smoke:
  - recreate `qpi_test_scratch`,
  - apply current repo schema,
  - run migration file(s).

Why file-level and not per-test:

- per-test full DB recreation is too slow,
- single shared DB for the entire suite is too fragile,
- file-level gives clean boundaries at reasonable cost.

### 6. DB Test Inventory And Contracts

Introduce explicit checked-in manifests under `tests/`, for example:

- `tests/db_integration_manifest.txt`
- `tests/schema_compat_manifest.txt`
- `tests/migration_smoke_manifest.txt`

Contract:

1. Ordinary DB-backed files:
   - assume current schema is already applied,
   - may rely on per-test truncation within the file,
   - must not leave schema drift behind.

2. Schema-compat files:
   - may mutate schema shape,
   - must run in their own fresh DB context,
   - must not be grouped with ordinary DB app suites.

3. Migration smoke files:
   - run against scratch DBs only,
   - remain isolated from all other DB-backed files.

The manifest should become the source of truth. Dynamic grep-based discovery should no longer decide what is part of the private DB suite.

### 7. Deployment Model

Keep the current split:

- runtime bot deploy via direct wrapper,
- Cloud Functions deploy via direct wrapper,
- Terraform only for infra mutations.

But change where the work runs:

- fast validation may still run on GitHub-hosted runners,
- DB-backed validation and deploy steps run on the private self-hosted runner,
- the deploy workflows must start the runner VM first and shut it down after.

## Concrete Improvements

### A. Bootstrap / Runner Lifecycle Automation

Add bootstrap automation for workflows that need the private runner:

Responsibilities:

1. start the preemptible runner VM in the correct YC folder,
2. wait for SSH reachability if needed,
3. wait for GitHub runner registration to become `online`,
4. expose clear timeout and failure reasons,
5. trigger shutdown after workflow completion,
6. support the weekly keepalive start.

Recommended implementation shape:

- GitHub-hosted bootstrap and cleanup jobs in workflow YAML,
- a small checked-in helper script for start/wait/stop logic,
- runner-local systemd service for the GitHub runner,
- runner-local idle shutdown mechanism with a 30-minute default window.

### B. Runner Host Guardrails

Provision the runner VM so it is operationally separate from the bot:

- dedicated VM,
- dedicated runner user such as `github-runner`,
- runner workspace under a non-app path,
- cached `uv` / dependency artifacts on the runner,
- no live application deployment under the runner workspace,
- minimal required credentials only.

The runner VM must not contain the live bot runtime. It is a private execution host only.

### C. New Private-Runner DB Test Driver

Add a first-class script, for example:

- `scripts/dev/run_db_tests_on_runner.sh`

Responsibilities:

1. read checked-in DB test manifests,
2. reset disposable DBs before each file or batch,
3. run DB-backed test files one by one,
4. print per-file pass/fail summary,
5. stop immediately on failure,
6. keep ordinary DB tests separate from schema-compat and migration batches.

This becomes the canonical path for:

- `integration`,
- `schema-compat`,
- `migration-smoke`,
- the DB-backed portion of `all`.

### D. New Remote Test DB Reprovision Script

Add a script, for example:

- `scripts/dev/reset_remote_test_dbs.sh`

Responsibilities:

1. connect from the private runner to the DB VM admin path,
2. recreate `qpi_test` and `qpi_test_scratch`,
3. apply current schema using `psqldef`,
4. optionally apply runtime compatibility first when needed,
5. verify key tables exist before returning success,
6. terminate lingering sessions when required.

This becomes the canonical full-suite reprovision path.

### E. Clarify Local Reset Script Scope

Keep the local reset script, but narrow its contract:

- it is for local/manual workflows only,
- it may require `TEST_DATABASE_ADMIN_URL`,
- it is not the primary path for the full DB suite,
- it is not the CI/deploy gate path.

Update wording so operators do not assume workstation tunnel access is the normal execution path.

### F. Tighten Test Classification

Separate DB-backed files into explicit groups:

1. ordinary app integration,
2. schema compatibility,
3. migration smoke.

Current implication:

- `tests/test_runtime_schema_compatibility.py` should move out of the ordinary integration batch,
- `tests/test_migrations.py` remains isolated in migration smoke or its own destructive schema batch,
- ordinary DB tests must be independently runnable in any manifest order.

### G. Remove Cross-File Deadlock Risk

Mitigations:

1. file-level DB recreation by default,
2. terminate leftover test backends before each file,
3. do not run DB-backed files in parallel against the same DB,
4. reserve concurrency for non-DB suites only.

### H. Tighten Function Deploy Wrapper

Extend the current direct deploy wrapper to:

1. include the bundler script in the cache hash,
2. emit built archive size before create,
3. print the exact version id created for each function,
4. replay live runtime config explicitly,
5. compare created version config back to expected critical fields and fail on drift,
6. check the intended YC folder explicitly before mutation.

Hard-fail drift set:

- runtime,
- entrypoint,
- memory,
- timeout,
- service account,
- environment,
- connectivity / network,
- log group,
- concurrency.

Warn-only drift set:

- description,
- tags that do not affect routing or behavior,
- other informational metadata.

### I. Tighten Runtime Deploy Wrapper

Add to runtime deploy:

1. explicit preflight:
   - correct YC folder,
   - reachable target VM,
   - expected host identity,
   - sufficient free disk space in releases path,
   - current service health status,
2. explicit decision on schema apply,
3. release/build metadata output,
4. explicit postflight:
   - current symlink target,
   - health endpoint payload,
   - seller `/start`,
   - buyer `/start`,
   - release id persisted to a release file or log.

Policy:

- if the bot is already unhealthy before rollout, deploy aborts by default,
- an explicit recovery override may allow deploy-through-incident behavior when intentionally requested.

### J. Workflow Concurrency And Safety

Add explicit GitHub concurrency controls so private work does not overlap unsafely.

Recommended rules:

- only one private DB-backed validation workflow per environment at a time,
- only one runtime deploy at a time,
- only one function deploy workflow touching the same target set at a time,
- bootstrap/start jobs should not race to start the same runner VM.

## Documentation Gaps To Fix

### AGENTS.md

Add and/or clarify:

1. full DB-backed tests run on the dedicated private self-hosted runner, not over the workstation tunnel by default,
2. `qpi_test` and `qpi_test_scratch` are disposable and recreated from scratch,
3. the DB VM admin path is the supported way to rebuild test DBs,
4. the runner VM is separate from the bot VM,
5. the runner VM is preemptible and normally powered off,
6. workflows start the runner on demand and a weekly keepalive boot exists,
7. direct runtime/function deploys remain the default for code-only changes.

### README.md

Keep it shorter than `AGENTS.md`, but add:

1. quick local setup,
2. fast tests locally or on GitHub-hosted runners,
3. DB-backed tests on the private runner,
4. direct deploy commands,
5. runner startup model at a high level,
6. when not to use Terraform apply.

### docs/dev_workflow.md

Create or expand a detailed doc focused on:

1. architecture of the test pipeline,
2. why DB-backed tests run on the private runner,
3. runner start / wait / stop lifecycle,
4. exact reset and execution commands,
5. common failure modes,
6. how to recover from stuck DB sessions,
7. how to verify live runtime/function deploys,
8. what the weekly keepalive does.

## Proposed Implementation Plan

### Phase 1. Introduce The On-Demand Private Runner

1. Provision a dedicated preemptible runner VM.
2. Install and register the GitHub self-hosted runner service on that VM.
3. Ensure the runner comes online automatically on boot.
4. Add bootstrap workflow logic to start the VM and wait for the runner.
5. Add cleanup logic to stop the VM after an idle window.
6. Add a weekly keepalive workflow.

Acceptance criteria:

- one bootstrap job can start the runner and observe it online,
- one cleanup path powers it down after work,
- the runner remains registered and observable week to week.

### Phase 2. Stabilize DB-Backed Test Execution

1. Add `scripts/dev/reset_remote_test_dbs.sh`.
2. Add `scripts/dev/run_db_tests_on_runner.sh`.
3. Add explicit DB suite manifests.
4. Make `scripts/dev/test.sh integration` delegate to the private-runner path when invoked from CI/deploy contexts.
5. Keep local/manual overrides only for advanced/manual workflows.

Acceptance criteria:

- one command runs the full DB-backed suite on the private runner,
- one command runs migration smoke on the private runner,
- no workstation SSH tunnel is needed for the standard full-suite path.

### Phase 3. Tighten Test Contracts

1. Separate schema-mutating files from ordinary DB-backed files.
2. Ensure runtime-compat and migration tests never share a DB with unrelated files.
3. Audit fixtures for hidden schema assumptions.
4. Keep per-test truncation only as an intra-file safeguard for ordinary DB tests.

Acceptance criteria:

- DB-backed files can run independently in any manifest order,
- schema-compat and migration smoke are fully isolated,
- no deadlock cleanup hacks are needed during a normal run.

### Phase 4. Move Workflow Gates To The Private Runner

1. Keep `fast` on GitHub-hosted runners.
2. Move DB-backed validation jobs in CI and deploy workflows to the private runner.
3. Require DB-backed validation before runtime deploys.
4. Require DB-backed validation before function deploys.
5. Require migration smoke when schema-affecting files changed.

Acceptance criteria:

- DB-backed deploy gates no longer rely on GitHub-hosted Postgres services,
- function deploys are no longer gated only by fast tests,
- all private-network validation uses the same operational path.

### Phase 5. Harden Runtime / Function Preflight

1. Add explicit YC folder checks to deploy wrappers.
2. Add release/build metadata output.
3. Add archive size and created version logging for functions.
4. Add post-deploy verification summaries.
5. Add unhealthy-service abort-by-default behavior for runtime deploys.

Acceptance criteria:

- failed deploys explain why immediately,
- successful deploys print enough data to confirm what changed,
- wrapper behavior is deterministic and environment-aware.

### Phase 6. Documentation Alignment

1. Update `AGENTS.md`.
2. Update `README.md`.
3. Update `docs/dev_workflow.md`.
4. Include both commands and reasons.

Acceptance criteria:

- an operator can follow the documented path without rediscovering hidden infra facts,
- no critical operational assumption lives only in human memory.

## Recommended Final Test / Deploy Strategy

This should become the official model:

1. `fast`
   - local or GitHub-hosted,
   - no private DB,
   - required on every change.

2. `db-integration`
   - dedicated on-demand preemptible private runner,
   - fresh `qpi_test` per test file,
   - required before runtime and function deploys.

3. `schema-compat`
   - dedicated on-demand preemptible private runner,
   - isolated fresh DBs,
   - required when compatibility-sensitive code changes.

4. `migration-smoke`
   - dedicated on-demand preemptible private runner,
   - fresh `qpi_test_scratch`,
   - required for schema-affecting changes.

5. `deploy-runtime`
   - private runner starts on demand,
   - direct bot VM rollout wrapper,
   - health + `/start` smoke,
   - then runner shuts down.

6. `deploy-functions`
   - private runner starts on demand,
   - direct YC function wrapper,
   - one version publish per changed function,
   - then runner shuts down.

7. `weekly-keepalive`
   - GitHub-hosted bootstrap starts the runner,
   - runner comes online,
   - health is checked,
   - runner powers off again.

## Recommended `qpi_test` Policy

Yes: the default should be to recreate the test DB from scratch from the current repo schema.

Recommended rule:

- never trust an old `qpi_test`,
- never assume a partially used shared DB is still valid,
- rebuild from current schema before DB-backed runs.

Specifically:

1. recreate `qpi_test`,
2. apply current repo schema,
3. run one ordinary DB-backed test file,
4. discard / rebuild again before the next file.

For schema compatibility:

1. recreate a fresh disposable DB,
2. apply current repo schema or intentional pre-compat shape as required,
3. run one schema-mutating file or tightly scoped batch,
4. discard.

For migration smoke:

1. recreate `qpi_test_scratch`,
2. apply current repo schema,
3. run destructive migration file,
4. discard.

This is the right baseline because it guarantees:

- alignment with current repo schema,
- deterministic test start state,
- no dependence on previous failed runs.

## Optional Later Improvements

If full file-by-file recreation becomes too slow:

- create a prebuilt template DB after schema apply,
- clone `qpi_test` / `qpi_test_scratch` from that template per file.

If the runner start latency becomes a problem:

- shorten boot path,
- warm critical caches,
- tune the idle timeout rather than keeping the VM always on.

If preemptible interruptions become too noisy:

- keep the same workflow shape and labels,
- move the private runner to a non-preemptible VM without redesigning the pipeline.

## Success Criteria

This effort is complete when:

1. Full DB-backed suite is one supported command through the private runner.
2. Schema-compat and migration smoke are isolated and each are one supported command.
3. DB-backed validation runs inside the private network, not through the workstation tunnel or GitHub-hosted Postgres by default.
4. The private runner VM starts on demand and shuts down automatically.
5. A weekly keepalive start confirms the runner still boots and registers.
6. Runtime deploy is one supported command with health + smoke verification.
7. Function deploy is one supported command per function and does not require Terraform apply.
8. AGENTS/README/docs fully describe the supported path and the reasons behind it.
