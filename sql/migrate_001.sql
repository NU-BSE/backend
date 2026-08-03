-- ============================================================================
-- Creepy.IM — backend schema (PostgreSQL)
--
-- Scope is deliberately minimal. The backend persists only:
--
--   users              the account
--   subscriptions      the system that gates agent (AI) usage
--
-- Everything else stays on the device:
--   * chat history            -> AsyncStorage (src/storage/history.ts)
--   * auth providers          -> local registry (src/auth/providers.ts)
--   * attestation / nonces / velocity / telegram launch / marketplace
--                             -> not persisted here
--
-- The only thing the server enforces is whether a user may use the agent,
-- via the subscription tables below.
--
-- Idempotent. Epoch-millisecond BIGINT columns are kept where the wire
-- protocol uses ms.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. Users
-- ----------------------------------------------------------------------------

-- The account everything else hangs off. `user_id` is the `customUserId`
-- sent in the `x-user-id` header (src/attestation/client/userSession.ts).
CREATE TABLE IF NOT EXISTS users (
  user_id    TEXT PRIMARY KEY,
  email      TEXT UNIQUE,
  name       TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ----------------------------------------------------------------------------
-- 2. Subscriptions — gates agent (AI) usage
-- ----------------------------------------------------------------------------

-- Catalogue of purchasable plans. `code` is the stable handle the client
-- references ('free', 'pro_monthly', 'pro_yearly', ...).
-- The agent-usage fields are what the server checks before letting a user
-- talk to the AI.
CREATE TABLE IF NOT EXISTS subscription_plans (
  plan_id                 TEXT PRIMARY KEY,
  code                    TEXT UNIQUE NOT NULL,
  name                    TEXT NOT NULL,
  description             TEXT,
  amount                  NUMERIC(12, 2) NOT NULL DEFAULT 0,
  currency                TEXT NOT NULL DEFAULT 'USD',
  interval                TEXT NOT NULL CHECK (interval IN ('week', 'month', 'year', 'lifetime')),
  interval_count          INTEGER NOT NULL DEFAULT 1,
  trial_days              INTEGER NOT NULL DEFAULT 0,
  -- Agent-usage limits enforced by the backend.
  agent_access            BOOLEAN NOT NULL DEFAULT FALSE,
  cloud_agent_allowed     BOOLEAN NOT NULL DEFAULT FALSE,
  max_agent_messages_per_day INTEGER,
  active                  BOOLEAN NOT NULL DEFAULT TRUE,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A user's subscription. Provider-agnostic: Stripe, Google Play Billing or
-- manual grants all land here; the provider's own id is kept for reconciliation.
CREATE TABLE IF NOT EXISTS subscriptions (
  subscription_id          TEXT PRIMARY KEY,
  user_id                  TEXT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
  plan_id                  TEXT NOT NULL REFERENCES subscription_plans (plan_id),
  status                   TEXT NOT NULL CHECK (status IN
                             ('incomplete', 'trialing', 'active', 'past_due', 'canceled', 'expired')),
  provider                 TEXT
                           CHECK (provider IN ('stripe', 'google_play', 'apple_app_store', 'manual')),
  provider_subscription_id TEXT,
  current_period_start     TIMESTAMPTZ NOT NULL,
  current_period_end       TIMESTAMPTZ NOT NULL,
  cancel_at_period_end     BOOLEAN NOT NULL DEFAULT FALSE,
  canceled_at              TIMESTAMPTZ,
  ended_at                 TIMESTAMPTZ,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (provider, provider_subscription_id)
);

CREATE INDEX IF NOT EXISTS subscriptions_user_idx
  ON subscriptions (user_id);

CREATE INDEX IF NOT EXISTS subscriptions_renewal_idx
  ON subscriptions (status, current_period_end);

-- Payments / invoices against a subscription.
CREATE TABLE IF NOT EXISTS subscription_invoices (
  invoice_id          TEXT PRIMARY KEY,
  subscription_id     TEXT NOT NULL REFERENCES subscriptions (subscription_id) ON DELETE CASCADE,
  user_id             TEXT NOT NULL REFERENCES users (user_id),
  amount              NUMERIC(12, 2) NOT NULL,
  currency            TEXT NOT NULL,
  status              TEXT NOT NULL CHECK (status IN
                        ('draft', 'open', 'paid', 'void', 'uncollectible', 'refunded')),
  provider            TEXT,
  provider_payment_id TEXT,
  period_start        TIMESTAMPTZ,
  period_end          TIMESTAMPTZ,
  paid_at             TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS subscription_invoices_subscription_idx
  ON subscription_invoices (subscription_id);

CREATE INDEX IF NOT EXISTS subscription_invoices_user_idx
  ON subscription_invoices (user_id);

-- Raw provider webhook / lifecycle events (audit log; process idempotently).
CREATE TABLE IF NOT EXISTS subscription_events (
  event_id        BIGSERIAL PRIMARY KEY,
  provider        TEXT NOT NULL,
  event_type      TEXT NOT NULL,
  payload         JSONB NOT NULL,
  subscription_id TEXT REFERENCES subscriptions (subscription_id) ON DELETE SET NULL,
  processed_at    TIMESTAMPTZ,
  received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS subscription_events_subscription_idx
  ON subscription_events (subscription_id);

-- Effective agent-usage rights, derived from an active subscription (or a
-- manual grant). The API checks this table — not the subscription rows —
-- before allowing an agent request, so expiry, grace windows and promo grants
-- all collapse into one lookup.
CREATE TABLE IF NOT EXISTS subscription_entitlements (
  user_id     TEXT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
  entitlement TEXT NOT NULL,
  source      TEXT NOT NULL DEFAULT 'subscription'
              CHECK (source IN ('subscription', 'grant', 'trial')),
  granted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at  TIMESTAMPTZ,
  PRIMARY KEY (user_id, entitlement)
);
