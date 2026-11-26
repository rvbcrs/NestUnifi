package onvif

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/Aiaid/onvif-proxy/internal/soap"
)

func TestMotionPullPoints(t *testing.T) {
	const path = "/onvif/events_service"
	eventCall := func(s *Server, path, body string) string {
		t.Helper()
		r := post(t, s, path, soapEnv(wsseHeader("admin", "secret"), body))
		assertValidXML(t, r.Body.Bytes())
		return r.Body.String()
	}
	first, second := testServer(t), testServer(t)
	create := `<CreatePullPointSubscription xmlns="http://www.onvif.org/ver10/events/wsdl"><Filter><TopicExpression>tns1:VideoSource/MotionAlarm</TopicExpression></Filter></CreatePullPointSubscription>`
	if r := post(t, first, path, soapEnv("", create)); !strings.Contains(r.Body.String(), "NotAuthorized") {
		t.Fatal("unprotected event subscription")
	}
	points := make([]string, 0, 2)
	for _, camera := range []*Server{first, second} {
		ref, ok := soap.ExtractElement([]byte(eventCall(camera, path, create)), "Address")
		address, err := url.Parse(ref)
		if !ok || err != nil || address.Host != "cam.local:8081" || address.Query().Get("sub") == "" {
			t.Fatalf("invalid pull point %q", ref)
		}
		points = append(points, address.RequestURI())
	}
	pull := `<PullMessages xmlns="http://www.onvif.org/ver10/events/wsdl"><Timeout>PT0S</Timeout><MessageLimit>1</MessageLimit></PullMessages>`
	for i, camera := range []*Server{first, second} {
		response := eventCall(camera, points[i], pull)
		if !strings.Contains(response, `PropertyOperation="Initialized"`) || !strings.Contains(response, `Name="State" Value="false"`) {
			t.Fatal("missing initial idle state")
		}
	}
	ln, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go first.ServeTrigger(ctx, ln)
	motion := func() (state struct {
		Active    bool
		Last      string
		Detected  map[string]string
		Listeners int
		Delivered string
		Viewers   int
	}) {
		t.Helper()
		r, err := http.Get("http://" + ln.Addr().String() + "/trigger/motion")
		if err != nil {
			t.Fatal(err)
		}
		defer r.Body.Close()
		if err := json.NewDecoder(r.Body).Decode(&state); err != nil {
			t.Fatal(err)
		}
		return state
	}
	trigger := func(query string) int {
		t.Helper()
		r, err := http.Post("http://"+ln.Addr().String()+"/trigger/"+query, "text/plain", nil)
		if err != nil {
			t.Fatal(err)
		}
		r.Body.Close()
		return r.StatusCode
	}
	if state := motion(); state.Active || state.Last != "" || state.Listeners != 1 || state.Delivered != "" || len(state.Detected) != 0 {
		t.Fatal("unexpected state before any trigger", state)
	}
	// The admin page's test is a motion the recorder hears, but not a detection.
	if trigger("motion?state=test") != 200 || trigger("motion?state=test") != 409 {
		t.Fatal("test motion not started once")
	}
	if state := motion(); !state.Active || state.Last != "" {
		t.Fatal("test motion counted as a detection", state)
	}
	if response := eventCall(first, points[0], pull); !strings.Contains(response, `Name="State" Value="true"`) {
		t.Fatal("test motion not delivered", response)
	}
	if state := motion(); state.Delivered == "" {
		t.Fatal("delivery not recorded", state)
	}
	first.events.mu.Lock()
	first.events.testing = false // as a real "off" does; the test's own timer then leaves the level alone
	first.events.set(false, time.Now())
	first.events.mu.Unlock()
	if response := eventCall(first, points[0], pull); !strings.Contains(response, `Name="State" Value="false"`) {
		t.Fatal("end of test motion not delivered", response)
	}
	// A recorder that opens the video stream is counted until it closes it.
	stream, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer stream.Close()
	counted := first.Watched(stream)
	dialed, err := net.Dial("tcp4", stream.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	accepted, err := counted.Accept()
	if err != nil {
		t.Fatal(err)
	}
	if state := motion(); state.Viewers != 1 {
		t.Fatal("open stream not counted", state)
	}
	accepted.Close()
	accepted.Close()
	dialed.Close()
	if state := motion(); state.Viewers != 0 {
		t.Fatal("closed stream still counted", state)
	}
	first.opts.SnapshotFunc = func(context.Context, string) ([]byte, string, error) { return []byte("JPEG"), "image/jpeg", nil }
	still, err := http.Get("http://" + ln.Addr().String() + "/trigger/snapshot")
	if err != nil {
		t.Fatal(err)
	}
	still.Body.Close()
	if still.StatusCode != 200 || still.Header.Get("Content-Type") != "image/jpeg" {
		t.Fatal("no still on the private listener", still.Status)
	}
	if trigger("event?kind=chime") != 200 || trigger("event?kind=earthquake") != 400 {
		t.Fatal("detection kinds not checked")
	}
	if state := motion(); state.Detected["chime"] == "" || len(state.Detected) != 1 {
		t.Fatal("detection kind not recorded", state)
	}
	for _, state := range []string{"on", "on", "off"} {
		if code := trigger("motion?state=" + state); code != 200 {
			t.Fatal(code)
		}
	}
	if state := motion(); state.Active || state.Last == "" {
		t.Fatal("last motion not recorded", state)
	}
	for _, expected := range []string{"true", "false"} {
		response := eventCall(first, points[0], pull)
		if strings.Count(response, "<wsnt:NotificationMessage>") != 1 || !strings.Contains(response, `Name="State" Value="`+expected+`"`) || !strings.Contains(response, `PropertyOperation="Changed"`) {
			t.Fatal("unexpected motion transition", response)
		}
	}
	for i, camera := range []*Server{first, second} {
		if strings.Contains(eventCall(camera, points[i], pull), "<wsnt:NotificationMessage>") {
			t.Fatal("duplicate or foreign motion")
		}
	}
	if !strings.Contains(eventCall(first, points[0], "<Renew><TerminationTime>PT45S</TerminationTime></Renew>"), "RenewResponse") {
		t.Fatal("renew failed")
	}
	if !strings.Contains(eventCall(first, points[0], "<Unsubscribe/>"), "UnsubscribeResponse") {
		t.Fatal("unsubscribe failed")
	}
	if !strings.Contains(eventCall(first, points[0], pull), "Fault") {
		t.Fatal("unsubscribed point accepted")
	}
	if r := post(t, first, "/trigger/motion?state=on", ""); r.Code != 404 {
		t.Fatal("trigger reachable over camera's public router")
	}
	public := httptest.NewRecorder()
	first.mux.ServeHTTP(public, httptest.NewRequest("GET", "/trigger/motion", nil))
	if public.Code != 404 {
		t.Fatal("motion state reachable over camera's public router")
	}
}
