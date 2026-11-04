CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    phone TEXT,
    balance INTEGER NOT NULL DEFAULT 0,
    referrer BIGINT
);

CREATE TABLE IF NOT EXISTS referrals (
    invited BIGINT PRIMARY KEY,
    referrer BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    gift TEXT NOT NULL,
    cost INTEGER NOT NULL,
    recipient TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    channel_message_id BIGINT
);
