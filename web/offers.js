"use strict";
// The offers page: what the user looks for ("Czego szukam"), and the job
// offers kept for them, rated by a model, in tabs by the user's mark. While
// the portals are searched or offers rated, the page asks for news every
// few seconds. Uses the helpers of app.js (el, api, fill, show...).

const OFFERS_POLL_MS = 4000;
const TABS = [
  ["best", "Polecane", (m) => m.status === "new" && m.score !== null],
  ["todo", "Do oceny", (m) => m.status === "new" && m.score === null],
  ["interested", "Interesujące", (m) => m.status === "interested"],
  ["applied", "Aplikowane", (m) => m.status === "applied"],
  ["rejected", "Odrzucone", (m) => m.status === "rejected"],
];
const MODE_NAMES = { remote: "zdalnie", hybrid: "hybrydowo", office: "stacjonarnie" };
const LEVEL_NAMES = { intern: "staż", junior: "junior", mid: "mid", senior: "senior", lead: "lead" };
const CONTRACT_NAMES = { uop: "UoP", b2b: "B2B", other: "inna" };
const PORTAL_NAMES = { justjoin: "JustJoin.it", rocketjobs: "RocketJobs", nofluffjobs: "No Fluff Jobs",
                       bulldogjob: "Bulldogjob" };

const money = (n) => Math.round(n).toLocaleString("pl-PL");

function scoreClass(score) {
  return score === null ? "none" : score >= 75 ? "high" : score >= 50 ? "mid" : "low";
}

class OffersView {
  constructor() {
    this.data = null;
    this.timer = null;
    this.stopped = false;
    this.tab = "best";
    this.editing = false; // the wishes' form has unsaved changes
    this.wishesOpen = null;
  }

  start() {
    show(el("p", { class: "loading" }, "Ładowanie…"));
    this.load();
  }

  stop() {
    this.stopped = true;
    clearTimeout(this.timer);
  }

  async load() {
    clearTimeout(this.timer);
    try {
      this.update(await api("GET", "offers"));
    } catch (e) {
      if (this.stopped) return;
      if (e.status === 401) return loadMe();
      if (!this.data) return show(el("section", { class: "panel notice" }, e.message));
      this.timer = setTimeout(() => this.load(), OFFERS_POLL_MS * 5);
    }
  }

  update(data) {
    if (this.stopped) return;
    const was = this.data;
    this.data = data;
    this.render(!was);
    clearTimeout(this.timer);
    const busy = data.status || data.wanted || data.matches.some((m) => m.wanted && m.score === null);
    this.timer = setTimeout(() => this.load(), busy ? OFFERS_POLL_MS : 60000);
  }

  async act(path, body) {
    try {
      this.update(await api("POST", path, body));
    } catch (e) {
      alert(e.message);
    }
  }

  render(first) {
    if (!this.page) {
      this.head = el("div", {});
      this.wishesPanel = el("section", { class: "panel" });
      this.list = el("section", {});
      this.page = el("div", { class: "stack" }, this.head, this.wishesPanel, this.list);
      show(this.page);
    }
    if (this.wishesOpen === null) {
      const w = this.data.wishes;
      this.wishesOpen = !(w.roles.length || w.keywords.length || this.data.goals);
    }
    this.renderHead();
    if (first || !this.editing) this.renderWishes();
    this.renderList();
  }

  // --- the state of the search

  renderHead() {
    const d = this.data;
    let state;
    if (d.status && d.status.state === "searching") {
      state = el("span", { class: "thinking dots" }, "Przeszukuję portale (" + d.status.done + "/" + d.status.total + ")");
    } else if (d.status && d.status.state === "reviewing") {
      state = el("span", { class: "thinking dots" }, "Model ocenia oferty (" + d.status.done + "/" + d.status.total + ")");
    } else if (d.wanted) {
      state = el("span", { class: "thinking dots" }, "Wyszukiwanie zaraz się zacznie");
    } else if (d.offers_at) {
      state = el("span", { class: "muted" }, "Ostatnie wyszukiwanie: " + fmtTime(d.offers_at) +
        "; kolejne co " + d.interval + " h.");
    } else {
      state = el("span", { class: "muted" }, "Jeszcze nie szukano.");
    }
    const busy = !!(d.status || d.wanted);
    fill(this.head,
      el("div", { class: "section-head" }, el("h1", {}, "Oferty pracy"),
        el("button", {
          class: "btn primary", type: "button", disabled: busy || !d.terms.length,
          title: d.terms.length ? null : "Napisz najpierw, czego szukasz",
          onclick: () => this.act("offers/search"),
        }, "Szukaj teraz")),
      el("p", { class: "search-state" }, state, " ",
        el("span", { class: "muted" }, "Ocenione dziś: " + d.daily.used + " z " + d.daily.limit + ".")),
      d.terms.length > 0 && el("p", { class: "muted small" }, "Szukane frazy: ",
        d.terms.map((t, i) => [i > 0 && ", ", el("b", {}, t)])),
      !me.has_key && el("p", { class: "hint warn" }, "⚠ Bez klucza API oferty nie będą oceniane: podaj go w Ustawieniach."),
      me.has_key && !me.has_models && el("p", { class: "hint warn" },
        "⚠ Wszystkie modele są wyłączone w Ustawieniach: oferty nie będą oceniane."));
  }

  // --- what the user looks for

  renderWishes() {
    const d = this.data;
    const w = d.wishes;
    const c = d.choices;
    const lines = (id, value, rows, placeholder) => {
      const t = el("textarea", { id, rows, placeholder });
      t.value = value.join("\n");
      return t;
    };
    const commas = (id, value, placeholder) => el("input", { type: "text", id, placeholder, value: value.join(", ") });
    const roles = lines("w-roles", w.roles, 4, "np. Inżynier oprogramowania C++\nEmbedded developer");
    const keywords = lines("w-keywords", w.keywords, 4, "np. C++\nLabVIEW\nsystemy pomiarowe");
    const cities = commas("w-cities", w.cities, "np. Warszawa, Kraków");
    const avoid = commas("w-avoid", w.avoid, "np. sprzedaż, call center");
    const salary = el("input", { type: "number", id: "w-salary", min: 0, step: 500, placeholder: "np. 12000",
                                 value: w.salary_min || "" });
    const goals = el("textarea", { id: "w-goals", rows: 4, maxlength: 6000,
      placeholder: "Twoimi słowami: czym chcesz się zajmować, co jest dla ciebie ważne, czego chcesz uniknąć…" });
    goals.value = d.goals || "";
    const checks = (key, options, chosen) => {
      const boxes = options.map(([value, label]) => {
        const box = el("input", { type: "checkbox", value, checked: chosen.includes(value) });
        return el("label", { class: "check" }, box, label);
      });
      return { node: el("div", { class: "check-grid" }, boxes),
               get: () => boxes.map((b) => b.firstChild).filter((b) => b.checked).map((b) => b.value) };
    };
    const modes = checks("modes", c.modes, w.modes);
    const contracts = checks("contracts", c.contracts, w.contracts);
    const levels = checks("levels", c.levels, w.levels);
    // no portal chosen means all of them
    const portals = checks("portals", c.portals, w.portals.length ? w.portals : c.portals.map((p) => p[0]));
    const error = el("p", { class: "error", role: "alert" });
    const note = el("span", { class: "muted small" });
    const save = el("button", { class: "btn primary", type: "submit" }, "Zapisz i szukaj");
    const split = (text, sep) => text.split(sep).map((s) => s.trim()).filter(Boolean);
    const addLines = (textarea, values) => {
      const now = split(textarea.value, "\n");
      for (const v of values) if (!now.some((n) => n.toLowerCase() === v.toLowerCase())) now.push(v);
      textarea.value = now.join("\n");
      this.editing = true;
    };
    const form = el("form", {
      oninput: () => { this.editing = true; note.textContent = ""; },
      onchange: () => { this.editing = true; },
      onsubmit: async (e) => {
        e.preventDefault();
        error.textContent = "";
        save.disabled = true;
        const chosen = portals.get();
        try {
          const data = await api("POST", "offers/wishes", {
            goals: goals.value,
            wishes: {
              roles: split(roles.value, "\n"), keywords: split(keywords.value, "\n"),
              cities: split(cities.value, ","), avoid: split(avoid.value, ","),
              modes: modes.get(), contracts: contracts.get(), levels: levels.get(),
              portals: chosen.length === c.portals.length ? [] : chosen, salary_min: salary.value,
            },
          });
          this.editing = false;
          this.update(data);
          this.wishesPanel.querySelector(".saved-note").textContent = "Zapisano – oferty zostaną wyszukane od nowa.";
        } catch (err) {
          error.textContent = err.message;
        } finally {
          save.disabled = false;
        }
      },
    },
      el("p", { class: "hint" }, "Na tej podstawie przeszukiwane są portale i wybierane oferty. Puste pole – bez " +
        "ograniczeń. Z każdego wiersza stanowisk i słów kluczowych powstaje fraza do wyszukania (najwyżej 8)."),
      d.suggestions.length > 0 && el("div", { class: "suggestions" },
        el("span", { class: "muted small" }, "Podpowiedzi z analizy profilu (kliknij, by dodać):"),
        el("div", { class: "tags" }, d.suggestions.map((s) => el("button", {
          type: "button", class: "tag-chip editable", title: "Stanowiska: " + s.roles.join(", ") +
            "\nSłowa kluczowe: " + s.keywords.join(", "),
          onclick: () => { addLines(roles, s.roles.slice(0, 3)); addLines(keywords, s.keywords.slice(0, 3)); },
        }, s.title, s.fit && el("span", { class: "level" }, s.fit))))),
      el("div", { class: "two" },
        el("div", {}, el("label", { for: "w-roles" }, "Stanowiska / czym chcesz się zajmować"), roles),
        el("div", {}, el("label", { for: "w-keywords" }, "Słowa kluczowe (technologie, dziedziny)"), keywords)),
      el("div", { class: "two" },
        el("div", {}, el("label", { for: "w-cities" }, "Miejscowości"), cities),
        el("div", {}, el("label", { for: "w-salary" }, "Wynagrodzenie co najmniej (PLN miesięcznie)"), salary)),
      el("div", { class: "two" },
        el("div", {}, el("label", {}, "Tryb pracy"), modes.node, el("label", {}, "Umowa"), contracts.node),
        el("div", {}, el("label", {}, "Poziom stanowiska"), levels.node)),
      el("label", { for: "w-avoid" }, "Unikaj ofert ze słowami (w tytule lub nazwie firmy)"), avoid,
      el("label", {}, "Portale"), portals.node,
      el("label", { for: "w-goals" }, "Czym chcesz się zajmować – twoimi słowami"), goals,
      el("div", { class: "row end" }, el("span", { class: "muted small saved-note" }), note, save),
      error);
    const details = el("details", { class: "wishes", open: this.wishesOpen,
                                    ontoggle: () => { this.wishesOpen = details.open; } },
      el("summary", {}, el("h2", {}, "Czego szukam")), form);
    fill(this.wishesPanel, details);
  }

  // --- the offers

  renderList() {
    const all = this.data.matches;
    const tabs = TABS.map(([key, label, test]) => [key, label, all.filter(test)]);
    const [, , shown] = tabs.find(([key]) => key === this.tab);
    if (this.tab === "todo") shown.sort((a, b) => (b.wanted - a.wanted) || (b.prescore - a.prescore));
    const empty = {
      best: all.length ? "Nie ma jeszcze ocenionych ofert – zajrzyj do „Do oceny”."
        : this.data.terms.length ? "Nie ma jeszcze ofert: wyszukiwanie trwa albo nic nie pasowało."
          : "Napisz powyżej, czego szukasz, a system zacznie przeszukiwać portale.",
      todo: "Brak ofert czekających na ocenę.",
      interested: "Oznacz oferty gwiazdką, żeby tu trafiły.",
      applied: "Oznacz oferty, na które aplikujesz.",
      rejected: "Brak odrzuconych ofert.",
    }[this.tab];
    fill(this.list,
      el("div", { class: "tabs offer-tabs" }, tabs.map(([key, label, items]) => el("button", {
        type: "button", class: key === this.tab ? "active" : "",
        onclick: () => { this.tab = key; this.renderList(); },
      }, label, el("span", { class: "count" }, items.length)))),
      this.tab === "todo" && shown.length > 0 && el("p", { class: "hint" },
        "Oferty, które przeszły sito programu, a których model jeszcze nie ocenił – najbardziej obiecujące " +
        "są oceniane same, do dziennego limitu. „Oceń” zleca ocenę od razu."),
      shown.length ? el("div", { class: "offer-list" }, shown.map((m) => this.card(m)))
        : el("p", { class: "muted empty-list" }, empty));
  }

  card(m) {
    const r = m.review;
    const salary = m.salary_min ? money(m.salary_min) + (m.salary_max && m.salary_max !== m.salary_min
      ? "–" + money(m.salary_max) : "") + " " + (m.currency || "") : null;
    const place = [m.cities.slice(0, 3).join(", ") + (m.cities.length > 3 ? " +" + (m.cities.length - 3) : ""),
                   MODE_NAMES[m.mode]].filter(Boolean).join(" · ");
    const meta = [place, salary, m.contracts.map((c) => CONTRACT_NAMES[c] || c).join("/"), LEVEL_NAMES[m.level],
                  PORTAL_NAMES[m.portal] || m.portal].filter(Boolean);
    const have = new Set(m.have);
    const mark = (status, label, cls, title) => el("button", {
      class: "btn small " + (cls || "") + (m.status === status ? " on" : ""), type: "button", title,
      onclick: () => this.act("offers/" + m.offer_id + "/status", { status: m.status === status ? "new" : status }),
    }, label);
    return el("article", { class: "panel offer" + (m.status === "rejected" ? " dim" : "") },
      el("div", { class: "offer-top" },
        el("div", { class: "score " + scoreClass(m.score), title: m.score === null ? "Jeszcze nieoceniona" : "Ocena modelu" },
          m.score === null ? "–" : m.score),
        el("div", { class: "offer-main" },
          el("a", { class: "offer-title", href: m.url, target: "_blank", rel: "noopener" }, m.title),
          el("div", { class: "offer-company" }, m.company),
          el("div", { class: "muted small" }, meta.join(" · "),
            m.last_seen < this.data.fresh && el("span", { class: "warn-text" }, " · może być już nieaktualna")))),
      m.skills.length > 0 && el("div", { class: "tags small" }, m.skills.map((s) =>
        el("span", { class: "tag-chip" + (have.has(s) ? " have" : ""), title: have.has(s) ? "Masz tę umiejętność" : null }, s))),
      r && el("p", { class: "offer-summary" }, r.summary),
      r && (r.pros.length > 0 || r.cons.length > 0) && el("div", { class: "two pros-cons" },
        el("ul", { class: "pros" }, r.pros.map((p) => el("li", {}, p))),
        el("ul", { class: "cons" }, r.cons.map((p) => el("li", {}, p)))),
      m.review_error && el("p", { class: "bad small" }, "⚠ " + m.review_error),
      m.wanted && m.score === null && el("p", { class: "thinking dots small" }, "Czeka na ocenę modelu"),
      el("div", { class: "offer-actions" },
        mark("interested", "★ Interesuje mnie", "", "Zapisz w „Interesujące”"),
        mark("applied", "✓ Aplikuję", "", "Zapisz w „Aplikowane”"),
        mark("rejected", "✕ Odrzuć", "danger", "Schowaj w „Odrzucone”"),
        m.score === null && !m.wanted && el("button", {
          class: "btn small", type: "button", onclick: () => this.act("offers/" + m.offer_id + "/review"),
        }, "Oceń"),
        el("span", { class: "muted small offer-dates" },
          (m.posted ? "dodana " + fmtTime(m.posted).slice(0, 10) : "znaleziona " + fmtTime(m.first_seen).slice(0, 10)) +
          (m.model ? " · oceniona przez " + m.model : ""))));
  }
}
