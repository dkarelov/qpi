CREATE TABLE "public"."accounts" (
    "id" bigserial NOT NULL,
    "owner_user_id" bigint,
    "account_code" text NOT NULL,
    "account_kind" text NOT NULL CONSTRAINT accounts_account_kind_check CHECK (account_kind = ANY (ARRAY['seller_available'::text, 'seller_collateral'::text, 'buyer_available'::text, 'buyer_withdraw_pending'::text, 'reward_reserved'::text, 'system_payout'::text])),
    "currency" text NOT NULL DEFAULT 'USDT'::text CONSTRAINT accounts_currency_check CHECK (currency = 'USDT'::text),
    "current_balance_usdt" numeric(20,6) NOT NULL DEFAULT 0 CONSTRAINT accounts_current_balance_usdt_check CHECK (current_balance_usdt >= 0::numeric),
    "created_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    "updated_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    CONSTRAINT accounts_pkey PRIMARY KEY ("id")
);

CREATE INDEX idx_accounts_owner_user_id ON public.accounts USING btree (owner_user_id);

ALTER TABLE ONLY "public"."accounts" ADD CONSTRAINT "accounts_owner_user_id_fkey" FOREIGN KEY ("owner_user_id") REFERENCES "public"."users" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE "public"."accounts" ADD CONSTRAINT "accounts_account_code_key" UNIQUE (account_code);

CREATE TABLE "public"."admin_audit_actions" (
    "id" bigserial NOT NULL,
    "admin_user_id" bigint NOT NULL,
    "action" text NOT NULL,
    "target_type" text NOT NULL,
    "target_id" text NOT NULL,
    "payload_json" jsonb NOT NULL DEFAULT '{}'::jsonb,
    "idempotency_key" text,
    "created_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    CONSTRAINT admin_audit_actions_pkey PRIMARY KEY ("id")
);

CREATE INDEX idx_admin_audit_actions_admin_user_id ON public.admin_audit_actions USING btree (admin_user_id);

ALTER TABLE ONLY "public"."admin_audit_actions" ADD CONSTRAINT "admin_audit_actions_admin_user_id_fkey" FOREIGN KEY ("admin_user_id") REFERENCES "public"."users" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE "public"."admin_audit_actions" ADD CONSTRAINT "admin_audit_actions_idempotency_key_key" UNIQUE (idempotency_key);

CREATE TABLE "public"."assignments" (
    "id" bigserial NOT NULL,
    "listing_id" bigint NOT NULL,
    "buyer_user_id" bigint NOT NULL,
    "wb_product_id" bigint NOT NULL,
    "status" text NOT NULL CONSTRAINT assignments_status_check CHECK (status = ANY (ARRAY['reserved'::text, 'order_submitted'::text, 'order_verified'::text, 'picked_up_wait_unlock'::text, 'eligible_for_withdrawal'::text, 'withdraw_pending_admin'::text, 'withdraw_sent'::text, 'expired_2h'::text, 'wb_invalid'::text, 'returned_within_14d'::text, 'delivery_expired'::text])),
    "reward_usdt" numeric(20,6) NOT NULL CONSTRAINT assignments_reward_usdt_check CHECK (reward_usdt > 0::numeric),
    "reservation_expires_at" timestamp with time zone NOT NULL,
    "order_id" text,
    "order_submitted_at" timestamp with time zone,
    "pickup_at" timestamp with time zone,
    "unlock_at" timestamp with time zone,
    "returned_at" timestamp with time zone,
    "cancel_reason" text,
    "idempotency_key" text NOT NULL,
    "created_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    "updated_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    CONSTRAINT assignments_pkey PRIMARY KEY ("id")
);

CREATE INDEX idx_assignments_buyer_status ON public.assignments USING btree (buyer_user_id, status);

CREATE INDEX idx_assignments_listing_status ON public.assignments USING btree (listing_id, status);

CREATE INDEX idx_assignments_buyer_product_status ON public.assignments USING btree (buyer_user_id, wb_product_id, status);

CREATE INDEX idx_assignments_reserved_expires_at ON public.assignments USING btree (reservation_expires_at) WHERE (status = 'reserved'::text);

CREATE INDEX idx_assignments_order_tracking_order_id ON public.assignments USING btree (order_id) WHERE (status = ANY (ARRAY['order_verified'::text, 'picked_up_wait_unlock'::text]));

CREATE INDEX idx_assignments_unlock_due ON public.assignments USING btree (unlock_at) WHERE (status = 'picked_up_wait_unlock'::text);

CREATE UNIQUE INDEX uq_assignments_order_id ON public.assignments USING btree (order_id) WHERE (order_id IS NOT NULL);

CREATE UNIQUE INDEX uq_assignments_buyer_product_active ON public.assignments USING btree (buyer_user_id, wb_product_id) WHERE (status = ANY (ARRAY['reserved'::text, 'order_submitted'::text, 'order_verified'::text, 'picked_up_wait_unlock'::text, 'eligible_for_withdrawal'::text, 'withdraw_pending_admin'::text, 'withdraw_sent'::text]));

ALTER TABLE ONLY "public"."assignments" ADD CONSTRAINT "assignments_buyer_user_id_fkey" FOREIGN KEY ("buyer_user_id") REFERENCES "public"."users" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."assignments" ADD CONSTRAINT "assignments_listing_id_fkey" FOREIGN KEY ("listing_id") REFERENCES "public"."listings" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE "public"."assignments" ADD CONSTRAINT "assignments_idempotency_key_key" UNIQUE (idempotency_key);

CREATE TABLE "public"."buyer_orders" (
    "id" bigserial NOT NULL,
    "assignment_id" bigint NOT NULL,
    "listing_id" bigint NOT NULL,
    "buyer_user_id" bigint NOT NULL,
    "order_id" text NOT NULL,
    "wb_product_id" bigint NOT NULL,
    "ordered_at" timestamp with time zone NOT NULL,
    "payload_version" integer NOT NULL,
    "raw_payload_json" jsonb NOT NULL DEFAULT '{}'::jsonb,
    "source" text NOT NULL DEFAULT 'plugin_base64'::text,
    "created_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    "updated_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    CONSTRAINT buyer_orders_pkey PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX uq_buyer_orders_order_id ON public.buyer_orders USING btree (order_id);

CREATE UNIQUE INDEX uq_buyer_orders_assignment_id ON public.buyer_orders USING btree (assignment_id);

CREATE INDEX idx_buyer_orders_listing_id ON public.buyer_orders USING btree (listing_id);

CREATE INDEX idx_buyer_orders_buyer_user_id ON public.buyer_orders USING btree (buyer_user_id);

ALTER TABLE ONLY "public"."buyer_orders" ADD CONSTRAINT "buyer_orders_assignment_id_fkey" FOREIGN KEY ("assignment_id") REFERENCES "public"."assignments" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."buyer_orders" ADD CONSTRAINT "buyer_orders_buyer_user_id_fkey" FOREIGN KEY ("buyer_user_id") REFERENCES "public"."users" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."buyer_orders" ADD CONSTRAINT "buyer_orders_listing_id_fkey" FOREIGN KEY ("listing_id") REFERENCES "public"."listings" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

CREATE TABLE "public"."buyer_saved_shops" (
    "id" bigserial NOT NULL,
    "buyer_user_id" bigint NOT NULL,
    "shop_id" bigint NOT NULL,
    "last_opened_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    "created_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    "updated_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    CONSTRAINT buyer_saved_shops_pkey PRIMARY KEY ("id")
);

CREATE INDEX idx_buyer_saved_shops_buyer_last_opened ON public.buyer_saved_shops USING btree (buyer_user_id, last_opened_at DESC);

ALTER TABLE ONLY "public"."buyer_saved_shops" ADD CONSTRAINT "buyer_saved_shops_buyer_user_id_fkey" FOREIGN KEY ("buyer_user_id") REFERENCES "public"."users" ("id") ON UPDATE NO ACTION ON DELETE CASCADE;

ALTER TABLE ONLY "public"."buyer_saved_shops" ADD CONSTRAINT "buyer_saved_shops_shop_id_fkey" FOREIGN KEY ("shop_id") REFERENCES "public"."shops" ("id") ON UPDATE NO ACTION ON DELETE CASCADE;

ALTER TABLE "public"."buyer_saved_shops" ADD CONSTRAINT "buyer_saved_shops_buyer_user_id_shop_id_key" UNIQUE (buyer_user_id, shop_id);

CREATE TABLE "public"."fx_rates" (
    "pair_code" text NOT NULL,
    "rate" numeric(20,6) NOT NULL CONSTRAINT fx_rates_rate_check CHECK (rate > 0::numeric),
    "source" text NOT NULL,
    "fetched_at" timestamp with time zone NOT NULL,
    "created_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    "updated_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    CONSTRAINT fx_rates_pkey PRIMARY KEY ("pair_code")
);

CREATE TABLE "public"."wb_report_rows" (
    "realizationreport_id" bigint,
    "create_dt" timestamp with time zone,
    "currency_name" text,
    "rrd_id" bigint NOT NULL,
    "subject_name" text,
    "nm_id" bigint,
    "brand_name" text,
    "sa_name" text,
    "ts_name" text,
    "quantity" integer,
    "retail_amount" numeric(20,6),
    "office_name" text,
    "supplier_oper_name" text,
    "order_dt" timestamp with time zone,
    "sale_dt" timestamp with time zone,
    "delivery_amount" integer,
    "return_amount" integer,
    "supplier_promo" numeric(20,6),
    "ppvz_office_name" text,
    "ppvz_office_id" bigint,
    "sticker_id" text,
    "site_country" text,
    "assembly_id" bigint,
    "wb_srid" text NOT NULL,
    "order_uid" text,
    "delivery_method" text,
    "uuid_promocode" text,
    "sale_price_promocode_discount_prc" numeric(20,6),
    CONSTRAINT wb_report_rows_pkey PRIMARY KEY ("rrd_id", "wb_srid")
);

CREATE INDEX idx_wb_report_rows_srid ON public.wb_report_rows USING btree (wb_srid);

CREATE INDEX idx_wb_report_rows_sale_dt ON public.wb_report_rows USING btree (sale_dt);

CREATE INDEX idx_wb_report_rows_order_dt ON public.wb_report_rows USING btree (order_dt);

CREATE TABLE "public"."balance_holds" (
    "id" bigserial NOT NULL,
    "account_id" bigint NOT NULL,
    "hold_type" text NOT NULL CONSTRAINT balance_holds_hold_type_check CHECK (hold_type = ANY (ARRAY['collateral'::text, 'slot_reserve'::text, 'withdrawal'::text])),
    "status" text NOT NULL CONSTRAINT balance_holds_status_check CHECK (status = ANY (ARRAY['active'::text, 'released'::text, 'consumed'::text])),
    "amount_usdt" numeric(20,6) NOT NULL CONSTRAINT balance_holds_amount_usdt_check CHECK (amount_usdt > 0::numeric),
    "listing_id" bigint,
    "assignment_id" bigint,
    "withdrawal_request_id" bigint,
    "idempotency_key" text NOT NULL,
    "created_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    "released_at" timestamp with time zone,
    CONSTRAINT balance_holds_pkey PRIMARY KEY ("id")
);

CREATE INDEX idx_balance_holds_active_account ON public.balance_holds USING btree (account_id) WHERE (status = 'active'::text);

ALTER TABLE ONLY "public"."balance_holds" ADD CONSTRAINT "balance_holds_account_id_fkey" FOREIGN KEY ("account_id") REFERENCES "public"."accounts" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."balance_holds" ADD CONSTRAINT "balance_holds_assignment_id_fkey" FOREIGN KEY ("assignment_id") REFERENCES "public"."assignments" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."balance_holds" ADD CONSTRAINT "balance_holds_listing_id_fkey" FOREIGN KEY ("listing_id") REFERENCES "public"."listings" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."balance_holds" ADD CONSTRAINT "balance_holds_withdrawal_request_id_fkey" FOREIGN KEY ("withdrawal_request_id") REFERENCES "public"."withdrawal_requests" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE "public"."balance_holds" ADD CONSTRAINT "balance_holds_idempotency_key_key" UNIQUE (idempotency_key);

CREATE TABLE "public"."ledger_entries" (
    "id" bigserial NOT NULL,
    "event_type" text NOT NULL,
    "idempotency_key" text NOT NULL,
    "entity_type" text,
    "entity_id" bigint,
    "metadata_json" jsonb NOT NULL DEFAULT '{}'::jsonb,
    "created_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    CONSTRAINT ledger_entries_pkey PRIMARY KEY ("id")
);

ALTER TABLE "public"."ledger_entries" ADD CONSTRAINT "ledger_entries_idempotency_key_key" UNIQUE (idempotency_key);

CREATE TABLE "public"."ledger_postings" (
    "id" bigserial NOT NULL,
    "entry_id" bigint NOT NULL,
    "account_id" bigint NOT NULL,
    "direction" text NOT NULL CONSTRAINT ledger_postings_direction_check CHECK (direction = ANY (ARRAY['debit'::text, 'credit'::text])),
    "amount_usdt" numeric(20,6) NOT NULL CONSTRAINT ledger_postings_amount_usdt_check CHECK (amount_usdt > 0::numeric),
    "created_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    CONSTRAINT ledger_postings_pkey PRIMARY KEY ("id")
);

CREATE INDEX idx_ledger_postings_account_id ON public.ledger_postings USING btree (account_id);

CREATE INDEX idx_ledger_postings_entry_id ON public.ledger_postings USING btree (entry_id);

ALTER TABLE ONLY "public"."ledger_postings" ADD CONSTRAINT "ledger_postings_account_id_fkey" FOREIGN KEY ("account_id") REFERENCES "public"."accounts" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."ledger_postings" ADD CONSTRAINT "ledger_postings_entry_id_fkey" FOREIGN KEY ("entry_id") REFERENCES "public"."ledger_entries" ("id") ON UPDATE NO ACTION ON DELETE CASCADE;

CREATE TABLE "public"."listings" (
    "id" bigserial NOT NULL,
    "shop_id" bigint NOT NULL,
    "seller_user_id" bigint NOT NULL,
    "wb_product_id" bigint NOT NULL,
    "display_title" text,
    "wb_source_title" text,
    "wb_subject_name" text,
    "wb_brand_name" text,
    "wb_vendor_code" text,
    "wb_description" text,
    "wb_photo_url" text,
    "wb_tech_sizes_json" jsonb NOT NULL DEFAULT '[]'::jsonb,
    "wb_characteristics_json" jsonb NOT NULL DEFAULT '[]'::jsonb,
    "reference_price_rub" integer CONSTRAINT listings_reference_price_rub_check CHECK ((reference_price_rub IS NULL) OR (reference_price_rub > 0)),
    "reference_price_source" text CONSTRAINT listings_reference_price_source_check CHECK ((reference_price_source IS NULL) OR (reference_price_source = ANY (ARRAY['orders'::text, 'manual'::text]))),
    "reference_price_updated_at" timestamp with time zone,
    "search_phrase" text NOT NULL DEFAULT 'legacy_search_phrase'::text CONSTRAINT listings_search_phrase_check CHECK (length(btrim(search_phrase)) > 0),
    "reward_usdt" numeric(20,6) NOT NULL CONSTRAINT listings_reward_usdt_check CHECK (reward_usdt > 0::numeric),
    "slot_count" integer NOT NULL CONSTRAINT listings_slot_count_check CHECK (slot_count > 0),
    "available_slots" integer NOT NULL CONSTRAINT listings_available_slots_check CHECK (available_slots >= 0),
    "collateral_required_usdt" numeric(20,6) NOT NULL CONSTRAINT listings_collateral_required_usdt_check CHECK (collateral_required_usdt >= 0::numeric),
    "status" text NOT NULL DEFAULT 'draft'::text CONSTRAINT listings_status_check CHECK (status = ANY (ARRAY['draft'::text, 'active'::text, 'paused'::text])),
    "activated_at" timestamp with time zone,
    "paused_at" timestamp with time zone,
    "pause_reason" text,
    "pause_source" text CONSTRAINT listings_pause_source_check CHECK (pause_source = ANY (ARRAY['manual'::text, 'scrapper_401_withdrawn'::text, 'scrapper_401_token_expired'::text])),
    "deleted_at" timestamp with time zone,
    "deleted_by_user_id" bigint,
    "created_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    "updated_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    CONSTRAINT listings_pkey PRIMARY KEY ("id"),
    CONSTRAINT listings_check CHECK (available_slots <= slot_count)
);

CREATE INDEX idx_listings_seller_status ON public.listings USING btree (seller_user_id, status);

CREATE INDEX idx_listings_shop_status ON public.listings USING btree (shop_id, status);

CREATE INDEX idx_listings_seller_active ON public.listings USING btree (seller_user_id) WHERE (deleted_at IS NULL);

ALTER TABLE ONLY "public"."listings" ADD CONSTRAINT "listings_seller_user_id_fkey" FOREIGN KEY ("seller_user_id") REFERENCES "public"."users" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."listings" ADD CONSTRAINT "listings_shop_id_fkey" FOREIGN KEY ("shop_id") REFERENCES "public"."shops" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."listings" ADD CONSTRAINT "listings_deleted_by_user_id_fkey" FOREIGN KEY ("deleted_by_user_id") REFERENCES "public"."users" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

CREATE TABLE "public"."payouts" (
    "id" bigserial NOT NULL,
    "withdrawal_request_id" bigint NOT NULL,
    "tx_hash" text NOT NULL,
    "status" text NOT NULL CONSTRAINT payouts_status_check CHECK (status = ANY (ARRAY['created'::text, 'sent'::text, 'failed'::text])),
    "error_message" text,
    "created_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    "updated_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    CONSTRAINT payouts_pkey PRIMARY KEY ("id")
);

ALTER TABLE ONLY "public"."payouts" ADD CONSTRAINT "payouts_withdrawal_request_id_fkey" FOREIGN KEY ("withdrawal_request_id") REFERENCES "public"."withdrawal_requests" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE "public"."payouts" ADD CONSTRAINT "payouts_tx_hash_key" UNIQUE (tx_hash);

ALTER TABLE "public"."payouts" ADD CONSTRAINT "payouts_withdrawal_request_id_key" UNIQUE (withdrawal_request_id);

CREATE TABLE "public"."manual_deposits" (
    "id" bigserial NOT NULL,
    "target_user_id" bigint NOT NULL,
    "target_account_id" bigint NOT NULL,
    "admin_user_id" bigint NOT NULL,
    "amount_usdt" numeric(20,6) NOT NULL CONSTRAINT manual_deposits_amount_usdt_check CHECK (amount_usdt > 0::numeric),
    "external_reference" text NOT NULL,
    "tx_hash" text,
    "note" text,
    "ledger_entry_id" bigint NOT NULL,
    "idempotency_key" text NOT NULL,
    "created_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    CONSTRAINT manual_deposits_pkey PRIMARY KEY ("id")
);

CREATE INDEX idx_manual_deposits_target_user_id ON public.manual_deposits USING btree (target_user_id);

CREATE INDEX idx_manual_deposits_admin_user_id ON public.manual_deposits USING btree (admin_user_id);

ALTER TABLE ONLY "public"."manual_deposits" ADD CONSTRAINT "manual_deposits_target_user_id_fkey" FOREIGN KEY ("target_user_id") REFERENCES "public"."users" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."manual_deposits" ADD CONSTRAINT "manual_deposits_target_account_id_fkey" FOREIGN KEY ("target_account_id") REFERENCES "public"."accounts" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."manual_deposits" ADD CONSTRAINT "manual_deposits_admin_user_id_fkey" FOREIGN KEY ("admin_user_id") REFERENCES "public"."users" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."manual_deposits" ADD CONSTRAINT "manual_deposits_ledger_entry_id_fkey" FOREIGN KEY ("ledger_entry_id") REFERENCES "public"."ledger_entries" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE "public"."manual_deposits" ADD CONSTRAINT "manual_deposits_idempotency_key_key" UNIQUE (idempotency_key);

ALTER TABLE "public"."manual_deposits" ADD CONSTRAINT "manual_deposits_ledger_entry_id_key" UNIQUE (ledger_entry_id);

CREATE TABLE "public"."deposit_shards" (
    "id" bigserial NOT NULL,
    "shard_key" text NOT NULL,
    "deposit_address" text NOT NULL,
    "chain" text NOT NULL CONSTRAINT deposit_shards_chain_check CHECK (chain = ANY (ARRAY['ton_mainnet'::text])),
    "asset" text NOT NULL CONSTRAINT deposit_shards_asset_check CHECK (asset = 'USDT'::text),
    "is_active" boolean NOT NULL DEFAULT true,
    "created_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    "updated_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    CONSTRAINT deposit_shards_pkey PRIMARY KEY ("id")
);

ALTER TABLE "public"."deposit_shards" ADD CONSTRAINT "deposit_shards_shard_key_key" UNIQUE (shard_key);

ALTER TABLE "public"."deposit_shards" ADD CONSTRAINT "deposit_shards_deposit_address_key" UNIQUE (deposit_address);

CREATE TABLE "public"."chain_scan_cursors" (
    "source_key" text NOT NULL,
    "last_lt" bigint NOT NULL DEFAULT 0,
    "updated_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    CONSTRAINT chain_scan_cursors_pkey PRIMARY KEY ("source_key")
);

CREATE TABLE "public"."chain_incoming_txs" (
    "id" bigserial NOT NULL,
    "shard_id" bigint NOT NULL,
    "provider" text NOT NULL,
    "chain" text NOT NULL CONSTRAINT chain_incoming_txs_chain_check CHECK (chain = ANY (ARRAY['ton_mainnet'::text])),
    "asset" text NOT NULL CONSTRAINT chain_incoming_txs_asset_check CHECK (asset = 'USDT'::text),
    "tx_hash" text NOT NULL,
    "tx_lt" bigint NOT NULL,
    "query_id" text NOT NULL,
    "trace_id" text NOT NULL,
    "operation_type" text NOT NULL,
    "source_address" text,
    "destination_address" text,
    "amount_raw" text NOT NULL,
    "amount_usdt" numeric(20,6) NOT NULL CONSTRAINT chain_incoming_txs_amount_usdt_check CHECK (amount_usdt > 0::numeric),
    "occurred_at" timestamp with time zone NOT NULL,
    "suffix_code" smallint CONSTRAINT chain_incoming_txs_suffix_code_check CHECK (suffix_code >= 1 AND suffix_code <= 999),
    "status" text NOT NULL CONSTRAINT chain_incoming_txs_status_check CHECK (status = ANY (ARRAY['ingested'::text, 'credited'::text, 'manual_review'::text])),
    "review_reason" text,
    "matched_intent_id" bigint,
    "credited_ledger_entry_id" bigint,
    "raw_payload_json" jsonb NOT NULL DEFAULT '{}'::jsonb,
    "processed_at" timestamp with time zone,
    "created_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    "updated_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    CONSTRAINT chain_incoming_txs_pkey PRIMARY KEY ("id")
);

CREATE INDEX idx_chain_incoming_txs_status ON public.chain_incoming_txs USING btree (status, occurred_at);

CREATE INDEX idx_chain_incoming_txs_shard_suffix ON public.chain_incoming_txs USING btree (shard_id, suffix_code) WHERE (status = ANY (ARRAY['ingested'::text, 'manual_review'::text]));

ALTER TABLE ONLY "public"."chain_incoming_txs" ADD CONSTRAINT "chain_incoming_txs_shard_id_fkey" FOREIGN KEY ("shard_id") REFERENCES "public"."deposit_shards" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."chain_incoming_txs" ADD CONSTRAINT "chain_incoming_txs_credited_ledger_entry_id_fkey" FOREIGN KEY ("credited_ledger_entry_id") REFERENCES "public"."ledger_entries" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE "public"."chain_incoming_txs" ADD CONSTRAINT "chain_incoming_txs_credited_ledger_entry_id_key" UNIQUE (credited_ledger_entry_id);

CREATE UNIQUE INDEX uq_chain_incoming_txs_provider_identity ON public.chain_incoming_txs USING btree (shard_id, tx_hash, tx_lt, query_id, operation_type);

CREATE TABLE "public"."deposit_intents" (
    "id" bigserial NOT NULL,
    "seller_user_id" bigint NOT NULL,
    "target_account_id" bigint NOT NULL,
    "shard_id" bigint NOT NULL,
    "request_amount_usdt" numeric(20,6) NOT NULL CONSTRAINT deposit_intents_request_amount_usdt_check CHECK (request_amount_usdt > 0::numeric),
    "base_amount_usdt" numeric(20,6) NOT NULL CONSTRAINT deposit_intents_base_amount_usdt_check CHECK (base_amount_usdt > 0::numeric),
    "expected_amount_usdt" numeric(20,6) NOT NULL CONSTRAINT deposit_intents_expected_amount_usdt_check CHECK (expected_amount_usdt > 0::numeric),
    "suffix_code" smallint NOT NULL CONSTRAINT deposit_intents_suffix_code_check CHECK (suffix_code >= 1 AND suffix_code <= 999),
    "status" text NOT NULL CONSTRAINT deposit_intents_status_check CHECK (status = ANY (ARRAY['pending'::text, 'matched'::text, 'credited'::text, 'expired'::text, 'manual_review'::text, 'cancelled'::text])),
    "expires_at" timestamp with time zone NOT NULL,
    "matched_chain_tx_id" bigint,
    "credited_ledger_entry_id" bigint,
    "credited_amount_usdt" numeric(20,6),
    "review_reason" text,
    "idempotency_key" text NOT NULL,
    "created_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    "updated_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    CONSTRAINT deposit_intents_pkey PRIMARY KEY ("id")
);

CREATE INDEX idx_deposit_intents_seller_status ON public.deposit_intents USING btree (seller_user_id, status, created_at);

CREATE INDEX idx_deposit_intents_expiry_pending ON public.deposit_intents USING btree (expires_at) WHERE (status = ANY (ARRAY['pending'::text, 'matched'::text]));

ALTER TABLE ONLY "public"."deposit_intents" ADD CONSTRAINT "deposit_intents_seller_user_id_fkey" FOREIGN KEY ("seller_user_id") REFERENCES "public"."users" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."deposit_intents" ADD CONSTRAINT "deposit_intents_target_account_id_fkey" FOREIGN KEY ("target_account_id") REFERENCES "public"."accounts" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."deposit_intents" ADD CONSTRAINT "deposit_intents_shard_id_fkey" FOREIGN KEY ("shard_id") REFERENCES "public"."deposit_shards" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."deposit_intents" ADD CONSTRAINT "deposit_intents_matched_chain_tx_id_fkey" FOREIGN KEY ("matched_chain_tx_id") REFERENCES "public"."chain_incoming_txs" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."deposit_intents" ADD CONSTRAINT "deposit_intents_credited_ledger_entry_id_fkey" FOREIGN KEY ("credited_ledger_entry_id") REFERENCES "public"."ledger_entries" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE "public"."deposit_intents" ADD CONSTRAINT "deposit_intents_credited_ledger_entry_id_key" UNIQUE (credited_ledger_entry_id);

ALTER TABLE "public"."deposit_intents" ADD CONSTRAINT "deposit_intents_idempotency_key_key" UNIQUE (idempotency_key);

CREATE UNIQUE INDEX uq_deposit_intents_active_suffix ON public.deposit_intents USING btree (shard_id, suffix_code) WHERE (status = ANY (ARRAY['pending'::text, 'matched'::text, 'manual_review'::text]));

ALTER TABLE ONLY "public"."chain_incoming_txs" ADD CONSTRAINT "chain_incoming_txs_matched_intent_id_fkey" FOREIGN KEY ("matched_intent_id") REFERENCES "public"."deposit_intents" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

CREATE TABLE "public"."shops" (
    "id" bigserial NOT NULL,
    "seller_user_id" bigint NOT NULL,
    "slug" text NOT NULL,
    "title" text NOT NULL,
    "wb_token_ciphertext" text,
    "wb_token_status" text NOT NULL DEFAULT 'unknown'::text CONSTRAINT shops_wb_token_status_check CHECK (wb_token_status = ANY (ARRAY['unknown'::text, 'valid'::text, 'invalid'::text, 'expired'::text])),
    "wb_token_last_validated_at" timestamp with time zone,
    "wb_token_last_error" text,
    "wb_token_status_source" text CONSTRAINT shops_wb_token_status_source_check CHECK (wb_token_status_source = ANY (ARRAY['manual'::text, 'scrapper_401_withdrawn'::text, 'scrapper_401_token_expired'::text])),
    "wb_token_invalidated_at" timestamp with time zone,
    "deleted_at" timestamp with time zone,
    "deleted_by_user_id" bigint,
    "created_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    "updated_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    CONSTRAINT shops_pkey PRIMARY KEY ("id")
);

CREATE INDEX idx_shops_seller_user_id ON public.shops USING btree (seller_user_id);

CREATE INDEX idx_shops_seller_active ON public.shops USING btree (seller_user_id) WHERE (deleted_at IS NULL);

ALTER TABLE ONLY "public"."shops" ADD CONSTRAINT "shops_seller_user_id_fkey" FOREIGN KEY ("seller_user_id") REFERENCES "public"."users" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."shops" ADD CONSTRAINT "shops_deleted_by_user_id_fkey" FOREIGN KEY ("deleted_by_user_id") REFERENCES "public"."users" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

CREATE UNIQUE INDEX uq_shops_slug_active ON public.shops USING btree (slug) WHERE (deleted_at IS NULL);

CREATE UNIQUE INDEX uq_shops_seller_title_active_ci ON public.shops USING btree (seller_user_id, lower(title)) WHERE (deleted_at IS NULL);

CREATE TABLE "public"."users" (
    "id" bigserial NOT NULL,
    "telegram_id" bigint NOT NULL,
    "username" text,
    "role" text NOT NULL CONSTRAINT users_role_check CHECK (role = ANY (ARRAY['seller'::text, 'buyer'::text, 'admin'::text])),
    "is_seller" boolean NOT NULL DEFAULT false,
    "is_buyer" boolean NOT NULL DEFAULT false,
    "is_admin" boolean NOT NULL DEFAULT false,
    "created_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    "updated_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    CONSTRAINT users_pkey PRIMARY KEY ("id")
);

ALTER TABLE "public"."users" ADD CONSTRAINT "users_telegram_id_key" UNIQUE (telegram_id);

CREATE TABLE "public"."withdrawal_requests" (
    "id" bigserial NOT NULL,
    "buyer_user_id" bigint NOT NULL,
    "from_account_id" bigint NOT NULL,
    "to_account_id" bigint NOT NULL,
    "amount_usdt" numeric(20,6) NOT NULL CONSTRAINT withdrawal_requests_amount_usdt_check CHECK (amount_usdt > 0::numeric),
    "status" text NOT NULL CONSTRAINT withdrawal_requests_status_check CHECK (status = ANY (ARRAY['withdraw_pending_admin'::text, 'approved'::text, 'rejected'::text, 'withdraw_sent'::text])),
    "payout_address" text NOT NULL,
    "admin_user_id" bigint,
    "requested_at" timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),
    "processed_at" timestamp with time zone,
    "sent_at" timestamp with time zone,
    "idempotency_key" text NOT NULL,
    "note" text,
    CONSTRAINT withdrawal_requests_pkey PRIMARY KEY ("id")
);

CREATE INDEX idx_withdrawal_requests_status ON public.withdrawal_requests USING btree (status);

ALTER TABLE ONLY "public"."withdrawal_requests" ADD CONSTRAINT "withdrawal_requests_admin_user_id_fkey" FOREIGN KEY ("admin_user_id") REFERENCES "public"."users" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."withdrawal_requests" ADD CONSTRAINT "withdrawal_requests_buyer_user_id_fkey" FOREIGN KEY ("buyer_user_id") REFERENCES "public"."users" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."withdrawal_requests" ADD CONSTRAINT "withdrawal_requests_from_account_id_fkey" FOREIGN KEY ("from_account_id") REFERENCES "public"."accounts" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE ONLY "public"."withdrawal_requests" ADD CONSTRAINT "withdrawal_requests_to_account_id_fkey" FOREIGN KEY ("to_account_id") REFERENCES "public"."accounts" ("id") ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE "public"."withdrawal_requests" ADD CONSTRAINT "withdrawal_requests_idempotency_key_key" UNIQUE (idempotency_key);
