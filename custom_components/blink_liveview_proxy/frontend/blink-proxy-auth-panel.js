// How long to watch a proxy update before saying it did not finish. An update
// that fails leaves the proxy on its old version and says nothing, so without
// a deadline the bar would spin forever and read as progress.
const UPDATE_TIMEOUT_MS = 5 * 60 * 1000;

class BlinkProxyAuthPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._state = { state: "idle", message: "Loading authentication state…" };
    this._panelData = null;
    this._panelError = "";
    this._notice = "";
    this._tab = "overview";
    this._showLogin = false;
    this._busy = false;
    this._yaml = "";
    this._yamlFormat = "dashboard";
    this._yamlCamera = "";
    this._timer = null;
    // Which prerequisite accordions are open. Seeded once from the first
    // payload so unmet checks explain themselves without a click, then left
    // alone: a poll that rebuilt the page must not close what is being read.
    this._openHelp = null;
    this._dialogRequested = false;
    // Set while a proxy update is being watched. The proxy restarts mid-update
    // and cannot report its own progress, so every phase below is inferred
    // from what the panel can still see: health, and the version it reports.
    this._update = null;
    // Polling runs while the PIN is being typed. Only rebuild when something
    // visible changed, or the focused credential field would be erased.
    this._signature = null;
  }

  set hass(value) {
    const first = !this._hass;
    this._hass = value;
    if (first) {
      this._ensureDialog();
      this._render();
      Promise.all([this._refresh(), this._refreshPanel()]).then(() => this._schedule());
    }
  }

  // Load the dialog module ourselves rather than assuming Lovelace did.
  //
  // Home Assistant loads Lovelace resources from exactly one place, the
  // Lovelace dashboard panel. This is a custom panel, so on a session that
  // came straight here nothing has ever loaded that module, nothing is
  // listening for the ll-custom events the Cameras tab fires, and every Live
  // view, Clips and Refresh snapshot button in this panel does nothing at all
  // - the same silent failure the resource exists to prevent, arrived at from
  // the other direction.
  //
  // The module guards itself with window.__blinkLiveviewDialogLoaded, so
  // importing it here is harmless when a dashboard already did.
  _ensureDialog() {
    if (window.__blinkLiveviewDialogLoaded || this._dialogRequested) return;
    this._dialogRequested = true;
    // The icon set too: this panel can be the first thing a session loads.
    import("/api/blink_liveview_proxy/assets/blink-liveview-icons.js").catch(() => {});
    import("/api/blink_liveview_proxy/assets/blink-liveview-dialog.js").catch((error) => {
      this._dialogRequested = false;
      this._notice = "The live view dialog helper could not be loaded, so the buttons on the Cameras tab will not open anything. Check the Lovelace dialog resource below.";
      // eslint-disable-next-line no-console
      console.error("Blink Live View Proxy: dialog helper failed to load", error);
      this._render();
    });
  }

  get hass() { return this._hass; }
  set narrow(_value) {}
  set panel(_value) {}
  set route(_value) {}

  disconnectedCallback() {
    window.clearTimeout(this._timer);
    this._timer = null;
  }

  // A custom panel draws its own toolbar. Without one, a phone (sidebar not
  // docked) has no way to leave this page except killing the app.
  _back() {
    if (window.history.length > 1) {
      window.history.back();
      return;
    }
    window.history.pushState(null, "", "/");
    window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
  }

  // Home Assistant's own way of moving between panels without a reload.
  _navigate(path) {
    window.history.pushState(null, "", path);
    window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
  }

  _configured() {
    return !this._panelData || this._panelData.configured !== false;
  }

  _schedule() {
    window.clearTimeout(this._timer);
    const watching = this._updateStatus();
    const live = ["authenticating", "waiting_for_pin"].includes(this._state.state)
      || (watching !== null && watching.phase === "working");
    // A watched update is inferred entirely from the panel payload, so it is
    // the one case that needs it even on the auth tab.
    const needsPanel = watching !== null || !(live || this._tab === "auth");
    this._timer = window.setTimeout(
      () => (needsPanel
        ? Promise.all([this._refresh(), this._refreshPanel()])
        : this._refresh()
      ).then(() => this._schedule()),
      live ? 2000 : 15000,
    );
  }

  async _api(method, path, body) {
    if (!this._hass) throw new Error("Home Assistant is not ready");
    return this._hass.callApi(method, `blink_liveview_proxy/auth/${path}`, body);
  }

  async _panelApi(method, path = "", body) {
    if (!this._hass) throw new Error("Home Assistant is not ready");
    return this._hass.callApi(method, `blink_liveview_proxy/panel${path}`, body);
  }

  async _refresh(force = false) {
    if ((this._busy && !force) || !this._hass) return;
    try {
      this._state = await this._api("GET", "status");
      if (["authenticating", "waiting_for_pin"].includes(this._state.state)) this._showLogin = false;
    } catch (error) {
      this._state = this._failureFrom(error, {
        state: "failure",
        message: "Home Assistant did not answer the authentication status request. Reload the page, and check the Home Assistant log.",
      });
    }
    this._render();
  }

  async _refreshPanel() {
    if (!this._hass) return;
    try {
      this._panelData = await this._panelApi("GET");
      this._panelError = "";
    } catch (_error) {
      this._panelError = "Home Assistant could not load proxy details. The integration may still be starting or need reauthentication.";
    }
    this._render();
  }

  _failureFrom(error, fallback) {
    const body = error && error.body;
    if (body && typeof body === "object" && body.message) return body;
    return fallback;
  }

  async _recheck() {
    this._busy = true;
    this._render();
    try {
      await Promise.all([this._refresh(true), this._refreshPanel()]);
    } finally {
      this._busy = false;
      this._render();
      this._schedule();
    }
  }

  async _startLogin(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const usernameInput = form.elements.username;
    const passwordInput = form.elements.password;
    const username = usernameInput.value;
    const password = passwordInput.value;
    usernameInput.value = "";
    passwordInput.value = "";
    this._busy = true;
    this._state = { state: "authenticating", message: "Contacting Blink securely…" };
    this._render();
    try {
      this._state = await this._api("POST", "login", { username, password });
      this._showLogin = false;
    } catch (error) {
      this._state = this._failureFrom(error, {
        state: "failure",
        message: "Blink login could not be started. Verify the credentials and that no other attempt is active.",
      });
    } finally {
      this._busy = false;
      this._render();
      this._schedule();
    }
  }

  async _submitPin(event) {
    event.preventDefault();
    const input = event.currentTarget.elements.pin;
    const pin = input.value;
    input.value = "";
    this._busy = true;
    this._state.message = "Verifying the newly issued PIN…";
    this._render();
    try {
      this._state = await this._api("POST", "pin", {
        challenge_id: this._state.challenge_id,
        pin,
      });
    } catch (error) {
      this._state = this._failureFrom(error, {
        state: "failure",
        message: "The PIN was rejected or the challenge is stale. Start a new login to request a fresh PIN.",
      });
    } finally {
      this._busy = false;
      this._render();
      this._schedule();
    }
  }

  async _cancel() {
    const challengeId = this._state.challenge_id;
    if (!challengeId) return;
    this._busy = true;
    try {
      this._state = await this._api("POST", "cancel", { challenge_id: challengeId });
    } catch (_error) {
      this._state = { state: "idle", message: "The previous challenge is no longer active." };
    } finally {
      this._busy = false;
      this._render();
    }
  }

  async _startUpdate() {
    if (!window.confirm("Start the Blink Live View Proxy update now? Live views may pause while it restarts.")) return;
    this._busy = true;
    this._notice = "Starting update…";
    this._render();
    try {
      await this._panelApi("POST", "/update", {});
      this._notice = "";
      this._update = {
        since: Date.now(),
        from: (this._panelData && this._panelData.versions.proxy) || "",
      };
    } catch (error) {
      this._notice = this._failureFrom(error, { message: "The update could not be started." }).message;
    } finally {
      this._busy = false;
      this._render();
      this._schedule();
    }
  }

  async _loadYaml() {
    const query = new URLSearchParams({ format: this._yamlFormat });
    if (this._yamlCamera) query.set("camera", this._yamlCamera);
    this._busy = true;
    this._render();
    try {
      const result = await this._panelApi("GET", `/yaml?${query}`);
      this._yaml = result.yaml || "";
      this._notice = "YAML generated from the current camera inventory.";
    } catch (_error) {
      this._notice = "Home Assistant could not generate the dashboard YAML.";
    } finally {
      this._busy = false;
      this._render();
    }
  }

  async _copyYaml() {
    if (!this._yaml) return;
    try {
      await navigator.clipboard.writeText(this._yaml);
      this._notice = "YAML copied to the clipboard.";
    } catch (_error) {
      this._notice = "Clipboard access was denied. Select the YAML and copy it manually.";
    }
    this._render();
  }

  _openEntity(entityId) {
    this.dispatchEvent(new CustomEvent("hass-more-info", {
      detail: { entityId }, bubbles: true, composed: true,
    }));
  }

  _openLive(camera) {
    this.dispatchEvent(new CustomEvent("ll-custom", {
      detail: { blink_liveview_proxy: {
        slug: camera.slug, entity_id: camera.live_entity_id, title: camera.name,
      } },
      bubbles: true, composed: true,
    }));
  }

  _openClips(camera) {
    this.dispatchEvent(new CustomEvent("ll-custom", {
      detail: { blink_liveview_proxy_clips: { slug: camera.slug, title: `${camera.name} Clips` } },
      bubbles: true, composed: true,
    }));
  }

  _refreshSnapshot(camera) {
    this.dispatchEvent(new CustomEvent("ll-custom", {
      detail: { blink_snapshot_refresh: {
        slug: camera.slug, source_entity_id: camera.entity_id,
      } },
      bubbles: true, composed: true,
    }));
  }

  // Home Assistant themes are not tied to prefers-color-scheme - a light OS
  // runs a dark theme perfectly happily - so ask the theme's own background
  // what it is. This is not decoration: the navy half of the wordmark measures
  // 1.28:1 against Home Assistant's dark background, well under the 3.0:1
  // floor for large text, which is why there is a white one to switch to.
  _darkTheme() {
    const styles = getComputedStyle(this);
    const rgb = this._rgb(styles.getPropertyValue("--primary-background-color"))
      || this._rgb(styles.backgroundColor);
    if (!rgb) return window.matchMedia("(prefers-color-scheme: dark)").matches;
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2] < 128;
  }

  // Themes state their colours as hex or rgb(), and a fully transparent
  // computed background says nothing about the theme, so it is not an answer.
  _rgb(value) {
    const text = String(value || "").trim();
    const hex = text.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
    if (hex) {
      const pairs = hex[1].length === 3
        ? [...hex[1]].map((character) => character + character)
        : hex[1].match(/../g);
      return pairs.map((pair) => parseInt(pair, 16));
    }
    const parts = text.match(/^rgba?\(([^)]+)\)$/i);
    if (!parts) return null;
    const numbers = parts[1].split(/[\s,/]+/).map(Number).filter((n) => !Number.isNaN(n));
    if (numbers.length < 3 || numbers[3] === 0) return null;
    return numbers.slice(0, 3);
  }

  _wordmark() {
    const file = this._darkTheme() ? "dark_logo.png" : "logo.png";
    // Intrinsic dimensions, so the header reserves the right box before the
    // image loads; CSS scales it. The alt text names the heading it sits in.
    return `<img class="wordmark" width="520" height="155" alt="Blink Live View Proxy"
      src="/api/blink_liveview_proxy/assets/${file}">`;
  }

  _renderExpiry() {
    const node = this.shadowRoot.getElementById("expires");
    if (!node) return;
    node.textContent = Number.isFinite(this._state.expires_in)
      ? `Challenge expires in about ${Math.max(0, this._state.expires_in)} seconds.` : "";
  }

  // Three answers, not two. "Not checked" is a real outcome - an older proxy
  // cannot report its environment, and Lovelace may not have started - and
  // colouring it like a failure would send people to fix working installs.
  _updateStatus() {
    if (!this._update) return null;
    const data = this._panelData;
    const versions = (data && data.versions) || {};
    const healthy = Boolean(data && data.health
      && (data.health.ok === true || data.health.status === "ok"));
    const arrived = versions.proxy && versions.proxy !== this._update.from;

    if (arrived && healthy && versions.proxy === versions.integration) {
      return { phase: "done", label: `The proxy is running ${versions.proxy}.` };
    }
    if (Date.now() - this._update.since > UPDATE_TIMEOUT_MS) {
      return {
        phase: "timeout",
        label: `The proxy still reports ${versions.proxy || "no version"} after five minutes. It may have failed — check the proxy log, and the Home Assistant log.`,
      };
    }
    if (this._panelError || !healthy) {
      return { phase: "working", label: "The proxy is restarting. Live views are unavailable until it answers again." };
    }
    return { phase: "working", label: "Update started. The proxy has not restarted yet." };
  }

  _updateBannerHtml() {
    const status = this._updateStatus();
    if (!status) return "";
    const busy = status.phase === "working";
    return `<ha-card class="update-banner ${status.phase}">
      <h2>Proxy update</h2>
      <div class="bar ${busy ? "busy" : ""}" role="progressbar" aria-label="Proxy update progress"><span></span></div>
      <p class="detail">${this._escape(status.label)}</p>
      ${busy ? "" : `<button type="button" id="update-reload">Reload page</button><button type="button" class="secondary" id="update-dismiss">Dismiss</button>`}
    </ha-card>`;
  }

  _checkLabel(check) {
    if (check.state === "ok") return "Ready";
    if (check.state === "unknown") return "Not checked";
    return check.required ? "Action needed" : "Not found";
  }

  _prerequisitesHtml() {
    const prerequisites = this._panelData.prerequisites || {};
    // Home Assistant caches frontend modules hard, so a browser can hold this
    // file across a downgrade. Never let a payload it does not recognise take
    // the whole Overview tab down with it.
    const checks = prerequisites.checks || [];
    if (!checks.length) return "";
    const summary = prerequisites.summary || { ok: 0, total: checks.length };
    if (!this._openHelp) {
      this._openHelp = new Set(
        checks.filter((check) => check.state !== "ok").map((check) => check.key),
      );
    }
    const headline = [
      `${summary.ok} of ${summary.total} ready`,
      summary.missing ? `${summary.missing} to look at` : "",
      summary.unknown ? `${summary.unknown} not checked` : "",
    ].filter(Boolean).join(" · ");

    return `
      <ha-card>
        <h2>Prerequisites</h2>
        <p class="muted">${this._escape(headline)}. The setup steps stay here whether or not a check passes — they are the reference for rebuilding this on another host, not just a repair note.</p>
        <ul class="checks">${checks.map((check) => `
          <li class="check ${check.state} ${check.required ? "required" : "optional"}">
            <div class="check-head">
              <span class="dot" aria-hidden="true"></span>
              <div class="check-text">
                <h3>${this._escape(check.label)}</h3>
                <p class="detail">${this._escape(check.detail)}</p>
                <p class="muted">Needed for: ${this._escape(check.needed_for)}</p>
              </div>
              <span class="badge status">${this._escape(this._checkLabel(check))}</span>
            </div>
            <details data-help="${this._escape(check.key)}" ${this._openHelp.has(check.key) ? "open" : ""}>
              <summary>Setup and reference</summary>
              <ul>${check.instructions.map((line) => `<li>${this._escape(line)}</li>`).join("")}</ul>
              ${check.docs_url ? `<p><a href="${this._escape(check.docs_url)}" target="_blank" rel="noreferrer noopener">Documentation</a></p>` : ""}
            </details>
          </li>`).join("")}</ul>
      </ha-card>`;
  }

  _setupHtml() {
    const docs = "https://github.com/Teethree89/ha-blink-live-view-proxy/blob/main/docs/INSTALL.md";
    return `<ha-card class="setup">
      <h2>Two halves, and only one is here yet</h2>
      <p class="muted">This integration is installed, but it is a client: the proxy is what signs in to Blink and streams the cameras, and none is connected. Run the proxy first, then add the integration and point it at the proxy. The checks below already say what else this Home Assistant needs.</p>
      <ol class="paths">
        <li><strong>Home Assistant OS or Supervised</strong> — install the <em>Blink Live View Proxy</em> add-on from this repository under Settings → Add-ons → Add-on Store → ⋮ → Repositories, start it, and complete the Blink login in its log. It shares its token with this integration, so the form below arrives filled in. <a href="${docs}#a--home-assistant-os-or-supervised" target="_blank" rel="noreferrer noopener">Steps</a></li>
        <li><strong>A Linux host with systemd</strong> — one command on that host installs the service and prints the URL and token to use. <a href="${docs}#b--linux-host-with-systemd" target="_blank" rel="noreferrer noopener">Steps</a></li>
        <li><strong>Docker, a NAS, anything else</strong> — run the published image with <code>/data</code> on a volume. <a href="${docs}#c--docker-anywhere" target="_blank" rel="noreferrer noopener">Steps</a></li>
      </ol>
      <button type="button" id="add-integration">Add the integration</button>
      <button type="button" class="secondary" id="recheck" ${this._busy ? "disabled" : ""}>Check again</button>
    </ha-card>`;
  }

  _overviewHtml() {
    if (!this._panelData) return `<ha-card><p>${this._escape(this._panelError || "Loading proxy details…")}</p></ha-card>`;
    const data = this._panelData;
    if (data.configured === false) return `${this._setupHtml()}${this._prerequisitesHtml()}`;
    const healthy = data.health && (data.health.ok === true || data.health.status === "ok");
    const behind = data.versions.behind;
    return `
      <section class="metrics">
        <ha-card><span>Proxy</span><strong class="${healthy ? "good" : "warn"}">${healthy ? "Healthy" : "Check status"}</strong></ha-card>
        <ha-card><span>Cameras</span><strong>${data.cameras.length}</strong></ha-card>
        <ha-card><span>Integration</span><strong>${this._escape(data.versions.integration)}</strong></ha-card>
        <ha-card><span>Proxy version</span><strong class="${behind ? "warn" : "good"}">${this._escape(data.versions.proxy)}</strong></ha-card>
      </section>
      <ha-card>
        <h2>${this._escape(data.title)}</h2>
        <dl><dt>Proxy URL</dt><dd><code>${this._escape(data.base_url)}</code></dd><dt>Authentication</dt><dd>${this._escape(data.status.auth_state || "unknown")}</dd><dt>Update method</dt><dd>${this._escape(data.update.method || "manual")}</dd>${data.environment && data.environment.python ? `<dt>Proxy host</dt><dd>Python ${this._escape(data.environment.python)}</dd>` : ""}</dl>
        ${behind ? `<p class="warn">The proxy is behind integration ${this._escape(data.versions.integration)}.</p>` : `<p class="good">The integration and proxy versions are aligned.</p>`}
        ${behind && data.update.available ? `<button id="update" ${this._busy ? "disabled" : ""}>Update proxy</button>` : !data.update.available ? `<p class="muted">Updates are manual for this installation${data.update.blocker ? `: ${this._escape(data.update.blocker)}` : "."}</p>` : ""}
        <button type="button" class="secondary" id="recheck" ${this._busy ? "disabled" : ""}>Refresh details</button>
      </ha-card>
      ${this._updateBannerHtml()}
      ${this._prerequisitesHtml()}`;
  }

  _camerasHtml() {
    if (!this._panelData) return `<ha-card><p>${this._escape(this._panelError || "Loading cameras…")}</p></ha-card>`;
    if (!this._configured()) return `<ha-card><p>No proxy is connected yet, so there are no cameras to list. The Overview tab has the install steps.</p></ha-card>`;
    if (!this._panelData.cameras.length) return `<ha-card><p>No configured cameras were reported.</p></ha-card>`;
    return `<section class="camera-grid">${this._panelData.cameras.map((camera, index) => `
      <ha-card class="camera-card">
        <div class="camera-head"><div><h2>${this._escape(camera.name)}</h2><p class="muted">${this._escape(camera.camera_type || "camera")} · ${this._escape(camera.product_type || "unknown model")}</p></div><span class="badge">${camera.ptt_supported ? "PTT" : "view only"}</span></div>
        <dl><dt>Serial</dt><dd>${this._escape(camera.serial || "—")}</dd><dt>Network</dt><dd>${this._escape(camera.network_id || "—")}</dd><dt>Source</dt><dd><code>${this._escape(camera.entity_id || "—")}</code></dd></dl>
        <p class="capabilities">${camera.capabilities.map((item) => `<span class="badge">${this._escape(item.replaceAll("_", " "))}</span>`).join("")}</p>
        <div class="actions"><button data-live="${index}" ${camera.live_entity_id ? "" : "disabled"}>Live view</button><button class="secondary" data-clips="${index}">Clips</button><button class="secondary" data-snapshot="${index}">Refresh snapshot</button></div>
        <h3>Home Assistant entities</h3>
        <div class="entities">${camera.entities.map((entity) => `<button class="entity ${entity.disabled ? "disabled" : ""}" data-entity="${this._escape(entity.entity_id)}"><span>${this._escape(entity.name)}</span><small>${this._escape(entity.entity_id)} · ${entity.disabled ? "disabled" : this._escape(entity.state)}${entity.unit ? ` ${this._escape(entity.unit)}` : ""}</small></button>`).join("") || `<p class="muted">No related entities found.</p>`}</div>
      </ha-card>`).join("")}</section>`;
  }

  _authHtml() {
    if (!this._configured()) {
      return `<ha-card><h2>Blink Authentication</h2><p class="muted">Signing in to Blink happens inside the proxy, and none is connected yet. Set one up from the Overview tab; this page then drives its login without the proxy token ever reaching the browser.</p></ha-card>`;
    }
    const state = this._state.state || "idle";
    const waiting = state === "waiting_for_pin";
    const active = state === "authenticating" || waiting;
    const showLogin = this._showLogin || ["idle", "expired", "failure"].includes(state);
    return `<ha-card>
      <h2>Blink Authentication</h2>
      <p class="muted">Credentials and PINs pass through Home Assistant in request bodies and are never stored by this page.</p>
      <div class="state ${state}" role="status"><strong>${this._escape(state.replaceAll("_", " "))}</strong><br>${this._escape(this._state.message || "")}</div>
      <p class="muted" id="expires"></p>
      <button type="button" class="secondary" id="recheck" ${this._busy ? "disabled" : ""}>Check proxy</button>
      ${this._state.remedy ? `<div class="remedy"><h3>How to fix it</h3><p class="muted">This install needs a manual repair before dashboard authentication can reach it.</p><pre>${this._escape(this._state.remedy)}</pre></div>` : ""}
      ${waiting ? `<form id="pin-form"><h3>Enter the new Blink PIN</h3><label for="pin">2FA PIN</label><input id="pin" name="pin" type="password" inputmode="numeric" pattern="[0-9]{4,10}" minlength="4" maxlength="10" autocomplete="off" required><button type="submit" ${this._busy ? "disabled" : ""}>Verify PIN</button><button type="button" class="secondary" id="cancel" ${this._busy ? "disabled" : ""}>Cancel</button></form>` : ""}
      ${active && !waiting ? `<p>Please wait. Do not restart the proxy while its OAuth session is open.</p><button type="button" class="secondary" id="cancel" ${this._busy ? "disabled" : ""}>Cancel</button>` : ""}
      ${showLogin ? `<form id="login-form"><h3>${this._state.authenticated ? "Reauthenticate deliberately" : "Start Blink login"}</h3><label for="username">Blink email</label><input id="username" name="username" type="email" autocomplete="off" autocapitalize="none" spellcheck="false" maxlength="320" required><label for="password">Blink password</label><input id="password" name="password" type="password" autocomplete="off" maxlength="1024" required><button type="submit" ${this._busy ? "disabled" : ""}>Start login</button></form>` : ""}
      ${state === "success" && !showLogin ? `<button type="button" id="reauth">Reauthenticate</button>` : ""}
    </ha-card>`;
  }

  _yamlHtml() {
    const cameras = this._panelData ? this._panelData.cameras : [];
    if (!this._configured()) {
      return `<ha-card><h2>Dashboard YAML</h2><p class="muted">The YAML is built from the cameras the proxy discovers, and no proxy is connected yet. Set one up from the Overview tab first.</p></ha-card>`;
    }
    return `<ha-card><h2>Dashboard YAML</h2><p class="muted">Build a whole dashboard, one view, or a paste-ready card from the cameras currently discovered by the proxy. The layout is a sections view, so it fills a desktop and stacks on a phone.</p>
      <div class="yaml-controls"><label>Output<select id="yaml-format"><option value="dashboard" ${this._yamlFormat === "dashboard" ? "selected" : ""}>Whole dashboard</option><option value="view" ${this._yamlFormat === "view" ? "selected" : ""}>One view</option><option value="card" ${this._yamlFormat === "card" ? "selected" : ""}>Card</option></select></label><label>Camera<select id="yaml-camera"><option value="">All cameras</option>${cameras.map((camera) => `<option value="${this._escape(camera.slug)}" ${this._yamlCamera === camera.slug ? "selected" : ""}>${this._escape(camera.name)}</option>`).join("")}</select></label></div>
      <button id="generate-yaml" ${this._busy || !cameras.length ? "disabled" : ""}>Generate YAML</button><button class="secondary" id="copy-yaml" ${!this._yaml ? "disabled" : ""}>Copy YAML</button>
      ${this._yaml ? `<textarea readonly spellcheck="false">${this._escape(this._yaml)}</textarea>` : ""}
    </ha-card>`;
  }

  _render() {
    const signature = JSON.stringify([
      this._tab,
      this._state.state,
      this._state.message,
      this._state.reason,
      this._state.remedy,
      this._state.challenge_id,
      this._state.authenticated,
      this._tab === "auth" ? (this._panelData ? this._panelData.configured : null) : this._panelData,
      this._tab === "auth" ? "" : this._panelError,
      this._notice,
      this._showLogin,
      this._busy,
      this._yaml,
      this._yamlFormat,
      this._yamlCamera,
      // Swapping themes swaps the wordmark, and nothing else here would say so.
      this._darkTheme(),
      // The banner's phase is derived, so nothing above it changes when a
      // watched update crosses into "restarting" or runs out of time.
      this._update ? this._updateStatus().label : "",
    ]);
    if (signature === this._signature) { this._renderExpiry(); return; }
    this._signature = signature;
    const content = { overview: this._overviewHtml(), cameras: this._camerasHtml(), auth: this._authHtml(), yaml: this._yamlHtml() }[this._tab];
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;min-height:100%;box-sizing:border-box;padding:24px;color:var(--primary-text-color);background:var(--primary-background-color);font-family:var(--paper-font-body1_-_font-family,sans-serif)} main{max-width:1100px;margin:0 auto} header{display:flex;align-items:center;gap:10px;margin-bottom:16px} header button.back{display:flex;flex:0 0 auto;width:48px;height:48px;margin:0;padding:12px;background:none;color:var(--primary-text-color);border-radius:50%}header button.back svg{width:24px;height:24px;fill:currentColor} h1{font-size:26px;margin:0} h1 .wordmark{display:block;height:40px;width:auto} h2{font-size:19px;margin:0 0 12px} h3{font-size:15px;margin:20px 0 8px} ha-card{display:block;padding:22px;margin-bottom:18px} nav{display:flex;gap:4px;overflow-x:auto;margin-bottom:20px;border-bottom:1px solid var(--divider-color)} nav button{margin:0;padding:12px 16px;background:transparent;color:var(--secondary-text-color);border-radius:0;border-bottom:3px solid transparent;white-space:nowrap} nav button.active{color:var(--primary-color);border-bottom-color:var(--primary-color)} button{margin:14px 8px 0 0;padding:10px 16px;font:inherit;border:0;border-radius:4px;cursor:pointer;background:var(--primary-color);color:var(--text-primary-color,white)} button.secondary{background:var(--secondary-background-color);color:var(--primary-text-color);border:1px solid var(--divider-color)} button:disabled{opacity:.55;cursor:default}.notice{padding:11px 14px;margin-bottom:16px;border-radius:6px;background:var(--secondary-background-color)}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.metrics ha-card{display:flex;flex-direction:column;gap:8px}.metrics strong{font-size:20px}.good{color:var(--success-color,#2e7d32)}.warn{color:var(--warning-color,#e68a00)}.muted{color:var(--secondary-text-color);font-size:14px;line-height:1.5}dl{display:grid;grid-template-columns:max-content 1fr;gap:8px 18px}dt{color:var(--secondary-text-color)}dd{margin:0;overflow-wrap:anywhere}.camera-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.camera-head{display:flex;justify-content:space-between;gap:12px}.badge{display:inline-block;height:max-content;padding:5px 9px;border-radius:999px;background:var(--secondary-background-color);font-size:12px}.capabilities{display:flex;flex-wrap:wrap;gap:6px}.entities{display:flex;flex-direction:column;gap:6px}.entity{display:flex;flex-direction:column;align-items:flex-start;width:100%;margin:0;padding:10px 12px;text-align:left;background:var(--secondary-background-color);color:var(--primary-text-color);border:1px solid var(--divider-color)}.entity.disabled{opacity:.65}.entity small{color:var(--secondary-text-color);margin-top:3px}.state{border-left:4px solid var(--primary-color);padding:12px 16px;background:var(--secondary-background-color);border-radius:4px}.state.success{border-color:var(--success-color,#2e7d32)}.state.failure,.state.expired{border-color:var(--error-color,#c62828)}label{display:block;margin:14px 0 6px;font-weight:600}input,select,textarea{box-sizing:border-box;width:100%;padding:11px;font:inherit;color:var(--primary-text-color);background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:4px}pre,textarea{font-family:monospace;font-size:13px;line-height:1.45}pre{padding:12px;overflow:auto;background:var(--secondary-background-color)}textarea{min-height:430px;margin-top:16px;white-space:pre}.yaml-controls{display:grid;grid-template-columns:1fr 1fr;gap:14px}.checks{list-style:none;display:flex;flex-direction:column;gap:12px;margin:18px 0 0;padding:0}.check{padding:14px 16px;background:var(--secondary-background-color);border:1px solid var(--divider-color);border-left:4px solid var(--secondary-text-color);border-radius:6px}.check.ok{border-left-color:var(--success-color,#2e7d32)}.check.missing.required{border-left-color:var(--error-color,#c62828)}.check.missing.optional{border-left-color:var(--warning-color,#e68a00)}.check-head{display:flex;align-items:flex-start;gap:12px}.check-text{flex:1 1 auto;min-width:0}.check h3{margin:0;font-size:15px}.check .detail{margin:4px 0 0;font-size:14px;line-height:1.5}.check .muted{margin:4px 0 0;font-size:13px}.dot{flex:0 0 auto;width:10px;height:10px;margin-top:6px;border-radius:50%;background:var(--secondary-text-color)}.check.ok .dot{background:var(--success-color,#2e7d32)}.check.missing.required .dot{background:var(--error-color,#c62828)}.check.missing.optional .dot{background:var(--warning-color,#e68a00)}.badge.status{white-space:nowrap}.check details{margin-top:10px}.check summary{padding:4px 0;font-size:13px;color:var(--secondary-text-color);cursor:pointer}.check details ul{margin:8px 0 0;padding-left:20px;font-size:14px;line-height:1.55;color:var(--secondary-text-color)}.check details li{margin-bottom:6px}.check details a{color:var(--primary-color)}.setup .paths{margin:4px 0 6px;padding-left:22px;line-height:1.55}.setup .paths li{margin-bottom:10px}.setup .paths a{color:var(--primary-color);white-space:nowrap}.setup code{font-size:13px}.update-banner .bar{position:relative;height:6px;margin:4px 0 0;border-radius:999px;background:var(--divider-color);overflow:hidden}.update-banner .bar span{position:absolute;top:0;bottom:0;left:0;width:100%;border-radius:999px;background:var(--primary-color)}.update-banner .bar.busy span{width:40%;animation:blink-proxy-slide 1.4s ease-in-out infinite}.update-banner.done .bar span{background:var(--success-color,#2e7d32)}.update-banner.timeout .bar span{background:var(--warning-color,#e68a00)}.update-banner .detail{margin:12px 0 0;font-size:14px;line-height:1.5}@keyframes blink-proxy-slide{0%{left:-40%}100%{left:100%}}@media(prefers-reduced-motion:reduce){.update-banner .bar.busy span{width:100%;animation:none;opacity:.6}}@media(max-width:760px){:host{padding:14px}.metrics,.camera-grid,.yaml-controls{grid-template-columns:1fr}header{align-items:center}.check-head{flex-wrap:wrap}h1 .wordmark{height:32px}}
    </style><main><header><button type="button" class="back" id="back" aria-label="Back"><svg viewBox="0 0 24 24"><path d="M20,11V13H8L13.5,18.5L12.08,19.92L4.16,12L12.08,4.08L13.5,5.5L8,11H20Z"/></svg></button><div><h1>${this._wordmark()}</h1><p class="muted">Admin dashboard</p></div></header><nav>${[["overview","Overview"],["cameras","Cameras & entities"],["auth","Authentication"],["yaml","YAML"]].map(([key,label]) => `<button data-tab="${key}" class="${this._tab === key ? "active" : ""}">${label}</button>`).join("")}</nav>${this._notice ? `<div class="notice" role="status">${this._escape(this._notice)}</div>` : ""}${content}</main>`;
    this._renderExpiry();
    this.shadowRoot.querySelectorAll("[data-tab]").forEach((node) => node.addEventListener("click", () => { this._tab = node.dataset.tab; this._notice = ""; this._render(); }));
    this.shadowRoot.getElementById("login-form")?.addEventListener("submit", (event) => this._startLogin(event));
    this.shadowRoot.getElementById("pin-form")?.addEventListener("submit", (event) => this._submitPin(event));
    this.shadowRoot.getElementById("cancel")?.addEventListener("click", () => this._cancel());
    this.shadowRoot.getElementById("reauth")?.addEventListener("click", () => { this._showLogin = true; this._render(); });
    this.shadowRoot.getElementById("recheck")?.addEventListener("click", () => this._recheck());
    this.shadowRoot.getElementById("update")?.addEventListener("click", () => this._startUpdate());
    this.shadowRoot.getElementById("add-integration")?.addEventListener("click", () => this._navigate("/config/integrations/dashboard/add?domain=blink_liveview_proxy"));
    this.shadowRoot.querySelectorAll("[data-entity]").forEach((node) => node.addEventListener("click", () => this._openEntity(node.dataset.entity)));
    this.shadowRoot.querySelectorAll("[data-live]").forEach((node) => node.addEventListener("click", () => this._openLive(this._panelData.cameras[Number(node.dataset.live)])));
    this.shadowRoot.querySelectorAll("[data-clips]").forEach((node) => node.addEventListener("click", () => this._openClips(this._panelData.cameras[Number(node.dataset.clips)])));
    this.shadowRoot.querySelectorAll("[data-snapshot]").forEach((node) => node.addEventListener("click", () => this._refreshSnapshot(this._panelData.cameras[Number(node.dataset.snapshot)])));
    this.shadowRoot.getElementById("yaml-format")?.addEventListener("change", (event) => { this._yamlFormat = event.target.value; this._yaml = ""; this._render(); });
    this.shadowRoot.getElementById("yaml-camera")?.addEventListener("change", (event) => { this._yamlCamera = event.target.value; this._yaml = ""; this._render(); });
    this.shadowRoot.getElementById("generate-yaml")?.addEventListener("click", () => this._loadYaml());
    this.shadowRoot.getElementById("copy-yaml")?.addEventListener("click", () => this._copyYaml());
    this.shadowRoot.getElementById("back")?.addEventListener("click", () => this._back());
    // Offered, never automatic: a proxy update needs no reload - the panel
    // repaints from its own poll - and reloading on its own would also hide a
    // failed update behind a fresh page.
    this.shadowRoot.getElementById("update-reload")?.addEventListener("click", () => window.location.reload());
    this.shadowRoot.getElementById("update-dismiss")?.addEventListener("click", () => {
      this._update = null;
      this._render();
      this._schedule();
    });
    // Recorded, never re-rendered: the browser has already opened or closed
    // the element, and rebuilding here would fight the click that did it.
    this.shadowRoot.querySelectorAll("details[data-help]").forEach((node) =>
      node.addEventListener("toggle", () => {
        if (node.open) this._openHelp.add(node.dataset.help);
        else this._openHelp.delete(node.dataset.help);
      }));
  }

  _escape(value) {
    const node = document.createElement("span");
    node.textContent = String(value || "");
    return node.innerHTML;
  }
}

if (!customElements.get("blink-proxy-auth-panel")) {
  customElements.define("blink-proxy-auth-panel", BlinkProxyAuthPanel);
}
