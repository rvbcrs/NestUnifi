package onvif

import (
	"context"
	"net"
	"net/http"
	"net/url"
	"strings"
	"testing"

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
	for _, state := range []string{"on", "on", "off"} {
		r, err := http.Post("http://"+ln.Addr().String()+"/trigger/motion?state="+state, "text/plain", nil)
		if err != nil {
			t.Fatal(err)
		}
		r.Body.Close()
		if r.StatusCode != 200 {
			t.Fatal(r.Status)
		}
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
}
