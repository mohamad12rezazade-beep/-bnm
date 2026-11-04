-- کاربران
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    phone TEXT UNIQUE,
    balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    referrer_id BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
    referrer_rewarded BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- سفارش‌ها
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id),
    gift_name TEXT NOT NULL,
    gift_price INTEGER NOT NULL CHECK (gift_price > 0),
    recipient TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'completed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published BOOLEAN NOT NULL DEFAULT FALSE
);

-- دعوت‌ها
CREATE TABLE IF NOT EXISTS referrals (
    id BIGSERIAL PRIMARY KEY,
    referrer_id BIGINT NOT NULL REFERENCES users(user_id),
    referred_id BIGINT NOT NULL UNIQUE REFERENCES users(user_id),
    rewarded BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- تراکنش‌ها
CREATE TABLE IF NOT EXISTS transactions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id),
    amount INTEGER NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('invite_reward', 'purchase', 'refund')),
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ایندکس‌ها
CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone);
CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referrer_id);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id);

-- تابع اتمیک کسر موجودی + ایجاد سفارش
CREATE OR REPLACE FUNCTION create_order_atomic(
    p_order_id TEXT,
    p_user_id BIGINT,
    p_gift_name TEXT,
    p_gift_price INTEGER,
    p_recipient TEXT
) RETURNS BOOLEAN AS $$
DECLARE
    v_balance INTEGER;
BEGIN
    SELECT balance INTO v_balance FROM users WHERE user_id = p_user_id FOR UPDATE;
    IF v_balance IS NULL OR v_balance < p_gift_price THEN
        RETURN FALSE;
    END IF;
    UPDATE users SET balance = balance - p_gift_price WHERE user_id = p_user_id;
    INSERT INTO orders(order_id, user_id, gift_name, gift_price, recipient, status)
    VALUES (p_order_id, p_user_id, p_gift_name, p_gift_price, p_recipient, 'pending');
    INSERT INTO transactions(user_id, amount, type, description)
    VALUES (p_user_id, -p_gift_price, 'purchase', 'خرید ' || p_gift_name);
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;

-- تابع اتمیک رد سفارش + بازگشت وجه
CREATE OR REPLACE FUNCTION reject_order_atomic(p_order_id TEXT) RETURNS BOOLEAN AS $$
DECLARE
    v_order orders%ROWTYPE;
BEGIN
    SELECT * INTO v_order FROM orders WHERE order_id = p_order_id AND status = 'pending' FOR UPDATE;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    UPDATE orders SET status = 'rejected', updated_at = NOW() WHERE order_id = p_order_id;
    UPDATE users SET balance = balance + v_order.gift_price WHERE user_id = v_order.user_id;
    INSERT INTO transactions(user_id, amount, type, description)
    VALUES (v_order.user_id, v_order.gift_price, 'refund', 'بازگشت وجه رد سفارش ' || p_order_id);
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;

-- تابع اتمیک پاداش دعوت
CREATE OR REPLACE FUNCTION reward_referrer_atomic(p_referred_id BIGINT) RETURNS BOOLEAN AS $$
DECLARE
    v_referrer_id BIGINT;
BEGIN
    SELECT referrer_id INTO v_referrer_id FROM users WHERE user_id = p_referred_id AND referrer_rewarded = FALSE FOR UPDATE;
    IF v_referrer_id IS NULL THEN
        RETURN FALSE;
    END IF;
    UPDATE users SET referrer_rewarded = TRUE WHERE user_id = p_referred_id;
    UPDATE users SET balance = balance + 2 WHERE user_id = v_referrer_id;
    INSERT INTO referrals(referrer_id, referred_id, rewarded) VALUES (v_referrer_id, p_referred_id, TRUE);
    INSERT INTO transactions(user_id, amount, type, description)
    VALUES (v_referrer_id, 2, 'invite_reward', 'پاداش دعوت کاربر ' || p_referred_id);
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;
