package store

import (
	"context"
	"encoding/base64"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/aivoya/acquisition/internal/core"
)

// Requires a dedicated, empty PostgreSQL 16 test database.
// TEST_DATABASE_URL=... go test ./internal/store -run Integration -v
func TestIntegrationLifecycle(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	s, err := Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer s.DB.Close()
	if err := s.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	id := "it-" + core.ID("")[:16]
	n, err := s.Register(ctx, []core.Source{{ID: id, Name: "Integration Test Publisher", OriginURL: "https://example.com/news", EndpointURL: "https://example.com/news/example-story", AccessMode: "html_static", Rights: "metadata_only", IntervalMinutes: 30}})
	if err != nil || n != 1 {
		t.Fatalf("register %d %v", n, err)
	}
	if _, err = s.Enqueue(ctx, id); err == nil {
		t.Fatal("unreviewed source was enqueued")
	}
	if err = s.Approve(ctx, id, "full_text_licensed", true, "qa-engineer", "test rights proof 123456"); err != nil {
		t.Fatal(err)
	}
	jobID, err := s.Enqueue(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	claim, err := s.Claim(ctx)
	if err != nil || claim == nil {
		t.Fatalf("claim %v", err)
	}
	if claim.JobID != jobID {
		t.Fatalf("claimed %s want %s; run against an empty test database", claim.JobID, jobID)
	}
	body := strings.Repeat("Evidence-based article testing demonstrates deterministic processing and traceable source data. ", 12)
	completed := core.Completion{SchemaVersion: "1.0", JobID: jobID, LeaseToken: claim.LeaseToken, Status: "success", MethodUsed: "html_static", HTTPStatus: 200, FinalURL: "https://example.com/news/example-story", AcquiredAt: time.Now().UTC(), MediaType: "text/html", RawBase64: base64.StdEncoding.EncodeToString([]byte("<html><body>" + body + "</body></html>")), Title: "Evidence based engineering report", BodyText: body, Language: "en", ExtractorName: "test-extractor", ExtractorVersion: "1.0"}
	state, err := s.Complete(ctx, completed)
	if err != nil || state != "succeeded" {
		t.Fatalf("completion %s %v", state, err)
	}
	if _, err = s.Complete(ctx, completed); err == nil {
		t.Fatal("replayed lease was accepted")
	}
	st, err := s.Stats(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if st["snapshots"].(int64) < 1 || st["records"].(int64) < 1 {
		t.Fatalf("unexpected stats %+v", st)
	}
	if err = s.Deny(ctx, id, "qa-engineer", "test rights revoked 123456"); err != nil {
		t.Fatal(err)
	}
	var retained int
	if err = s.DB.QueryRow(ctx, `SELECT count(*) FROM snapshots sn JOIN jobs j ON j.job_id=sn.job_id WHERE j.source_id=$1`, id).Scan(&retained); err != nil || retained != 0 {
		t.Fatalf("revocation failed to purge raw data %d %v", retained, err)
	}
}
