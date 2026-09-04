// The "blink" icon set, registered the way Home Assistant's own ha-icon looks
// icons up: any prefix that is not MDI is resolved against window.customIcons,
// so once this runs "blink:logo" works anywhere an icon name does - the
// sidebar entry, a button-card, a tile card, an entity's icon override.
//
// Home Assistant loads this on every page through frontend.add_extra_js_url
// (see __init__.py), which is also how HACS gets its own sidebar icon. It is
// not a Lovelace resource, so it does not depend on the dashboard helper
// being registered, and it is safe to run twice.
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

  window.customIcons = window.customIcons || {};
  window.customIcons.blink = set;
})();
