(function () {
  if (window.__blinkLiveviewDialogLoaded) return;
  window.__blinkLiveviewDialogLoaded = true;

  // Pull in the icon set. index.html carries it too, but a tab that was open
  // when the integration was installed never re-parses index.html, so the
  // sidebar icon stays blank there. This resource loads on every Lovelace
  // dashboard, and the module repaints icons that already gave up.
  import("/api/blink_liveview_proxy/assets/blink-liveview-icons.js").catch(() => {});

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
        /* 100vh on a phone is the height with the browser toolbar hidden, so
           the bottom of the dialog - where the video's controls live - sat
           behind the toolbar in portrait and only appeared in landscape,
           where the toolbar collapses. dvh is the height actually visible;
           the vh line stays for engines without it. */
        height: 100vh;
        height: 100dvh;
        max-width: 100vw;
        max-height: 100vh;
        max-height: 100dvh;
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
        height: min(760px, calc(100dvh - 32px));
        display: grid;
        grid-template-rows: auto minmax(0, 1fr);
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
        min-height: 56px;
        padding: 0 8px;
        background: var(--app-header-background-color, #1f2937);
      }
      #${DIALOG_ID} button svg {
        width: 24px;
        height: 24px;
        fill: currentColor;
        vertical-align: middle;
      }
      #${DIALOG_ID} .blink-liveview-title,
      #${DIALOG_ID} button {
        -webkit-user-select: none;
        user-select: none;
        -webkit-touch-callout: none;
        -webkit-tap-highlight-color: transparent;
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
      /* A phone in either orientation, or anything short enough that the
         framed dialog would leave no room for the picture: fill the screen,
         and keep the header and controls out of the notch and home bar. */
      @media (max-width: 720px), (max-height: 520px) {
        #${DIALOG_ID} {
          align-items: stretch;
          justify-content: stretch;
        }
        #${DIALOG_ID} .blink-liveview-shell {
          width: 100%;
          height: 100%;
          border-radius: 0;
          box-sizing: border-box;
          padding-bottom: env(safe-area-inset-bottom, 0px);
        }
        #${DIALOG_ID} .blink-liveview-header {
          padding-top: env(safe-area-inset-top, 0px);
          padding-left: max(8px, env(safe-area-inset-left, 0px));
          padding-right: max(8px, env(safe-area-inset-right, 0px));
        }
      }
    `;
    document.head.appendChild(style);
  }

  // iOS lies about height in more than one way. 100vh is the height with the
  // browser toolbar hidden, and even 100dvh does not account for the chrome
  // the Home Assistant companion app puts around its webview - so the bottom
  // of the shell, where the video element's native controls live, sat below
  // the fold. window.innerHeight is what is actually visible, so use it and
  // keep it current; the CSS units stay as the fallback.
  function sizeDialog(root) {
    if (!root) return;
    // Clamp, never set.
    //
    // Setting an explicit height replaces the box that `inset: 0` already
    // describes, so a measurement that reads high makes the dialog taller
    // than the screen rather than shorter - which is what put a landscape
    // live view's bottom row off the display even after portrait was fixed.
    // Taking the smallest of everything on offer, and applying it only as an
    // upper bound, can shrink the dialog to fit but can never stretch it.
    const measures = [window.innerHeight];
    if (window.visualViewport && window.visualViewport.height) {
      measures.push(window.visualViewport.height);
    }
    const height = Math.floor(Math.min(...measures.filter(Boolean)));
    if (!height) return;
    root.style.height = "";
    root.style.maxHeight = `${height}px`;
  }

  function watchViewport(root) {
    const apply = () => sizeDialog(root);
    // orientationchange fires BEFORE iOS updates its viewport metrics, so
    // reading the height in that handler returns the height of the
    // orientation being left - which is how a portrait-sized dialog ended up
    // in a landscape window with its bottom off the screen. Measure again as
    // the rotation settles rather than trusting the first read.
    const applyThroughRotation = () => {
      apply();
      for (const delay of [60, 180, 400, 800]) setTimeout(apply, delay);
    };
    apply();
    window.addEventListener("resize", apply);
    window.addEventListener("orientationchange", applyThroughRotation);
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", apply);
      window.visualViewport.addEventListener("scroll", apply);
    }
    root.__blinkStopWatching = () => {
      window.removeEventListener("resize", apply);
      window.removeEventListener("orientationchange", applyThroughRotation);
      if (window.visualViewport) {
        window.visualViewport.removeEventListener("resize", apply);
        window.visualViewport.removeEventListener("scroll", apply);
      }
    };
  }

  function closeDialog() {
    const existing = document.getElementById(DIALOG_ID);
    if (!existing) return;
    if (typeof existing.__blinkStopWatching === "function") existing.__blinkStopWatching();
    if (typeof existing.close === "function" && existing.open) existing.close();

    // Tell the player to stop before the iframe goes.
    //
    // Dropping the src and removing the element is enough on a desktop, and
    // is not on iOS: there the player runs native HLS, and a detached
    // document's <video> was left fetching segments. The proxy therefore
    // never saw the stream go idle, held the Blink live view open, and every
    // later call that needs the Blink client - listing clips, above all -
    // queued behind a session nobody was watching. Killing the app was the
    // only thing that ended it, which is exactly the report.
    //
    // The player is same-origin, so ask it to shut down properly, then
    // navigate the frame to about:blank so the document really does unload.
    const iframe = existing.querySelector("iframe");
    if (iframe) {
      try {
        const frame = iframe.contentWindow;
        if (frame && typeof frame.__blinkStopPlayer === "function") frame.__blinkStopPlayer();
      } catch (err) {
        /* a cross-origin or already-torn-down frame has nothing to stop */
      }
      iframe.src = "about:blank";
    }
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
    // The MDI "close" glyph, inline so this file stays self-contained.
    close.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19,6.41L17.59,5L12,10.59L6.41,5L5,6.41L10.59,12L5,17.59L6.41,19L12,13.41L17.59,19L19,17.59L13.41,12L19,6.41Z"/></svg>';
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
    watchViewport(root);

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
      `Live view ${slug}`;
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
      title: config.title || "Local clips",
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
