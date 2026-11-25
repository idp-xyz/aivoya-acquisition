package httpapi

import (
	"crypto/subtle"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/aivoya/acquisition/internal/core"
	"github.com/aivoya/acquisition/internal/store"
	"github.com/jackc/pgx/v5"
)

type Server struct {
	Store                   *store.Store
	AdminToken, WorkerToken string
	Logger                  *slog.Logger
}

func (s Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { send(w, 200, map[string]any{"status": "ok"}) })
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		if e := s.Store.DB.Ping(r.Context()); e != nil {
			send(w, 503, map[string]string{"status": "unavailable"})
			return
		}
		send(w, 200, map[string]string{"status": "ready"})
	})
	mux.Handle("GET /v1/stats", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { v, e := s.Store.Stats(r.Context()); respond(w, v, e) })))
	mux.Handle("GET /v1/operations/hourly-results", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		v, e := s.Store.HourlyResults(r.Context())
		respond(w, v, e)
	})))
	mux.Handle("GET /v1/operations/category-yield", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		v, e := s.Store.CategoryYield(r.Context())
		respond(w, v, e)
	})))
	mux.Handle("GET /v1/operations/source-health", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		v, e := s.Store.SourceHealth(r.Context())
		respond(w, v, e)
	})))
	mux.Handle("GET /v1/categories", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		v, e := s.Store.ListCategories(r.Context())
		respond(w, v, e)
	})))
	mux.Handle("POST /v1/categories", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Name string `json:"name"`
		}
		if !decode(w, r, &req) {
			return
		}
		v, e := s.Store.CreateCategory(r.Context(), req.Name)
		respond(w, v, e)
	})))
	mux.Handle("POST /v1/categories/reorder", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			CategoryIDs []string `json:"category_ids"`
		}
		if !decode(w, r, &req) {
			return
		}
		e := s.Store.ReorderCategories(r.Context(), req.CategoryIDs)
		respond(w, map[string]any{"ordered": len(req.CategoryIDs)}, e)
	})))
	mux.Handle("POST /v1/categories/{id}/rename", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Name string `json:"name"`
		}
		if !decode(w, r, &req) {
			return
		}
		e := s.Store.RenameCategory(r.Context(), r.PathValue("id"), req.Name)
		respond(w, map[string]any{"category_id": r.PathValue("id"), "name": req.Name}, e)
	})))
	mux.Handle("DELETE /v1/categories/{id}", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		detach := r.URL.Query().Get("detach") == "true"
		e := s.Store.DeleteCategory(r.Context(), r.PathValue("id"), detach)
		respond(w, map[string]any{"deleted": r.PathValue("id"), "detached_sources": detach}, e)
	})))
	mux.Handle("POST /v1/sources/{id}/category", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			CategoryID string `json:"category_id"`
		}
		if !decode(w, r, &req) {
			return
		}
		e := s.Store.AssignCategory(r.Context(), r.PathValue("id"), req.CategoryID)
		respond(w, map[string]any{"source_id": r.PathValue("id"), "category_id": req.CategoryID}, e)
	})))
	mux.Handle("GET /v1/sources", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		v, e := s.Store.ListSources(r.Context(), 500)
		respond(w, v, e)
	})))
	mux.Handle("POST /v1/sources/import", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Sources []core.Source `json:"sources"`
		}
		if !decode(w, r, &req) {
			return
		}
		n, e := s.Store.Register(r.Context(), req.Sources)
		respond(w, map[string]any{"imported": n, "all_pending_by_default": true}, e)
	})))
	mux.Handle("POST /v1/sources/{id}/configure", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			OriginURL       string `json:"origin_url"`
			EndpointURL     string `json:"endpoint_url"`
			AccessMode      string `json:"access_mode"`
			IntervalMinutes int    `json:"interval_minutes"`
			Reviewer        string `json:"reviewer"`
			Basis           string `json:"basis"`
		}
		if !decode(w, r, &req) {
			return
		}
		e := s.Store.UpdateConfig(r.Context(), r.PathValue("id"), req.OriginURL, req.EndpointURL, req.AccessMode, req.IntervalMinutes, req.Reviewer, req.Basis)
		respond(w, map[string]any{"status": "pending_review", "source_id": r.PathValue("id")}, e)
	})))
	mux.Handle("POST /v1/sources/{id}/approve", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Rights   string `json:"rights"`
			Enabled  bool   `json:"enabled"`
			Reviewer string `json:"reviewer"`
			Basis    string `json:"basis"`
		}
		if !decode(w, r, &req) {
			return
		}
		e := s.Store.Approve(r.Context(), r.PathValue("id"), req.Rights, req.Enabled, req.Reviewer, req.Basis)
		respond(w, map[string]any{"status": "approved", "source_id": r.PathValue("id")}, e)
	})))
	mux.Handle("POST /v1/sources/{id}/deny", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Reviewer string `json:"reviewer"`
			Basis    string `json:"basis"`
		}
		if !decode(w, r, &req) {
			return
		}
		e := s.Store.Deny(r.Context(), r.PathValue("id"), req.Reviewer, req.Basis)
		respond(w, map[string]any{"status": "denied"}, e)
	})))
	mux.Handle("POST /v1/sources/{id}/enqueue", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id, e := s.Store.Enqueue(r.Context(), r.PathValue("id"))
		respond(w, map[string]any{"job_id": id}, e)
	})))
	mux.Handle("GET /v1/jobs", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		v, e := s.Store.ListJobs(r.Context(), pageLimit(r))
		respond(w, v, e)
	})))
	mux.Handle("GET /v1/records", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		v, e := s.Store.ListRecords(r.Context(), pageLimit(r))
		respond(w, v, e)
	})))
	mux.Handle("GET /v1/records/search", s.auth(false, http.HandlerFunc(s.searchRecords)))
	mux.Handle("GET /v1/records/{id}", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		v, e := s.Store.GetRecord(r.Context(), r.PathValue("id"))
		respond(w, v, e)
	})))
	mux.Handle("GET /v1/jobs/{id}/events", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		v, e := s.Store.JobEvents(r.Context(), r.PathValue("id"))
		respond(w, v, e)
	})))
	mux.Handle("GET /v1/sources/{id}/policy-events", s.auth(false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		v, e := s.Store.PolicyEvents(r.Context(), r.PathValue("id"))
		respond(w, v, e)
	})))
	mux.Handle("POST /v1/jobs/claim", s.auth(true, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		v, e := s.Store.Claim(r.Context())
		if e != nil {
			fail(w, e)
			return
		}
		if v == nil {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		send(w, 200, v)
	})))
	mux.Handle("POST /v1/jobs/complete", s.auth(true, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req core.Completion
		if !decode(w, r, &req) {
			return
		}
		status, e := s.Store.Complete(r.Context(), req)
		respond(w, map[string]any{"status": status, "job_id": req.JobID}, e)
	})))
	mux.HandleFunc("GET /console", serveConsole)
	mux.HandleFunc("GET /pulse", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/console", http.StatusFound)
	})
	return requestLog(mux, s.Logger)
}
func (s Server) searchRecords(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	var since, until *time.Time
	if hour := strings.TrimSpace(q.Get("hour")); hour != "" {
		parsed, err := time.Parse(time.RFC3339Nano, hour)
		if err != nil {
			parsed, err = time.Parse(time.RFC3339, hour)
		}
		if err != nil {
			respond(w, nil, errors.New("invalid hour"))
			return
		}
		start := parsed.UTC().Truncate(time.Hour)
		end := start.Add(time.Hour)
		since, until = &start, &end
	} else {
		switch q.Get("range") {
		case "", "24h":
			start := time.Now().Add(-24 * time.Hour)
			since = &start
		case "7d":
			start := time.Now().Add(-7 * 24 * time.Hour)
			since = &start
		case "all":
		default:
			respond(w, nil, errors.New("invalid range"))
			return
		}
	}
	title := strings.TrimSpace(q.Get("q"))
	if len([]rune(title)) > 80 {
		respond(w, nil, errors.New("invalid query"))
		return
	}
	var before *time.Time
	beforeID := strings.TrimSpace(q.Get("before_id"))
	if raw := strings.TrimSpace(q.Get("before")); raw != "" {
		parsed, err := time.Parse(time.RFC3339Nano, raw)
		if err != nil {
			parsed, err = time.Parse(time.RFC3339, raw)
		}
		if err != nil || beforeID == "" {
			respond(w, nil, errors.New("invalid cursor"))
			return
		}
		before = &parsed
	} else if beforeID != "" {
		respond(w, nil, errors.New("invalid cursor"))
		return
	}
	page, err := s.Store.SearchRecords(r.Context(), since, until, strings.TrimSpace(q.Get("source_id")), strings.TrimSpace(q.Get("category_id")), title, before, beforeID, 50)
	respond(w, page, err)
}

func pageLimit(r *http.Request) int {
	n, e := strconv.Atoi(r.URL.Query().Get("limit"))
	if e != nil || n < 1 {
		return 40
	}
	if n > 200 {
		return 200
	}
	return n
}
func (s Server) auth(worker bool, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		tok := s.AdminToken
		if worker {
			tok = s.WorkerToken
		}
		b := r.Header.Get("Authorization")
		if !strings.HasPrefix(b, "Bearer ") || subtle.ConstantTimeCompare([]byte(strings.TrimPrefix(b, "Bearer ")), []byte(tok)) != 1 {
			send(w, 401, map[string]string{"error": "unauthorized"})
			return
		}
		next.ServeHTTP(w, r)
	})
}
func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 4<<20)
	d := json.NewDecoder(r.Body)
	d.DisallowUnknownFields()
	if e := d.Decode(v); e != nil {
		send(w, 400, map[string]string{"error": "invalid JSON: " + e.Error()})
		return false
	}
	if err := d.Decode(new(any)); !errors.Is(err, io.EOF) {
		send(w, 400, map[string]string{"error": "unexpected trailing JSON"})
		return false
	}
	return true
}
func respond(w http.ResponseWriter, v any, e error) {
	if e != nil {
		fail(w, e)
		return
	}
	send(w, 200, v)
}
func fail(w http.ResponseWriter, e error) {
	switch {
	case errors.Is(e, pgx.ErrNoRows):
		send(w, 404, map[string]string{"error": "not found or stale lease"})
	case strings.Contains(e.Error(), "stale/unknown lease"):
		send(w, 409, map[string]string{"error": "stale lease"})
	case strings.Contains(e.Error(), "constraint"):
		send(w, 409, map[string]string{"error": "constraint violation"})
	default:
		if isInvalid(e) {
			send(w, 400, map[string]string{"error": e.Error()})
		} else {
			send(w, 500, map[string]string{"error": "internal error"})
		}
	}
}
func isInvalid(e error) bool {
	t := e.Error()
	for _, v := range []string{"invalid", "not approved", "requires dedicated", "active job exists", "unknown completion", "missing", "mismatch", "non-2xx", "raw snapshot", "redirect changed", "unsupported schema", "private", "not allowed", "non-standard", "URL must", "bulk limit", "quality", "reviewer and basis", "category", "interval"} {
		if strings.Contains(t, v) {
			return true
		}
	}
	return false
}
func send(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
func requestLog(next http.Handler, logger *slog.Logger) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t := time.Now()
		next.ServeHTTP(w, r)
		if logger != nil {
			logger.Info("http", "method", r.Method, "path", r.URL.Path, "elapsed_ms", time.Since(t).Milliseconds())
		}
	})
}
func (s Server) String() string { return fmt.Sprintf("acquisition-api") }

//go:embed console.html
var consoleHTML embed.FS

func serveConsole(w http.ResponseWriter, r *http.Request) {
	content, e := consoleHTML.ReadFile("console.html")
	if e != nil {
		http.Error(w, "console unavailable", 500)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("X-Frame-Options", "DENY")
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write(content)
}
