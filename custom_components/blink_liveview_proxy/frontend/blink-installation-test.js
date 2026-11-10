// All actions stay on the installation-test endpoint. This element never
// receives the live proxy's credentials, client, or configuration entry.
const CHECK_LABELS = {
  isolated_source_and_config: "Separate source, configuration and data directory",
  fresh_dependency_install: "Dependencies installed into a new Python environment",
  separate_proxy_started: "Temporary proxy process started on its own port",
  health: "Temporary proxy answered its health check",
  generated_token_authenticated: "New API token accepted by the temporary proxy",
};
const PHASE_LABELS = {
  idle: "Ready to start", preparing: "Preparing the temporary installation…",
  installing_dependencies: "Installing dependencies… This can take a few minutes.",
  starting: "Starting the temporary proxy…", ready: "Temporary installation running",
  failed: "Installation test failed", discarded: "Temporary installation discarded",
};
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);

class BlinkInstallationTest extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._api = null;
    this._data = null;
    this._busy = false;
    this._error = "";
    this._timer = null;
    this._connectionVerified = false;
    this._report = "";
    this._signature = "";
    this._render();
  }

  set api(callback) {
    this._api = callback;
    this._refresh();
  }

  connectedCallback() {
    if (this._api) this._refresh();
  }

  disconnectedCallback() {
    clearTimeout(this._timer);
  }

  async _refresh() {
    clearTimeout(this._timer);
    try {
      this._data = await this._api("GET");
      this._error = "";
    } catch (_error) {
      this._error = "Home Assistant could not read the test status. Try again; the test will expire automatically.";
    }
    this._render();
    if (this.isConnected) this._timer = setTimeout(() => this._refresh(), 2000);
  }

  async _action(action, values = {}) {
    if (this._busy) return;
    this._busy = true;
    this._error = "";
    this._render();
    try {
      const result = await this._api("POST", {
        action, id: this._data?.test?.id, ...values,
      });
      if (action === "start") {
        this._connectionVerified = false;
        this._report = "";
        this._data.test = result;
      } else if (action === "check") {
        this._connectionVerified = result.connection_verified === true;
      } else if (action === "discard") {
        this._data.test = result;
      } else {
        this._data.auth = result;
      }
    } catch (_error) {
      this._error = "The test action failed. Refresh its status or discard the test and try again.";
    } finally {
      this._busy = false;
      await this._refresh();
    }
  }

  async _finish() {
    const data = this._data || {};
    const test = data.test || {};
    this._report = [
      "Blink Live View Proxy — installation test",
      `Home Assistant: ${data.home_assistant_installation || "unknown"}`,
      `Proxy installation: ${test.proxy_installation || "unknown"}`,
      `Source version: ${test.source_version || "unknown"}`,
      `Reused settings: ${test.reuse_settings ? "yes" : "no"}`,
      ...((test.checks || []).map((key) => `PASS: ${CHECK_LABELS[key] || key}`)),
      `HA setup connection validation: ${this._connectionVerified ? "PASS" : "not run"}`,
      `Separate Blink login: ${data.auth?.authenticated ? "PASS" : "not completed"}`,
      `Camera discovery: ${Number.isInteger(data.camera_count) ? data.camera_count : "not checked"}`,
      `Coverage: ${test.coverage || "unknown"}`,
      "Marketplace/HACS download, native provisioning and live video: not tested by this run.",
    ].join("\n");
    await this._action("discard");
    this._report += `\nCleanup: ${this._data?.test?.cleanup_reason || "pending"}`;
    this._render();
  }

  _render() {
    const signature = JSON.stringify([this._data, this._busy, this._error, this._connectionVerified, this._report]);
    if (signature === this._signature) return;
    this._signature = signature;
    // Polls preserve typed credentials and focus when no visible state changed.
    const active = this.shadowRoot.activeElement;
    const draft = {};
    for (const name of ["username", "password", "pin"]) {
      const input = this.shadowRoot.querySelector(`[name="${name}"]`);
      if (input) draft[name] = input.value;
    }
    const data = this._data;
    const test = data?.test || {};
    const phase = test.phase || "idle";
    const exists = !["idle", "discarded"].includes(phase);
    const ready = phase === "ready";
    const auth = data?.auth || {};
    const disabled = this._busy ? "disabled" : "";
    const phaseLabel = test.cleanup_reason === "expired"
      ? "Temporary installation expired and was removed"
      : PHASE_LABELS[phase] || phase;
    this.shadowRoot.innerHTML = `<style>
      :host{display:block}ha-card{display:block;padding:22px;margin-bottom:18px}
      h2{margin:0 0 12px;font-size:19px}h3{font-size:16px}p,li{line-height:1.5}
      .muted{color:var(--secondary-text-color)}.notice{padding:12px;background:var(--secondary-background-color);border-radius:6px}
      .error{color:var(--error-color,#c62828)}label{display:block;margin:14px 0 6px}input:not([type=checkbox]),textarea{box-sizing:border-box;width:100%;padding:11px;font:inherit;color:var(--primary-text-color);background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:4px}
      input[type=checkbox]{margin-right:8px}button{margin:14px 8px 0 0;padding:10px 16px;font:inherit;border:0;border-radius:4px;background:var(--primary-color);color:var(--text-primary-color,white);cursor:pointer}button.secondary{background:var(--secondary-background-color);color:var(--primary-text-color)}button:disabled{opacity:.55;cursor:default}textarea{min-height:260px;font-family:monospace;font-size:13px}li{margin-bottom:6px}
    </style><ha-card>
      <h2>Test installation</h2>
      <p>Build a real temporary proxy with a fresh Python environment, generated token and separate data. Complete setup checks, then discard it.</p>
      <p class="notice">The temporary install creates no Home Assistant integration entry. Reusing settings excludes saved Blink sessions, passwords and the working API token. A new Blink login is optional and may send a new 2FA code.</p>
      ${data ? `<p><strong>Home Assistant:</strong> ${escapeHtml(data.home_assistant_installation)}<br><strong>Proxy:</strong> ${escapeHtml(test.proxy_installation || "not detected")}</p>` : `<p>Detecting installation…</p>`}
      ${this._error ? `<p role="alert" class="error">${escapeHtml(this._error)}</p>` : ""}
      ${data && !data.supported ? `<p>${escapeHtml(data.message)}</p><button id="refresh">Check again</button>` : ""}
      ${!exists && test.cleanup_reason ? `<p role="status">${escapeHtml(phaseLabel)}</p>` : ""}
      ${data?.supported && !exists ? `<label><input type="checkbox" id="reuse" checked>Use current installation settings</label><p class="muted">Reuse camera mappings and playback settings, or leave unchecked for the default configuration.</p><button id="start" ${disabled}>Start temporary installation</button>` : ""}
      ${exists ? `<h3 role="status">${escapeHtml(phaseLabel)}</h3><p class="muted">Expires in ${Math.ceil((test.expires_in || 0) / 60)} minutes. Closing this page leaves automatic cleanup active.</p><ol>${(test.checks || []).map((key) => `<li>${escapeHtml(CHECK_LABELS[key] || key)}</li>`).join("")}</ol>${test.error ? `<p class="error">${escapeHtml(test.error)}. Discard this run before retrying.</p>` : ""}` : ""}
      ${ready ? `<h3>Connect Home Assistant</h3><p>Run the same URL/token validation as Add integration, targeting the temporary proxy and stopping before saving a connection.</p><button id="check" ${disabled}>${this._connectionVerified ? "Connection verified — check again" : "Test connection"}</button>
        ${this._connectionVerified ? `<h3>Blink login (optional)</h3><p class="muted">You can finish now, or verify a separate login and camera discovery. This creates a new Blink session; it does not reuse the working session.</p><p role="status">${escapeHtml(auth.message || "Not started")}</p>
          ${auth.state === "waiting_for_pin" ? `<form id="test-pin"><label for="test-pin-input">New Blink PIN</label><input id="test-pin-input" name="pin" type="password" inputmode="numeric" autocomplete="off" required><button ${disabled}>Verify PIN</button></form>`
            : auth.authenticated ? `<p>Camera discovery: ${Number.isInteger(data.camera_count) ? data.camera_count : "checking…"}</p>`
              : auth.state === "authenticating" ? `<p>Waiting for Blink…</p>`
                : `<form id="test-login"><label for="test-username">Blink email</label><input id="test-username" name="username" type="email" autocomplete="off" required><label for="test-password">Blink password</label><input id="test-password" name="password" type="password" autocomplete="off" required><button ${disabled}>Start separate Blink login</button></form>`}` : ""}` : ""}
      ${exists ? `<button id="finish" class="secondary" ${disabled}>${ready ? "Finish and discard" : "Cancel and discard"}</button>` : ""}
      ${this._report ? `<h3>Test report</h3><p>${phase === "discarded" ? "The temporary process and data have been removed." : "Cleanup has not been confirmed. Retry discard or wait for automatic expiry."}</p><textarea readonly aria-label="Installation test report">${escapeHtml(this._report)}</textarea><button id="copy" class="secondary">Copy report</button>` : ""}
      <p class="muted">${escapeHtml(test.coverage || "Tests a fresh Python proxy installation inside the detected host environment.")} This does not install another Supervisor add-on, Docker container or systemd service.</p>
    </ha-card>`;
    for (const [name, value] of Object.entries(draft)) {
      const input = this.shadowRoot.querySelector(`[name="${name}"]`);
      if (input) input.value = value;
    }
    if (active?.id) this.shadowRoot.getElementById(active.id)?.focus();
    this.shadowRoot.getElementById("refresh")?.addEventListener("click", () => this._refresh());
    this.shadowRoot.getElementById("start")?.addEventListener("click", () => this._action("start", {
      reuse_settings: this.shadowRoot.getElementById("reuse").checked,
    }));
    this.shadowRoot.getElementById("check")?.addEventListener("click", () => this._action("check"));
    this.shadowRoot.getElementById("finish")?.addEventListener("click", () => this._finish());
    this.shadowRoot.getElementById("copy")?.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(this._report); }
      catch (_error) { this._error = "Copy was unavailable. Select the report text and copy it manually."; this._render(); }
    });
    this.shadowRoot.getElementById("test-login")?.addEventListener("submit", (event) => {
      event.preventDefault();
      const username = event.currentTarget.elements.username.value;
      const password = event.currentTarget.elements.password.value;
      event.currentTarget.reset();
      this._action("login", { username, password });
    });
    this.shadowRoot.getElementById("test-pin")?.addEventListener("submit", (event) => {
      event.preventDefault();
      const pin = event.currentTarget.elements.pin.value;
      event.currentTarget.reset();
      this._action("pin", { pin, challenge_id: this._data.auth.challenge_id });
    });
  }
}

if (!customElements.get("blink-installation-test")) {
  customElements.define("blink-installation-test", BlinkInstallationTest);
}
