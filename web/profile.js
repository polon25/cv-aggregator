"use strict";
// The profile page: the user's CVs (uploading, what was read from each),
// the analysis a model wrote of the candidate, what the user says of
// themselves (what they look for is on the offers page, offers.js), and the profile merged from the CVs, section by section,
// which the user can correct: edit, add, delete and restore items. While a
// CV is being read or the analysis written, the page asks for news every
// few seconds. Uses the helpers of app.js (el, api, show...) and `schema`.

const POLL_MS = 3000;
const ACCEPT = ".pdf,.docx,.odt,.doc,.rtf,.txt,.md";
const LANGUAGES = { pl: "po polsku", en: "po angielsku", de: "po niemiecku" };
const CHIP_SECTIONS = ["skills", "languages", "interests"];
const DATE_RE = /^(\d{4}(-\d{1,2})?|\d{1,2}[./]\d{4})$/;

// How an item of each section is shown: its title, what comes under it and
// a line of details.
const CARDS = {
  experience: { title: (i) => i.title, sub: (i) => i.organization, meta: (i) => [fmtSpan(i), i.location] },
  education: { title: (i) => i.institution, sub: (i) => [i.degree, i.field].filter(Boolean).join(", "),
               meta: (i) => [fmtSpan(i)] },
  certificates: { title: (i) => i.name, sub: (i) => i.issuer, meta: (i) => [fmtDate(i.date)] },
  projects: { title: (i) => i.name, sub: (i) => i.role, meta: (i) => [fmtSpan(i)] },
  publications: { title: (i) => i.title, sub: (i) => i.authors, meta: (i) => [i.venue, fmtDate(i.date)] },
  awards: { title: (i) => i.name, sub: (i) => i.issuer, meta: (i) => [fmtDate(i.date)] },
};
const FOUND = { experience: "stanowiska", education: "szkoły", skills: "umiejętności", languages: "języki",
                certificates: "kursy", projects: "projekty", publications: "publikacje", awards: "nagrody",
                interests: "zainteresowania" };
const FIT = { wysokie: "good", "średnie": "mid", niskie: "low" };

function kilobytes(size) {
  return size < 1024 * 1024 ? Math.max(1, Math.round(size / 1024)) + " kB"
    : (size / 1024 / 1024).toFixed(1).replace(".", ",") + " MB";
}

function formDialog(title, body, buttons, onSubmit) {
  const error = el("p", { class: "error", role: "alert" });
  const dialog = el("dialog", { class: "panel wide-dialog" });
  dialog.append(
    el("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        error.textContent = "";
        try {
          if (await onSubmit() !== false) dialog.close();
        } catch (err) {
          error.textContent = err.message;
        }
      },
    },
      el("h2", {}, title), body,
      el("div", { class: "row end" }, buttons(dialog, error),
        el("button", { class: "btn", type: "button", onclick: () => dialog.close() }, "Anuluj"),
        el("button", { class: "btn primary", type: "submit" }, "Zapisz")),
      error));
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  const first = dialog.querySelector("input, textarea");
  if (first) first.focus();
}

// Inputs for fields of the schema; returns {node, values()}.
function fieldInputs(fields, values) {
  const inputs = {};
  const rows = fields.map((f) => {
    const id = "f-" + f.key;
    const value = values[f.key];
    let input;
    if (f.kind === "bool") {
      input = el("input", { type: "checkbox", id, checked: !!value });
      return el("label", { class: "inline check-row", for: id }, (inputs[f.key] = input), f.label);
    }
    if (f.kind === "textarea" || f.kind === "list") {
      input = el("textarea", { id, rows: f.kind === "list" ? 5 : 4, maxlength: f.kind === "list" ? 20000 : f.max });
      input.value = f.kind === "list" ? (value || []).join("\n") : value || "";
    } else {
      input = el("input", { type: "text", id, maxlength: f.kind === "date" ? 10 : f.max,
                            placeholder: f.kind === "date" ? "RRRR-MM, MM.RRRR albo RRRR" : null });
      input.value = value || "";
    }
    inputs[f.key] = input;
    return [el("label", { for: id }, f.label),
      f.kind === "list" && el("p", { class: "hint" }, "Jedna pozycja w wierszu."), input];
  });
  return {
    node: el("div", { class: "fields" }, rows),
    values() {
      const out = {};
      for (const f of fields) {
        const input = inputs[f.key];
        if (f.kind === "bool") out[f.key] = input.checked;
        else if (f.kind === "list") out[f.key] = input.value.split("\n").map((s) => s.trim()).filter(Boolean);
        else out[f.key] = input.value.trim();
        if (f.kind === "date" && out[f.key] && !DATE_RE.test(out[f.key])) {
          throw new Error("Pole „" + f.label + "”: wpisz datę jako RRRR-MM, MM.RRRR albo sam rok.");
        }
      }
      return out;
    },
  };
}

class ProfileView {
  constructor() {
    this.data = null;
    this.timer = null;
    this.stopped = false;
    this.uploads = []; // {name, error} of the files being sent
    this.dirty = false; // the description is being edited
    this.aboutSaved = false;
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
      this.update(await api("GET", "profile"));
    } catch (e) {
      if (this.stopped) return;
      if (e.status === 401) return loadMe();
      if (!this.data) return show(el("section", { class: "panel notice" }, e.message));
      this.timer = setTimeout(() => this.load(), POLL_MS * 3);
    }
  }

  update(data) {
    if (this.stopped) return;
    this.data = data;
    this.render();
    clearTimeout(this.timer);
    if (data.busy) this.timer = setTimeout(() => this.load(), POLL_MS);
  }

  // An action that returns the new profile.
  async act(method, path, body) {
    this.update(await api(method, path, body));
  }

  cvLabel(id) {
    const index = this.data.cvs.findIndex((c) => c.id === id);
    return index < 0 ? null : "CV " + (index + 1);
  }

  render() {
    if (!this.page) {
      this.head = el("div", {});
      this.cvPanel = el("section", { class: "panel" });
      this.analysisPanel = el("section", { class: "panel" });
      this.aboutPanel = el("section", { class: "panel" });
      this.personalPanel = el("section", { class: "panel" });
      this.sections = el("div", { class: "stack" });
      this.page = el("div", { class: "stack" }, this.head, this.cvPanel, this.analysisPanel,
                     this.aboutPanel, this.personalPanel, this.sections);
      show(this.page);
    }
    this.renderHead();
    this.renderCvs();
    this.renderAnalysis();
    if (!this.dirty) this.renderAbout();
    this.renderPersonal();
    this.renderSections();
  }

  renderHead() {
    const p = this.data.profile.personal;
    const s = this.data.stats;
    const facts = [
      s.experience_months > 0 && ["Staż pracy", s.experience + (s.career_start ? " (od " + s.career_start + ")" : "")],
      s.highest_degree && ["Wykształcenie", s.highest_degree],
      s.current_jobs.length > 0 && ["Obecnie", s.current_jobs.join("; ")],
    ].filter(Boolean);
    fill(this.head, 
      el("h1", {}, p.name.value || "Twój profil"),
      p.headline.value && el("p", { class: "lead" }, p.headline.value),
      facts.length > 0 && el("div", { class: "facts" }, facts.map(([label, value]) =>
        el("span", { class: "fact" }, el("span", { class: "muted" }, label + ": "), el("b", {}, value)))));
  }

  // --- CVs

  renderCvs() {
    const d = this.data;
    const full = d.cvs.length >= d.max_cvs;
    const input = el("input", { type: "file", accept: ACCEPT, multiple: true, hidden: true,
                                onchange: () => this.upload([...input.files]) });
    const drop = el("label", {
      class: "dropzone" + (full ? " disabled" : ""),
      ondragover: (e) => { e.preventDefault(); drop.classList.add("over"); },
      ondragleave: () => drop.classList.remove("over"),
      ondrop: (e) => {
        e.preventDefault();
        drop.classList.remove("over");
        if (!full) this.upload([...e.dataTransfer.files]);
      },
    }, full ? null : input,
      el("b", {}, full ? "Masz już " + d.max_cvs + " CV – usuń któreś, żeby dodać nowe."
        : "Przeciągnij tu pliki CV albo kliknij, żeby wybrać"),
      el("span", { class: "muted small" }, "PDF, DOCX, ODT, DOC, RTF lub TXT, do " +
        String(d.max_mb).replace(".", ",") + " MB. Różne wersje CV się uzupełniają: nowsze aktualizują starsze."));
    fill(this.cvPanel, 
      el("div", { class: "section-head" }, el("h2", {}, "Twoje CV"),
        el("span", { class: "muted" }, d.cvs.length + " / " + d.max_cvs)),
      d.cvs.length > 0 && el("ul", { class: "cv-list" }, d.cvs.map((cv) => this.cvRow(cv))),
      this.uploads.length > 0 && el("ul", { class: "uploads" }, this.uploads.map((u) =>
        el("li", { class: u.error ? "error" : "muted" }, u.error ? "⚠ " + u.name + ": " + u.error
          : el("span", { class: "dots" }, "Wysyłam " + u.name)))),
      drop,
      !me.has_key && el("p", { class: "hint warn" }, "⚠ Bez klucza API OpenRoutera CV nie zostaną odczytane: " +
        "podaj go w Ustawieniach."),
      me.has_key && !me.has_models && el("p", { class: "hint warn" }, "⚠ Wszystkie modele są wyłączone, więc " +
        "CV nie zostaną odczytane: włącz płatne albo darmowe w Ustawieniach."));
  }

  cvRow(cv) {
    let status;
    if (cv.status === "pending") status = el("span", { class: "muted" }, "czeka w kolejce");
    else if (cv.status === "working") status = el("span", { class: "thinking dots" }, "model czyta CV");
    else if (cv.status === "failed") status = el("span", { class: "bad" }, "⚠ " + (cv.error || "nie udało się"));
    else {
      const found = Object.entries(cv.found || {}).filter(([, n]) => n)
        .map(([s, n]) => FOUND[s] + " " + n).join(", ");
      status = el("span", { class: "good", title: cv.model ? "Odczytał: " + cv.model : null },
                  "✓ " + (found || "nic nie znaleziono"));
    }
    const meta = [cv.kind, kilobytes(cv.size), "wgrane " + fmtTime(cv.uploaded_at),
                  cv.language && (LANGUAGES[cv.language] || cv.language)];
    return el("li", { class: "cv" },
      el("span", { class: "source" }, this.cvLabel(cv.id)),
      el("div", { class: "cv-body" },
        el("a", { class: "cv-name", href: "api/cvs/" + cv.id + "/file" }, cv.filename),
        el("div", { class: "muted small" }, meta.filter(Boolean).join(" · ")),
        el("div", { class: "small" }, status)),
      el("div", { class: "cv-buttons" },
        cv.status === "failed" && el("button", {
          class: "btn small", type: "button", onclick: () => this.act("POST", "cvs/" + cv.id + "/retry").catch((e) => alert(e.message)),
        }, "Spróbuj ponownie"),
        cv.chars > 0 && el("button", { class: "btn small", type: "button", onclick: () => this.showText(cv) }, "Tekst"),
        el("button", {
          class: "btn small danger", type: "button", onclick: async () => {
            if (!confirm("Usunąć " + cv.filename + "? Z profilu znikną informacje, które były tylko w tym CV " +
                         "(twoje ręczne zmiany zostają).")) return;
            await this.act("POST", "cvs/" + cv.id + "/delete").catch((e) => alert(e.message));
          },
        }, "Usuń")));
  }

  async showText(cv) {
    let text;
    try {
      text = (await api("GET", "cvs/" + cv.id + "/text")).text;
    } catch (e) {
      return alert(e.message);
    }
    const dialog = el("dialog", {
      class: "panel wide-dialog", onclick: (e) => { if (e.target === dialog) dialog.close(); },
    },
      el("div", { class: "item-top" },
        el("div", {}, el("h2", {}, cv.filename),
          el("div", { class: "muted small" }, "Tekst odczytany z pliku, który dostaje model")),
        el("button", { class: "btn small", type: "button", onclick: () => dialog.close() }, "Zamknij")),
      el("pre", { class: "cv-text" }, text));
    dialog.addEventListener("close", () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
  }

  async upload(files) {
    if (!files.length) return;
    this.uploads = files.map((f) => ({ name: f.name, error: null }));
    this.renderCvs();
    for (const [n, file] of files.entries()) {
      const entry = this.uploads[n];
      if (file.size > this.data.max_mb * 1024 * 1024) {
        entry.error = "plik jest za duży";
        continue;
      }
      try {
        this.update(await api("POST", "cvs?name=" + encodeURIComponent(file.name), file));
        this.uploads.splice(this.uploads.indexOf(entry), 1);
      } catch (e) {
        entry.error = e.message;
      }
      this.renderCvs();
    }
    // errors stay on show until the next upload
    this.uploads = this.uploads.filter((u) => u.error);
    this.renderCvs();
  }

  // --- the analysis

  renderAnalysis() {
    const a = this.data.analysis;
    const r = a.result;
    const working = a.status === "working";
    const waiting = a.status === "pending";
    let state = null;
    if (working) state = el("p", { class: "thinking dots" }, "Model pisze analizę");
    else if (waiting) state = el("p", { class: "muted" }, "Analiza czeka – powstanie, gdy wszystkie CV zostaną odczytane.");
    else if (a.status === "failed") state = el("p", { class: "bad" }, "⚠ Nie udało się napisać analizy: " + a.error);
    else if (a.stale) state = el("p", { class: "hint warn" }, "Profil zmienił się od czasu tej analizy.");
    const again = el("button", {
      class: "btn small" + (a.stale || a.status === "failed" || (!r && !this.data.empty) ? " primary" : ""),
      type: "button", disabled: working || waiting || this.data.empty,
      onclick: () => this.act("POST", "profile/analysis").catch((e) => alert(e.message)),
    }, r ? "Przygotuj ponownie" : "Przygotuj analizę");
    const list = (items) => el("ul", { class: "points" }, items.map((p) =>
      el("li", {}, el("b", {}, p.title), p.detail && el("span", {}, " – " + p.detail))));
    fill(this.analysisPanel, 
      el("div", { class: "section-head" }, el("h2", {}, "Analiza kandydata"), again),
      state,
      !r && !state && el("p", { class: "muted" }, this.data.empty
        ? "Wgraj CV albo uzupełnij profil: po odczytaniu CV analiza powstanie sama."
        : "Nie ma jeszcze analizy."),
      r && el("div", { class: "analysis" + (working || a.stale ? " faded" : "") },
        el("p", { class: "summary" }, r.summary),
        el("div", { class: "two" },
          el("div", {}, el("h3", { class: "good" }, "Mocne strony"), list(r.strengths)),
          el("div", {}, el("h3", { class: "bad" }, "Słabe strony"), list(r.weaknesses))),
        el("h3", {}, "Potencjalne ścieżki kariery"),
        el("div", { class: "cards" }, r.career_paths.map((p) => el("div", { class: "path" },
          p.fit && el("span", { class: "badge fit-" + FIT[p.fit] }, "dopasowanie: " + p.fit),
          el("div", { class: "item-title" }, p.title),
          p.why && el("p", {}, p.why),
          p.gaps && el("p", { class: "small" }, el("b", {}, "Czego brakuje: "), p.gaps),
          p.roles.length > 0 && el("div", { class: "tags" }, p.roles.map((t) => el("span", { class: "tag-chip" }, t))),
          p.keywords.length > 0 && el("p", { class: "muted small" }, "Słowa kluczowe: " + p.keywords.join(", "))))),
        el("p", { class: "muted small" }, "Przygotowano " + fmtTime(a.at) + (a.model ? " · " + a.model : ""))));
  }

  // --- the user's own words

  renderAbout() {
    const about = el("textarea", { id: "about", rows: 4, maxlength: 6000,
      placeholder: "Kilka zdań o sobie: czego nie widać w CV, co lubisz robić, w czym chcesz się rozwijać…" });
    about.value = this.data.about;
    const note = el("span", { class: "muted small" }, this.aboutSaved ? "Zapisano." : "");
    this.aboutSaved = false;
    const save = el("button", { class: "btn primary", type: "submit", disabled: true }, "Zapisz");
    const changed = () => {
      this.dirty = about.value !== this.data.about;
      save.disabled = !this.dirty;
      note.textContent = "";
    };
    about.addEventListener("input", changed);
    fill(this.aboutPanel, el("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        save.disabled = true;
        try {
          const data = await api("POST", "profile/about", { about: about.value });
          this.dirty = false;
          this.aboutSaved = true;
          this.update(data);
        } catch (err) {
          this.dirty = true;
          note.textContent = err.message;
          save.disabled = false;
        }
      },
    },
      el("h2", {}, "O mnie"),
      el("p", { class: "hint" }, "Twoimi słowami – analiza i ocena ofert biorą to pod uwagę. Czego szukasz, " +
        "napiszesz w zakładce ", el("a", { href: "#/oferty" }, "Oferty"), "."),
      about,
      el("div", { class: "row end" }, note, save)));
  }

  // --- personal details

  renderPersonal() {
    const p = this.data.profile.personal;
    const rows = schema.personal.map((f) => {
      const entry = p[f.key];
      const value = f.kind === "list" ? entry.value.join(", ") : entry.value;
      const mine = entry.from === "user";
      if (!value && !mine) return null;
      const shown = f.key === "email" && value ? el("a", { href: "mailto:" + value }, value)
        : f.kind === "list" ? entry.value.map((link, n) => [n > 0 && ", ",
          el("a", { href: /^https?:/.test(link) ? link : "https://" + link, target: "_blank", rel: "noopener" }, link)])
        : value || el("span", { class: "muted" }, "(puste)");
      return [el("dt", {}, f.label), el("dd", { class: f.kind === "textarea" ? "prose" : "" }, shown,
        mine && el("span", { class: "muted small" }, " · zmienione ręcznie ",
          el("button", {
            class: "link", type: "button", title: "Z CV: " + ([].concat(entry.cv || []).join(", ") || "(puste)"),
            onclick: () => this.act("POST", "profile/personal", { [f.key]: null }).catch((e) => alert(e.message)),
          }, "przywróć z CV")))];
    }).filter(Boolean);
    fill(this.personalPanel, 
      el("div", { class: "section-head" }, el("h2", {}, "Dane osobowe"),
        el("button", { class: "btn small", type: "button", onclick: () => this.editPersonal() }, "Edytuj")),
      rows.length ? el("dl", { class: "details" }, rows) : el("p", { class: "muted" }, "Brak danych."));
  }

  editPersonal() {
    const p = this.data.profile.personal;
    const values = Object.fromEntries(schema.personal.map((f) => [f.key, p[f.key].value]));
    const inputs = fieldInputs(schema.personal, values);
    formDialog("Dane osobowe", inputs.node, () => null, async () => {
      const now = inputs.values();
      const changed = {};
      for (const f of schema.personal) {
        if (JSON.stringify(now[f.key]) !== JSON.stringify(values[f.key])) changed[f.key] = now[f.key];
      }
      if (Object.keys(changed).length) await this.act("POST", "profile/personal", changed);
    });
  }

  // --- the sections

  renderSections() {
    const d = this.data;
    fill(this.sections, ...schema.sections.map((s) => {
      const items = d.profile.sections[s.key];
      const removed = d.profile.removed[s.key];
      const add = el("button", { class: "btn small", type: "button", onclick: () => this.editItem(s, null) }, "Dodaj");
      return el("section", { class: "panel" + (items.length ? "" : " empty-section") },
        el("div", { class: "section-head" },
          el("h2", {}, s.title, items.length > 0 && el("span", { class: "count" }, items.length)), add),
        items.length === 0 ? el("p", { class: "muted small" }, "Brak wpisów.")
          : CHIP_SECTIONS.includes(s.key) ? this.chips(s, items)
            : el("div", { class: "items" }, items.map((i) => this.itemCard(s, i))),
        removed.length > 0 && el("details", { class: "removed" },
          el("summary", {}, "Usunięte (" + removed.length + ")"),
          el("ul", {}, removed.map((r) => el("li", {}, r.label, " ",
            el("button", {
              class: "link", type: "button",
              onclick: () => this.act("POST", "profile/removed/" + s.key + "/" + r.id + "/restore")
                .catch((e) => alert(e.message)),
            }, "przywróć"))))));
    }));
  }

  sources(item) {
    const labels = item.sources.map((id) => this.cvLabel(id)).filter(Boolean);
    const title = item.sources.map((id) => (this.data.cvs.find((c) => c.id === id) || {}).filename)
      .filter(Boolean).join(", ");
    return el("span", { class: "sources", title: title ? "Z: " + title : null },
      labels.map((l) => el("span", { class: "source" }, l)),
      item.manual && el("span", { class: "source mine" }, item.edited ? "poprawione" : "dodane ręcznie"));
  }

  chips(s, items) {
    const groups = new Map();
    for (const item of items) {
      const key = item.category || "";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(item);
    }
    return el("div", { class: "chip-groups" }, [...groups].map(([category, list]) => el("div", { class: "chip-group" },
      category && el("h4", {}, category),
      el("div", { class: "tags" }, list.map((item) => el("button", {
        type: "button", class: "tag-chip editable" + (item.manual ? " mine" : ""),
        title: "Edytuj · " + (item.manual ? (item.edited ? "poprawione ręcznie" : "dodane ręcznie") : "") +
          item.sources.map((id) => this.cvLabel(id)).filter(Boolean).join(", "),
        onclick: () => this.editItem(s, item),
      }, item.name, item.level && el("span", { class: "level" }, item.level)))))));
  }

  itemCard(s, item) {
    const card = CARDS[s.key];
    const meta = card.meta(item).filter(Boolean);
    const lists = s.fields.filter((f) => f.kind === "list");
    const texts = s.fields.filter((f) => f.kind === "textarea" || (f.kind === "text" && ["thesis", "url"].includes(f.key)));
    const url = item.url && (/^https?:/.test(item.url) ? item.url : /^10\./.test(item.url) ? "https://doi.org/" + item.url : null);
    return el("article", { class: "item" },
      el("div", { class: "item-top" },
        el("div", {},
          el("div", { class: "item-title" }, card.title(item)),
          card.sub(item) && el("div", {}, card.sub(item)),
          meta.length > 0 && el("div", { class: "muted small" }, meta.join(" · "))),
        el("div", { class: "item-buttons" },
          item.edited && el("button", {
            class: "icon-btn", type: "button", title: "Wróć do wersji z CV", "aria-label": "Wróć do wersji z CV",
            onclick: () => this.act("POST", "profile/items/" + s.key + "/" + item.id + "/revert").catch((e) => alert(e.message)),
          }, "↺"),
          el("button", { class: "icon-btn", type: "button", title: "Edytuj", "aria-label": "Edytuj",
                         onclick: () => this.editItem(s, item) }, "✎"),
          el("button", { class: "icon-btn danger", type: "button", title: "Usuń", "aria-label": "Usuń",
                         onclick: () => this.deleteItem(s, item) }, "✕"))),
      texts.map((f) => item[f.key] && (f.key === "url"
        ? el("p", { class: "small" }, url ? el("a", { href: url, target: "_blank", rel: "noopener" }, item.url) : item.url)
        : el("p", { class: f.kind === "textarea" ? "prose" : "small" },
          f.key === "thesis" ? [el("span", { class: "muted" }, "Praca: "), item[f.key]] : item[f.key]))),
      lists.map((f) => item[f.key].length > 0 && (f.key === "skills"
        ? el("div", { class: "tags small" }, item[f.key].map((t) => el("span", { class: "tag-chip" }, t)))
        : el("ul", { class: "highlights" }, item[f.key].map((h) => el("li", {}, h))))),
      this.sources(item));
  }

  editItem(s, item) {
    const values = item || {};
    const inputs = fieldInputs(s.fields, values);
    const note = item && !item.manual && el("p", { class: "hint" },
      "Zapisana zmiana zastąpi to, co mówią CV; przy wpisie pojawi się „↺”, żeby wrócić do wersji z CV.");
    formDialog(item ? "Edycja: " + s.title.toLowerCase() : "Nowy wpis: " + s.title.toLowerCase(),
      [note, inputs.node],
      (dialog, error) => item && el("button", {
        class: "btn danger", type: "button", onclick: async () => {
          try {
            if (await this.deleteItem(s, item)) dialog.close();
          } catch (e) {
            error.textContent = e.message;
          }
        },
      }, "Usuń"),
      () => this.act("POST", "profile/items/" + s.key, { id: item ? item.id : null, fields: inputs.values() }));
  }

  async deleteItem(s, item) {
    const fromCv = item.sources.length > 0;
    if (!confirm(fromCv ? "Usunąć ten wpis? Nie wróci z tych CV ani z nowych, które go zawierają – " +
      "można go przywrócić z listy „Usunięte”." : "Usunąć ten wpis?")) return false;
    try {
      await this.act("POST", "profile/items/" + s.key + "/" + item.id + "/delete");
    } catch (e) {
      alert(e.message);
      return false;
    }
    return true;
  }
}
