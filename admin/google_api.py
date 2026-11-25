"""Google calls made on behalf of the setup wizard. Nothing here keeps state or stores a credential."""
import base64
import json
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

TOKEN = "https://oauth2.googleapis.com/token"
USAGE = "https://serviceusage.googleapis.com/v1/"
PROJECTS = "https://cloudresourcemanager.googleapis.com/v3/projects/"
PUBSUB = "https://pubsub.googleapis.com/v1/"
IAM = "https://iam.googleapis.com/v1/"
CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
DEVICE_SCOPE = "https://www.googleapis.com/auth/sdm.service"
SERVICES = ["smartdevicemanagement.googleapis.com", "pubsub.googleapis.com", "iam.googleapis.com",
            "cloudresourcemanager.googleapis.com"]
PUBLISHER = "group:sdm-publisher@googlegroups.com"
NAME = "nestunifi-events"
# A freshly enabled API or a new service account takes a moment to exist everywhere at Google.
SETTLING = (400, 403, 404)


class Failure(Exception):
    pass


def send(method, url, token=None, body=None, form=None):
    headers = {"Accept": "application/json"}
    data = None
    if form is not None:
        data = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except OSError:
        raise Failure("Google is vanaf dit systeem niet bereikbaar. Controleer de internetverbinding en probeer het opnieuw.")
    try:
        answer = json.loads(raw or b"{}")
    except ValueError:
        answer = {}
    return status, answer if isinstance(answer, dict) else {}


def reason(answer):
    error = answer.get("error")
    if isinstance(error, dict):
        return error.get("message") or error.get("status") or ""
    return answer.get("error_description") or error or ""


def call(step, method, url, token, body=None, accept=(200,), until=None):
    """Run one Google API call. With `until`, retry while Google is still settling an earlier step."""
    while True:
        status, answer = send(method, url, token, body)
        if status in accept:
            return answer
        if until is None or status not in SETTLING or time.monotonic() > until:
            raise Failure(step + " is niet gelukt. Google meldt: " + (reason(answer) or "HTTP " + str(status)))
        time.sleep(4)


def consent_url(client_id, redirect, state):
    """Consent for the one-off Cloud setup. No refresh token is requested."""
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": CLOUD_SCOPE, "state": state, "prompt": "consent"})


def link_url(project_id, client_id, redirect, state):
    """Device Access authorises through its own host; the standard endpoint yields a token SDM rejects."""
    return "https://nestservices.google.com/partnerconnections/" + quote(project_id, safe="") + "/auth?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": DEVICE_SCOPE,
        "state": state, "access_type": "offline", "prompt": "consent"})


def exchange(client_id, client_secret, redirect, code):
    status, answer = send("POST", TOKEN, form={
        "client_id": client_id, "client_secret": client_secret, "code": code,
        "grant_type": "authorization_code", "redirect_uri": redirect})
    if status != 200 or not answer.get("access_token"):
        raise Failure("Google heeft de koppeling geweigerd (" + (reason(answer) or "HTTP " + str(status)) +
                      "). Controleer client-ID, clientgeheim en redirect-URI en start de koppeling opnieuw.")
    return answer


def grant(step, resource, role, member, token, until):
    policy = call(step, "GET", PUBSUB + resource + ":getIamPolicy", token, until=until)
    bindings = policy.setdefault("bindings", [])
    if any(b.get("role") == role and member in b.get("members", []) for b in bindings):
        return
    bindings.append({"role": role, "members": [member]})
    call(step, "POST", PUBSUB + resource + ":setIamPolicy", token, {"policy": policy}, until=until)


def provision(token, client_id, known_account=None, patience=150):
    """Enable the APIs and create the event topic, subscription and reader in the OAuth client's own project.

    Every step accepts what an earlier attempt already made, so a retry continues where the last one stopped.
    """
    number = client_id.split("-", 1)[0]
    until = time.monotonic() + patience
    step = "API's aanzetten"
    job = call(step, "POST", USAGE + "projects/" + number + "/services:batchEnable", token, {"serviceIds": SERVICES})
    while not job.get("done"):
        if time.monotonic() > until:
            raise Failure(step + " duurt langer dan verwacht. Wacht een minuut en verbind opnieuw.")
        time.sleep(3)
        job = call(step, "GET", USAGE + job["name"], token)
    if job.get("error"):
        raise Failure(step + " is niet gelukt. Google meldt: " + (job["error"].get("message") or "onbekende fout"))
    project = call("Project opzoeken", "GET", PROJECTS + number, token, until=until)["projectId"]
    topic = "projects/" + project + "/topics/" + NAME
    subscription = "projects/" + project + "/subscriptions/" + NAME
    account = NAME + "@" + project + ".iam.gserviceaccount.com"
    step = "Meldingskanaal aanmaken"
    call(step, "PUT", PUBSUB + topic, token, {}, accept=(200, 409), until=until)
    grant(step, topic, "roles/pubsub.publisher", PUBLISHER, token, until)
    # Motion older than ten minutes is of no use; an idle subscription must still never expire.
    call(step, "PUT", PUBSUB + subscription, token, {"topic": topic, "ackDeadlineSeconds": 10,
         "messageRetentionDuration": "600s", "expirationPolicy": {}}, accept=(200, 409), until=until)
    step = "Leesaccount aanmaken"
    call(step, "POST", IAM + "projects/" + project + "/serviceAccounts", token,
         {"accountId": NAME, "serviceAccount": {"displayName": "NestUnifi meldingen"}}, accept=(200, 409), until=until)
    grant(step, subscription, "roles/pubsub.subscriber", "serviceAccount:" + account, token, until)
    key = None
    if known_account != account:
        made = call(step, "POST", IAM + "projects/" + project + "/serviceAccounts/" + account + "/keys",
                    token, {}, until=until)
        try:
            key = json.loads(base64.b64decode(made["privateKeyData"]))
        except (KeyError, ValueError):
            raise Failure(step + " is niet gelukt. Google gaf geen bruikbare sleutel terug.")
    return {"project": project, "topic": topic, "subscription": subscription, "account": account, "key": key}
