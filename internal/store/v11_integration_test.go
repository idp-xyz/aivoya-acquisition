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

func TestIntegrationUpdateOriginResetsApproval(t *testing.T) {
	s, ctx := integrationDB(t)
	id := "it-origin-" + core.ID("")[:16]
	endpoint := "https://rss.example.org/rss/cs"
	_, err := s.Register(ctx, []core.Source{{ID: id, Name: "Origin correction test", OriginURL: "https://info.example.org/help", EndpointURL: endpoint, AccessMode: "rss", Rights: "metadata_only", IntervalMinutes: 60}})
	if err != nil {
		t.Fatal(err)
	}
	if err = s.Approve(ctx, id, "metadata_only", true, "test-reviewer", "initial authorization basis"); err != nil {
		t.Fatal(err)
	}
	corrected := "https://example.org/list/cs/recent"
	if err = s.UpdateConfig(ctx, id, corrected, endpoint, "rss", 60, "test-reviewer", "article links live on the publisher host"); err != nil {
		t.Fatal(err)
	}
	var origin, status string
	var enabled bool
	if err = s.DB.QueryRow(ctx, `SELECT origin_url,review_status,enabled FROM sources WHERE source_id=$1`, id).Scan(&origin, &status, &enabled); err != nil {
		t.Fatal(err)
	}
	if origin != corrected || status != "pending" || enabled {
		t.Fatalf("origin update did not reset approval: %s %s %v", origin, status, enabled)
	}
	if err = s.Approve(ctx, id, "metadata_only", true, "test-reviewer", "renewed authorization basis"); err != nil {
		t.Fatal(err)
	}
	if err = s.UpdateConfig(ctx, id, "", endpoint, "rss", 90, "test-reviewer", "interval only, keep the publisher host"); err != nil {
		t.Fatal(err)
	}
	var interval int
	if err = s.DB.QueryRow(ctx, `SELECT origin_url,interval_minutes,review_status FROM sources WHERE source_id=$1`, id).Scan(&origin, &interval, &status); err != nil {
		t.Fatal(err)
	}
	if origin != corrected || interval != 90 || status != "pending" {
		t.Fatalf("empty origin changed the publisher host: %s %d %s", origin, interval, status)
	}
}

func TestIntegrationCategoriesPersistAndAssign(t *testing.T) {
	s, ctx := integrationDB(t)
	id := "it-cat-" + core.ID("")[:12]
	_, err := s.Register(ctx, []core.Source{{ID: id, Name: "Category source", OriginURL: "https://example.com/news", EndpointURL: "https://example.com/news", AccessMode: "html_index", Category: "模型发布", Rights: "metadata_only", IntervalMinutes: 60}})
	if err != nil {
		t.Fatal(err)
	}
	cats, err := s.ListCategories(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var original core.Category
	for _, item := range cats {
		if item.Name == "模型发布" {
			original = item
		}
	}
	if original.ID == "" || original.Sources < 1 {
		t.Fatalf("imported label was not persisted: %+v", cats)
	}
	created, err := s.CreateCategory(ctx, "基础设施")
	if err != nil {
		t.Fatal(err)
	}
	if err = s.AssignCategory(ctx, id, created.ID); err != nil {
		t.Fatal(err)
	}
	var label, linked string
	if err = s.DB.QueryRow(ctx, `SELECT category, category_id FROM sources WHERE source_id=$1`, id).Scan(&label, &linked); err != nil {
		t.Fatal(err)
	}
	if label != "基础设施" || linked != created.ID {
		t.Fatalf("assignment = %s %s", label, linked)
	}
	if err = s.RenameCategory(ctx, created.ID, "基础设施平台"); err != nil {
		t.Fatal(err)
	}
	var status string
	var version int
	if err = s.DB.QueryRow(ctx, `SELECT category, review_status, policy_version FROM sources WHERE source_id=$1`, id).Scan(&label, &status, &version); err != nil || label != "基础设施平台" || status != "pending" || version != 1 {
		t.Fatalf("rename changed source policy: label=%s status=%s version=%d err=%v", label, status, version, err)
	}
	if _, err = s.Register(ctx, []core.Source{{ID: id, Name: "Category source", OriginURL: "https://example.com/news", EndpointURL: "https://example.com/news", AccessMode: "html_index", Category: "模型发布", Rights: "metadata_only", IntervalMinutes: 60}}); err != nil {
		t.Fatal(err)
	}
	if err = s.DB.QueryRow(ctx, `SELECT category, category_id FROM sources WHERE source_id=$1`, id).Scan(&label, &linked); err != nil || label != "基础设施平台" || linked != created.ID {
		t.Fatalf("import overwrote maintained category: %s %s %v", label, linked, err)
	}
	ordered := make([]string, 0, len(cats))
	for _, item := range cats {
		if item.ID != created.ID {
			ordered = append(ordered, item.ID)
		}
	}
	ordered = append(ordered, created.ID)
	if err = s.ReorderCategories(ctx, []string{created.ID}); err == nil {
		t.Fatal("partial category order was accepted")
	}
	if err = s.ReorderCategories(ctx, ordered); err != nil {
		t.Fatal(err)
	}
	if cats, err = s.ListCategories(ctx); err != nil || cats[len(cats)-1].ID != created.ID {
		t.Fatalf("reorder = %+v %v", cats, err)
	}
	if err = s.DeleteCategory(ctx, created.ID, false); err == nil {
		t.Fatal("deleted a category that still has sources")
	}
	if err = s.DeleteCategory(ctx, created.ID, true); err != nil {
		t.Fatal(err)
	}
	var clearedID *string
	if err = s.DB.QueryRow(ctx, `SELECT category, category_id, review_status, policy_version FROM sources WHERE source_id=$1`, id).Scan(&label, &clearedID, &status, &version); err != nil || label != "" || clearedID != nil || status != "pending" || version != 1 {
		t.Fatalf("detach changed source policy: label=%q id=%v status=%s version=%d err=%v", label, clearedID, status, version, err)
	}
	if err = s.DeleteCategory(ctx, original.ID, false); err != nil {
		t.Fatal(err)
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
