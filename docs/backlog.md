# QPI Backlog

Open items and deliberately deferred improvements. Current behavior and active
decisions live in `docs/product/requirements.md` and `AGENTS.md`.

## Open Items

- Final production payout broadcasting integration and key lifecycle policy.
- Tighten SSH ingress to operator CIDRs.
- Optional migration from self-signed IP TLS to domain-managed trusted TLS.
- Payload integrity/signature scheme for extension tokens (post-MVP).
- Post-MVP WB correction operation semantics (`Коррекция продаж`, `Коррекция возвратов`).
- Optional extraction of CF services into separate repositories.
- Terraform remote backend strategy for safe CI-driven apply.
- Replace app-level token cipher with managed secret/KMS-backed mechanism.

## Potential Improvements (Deliberately Deferred for MVP)

- Token-at-rest cryptography hardening:
  - replace current app-level reversible token cipher with authenticated encryption + managed KMS/HSM-backed key lifecycle.
  - status: intentionally deferred; current implementation is accepted as an MVP tradeoff.

- Runtime secret strictness hardening:
  - remove insecure default fallbacks for sensitive settings (e.g. cipher/webhook secrets) and fail-fast on unsafe values outside local dev.
  - status: intentionally deferred; current defaults are accepted for MVP-only environments.
