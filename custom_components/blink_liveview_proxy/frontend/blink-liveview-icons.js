// The "blink" icon set, registered the way Home Assistant's own ha-icon looks
// icons up: any prefix that is not MDI is resolved against window.customIcons,
// so once this runs "blink:logo" works anywhere an icon name does - the
// sidebar entry, a button-card, a tile card, an entity's icon override.
//
// It is loaded three ways on purpose, because one is not enough:
//
//   1. frontend.add_extra_js_url puts a <script> for it in index.html, which
//      is the right primary path and covers every fresh page load.
//   2. blink-liveview-dialog.js imports it, so any Lovelace dashboard pulls it
//      in through the registered resource.
//   3. the admin panel imports it too.
//
// (1) alone leaves a hole that bit a real upgrade: a browser tab or a
// companion-app webview that was already open when the integration was
// installed keeps its old index.html forever. Home Assistant pushes the new
// panel title over the websocket, so the sidebar entry renames itself and
// looks updated, but the script tag never arrives and the icon stays blank
// until someone hard-refreshes - which on iOS is not a thing a user can do.
//
// One icon, one colour: the mark from the wordmark, traced as a filled 24x24
// path in the MDI convention. Rings are drawn as disks with reversed inner
// subpaths so nonzero winding cuts the gaps, which is what lets the same path
// render with a plain fill and no stroke at any size the sidebar picks.
(function () {
  const ICONS = {
    logo: {
      path: "M18.5,11A8,8 0 1,1 2.5,11A8,8 0 1,1 18.5,11ZM16.85,11A6.35,6.35 0 1,0 4.15,11A6.35,6.35 0 1,0 16.85,11ZM15.65,11A5.15,5.15 0 1,1 5.35,11A5.15,5.15 0 1,1 15.65,11ZM14.15,11A3.65,3.65 0 1,0 6.85,11A3.65,3.65 0 1,0 14.15,11ZM8.95,8.75L12.95,11L8.95,13.25ZM5.9,19.4H15.1A1.5,1.5 0 0,1 16.6,20.9V20.9A1.5,1.5 0 0,1 15.1,22.4H5.9A1.5,1.5 0 0,1 4.4,20.9V20.9A1.5,1.5 0 0,1 5.9,19.4ZM16,3.13A3.27,3.27 0 0,1 19.27,6.4A0.72,0.72 0 0,1 17.82,6.4A1.82,1.82 0 0,0 16,4.58A0.72,0.72 0 0,1 16,3.13ZM16,1.03A5.38,5.38 0 0,1 21.38,6.4A0.72,0.72 0 0,1 19.93,6.4A3.93,3.93 0 0,0 16,2.48A0.72,0.72 0 0,1 16,1.03Z",
    },
  };

  const set = {
    getIcon: (name) => (ICONS[name]
      ? Promise.resolve(ICONS[name])
      : Promise.reject(new Error(`Unknown icon blink:${name}`))),
    // The icon picker in the entity settings dialog reads this list.
    getIconList: () => Promise.resolve(
      Object.keys(ICONS).map((name) => ({ name, keywords: ["blink", "camera", "live view", "proxy"] })),
    ),
  };

  const already = window.customIcons && window.customIcons.blink;
  window.customIcons = window.customIcons || {};
  window.customIcons.blink = set;
  if (already) return;

  // Repaint anything that already gave up on us.
  //
  // ha-icon resolves a custom prefix once, in willUpdate. If the set was not
  // registered at that moment it sets an internal legacy flag, renders
  // nothing, and never asks again - so on a page that rendered before this
  // file loaded, the sidebar entry is blank until a reload. Re-assigning the
  // same icon name is the supported way to make it resolve again: it is a
  // reactive property, so the element re-runs the lookup, which now succeeds.
  const repaint = (root, depth) => {
    if (!root || !root.querySelectorAll || depth > 12) return;
    for (const node of root.querySelectorAll("ha-icon")) {
      const icon = node.icon;
      if (typeof icon === "string" && icon.startsWith("blink:")) {
        node.icon = "";
        node.icon = icon;
      }
    }
    for (const node of root.querySelectorAll("*")) {
      if (node.shadowRoot) repaint(node.shadowRoot, depth + 1);
    }
  };
  const heal = () => {
    try { repaint(document, 0); } catch (err) { /* never break a page over an icon */ }
  };
  // Several passes, not one. The sidebar is built from a websocket round trip,
  // so on a cold start - a phone waking, a home-screen PWA launching - it can
  // render well after this file has run. These are a handful of cheap DOM
  // walks spread over the first few seconds.
  heal();
  for (const delay of [200, 600, 1500, 3500]) setTimeout(heal, delay);
  // A home-screen PWA resumes rather than reloads, so it never runs this file
  // again. Repaint when it comes back to the foreground.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) heal();
  });
})();
