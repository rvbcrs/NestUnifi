package onvif

import (
	"crypto/rand"
	"crypto/subtle"
	"net/http"
	"sync"
	"time"

	"github.com/icholy/digest"
)

const snapshotRealm = "ONVIF Snapshot"

// Protect uses the camera's Digest challenge when fetching an event thumbnail.
type snapshotDigest struct {
	mu     sync.Mutex
	issued map[string]snapshotNonce
}

type snapshotNonce struct {
	expires time.Time
	count   int
}

func (d *snapshotDigest) challenge() *digest.Challenge {
	d.mu.Lock()
	defer d.mu.Unlock()

	if d.issued == nil {
		d.issued = make(map[string]snapshotNonce)
	}
	now := time.Now()
	var oldest string
	for key, n := range d.issued {
		if !now.Before(n.expires) {
			delete(d.issued, key)
		} else if oldest == "" || n.expires.Before(d.issued[oldest].expires) {
			oldest = key
		}
	}
	if len(d.issued) >= 256 {
		delete(d.issued, oldest)
	}
	nonce := rand.Text()
	d.issued[nonce] = snapshotNonce{expires: now.Add(2 * time.Minute)}
	return &digest.Challenge{Realm: snapshotRealm, Algorithm: "MD5", QOP: []string{"auth"}, Nonce: nonce}
}

func (d *snapshotDigest) verify(r *http.Request, username, password string) bool {
	c, err := digest.ParseCredentials(r.Header.Get("Authorization"))
	if err != nil || c.Username != username || c.Realm != snapshotRealm ||
		c.URI != r.URL.RequestURI() || (c.Algorithm != "" && c.Algorithm != "MD5") ||
		c.QOP != "auth" || c.Cnonce == "" || c.Nc < 1 {
		return false
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	n, ok := d.issued[c.Nonce]
	if !ok || !time.Now().Before(n.expires) || c.Nc <= n.count {
		return false
	}
	want, err := digest.Digest(&digest.Challenge{Realm: snapshotRealm, Nonce: c.Nonce, Algorithm: "MD5", QOP: []string{"auth"}}, digest.Options{
		Method: r.Method, URI: c.URI, Username: username, Password: password, Cnonce: c.Cnonce, Count: c.Nc,
	})
	if err != nil || subtle.ConstantTimeCompare([]byte(want.Response), []byte(c.Response)) != 1 {
		return false
	}
	n.count = c.Nc
	d.issued[c.Nonce] = n
	return true
}
