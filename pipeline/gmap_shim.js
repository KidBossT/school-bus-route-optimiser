/*
 * The small part of Leaflet's API the two pages use, drawn on a Google map.
 *
 * Why: the pins and road lines on these pages come from Google (Places,
 * Geocoding, Routes), and Google's terms do not allow showing that content on
 * a non-Google map. The pages keep their own code; this file supplies a
 * `window.L` whose maps are Google maps (Maps JavaScript API).
 *
 * Map loads are billed per `new google.maps.Map`, so a map requested again for
 * the same container id is re-used (its element is moved into the new
 * container) instead of being created again. The dashboard rebuilds its DOM on
 * every slider move; without the pool each move would cost a map load.
 *
 * With no Google (no key, offline, key rejected) every call is a harmless
 * no-op and each map container says why, so the rest of the page still works.
 */
(function () {
  "use strict";
  const HAS_G = !!(window.google && google.maps && google.maps.Map);
  const THEME = window.GMAP_THEME || "dark";
  const DARK = [
    { elementType: "geometry", stylers: [{ color: "#121824" }] },
    { elementType: "labels.text.fill", stylers: [{ color: "#8b95a7" }] },
    { elementType: "labels.text.stroke", stylers: [{ color: "#0b0f17" }] },
    { featureType: "administrative", elementType: "geometry", stylers: [{ color: "#2a3345" }] },
    { featureType: "administrative.locality", elementType: "labels.text.fill", stylers: [{ color: "#aeb8c8" }] },
    { featureType: "poi", elementType: "labels.icon", stylers: [{ saturation: -80 }, { lightness: -35 }] },
    { featureType: "poi", elementType: "labels.text.fill", stylers: [{ color: "#6b7689" }] },
    { featureType: "poi", elementType: "geometry", stylers: [{ color: "#151c29" }] },
    { featureType: "poi.park", elementType: "geometry", stylers: [{ color: "#10201a" }] },
    { featureType: "road", elementType: "geometry", stylers: [{ color: "#232c3c" }] },
    { featureType: "road", elementType: "geometry.stroke", stylers: [{ color: "#171e2a" }] },
    { featureType: "road", elementType: "labels.text.fill", stylers: [{ color: "#8591a4" }] },
    { featureType: "road.highway", elementType: "geometry", stylers: [{ color: "#34405a" }] },
    { featureType: "road.highway", elementType: "geometry.stroke", stylers: [{ color: "#1d2433" }] },
    { featureType: "transit", elementType: "geometry", stylers: [{ color: "#1a2130" }] },
    { featureType: "transit.station", elementType: "labels.text.fill", stylers: [{ color: "#8b95a7" }] },
    { featureType: "water", elementType: "geometry", stylers: [{ color: "#0a1b2e" }] },
    { featureType: "water", elementType: "labels.text.fill", stylers: [{ color: "#3d5a80" }] },
  ];

  const ll = a => Array.isArray(a) ? { lat: +a[0], lng: +a[1] }
    : { lat: +a.lat, lng: +(a.lng !== undefined ? a.lng : a.lon) };

  /* ---- one floating tooltip for hover labels ---- */
  let tipEl = null;
  function tip(html, ev) {
    if (!tipEl) {
      tipEl = document.createElement("div");
      tipEl.className = "gm-tip";
      tipEl.style.cssText = "position:fixed;z-index:99999;pointer-events:none;padding:5px 8px;border-radius:7px;" +
        "font:600 11.5px system-ui,sans-serif;background:rgba(16,21,31,.95);color:#eef2f7;" +
        "border:1px solid rgba(255,255,255,.12);box-shadow:0 6px 20px rgba(0,0,0,.4);display:none;max-width:280px";
      document.body.appendChild(tipEl);
    }
    if (!html) { tipEl.style.display = "none"; return; }
    const e = ev && (ev.domEvent || ev);
    tipEl.innerHTML = html; tipEl.style.display = "block";
    if (e && e.clientX !== undefined) { tipEl.style.left = (e.clientX + 12) + "px"; tipEl.style.top = (e.clientY + 12) + "px"; }
  }

  /* ---- base layer ---- */
  class Layer {
    constructor() { this._h = {}; this._m = null; this._group = null; }
    addTo(t) { if (t instanceof Group) t._add(this); else this._attach(t); return this; }
    remove() { this._detach(); if (this._group) this._group._items.delete(this); return this; }
    on(ev, fn) { (this._h[ev] = this._h[ev] || []).push(fn); return this; }
    _fire(ev, e) { (this._h[ev] || []).forEach(f => f(Object.assign({ target: this }, e || {}))); }
    bindPopup(html, opts) { this._popup = html; this._popupOpts = opts || {}; return this; }
    bindTooltip(text, opts) { this._tip = text; this._tipOpts = opts || {}; if (this._m) this._applyTip(); return this; }
    openPopup() {
      if (!this._m || !this._popup || !this._m.g) return this;
      const iw = this._m._iw;
      iw.setOptions({ content: this._popup, maxWidth: this._popupOpts.maxWidth || 300,
        pixelOffset: new google.maps.Size(0, -(this._popupLift || 0)) });
      iw.setPosition(this._anchorLatLng());
      iw.open({ map: this._m.g });
      return this;
    }
    _applyTip() {}
  }

  class Group {
    constructor() { this._items = new Set(); this._m = null; }
    addTo(m) { this._m = m; this._items.forEach(i => i._attach(m)); return this; }
    _add(i) { this._items.add(i); i._group = this; if (this._m) i._attach(this._m); }
    clearLayers() { this._items.forEach(i => i._detach()); this._items.clear(); return this; }
    remove() { return this.clearLayers(); }
    eachLayer(fn) { this._items.forEach(fn); return this; }
  }

  /* ---- polylines ---- */
  class Poly extends Layer {
    constructor(coords, o) { super(); this._path = (coords || []).map(ll); this._o = Object.assign({}, o); }
    _opts() {
      const o = this._o, w = o.weight == null ? 3 : o.weight, op = o.opacity == null ? 1 : o.opacity;
      const base = { path: this._path, strokeColor: o.color || "#3b82f6", strokeWeight: w, strokeOpacity: op,
        clickable: !!(this._tip || this._popup), zIndex: o.className === "line-opt" ? 3 : 2 };
      if (o.dashArray) {
        const [d, g] = String(o.dashArray).split(/[ ,]+/).map(Number);
        return Object.assign(base, { strokeOpacity: 0, icons: [{ icon: { path: "M 0,-1 0,1", strokeColor: o.color,
          strokeOpacity: op, strokeWeight: w, scale: Math.max(1, d / 2) }, offset: "0", repeat: (d + (g || d)) + "px" }] });
      }
      if (o.className === "line-opt") {
        // moving dots along the new route, like the page's CSS animation
        return Object.assign(base, { strokeOpacity: op * .35, icons: [{ icon: { path: google.maps.SymbolPath.CIRCLE,
          scale: Math.max(1.6, w * .7), fillColor: "#dbeafe", fillOpacity: op, strokeWeight: 0 }, offset: "0", repeat: "16px" }] });
      }
      return base;
    }
    _attach(m) {
      this._detach(); this._m = m; if (!m.g) return; m._layers.add(this);
      this.g = new google.maps.Polyline(Object.assign(this._opts(), { map: m.g }));
      this._applyTip();
      if (this._popup) this.g.addListener("click", e => { this._at = e.latLng; this.openPopup(); });
      if (this._o.className === "line-opt") {
        let off = 0;
        this._anim = setInterval(() => { off = (off + 1) % 16; const ic = this.g.get("icons");
          if (ic && ic[0]) { ic[0].offset = off + "px"; this.g.set("icons", ic); } }, 70);
      }
    }
    _applyTip() {
      if (!this.g || !this._tip) return;
      this.g.setOptions({ clickable: true });
      this.g.addListener("mouseover", e => tip(this._tip, e));
      this.g.addListener("mousemove", e => tip(this._tip, e));
      this.g.addListener("mouseout", () => tip(null));
    }
    _detach() { if (this._anim) clearInterval(this._anim); this._anim = null; if (this.g) this.g.setMap(null); this.g = null;
      if (this._m && this._m._layers) this._m._layers.delete(this); }
    _anchorLatLng() { return this._at || this._path[Math.floor(this._path.length / 2)]; }
    setStyle(o) { Object.assign(this._o, o); if (this.g) this.g.setOptions(this._opts()); return this; }
  }

  /* ---- HTML markers (divIcon, circleMarker, default pin) ---- */
  let Overlay = null;
  if (HAS_G) {
    Overlay = class extends google.maps.OverlayView {
      constructor(mk) { super(); this.mk = mk; }
      onAdd() { this.getPanes().overlayMouseTarget.appendChild(this.mk._el); }
      draw() {
        const p = this.getProjection().fromLatLngToDivPixel(new google.maps.LatLng(this.mk._ll));
        if (!p) return;
        const [ax, ay] = this.mk._anchor;
        this.mk._el.style.left = (p.x - ax) + "px"; this.mk._el.style.top = (p.y - ay) + "px";
      }
      onRemove() { this.mk._el.remove(); }
    };
  }

  class Marker extends Layer {
    constructor(latlng, o, icon) {
      super();
      o = o || {};
      this._ll = ll(latlng);
      const ic = icon || o.icon || { className: "gm-pin", html: "<div></div>", iconSize: [18, 18], iconAnchor: [9, 9] };
      const [w, h] = ic.iconSize || [20, 20];
      this._anchor = ic.iconAnchor || [w / 2, h / 2];
      this._popupLift = this._anchor[1];
      const el = document.createElement("div");
      el.className = ic.className || "";
      el.innerHTML = ic.html || "";
      el.style.cssText += `;position:absolute;width:${w}px;height:${h}px;z-index:${1000 + (o.zIndexOffset || 0)};` +
        `opacity:${o.opacity == null ? 1 : o.opacity};cursor:${o.draggable ? "grab" : "pointer"}`;
      this._el = el; this._icon = el;
      el.addEventListener("mouseenter", e => { this._fire("mouseover", { originalEvent: e }); if (this._tip && !this._tipOpts.permanent) tip(this._tip, e); });
      el.addEventListener("mousemove", e => { if (this._tip && !this._tipOpts.permanent) tip(this._tip, e); });
      el.addEventListener("mouseleave", e => { this._fire("mouseout", { originalEvent: e }); tip(null); });
      el.addEventListener("click", e => { e.stopPropagation(); if (this._moved) return; this._fire("click"); this.openPopup(); });
      if (HAS_G) {
        // A draggable pin keeps its drags to itself; any other pin only keeps its
        // clicks, so a drag that starts on it still pans the map. The map's own
        // gesture setting is never touched, so nothing can leave it frozen.
        if (o.draggable) { google.maps.OverlayView.preventMapHitsAndGesturesFrom(el); this._dragify(); }
        else google.maps.OverlayView.preventMapHitsFrom(el);
      }
    }
    _dragify() {
      const el = this._el;
      el.style.touchAction = "none";
      el.addEventListener("pointerdown", e => {
        if (!this._m || !this._ov || e.button > 0) return;
        const proj = this._ov.getProjection(); if (!proj) return;
        e.preventDefault();
        const start = proj.fromLatLngToDivPixel(new google.maps.LatLng(this._ll));
        const sx = e.clientX, sy = e.clientY, id = e.pointerId, orig = this._ll;
        this._moved = false;
        try { el.setPointerCapture(id); } catch (_) { /* element already gone */ }
        el.style.cursor = "grabbing";
        const move = ev => {
          if (ev.pointerId !== id) return;
          const dx = ev.clientX - sx, dy = ev.clientY - sy;
          if (Math.abs(dx) + Math.abs(dy) > 3) this._moved = true;
          const p = proj.fromDivPixelToLatLng(new google.maps.Point(start.x + dx, start.y + dy));
          this._ll = { lat: p.lat(), lng: p.lng() }; this._ov.draw();
        };
        const end = ev => {                // up, cancel or lost capture: always finish cleanly
          if (ev && ev.pointerId !== undefined && ev.pointerId !== id) return;
          el.removeEventListener("pointermove", move);
          for (const t of ["pointerup", "pointercancel", "lostpointercapture"]) el.removeEventListener(t, end);
          el.style.cursor = "grab";
          if (this._moved && ev && ev.type === "pointerup") this._fire("dragend");
          else if (this._moved) { this._ll = orig; if (this._ov) this._ov.draw(); }   // cancelled: put it back
          setTimeout(() => { this._moved = false; }, 0);
        };
        el.addEventListener("pointermove", move);
        for (const t of ["pointerup", "pointercancel", "lostpointercapture"]) el.addEventListener(t, end);
      });
    }
    _attach(m) {
      this._detach(); this._m = m; if (!m.g) return; m._layers.add(this);
      this._ov = new Overlay(this); this._ov.setMap(m.g); this._applyTip();
    }
    _applyTip() {
      if (!this._tip || !this._tipOpts.permanent) return;
      let s = this._el.querySelector(".gm-perm");
      if (!s) { s = document.createElement("span"); this._el.appendChild(s); }
      s.className = "gm-perm " + (this._tipOpts.className || "");
      s.style.cssText = "position:absolute;inset:0;display:grid;place-items:center;pointer-events:none;font:700 9px system-ui,sans-serif;color:#fff";
      s.textContent = this._tip;
    }
    _detach() { if (this._ov) this._ov.setMap(null); this._ov = null;
      if (this._m && this._m._layers) this._m._layers.delete(this); }
    _anchorLatLng() { return this._ll; }
    getLatLng() { return { lat: this._ll.lat, lng: this._ll.lng }; }
    setLatLng(p) { this._ll = ll(p); if (this._ov) this._ov.draw(); return this; }
  }

  function circleMarker(latlng, o) {
    o = o || {};
    const r = o.radius || 6, w = o.weight == null ? 1 : o.weight, d = 2 * r + 2 * w;
    const fill = o.fillColor || o.color || "#3b82f6", fo = o.fillOpacity == null ? .2 : o.fillOpacity;
    // outer ring = stroke; inner disc = fill at its own opacity
    const html = `<div style="width:${2 * r}px;height:${2 * r}px;border-radius:50%;box-sizing:content-box;` +
      `border:${w}px ${o.dashArray ? "dashed" : "solid"} ${o.color || fill};position:relative;` +
      `opacity:${o.opacity == null ? 1 : o.opacity}">` +
      `<div style="position:absolute;inset:0;border-radius:50%;background:${fill};opacity:${fo}"></div></div>`;
    return new Marker(latlng, o, { className: "gm-cm", html, iconSize: [d, d], iconAnchor: [d / 2, d / 2] });
  }

  class Circle extends Layer {
    constructor(c, o) { super(); this._c = ll(c); this._o = o || {}; }
    _attach(m) {
      this._detach(); this._m = m; if (!m.g) return; m._layers.add(this);
      const o = this._o;
      this.g = new google.maps.Circle({ map: m.g, center: this._c, radius: o.radius || 100, strokeColor: o.color || "#3b82f6",
        strokeWeight: o.weight == null ? 2 : o.weight, strokeOpacity: o.opacity == null ? 1 : o.opacity,
        fillColor: o.fillColor || o.color || "#3b82f6", fillOpacity: o.fillOpacity == null ? .2 : o.fillOpacity, clickable: false });
    }
    _detach() { if (this.g) this.g.setMap(null); this.g = null;
      if (this._m && this._m._layers) this._m._layers.delete(this); }
    _anchorLatLng() { return this._c; }
  }

  /* ---- maps ---- */
  const pool = {};
  let loads = 0;                       // new google.maps.Map calls = billable map loads
  const POS = HAS_G ? { topright: google.maps.ControlPosition.RIGHT_TOP, topleft: google.maps.ControlPosition.LEFT_TOP,
    bottomright: google.maps.ControlPosition.RIGHT_BOTTOM, bottomleft: google.maps.ControlPosition.LEFT_BOTTOM } : {};

  function notice(node, text) {
    const n = document.createElement("div");
    n.className = "gm-notice";
    n.style.cssText = "position:absolute;inset:0;display:grid;place-items:center;padding:24px;text-align:center;" +
      "font:500 13px/1.5 system-ui,sans-serif;color:#8b95a7;background:" + (THEME === "dark" ? "#0b0f17" : "#f3f4f6");
    n.innerHTML = text;
    node.style.position = node.style.position || "relative";
    node.appendChild(n);
  }
  const NO_MAP = window.GMAP_NO_KEY_MSG ||
    "Map off: no Google Maps browser key in <code>.env</code> (GOOGLE_MAPS_BROWSER_KEY). See README, “Maps on the pages”.";

  class MapW {
    constructor(el, o) {
      o = o || {};
      const node = typeof el === "string" ? document.getElementById(el) : el;
      this._node = node; this._h = {}; this._layers = new Set();
      this._gesture = o.scrollWheelZoom === false ? "cooperative" : "greedy";
      if (!HAS_G) { notice(node, NO_MAP); this.g = null; return; }
      this._inner = document.createElement("div");
      this._inner.style.cssText = "position:absolute;inset:0";
      if (getComputedStyle(node).position === "static") node.style.position = "relative";
      node.appendChild(this._inner);
      loads++;
      this.g = new google.maps.Map(this._inner, {
        center: { lat: 28.57, lng: 77.39 }, zoom: 11,
        styles: THEME === "dark" ? DARK : null, backgroundColor: THEME === "dark" ? "#0b0f17" : "#e5e7eb",
        disableDefaultUI: true, zoomControl: o.zoomControl !== false, clickableIcons: false,
        zoomControlOptions: { position: google.maps.ControlPosition.RIGHT_TOP },
        gestureHandling: this._gesture, keyboardShortcuts: true,
      });
      this._iw = new google.maps.InfoWindow();
      // A fit requested before Google has laid the map out (just created, or in
      // a hidden container) is held and applied once the map has a real size.
      this._ready = false; this._fit = null; this._w = 0;
      google.maps.event.addListenerOnce(this.g, "idle", () => { this._ready = true; this._applyFit(); });
      new ResizeObserver(() => {
        const w = this._inner.clientWidth, was = this._w; this._w = w;
        if (w > 0 && was === 0) this._applyFit();
      }).observe(this._inner);
      // Leaflet's "move" means the view moved. Google also fires bounds_changed
      // when a map is resized or shown, which would make a synced partner map
      // copy a stale view - so only real centre/zoom changes are passed on.
      // Start from the initial view, so a map appearing for the first time (e.g.
      // the hidden second map in Compare) does not announce its default view
      // and pull its synced partner back to it.
      this._last = `${(28.57).toFixed(6)},${(77.39).toFixed(6)},11`;
      this.g.addListener("bounds_changed", () => {
        const c = this.g.getCenter(), z = this.g.getZoom();
        if (!c) return;
        const k = `${c.lat().toFixed(6)},${c.lng().toFixed(6)},${z}`;
        if (k === this._last) return;
        this._last = k;
        if (this._following) return;       // moved to follow another map: do not echo back
        (this._h.move || []).forEach(f => f());
      });
      this.g.addListener("click", () => this._iw.close());
    }
    _rehome(node) {
      if (!this.g || !node || node === this._node) return;
      this._node = node;
      if (getComputedStyle(node).position === "static") node.style.position = "relative";
      node.appendChild(this._inner);
    }
    getContainer() { return this._node; }
    remove() {                         // the page is done with this map: clear it, keep it in the pool
      if (this._iw) this._iw.close();
      [...this._layers].forEach(l => l._detach());
      return this;
    }
    removeLayer(l) { if (l) l.remove(); return this; }
    invalidateSize() { return this; }                                    // Google resizes itself
    on(ev, fn) { (this._h[ev] = this._h[ev] || []).push(fn); return this; }
    getZoom() { return this.g ? this.g.getZoom() : 11; }
    getCenter() { if (!this.g) return { lat: 28.57, lng: 77.39 }; const c = this.g.getCenter(); return { lat: c.lat(), lng: c.lng() }; }
    setView(c, z, opts) {
      if (!this.g) return this;
      // {animate:false} is how the pages keep a partner map in step. That map
      // stays silent while it is being led (until 400 ms after the last update),
      // otherwise every step of a drag bounces back and fights the user's hand.
      if (opts && opts.animate === false) {
        this._following = true;
        clearTimeout(this._followT);
        this._followT = setTimeout(() => { this._following = false; }, 400);
      }
      this.g.moveCamera({ center: ll(c), zoom: z });
      return this;
    }
    flyTo(c, z) {                     // a deliberate jump also cancels a pending automatic fit
      this._fit = null;
      if (this.g) { this.g.panTo(ll(c)); if (z) this.g.setZoom(z); } return this;
    }
    setMapType(t) { if (this.g) this.g.setMapTypeId(t); return this; }
    fitBounds(pts, o) {
      if (!this.g || !pts || !pts.length) return this;
      o = o || {};
      // Fit now if the map has a size, and (if Google is not ready yet) once more
      // when it is - a fit asked of a map Google has not laid out is ignored.
      if (this._inner.clientWidth > 0) this._doFit(pts, o);
      this._fit = this._ready && this._inner.clientWidth > 0 ? null : [pts, o];
      return this;
    }
    _applyFit() {
      if (!this._fit || !this._inner.clientWidth) return;
      const [pts, o] = this._fit; this._fit = null;
      this._doFit(pts, o);
    }
    _doFit(pts, o) {
      const P = v => Array.isArray(v) ? v : [v || 0, v || 0];
      const tl = P(o.paddingTopLeft || o.padding), br = P(o.paddingBottomRight || o.padding);
      const pad = { left: tl[0], top: tl[1], right: br[0], bottom: br[1] };
      const b = new google.maps.LatLngBounds(); pts.forEach(p => b.extend(ll(p)));
      const ne = b.getNorthEast(), sw = b.getSouthWest();
      if (Math.abs(ne.lat() - sw.lat()) < 1e-6 && Math.abs(ne.lng() - sw.lng()) < 1e-6) {
        this.g.moveCamera({ center: b.getCenter(), zoom: o.maxZoom || 14 }); return;
      }
      this.g.fitBounds(b, pad);
      if (o.maxZoom) google.maps.event.addListenerOnce(this.g, "idle", () => {
        if (this.g.getZoom() > o.maxZoom) this.g.setZoom(o.maxZoom); });
    }
  }

  function map(el, o) {
    const node = typeof el === "string" ? document.getElementById(el) : el;
    const key = typeof el === "string" ? el : (el.id || null);
    if (key && pool[key]) { if (node) pool[key]._rehome(node); return pool[key]; }
    const m = new MapW(node, o);
    if (key) pool[key] = m;
    return m;
  }

  const noop = { addTo() { return this; } };
  window.L = {
    map,
    tileLayer: () => Object.create(noop),
    control: { zoom: o => ({ addTo(m) { if (m.g) m.g.setOptions({ zoomControl: true,
      zoomControlOptions: { position: POS[(o && o.position) || "topright"] || POS.topright } }); return this; } }) },
    layerGroup: () => new Group(),
    polyline: (c, o) => new Poly(c, o),
    marker: (p, o) => new Marker(p, o),
    circleMarker, circle: (c, o) => new Circle(c, o),
    divIcon: o => o,
    hasGoogle: HAS_G,
    mapLoads: () => loads,
  };

  // Google calls this when it rejects the key (wrong address, API not enabled).
  window.gm_authFailure = function () {
    Object.values(pool).forEach(m => m._node && notice(m._node,
      "Google rejected the map key for this address. Open the pages with <code>python pipeline/serve.py</code> " +
      "(http://localhost:8777) and check the browser key allows that address and the Maps JavaScript API."));
  };
})();
