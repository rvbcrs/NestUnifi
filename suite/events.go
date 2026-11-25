// Compiled into the pinned ONVIF proxy by build-camera.sh. Each virtual device
// owns its motion state, pull points and private bridge listener.
package onvif

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Aiaid/onvif-proxy/internal/soap"
)

const eventNS = "http://www.onvif.org/ver10/events/wsdl"
const concreteSet = "http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet"
const maxPullPoints = 16
const queueLimit = 192
const subscriptionLife = 5 * time.Minute
const eventCaps = `<tev:Capabilities WSPullPointSupport="true" WSSubscriptionPolicySupport="false" WSPausableSubscriptionManagerInterfaceSupport="false" MaxPullPoints="16" PersistentNotificationStorage="false"/>`

type motionTopic struct{ path, field, rule string }

var motionTopics = []motionTopic{
	{"VideoSource/MotionAlarm", "State", ""},
	{"RuleEngine/CellMotionDetector/Motion", "IsMotion", "CellMotionDetector"},
	{"RuleEngine/MotionRegionDetector/Motion", "IsMotion", "MotionRegionDetector"},
}

func (t motionTopic) source(description bool) string {
	item := func(name, value, kind string) string {
		if description {
			return fmt.Sprintf(`<tt:SimpleItemDescription Name="%s" Type="%s"/>`, name, kind)
		}
		return fmt.Sprintf(`<tt:SimpleItem Name="%s" Value="%s"/>`, name, value)
	}
	if t.rule == "" {
		return item("Source", "src", "tt:ReferenceToken")
	}
	return item("VideoSourceConfigurationToken", "vsc", "tt:ReferenceToken") + item("Rule", t.rule, "xs:string")
}

func topicSet() string {
	var video, rules string
	for _, t := range motionTopics {
		d := `<tt:MessageDescription IsProperty="true"><tt:Source>` + t.source(true) + `</tt:Source><tt:Data><tt:SimpleItemDescription Name="` + t.field + `" Type="xs:boolean"/></tt:Data></tt:MessageDescription>`
		if t.rule == "" {
			video = `<tns1:VideoSource><MotionAlarm wstop:topic="true">` + d + `</MotionAlarm></tns1:VideoSource>`
			continue
		}
		rules += "<" + t.rule + `><Motion wstop:topic="true">` + d + "</Motion></" + t.rule + ">"
	}
	return video + "<tns1:RuleEngine>" + rules + "</tns1:RuleEngine>"
}

func (t motionTopic) notification(active bool, at time.Time, operation string) string {
	return fmt.Sprintf(`<wsnt:NotificationMessage><wsnt:Topic Dialect="%s">tns1:%s</wsnt:Topic><wsnt:Message><tt:Message UtcTime="%s" PropertyOperation="%s"><tt:Source>%s</tt:Source><tt:Data><tt:SimpleItem Name="%s" Value="%t"/></tt:Data></tt:Message></wsnt:Message></wsnt:NotificationMessage>`, concreteSet, t.path, stamp(at), operation, t.source(false), t.field, active)
}

type pullPoint struct {
	expires time.Time
	topics  []motionTopic
	queue   []string
	pulling bool
}

func (p *pullPoint) enqueue(active bool, at time.Time, operation string) {
	for _, t := range p.topics {
		p.queue = append(p.queue, t.notification(active, at, operation))
	}
	if len(p.queue) > queueLimit {
		p.queue = p.queue[len(p.queue)-queueLimit:]
	}
}

type eventState struct {
	mu      sync.Mutex
	active  bool
	since   time.Time // start of the latest motion, for the admin page
	points  map[string]*pullPoint
	changed chan struct{}
}

func (e *eventState) wake() { close(e.changed); e.changed = make(chan struct{}) }

func (s *Server) ServeTrigger(ctx context.Context, ln net.Listener) error {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /trigger/motion", func(w http.ResponseWriter, r *http.Request) {
		var active bool
		switch r.URL.Query().Get("state") {
		case "on", "true":
			active = true
		case "off", "false":
		default:
			http.Error(w, "invalid state", 400)
			return
		}
		e := &s.events
		e.mu.Lock()
		defer e.mu.Unlock()
		if e.active != active {
			e.active = active
			now := time.Now()
			if active {
				e.since = now
			}
			for _, p := range e.points {
				if now.Before(p.expires) {
					p.enqueue(active, now, "Changed")
				}
			}
			e.wake()
		}
		w.WriteHeader(http.StatusOK)
	})
	// Only processes inside the container reach this loopback listener. The admin page reads it.
	mux.HandleFunc("GET /trigger/motion", func(w http.ResponseWriter, r *http.Request) {
		e := &s.events
		e.mu.Lock()
		state := struct {
			Active bool   `json:"active"`
			Last   string `json:"last"`
		}{Active: e.active}
		if !e.since.IsZero() {
			state.Last = stamp(e.since)
		}
		e.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(state)
	})
	srv := s.httpServer()
	srv.Handler = mux
	stop := context.AfterFunc(ctx, func() { _ = srv.Close() })
	defer stop()
	return srv.Serve(ln)
}

func stamp(t time.Time) string { return t.UTC().Format(time.RFC3339Nano) }

func interval(body []byte, name string, fallback, ceiling time.Duration) time.Duration {
	v, ok := soap.ExtractElement(body, name)
	if !ok {
		return fallback
	}
	var d time.Duration
	if absolute, err := time.Parse(time.RFC3339, v); err == nil {
		d = time.Until(absolute)
	} else {
		if !strings.HasPrefix(v, "PT") {
			return fallback
		}
		var err error
		d, err = time.ParseDuration(strings.ToLower(v[2:]))
		if err != nil {
			return fallback
		}
	}
	return min(max(0, d), ceiling)
}

func filterTopics(body []byte) []motionTopic {
	expr, ok := soap.ExtractElement(body, "TopicExpression")
	if !ok {
		return motionTopics
	}
	var selected []motionTopic
	for _, t := range motionTopics {
		for _, part := range strings.Split(expr, "|") {
			_, path, ok := strings.Cut(strings.TrimSpace(part), ":")
			if !ok {
				path = strings.TrimSpace(part)
			}
			if path == t.path || (strings.HasSuffix(path, "//.") && strings.HasPrefix(t.path, strings.TrimSuffix(path, "//.")+"/")) {
				selected = append(selected, t)
				break
			}
		}
	}
	return selected
}

func eventsResponse(w http.ResponseWriter, req *soap.Request, action, body string) {
	w.Header().Set("Content-Type", soap.ContentType)
	const ns = ` xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:wsa="http://www.w3.org/2005/08/addressing" xmlns:tev="http://www.onvif.org/ver10/events/wsdl" xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" xmlns:wstop="http://docs.oasis-open.org/wsn/t-1" xmlns:tns1="http://www.onvif.org/ver10/topics" xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:xs="http://www.w3.org/2001/XMLSchema"`
	header := "<wsa:Action>" + action + "</wsa:Action>"
	if req.MessageID != "" {
		header += "<wsa:RelatesTo>" + soap.XMLEscape(req.MessageID) + "</wsa:RelatesTo>"
	}
	fmt.Fprint(w, "<s:Envelope"+ns+"><s:Header>"+header+"</s:Header><s:Body>"+body+"</s:Body></s:Envelope>")
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	req, err := parse(w, r)
	if err != nil {
		soap.WriteInvalidArg(w, "invalid SOAP")
		return
	}
	if !s.authorized(req, "Events") {
		soap.WriteNotAuthorized(w)
		return
	}
	var body, action string
	defer func() {
		if body != "" {
			eventsResponse(w, req, action, body)
		}
	}()
	e := &s.events
	e.mu.Lock()
	defer e.mu.Unlock()
	now := time.Now()
	for id, p := range e.points {
		if !now.Before(p.expires) && !p.pulling {
			delete(e.points, id)
		}
	}
	id := r.URL.Query().Get("sub")
	p := e.points[id]
	times := func(prefix string) string {
		return "<" + prefix + ":CurrentTime>" + stamp(time.Now()) + "</" + prefix + ":CurrentTime><" + prefix + ":TerminationTime>" + stamp(p.expires) + "</" + prefix + ":TerminationTime>"
	}
	switch req.Action {
	case "GetServiceCapabilities":
		action = eventNS + "/EventPortType/GetServiceCapabilitiesResponse"
		body = "<tev:GetServiceCapabilitiesResponse>" + eventCaps + "</tev:GetServiceCapabilitiesResponse>"
	case "GetEventProperties":
		action = eventNS + "/EventPortType/GetEventPropertiesResponse"
		body = "<tev:GetEventPropertiesResponse><tev:TopicNamespaceLocation>http://www.onvif.org/onvif/ver10/topics/topicns.xml</tev:TopicNamespaceLocation><wsnt:FixedTopicSet>true</wsnt:FixedTopicSet><wstop:TopicSet>" + topicSet() + "</wstop:TopicSet><wsnt:TopicExpressionDialect>" + concreteSet + "</wsnt:TopicExpressionDialect><tev:MessageContentSchemaLocation>http://www.onvif.org/onvif/ver10/schema/onvif.xsd</tev:MessageContentSchemaLocation></tev:GetEventPropertiesResponse>"
	case "CreatePullPointSubscription":
		if len(e.points) >= maxPullPoints {
			soap.WriteFault(w, "Receiver", "ter:MaxNVTProfiles", "pull point limit reached")
			return
		}
		topics := filterTopics(req.Body)
		if len(topics) == 0 {
			soap.WriteInvalidArg(w, "unsupported topic filter")
			return
		}
		var token [16]byte
		rand.Read(token[:])
		id = hex.EncodeToString(token[:])
		p = &pullPoint{expires: now.Add(interval(req.Body, "InitialTerminationTime", subscriptionLife, time.Hour)), topics: topics}
		p.enqueue(e.active, now, "Initialized")
		e.points[id] = p
		log.Printf("ONVIF events: subscribed camera=%q peer=%s", s.dev.Name, r.RemoteAddr)
		addr := soap.XMLEscape("http://" + s.hostPort(r) + "/onvif/events_service?sub=" + id)
		action = eventNS + "/EventPortType/CreatePullPointSubscriptionResponse"
		body = "<tev:CreatePullPointSubscriptionResponse><tev:SubscriptionReference><wsa:Address>" + addr + "</wsa:Address></tev:SubscriptionReference>" + times("wsnt") + "</tev:CreatePullPointSubscriptionResponse>"
	default:
		if p == nil {
			soap.WriteInvalidArg(w, "unknown or expired pull point")
			return
		}
		switch req.Action {
		case "PullMessages":
			if p.pulling {
				soap.WriteInvalidArg(w, "pull already in progress")
				return
			}
			limit := 32
			if text, ok := soap.ExtractElement(req.Body, "MessageLimit"); ok {
				n, err := strconv.Atoi(text)
				if err != nil || n < 1 {
					soap.WriteInvalidArg(w, "invalid MessageLimit")
					return
				}
				limit = min(n, queueLimit)
			}
			p.pulling = true
			defer func() { p.pulling = false }()
			wait := interval(req.Body, "Timeout", time.Second, 60*time.Second)
			p.expires = maxTime(p.expires, now.Add(subscriptionLife))
			if len(p.queue) == 0 && wait > 0 {
				ch := e.changed
				e.mu.Unlock()
				timer := time.NewTimer(wait)
				select {
				case <-ch:
				case <-timer.C:
				case <-r.Context().Done():
				}
				timer.Stop()
				e.mu.Lock()
				if e.points[id] != p || r.Context().Err() != nil {
					return
				}
			}
			n := min(limit, len(p.queue))
			body = "<tev:PullMessagesResponse>" + times("tev") + strings.Join(p.queue[:n], "") + "</tev:PullMessagesResponse>"
			if strings.Contains(body, `PropertyOperation="Changed"`) {
				log.Printf("ONVIF events: motion delivered camera=%q peer=%s", s.dev.Name, r.RemoteAddr)
			}
			p.queue = p.queue[n:]
			action = eventNS + "/PullPointSubscription/PullMessagesResponse"
		case "Renew":
			p.expires = now.Add(interval(req.Body, "TerminationTime", subscriptionLife, time.Hour))
			action = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewResponse"
			body = "<wsnt:RenewResponse><wsnt:TerminationTime>" + stamp(p.expires) + "</wsnt:TerminationTime><wsnt:CurrentTime>" + stamp(now) + "</wsnt:CurrentTime></wsnt:RenewResponse>"
		case "SetSynchronizationPoint":
			p.enqueue(e.active, now, "Initialized")
			e.wake()
			action = eventNS + "/PullPointSubscription/SetSynchronizationPointResponse"
			body = "<tev:SetSynchronizationPointResponse/>"
		case "Unsubscribe":
			delete(e.points, id)
			e.wake()
			action = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeResponse"
			body = "<wsnt:UnsubscribeResponse/>"
		default:
			soap.WriteActionNotSupported(w, req.Action)
			return
		}
	}
}

func maxTime(a, b time.Time) time.Time {
	if a.After(b) {
		return a
	}
	return b
}
