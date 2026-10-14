-- Dedicated platform database. Never run this migration against transcript SQLite.
CREATE TABLE IF NOT EXISTS platform_users (
 id text PRIMARY KEY, active boolean NOT NULL DEFAULT true,
 daily_limit integer NOT NULL DEFAULT 6 CHECK (daily_limit BETWEEN 1 AND 20)
);
CREATE TABLE IF NOT EXISTS device_credentials (
 hash text PRIMARY KEY, user_id text NOT NULL REFERENCES platform_users(id),
 expires_at timestamptz NOT NULL, revoked boolean NOT NULL DEFAULT false
);
ALTER TABLE device_credentials ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'collector' CHECK (kind IN ('collector','browser'));
CREATE TABLE IF NOT EXISTS activation_invites (
 hash text PRIMARY KEY, user_id text NOT NULL REFERENCES platform_users(id),
 expires_at timestamptz NOT NULL, redeemed boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS analysis_jobs (
 id text PRIMARY KEY, user_id text NOT NULL REFERENCES platform_users(id),
 digest text NOT NULL, status text NOT NULL DEFAULT 'queued',
 created_at timestamptz NOT NULL DEFAULT now(), session_id text,
 evidence_ids jsonb NOT NULL, content jsonb, telemetry jsonb, error text
);
ALTER TABLE analysis_jobs ADD COLUMN IF NOT EXISTS evidence_digests jsonb;
CREATE INDEX IF NOT EXISTS analysis_user_day ON analysis_jobs(user_id,created_at);
CREATE TABLE IF NOT EXISTS daily_usage (day date PRIMARY KEY, requests integer NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS collector_library (
 user_id text PRIMARY KEY REFERENCES platform_users(id), sessions jsonb NOT NULL DEFAULT '[]',
 report_name text NOT NULL DEFAULT '', last_seen timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS published_reports (
 user_id text NOT NULL REFERENCES platform_users(id), id text NOT NULL,
 data jsonb NOT NULL, created_at timestamptz NOT NULL, PRIMARY KEY(user_id,id)
);
CREATE INDEX IF NOT EXISTS published_user_created ON published_reports(user_id,created_at DESC);
CREATE OR REPLACE FUNCTION create_collector_pairing(owner_id text, invite_hash text)
RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
 PERFORM 1 FROM platform_users WHERE id=owner_id AND active FOR UPDATE;
 IF NOT FOUND THEN RETURN false; END IF;
 IF (SELECT count(*) FROM activation_invites WHERE user_id=owner_id AND NOT redeemed AND expires_at>now())>=3 THEN RETURN false; END IF;
 INSERT INTO activation_invites(hash,user_id,expires_at) VALUES(invite_hash,owner_id,now()+interval '15 minutes');
 RETURN true;
END $$;

CREATE OR REPLACE FUNCTION activate_device(invite_hash text, device_hash text)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE invite activation_invites; user_record platform_users;
BEGIN
 SELECT * INTO invite FROM activation_invites WHERE hash=invite_hash FOR UPDATE;
 IF NOT FOUND OR invite.redeemed OR invite.expires_at<=now() THEN
  RAISE EXCEPTION 'INVALID_INVITE';
 END IF;
 SELECT * INTO user_record FROM platform_users WHERE id=invite.user_id FOR UPDATE;
 IF NOT user_record.active THEN RAISE EXCEPTION 'ACCESS_DISABLED'; END IF;
 IF (SELECT count(*) FROM device_credentials WHERE user_id=invite.user_id AND kind='collector' AND NOT revoked AND expires_at>now())>=3 THEN
  RAISE EXCEPTION 'DEVICE_LIMIT';
 END IF;
 UPDATE activation_invites SET redeemed=true WHERE hash=invite_hash;
 INSERT INTO device_credentials(hash,user_id,expires_at,revoked,kind) VALUES(device_hash,invite.user_id,now()+interval '30 days',false,'collector');
 RETURN invite.user_id;
END $$;

-- A Clerk session receives one deterministic browser credential. This function
-- serializes account admission without changing existing limits or usage rows.
CREATE OR REPLACE FUNCTION provision_web_account(owner_id text, credential_hash text)
RETURNS TABLE(provisioned_user_id text, provisioned_daily_limit integer, credential_expires_at timestamptz)
LANGUAGE plpgsql AS $$
DECLARE account platform_users; credential device_credentials;
BEGIN
 IF owner_id !~ '^clerk:user_[A-Za-z0-9]{1,100}$' OR credential_hash !~ '^[a-f0-9]{64}$' THEN RAISE EXCEPTION 'BROWSER_ACCESS_DISABLED'; END IF;
 INSERT INTO platform_users(id,daily_limit) VALUES(owner_id,2) ON CONFLICT(id) DO NOTHING;
 SELECT * INTO account FROM platform_users WHERE id=owner_id FOR UPDATE;
 IF NOT account.active THEN RAISE EXCEPTION 'BROWSER_ACCESS_DISABLED'; END IF;
 SELECT * INTO credential FROM device_credentials WHERE hash=credential_hash;
 IF FOUND THEN
  IF credential.user_id<>owner_id OR credential.kind<>'browser' OR credential.revoked OR credential.expires_at<=now() THEN RAISE EXCEPTION 'BROWSER_ACCESS_DISABLED'; END IF;
 ELSE
  IF (SELECT count(*) FROM device_credentials WHERE user_id=owner_id AND kind='browser' AND NOT revoked AND expires_at>now())>=5 THEN RAISE EXCEPTION 'BROWSER_LIMIT'; END IF;
  INSERT INTO device_credentials(hash,user_id,expires_at,kind) VALUES(credential_hash,owner_id,now()+interval '30 days','browser') RETURNING * INTO credential;
 END IF;
 RETURN QUERY SELECT account.id,account.daily_limit,credential.expires_at;
END $$;

-- Row locks make quota admission atomic across serverless instances/devices.
CREATE OR REPLACE FUNCTION reserve_analysis(device_hash text, job_id text, payload_digest text, ids jsonb)
RETURNS SETOF analysis_jobs LANGUAGE plpgsql AS $$
DECLARE u platform_users; credential device_credentials; existing analysis_jobs; total integer;
BEGIN
 SELECT * INTO credential FROM device_credentials WHERE hash=device_hash;
 IF NOT FOUND OR credential.revoked OR credential.expires_at<=now() THEN RAISE EXCEPTION 'ACCESS_DISABLED'; END IF;
 SELECT * INTO u FROM platform_users WHERE id=credential.user_id FOR UPDATE;
 IF NOT u.active THEN RAISE EXCEPTION 'ACCESS_DISABLED'; END IF;
 SELECT * INTO existing FROM analysis_jobs WHERE id=job_id;
 IF FOUND THEN
  IF existing.user_id<>u.id OR existing.digest<>payload_digest THEN RAISE EXCEPTION 'IDEMPOTENCY_CONFLICT'; END IF;
  RETURN NEXT existing; RETURN;
 END IF;
 IF (SELECT count(*) FROM analysis_jobs WHERE user_id=u.id AND created_at>=(now() AT TIME ZONE 'UTC')::date)>=u.daily_limit THEN
  RAISE EXCEPTION 'USER_QUOTA';
 END IF;
 IF EXISTS(SELECT 1 FROM analysis_jobs WHERE user_id=u.id AND status IN ('queued','running') AND created_at>now()-interval '5 minutes') THEN
  RAISE EXCEPTION 'ALREADY_RUNNING';
 END IF;
 INSERT INTO daily_usage VALUES((now() AT TIME ZONE 'UTC')::date,0) ON CONFLICT DO NOTHING;
 SELECT requests INTO total FROM daily_usage WHERE day=(now() AT TIME ZONE 'UTC')::date FOR UPDATE;
 IF total>=40 THEN RAISE EXCEPTION 'PLATFORM_QUOTA'; END IF;
 UPDATE daily_usage SET requests=requests+1 WHERE day=(now() AT TIME ZONE 'UTC')::date;
 INSERT INTO analysis_jobs(id,user_id,digest,evidence_ids) VALUES(job_id,u.id,payload_digest,ids) RETURNING * INTO existing;
 RETURN NEXT existing;
END $$;
