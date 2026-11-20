package core

import "testing"

func TestCanonicalURL(t *testing.T) {
	got, e := CanonicalURL("https://EXAMPLE.com/a?utm_source=x&b=1#frag")
	if e == nil {
		t.Fatal("must reject fragment")
	}
	got, e = CanonicalURL("https://EXAMPLE.com/a?utm_source=x&b=1")
	if e != nil || got != "https://example.com/a?b=1" {
		t.Fatalf("%q %v", got, e)
	}
}
func TestDisallowedURL(t *testing.T) {
	for _, u := range []string{"http://127.0.0.1/x", "http://10.0.0.1/", "file:///tmp/foo", "http://localhost/a", "http://169.254.169.254/", "http://user:pass@example.com/"} {
		if ValidatePublicURL(u) == nil {
			t.Errorf("allowed %s", u)
		}
	}
}
func TestScore(t *testing.T) {
	if Score("Hi", "test") != 0 {
		t.Fatal("short article passed")
	}
	if Score("Sample title", "verify you are human "+string(make([]byte, 250))) != 0 {
		t.Fatal("blocked challenge page passed")
	}
}

func TestGitHubReleasesEndpoint(t *testing.T) {
	valid := []string{
		"https://api.github.com/repos/D4Vinci/Scrapling/releases",
		"https://api.github.com/repos/org/repo/releases?per_page=20",
	}
	for _, v := range valid {
		if e := CheckGitHubReleasesURL(v); e != nil {
			t.Fatalf("rejected %q: %v", v, e)
		}
	}
	invalid := []string{
		"https://github.com/D4Vinci/Scrapling/releases",
		"https://api.github.com.evil.test/repos/org/repo/releases",
		"http://api.github.com/repos/org/repo/releases",
		"https://api.github.com/repos/org/repo/issues",
		"https://api.github.com/repos/org/repo/releases?access_token=s",
		"https://api.github.com/repos/org/repo/releases?per_page=9999",
	}
	for _, v := range invalid {
		if CheckGitHubReleasesURL(v) == nil {
			t.Errorf("accepted %q", v)
		}
	}
}
