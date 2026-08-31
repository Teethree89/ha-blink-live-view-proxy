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
      /* This is a real <dialog> opened with showModal(), NOT a fixed-position
         div. As a plain div the overlay was inserted but never painted in the
         Home Assistant iOS app's WKWebView: it opened invisibly "behind" the
         app and only appeared once something forced a repaint, like switching
         apps. Layer promotion alone fixed it in mobile Safari but not in the
         app. showModal() puts the element in the browser's top layer, which
         sidesteps that compositing path completely. */
      #${DIALOG_ID} {
        position: fixed;
        inset: 0;
        width: 100vw;
        height: 100vh;
        max-width: 100vw;
        max-height: 100vh;
        margin: 0;
        border: 0;
        padding: 0;
        z-index: 2147483000;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 10px;
        background: rgba(0, 0, 0, 0.72);
        color: var(--primary-text-color, #f8fafc);
        transform: translateZ(0);
        -webkit-transform: translateZ(0);
        will-change: transform, opacity;
        -webkit-backface-visibility: hidden;
        backface-visibility: hidden;
      }
      #${DIALOG_ID}::backdrop {
        background: rgba(0, 0, 0, 0.72);
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
      .blink-camera-reload-target {
        position: relative !important;
        overflow: hidden !important;
      }
      .blink-camera-reload-target.blink-camera-reloading img {
        filter: brightness(0.58) saturate(0.85);
      }
      .blink-camera-reload-overlay {
        position: absolute;
        inset: 0;
        z-index: 2147482000;
        display: grid;
        place-items: center;
        pointer-events: none;
        background:
          linear-gradient(135deg, rgba(15, 23, 42, 0.72), rgba(2, 6, 23, 0.42));
        color: #f8fafc;
        font-size: clamp(12px, 1.7vw, 18px);
        font-weight: 850;
        letter-spacing: 0;
        text-shadow: 0 2px 10px rgba(0, 0, 0, 0.55);
      }
      .blink-camera-reload-overlay::before {
        content: "";
        width: 28px;
        height: 28px;
        border-radius: 999px;
        border: 3px solid rgba(255, 255, 255, 0.28);
        border-top-color: #38bdf8;
        animation: blinkCameraSpin 0.9s linear infinite;
      }
      @keyframes blinkCameraSpin {
        to { transform: rotate(360deg); }
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
    if (typeof existing.close === "function" && existing.open) existing.close();
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

    const root = document.createElement("dialog");
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

    // showModal() promotes the element into the top layer, which is what
    // actually gets it painted in the iOS app's WKWebView. Fall back to leaving
    // it as a plain in-flow element if the browser has no <dialog> support.
    if (typeof root.showModal === "function") {
      try {
        root.showModal();
      } catch (err) {
        /* already open, or not yet connected; the CSS still displays it */
      }
    }

    // Belt and braces for the same iOS paint bug: flush layout, then nudge the
    // overlay across two animation frames so the compositor is forced to draw
    // the new layer instead of waiting for an unrelated repaint.
    void root.offsetHeight;
    root.style.opacity = "0.999";
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        root.style.opacity = "";
      });
    });
  }

  function openLiveDialog(config, hass) {
    const slug = config.slug;
    // Same fallback the clips and snapshot handlers use, so a card only has to
    // supply the slug. Generated dashboards rely on this.
    const entityId =
      config.entity_id || (slug ? `camera.blink_live_${slug}` : "");
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
    ensureStyle();
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
    try {
      setCameraReloading(sourceEntityId, true);
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
      await refreshCameraImages(
        payload.entity_id || sourceEntityId,
        payload.snapshot_url || ""
      );
    } finally {
      setCameraReloading(sourceEntityId, false);
    }
  }

  function querySelectorAllDeep(selector, root = document) {
    const results = [];
    const visit = (node) => {
      if (!node || !node.querySelectorAll) return;
      results.push(...node.querySelectorAll(selector));
      for (const child of node.querySelectorAll("*")) {
        if (child.shadowRoot) visit(child.shadowRoot);
      }
    };
    visit(root);
    return results;
  }

  function cameraProxyNeedle(entityId) {
    return `/api/camera_proxy/${encodeURIComponent(entityId)}`;
  }

  function cameraTargets(entityId) {
    if (!entityId) return [];
    const needle = cameraProxyNeedle(entityId);
    const targets = new Set();

    for (const image of querySelectorAllDeep("img")) {
      if (image.src && image.src.includes(needle)) {
        targets.add(image.closest("ha-card") || image.parentElement || image);
      }
    }

    for (const element of querySelectorAllDeep("*")) {
      const background = element.style && element.style.backgroundImage;
      if (background && background.includes(needle)) {
        targets.add(element);
      }
    }

    return [...targets].filter(Boolean);
  }

  function setCameraReloading(entityId, loading) {
    for (const target of cameraTargets(entityId)) {
      target.classList.toggle("blink-camera-reload-target", loading);
      target.classList.toggle("blink-camera-reloading", loading);

      let overlay = target.querySelector(":scope > .blink-camera-reload-overlay");
      if (loading) {
        if (!overlay) {
          overlay = document.createElement("div");
          overlay.className = "blink-camera-reload-overlay";
          overlay.textContent = "Reloading camera";
          target.append(overlay);
        }
      } else if (overlay) {
        overlay.remove();
      }
    }
  }

  function preloadImage(src) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(src);
      image.onerror = reject;
      image.src = src;
    });
  }

  async function refreshCameraImages(entityId, snapshotUrl) {
    if (!entityId || !snapshotUrl) return;
    const needle = cameraProxyNeedle(entityId);
    const absolute = new URL(snapshotUrl, window.location.origin).href;
    await preloadImage(absolute);

    for (const image of querySelectorAllDeep("img")) {
      if (image.src && image.src.includes(needle)) {
        image.src = absolute;
      }
    }

    for (const element of querySelectorAllDeep("*")) {
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
