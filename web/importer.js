"use strict";
// Pages sent from the browser: the bookmarklet "→ CV Aggregator" that the
// user drags to their bookmarks bar, and the page it opens (#/import),
// which hands what it got to the server (importer.py) and shows the
// offers read from it as they're rated. Uses app.js' helpers and
// offers.js' OffersView.

// The bookmarklet's code, run on the portal's page: it takes what the
// browser shows (the text, JSON-LD JobPostings, and the tile around each
// link to an offer, by the portals' "offer" patterns), packs it (gzip,
// base64url) and opens the app with it in the address' fragment, which
// never reaches a server but the app's own page. It mustn't use anything
// outside itself: it's turned into a javascript: address.
function sendPage(app, patterns) {
  var tests = patterns.map(function (p) { return new RegExp(p); });
  var isOffer = function (href) { return tests.some(function (t) { return t.test(href); }); };
  var plain = function (href) { return href.replace(/[?#].*$/, ""); };
  var text = function (el, n) {
    return (el && el.innerText || "").replace(/[ \t]+/g, " ").replace(/\n\s*\n+/g, "\n").trim().slice(0, n);
  };
  var ld = [];
  document.querySelectorAll('script[type="application/ld+json"]').forEach(function (s) {
    try {
      [].concat(JSON.parse(s.textContent)).forEach(function (x) {
        if (x && x["@type"] === "JobPosting") ld.push(x);
      });
    } catch (e) { /* not JSON */ }
  });
  var seen = {}, cards = [];
  [].slice.call(document.querySelectorAll("a[href]")).forEach(function (a) {
    var key = plain(a.href);
    if (!isOffer(a.href) || seen[key]) return;
    seen[key] = 1;
    // the tile: the largest element around the link with no other offer
    var el = a;
    while (el.parentElement && el.parentElement !== document.body &&
           ![].slice.call(el.parentElement.querySelectorAll("a[href]")).some(function (b) {
             return isOffer(b.href) && plain(b.href) !== key;
           })) {
      el = el.parentElement;
    }
    cards.push({ url: a.href, text: text(el, 600) });
  });
  var data = JSON.stringify({
    url: location.href, title: document.title, ld: ld.slice(0, 1), cards: cards.slice(0, 120),
    text: text(document.querySelector("main") || document.body, 20000),
  });
  var tab = window.open("", "_blank");
  if (!tab) return alert("Przeglądarka zablokowała nowe okno: zezwól tej stronie na wyskakujące okna.");
  var go = function (hash) { tab.location.href = app + "#/import?" + hash; };
  if (!window.CompressionStream) return go("j=" + encodeURIComponent(data));
  new Response(new Blob([data]).stream().pipeThrough(new CompressionStream("gzip"))).arrayBuffer()
    .then(function (buf) {
      var bytes = new Uint8Array(buf), bin = "";
      for (var i = 0; i < bytes.length; i += 8192) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
      go("z=" + btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""));
    });
}

function bookmarkletHref(importers) {
  const app = location.origin + location.pathname;
  const patterns = importers.map((i) => i.offer).filter(Boolean);
  return "javascript:" + encodeURIComponent("(" + sendPage.toString() + ")(" + JSON.stringify(app) + "," +
    JSON.stringify(patterns) + ")");
}

// What the bookmarklet packed, from the fragment ("z=": gzip, base64url;
// "j=": JSON).
async function unpackPage(query) {
  if (query.startsWith("j=")) return JSON.parse(decodeURIComponent(query.slice(2)));
  if (!query.startsWith("z=") || !window.DecompressionStream) throw new Error("Nie da się odczytać przesłanej strony.");
  const b64 = query.slice(2).replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64 + "===".slice((b64.length + 3) % 4));
  const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
  return JSON.parse(await new Response(stream).text());
}

// The panel on the offers page that gives the bookmarklet.
function bookmarkletPanel(data, open, ontoggle) {
  const names = data.importers.map((i) => i.name).join(" i ");
  const link = el("a", { class: "bookmarklet", href: bookmarkletHref(data.importers),
                         onclick: (e) => { e.preventDefault(); alert("Przeciągnij ten przycisk na pasek zakładek."); } },
    "→ CV Aggregator");
  const details = el("details", { class: "wishes", open, ontoggle: () => ontoggle(details.open) },
    el("summary", {}, el("h2", {}, "Oferty z " + (names || "innych portali"))),
    el("p", { class: "hint" }, names + " nie pozwalają programom czytać swoich ofert, więc wysyłasz je z przeglądarki, " +
      "w której i tak je oglądasz:"),
    el("ol", { class: "steps" },
      el("li", {}, "Przeciągnij ten przycisk na pasek zakładek: ", link),
      el("li", {}, "Na stronie wyników wyszukiwania kliknij zakładkę: oferty z widocznej strony zostaną przesiane według " +
        "„Czego szukam”, a najlepsze ocenione (na podstawie kafelków)."),
      el("li", {}, "Na stronie oferty kliknij ją znowu: model oceni ofertę z pełnym opisem."),
    ),
    el("p", { class: "hint" }, "Przycisk działa też na innych portalach, na pojedynczych ofertach. Wysyła tylko to, co " +
      "widzisz na stronie; oferty trafiają do twojej listy ofert."));
  return details;
}

// The page the bookmarklet opens: sends what it got, then shows the
// offers read from it.
class ImportView extends OffersView {
  constructor() {
    super();
    this.importId = null;
    this.query = location.hash.includes("?") ? location.hash.slice(location.hash.indexOf("?") + 1) : "";
    // what came in the address isn't to stay in the history
    history.replaceState(null, "", location.pathname + "#/import");
  }

  async start() {
    show(el("p", { class: "loading" }, "Wysyłam stronę…"));
    if (!this.query) {
      return show(el("section", { class: "panel notice" }, "Nic tu nie przesłano: kliknij zakładkę „→ CV Aggregator” " +
        "na stronie z ofertami.", el("a", { class: "btn", href: "#/oferty" }, "Oferty")));
    }
    try {
      const page = await unpackPage(this.query);
      this.importId = (await api("POST", "offers/import", page)).id;
    } catch (e) {
      if (e.status === 401) return loadMe();
      return show(el("section", { class: "panel notice" }, e.message, el("a", { class: "btn", href: "#/oferty" }, "Oferty")));
    }
    this.load();
  }

  async load() {
    clearTimeout(this.timer);
    try {
      this.update(await api("GET", "offers/import/" + this.importId));
    } catch (e) {
      if (this.stopped) return;
      if (!this.data) return show(el("section", { class: "panel notice" }, e.message));
      this.timer = setTimeout(() => this.load(), 10000);
    }
  }

  update(data) {
    if (this.stopped) return;
    this.data = data;
    this.render();
    clearTimeout(this.timer);
    const busy = ["pending", "working"].includes(data.import.status) || data.status ||
      data.matches.some((m) => m.wanted);
    if (busy) this.timer = setTimeout(() => this.load(), 3000);
  }

  async act(path, body) {
    try {
      await api("POST", path, body);
      this.load();
    } catch (e) {
      alert(e.message);
    }
  }

  render() {
    const d = this.data;
    const item = d.import;
    const r = item.result;
    let host = item.url;
    try { host = new URL(item.url).hostname.replace(/^www\./, ""); } catch (e) { /* as it is */ }
    let state;
    if (item.status === "pending" || item.status === "working") {
      state = el("p", { class: "thinking dots" }, "Model czyta stronę");
    } else if (item.status === "failed") {
      state = el("p", { class: "bad" }, "⚠ " + item.error);
    } else {
      const rating = d.matches.filter((m) => m.wanted).length;
      state = el("p", {}, r.single && !r.tiles ? "Oferta odczytana" : "Znalazłam " + r.read + " ofert" +
        (r.out ? "; " + r.out + " odpadło (inna umowa albo unikane słowo)" : "") + ".",
        rating > 0 && el("span", { class: "thinking dots" }, " Model ocenia: zostało " + rating));
    }
    const shown = d.matches.slice().sort((a, b) =>
      (b.score === null ? -1 : b.score) - (a.score === null ? -1 : a.score) || b.prescore - a.prescore);
    show(el("div", { class: "stack" },
      el("div", {},
        el("div", { class: "section-head" }, el("h1", {}, "Ze strony " + host),
          el("a", { class: "btn", href: "#/oferty" }, "Wszystkie oferty")),
        el("p", { class: "muted small import-url" }, el("a", { href: item.url, target: "_blank", rel: "noopener" }, item.url)),
        state),
      shown.length > 0 && el("div", { class: "offer-list" }, shown.map((m) => this.card(m)))));
  }
}
