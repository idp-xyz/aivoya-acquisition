-- Managed acquisition categories. Existing source labels are linked without changing approvals.
CREATE TABLE IF NOT EXISTS categories (
  category_id text PRIMARY KEY CHECK (category_id ~ '^[a-z0-9][a-z0-9-]{0,40}$'),
  name text NOT NULL UNIQUE CHECK (char_length(btrim(name)) BETWEEN 1 AND 40),
  sort_order integer NOT NULL DEFAULT 100,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE sources ADD COLUMN IF NOT EXISTS category_id text REFERENCES categories(category_id);
CREATE INDEX IF NOT EXISTS sources_category_idx ON sources (category_id);
