package onvif

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/icholy/digest"
)

func TestSnapshotDigest(t *testing.T) {
	s := testServer(t)
	s.opts.SnapshotFunc = func(context.Context, string) ([]byte, string, error) { return []byte("JPEG"), "image/jpeg", nil }
	const uri = "/onvif/snapshot?token=profile_main"
	fetch := func(uri, auth string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(http.MethodGet, uri, nil)
		r.Header.Set("Authorization", auth)
		w := httptest.NewRecorder()
		s.mux.ServeHTTP(w, r)
		return w
	}
	first := fetch(uri, "")
	chal, err := digest.FindChallenge(first.Header())
	if first.Code != 401 || err != nil {
		t.Fatalf("missing digest challenge: status %d, err %v", first.Code, err)
	}
	credentials := func(pass string, count int) string {
		c, err := digest.Digest(chal, digest.Options{Method: "GET", URI: uri, Username: "admin", Password: pass, Cnonce: "client", Count: count})
		if err != nil {
			t.Fatal(err)
		}
		return c.String()
	}
	valid := credentials("secret", 1)
	for _, tt := range []struct {
		uri, auth string
		status    int
	}{
		{uri, credentials("incorrect", 1), 401},
		{"/onvif/snapshot?token=profile_sub", valid, 401},
		{uri, valid, 200},
		{uri, valid, 401},
		{uri, credentials("secret", 2), 200},
	} {
		if got := fetch(tt.uri, tt.auth); got.Code != tt.status {
			t.Errorf("snapshot status %d, want %d", got.Code, tt.status)
		}
	}
	s.digest.issued[chal.Nonce] = snapshotNonce{expires: time.Now().Add(-time.Second)}
	if fetch(uri, credentials("secret", 3)).Code != 401 {
		t.Error("expired nonce accepted")
	}
}
