(function () {
  if (window.__blinkLiveviewDialogLoaded) return;
  window.__blinkLiveviewDialogLoaded = true;

  const STYLE_ID = "blink-liveview-dialog-style";
  const DIALOG_ID = "blink-liveview-dialog";

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;

    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = `
      #${DIALOG_ID} {
        position: fixed;
        inset: 0;
        z-index: 2147483000;
        display: grid;
        place-items: center;
        background: rgba(0, 0, 0, 0.72);
        color: var(--primary-text-color, #f8fafc);
      }
      #${DIALOG_ID} .blink-liveview-shell {
        width: min(1120px, calc(100vw - 32px));
        height: min(760px, calc(100vh - 32px));
        display: grid;
        grid-template-rows: 56px 1fr;
        overflow: hidden;
        border-radius: 8px;
        background: var(--card-background-color, #111827);
        box-shadow: 0 24px 80px rgba(0, 0, 0, 0.48);
      }
      #${DIALOG_ID} .blink-liveview-header {
        display: grid;
        grid-template-columns: 48px 1fr auto;
        align-items: center;
        gap: 8px;
        min-width: 0;
        padding: 0 8px;
        background: var(--app-header-background-color, #1f2937);
      }
      #${DIALOG_ID} .blink-liveview-title {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        font-size: 18px;
        font-weight: 650;
      }
      #${DIALOG_ID} button {
        width: 40px;
        height: 40px;
        border: 0;
        border-radius: 999px;
        background: transparent;
        color: inherit;
        cursor: pointer;
        font-size: 28px;
        line-height: 40px;
      }
      #${DIALOG_ID} button.blink-liveview-header-action {
        width: auto;
        min-width: 40px;
        border-radius: 6px;
        padding: 0 12px;
        font-size: 13px;
        font-weight: 800;
      }
      #${DIALOG_ID} button:hover {
        background: rgba(148, 163, 184, 0.16);
      }
      #${DIALOG_ID} iframe {
        width: 100%;
        height: 100%;
        border: 0;
        background: #05070a;
      }
      #${DIALOG_ID} .blink-liveview-error {
        display: grid;
        place-items: center;
        padding: 24px;
        text-align: center;
        color: var(--secondary-text-color, #cbd5e1);
        background: #05070a;
      }
      @media (max-width: 720px) {
        #${DIALOG_ID} {
          place-items: stretch;
        }
        #${DIALOG_ID} .blink-liveview-shell {
          width: 100vw;
          height: 100vh;
          border-radius: 0;
        }
      }
    `;
    document.head.appendChild(style);
  }

  function closeDialog() {
    const existing = document.getElementById(DIALOG_ID);
    if (!existing) return;
    const iframe = existing.querySelector("iframe");
    if (iframe) iframe.removeAttribute("src");
    existing.remove();
  }

  function hassFromEvent(event) {
    const path = typeof event.composedPath === "function" ? event.composedPath() : [];
    for (const item of path) {
      if (item && item.hass) return item.hass;
    }
    const root = document.querySelector("home-assistant");
    return root && root.hass ? root.hass : null;
  }

  function openFrameDialog({ title, src, headerAction }) {
    ensureStyle();
    closeDialog();

    const root = document.createElement("div");
    root.id = DIALOG_ID;

    const shell = document.createElement("section");
    shell.className = "blink-liveview-shell";
    shell.setAttribute("role", "dialog");
    shell.setAttribute("aria-modal", "true");
    shell.setAttribute("aria-label", title);

    const header = document.createElement("header");
    header.className = "blink-liveview-header";

    const close = document.createElement("button");
    close.type = "button";
    close.setAttribute("aria-label", "Close");
    close.textContent = "x";
    close.addEventListener("click", closeDialog);

    const heading = document.createElement("div");
    heading.className = "blink-liveview-title";
    heading.textContent = title;

    header.append(close, heading);
    if (headerAction) {
      header.append(headerAction);
    } else {
      header.append(document.createElement("span"));
    }
    shell.append(header);

    if (!src) {
      const error = document.createElement("div");
      error.className = "blink-liveview-error";
      error.textContent = "Camera access token is not ready yet. Refresh the dashboard and try again.";
      shell.append(error);
    } else {
      const iframe = document.createElement("iframe");
      iframe.allow = "autoplay; fullscreen; microphone; picture-in-picture";
      iframe.src = src;
      shell.append(iframe);
    }

    root.append(shell);
    root.addEventListener("click", (event) => {
      if (event.target === root) closeDialog();
    });
    document.body.append(root);
  }

  function openLiveDialog(config, hass) {
    const slug = config.slug;
    const entityId = config.entity_id;
    const state = hass && entityId ? hass.states[entityId] : null;
    const token = state && state.attributes ? state.attributes.access_token : "";
    const title =
      config.title ||
      (state && state.attributes && state.attributes.friendly_name) ||
      `Blink Live ${slug}`;
    let src = "";
    if (slug && entityId && token) {
      src = `/api/blink_liveview_proxy/cameras/${encodeURIComponent(
        slug
      )}/player?token=${encodeURIComponent(token)}`;
    }
    openFrameDialog({ title, src });
  }

  function openClipsDialog(config, hass) {
    const params = new URLSearchParams();
    if (config.slug) params.set("camera", config.slug);
    const entityId =
      config.entity_id ||
      (config.slug ? `camera.blink_live_${config.slug}` : "");
    const state = hass && entityId ? hass.states[entityId] : null;
    const token = state && state.attributes ? state.attributes.access_token : "";
    if (token) params.set("token", token);
    const query = params.toString();
    openFrameDialog({
      title: config.title || "Blink Local Clips",
      src: `/api/blink_liveview_proxy/clips/viewer${query ? `?${query}` : ""}`
    });
  }

  async function refreshSnapshot(config, hass) {
    if (!config || !config.slug) return;
    const entityId =
      config.entity_id ||
      (config.slug ? `camera.blink_live_${config.slug}` : "");
    const sourceEntityId =
      config.source_entity_id ||
      config.camera_entity_id ||
      (config.slug ? `camera.${config.slug}` : "");
    const state = hass && entityId ? hass.states[entityId] : null;
    const token = state && state.attributes ? state.attributes.access_token : "";
    const params = new URLSearchParams();
    if (token) params.set("token", token);
    const query = params.toString();
    const response = await fetch(
      `/api/blink_liveview_proxy/cameras/${encodeURIComponent(
        config.slug
      )}/snapshot-refresh${query ? `?${query}` : ""}`,
      {
        method: "POST",
        cache: "no-store",
        credentials: "same-origin"
      }
    );
    if (!response.ok) return;
    let payload = {};
    try {
      payload = await response.json();
    } catch (err) {}
    refreshCameraImages(
      payload.entity_id || sourceEntityId,
      payload.snapshot_url || ""
    );
  }

  function refreshCameraImages(entityId, snapshotUrl) {
    if (!entityId || !snapshotUrl) return;
    const needle = `/api/camera_proxy/${encodeURIComponent(entityId)}`;
    const absolute = new URL(snapshotUrl, window.location.origin).href;
    for (const image of document.querySelectorAll("img")) {
      if (image.src && image.src.includes(needle)) {
        image.src = absolute;
      }
    }
    for (const element of document.querySelectorAll("*")) {
      const background = element.style && element.style.backgroundImage;
      if (background && background.includes(needle)) {
        element.style.backgroundImage = `url("${absolute}")`;
      }
    }
  }

  window.addEventListener(
    "keydown",
    (event) => {
      if (event.key === "Escape") closeDialog();
    },
    true
  );

  window.addEventListener(
    "ll-custom",
    (event) => {
      const detail = event.detail || {};
      const config = detail.blink_liveview_proxy;
      const clipsConfig = detail.blink_liveview_proxy_clips;
      const snapshotConfig = detail.blink_snapshot_refresh;
      if (!config && !clipsConfig && !snapshotConfig) return;
      event.preventDefault();
      event.stopPropagation();
      if (config) {
        openLiveDialog(config, hassFromEvent(event));
      } else if (clipsConfig) {
        openClipsDialog(clipsConfig, hassFromEvent(event));
      } else {
        refreshSnapshot(snapshotConfig, hassFromEvent(event)).catch(() => {});
      }
    },
    true
  );
})();
