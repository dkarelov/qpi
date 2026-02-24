"""Initial Phase 2 schema

Revision ID: 20260223_0001
Revises:
Create Date: 2026-02-23 02:00:00
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260223_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE users (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL UNIQUE,
            username TEXT NULL,
            role TEXT NOT NULL CHECK (role IN ('seller', 'buyer', 'admin')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now()),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
        )
        """
    )

    op.execute(
        """
        CREATE TABLE shops (
            id BIGSERIAL PRIMARY KEY,
            seller_user_id BIGINT NOT NULL REFERENCES users(id),
            slug TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            wb_token_ciphertext TEXT NOT NULL,
            wb_token_status TEXT NOT NULL DEFAULT 'unknown'
                CHECK (wb_token_status IN ('unknown', 'valid', 'invalid', 'expired')),
            wb_token_last_validated_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now()),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
        )
        """
    )
    op.execute("CREATE INDEX idx_shops_seller_user_id ON shops (seller_user_id)")

    op.execute(
        """
        CREATE TABLE listings (
            id BIGSERIAL PRIMARY KEY,
            shop_id BIGINT NOT NULL REFERENCES shops(id),
            seller_user_id BIGINT NOT NULL REFERENCES users(id),
            wb_product_id BIGINT NOT NULL,
            discount_percent SMALLINT NOT NULL CHECK (discount_percent BETWEEN 10 AND 100),
            reward_usdt NUMERIC(20, 6) NOT NULL CHECK (reward_usdt > 0),
            slot_count INTEGER NOT NULL CHECK (slot_count > 0),
            available_slots INTEGER NOT NULL CHECK (available_slots >= 0),
            collateral_required_usdt NUMERIC(20, 6) NOT NULL CHECK (collateral_required_usdt >= 0),
            status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'paused')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now()),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now()),
            CHECK (available_slots <= slot_count)
        )
        """
    )
    op.execute("CREATE INDEX idx_listings_shop_status ON listings (shop_id, status)")
    op.execute("CREATE INDEX idx_listings_seller_status ON listings (seller_user_id, status)")

    op.execute(
        """
        CREATE TABLE assignments (
            id BIGSERIAL PRIMARY KEY,
            listing_id BIGINT NOT NULL REFERENCES listings(id),
            buyer_user_id BIGINT NOT NULL REFERENCES users(id),
            status TEXT NOT NULL CHECK (
                status IN (
                    'reserved',
                    'order_submitted',
                    'order_verified',
                    'picked_up_wait_unlock',
                    'eligible_for_withdrawal',
                    'withdraw_pending_admin',
                    'withdraw_sent',
                    'expired_2h',
                    'wb_invalid',
                    'returned_within_14d'
                )
            ),
            reward_usdt NUMERIC(20, 6) NOT NULL CHECK (reward_usdt > 0),
            reservation_expires_at TIMESTAMPTZ NOT NULL,
            order_id TEXT NULL,
            order_submitted_at TIMESTAMPTZ NULL,
            pickup_at TIMESTAMPTZ NULL,
            unlock_at TIMESTAMPTZ NULL,
            returned_at TIMESTAMPTZ NULL,
            cancel_reason TEXT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now()),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_assignments_order_id
        ON assignments (order_id)
        WHERE order_id IS NOT NULL
        """
    )
    op.execute("CREATE INDEX idx_assignments_listing_status ON assignments (listing_id, status)")
    op.execute("CREATE INDEX idx_assignments_buyer_status ON assignments (buyer_user_id, status)")

    op.execute(
        """
        CREATE TABLE accounts (
            id BIGSERIAL PRIMARY KEY,
            owner_user_id BIGINT NULL REFERENCES users(id),
            account_code TEXT NOT NULL UNIQUE,
            account_kind TEXT NOT NULL CHECK (
                account_kind IN (
                    'seller_available',
                    'seller_collateral',
                    'buyer_available',
                    'buyer_withdraw_pending',
                    'reward_reserved',
                    'system_payout'
                )
            ),
            currency TEXT NOT NULL DEFAULT 'USDT' CHECK (currency = 'USDT'),
            current_balance_usdt NUMERIC(20, 6) NOT NULL DEFAULT 0
                CHECK (current_balance_usdt >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now()),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
        )
        """
    )
    op.execute("CREATE INDEX idx_accounts_owner_user_id ON accounts (owner_user_id)")

    op.execute(
        """
        CREATE TABLE ledger_entries (
            id BIGSERIAL PRIMARY KEY,
            event_type TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            entity_type TEXT NULL,
            entity_id BIGINT NULL,
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
        )
        """
    )

    op.execute(
        """
        CREATE TABLE ledger_postings (
            id BIGSERIAL PRIMARY KEY,
            entry_id BIGINT NOT NULL REFERENCES ledger_entries(id) ON DELETE CASCADE,
            account_id BIGINT NOT NULL REFERENCES accounts(id),
            direction TEXT NOT NULL CHECK (direction IN ('debit', 'credit')),
            amount_usdt NUMERIC(20, 6) NOT NULL CHECK (amount_usdt > 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
        )
        """
    )
    op.execute("CREATE INDEX idx_ledger_postings_entry_id ON ledger_postings (entry_id)")
    op.execute("CREATE INDEX idx_ledger_postings_account_id ON ledger_postings (account_id)")

    op.execute(
        """
        CREATE TABLE withdrawal_requests (
            id BIGSERIAL PRIMARY KEY,
            buyer_user_id BIGINT NOT NULL REFERENCES users(id),
            from_account_id BIGINT NOT NULL REFERENCES accounts(id),
            to_account_id BIGINT NOT NULL REFERENCES accounts(id),
            amount_usdt NUMERIC(20, 6) NOT NULL CHECK (amount_usdt > 0),
            status TEXT NOT NULL CHECK (
                status IN ('withdraw_pending_admin', 'approved', 'rejected', 'withdraw_sent')
            ),
            payout_address TEXT NOT NULL,
            admin_user_id BIGINT NULL REFERENCES users(id),
            requested_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now()),
            processed_at TIMESTAMPTZ NULL,
            sent_at TIMESTAMPTZ NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            note TEXT NULL
        )
        """
    )
    op.execute("CREATE INDEX idx_withdrawal_requests_status ON withdrawal_requests (status)")

    op.execute(
        """
        CREATE TABLE payouts (
            id BIGSERIAL PRIMARY KEY,
            withdrawal_request_id BIGINT NOT NULL UNIQUE REFERENCES withdrawal_requests(id),
            tx_hash TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK (status IN ('created', 'sent', 'failed')),
            error_message TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now()),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
        )
        """
    )

    op.execute(
        """
        CREATE TABLE balance_holds (
            id BIGSERIAL PRIMARY KEY,
            account_id BIGINT NOT NULL REFERENCES accounts(id),
            hold_type TEXT NOT NULL
                CHECK (hold_type IN ('collateral', 'slot_reserve', 'withdrawal')),
            status TEXT NOT NULL CHECK (status IN ('active', 'released', 'consumed')),
            amount_usdt NUMERIC(20, 6) NOT NULL CHECK (amount_usdt > 0),
            listing_id BIGINT NULL REFERENCES listings(id),
            assignment_id BIGINT NULL REFERENCES assignments(id),
            withdrawal_request_id BIGINT NULL REFERENCES withdrawal_requests(id),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now()),
            released_at TIMESTAMPTZ NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_balance_holds_active_account
        ON balance_holds (account_id)
        WHERE status = 'active'
        """
    )

    op.execute(
        """
        CREATE TABLE admin_audit_actions (
            id BIGSERIAL PRIMARY KEY,
            admin_user_id BIGINT NOT NULL REFERENCES users(id),
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key TEXT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_admin_audit_actions_admin_user_id
        ON admin_audit_actions (admin_user_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS admin_audit_actions")
    op.execute("DROP TABLE IF EXISTS balance_holds")
    op.execute("DROP TABLE IF EXISTS payouts")
    op.execute("DROP TABLE IF EXISTS withdrawal_requests")
    op.execute("DROP TABLE IF EXISTS ledger_postings")
    op.execute("DROP TABLE IF EXISTS ledger_entries")
    op.execute("DROP TABLE IF EXISTS accounts")
    op.execute("DROP TABLE IF EXISTS assignments")
    op.execute("DROP TABLE IF EXISTS listings")
    op.execute("DROP TABLE IF EXISTS shops")
    op.execute("DROP TABLE IF EXISTS users")
