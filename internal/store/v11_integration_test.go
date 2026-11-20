package store

import (
	"context"
	"encoding/base64"
	"github.com/aivoya/acquisition/internal/core"
	"os"
	"testing"
	"time"
)

// Run only against a dedicated disposable PG16 DB, never production.
func integrationDB(t *testing.T) (*Store, context.Context) {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	t.Cleanup(cancel)
	s, err := Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(s.DB.Close)
	if err = s.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	return s, ctx
}

func TestIntegrationV11PolicyRotationAndGitHub(t *testing.T) {
	s, ctx := integrationDB(t)
	id := "it-gh-" + core.ID("")[:16]
	apiURL := "https://api.github.com/repos/acme/testing/releases?per_page=20"
	_, err := s.Register(ctx, []core.Source{{ID: id, Name: "Official release metadata test", OriginURL: "https://github.com/acme/testing", EndpointURL: apiURL, AccessMode: "github_releases", Rights: "metadata_only", IntervalMinutes: 60}})
	if err != nil {
		t.Fatal(err)
	}
	if err = s.Approve(ctx, id, "metadata_only", true, "test-reviewer", "integration authorization basis 123"); err != nil {
		t.Fatal(err)
	}
	oldID, err := s.Enqueue(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	// Re-approving a source invalidates the old policy version and queued job.
	if err = s.Approve(ctx, id, "metadata_only", true, "test-reviewer", "renewed authorization basis 1234"); err != nil {
		t.Fatal(err)
	}
	var oldStatus string
	if err = s.DB.QueryRow(ctx, `SELECT status FROM jobs WHERE job_id=$1`, oldID).Scan(&oldStatus); err != nil {
		t.Fatal(err)
	}
	if oldStatus != "blocked" {
		t.Fatalf("old-policy job must be blocked, got %s", oldStatus)
	}
	id2, err := s.Enqueue(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	claim, err := s.Claim(ctx)
	if err != nil || claim == nil {
		t.Fatalf("claim err=%v, claim=%+v", err, claim)
	}
	if claim.JobID != id2 || claim.OriginURL != "https://github.com/acme/testing" {
		t.Fatalf("wrong claim %+v", claim)
	}
	raw := []byte(`[ {"name":"v1.1","html_url":"https://github.com/acme/testing/releases/tag/v1.1"} ]`)
	result := core.Completion{SchemaVersion: "1.0", JobID: id2, LeaseToken: claim.LeaseToken, Status: "success", MethodUsed: "github_releases", HTTPStatus: 200, FinalURL: apiURL,
		AcquiredAt: time.Now().UTC(), MediaType: "application/json", RawBase64: base64.StdEncoding.EncodeToString(raw), ExtractorName: "github-official-api", ExtractorVersion: "2022-11-28",
		Entries: []core.FeedEntry{{URL: "https://github.com/acme/testing/releases/tag/v1.1", Title: "Release v1.1"}, {URL: "https://github.com/other/repo/releases/tag/v5", Title: "Injected release"}}}
	state, err := s.Complete(ctx, result)
	if err != nil || state != "succeeded" {
		t.Fatalf("complete %s %v", state, err)
	}
	var goodCount, badCount, articleJobs int
	if err = s.DB.QueryRow(ctx, `SELECT count(*) FROM feed_entries WHERE source_id=$1`, id).Scan(&goodCount); err != nil {
		t.Fatal(err)
	}
	if err = s.DB.QueryRow(ctx, `SELECT count(*) FROM records WHERE source_id=$1 AND record_kind='article'`, id).Scan(&badCount); err != nil {
		t.Fatal(err)
	}
	if err = s.DB.QueryRow(ctx, `SELECT count(*) FROM jobs WHERE source_id=$1 AND kind='feed_entry'`, id).Scan(&articleJobs); err != nil {
		t.Fatal(err)
	}
	if goodCount != 1 || badCount != 0 || articleJobs != 0 {
		t.Fatalf("out of scope metadata persisted %d %d %d", goodCount, badCount, articleJobs)
	}
	if err = s.Deny(ctx, id, "test-reviewer", "revoked test permission case"); err != nil {
		t.Fatal(err)
	}
	if err = s.DB.QueryRow(ctx, `SELECT count(*) FROM feed_entries WHERE source_id=$1`, id).Scan(&goodCount); err != nil || goodCount != 0 {
		t.Fatalf("revocation did not erase metadata: %d %v", goodCount, err)
	}
}

func TestIntegrationV11RetryAfterDomainCooldown(t *testing.T) {
	s, ctx := integrationDB(t)
	id := "it-cooldown-" + core.ID("")[:16]
	endpoint := "https://cooldown-example.org/feed.xml"
	_, err := s.Register(ctx, []core.Source{{ID: id, Name: "Rate limit test", OriginURL: "https://cooldown-example.org", EndpointURL: endpoint, AccessMode: "rss", Rights: "metadata_only", IntervalMinutes: 60}})
	if err != nil {
		t.Fatal(err)
	}
	if err = s.Approve(ctx, id, "metadata_only", true, "test-reviewer", "rate-limiting test permission"); err != nil {
		t.Fatal(err)
	}
	jobID, err := s.Enqueue(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	claim, err := s.Claim(ctx)
	if err != nil || claim == nil || claim.JobID != jobID {
		t.Fatalf("claim %+v %v", claim, err)
	}
	state, err := s.Complete(ctx, core.Completion{SchemaVersion: "1.0", JobID: claim.JobID, LeaseToken: claim.LeaseToken, Status: "retryable_error", ErrorCode: "rate_limited", Error: "HTTP 429", HTTPStatus: 429, RetryAfterSeconds: 240})
	if err != nil || state != "pending" {
		t.Fatalf("retry %q %v", state, err)
	}
	var waitJob, waitHost float64
	if err = s.DB.QueryRow(ctx, `SELECT extract(epoch from available_at-now()) FROM jobs WHERE job_id=$1`, jobID).Scan(&waitJob); err != nil {
		t.Fatal(err)
	}
	if err = s.DB.QueryRow(ctx, `SELECT extract(epoch from next_allowed_at-now()) FROM domain_limits WHERE hostname=$1`, `cooldown-example.org`).Scan(&waitHost); err != nil {
		t.Fatal(err)
	}
	if waitJob < 220 || waitHost < 220 {
		t.Fatalf("Retry-After cooldown not enforced (job=%v, host=%v)", waitJob, waitHost)
	}
}
