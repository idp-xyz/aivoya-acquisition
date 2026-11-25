package core

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/url"
	"regexp"
	"strings"
	"time"
)

type Source struct {
	ID                   string `json:"source_id"`
	Name                 string `json:"name"`
	OriginURL            string `json:"origin_url"`
	EndpointURL          string `json:"endpoint_url"`
	Region               string `json:"region"`
	Language             string `json:"language"`
	Category             string `json:"category"`
	CategoryID           string `json:"category_id"`
	SourceMode           string `json:"source_mode"`
	OnboardingBatch      string `json:"onboarding_batch"`
	EndpointVerification string `json:"endpoint_verification"`
	EndpointHint         string `json:"endpoint_hint"`
	IngestionNotes       string `json:"ingestion_notes"`
	AccessMode           string `json:"access_mode"`
	Rights               string `json:"rights"`
	ReviewStatus         string `json:"review_status"`
	PolicyVersion        int    `json:"policy_version"`
	Enabled              bool   `json:"enabled"`
	IntervalMinutes      int    `json:"interval_minutes"`
}

type Category struct {
	ID        string `json:"category_id"`
	Name      string `json:"name"`
	SortOrder int    `json:"sort_order"`
	Sources   int    `json:"sources"`
}

type FeedEntry struct {
	URL         string     `json:"url"`
	Title       string     `json:"title"`
	PublishedAt *time.Time `json:"published_at,omitempty"`
}

type Completion struct {
	SchemaVersion     string      `json:"schema_version"`
	JobID             string      `json:"job_id"`
	LeaseToken        string      `json:"lease_token"`
	Status            string      `json:"status"` // success, retryable_error, blocked, permanent_error
	ErrorCode         string      `json:"error_code,omitempty"`
	Error             string      `json:"error,omitempty"`
	MethodUsed        string      `json:"method_used,omitempty"`
	HTTPStatus        int         `json:"http_status,omitempty"`
	FinalURL          string      `json:"final_url,omitempty"`
	AcquiredAt        time.Time   `json:"acquired_at"`
	RawBase64         string      `json:"raw_base64,omitempty"`
	MediaType         string      `json:"media_type,omitempty"`
	Title             string      `json:"title,omitempty"`
	BodyText          string      `json:"body_text,omitempty"`
	Language          string      `json:"language,omitempty"`
	PublishedAt       *time.Time  `json:"published_at,omitempty"`
	ExtractorName     string      `json:"extractor_name,omitempty"`
	ExtractorVersion  string      `json:"extractor_version,omitempty"`
	Entries           []FeedEntry `json:"entries,omitempty"`
	DurationMS        int64       `json:"duration_ms,omitempty"`
	RetryAfterSeconds int         `json:"retry_after_seconds,omitempty"`
}

type Claim struct {
	SchemaVersion string `json:"schema_version"`
	JobID         string `json:"job_id"`
	SourceID      string `json:"source_id"`
	URL           string `json:"url"`
	OriginURL     string `json:"origin_url,omitempty"`
	Method        string `json:"method"`
	Rights        string `json:"rights"`
	PolicyVersion int    `json:"policy_version"`
	LeaseToken    string `json:"lease_token"`
	Attempt       int    `json:"attempt"`
	DeadlineMS    int    `json:"deadline_ms"`
}

var sourceIDRE = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{1,94}$`)
var trackingKeys = map[string]bool{"utm_source": true, "utm_medium": true, "utm_campaign": true, "utm_term": true, "utm_content": true, "gclid": true, "fbclid": true, "mc_cid": true, "mc_eid": true}

func ValidatePublicURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || u == nil || !u.IsAbs() || (u.Scheme != "https" && u.Scheme != "http") || u.Hostname() == "" || u.User != nil || u.Fragment != "" {
		return errors.New("URL must be an absolute http(s) URL without credentials or fragment")
	}
	h := strings.TrimSuffix(strings.ToLower(u.Hostname()), ".")
	if h == "localhost" || strings.HasSuffix(h, ".localhost") || strings.HasSuffix(h, ".local") || strings.HasSuffix(h, ".internal") {
		return errors.New("internal host is not allowed")
	}
	if ip := net.ParseIP(h); ip != nil && (!ip.IsGlobalUnicast() || ip.IsPrivate()) {
		return errors.New("private or non-global IP not allowed")
	}
	if u.Port() != "" && u.Port() != "80" && u.Port() != "443" {
		return errors.New("non-standard ports are not allowed")
	}
	return nil
}
func CanonicalURL(raw string) (string, error) {
	if err := ValidatePublicURL(raw); err != nil {
		return "", err
	}
	u, _ := url.Parse(raw)
	u.Host = strings.ToLower(u.Host)
	u.Fragment = ""
	if (u.Scheme == "https" && u.Port() == "443") || (u.Scheme == "http" && u.Port() == "80") {
		u.Host = u.Hostname()
	}
	q := u.Query()
	for k := range q {
		if trackingKeys[strings.ToLower(k)] {
			q.Del(k)
		}
	}
	u.RawQuery = q.Encode()
	return u.String(), nil
}

// CheckGitHubReleasesURL validates the exact official public REST endpoint.
// No arbitrary API target is granted access through this connector.
func CheckGitHubReleasesURL(raw string) error {
	if err := ValidatePublicURL(raw); err != nil {
		return err
	}
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "https" || !strings.EqualFold(u.Hostname(), "api.github.com") || u.Port() != "" {
		return errors.New("github_releases requires https://api.github.com")
	}
	parts := strings.Split(strings.Trim(u.Path, "/"), "/")
	if len(parts) != 4 || parts[0] != "repos" || parts[3] != "releases" {
		return errors.New("github_releases requires /repos/{owner}/{repo}/releases")
	}
	repoPart := regexp.MustCompile(`^[A-Za-z0-9_.-]{1,100}$`)
	if !repoPart.MatchString(parts[1]) || !repoPart.MatchString(parts[2]) {
		return errors.New("invalid GitHub repository identifier")
	}
	for key, vals := range u.Query() {
		if key != "per_page" && key != "page" {
			return errors.New("unsupported GitHub query parameter")
		}
		if len(vals) != 1 {
			return errors.New("invalid GitHub pagination")
		}
		maxValue := 100
		if key == "page" {
			maxValue = 10000
		}
		n := 0
		if len(vals[0]) == 0 {
			return errors.New("invalid GitHub pagination")
		}
		for _, ch := range vals[0] {
			if ch < '0' || ch > '9' {
				return errors.New("invalid GitHub pagination")
			}
			n = n*10 + int(ch-'0')
			if n > maxValue {
				return errors.New("invalid GitHub pagination")
			}
		}
		if n < 1 {
			return errors.New("invalid GitHub pagination")
		}
	}
	return nil
}

func CheckSource(s Source) error {
	if !sourceIDRE.MatchString(s.ID) || strings.TrimSpace(s.Name) == "" {
		return errors.New("invalid source ID/name")
	}
	if err := ValidatePublicURL(s.OriginURL); err != nil {
		return fmt.Errorf("origin: %w", err)
	}
	if err := ValidatePublicURL(s.EndpointURL); err != nil {
		return fmt.Errorf("endpoint: %w", err)
	}
	switch s.AccessMode {
	case "rss", "html_index", "monitor", "html_static", "browser_render", "github_releases", "api", "manual":
	default:
		return errors.New("invalid access mode")
	}
	switch s.Rights {
	case "metadata_only", "extract_only", "full_text_licensed", "prohibited":
	default:
		return errors.New("invalid rights")
	}
	if s.AccessMode == "github_releases" {
		if err := CheckGitHubReleasesURL(s.EndpointURL); err != nil {
			return err
		}
	}
	if s.IntervalMinutes < 5 || s.IntervalMinutes > 10080 {
		return errors.New("interval must be 5..10080 minutes")
	}
	return nil
}
func ID(prefix string) string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	return prefix + hex.EncodeToString(b[:])
}

func UUID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%s-%s-%s-%s-%s", hex.EncodeToString(b[0:4]), hex.EncodeToString(b[4:6]), hex.EncodeToString(b[6:8]), hex.EncodeToString(b[8:10]), hex.EncodeToString(b[10:]))
}
func Score(title, body string) float64 {
	// Deterministic heuristic only; never mislabel this as article correctness.
	title = strings.TrimSpace(title)
	body = strings.TrimSpace(body)
	if len([]rune(title)) < 5 || len([]rune(body)) < 120 {
		return 0
	}
	lower := strings.ToLower(body)
	for _, blocked := range []string{"verify you are human", "checking your browser", "enable javascript to continue", "captcha", "access denied"} {
		if strings.Contains(lower, blocked) {
			return 0
		}
	}
	score := 0.55
	n := len([]rune(body))
	if n >= 300 {
		score += 0.15
	}
	if n >= 800 {
		score += 0.15
	}
	if n >= 1800 {
		score += 0.1
	}
	return score
}
