"use strict";
const el = id => document.getElementById(id);
const make = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
};
function message(id, value = "", error = false) {
  const node = el(id);
  node.textContent = value;
  node.classList.toggle("error", error);
}
async function api(action, data = {}) {
  const r = await fetch("/api/" + action, {method: "POST", headers: {"Content-Type": "application/json", "X-NestUnifi": "1"}, body: JSON.stringify(data)});
  const json = await r.json().catch(() => ({}));
  // The session ended, for example after a restart. Ask for the password again.
  if (r.status === 401) showAccess(true);
  if (!r.ok) throw new Error(json.error || "NestUnifi is niet bereikbaar.");
  return json;
}
function button(title, fn) {
  const b = make("button", title, "text-btn");
  b.type = "button";
  b.addEventListener("click", fn);
  return b;
}
const kinds = {
  CAMERA: ["Camera", "Camera’s", "video-camera"],
  DOORBELL: ["Deurbel", "Deurbellen", "bell"],
  DISPLAY: ["Nest Hub", "Nest Hubs", "monitor"]
};
function icon(name) {
  const img = make("img");
  img.src = "/assets/" + name + ".svg";
  img.alt = "";
  return img;
}
let devices = [], filter = "";
// The time of a camera's latest motion, short when it was today.
function when(stamp) {
  const at = new Date(stamp);
  return at.toDateString() === new Date().toDateString()
    ? at.toLocaleTimeString("nl-NL", {hour: "2-digit", minute: "2-digit"})
    : at.toLocaleString("nl-NL", {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"});
}
function motionText(motion) {
  return !motion ? "" : motion.active ? "beweging nu" : motion.last ? "beweging " + when(motion.last) : "";
}
function renderDevices() {
  const list = el("devices");
  list.replaceChildren();
  for (const cam of devices.filter(c => !filter || c.kind === filter)) {
    const row = make("article", undefined, "device");
    row.dataset.kind = cam.kind;
    const [type,, symbol] = kinds[cam.kind] || kinds.CAMERA;
    const head = make("div", undefined, "device-title");
    const badge = make("span", undefined, "kind-icon");
    badge.append(icon(symbol));
    const title = make("div");
    title.append(make("h3", cam.name), make("p", [cam.model || type, cam.ip, motionText(cam.motion)].filter(Boolean).join(" · ")));
    head.append(badge, title);
    const open = button("", () => watch(cam));
    open.className = "stream-link";
    open.setAttribute("aria-label", "Livebeeld van " + cam.name);
    const footer = make("div", undefined, "device-footer");
    const liveStatus = cam.live === null ? "Beeldstatus onbekend" : cam.live ? "Livebeeld beschikbaar" : "Geen bronbeeld";
    const status = make("span", liveStatus, "status" + (cam.live ? " online" : ""));
    status.title = cam.online ? "ONVIF actief" : "ONVIF offline";
    const play = make("span", undefined, "play-hint");
    play.append(icon("play"), document.createTextNode("Kijken"));
    const setup = button("", () => settings(cam));
    setup.title = "Instellingen voor " + cam.name;
    setup.setAttribute("aria-label", setup.title);
    setup.className = "icon-button setup";
    setup.append(icon("gear-six"));
    footer.append(status, play, setup);
    row.append(open, head, footer);
    list.append(row);
  }
  for (const btn of el("filters").children) btn.setAttribute("aria-pressed", String(btn.value === filter));
}
// What Google's answer about the stored link means, and which setup step repairs it.
const linkProblems = {
  revoked: ["Google heeft de toegang van NestUnifi ingetrokken of laten verlopen. Je camera’s krijgen dan geen beeld. Koppel ze opnieuw in stap 4 van de installatie. Staat je app in Google Cloud nog op Testing? Zet hem dan eerst op In production, anders verloopt de koppeling elke 7 dagen.", "Opnieuw koppelen", 3],
  client: ["Google weigert de inlog-app van NestUnifi. Misschien is de OAuth-client verwijderd of het clientgeheim vernieuwd. Vul de gegevens opnieuw in bij stap 1 en koppel daarna opnieuw in stap 4.", "Inlog-app bijwerken", 0],
  unreachable: ["NestUnifi kon de Google-koppeling niet controleren. Controleer de internetverbinding van dit systeem.", "", null]
};
let repairStep = null;
function render({cameras, bridge, google, version, update}) {
  el("update").hidden = !update;
  el("update").textContent = update ? "Versie " + update + " is beschikbaar. Draai sh install.sh opnieuw op dit systeem om bij te werken. Je instellingen blijven staan." : "";
  const problem = linkProblems[google];
  el("health").hidden = !problem;
  if (problem) {
    [el("healthText").textContent, el("healthFix").textContent, repairStep] = problem;
    el("healthFix").hidden = repairStep === null;
  }
  devices = cameras;
  if (!devices.some(c => c.kind === filter)) filter = "";
  const filters = el("filters");
  filters.replaceChildren();
  for (const value of ["", ...Object.keys(kinds)]) {
    const count = devices.filter(c => !value || c.kind === value).length;
    if (!count && value) continue;
    const btn = button(value ? kinds[value][1] : "Alle", () => { filter = value; renderDevices(); });
    btn.value = value;
    btn.append(make("span", String(count)));
    filters.append(btn);
  }
  renderDevices();
  message("summary", cameras.length + " apparaten");
  message("endpoints", cameras.filter(c => c.online).length + " ONVIF actief" + (version && version !== "dev" ? " · versie " + version : ""));
  message("bridge", bridge ? "Bridge actief" : "Bridge gestopt");
  el("bridge").classList.toggle("active", bridge);
  el("overviewStatus").hidden = el("bridge").hidden = false;
}
async function refresh() {
  el("refresh").disabled = true;
  try { render(await api("list")); } finally { el("refresh").disabled = false; }
}
let submitAction;
let modalEpoch = 0;
let player = null, video = null, startupTimer;
function stopPlayback() {
  clearTimeout(startupTimer);
  if (player) { player.destroy(); player = null; }
  if (video) {
    video.onplaying = video.onerror = video.onwaiting = null;
    video.pause(); video.removeAttribute("src"); video.load(); video = null;
  }
}
function modal(title) {
  modalEpoch++;
  stopPlayback();
  el("modal").classList.remove("viewer");
  el("cancel").hidden = false;
  el("modalTitle").textContent = title;
  el("fields").replaceChildren();
  message("modalMessage");
  submitAction = null;
  el("submit").hidden = true;
  el("submit").classList.remove("danger");
  el("submit").disabled = el("close").disabled = el("cancel").disabled = false;
  if (!el("modal").open) el("modal").showModal();
  return modalEpoch;
}
function inputName(value) {
  const label = make("label", "ONVIF-naam");
  label.htmlFor = "cameraName";
  const input = make("input");
  input.id = "cameraName";
  input.maxLength = 48;
  input.required = true;
  input.value = value;
  el("fields").append(label, input);
  return input;
}
function onSubmit(title, fn) {
  el("submit").textContent = title;
  el("submit").hidden = false;
  submitAction = fn;
}
async function watch(cam, reconnect = true) {
  const epoch = modal(cam.name);
  el("modal").classList.add("viewer");
  el("cancel").hidden = true;
  const media = make("video");
  video = media;
  media.controls = media.autoplay = media.muted = media.playsInline = true;
  media.setAttribute("aria-label", "Livebeeld van " + cam.name);
  const retry = button("Opnieuw proberen", () => watch(cam));
  retry.className = "quiet retry";
  retry.hidden = true;
  el("fields").append(media, retry);
  function failed(text) {
    if (epoch !== modalEpoch || video !== media) return;
    stopPlayback();
    media.hidden = true;
    retry.hidden = false;
    message("modalMessage", text || "De bridge ontvangt geen video van " + cam.name + ". Controleer het livebeeld van dit apparaat in Google Home.", true);
  }
  function interrupted() {
    if (epoch !== modalEpoch || video !== media) return;
    if (!reconnect) {
      failed("De videostream is onderbroken. Probeer opnieuw verbinding te maken.");
      return;
    }
    stopPlayback();
    message("modalMessage", "De videostream is onderbroken. Opnieuw verbinden…");
    startupTimer = setTimeout(() => {
      if (epoch === modalEpoch && el("modal").open) watch(cam, false);
    }, 1200);
  }
  media.onplaying = () => {
    clearTimeout(startupTimer);
    message("modalMessage", "Livebeeld · " + media.videoWidth + " × " + media.videoHeight);
  };
  media.onwaiting = () => {
    message("modalMessage", "Wachten op videobeeld…");
    clearTimeout(startupTimer);
    startupTimer = setTimeout(interrupted, 45000);
  };
  media.onerror = interrupted;
  message("modalMessage", "Verbinding maken met " + cam.name + "…");
  startupTimer = setTimeout(interrupted, 45000);
  try {
    const {url} = await api("view", {id: cam.id});
    if (epoch !== modalEpoch || video !== media) return;
    if (window.Hls && Hls.isSupported()) {
      player = new Hls({enableWorker: false, lowLatencyMode: false});
      player.on(Hls.Events.ERROR, (_, data) => {
        if (data.details === Hls.ErrorDetails.MANIFEST_LOAD_ERROR && data.response?.code === 404) failed();
        else if (data.fatal) interrupted();
      });
      player.loadSource(url);
      player.attachMedia(media);
    } else if (media.canPlayType("application/vnd.apple.mpegurl")) {
      media.src = url;
    } else {
      failed("Deze browser ondersteunt de videospeler niet. Open de camera in Safari, Chrome, Edge of Firefox.");
    }
  } catch (e) { failed(e.message); }
}
function settings(cam) {
  modal(cam.name);
  // Without Pub/Sub events the camera reports no motion at all.
  if (cam.motion) el("fields").append(make("p", cam.motion.active ? "Beweging gemeld op dit moment."
    : cam.motion.last ? "Laatste beweging: " + when(cam.motion.last) + "."
    : "Sinds deze virtuele camera startte, kwam er nog geen beweging binnen. Ziet Google Home wel beweging? Controleer dan in stap 3 van de installatie of het Pub/Sub-topic in je Device Access-project staat."));
  for (const [title, symbol, action] of [["Naam wijzigen", "pencil-simple", rename],
      ["Wachtwoord wijzigen", "key", rotate], ["Stream testen", "waveform", test], ["Verwijderen", "x", removeCamera]]) {
    const btn = button(title, () => action(cam));
    btn.className = "action-option";
    btn.prepend(icon(symbol));
    el("fields").append(btn);
  }
}
function passwordFields(target = el("fields")) {
  const fields = [];
  for (const [id, title] of [["camPassword", "Nieuw camerawachtwoord"], ["confirmPassword", "Herhaal het wachtwoord"]]) {
    const label = make("label", title), field = make("input");
    field.type = "password";
    field.autocomplete = "new-password";
    field.id = label.htmlFor = id;
    field.required = true;
    field.minLength = 8;
    field.maxLength = 64;
    fields.push(field);
    target.append(label, field);
  }
  const allowed = /^[A-Za-z0-9!$()*+.;<=>\[\]^_{}@#&-]{8,64}$/;
  const validate = () => {
    fields[0].setCustomValidity(!fields[0].value || allowed.test(fields[0].value) ? "" : "Kies 8 tot 64 tekens uit de lijst onder het veld.");
    fields[1].setCustomValidity(fields[0].value === fields[1].value ? "" : "De wachtwoorden verschillen.");
  };
  fields.forEach(f => f.addEventListener("input", validate));
  target.append(make("p", "Gebruik 8 tot 64 tekens. Letters, cijfers en deze leestekens zijn toegestaan: ! $ ( ) * + . ; < = > [ ] ^ _ - { } @ # &"));
  return fields[0];
}
function rotate(cam) {
  modal("Wachtwoord wijzigen");
  el("fields").append(make("strong", cam.name), make("p", "Gebruikersnaam: " + cam.username),
    make("p", "Na opslaan herstart deze virtuele camera kort. Vul het nieuwe wachtwoord ook in bij deze camera in UniFi Protect om de verbinding te herstellen."));
  const field = passwordFields();
  field.focus();
  onSubmit("Wachtwoord opslaan", () => api("password", {id: cam.id, password: field.value}));
}
function rename(cam) {
  modal("Naam wijzigen");
  el("fields").append(make("p", "Deze naam verschijnt bij ONVIF-detectie. Een opgeslagen naam in Protect wijzig je in Protect zelf."));
  const field = inputName(cam.name);
  field.select();
  onSubmit("Opslaan", () => api("rename", {id: cam.id, name: field.value, previous: cam.name}));
}
function removeCamera(cam) {
  modal("Apparaat verwijderen");
  el("fields").append(make("strong", cam.name),
    make("p", "NestUnifi stopt deze virtuele camera en geeft haar netwerkadres vrij. Verwijder het apparaat ook in UniFi Protect. De gedeelde videobridge herstart kort, dus de andere camera’s haperen even."),
    make("p", "Later opnieuw toevoegen kan via Apparaat toevoegen; het apparaat krijgt dan dezelfde netwerkidentiteit. Wil je het niet meer met NestUnifi delen, zet het dan uit bij de Google-koppeling in stap 4."));
  el("submit").classList.add("danger");
  onSubmit("Verwijderen", () => api("remove", {id: cam.id}));
}
async function test(cam) {
  const epoch = modal("Stream testen");
  el("fields").append(make("strong", cam.name));
  message("modalMessage", "Videopakketten ophalen…");
  try {
    const info = await api("test", {id: cam.id});
    if (epoch !== modalEpoch) return;
    message("modalMessage", info.live ? "Livebeeld ontvangen · " + info.width + " × " + info.height + " · " + info.codec.toUpperCase() : "Geen videopakketten ontvangen. Controleer het livebeeld ook in Google Home.", !info.live);
    if (info.live && (info.width !== cam.width || info.height !== cam.height)) {
      el("fields").append(make("p", "Het ONVIF-profiel vermeldt " + cam.width + " × " + cam.height + ". Overnemen werkt de profielresolutie bij en herstart dit apparaat kort."));
      onSubmit("Resolutie overnemen", () => api("profile", {id: cam.id}));
    }
  } catch (e) { if (epoch === modalEpoch) message("modalMessage", e.message, true); }
}
async function find() {
  const epoch = modal("Google-apparaat toevoegen");
  message("modalMessage", "Apparaten ophalen bij Google…");
  try {
    const {available, configured} = await api("discover");
    if (epoch !== modalEpoch) return;
    if (!available.length) {
      message("modalMessage", "Geen nieuwe apparaten gevonden. Je hebt " + configured + " apparaten ingesteld. Deel nieuwe apparaten met NestUnifi via je Google Device Access-koppeling en zoek opnieuw.");
      return;
    }
    message("modalMessage");
    const label = make("label", "Beschikbaar via Google");
    const select = make("select");
    label.htmlFor = select.id = "googleDevice";
    const types = {DISPLAY: "Scherm", DOORBELL: "Deurbel", CAMERA: "Camera"};
    for (const d of available) select.add(new Option(d.name + " · " + types[d.type], d.id));
    el("fields").append(label, select);
    const field = inputName(available[0].name);
    const password = passwordFields();
    select.onchange = () => { field.value = available[select.selectedIndex].name; };
    el("fields").append(make("p", "Toevoegen herstart de gedeelde videobridge kort. Daarna kun je het apparaat in Protect adopteren."));
    onSubmit("Toevoegen", () => api("add", {id: select.value, name: field.value, password: password.value}));
  } catch (e) { if (epoch === modalEpoch) message("modalMessage", e.message, true); }
}
el("dialogForm").onsubmit = async e => {
  e.preventDefault();
  if (!submitAction || el("submit").disabled) return;
  el("submit").disabled = el("close").disabled = el("cancel").disabled = true;
  message("modalMessage", "Wijziging toepassen…");
  try {
    const result = await submitAction();
    el("modal").close();
    message("notice", result.already ? "Dit apparaat is al toegevoegd." : (result.removed ? "Verwijderd · " : "Opgeslagen · ") + result.name);
    await refresh().catch(err => message("notice", err.message, true));
  } catch (err) { message("modalMessage", err.message, true); }
  finally { el("submit").disabled = el("close").disabled = el("cancel").disabled = false; }
};
el("modal").oncancel = e => { if (el("cancel").disabled) e.preventDefault(); };
el("close").onclick = el("cancel").onclick = () => el("modal").close();
el("modal").onclose = () => { modalEpoch++; stopPlayback(); el("fields").replaceChildren(); submitAction = null; };
window.addEventListener("pagehide", stopPlayback);
el("find").onclick = find;
el("refresh").onclick = () => { message("notice"); refresh().catch(e => message("notice", e.message, true)); };

// Setup wizard. Each step is done when the server says so, so a reload or a second browser continues where it was.
const cloud = "https://console.cloud.google.com/";
let setupState, openStep = null;
function link(text, href) {
  const a = make("a", text);
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}
function line(...parts) {
  const li = make("li");
  li.append(...parts);
  return li;
}
function copyable(value) {
  const row = make("div", undefined, "copy"), field = make("input");
  const copy = button("Kopiëren", async () => {
    try { await navigator.clipboard.writeText(value); copy.textContent = "Gekopieerd"; } catch { field.select(); }
  });
  field.readOnly = true;
  field.value = value;
  field.setAttribute("aria-label", "Waarde om te kopiëren");
  copy.className = "quiet";
  row.append(field, copy);
  return row;
}
function entry(id, title, type = "text") {
  const label = make("label", title), input = make("input");
  input.id = label.htmlFor = id;
  input.type = type;
  input.required = true;
  input.autocomplete = "off";
  input.spellcheck = false;
  return [label, input];
}
function submit(title) {
  const row = make("div", undefined, "actions"), go = make("button", title, "primary");
  row.append(go);
  return row;
}
async function act(form, text, fn) {
  const note = form.querySelector(".message"), controls = [...form.elements];
  controls.forEach(c => { c.disabled = true; });
  note.classList.remove("error");
  note.textContent = text;
  try {
    await fn(note);
    if (note.textContent === text) note.textContent = "";
  } catch (e) {
    note.textContent = e.message;
    note.classList.add("error");
  } finally { controls.forEach(c => { c.disabled = false; }); }
}
function advance(state) {
  setupState = state;
  openStep = null;
  renderSetup();
}
function returned(text) {
  try {
    const query = new URL(text.trim()).searchParams;
    if (query.get("code")) return {code: query.get("code"), state: query.get("state") || undefined};
  } catch {}
  return {code: text.trim()};
}
function stepClient(s, form) {
  const [idLabel, id] = entry("clientId", "Client ID"), [secretLabel, secret] = entry("clientSecret", "Client secret", "password");
  id.value = s.client_id;
  const list = make("ol");
  list.append(
    line("Maak een project aan in ", link("Google Cloud", cloud + "projectcreate"), ". Gebruik het Google-account waarin je Nest-camera’s staan. Dat moet een gewoon Google-account zijn, geen Workspace-account."),
    line("Open ", link("Branding", cloud + "auth/branding"), " en kies Get started. Vul een app-naam in, bijvoorbeeld NestUnifi, en je e-mailadres. Kies External en rond af. Laat logo en domeinen leeg."),
    line("Open ", link("Audience", cloud + "auth/audience"), " en kies Publish app. De status wordt dan In production. In de teststatus verloopt de koppeling elke 7 dagen."),
    line("Open ", link("Clients", cloud + "auth/clients"), ", kies Create client en daarna Web application. Voeg bij Authorized redirect URIs dit adres toe:", copyable(s.register)),
    line("Kies Create en neem de Client ID en het Client secret hieronder over."));
  form.onsubmit = e => {
    e.preventDefault();
    act(form, "Opslaan…", async () => advance(await api("client", {client_id: id.value, client_secret: secret.value})));
  };
  return [make("p", "Controleer bij elke link of bovenaan in Google Cloud je nieuwe project is gekozen."), list, idLabel, id, secretLabel, secret, submit("Opslaan en verder")];
}
function stepConsent(s, form, kind) {
  const first = kind === "cloud";
  const [label, address] = entry(kind + "Address", "Adres van de pagina waar Google je naartoe stuurde");
  const begin = submit(first ? "Verbinden met Google" : "Camera’s koppelen"), paste = make("div"), open = link("Google openen in een nieuw tabblad", "");
  open.className = "primary";
  address.required = false;
  paste.hidden = true;
  paste.append(open, make("p", "Na het toestaan stuurt Google je door. Kopieer het volledige adres uit de adresbalk van die pagina en plak het hieronder."), label, address, submit("Verder"));
  form.onsubmit = e => {
    e.preventDefault();
    if (paste.hidden) act(form, "Google openen…", async () => {
      const {url} = await api("consent", {kind});
      if (!s.manual) {
        location.assign(url);
        return new Promise(() => {});
      }
      open.href = url;
      begin.hidden = true;
      paste.hidden = false;
      address.required = true;
    });
    else act(form, first ? "Google Cloud wordt ingericht. Dit duurt ongeveer een minuut…" : "Koppeling afronden…",
      async () => advance(await api("code", returned(address.value))));
  };
  return [make("p", first
    ? "NestUnifi zet nu zelf de benodigde API’s aan en maakt het meldingskanaal voor beweging en de deurbel aan. Google vraagt daarvoor één keer toegang tot je Cloud-project. NestUnifi gebruikt die toegang alleen voor deze stap en bewaart haar niet."
    : "Kies bij Google je woning en zet de camera’s en deurbellen aan die je wilt gebruiken."),
    make("p", "Google meldt dat de app niet is geverifieerd. Het is je eigen app: kies Advanced en daarna Go to NestUnifi (unsafe)."), begin, paste];
}
function stepProject(s, form) {
  const [label, project] = entry("accessProject", "Project ID");
  project.value = s.project;
  const list = make("ol");
  list.append(
    line("Open de ", link("Device Access Console", "https://console.nest.google.com/device-access"), ", accepteer de voorwaarden en betaal de eenmalige registratie van US$ 5."),
    line("Kies Create project en geef het een naam. Vul bij OAuth client ID dit in:", copyable(s.client_id)),
    s.topic ? line("Kies Enable events en maak het project aan. Open daarna het project, kies bij Pub/Sub topic voor Enable events with PubSub topic en vul dit in:", copyable(s.topic))
      : line("Maak het project aan."),
    line("Neem de Project ID van het project hieronder over."));
  form.onsubmit = e => {
    e.preventDefault();
    act(form, "Opslaan…", async () => advance(await api("project", {project: project.value})));
  };
  return [list, label, project, submit("Opslaan en verder")];
}
function stepCameras(s, form) {
  const box = make("div"), rows = [];
  // Runs once the step is on the page. A failure from the previous attempt stays visible while the list loads.
  queueMicrotask(async () => {
    const status = form.querySelector(".message"), failure = status.textContent;
    if (!failure) status.textContent = "Apparaten ophalen bij Google…";
    try {
      await list();
      if (!failure) status.textContent = "";
    } catch (e) {
      status.textContent = e.message;
      status.classList.add("error");
    }
  });
  async function list() {
    const {available} = await api("discover");
    if (!available.length) throw new Error(s.cameras ? "Alle gedeelde apparaten zijn al toegevoegd." : "Google deelt nog geen camera’s met NestUnifi. Open stap 4 opnieuw en zet daar je camera’s aan.");
    // Google gives unnamed devices the same label, and two cameras cannot share a name.
    const taken = new Set(devices.map(d => d.name.toLowerCase()));
    for (const device of available) {
      const row = make("div", undefined, "pick"), check = make("input"), name = make("input");
      let label = device.name;
      for (let n = 2; taken.has(label.toLowerCase()); n++) label = device.name + " " + n;
      taken.add(label.toLowerCase());
      check.type = "checkbox";
      check.checked = true;
      check.setAttribute("aria-label", device.name + " toevoegen");
      name.value = label;
      name.maxLength = 48;
      name.setAttribute("aria-label", "ONVIF-naam voor " + device.name);
      row.append(check, name);
      rows.push({id: device.id, check, name});
      box.append(row);
    }
    box.append(make("p", "Kies één wachtwoord voor deze camera’s. UniFi Protect vraagt erom bij het adopteren."));
    const password = passwordFields(box);
    box.append(submit("Toevoegen"));
    form.onsubmit = e => {
      e.preventDefault();
      act(form, "Toevoegen…", async note => {
        const chosen = rows.filter(r => r.check.checked);
        if (!chosen.length) throw new Error("Kies minstens één apparaat.");
        try {
          for (const [i, r] of chosen.entries()) {
            note.textContent = r.name.value + " toevoegen (" + (i + 1) + " van " + chosen.length + "). Dit duurt ongeveer een minuut per camera…";
            await api("add", {id: r.id, name: r.name.value, password: password.value});
          }
        } catch (e) {
          // Earlier cameras in this run are already added. Show what is left.
          setupState = await api("setup").catch(() => setupState);
          openStep = 4;
          return renderSetup([e.message, true]);
        }
        await showOverview();
        message("notice", "Camera’s toegevoegd. Zet in UniFi Protect bij Settings → System de camera’s van derden (Third-Party Cameras) aan en adopteer ze met gebruikersnaam " + (devices[0]?.username || "nestunifi") + " en het zojuist gekozen wachtwoord.");
      });
    };
  }
  return [make("p", "Gebruik camera’s met vaste voeding. De bridge streamt continu, dus een camera op batterij raakt snel leeg."), box];
}
const steps = [
  ["Google Cloud-project en inlog-app", s => s.client, stepClient],
  ["Google Cloud automatisch inrichten", s => s.cloud, (s, form) => stepConsent(s, form, "cloud")],
  ["Device Access-project", s => Boolean(s.project), stepProject],
  ["Camera’s koppelen", s => s.linked, (s, form) => stepConsent(s, form, "link")],
  ["Camera’s toevoegen", s => s.cameras > 0, stepCameras]
];
function renderSetup(note) {
  const s = setupState, current = steps.findIndex(([, done]) => !done(s)), open = openStep ?? current;
  el("steps").replaceChildren();
  steps.forEach(([title, done, body], i) => {
    const item = make("li", undefined, "step"), head = button("", () => { openStep = open === i ? -2 : i; renderSetup(); });
    item.dataset.state = done(s) ? "done" : i === current ? "current" : "todo";
    head.className = "step-head";
    head.append(make("span", done(s) ? "✓" : String(i + 1), "step-mark"), make("span", title));
    head.setAttribute("aria-expanded", String(open === i));
    // A later step needs what the earlier ones produce.
    head.disabled = current !== -1 && i > current;
    item.append(head);
    if (open === i) {
      const form = make("form", undefined, "step-body"), status = make("p", note?.[0], "message" + (note?.[1] ? " error" : ""));
      status.setAttribute("role", "status");
      form.append(...body(s, form), status);
      item.append(form);
    }
    el("steps").append(item);
  });
  el("setupClose").hidden = !(s.linked && s.cameras);
  renderBackup(s);
}
// A new installation can take over a backup. Any other one can make one.
function renderBackup(s) {
  const box = el("backup"), note = make("p", undefined, "message");
  note.setAttribute("role", "status");
  box.replaceChildren();
  const failed = e => {
    note.textContent = e instanceof SyntaxError ? "Dit bestand is geen back-up van NestUnifi." : e.message;
    note.classList.add("error");
  };
  if (!s.cameras) {
    const file = make("input"), pick = button("Back-up terugzetten", () => file.click());
    file.type = "file";
    file.accept = ".json,application/json";
    file.hidden = true;
    pick.className = "quiet";
    file.onchange = async () => {
      const chosen = file.files[0];
      if (!chosen) return;
      pick.disabled = true;
      note.classList.remove("error");
      note.textContent = "Back-up terugzetten…";
      try {
        setupState = await api("restore", {backup: JSON.parse(await chosen.text())});
        openStep = null;
        if (setupState.linked && setupState.cameras) {
          await showOverview();
          message("notice", "Back-up teruggezet. Je camera’s starten nu.");
        } else renderSetup();
      } catch (e) { failed(e); }
      finally { pick.disabled = false; file.value = ""; }
    };
    box.append(make("h2", "Verhuizen"), make("p", "Draaide NestUnifi eerder op een ander systeem? Zet het daar eerst uit, want beide zouden dezelfde netwerkadressen gebruiken. Zet daarna hier de back-up terug; de stappen hierboven zijn dan niet meer nodig."), pick, file, note);
    return;
  }
  const save = button("Back-up downloaden", async () => {
    save.disabled = true;
    note.classList.remove("error");
    note.textContent = "";
    try {
      const link = make("a");
      link.href = URL.createObjectURL(new Blob([JSON.stringify(await api("backup"), null, 2)], {type: "application/json"}));
      link.download = "nestunifi-backup-" + new Date().toISOString().slice(0, 10) + ".json";
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    } catch (e) { failed(e); }
    finally { save.disabled = false; }
  });
  save.className = "quiet";
  box.append(make("h2", "Back-up"), make("p", "Het bestand bevat je instellingen en de koppeling met Google, maar niet je beheerwachtwoord. Bewaar het veilig en deel het met niemand: wie het heeft, kan je camera’s bekijken."), save, note);
}
function showSetup(note) {
  el("overview").hidden = true;
  el("setup").hidden = false;
  renderSetup(note);
}
function showOverview() {
  el("setup").hidden = true;
  el("overview").hidden = false;
  return refresh();
}
el("healthFix").onclick = async () => {
  try {
    setupState = await api("setup");
    openStep = repairStep;
    showSetup();
  } catch (e) { message("notice", e.message, true); }
};
el("setupClose").onclick = () => showOverview().catch(e => message("notice", e.message, true));
el("openSetup").onclick = async () => {
  try {
    setupState = await api("setup");
    openStep = null;
    showSetup();
  } catch (e) { message("notice", e.message, true); }
};
// The first visitor chooses the admin password. After that every visit starts by signing in.
function showAccess(claimed) {
  for (const id of ["setup", "overview", "bridge", "openSetup", "signOut"]) el(id).hidden = true;
  if (el("modal").open) el("modal").close();
  el("access").hidden = false;
  el("accessTitle").textContent = claimed ? "Aanmelden" : "Welkom bij NestUnifi";
  el("accessHint").textContent = claimed ? "Vul je beheerwachtwoord in." : "Kies eerst een beheerwachtwoord. Daarmee meld je je voortaan aan op deze pagina.";
  const form = el("accessForm"), fields = [];
  form.replaceChildren();
  for (const [id, title] of claimed ? [["adminPassword", "Beheerwachtwoord"]] : [["adminPassword", "Nieuw beheerwachtwoord"], ["adminRepeat", "Herhaal het wachtwoord"]]) {
    const [label, input] = entry(id, title, "password");
    input.autocomplete = claimed ? "current-password" : "new-password";
    input.minLength = 10;
    input.maxLength = 128;
    fields.push(input);
    form.append(label, input);
  }
  const status = make("p", undefined, "message");
  status.setAttribute("role", "status");
  form.append(claimed ? make("p", "Wachtwoord vergeten? Verwijder het bestand private/admin/auth.json op het systeem waar NestUnifi draait en vernieuw deze pagina.") : make("p", "Gebruik minstens 10 tekens."), submit(claimed ? "Aanmelden" : "Wachtwoord instellen"), status);
  form.onsubmit = e => {
    e.preventDefault();
    act(form, "Controleren…", async () => {
      if (!claimed && fields[0].value !== fields[1].value) throw new Error("De wachtwoorden verschillen.");
      await api(claimed ? "login" : "claim", {password: fields[0].value});
      el("access").hidden = true;
      await start();
    });
  };
}
el("signOut").onclick = async () => {
  await api("logout").catch(() => {});
  showAccess(true);
};
async function start() {
  const back = new URLSearchParams(location.search), returning = location.pathname === "/oauth2callback";
  let note;
  // Take the one-time code out of the address bar and the history before anything else.
  if (returning) history.replaceState(null, "", "/");
  try {
    const access = await api("auth");
    if (!access.claimed || !access.signed_in) return showAccess(access.claimed);
    el("signOut").hidden = false;
    if (returning && back.get("code")) {
      message("notice", "Koppeling met Google afronden. Dit kan een minuut duren…");
      try { setupState = await api("code", {code: back.get("code"), state: back.get("state")}); }
      catch (e) { note = [e.message, true]; }
    } else if (returning) note = ["Google heeft geen toestemming doorgegeven. Start de stap opnieuw.", true];
    setupState = setupState || await api("setup");
    el("openSetup").hidden = false;
    if (returning || !setupState.linked || !setupState.cameras) showSetup(note);
    else await showOverview();
    message("notice");
  } catch (e) { message("notice", e.message, true); }
}
start();
