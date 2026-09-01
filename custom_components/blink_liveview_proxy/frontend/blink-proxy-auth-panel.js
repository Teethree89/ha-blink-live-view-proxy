class BlinkProxyAuthPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._state = { state: "idle", message: "Loading authentication state…" };
    this._showLogin = false;
    this._busy = false;
    this._poll = null;
    // Polling runs while the PIN is being typed, so the DOM is only rebuilt
    // when something a user can see actually changed. Rebuilding on every tick
    // would clear the field and steal focus mid-entry.
    this._signature = null;
  }

  set hass(value) {
    const first = !this._hass;
    this._hass = value;
    if (first) {
      this._render();
      this._refresh();
      this._poll = window.setInterval(() => this._refresh(), 2000);
    }
  }

  set narrow(_value) {}
  set panel(_value) {}
  set route(_value) {}

  disconnectedCallback() {
    if (this._poll) window.clearInterval(this._poll);
    this._poll = null;
  }

  async _api(method, path, body) {
    if (!this._hass) throw new Error("Home Assistant is not ready");
    return this._hass.callApi(method, `blink_liveview_proxy/auth/${path}`, body);
  }

  async _refresh(force = false) {
    if ((this._busy && !force) || !this._hass) return;
    try {
      this._state = await this._api("GET", "status");
      if (["authenticating", "waiting_for_pin"].includes(this._state.state)) {
        this._showLogin = false;
      }
      this._render();
    } catch (error) {
      // The status route reports proxy problems as a state, so reaching this
      // means Home Assistant itself did not answer.
      this._state = this._failureFrom(error, {
        state: "failure",
        message: "Home Assistant did not answer the authentication status request. Reload the page, and check the Home Assistant log.",
      });
      this._render();
    }
  }

  _failureFrom(error, fallback) {
    // Action routes answer with an error status but a classified body.
    const body = error && error.body;
    if (body && typeof body === "object" && body.message) return body;
    return fallback;
  }

  async _recheck() {
    this._busy = true;
    this._render();
    try {
      await this._refresh(true);
    } finally {
      this._busy = false;
      this._render();
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

  _renderExpiry() {
    const node = this.shadowRoot.getElementById("expires");
    if (!node) return;
    node.textContent = Number.isFinite(this._state.expires_in)
      ? `Challenge expires in about ${Math.max(0, this._state.expires_in)} seconds.`
      : "";
  }

  _render() {
    const state = this._state.state || "idle";
    const waiting = state === "waiting_for_pin";
    const active = state === "authenticating" || waiting;
    const showLogin = this._showLogin || ["idle", "expired", "failure"].includes(state);
    const signature = JSON.stringify([
      state,
      this._state.message,
      this._state.reason,
      this._state.remedy,
      this._state.challenge_id,
      this._state.authenticated,
      showLogin,
      this._busy,
    ]);
    if (signature === this._signature) {
      this._renderExpiry();
      return;
    }
    this._signature = signature;

    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; min-height:100%; box-sizing:border-box; padding:24px; color:var(--primary-text-color); background:var(--primary-background-color); font-family:var(--paper-font-body1_-_font-family, sans-serif); }
        main { max-width:640px; margin:0 auto; }
        ha-card { display:block; padding:24px; }
        h1 { font-size:24px; margin:0 0 8px; }
        h2 { font-size:18px; margin:24px 0 12px; }
        p { line-height:1.5; }
        .state { border-left:4px solid var(--primary-color); padding:12px 16px; background:var(--secondary-background-color); border-radius:4px; }
        .state.success { border-color:var(--success-color, #2e7d32); }
        .state.failure, .state.expired { border-color:var(--error-color, #c62828); }
        label { display:block; margin:14px 0 6px; font-weight:600; }
        input { box-sizing:border-box; width:100%; padding:12px; font:inherit; color:var(--primary-text-color); background:var(--card-background-color); border:1px solid var(--divider-color); border-radius:4px; }
        button { margin:16px 8px 0 0; padding:10px 16px; font:inherit; border:0; border-radius:4px; cursor:pointer; background:var(--primary-color); color:var(--text-primary-color, white); }
        button.secondary { background:var(--secondary-background-color); color:var(--primary-text-color); border:1px solid var(--divider-color); }
        button:disabled { opacity:.55; cursor:default; }
        .muted { color:var(--secondary-text-color); font-size:14px; }
        code { overflow-wrap:anywhere; }
        pre { margin:12px 0 0; padding:12px; overflow-x:auto; border-radius:4px; background:var(--secondary-background-color); border:1px solid var(--divider-color); font-size:13px; line-height:1.45; }
        .remedy { margin-top:16px; }
        .remedy h3 { font-size:15px; margin:0; }
      </style>
      <main>
        <ha-card>
          <h1>Blink Authentication</h1>
          <p class="muted">Admin-only. Credentials and PINs are sent in request bodies through Home Assistant and are never stored by this page.</p>
          <div class="state ${state}" role="status"><strong>${this._escape(state.replaceAll("_", " "))}</strong><br>${this._escape(this._state.message || "")}</div>
          <p class="muted" id="expires"></p>
          <button type="button" class="secondary" id="recheck" ${this._busy ? "disabled" : ""}>Check proxy</button>
          ${this._state.remedy ? `
            <div class="remedy">
              <h3>How to fix it</h3>
              <p class="muted">Home Assistant cannot run this for you: the proxy is a separate service, on a host it has no shell on.</p>
              <pre>${this._escape(this._state.remedy)}</pre>
            </div>` : ""}
          ${waiting ? `
            <form id="pin-form">
              <h2>Enter the new Blink PIN</h2>
              <p>Use only the PIN Blink issued for this attempt. Keep the proxy running.</p>
              <label for="pin">2FA PIN</label>
              <input id="pin" name="pin" type="password" inputmode="numeric" pattern="[0-9]{4,10}" minlength="4" maxlength="10" autocomplete="off" required>
              <button type="submit" ${this._busy ? "disabled" : ""}>Verify PIN</button>
              <button type="button" class="secondary" id="cancel" ${this._busy ? "disabled" : ""}>Cancel</button>
            </form>` : ""}
          ${active && !waiting ? `<p>Please wait. Do not restart the proxy; the OAuth session must remain open.</p><button type="button" class="secondary" id="cancel" ${this._busy ? "disabled" : ""}>Cancel</button>` : ""}
          ${showLogin ? `
            <form id="login-form">
              <h2>${this._state.authenticated ? "Reauthenticate deliberately" : "Start Blink login"}</h2>
              <label for="username">Blink email</label>
              <input id="username" name="username" type="email" autocomplete="off" autocapitalize="none" spellcheck="false" maxlength="320" required>
              <label for="password">Blink password</label>
              <input id="password" name="password" type="password" autocomplete="off" maxlength="1024" required>
              <button type="submit" ${this._busy ? "disabled" : ""}>Start login</button>
            </form>` : ""}
          ${state === "success" && !showLogin ? `<button type="button" id="reauth">Reauthenticate</button>` : ""}
        </ha-card>
      </main>`;

    this._renderExpiry();
    this.shadowRoot.getElementById("login-form")?.addEventListener("submit", (event) => this._startLogin(event));
    this.shadowRoot.getElementById("pin-form")?.addEventListener("submit", (event) => this._submitPin(event));
    this.shadowRoot.getElementById("cancel")?.addEventListener("click", () => this._cancel());
    this.shadowRoot.getElementById("reauth")?.addEventListener("click", () => { this._showLogin = true; this._render(); });
    this.shadowRoot.getElementById("recheck")?.addEventListener("click", () => this._recheck());
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
