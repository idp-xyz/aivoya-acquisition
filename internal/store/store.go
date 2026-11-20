package store

import (
	"context"
	"crypto/sha256"
	"embed"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net/url"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/aivoya/acquisition/internal/core"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

//go:embed schema.sql migrations/*.sql
var schema embed.FS

type Store struct{ DB *pgxpool.Pool }

func Open(ctx context.Context, dsn string) (*Store, error) {
	db, e := pgxpool.New(ctx, dsn)
	if e != nil {
		return nil, e
	}
	if e = db.Ping(ctx); e != nil {
		db.Close()
		return nil, e
	}
	return &Store{db}, nil
}

// Migrate serializes startup DDL across replicas and upgrades existing V1.0
// databases without replaying already-applied migrations.
func (s *Store) Migrate(ctx context.Context) error {
	tx, err := s.DB.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(98114751)`); err != nil {
		return err
	}
	ddl, err := schema.ReadFile("schema.sql")
	if err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, string(ddl), pgx.QueryExecModeSimpleProtocol); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())`); err != nil {
		return err
	}
	paths, err := fs.Glob(schema, "migrations/*.sql")
	if err != nil {
		return err
	}
	sort.Strings(paths)
	for _, path := range paths {
		var applied bool
		version := filepath.Base(path)
		if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version=$1)`, version).Scan(&applied); err != nil {
			return err
		}
		if applied {
			continue
		}
		data, readErr := schema.ReadFile(path)
		if readErr != nil {
			return readErr
		}
		if _, err = tx.Exec(ctx, string(data), pgx.QueryExecModeSimpleProtocol); err != nil {
			return fmt.Errorf("migration %s: %w", version, err)
		}
		if _, err = tx.Exec(ctx, `INSERT INTO schema_migrations(version) VALUES($1)`, version); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}
func (s *Store) Register(ctx context.Context, items []core.Source) (int, error) {
	if len(items) > 500 {
		return 0, errors.New("bulk limit 500")
	}
	tx, e := s.DB.Begin(ctx)
	if e != nil {
		return 0, e
	}
	defer tx.Rollback(ctx)
	n := 0
	for _, v := range items {
		if v.IntervalMinutes == 0 {
			v.IntervalMinutes = 60
		}
		if v.Rights == "" {
			v.Rights = "metadata_only"
		}
		if v.EndpointURL == "" {
			v.EndpointURL = v.OriginURL
		}
		if e := core.CheckSource(v); e != nil {
			return n, fmt.Errorf("%s: %w", v.ID, e)
		}
		// Deliberately never overwrite approval, rights, enabled or policy version on import.
		_, e := tx.Exec(ctx, `INSERT INTO sources(source_id,name,origin_url,endpoint_url,region,language,category,source_mode,access_mode,interval_minutes,onboarding_batch,endpoint_verification,endpoint_hint,ingestion_notes)
 VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
 ON CONFLICT(source_id) DO UPDATE SET name=EXCLUDED.name,region=EXCLUDED.region,language=EXCLUDED.language,category=EXCLUDED.category,source_mode=EXCLUDED.source_mode,onboarding_batch=EXCLUDED.onboarding_batch,endpoint_verification=EXCLUDED.endpoint_verification,endpoint_hint=EXCLUDED.endpoint_hint,ingestion_notes=EXCLUDED.ingestion_notes,updated_at=now()`, v.ID, v.Name, v.OriginURL, v.EndpointURL, v.Region, v.Language, v.Category, v.SourceMode, v.AccessMode, v.IntervalMinutes, v.OnboardingBatch, v.EndpointVerification, v.EndpointHint, v.IngestionNotes)
		if e != nil {
			return n, e
		}
		n++
	}
	if e = tx.Commit(ctx); e != nil {
		return 0, e
	}
	return n, nil
}
func (s *Store) ListSources(ctx context.Context, limit int) ([]core.Source, error) {
	rows, e := s.DB.Query(ctx, `SELECT source_id,name,origin_url,endpoint_url,region,language,category,source_mode,access_mode,rights,review_status,policy_version,enabled,interval_minutes,onboarding_batch,endpoint_verification,endpoint_hint,ingestion_notes FROM sources ORDER BY source_id LIMIT $1`, limit)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []core.Source{}
	for rows.Next() {
		var v core.Source
		if e = rows.Scan(&v.ID, &v.Name, &v.OriginURL, &v.EndpointURL, &v.Region, &v.Language, &v.Category, &v.SourceMode, &v.AccessMode, &v.Rights, &v.ReviewStatus, &v.PolicyVersion, &v.Enabled, &v.IntervalMinutes, &v.OnboardingBatch, &v.EndpointVerification, &v.EndpointHint, &v.IngestionNotes); e != nil {
			return nil, e
		}
		out = append(out, v)
	}
	return out, rows.Err()
}
func (s *Store) Approve(ctx context.Context, id, rights string, enabled bool, reviewer, basis string) error {
	if rights != "metadata_only" && rights != "extract_only" && rights != "full_text_licensed" {
		return errors.New("invalid rights")
	}
	if len(strings.TrimSpace(reviewer)) < 3 || len(strings.TrimSpace(basis)) < 12 {
		return errors.New("reviewer and basis must be provided")
	}
	tx, e := s.DB.Begin(ctx)
	if e != nil {
		return e
	}
	defer tx.Rollback(ctx)
	tag, e := tx.Exec(ctx, `UPDATE sources SET rights=$2,review_status='approved',enabled=$3,policy_version=policy_version+1,updated_at=now() WHERE source_id=$1`, id, rights, enabled)
	if e != nil {
		return e
	}
	if tag.RowsAffected() != 1 {
		return pgx.ErrNoRows
	}
	if e = blockOldPolicyJobs(ctx, tx, id); e != nil {
		return e
	}
	if rights != "full_text_licensed" {
		if e = eraseLicensedPayloads(ctx, tx, id, false); e != nil {
			return e
		}
	}
	_, e = tx.Exec(ctx, `INSERT INTO source_policy_events(source_id,action,reviewer,basis,rights) VALUES($1,'approved',$2,$3,$4)`, id, limitString(reviewer, 120), limitString(basis, 2000), rights)
	if e != nil {
		return e
	}
	return tx.Commit(ctx)
}

// Re-review increments policy_version; pending/leased work from the previous
// decision must not silently remain pending forever or write under old rights.
func blockOldPolicyJobs(ctx context.Context, tx pgx.Tx, id string) error {
	_, err := tx.Exec(ctx, `UPDATE jobs j SET status='blocked',last_error_code='policy_superseded',lease_token=NULL,lease_until=NULL,completed_at=now(),updated_at=now()
		FROM sources s WHERE s.source_id=$1 AND j.source_id=s.source_id AND j.policy_version<>s.policy_version AND j.status IN ('pending','leased')`, id)
	return err
}

func (s *Store) Deny(ctx context.Context, id, reviewer, basis string) error {
	if len(strings.TrimSpace(reviewer)) < 3 || len(strings.TrimSpace(basis)) < 12 {
		return errors.New("reviewer and basis must be provided")
	}
	tx, e := s.DB.Begin(ctx)
	if e != nil {
		return e
	}
	defer tx.Rollback(ctx)
	tag, e := tx.Exec(ctx, `UPDATE sources SET rights='prohibited',review_status='denied',enabled=false,policy_version=policy_version+1,updated_at=now() WHERE source_id=$1`, id)
	if e != nil {
		return e
	}
	if tag.RowsAffected() != 1 {
		return pgx.ErrNoRows
	}
	if e = eraseLicensedPayloads(ctx, tx, id, true); e != nil {
		return e
	}
	_, e = tx.Exec(ctx, `INSERT INTO source_policy_events(source_id,action,reviewer,basis,rights) VALUES($1,'denied',$2,$3,'prohibited')`, id, limitString(reviewer, 120), limitString(basis, 2000))
	if e != nil {
		return e
	}
	return tx.Commit(ctx)
}
func (s *Store) Schedule(ctx context.Context) (int, error) {
	tx, e := s.DB.Begin(ctx)
	if e != nil {
		return 0, e
	}
	defer tx.Rollback(ctx)
	rows, e := tx.Query(ctx, `UPDATE sources s SET next_due=now()+(s.interval_minutes * interval '1 minute'),last_checked=now(),updated_at=now()
 WHERE source_id IN (SELECT source_id FROM sources WHERE enabled=true AND review_status='approved' AND rights<>'prohibited' AND access_mode IN ('rss','html_index','monitor','html_static','browser_render','github_releases') AND next_due<=now() ORDER BY next_due LIMIT 100 FOR UPDATE SKIP LOCKED)
 RETURNING source_id,endpoint_url,access_mode,policy_version`)
	if e != nil {
		return 0, e
	}
	type due struct {
		id, url, method string
		version         int
	}
	q := []due{}
	for rows.Next() {
		var d due
		if e = rows.Scan(&d.id, &d.url, &d.method, &d.version); e != nil {
			rows.Close()
			return 0, e
		}
		q = append(q, d)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return 0, e
	}
	created := 0
	for _, d := range q {
		canonical, e := core.CanonicalURL(d.url)
		if e != nil {
			continue
		}
		tag, e := tx.Exec(ctx, `INSERT INTO jobs(job_id,source_id,target_url,canonical_url,method,policy_version,kind) VALUES($1,$2,$3,$4,$5,$6,'scheduled') ON CONFLICT DO NOTHING`, core.ID("job_"), d.id, d.url, canonical, d.method, d.version)
		if e != nil {
			return 0, e
		}
		created += int(tag.RowsAffected())
	}
	if e = tx.Commit(ctx); e != nil {
		return 0, e
	}
	return created, nil
}
func (s *Store) Enqueue(ctx context.Context, id string) (string, error) {
	var urlstr, method string
	var ver int
	var approved bool
	e := s.DB.QueryRow(ctx, `SELECT endpoint_url,access_mode,policy_version,(review_status='approved' AND enabled AND rights<>'prohibited') FROM sources WHERE source_id=$1`, id).Scan(&urlstr, &method, &ver, &approved)
	if e != nil {
		return "", e
	}
	if !approved {
		return "", errors.New("source not approved or disabled")
	}
	if method == "manual" || method == "api" {
		return "", errors.New("requires dedicated connector")
	}
	canonical, e := core.CanonicalURL(urlstr)
	if e != nil {
		return "", e
	}
	jid := core.ID("job_")
	tag, e := s.DB.Exec(ctx, `INSERT INTO jobs(job_id,source_id,target_url,canonical_url,method,policy_version,kind) VALUES($1,$2,$3,$4,$5,$6,'manual') ON CONFLICT DO NOTHING`, jid, id, urlstr, canonical, method, ver)
	if e != nil {
		return "", e
	}
	if tag.RowsAffected() == 0 {
		return "", errors.New("active job exists")
	}
	return jid, nil
}
func (s *Store) Claim(ctx context.Context) (*core.Claim, error) {
	tx, e := s.DB.Begin(ctx)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(ctx)
	// expired leases are retried; stale completions are fenced by a new lease token.
	var j core.Claim
	var attempt int
	var status string
	e = tx.QueryRow(ctx, `SELECT j.job_id,j.source_id,j.target_url,s.origin_url,j.method,s.rights,j.policy_version,j.attempt,j.status FROM jobs j JOIN sources s USING(source_id)
 WHERE (j.status='pending' AND j.available_at<=now() OR j.status='leased' AND j.lease_until<now()) AND j.attempt<j.max_attempts AND s.enabled AND s.review_status='approved' AND s.rights<>'prohibited' AND s.policy_version=j.policy_version AND (s.last_claim_at IS NULL OR s.last_claim_at < now()-interval '10 seconds') AND NOT EXISTS (SELECT 1 FROM domain_limits dl WHERE dl.hostname=lower(substring(j.target_url from '^https?://([^/?#]+)')) AND dl.next_allowed_at>now())
 ORDER BY j.available_at,j.created_at LIMIT 1 FOR UPDATE OF j SKIP LOCKED`).Scan(&j.JobID, &j.SourceID, &j.URL, &j.OriginURL, &j.Method, &j.Rights, &j.PolicyVersion, &attempt, &status)
	if errors.Is(e, pgx.ErrNoRows) {
		return nil, nil
	}
	if e != nil {
		return nil, e
	}
	host, _ := url.Parse(j.URL)
	if host == nil {
		return nil, errors.New("invalid claimed URL")
	}
	var lockedHost string
	e = tx.QueryRow(ctx, `INSERT INTO domain_limits(hostname,next_allowed_at) VALUES($1,now()+interval '10 seconds') ON CONFLICT(hostname) DO UPDATE SET next_allowed_at=now()+(domain_limits.interval_seconds*interval '1 second') WHERE domain_limits.next_allowed_at<=now() RETURNING hostname`, strings.ToLower(host.Hostname())).Scan(&lockedHost)
	if errors.Is(e, pgx.ErrNoRows) {
		return nil, nil
	}
	if e != nil {
		return nil, e
	}
	j.SchemaVersion = "1.0"
	j.LeaseToken = core.ID("lease_")
	j.Attempt = attempt + 1
	j.DeadlineMS = 25000
	_, e = tx.Exec(ctx, `UPDATE jobs SET status='leased',attempt=attempt+1,lease_token=$2,lease_until=now()+interval '90 seconds',updated_at=now() WHERE job_id=$1`, j.JobID, j.LeaseToken)
	if e == nil {
		_, e = tx.Exec(ctx, `UPDATE sources SET last_claim_at=now() WHERE source_id=$1`, j.SourceID)
	}
	if e != nil {
		return nil, e
	}
	_, e = tx.Exec(ctx, `INSERT INTO job_events(job_id,event_type,details) VALUES($1,'leased',jsonb_build_object('attempt',$2::integer))`, j.JobID, j.Attempt)
	if e != nil {
		return nil, e
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, e
	}
	return &j, nil
}
func sameHost(a, b string) bool {
	ua, e := url.Parse(a)
	if e != nil {
		return false
	}
	ub, e := url.Parse(b)
	if e != nil {
		return false
	}
	x := strings.ToLower(ua.Hostname())
	y := strings.ToLower(ub.Hostname())
	return strings.TrimSuffix(x, ".") == strings.TrimSuffix(y, ".")
}

// Only release web links under the exact configured GitHub repository are
// eligible. An API response must not invent crawl targets on arbitrary hosts.
func entryPermitted(method, target, endpoint, sourceOrigin string) bool {
	if method != "github_releases" {
		return sameHost(target, endpoint) || (sourceOrigin != "" && sameHost(target, sourceOrigin))
	}
	origin := endpoint
	api, err := url.Parse(origin)
	if err != nil || !strings.EqualFold(api.Hostname(), "api.github.com") {
		return false
	}
	pieces := strings.Split(strings.Trim(api.Path, "/"), "/")
	if len(pieces) != 4 || pieces[0] != "repos" || pieces[3] != "releases" {
		return false
	}
	u, err := url.Parse(target)
	if err != nil || u.Scheme != "https" || !strings.EqualFold(u.Hostname(), "github.com") {
		return false
	}
	if u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Port() != "" {
		return false
	}
	pathParts := strings.Split(strings.Trim(u.Path, "/"), "/")
	return len(pathParts) == 5 && strings.EqualFold(pathParts[0], pieces[1]) && strings.EqualFold(pathParts[1], pieces[2]) && pathParts[2] == "releases" && pathParts[3] == "tag" && pathParts[4] != ""
}

func (s *Store) Complete(ctx context.Context, c core.Completion) (string, error) {
	if c.SchemaVersion != "1.0" {
		return "", errors.New("unsupported schema version")
	}
	if c.JobID == "" || c.LeaseToken == "" {
		return "", errors.New("missing job or lease token")
	}
	tx, e := s.DB.Begin(ctx)
	if e != nil {
		return "", e
	}
	defer tx.Rollback(ctx)
	var id, src, original, sourceOrigin, method, rights, review string
	var ver, currentVer, attempt, maxAttempts int
	var enabled bool
	e = tx.QueryRow(ctx, `SELECT j.job_id,j.source_id,j.target_url,s.origin_url,j.method,s.rights,s.review_status,j.policy_version,s.policy_version,j.attempt,j.max_attempts,s.enabled FROM jobs j JOIN sources s USING(source_id) WHERE j.job_id=$1 AND j.status='leased' AND j.lease_token=$2 AND j.lease_until>=now() FOR UPDATE OF j`, c.JobID, c.LeaseToken).Scan(&id, &src, &original, &sourceOrigin, &method, &rights, &review, &ver, &currentVer, &attempt, &maxAttempts, &enabled)
	if e != nil {
		return "", fmt.Errorf("stale/unknown lease: %w", e)
	}
	if review != "approved" || !enabled || rights == "prohibited" || ver != currentVer {
		_, e = tx.Exec(ctx, `UPDATE jobs SET status='blocked',last_error_code='policy_changed',lease_token=NULL,lease_until=NULL,updated_at=now(),completed_at=now() WHERE job_id=$1`, id)
		if e != nil {
			return "", e
		}
		return "blocked", tx.Commit(ctx)
	}
	status := "failed"
	if c.Status != "success" {
		switch c.Status {
		case "blocked":
			status = "blocked"
		case "retryable_error":
			if attempt < maxAttempts {
				status = "pending"
			}
		case "permanent_error":
			status = "failed"
		default:
			return "", errors.New("unknown completion status")
		}
		delay := min(3600, 15*(1<<min(attempt, 7)))
		if c.Status == "retryable_error" && c.RetryAfterSeconds > delay {
			delay = min(3600, c.RetryAfterSeconds)
		}
		// Apply origin-wide cooldown on observed 429/503 with Retry-After;
		// no additional simultaneous job from this host should be leased.
		if c.RetryAfterSeconds > 0 && (c.HTTPStatus == 429 || c.HTTPStatus == 503) {
			hostURL, parseErr := url.Parse(original)
			if parseErr == nil && hostURL.Hostname() != "" {
				_, e = tx.Exec(ctx, `INSERT INTO domain_limits(hostname,next_allowed_at) VALUES($1,now()+($2 * interval '1 second')) ON CONFLICT(hostname) DO UPDATE SET next_allowed_at=greatest(domain_limits.next_allowed_at,excluded.next_allowed_at)`, strings.ToLower(hostURL.Hostname()), min(3600, c.RetryAfterSeconds))
				if e != nil {
					return "", e
				}
			}
		}
		_, e = tx.Exec(ctx, `UPDATE jobs SET status=$2,last_error_code=$3,last_error=$4,available_at=now()+($5 * interval '1 second'),lease_token=NULL,lease_until=NULL,updated_at=now(),completed_at=CASE WHEN $2='pending' THEN NULL ELSE now() END WHERE job_id=$1`, id, status, limitString(c.ErrorCode, 90), limitString(c.Error, 1000), delay)
		if e != nil {
			return "", e
		}
	} else {
		if c.MethodUsed != "" && c.MethodUsed != method {
			return "", errors.New("method mismatch")
		}
		if c.HTTPStatus < 200 || c.HTTPStatus >= 300 {
			return "", errors.New("non-2xx success result")
		}
		if c.FinalURL == "" {
			c.FinalURL = original
		}
		if e = core.ValidatePublicURL(c.FinalURL); e != nil {
			return "", fmt.Errorf("invalid final URL: %w", e)
		}
		if !sameHost(c.FinalURL, original) {
			return "", errors.New("redirect changed registered source hostname")
		}
		if c.AcquiredAt.IsZero() {
			return "", errors.New("missing acquired_at")
		}
		raw, e := base64.StdEncoding.DecodeString(c.RawBase64)
		if e != nil {
			return "", e
		}
		if len(raw) == 0 || len(raw) > 2*1024*1024 {
			return "", errors.New("raw snapshot missing or exceeds 2 MiB cap")
		}
		digest := sha256.Sum256(raw)
		hash := hex.EncodeToString(digest[:])
		snapID := core.ID("raw_")
		_, e = tx.Exec(ctx, `INSERT INTO snapshots(snapshot_id,job_id,content_sha256,media_type,payload,byte_size,acquired_at) VALUES($1,$2,$3,$4,$5,$6,$7)`, snapID, id, hash, limitString(c.MediaType, 100), raw, len(raw), c.AcquiredAt)
		if e != nil {
			return "", e
		}
		if method == "rss" || method == "html_index" || method == "github_releases" {
			if len(c.Entries) == 0 {
				status = "review"
			}
			for _, entry := range c.Entries {
				if len(entry.URL) > 2048 || strings.TrimSpace(entry.Title) == "" || !entryPermitted(method, entry.URL, original, sourceOrigin) || core.ValidatePublicURL(entry.URL) != nil {
					continue
				}
				canon, e := core.CanonicalURL(entry.URL)
				if e != nil {
					continue
				}
				_, e = tx.Exec(ctx, `INSERT INTO feed_entries(source_id,canonical_url,title,published_at) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING`, src, canon, limitString(entry.Title, 500), entry.PublishedAt)
				if e != nil {
					return "", e
				}
				if rights == "metadata_only" || rights == "extract_only" || method == "github_releases" {
					_, e = tx.Exec(ctx, `INSERT INTO records(record_id,source_id,job_id,snapshot_id,canonical_url,content_sha256,title,body_text,language,published_at,acquired_at,extractor_name,extractor_version,quality_score,record_kind) VALUES($1,$2,$3,$4,$5,NULL,$6,'','und',$7,$8,'feed-parser','1.0',0.500,'metadata') ON CONFLICT DO NOTHING`, core.ID("rec_"), src, id, snapID, canon, limitString(entry.Title, 500), entry.PublishedAt, c.AcquiredAt)
					if e != nil {
						return "", e
					}
				}
				if rights == "full_text_licensed" && method != "github_releases" {
					// Every policy version gets at most one article attempt per discovered URL.
					// Backfill is allowed after rights are upgraded even for previously seen feed entries.
					_, e = tx.Exec(ctx, `INSERT INTO jobs(job_id,source_id,target_url,canonical_url,method,policy_version,kind)
                      SELECT $1,$2,$3,$4,'html_static',$5,'feed_entry'
                      WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE source_id=$2 AND canonical_url=$4 AND kind='feed_entry' AND policy_version=$5)
                      AND NOT EXISTS (SELECT 1 FROM records WHERE source_id=$2 AND canonical_url=$4 AND record_kind='article')
                      ON CONFLICT DO NOTHING`, core.ID("job_"), src, entry.URL, canon, ver)
					if e != nil {
						return "", e
					}
				}
			}
		} else if method == "monitor" {
			canonical, e := core.CanonicalURL(c.FinalURL)
			if e != nil {
				return "", e
			}
			title := c.Title
			if title == "" {
				title = "Monitored page: " + src
			}
			_, e = tx.Exec(ctx, `INSERT INTO records(record_id,source_id,job_id,snapshot_id,canonical_url,content_sha256,title,body_text,language,acquired_at,extractor_name,extractor_version,quality_score,record_kind) VALUES($1,$2,$3,$4,$5,$6,$7,'','und',$8,'page-monitor','1.0',0.500,'metadata') ON CONFLICT DO NOTHING`, core.ID("rec_"), src, id, snapID, canonical, hash, limitString(title, 500), c.AcquiredAt)
			if e != nil {
				return "", e
			}
		} else {
			normalizedDigest := sha256.Sum256([]byte(strings.TrimSpace(c.Title) + "\n" + strings.TrimSpace(c.BodyText)))
			normalizedHash := hex.EncodeToString(normalizedDigest[:])
			score := core.Score(c.Title, c.BodyText)
			status = "succeeded"
			if score < 0.7 {
				status = "review"
			}
			if status == "succeeded" {
				canonical, e := core.CanonicalURL(c.FinalURL)
				if e != nil {
					return "", e
				}
				if rights == "full_text_licensed" || rights == "extract_only" {
					// extract_only: derived result can be evaluated internally, but do not persist full body.
					body := ""
					kind := "metadata"
					if rights == "full_text_licensed" {
						body = limitString(c.BodyText, 200000)
						kind = "article"
					}
					_, e = tx.Exec(ctx, `INSERT INTO records(record_id,source_id,job_id,snapshot_id,canonical_url,content_sha256,title,body_text,language,published_at,acquired_at,extractor_name,extractor_version,quality_score,record_kind) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) ON CONFLICT DO NOTHING`, core.ID("rec_"), src, id, snapID, canonical, normalizedHash, limitString(c.Title, 500), body, limitString(c.Language, 12), c.PublishedAt, c.AcquiredAt, limitString(c.ExtractorName, 50), limitString(c.ExtractorVersion, 30), score, kind)
					if e != nil {
						return "", e
					}
				} else {
					_, e = tx.Exec(ctx, `INSERT INTO records(record_id,source_id,job_id,snapshot_id,canonical_url,content_sha256,title,body_text,language,published_at,acquired_at,extractor_name,extractor_version,quality_score,record_kind) VALUES($1,$2,$3,$4,$5,NULL,$6,'','und',$7,$8,$9,$10,$11,'metadata') ON CONFLICT DO NOTHING`, core.ID("rec_"), src, id, snapID, canonical, limitString(c.Title, 500), c.PublishedAt, c.AcquiredAt, limitString(c.ExtractorName, 50), limitString(c.ExtractorVersion, 30), score)
					if e != nil {
						return "", e
					}
				}
			}
		}
		_, e = tx.Exec(ctx, `UPDATE records SET raw_sha256=$2 WHERE job_id=$1 AND raw_sha256 IS NULL`, id, hash)
		if e != nil {
			return "", e
		}
		if status != "review" {
			status = "succeeded"
		}
		_, e = tx.Exec(ctx, `UPDATE jobs SET status=$2,completed_at=now(),lease_token=NULL,lease_until=NULL,updated_at=now(),last_error_code=CASE WHEN $2='review' THEN 'quality_rejected' ELSE NULL END WHERE job_id=$1`, id, status)
		if e != nil {
			return "", e
		}
	}
	details, _ := json.Marshal(map[string]any{"status": status, "error_code": limitString(c.ErrorCode, 90), "duration_ms": c.DurationMS, "http_status": c.HTTPStatus})
	_, e = tx.Exec(ctx, `INSERT INTO job_events(job_id,event_type,details) VALUES($1,'completed',$2::jsonb)`, id, string(details))
	if e != nil {
		return "", e
	}
	return status, tx.Commit(ctx)
}
func limitString(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}
func (s *Store) Stats(ctx context.Context) (map[string]any, error) {
	out := map[string]any{}
	var n int64
	for _, r := range []struct{ name, sql string }{{"sources", `SELECT count(*) FROM sources`}, {"approved_sources", `SELECT count(*) FROM sources WHERE review_status='approved'`}, {"records", `SELECT count(*) FROM records`}, {"snapshots", `SELECT count(*) FROM snapshots`}} {
		if e := s.DB.QueryRow(ctx, r.sql).Scan(&n); e != nil {
			return nil, e
		}
		out[r.name] = n
	}
	rows, e := s.DB.Query(ctx, `SELECT status,count(*) FROM jobs GROUP BY status`)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	statuses := map[string]int64{}
	for rows.Next() {
		var x string
		var c int64
		if e = rows.Scan(&x, &c); e != nil {
			return nil, e
		}
		statuses[x] = c
	}
	out["jobs"] = statuses
	return out, rows.Err()
}
func (s *Store) ListJobs(ctx context.Context, limit int) ([]map[string]any, error) {
	rows, e := s.DB.Query(ctx, `SELECT job_id,source_id,target_url,method,status,attempt,created_at,last_error_code FROM jobs ORDER BY created_at DESC LIMIT $1`, limit)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	r := []map[string]any{}
	for rows.Next() {
		var id, source, url, method, status string
		var attempt int
		var created time.Time
		var errorCode *string
		if e = rows.Scan(&id, &source, &url, &method, &status, &attempt, &created, &errorCode); e != nil {
			return nil, e
		}
		m := map[string]any{"job_id": id, "source_id": source, "url": url, "method": method, "status": status, "attempt": attempt, "created_at": created}
		if errorCode != nil {
			m["error_code"] = *errorCode
		}
		r = append(r, m)
	}
	return r, rows.Err()
}
func (s *Store) ListRecords(ctx context.Context, limit int) ([]map[string]any, error) {
	rows, e := s.DB.Query(ctx, `SELECT record_id,source_id,canonical_url,title,record_kind,quality_score,created_at FROM records ORDER BY created_at DESC LIMIT $1`, limit)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	r := []map[string]any{}
	for rows.Next() {
		var id, source, url, title, kind string
		var score float64
		var created time.Time
		if e = rows.Scan(&id, &source, &url, &title, &kind, &score, &created); e != nil {
			return nil, e
		}
		r = append(r, map[string]any{"record_id": id, "source_id": source, "url": url, "title": title, "kind": kind, "quality_score": score, "created_at": created})
	}
	return r, rows.Err()
}

// ReapExpired converts exhausted leases into terminal failures rather than
// leaving them invisible in the queue indefinitely.
func (s *Store) ReapExpired(ctx context.Context) error {
	_, e := s.DB.Exec(ctx, `UPDATE jobs SET status='failed',last_error_code='lease_expired',last_error='worker did not acknowledge before lease deadline',lease_token=NULL,lease_until=NULL,completed_at=now(),updated_at=now() WHERE status='leased' AND lease_until<now() AND attempt>=max_attempts`)
	return e
}

// Purge unlicensed operational raw snapshots after the configured short TTL.
// Records retain source URL, metadata and hash only; no stored body is served to consumers.
func (s *Store) PurgeRestrictedSnapshots(ctx context.Context) (int64, error) {
	tx, e := s.DB.Begin(ctx)
	if e != nil {
		return 0, e
	}
	defer tx.Rollback(ctx)
	// Foreign keys have no cascade; detach record pointers before purge.
	_, e = tx.Exec(ctx, `UPDATE records r SET snapshot_id=NULL FROM snapshots sn JOIN jobs j ON j.job_id=sn.job_id JOIN sources s ON s.source_id=j.source_id WHERE r.snapshot_id=sn.snapshot_id AND sn.created_at<now()-(CASE WHEN s.rights='full_text_licensed' THEN interval '30 days' ELSE interval '7 days' END)`)
	if e != nil {
		return 0, e
	}
	tag, e := tx.Exec(ctx, `DELETE FROM snapshots sn USING jobs j,sources s WHERE sn.job_id=j.job_id AND j.source_id=s.source_id AND sn.created_at<now()-(CASE WHEN s.rights='full_text_licensed' THEN interval '30 days' ELSE interval '7 days' END)`)
	if e != nil {
		return 0, e
	}
	return tag.RowsAffected(), tx.Commit(ctx)
}

// Erase bytes and derived text when a source's content rights are downgraded
// or revoked. Audit events remain; policy reasons remain in policy events.
func eraseLicensedPayloads(ctx context.Context, tx pgx.Tx, id string, fullRevocation bool) error {
	_, e := tx.Exec(ctx, `UPDATE records SET snapshot_id=NULL WHERE source_id=$1`, id)
	if e != nil {
		return e
	}
	_, e = tx.Exec(ctx, `DELETE FROM snapshots sn USING jobs j WHERE sn.job_id=j.job_id AND j.source_id=$1`, id)
	if e != nil {
		return e
	}
	_, e = tx.Exec(ctx, `DELETE FROM records WHERE source_id=$1 AND record_kind='article'`, id)
	if e != nil {
		return e
	}
	if fullRevocation {
		_, e = tx.Exec(ctx, `DELETE FROM records WHERE source_id=$1`, id)
		if e != nil {
			return e
		}
		_, e = tx.Exec(ctx, `DELETE FROM feed_entries WHERE source_id=$1`, id)
		if e != nil {
			return e
		}
		_, e = tx.Exec(ctx, `UPDATE jobs SET status='blocked',last_error_code='policy_revoked',lease_token=NULL,lease_until=NULL,completed_at=now() WHERE source_id=$1 AND status IN ('pending','leased')`, id)
		if e != nil {
			return e
		}
	}
	return nil
}

func (s *Store) GetRecord(ctx context.Context, id string) (map[string]any, error) {
	var recordID, sourceID, jobID, canonical, title, body, lang, engine, version, kind string
	var snapshotID, contentHash, rawHash *string
	var published *time.Time
	var acquired time.Time
	var score float64
	var rights string
	e := s.DB.QueryRow(ctx, `SELECT r.record_id,r.source_id,r.job_id,r.snapshot_id,r.canonical_url,r.content_sha256,r.raw_sha256,r.title,r.body_text,r.language,r.published_at,r.acquired_at,r.extractor_name,r.extractor_version,r.quality_score,r.record_kind,s.rights FROM records r JOIN sources s USING(source_id) WHERE r.record_id=$1`, id).Scan(&recordID, &sourceID, &jobID, &snapshotID, &canonical, &contentHash, &rawHash, &title, &body, &lang, &published, &acquired, &engine, &version, &score, &kind, &rights)
	if e != nil {
		return nil, e
	}
	if rights != "full_text_licensed" {
		body = ""
	}
	return map[string]any{"record_id": recordID, "source_id": sourceID, "job_id": jobID, "snapshot_id": snapshotID, "canonical_url": canonical, "raw_sha256": rawHash, "content_sha256": contentHash, "title": title, "body_text": body, "language": lang, "published_at": published, "acquired_at": acquired, "extractor_name": engine, "extractor_version": version, "quality_score": score, "record_kind": kind, "rights": rights}, nil
}
func (s *Store) JobEvents(ctx context.Context, id string) ([]map[string]any, error) {
	rows, e := s.DB.Query(ctx, `SELECT event_type,details,event_time FROM job_events WHERE job_id=$1 ORDER BY event_id DESC LIMIT 100`, id)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []map[string]any{}
	for rows.Next() {
		var event string
		var details []byte
		var tm time.Time
		if e = rows.Scan(&event, &details, &tm); e != nil {
			return nil, e
		}
		var data map[string]any
		_ = json.Unmarshal(details, &data)
		out = append(out, map[string]any{"event_type": event, "details": data, "event_time": tm})
	}
	return out, rows.Err()
}
func (s *Store) PolicyEvents(ctx context.Context, id string) ([]map[string]any, error) {
	rows, e := s.DB.Query(ctx, `SELECT action,reviewer,basis,rights,created_at FROM source_policy_events WHERE source_id=$1 ORDER BY created_at DESC LIMIT 100`, id)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []map[string]any{}
	for rows.Next() {
		var action, reviewer, basis, rights string
		var tm time.Time
		if e = rows.Scan(&action, &reviewer, &basis, &rights, &tm); e != nil {
			return nil, e
		}
		out = append(out, map[string]any{"action": action, "reviewer": reviewer, "basis": basis, "rights": rights, "created_at": tm})
	}
	return out, rows.Err()
}

// UpdateConfig deliberately resets approval on transport or endpoint change.
// It also deletes previously stored source content until a fresh rights review.
func (s *Store) UpdateConfig(ctx context.Context, id, endpoint, mode string, interval int, reviewer, basis string) error {
	if err := core.ValidatePublicURL(endpoint); err != nil {
		return err
	}
	if mode == "github_releases" {
		if err := core.CheckGitHubReleasesURL(endpoint); err != nil {
			return err
		}
	}
	if interval < 5 || interval > 10080 {
		return errors.New("invalid interval")
	}
	switch mode {
	case "rss", "html_index", "monitor", "html_static", "browser_render", "github_releases", "api", "manual":
	default:
		return errors.New("invalid access mode")
	}
	if len(strings.TrimSpace(reviewer)) < 3 || len(strings.TrimSpace(basis)) < 12 {
		return errors.New("reviewer and basis must be provided")
	}
	tx, e := s.DB.Begin(ctx)
	if e != nil {
		return e
	}
	defer tx.Rollback(ctx)
	tag, e := tx.Exec(ctx, `UPDATE sources SET endpoint_url=$2,access_mode=$3,interval_minutes=$4,rights='metadata_only',review_status='pending',enabled=false,policy_version=policy_version+1,updated_at=now() WHERE source_id=$1`, id, endpoint, mode, interval)
	if e != nil {
		return e
	}
	if tag.RowsAffected() != 1 {
		return pgx.ErrNoRows
	}
	if e = eraseLicensedPayloads(ctx, tx, id, true); e != nil {
		return e
	}
	_, e = tx.Exec(ctx, `INSERT INTO source_policy_events(source_id,action,reviewer,basis,rights) VALUES($1,'config_changed',$2,$3,'metadata_only')`, id, limitString(reviewer, 120), limitString(basis, 2000))
	if e != nil {
		return e
	}
	return tx.Commit(ctx)
}

// SourceHealth is a rolling operational view, not a claim of factual accuracy.
// Success is a completed transport/parse job that passed basic heuristic QC;
// article correctness requires an independently labeled evaluation dataset.
func (s *Store) SourceHealth(ctx context.Context) ([]map[string]any, error) {
	rows, err := s.DB.Query(ctx, `SELECT s.source_id, s.name, s.access_mode, s.review_status, s.enabled,
  count(j.job_id),
  count(j.job_id) FILTER (WHERE j.status='succeeded'),
  count(j.job_id) FILTER (WHERE j.status='review'),
  count(j.job_id) FILTER (WHERE j.status='failed'),
  count(j.job_id) FILTER (WHERE j.status='blocked'),
  count(j.job_id) FILTER (WHERE j.status IN ('pending','leased')),
  max(j.completed_at)
 FROM sources s LEFT JOIN jobs j ON j.source_id=s.source_id AND j.created_at >= now() - interval '24 hours'
 GROUP BY s.source_id,s.name,s.access_mode,s.review_status,s.enabled
 ORDER BY count(j.job_id) FILTER (WHERE j.status IN ('failed','review')) DESC, s.source_id ASC LIMIT 500`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]map[string]any, 0)
	for rows.Next() {
		var id, name, mode, approval string
		var enabled bool
		var total, succeeded, review, failed, blocked, inFlight int64
		var lastCompleted *time.Time
		if err = rows.Scan(&id, &name, &mode, &approval, &enabled, &total, &succeeded, &review, &failed, &blocked, &inFlight, &lastCompleted); err != nil {
			return nil, err
		}
		var rate any = nil
		if terminal := succeeded + review + failed + blocked; terminal > 0 {
			rate = float64(succeeded) / float64(terminal)
		}
		out = append(out, map[string]any{"source_id": id, "name": name, "access_mode": mode, "review_status": approval, "enabled": enabled,
			"window_hours": 24, "jobs": total, "succeeded": succeeded, "review": review, "failed": failed, "blocked": blocked, "in_flight": inFlight,
			"terminal_success_ratio": rate, "last_completed_at": lastCompleted})
	}
	return out, rows.Err()
}
