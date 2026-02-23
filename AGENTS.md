# QPI AGENTS

Last updated: 2026-02-23 UTC

## 1. Purpose and Maintenance Rules

This file is the operational source of truth for architecture decisions, infrastructure state, constraints, and runbooks.

Maintenance policy:

- Keep exactly two project docs:
  - `AGENTS.md`: decisions, constraints, deployed state, runbooks.
  - `PLAN.md`: detailed requirements baseline, phased implementation plan, and phase status.
- Update `AGENTS.md` and `PLAN.md` together when decisions, requirements, or execution status changes.
- Keep both files internally consistent with Terraform code and deployed infrastructure.

## 2. Product Scope (MVP)

Goal:

- Build a minimal Python-based Telegram marketplace bot where WB sellers fund buyer rewards in USDT for honest review flow completion.

In-scope:

- One Telegram bot (PTB) with role-based flows (seller, buyer, admin).
- Russian-only UX.
- No Telegram Mini App (anonymity requirement).
- WB integration for product ownership/order/pickup/return checks.
- USDT ledger and payouts (TON ecosystem), with manual ops for MVP.

Out-of-scope (MVP):

- Dispute handling.
- Automated on-chain deposit reconciliation.
- Advanced wallet security (multisig/HSM).

Detailed baseline requirements and phase-by-phase execution plan are tracked in `PLAN.md`.

## 3. Confirmed Product Decisions

### 3.1 Roles and entry points

- Single bot with role-based behavior (chosen for simplicity and reliability).

### 3.2 Listing and collateral

- Seller creates listings for full WB products (not samples).
- Seller sets discount from 10% to 100%.
- Seller must provide full collateral for all `N` slots before listing activation.
- Rewards are reserved per buyer/slot after accept.

### 3.3 Buyer assignment rules

- `1 order_id = 1 slot`.
- `order_id` must belong to listing `product_id`.
- Reservation timeout: 2 hours.
- Unlock timer starts from WB pickup timestamp.
- Unlock period: 14 days after pickup.
- If returned within 14 days: cancel reward.
- After 14 days: do not cancel for return (per WB policy assumption).

### 3.4 Finance flow (MVP)

- Deposits: manual credit by admin.
- Withdrawals: buyer requests -> admin approval required -> payout.
- If fee policy changes, user should be notified.

### 3.5 Display and localization

- Money display format: `~350 руб. (4.55 USDT)`.
- Primary ledger currency: USDT.

### 3.6 Operations and moderation

- Minimal admin control panel is acceptable.
- Logging quality must be high (Yandex Logging).
- Sensitive inputs in chat should be deleted after parsing with user notice.

## 4. Functional Workflow Summary

Seller flow:

1. Register in bot.
2. Create shop and submit WB read-only token.
3. Create listing(s) with WB product binding, discount, reward, slots.
4. Fund collateral.
5. Share shop deep link in Telegram channels.

Buyer flow:

1. Open shop via deep link.
2. Accept available slot (funds reserved).
3. Submit `order_id` within 2 hours.
4. Bot verifies order and tracks pickup/return.
5. After 14 days from pickup with no cancellation condition, reward becomes withdrawable.
6. Buyer requests withdrawal; admin approves; payout sent.

Automation checkpoints:

- Reservation timeout.
- Order verification.
- Pickup detection.
- 14-day unlock check.
- Return detection (within 14 days only).

## 5. State Model (Assignment)

Core states:

- `reserved`
- `order_submitted`
- `order_verified`
- `picked_up_wait_unlock`
- `eligible_for_withdrawal`
- `withdraw_pending_admin`
- `withdraw_sent`

Cancel/failure states:

- `expired_2h`
- `wb_invalid`
- `returned_within_14d`

## 6. Technical and Platform Constraints

- All services in Python.
- Infrastructure changes via Terraform only (avoid drift).
- YC CLI allowed for checks/debugging only.
- OS Login must be enabled and used (`yc compute ssh`).
- Target initial load: ~100 concurrent users.
- Deployment mode: Telegram webhook.
- Zone: `ru-central1-d`.
- Domain is not required for now (IP is acceptable for current stage).

## 7. Infrastructure Architecture (Current)

Folder:

- YC folder ID: `b1gmeblqlrrvm912n1uq`.

Compute:

- Bot runtime: instance group, size 1, preemptible VM, auto-heal.
- Bot VM shape: 2 vCPU, 2 GB RAM, 20 GB network-SSD.
- DB VM: non-preemptible, 2 vCPU, 4 GB RAM, 40 GB network-SSD.
- PostgreSQL target version: 18+ (current bootstrap path installs 18).

Network:

- Bot remains on default subnet with static public IP for webhook.
- DB moved to dedicated private subnet.
- Private subnet egress is via NAT gateway and route table.

Logging and access:

- Yandex Logging group enabled.
- OS Login enabled on VMs.

## 8. Deployed Resource Snapshot (As Implemented)

- Bot instance group: `cl17ilrmf3ukgtg14gbe` (`qpi-bot-ig`)
- Bot public IP: `158.160.187.114`
- DB instance: `fv4ii9h4960ot6g5ei29` (`qpi-db`)
- DB private IP: `10.131.0.9`
- DB public IP: none
- NAT gateway: `enpkq1bnf0ij8jcjmf7s` (`qpi-nat-gw`)
- Private route table: `enpmdt4gs3gav0qd4nce` (`qpi-rt-private`)
- Private subnet: `fl8oled9cdd9u2efqaae` (`qpi-private-ru-central1-d`, `10.131.0.0/24`)
- Logging group: `e2345psnoc0appog5lil` (`qpi-prod-logs`)

Note:

- Run `terraform -chdir=infra output` for latest runtime values before operational actions.

## 9. Cost Notes (Reference, February 23, 2026)

From YC public price API used during planning:

- NAT gateway (`vpc.gateway.shared_egress_gateway.v1`): `0.39528 RUB/hour`.
- NAT egress surcharge SKU (`network.egress.nat`): `0 RUB/GB`.
- Public IP (`network.public_fips`): `0.26352 RUB/hour`.

Approximation at 730 h/month:

- NAT gateway: ~288.55 RUB/month.
- One public IP: ~192.37 RUB/month.

Interpretation:

- For a single host, NAT can be slightly more expensive than a single public IP.
- For multiple private hosts, NAT usually improves security posture and can become cost-efficient.

## 10. Terraform Runbook

Working directory:

- `infra/`

Common commands:

```bash
terraform -chdir=infra init
terraform -chdir=infra fmt
terraform -chdir=infra validate
YC_TOKEN="$(yc config get token)" terraform -chdir=infra plan
YC_TOKEN="$(yc config get token)" terraform -chdir=infra apply
terraform -chdir=infra output
```

Access:

```bash
yc compute instance-group list-instances --name qpi-bot-ig --folder-id b1gmeblqlrrvm912n1uq
yc compute ssh --name <bot-instance-name> --folder-id b1gmeblqlrrvm912n1uq
yc compute ssh --name qpi-db --folder-id b1gmeblqlrrvm912n1uq
```

## 11. Security and Risk Notes (MVP)

Accepted temporary risks:

- Hot wallet with one key.
- Broad SSH allowlist (`0.0.0.0/0`) for now.
- Manual finance operations.

Required controls even in MVP:

- Immutable ledger/audit records for balance-changing operations.
- Admin action audit trail (who/what/when).
- Sensitive input message cleanup in Telegram chats.

## 12. Open Items / Pending Inputs

- Production handling policy for secrets (wallet key/token lifecycle, rotation cadence).
- Final payout integration details and transaction broadcast implementation.
- Tightening SSH ingress from `0.0.0.0/0` to operator CIDRs before production launch.
- Optional domain/TLS strategy if webhook setup is hardened later.

## 13. Change Log

- 2026-02-23: Initial Terraform baseline deployed (bot IG, DB VM, SGs, logging, static IP).
- 2026-02-23: DB moved to private-only subnet, NAT gateway + route table added.
- 2026-02-23: Documentation consolidated into this single `AGENTS.md` file.
- 2026-02-23: Added `PLAN.md` and split documentation responsibilities between `AGENTS.md` and `PLAN.md`.
