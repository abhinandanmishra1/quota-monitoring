-- ====================================================================
-- Quota Metering Service - Database Schema Definition
-- Adhering to DESIGN.md Section 5 Data Model
-- ====================================================================

-- 1. Default monthly quota limits for each feature
CREATE TABLE IF NOT EXISTS quota_config_default (
    feature VARCHAR(100) PRIMARY KEY,
    monthly_limit BIGINT NOT NULL CHECK (monthly_limit >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Organization-specific quota overrides for a feature
CREATE TABLE IF NOT EXISTS quota_config_override (
    org_id VARCHAR(100) NOT NULL,
    feature VARCHAR(100) NOT NULL,
    monthly_limit BIGINT NOT NULL CHECK (monthly_limit >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, feature)
);

-- 3. Monthly period-based quota usage records
-- Unique on (org_id, feature, period); check constraint: 0 <= used <= quota_limit
CREATE TABLE IF NOT EXISTS quota_usage (
    org_id VARCHAR(100) NOT NULL,
    feature VARCHAR(100) NOT NULL,
    period VARCHAR(7) NOT NULL, -- Format: YYYY-MM
    quota_limit BIGINT NOT NULL CHECK (quota_limit >= 0),
    used BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, feature, period),
    CONSTRAINT chk_quota_usage_range CHECK (used >= 0 AND used <= quota_limit)
);

-- 4. Quota operation audit and idempotency records
-- Unique on (org_id, feature, idempotency_key)
CREATE TABLE IF NOT EXISTS quota_operations (
    operation_id UUID PRIMARY KEY,
    org_id VARCHAR(100) NOT NULL,
    feature VARCHAR(100) NOT NULL,
    idempotency_key VARCHAR(255) NOT NULL,
    request_units BIGINT NOT NULL,
    period VARCHAR(7) NOT NULL, -- Original billing period charged
    status VARCHAR(20) NOT NULL CHECK (status IN ('CHARGED', 'FINALIZED', 'REFUNDED', 'REJECTED')),
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_quota_operations_key UNIQUE (org_id, feature, idempotency_key)
);

-- Index for background reconciliation of stale CHARGED operations
CREATE INDEX IF NOT EXISTS idx_quota_operations_status_created 
ON quota_operations (status, created_at);

-- Performance Optimization (DESIGN.md Section 8):
-- Consolidated atomic deduction function to minimize network round trips and lock duration.
DROP FUNCTION IF EXISTS fn_quota_consume;

CREATE OR REPLACE FUNCTION fn_quota_consume(
    p_org_id VARCHAR,
    p_feature VARCHAR,
    p_units BIGINT,
    p_idempotency_key VARCHAR,
    p_operation_id UUID,
    p_period VARCHAR,
    p_reset_at TIMESTAMPTZ
)
RETURNS TABLE (
    out_status_code INT,
    out_operation_id UUID,
    out_status VARCHAR,
    out_units BIGINT,
    out_remaining BIGINT,
    out_reset_at TIMESTAMPTZ,
    out_period VARCHAR,
    out_detail VARCHAR
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_op RECORD;
    v_limit BIGINT;
    v_usage RECORD;
    v_rem BIGINT;
    v_resp JSONB;
BEGIN
    -- 1. Check idempotency record (lock row to serialize concurrent identical retries)
    SELECT q.operation_id, q.request_units, q.status, q.response
    INTO v_op
    FROM quota_operations q
    WHERE q.org_id = p_org_id AND q.feature = p_feature AND q.idempotency_key = p_idempotency_key
    FOR UPDATE;

    IF FOUND THEN
        IF v_op.request_units = p_units THEN
            -- Stored response replay
            RETURN QUERY SELECT
                CASE v_op.status
                    WHEN 'CHARGED' THEN 200
                    WHEN 'FINALIZED' THEN 200
                    WHEN 'REFUNDED' THEN 409
                    ELSE 429
                END,
                v_op.operation_id,
                v_op.status,
                COALESCE((v_op.response->>'units')::BIGINT, (v_op.response->>'requested')::BIGINT, p_units),
                COALESCE((v_op.response->>'remaining')::BIGINT, 0::BIGINT),
                COALESCE((v_op.response->>'reset_at')::TIMESTAMPTZ, p_reset_at),
                COALESCE((v_op.response->>'period')::VARCHAR, p_period),
                CASE WHEN v_op.status = 'REFUNDED'
                    THEN 'Operation was refunded; use a new idempotency key'
                    ELSE (v_op.response->>'detail')
                END::VARCHAR;
            RETURN;
        ELSE
            -- 409 Conflict
            RETURN QUERY SELECT
                409,
                v_op.operation_id,
                'CONFLICT'::VARCHAR,
                p_units,
                0::BIGINT,
                p_reset_at,
                p_period,
                'Idempotency key reused with different parameters'::VARCHAR;
            RETURN;
        END IF;
    END IF;

    -- 2. Determine effective limit (override first, then feature default)
    SELECT qco.monthly_limit INTO v_limit
    FROM quota_config_override qco
    WHERE qco.org_id = p_org_id AND qco.feature = p_feature;

    IF v_limit IS NULL THEN
        SELECT qcd.monthly_limit INTO v_limit
        FROM quota_config_default qcd
        WHERE qcd.feature = p_feature;
    END IF;

    IF v_limit IS NULL THEN
        RETURN QUERY SELECT
            404,
            p_operation_id,
            'NOT_FOUND'::VARCHAR,
            p_units,
            0::BIGINT,
            p_reset_at,
            p_period,
            ('No quota configured for feature ''' || p_feature || '''')::VARCHAR;
        RETURN;
    END IF;

    -- 3. Lazily initialize usage row for current period
    INSERT INTO quota_usage (org_id, feature, period, quota_limit, used)
    VALUES (p_org_id, p_feature, p_period, v_limit, 0)
    ON CONFLICT (org_id, feature, period) DO NOTHING;

    -- 4. Atomic conditional deduction (DESIGN.md Section 4.1)
    UPDATE quota_usage qu
    SET used = qu.used + p_units, updated_at = NOW()
    WHERE qu.org_id = p_org_id
      AND qu.feature = p_feature
      AND qu.period = p_period
      AND qu.used <= qu.quota_limit - p_units
    RETURNING qu.used, qu.quota_limit INTO v_usage;

    IF FOUND THEN
        v_rem := v_usage.quota_limit - v_usage.used;
        v_resp := jsonb_build_object(
            'operation_id', p_operation_id,
            'status', 'CHARGED',
            'units', p_units,
            'remaining', v_rem,
            'reset_at', to_char(p_reset_at, 'YYYY-MM-DD"T"HH24:MI:SSOF'),
            'period', p_period
        );

        INSERT INTO quota_operations (
            operation_id, org_id, feature, idempotency_key, request_units, period, status, response
        )
        VALUES (
            p_operation_id, p_org_id, p_feature, p_idempotency_key, p_units, p_period, 'CHARGED', v_resp
        );

        RETURN QUERY SELECT
            200,
            p_operation_id,
            'CHARGED'::VARCHAR,
            p_units,
            v_rem,
            p_reset_at,
            p_period,
            NULL::VARCHAR;
        RETURN;
    ELSE
        -- Insufficient quota: all-or-nothing (DESIGN.md Section 4.2)
        SELECT qu.used, qu.quota_limit INTO v_usage
        FROM quota_usage qu
        WHERE qu.org_id = p_org_id AND qu.feature = p_feature AND qu.period = p_period;

        v_rem := COALESCE(v_usage.quota_limit - v_usage.used, 0);
        v_resp := jsonb_build_object(
            'detail', 'Quota limit exceeded',
            'requested', p_units,
            'remaining', v_rem,
            'reset_at', to_char(p_reset_at, 'YYYY-MM-DD"T"HH24:MI:SSOF'),
            'period', p_period
        );

        INSERT INTO quota_operations (
            operation_id, org_id, feature, idempotency_key, request_units, period, status, response
        )
        VALUES (
            p_operation_id, p_org_id, p_feature, p_idempotency_key, p_units, p_period, 'REJECTED', v_resp
        );

        RETURN QUERY SELECT
            429,
            p_operation_id,
            'REJECTED'::VARCHAR,
            p_units,
            v_rem,
            p_reset_at,
            p_period,
            'Quota limit exceeded'::VARCHAR;
        RETURN;
    END IF;
END;
$$;
