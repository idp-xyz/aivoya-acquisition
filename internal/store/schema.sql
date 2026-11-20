-- AIVOYA Acquisition Fabric | PostgreSQL 16 | transactional schema v1
CREATE TABLE IF NOT EXISTS sources (
  source_id text PRIMARY KEY CHECK (source_id ~ '^[a-z0-9][a-z0-9-]{1,94}$'),
  name text NOT NULL,
  origin_url text NOT NULL,
  endpoint_url text NOT NULL,
  region text NOT NULL DEFAULT '',
  language text NOT NULL DEFAULT 'und',
  category text NOT NULL DEFAULT '',
  source_mode text NOT NULL DEFAULT '',
  onboarding_batch text NOT NULL DEFAULT '',
  endpoint_verification text NOT NULL DEFAULT '',
  endpoint_hint text NOT NULL DEFAULT '',
  ingestion_notes text NOT NULL DEFAULT '', 
  access_mode text NOT NULL CHECK (access_mode IN ('rss','html_index','monitor','html_static','browser_render','github_releases','api','manual')),
  rights text NOT NULL DEFAULT 'metadata_only' CHECK (rights IN ('metadata_only','extract_only','full_text_licensed','prohibited')),
  review_status text NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending','approved','denied')),
  policy_version integer NOT NULL DEFAULT 1 CHECK (policy_version>=1),
  enabled boolean NOT NULL DEFAULT false,
  interval_minutes integer NOT NULL DEFAULT 60 CHECK (interval_minutes BETWEEN 5 AND 10080),
  next_due timestamptz NOT NULL DEFAULT now(),
  last_checked timestamptz,
  last_claim_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sources_due_idx ON sources (next_due) WHERE enabled AND review_status='approved';

CREATE TABLE IF NOT EXISTS jobs (
  job_id text PRIMARY KEY,
  source_id text NOT NULL REFERENCES sources(source_id),
  target_url text NOT NULL,
  canonical_url text NOT NULL,
  method text NOT NULL CHECK (method IN ('rss','html_index','monitor','html_static','browser_render','github_releases','api')),
  policy_version integer NOT NULL,
  kind text NOT NULL DEFAULT 'scheduled' CHECK (kind IN ('scheduled','feed_entry','manual')),
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','leased','succeeded','review','blocked','failed')),
  attempt integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
  available_at timestamptz NOT NULL DEFAULT now(),
  lease_until timestamptz,
  lease_token text,
  last_error_code text,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);
-- Prevent many concurrent duplicate active fetches of the same article / source.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_unique ON jobs (source_id,canonical_url) WHERE status IN ('pending','leased');
CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs(status,available_at,lease_until);
CREATE INDEX IF NOT EXISTS jobs_source_idx ON jobs(source_id,created_at DESC);

CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id text PRIMARY KEY,
  job_id text NOT NULL UNIQUE REFERENCES jobs(job_id),
  content_sha256 text NOT NULL CHECK(length(content_sha256)=64),
  media_type text NOT NULL,
  payload bytea NOT NULL,
  byte_size integer NOT NULL,
  acquired_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS snapshots_hash_idx ON snapshots(content_sha256);

CREATE TABLE IF NOT EXISTS records (
  record_id text PRIMARY KEY,
  source_id text NOT NULL REFERENCES sources(source_id),
  job_id text NOT NULL REFERENCES jobs(job_id),
  snapshot_id text REFERENCES snapshots(snapshot_id),
  canonical_url text NOT NULL,
  content_sha256 text,
  raw_sha256 text,
  title text NOT NULL,
  body_text text NOT NULL DEFAULT '',
  language text NOT NULL DEFAULT 'und',
  published_at timestamptz,
  acquired_at timestamptz NOT NULL,
  extractor_name text NOT NULL,
  extractor_version text NOT NULL,
  quality_score numeric(4,3) NOT NULL,
  record_kind text NOT NULL CHECK(record_kind IN ('metadata','article')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source_id,canonical_url,content_sha256,record_kind)
);
CREATE INDEX IF NOT EXISTS records_recent_idx ON records(created_at DESC);
CREATE INDEX IF NOT EXISTS records_source_idx ON records(source_id,created_at DESC);

CREATE TABLE IF NOT EXISTS feed_entries (
  source_id text NOT NULL REFERENCES sources(source_id),
  canonical_url text NOT NULL,
  title text NOT NULL,
  published_at timestamptz,
  discovered_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(source_id,canonical_url)
);

CREATE TABLE IF NOT EXISTS job_events (
  event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id text NOT NULL REFERENCES jobs(job_id),
  event_type text NOT NULL,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  event_time timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS job_events_job_idx ON job_events(job_id,event_time);

-- Metadata records (NULL content hash) must also be unique on re-discovery.
CREATE UNIQUE INDEX IF NOT EXISTS records_dedupe_idx ON records(source_id,canonical_url,COALESCE(content_sha256,''),record_kind);

CREATE TABLE IF NOT EXISTS source_policy_events (
 event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 source_id text NOT NULL REFERENCES sources(source_id),
 action text NOT NULL CHECK(action IN ('approved','denied','config_changed')),
 reviewer text NOT NULL,
 basis text NOT NULL,
 rights text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS source_policy_events_recent_idx ON source_policy_events (source_id,created_at DESC);

-- Cross-source hostname pacing; claim reservation is serialized by the row lock.
CREATE TABLE IF NOT EXISTS domain_limits (
 hostname text PRIMARY KEY,
 next_allowed_at timestamptz NOT NULL DEFAULT now(),
 interval_seconds integer NOT NULL DEFAULT 10 CHECK(interval_seconds BETWEEN 5 AND 3600)
);
