-- Supabase/Postgres schema for Garmin Connect data
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_name TEXT NOT NULL DEFAULT 'manga' UNIQUE,
    garmin_user_id TEXT UNIQUE,
    email TEXT UNIQUE,
    display_name TEXT,
    hr_zone_settings JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sport_types (
    id SMALLSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    category TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS activities (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    garmin_activity_id TEXT NOT NULL UNIQUE,
    sport_type_id SMALLINT NOT NULL REFERENCES sport_types(id),
    activity_name TEXT,
    start_time TIMESTAMPTZ NOT NULL,
    activity_type TEXT,
    elapsed_duration_seconds INTEGER,
    moving_duration_seconds INTEGER,
    distance_meters REAL,
    elevation_meters REAL,
    average_speed REAL,
    max_speed REAL,
    average_heart_rate INTEGER,
    max_heart_rate INTEGER,
    average_cadence REAL,
    max_cadence REAL,
    calories INTEGER,
    steps INTEGER,
    hr_zone_seconds JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS activity_laps (
    id BIGSERIAL PRIMARY KEY,
    activity_id BIGINT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    lap_number INTEGER NOT NULL,
    start_time TIMESTAMPTZ,
    elapsed_seconds INTEGER,
    distance_meters REAL,
    average_speed REAL,
    max_speed REAL,
    average_heart_rate INTEGER,
    max_heart_rate INTEGER,
    UNIQUE(activity_id, lap_number)
);

CREATE TABLE IF NOT EXISTS daily_summaries (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    summary_date DATE NOT NULL,
    steps INTEGER,
    active_calories INTEGER,
    total_calories INTEGER,
    total_distance_meters REAL,
    active_seconds INTEGER,
    sedentary_seconds INTEGER,
    floors_climbed INTEGER,
    resting_heart_rate INTEGER,
    is_final BOOLEAN NOT NULL DEFAULT TRUE,
    source_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, summary_date)
);

ALTER TABLE daily_summaries
    ADD COLUMN IF NOT EXISTS is_final BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS account_name TEXT NOT NULL DEFAULT 'manga';

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS hr_zone_settings JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE activities
    ADD COLUMN IF NOT EXISTS hr_zone_seconds JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_account_name
    ON users(account_name);

CREATE TABLE IF NOT EXISTS daily_sport_totals (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    summary_date DATE NOT NULL,
    sport_type_id SMALLINT NOT NULL REFERENCES sport_types(id),
    activity_count INTEGER NOT NULL DEFAULT 0,
    total_distance_meters REAL NOT NULL DEFAULT 0,
    total_duration_seconds INTEGER NOT NULL DEFAULT 0,
    total_calories INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, summary_date, sport_type_id)
);

CREATE INDEX IF NOT EXISTS idx_activities_user_start_time
    ON activities(user_id, start_time DESC);

CREATE INDEX IF NOT EXISTS idx_activities_sport_type
    ON activities(sport_type_id);

CREATE INDEX IF NOT EXISTS idx_activity_laps_activity_id
    ON activity_laps(activity_id);

CREATE INDEX IF NOT EXISTS idx_daily_summaries_user_date
    ON daily_summaries(user_id, summary_date DESC);

CREATE INDEX IF NOT EXISTS idx_daily_sport_totals_user_date
    ON daily_sport_totals(user_id, summary_date DESC);

CREATE INDEX IF NOT EXISTS idx_daily_sport_totals_sport_type
    ON daily_sport_totals(sport_type_id);

-- Optional seed rows for common Garmin sport categories
INSERT INTO sport_types (name, category)
VALUES
    ('running', 'cardio'),
    ('cycling', 'cardio'),
    ('walking', 'cardio'),
    ('swimming', 'cardio'),
    ('hiking', 'outdoor'),
    ('strength_training', 'strength'),
    ('yoga', 'wellness'),
    ('other', 'other')
ON CONFLICT (name) DO NOTHING;

-- Pick-and-mix workout blocks, edited client-side via Supabase Auth.
-- duration_weeks lets a block cover any length (one week, two weeks, etc.).
CREATE TABLE IF NOT EXISTS program_blocks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    start_date DATE NOT NULL,
    duration_weeks INTEGER NOT NULL DEFAULT 2,
    workouts JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, start_date)
);

ALTER TABLE program_blocks
    ADD COLUMN IF NOT EXISTS duration_weeks INTEGER NOT NULL DEFAULT 2;

ALTER TABLE program_blocks ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can view their own program blocks" ON program_blocks;
CREATE POLICY "Users can view their own program blocks"
    ON program_blocks FOR SELECT
    USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can insert their own program blocks" ON program_blocks;
CREATE POLICY "Users can insert their own program blocks"
    ON program_blocks FOR INSERT
    WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can update their own program blocks" ON program_blocks;
CREATE POLICY "Users can update their own program blocks"
    ON program_blocks FOR UPDATE
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

CREATE INDEX IF NOT EXISTS idx_program_blocks_user_start_date
    ON program_blocks(user_id, start_date);

-- Links a Supabase Auth user to a Garmin account_name (manga/chips), chosen
-- by the user themselves the first time they log in on program.html.
CREATE TABLE IF NOT EXISTS account_links (
    user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    account_name TEXT NOT NULL REFERENCES users(account_name),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE account_links ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can view their own account link" ON account_links;
CREATE POLICY "Users can view their own account link"
    ON account_links FOR SELECT
    USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can set their own account link" ON account_links;
CREATE POLICY "Users can set their own account link"
    ON account_links FOR INSERT
    WITH CHECK (auth.uid() = user_id);
