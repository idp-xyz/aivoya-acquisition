-- Existing V1.0 databases had CHECK constraints without this explicit connector.
-- Apply in a single migration transaction; source approvals remain unchanged.
ALTER TABLE sources DROP CONSTRAINT IF EXISTS sources_access_mode_check;
ALTER TABLE sources ADD CONSTRAINT sources_access_mode_check
  CHECK (access_mode IN ('rss','html_index','monitor','html_static','browser_render','github_releases','api','manual'));
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_method_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_method_check
  CHECK (method IN ('rss','html_index','monitor','html_static','browser_render','github_releases','api'));
