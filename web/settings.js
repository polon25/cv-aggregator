"use strict";
// The user's settings: their OpenRouter API key, which their CVs are read
// with, the mailbox their job alerts are forwarded to, their ladder of models, paid ones and free ones, in the
// order the agents ask them, and whether each of the two may be used: free
// models' providers may keep what they're sent, so they're off until the
// user turns them on. Uses the helpers of app.js (el, api) and its `me`.

function dollars(amount) {
  return amount.toFixed(2).replace(".", ",") + " USD";
}

// What a model costs, from OpenRouter's list (null: the list isn't known).
function priceNote(model, catalogue) {
  if (!catalogue) return "";
  const known = catalogue.get(model);
  if (!known) return "nie ma takiego modelu w OpenRouterze";
  if (known.free) return "darmowy";
  const price = (n) => n.toFixed(n < 1 ? 3 : 2).replace(/0+$/, "").replace(/\.$/, "").replace(".", ",");
  return price(known.input) + " / " + price(known.output) + " USD za mln tokenów (wejście / wyjście)";
}

// One of the two lists: rows to move up, down and out, and a field to add
// a model with OpenRouter's models as suggestions. Returns {node, get, set}.
function modelList(id, title, hint, catalogue, suggest, max) {
  let models = [];
  const rows = el("ol", { class: "model-list" });
  const input = el("input", { type: "text", list: id + "-models", maxlength: 100,
                              placeholder: "nazwa modelu, np. " + (id === "paid" ? "~anthropic/claude-haiku-latest" : "google/gemma-4-31b-it:free"),
                              "aria-label": "Model do dodania: " + title });
  const options = el("datalist", { id: id + "-models" },
    catalogue ? [...catalogue.values()].filter(suggest).map((m) =>
      el("option", { value: m.id }, m.name + (m.free ? "" : " – " + priceNote(m.id, catalogue)))) : []);
  const note = el("p", { class: "error", role: "alert" });
  const render = () => {
    rows.replaceChildren(...models.map((model, i) => {
      const price = priceNote(model, catalogue);
      const move = (to) => () => {
        models.splice(to, 0, models.splice(i, 1)[0]);
        render();
      };
      return el("li", {},
        el("span", { class: "model-name" }, el("b", {}, model),
          price && el("span", { class: "muted small" + (catalogue && !catalogue.get(model) ? " bad" : "") }, price)),
        el("span", { class: "model-buttons" },
          el("button", { type: "button", class: "icon-btn", disabled: i === 0, title: "Wyżej",
                         "aria-label": "Przesuń wyżej: " + model, onclick: move(i - 1) }, "↑"),
          el("button", { type: "button", class: "icon-btn", disabled: i === models.length - 1, title: "Niżej",
                         "aria-label": "Przesuń niżej: " + model, onclick: move(i + 1) }, "↓"),
          el("button", { type: "button", class: "icon-btn danger", title: "Usuń",
                         "aria-label": "Usuń: " + model, onclick: () => { models.splice(i, 1); render(); } }, "✕")));
    }));
    if (!models.length) rows.append(el("li", { class: "muted empty" }, "(lista jest pusta)"));
  };
  const add = () => {
    const model = input.value.trim();
    note.textContent = "";
    if (!model) return;
    if (models.includes(model)) note.textContent = "Ten model już jest na liście.";
    else if (models.length >= max) note.textContent = "Najwyżej " + max + " modeli na liście.";
    else if (catalogue && !catalogue.get(model)) note.textContent = "OpenRouter nie ma modelu „" + model + "”.";
    else if (catalogue && !suggest(catalogue.get(model))) {
      note.textContent = id === "free" ? "Ten model jest płatny: dodaj go do listy płatnych."
        : "Ten model jest darmowy: dodaj go do listy darmowych.";
    } else {
      models.push(model);
      input.value = "";
      render();
    }
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); add(); }
  });
  return {
    node: el("div", { class: "model-box" },
      el("h4", {}, title), el("p", { class: "hint" }, hint), rows,
      el("div", { class: "row" }, input, options,
        el("button", { class: "btn small", type: "button", onclick: add }, "Dodaj")),
      note),
    get: () => models.slice(),
    set: (list) => { models = list.slice(); note.textContent = ""; render(); },
  };
}

async function settingsDialog() {
  let settings, catalogue = null;
  try {
    settings = await api("GET", "settings");
  } catch (e) {
    return alert(e.message);
  }
  try {
    const { models } = await api("GET", "models");
    if (models.length) catalogue = new Map(models.map((m) => [m.id, m]));
  } catch (e) { /* no suggestions or prices then */ }

  // --- the key
  const keyState = el("div", {});
  const keyInput = el("input", { type: "password", id: "api-key", autocomplete: "off", maxlength: 300,
                                 placeholder: "sk-or-v1-…" });
  const keyError = el("p", { class: "error", role: "alert" });
  const saveKey = el("button", { class: "btn primary", type: "submit" }, "Sprawdź i zapisz klucz");
  const renderKey = () => {
    const funds = settings.funds;
    const left = funds && funds.left !== null && el("span", { class: funds.low ? "warn" : "" },
      " " + (funds.low ? "⚠ " : "") + "Na płatne modele zostało ok. " + dollars(funds.left) + ".");
    keyState.replaceChildren(settings.key.set
      ? el("p", { class: "key-state ok" }, "✓ Masz zapisany klucz kończący się na …" + settings.key.hint + ".",
          left, " ",
          el("button", {
            class: "link danger", type: "button", onclick: async () => {
              if (!confirm("Usunąć klucz? Bez klucza CV nie będą odczytywane, dopóki nie podasz nowego.")) return;
              await send("settings/key", { key: "" }, keyError);
            },
          }, "Usuń klucz"))
      : el("p", { class: "key-state warn" }, "⚠ Nie masz jeszcze klucza: bez niego CV nie zostaną odczytane."));
    keyInput.placeholder = settings.key.set ? "wklej nowy klucz, żeby zmienić" : "sk-or-v1-…";
  };

  // --- the models
  const paid = modelList("paid", "Płatne modele",
    "Pytane najpierw, po kolei. Kosztują środki z klucza.",
    catalogue, (m) => !m.free, settings.max);
  const free = modelList("free", "Darmowe modele",
    "Pytane po płatnych (albo od razu, gdy płatne są wyłączone).", catalogue, (m) => m.free, settings.max);
  const modelsError = el("p", { class: "error", role: "alert" });
  const modelsNote = el("span", { class: "muted small" });
  const usePaid = el("input", { type: "checkbox", id: "use-paid" });
  const useFree = el("input", { type: "checkbox", id: "use-free" });
  const lastNote = el("p", { class: "model-last" }, "🔒 Na końcu zawsze: ", el("b", {}, settings.last),
    el("span", { class: "muted small" }, " – dowolny darmowy model, gdy żaden z powyższych nie odpowie."));
  const dailyInput = el("input", { type: "number", id: "daily-reviews", min: 0, max: settings.max_reviews,
                                   value: settings.daily_reviews });
  dailyInput.addEventListener("change", () => send("settings/reviews", { daily: dailyInput.value }, modelsError));
  const noneNote = el("p", { class: "key-state warn" }, "⚠ Wszystkie modele są wyłączone: CV nie będą odczytywane.");
  const renderModelLists = () => {
    paid.set(settings.paid);
    free.set(settings.free);
    modelsNote.textContent = settings.custom ? "To twoja własna lista." : "To lista domyślna.";
  };
  const renderSwitch = () => {
    usePaid.checked = settings.use_paid;
    usePaid.disabled = !settings.paid.length;
    useFree.checked = settings.use_free;
    paid.node.classList.toggle("off", !settings.use_paid);
    free.node.classList.toggle("off", !settings.use_free);
    lastNote.hidden = !settings.use_free;
    noneNote.hidden = settings.use_paid || settings.use_free;
  };
  usePaid.addEventListener("change", () => send("settings/use", { use_paid: usePaid.checked }, modelsError));
  useFree.addEventListener("change", async () => {
    if (useFree.checked && !confirm("Włączyć darmowe modele?\n\nDostawcy darmowych modeli mogą zapisywać " +
        "przesłane im dane i używać ich do trenowania modeli. Trafi do nich treść twoich CV: dane osobowe, " +
        "kontakt i historia zatrudnienia.")) {
      useFree.checked = false;
      return;
    }
    await send("settings/use", { use_free: useFree.checked }, modelsError);
  });

  async function send(path, body, error, button) {
    error.textContent = "";
    if (button) button.disabled = true;
    try {
      settings = await api("POST", path, body);
      me.has_key = settings.key.set;
      me.has_models = settings.use_paid || settings.use_free;
      renderAccount();
      renderKey();
      if (path === "settings/models") renderModelLists();
      renderSwitch();
      return true;
    } catch (e) {
      error.textContent = e.message;
      return false;
    } finally {
      if (button) button.disabled = false;
    }
  }

  const saveModels = el("button", { class: "btn primary", type: "button" }, "Zapisz modele");
  saveModels.addEventListener("click", async () => {
    if (await send("settings/models", { paid: paid.get(), free: free.get() }, modelsError, saveModels)) {
      modelsNote.textContent = "Zapisano.";
    }
  });
  // --- the mailbox job alerts are forwarded to
  const mailAddress = el("input", { type: "text", id: "mail-address", autocomplete: "off", maxlength: 200,
                                    placeholder: "np. moje.alerty@gmail.com" });
  const mailPassword = el("input", { type: "password", id: "mail-password", autocomplete: "new-password",
                                     maxlength: 200 });
  const mailHost = el("input", { type: "text", id: "mail-host", maxlength: 253,
                                 placeholder: "uzupełni się dla Gmaila, WP, Onetu, Interii…" });
  const mailFolder = el("input", { type: "text", id: "mail-folder", maxlength: 100, placeholder: "INBOX" });
  const mailState = el("div", {});
  const mailError = el("p", { class: "error", role: "alert" });
  const saveMail = el("button", { class: "btn primary", type: "submit" }, "Sprawdź i zapisz");
  const renderMailbox = () => {
    const box = settings.mailbox;
    mailAddress.value = box ? box.address : "";
    mailHost.value = box ? box.host : "";
    mailFolder.value = box ? box.folder : "";
    mailPassword.value = "";
    mailPassword.placeholder = box ? "zostaw puste, żeby nie zmieniać" : "";
    fill(mailState, box
      ? el("p", { class: "key-state ok" }, "✓ Połączono ze skrzynką " + box.address + " (" + box.host + "): " +
          box.messages + " wiadomości w folderze " + box.folder + ", sprawdzono " + fmtTime(box.checked_at) + ". ",
          el("button", {
            class: "link danger", type: "button", onclick: async () => {
              if (!confirm("Usunąć skrzynkę? Hasło zostanie usunięte z serwera.")) return;
              await send("settings/mailbox", { remove: true }, mailError);
              renderMailbox();
            },
          }, "Usuń skrzynkę"))
      : el("p", { class: "key-state" }, "Nie podano skrzynki."));
  };
  async function saveMailbox(e) {
    e.preventDefault();
    saveMail.textContent = "Łączę…";
    if (await send("settings/mailbox", { address: mailAddress.value, password: mailPassword.value,
                                          host: mailHost.value, folder: mailFolder.value }, mailError, saveMail)) {
      renderMailbox();
    }
    saveMail.textContent = "Sprawdź i zapisz";
  }

  const dialog = el("dialog", { class: "panel settings-dialog" },
    el("div", { class: "item-top" }, el("h2", {}, "Ustawienia"),
      el("button", { class: "btn small", type: "button", onclick: () => dialog.close() }, "Zamknij")),

    el("h3", { class: "form-section" }, "Klucz API OpenRoutera"),
    el("p", { class: "hint" }, "CV czytają i analizują modele językowe z ",
      el("a", { href: "https://openrouter.ai/keys", target: "_blank", rel: "noopener" }, "OpenRoutera"),
      ". Podaj swój klucz: twoje CV są czytane z jego środków. Klucz jest przechowywany na serwerze " +
      "zaszyfrowany i nigdy nie jest pokazywany. Warto ustawić mu w OpenRouterze limit wydatków."),
    keyState,
    el("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        if (!keyInput.value.trim()) { keyError.textContent = "Wklej klucz."; return; }
        saveKey.textContent = "Sprawdzam…";
        if (await send("settings/key", { key: keyInput.value }, keyError, saveKey)) keyInput.value = "";
        saveKey.textContent = "Sprawdź i zapisz klucz";
      },
    },
      el("label", { for: "api-key" }, "Klucz"),
      el("div", { class: "row" }, keyInput, saveKey),
      keyError),

    el("h3", { class: "form-section" }, "Modele"),
    el("p", { class: "hint" }, "Modele są pytane po kolei, aż któryś da poprawną odpowiedź: model przeciążony " +
      "albo taki, na który zabrakło środków, jest pomijany."),
    el("label", { class: "toggle", for: "use-paid" }, usePaid,
      el("span", {}, el("b", {}, "Używaj płatnych modeli"),
        el("span", { class: "option-hint" }, "Szybsze i dokładniejsze; ich dostawcy zwykle nie trenują na " +
          "przesłanych danych (można to wymusić w ",
          el("a", { href: "https://openrouter.ai/settings/privacy", target: "_blank", rel: "noopener" },
            "ustawieniach prywatności OpenRoutera"),
          "). Odczyt jednego CV kosztuje ułamek centa."))),
    paid.node,
    el("label", { class: "toggle", for: "use-free" }, useFree,
      el("span", {}, el("b", {}, "Używaj darmowych modeli"),
        el("span", { class: "option-hint" }, "Także gdy płatne nie odpowiedzą. Wolniejsze, często przeciążone."))),
    el("p", { class: "privacy-warning" }, "⚠ ", el("b", {}, "Uwaga na prywatność: "),
      "dostawcy darmowych modeli mogą zapisywać przesłane im dane i używać ich do trenowania modeli. " +
      "Trafi do nich treść twoich CV – dane osobowe, kontakt i historia zatrudnienia. Gdy darmowe są " +
      "wyłączone, CV nie zostanie im wysłane nigdy, nawet jeśli płatne zawiodą."),
    free.node, lastNote,
    noneNote,
    el("label", { class: "reviews-row", for: "daily-reviews" }, el("b", {}, "Ocen ofert dziennie"),
      el("span", { class: "option-hint" }, "Ile najbardziej obiecujących ofert model ocenia każdego dnia " +
        "(ocena jednej to ułamek centa). Oferty, które ocenić każesz sam, nie wliczają się."),
      dailyInput),
    modelsError,
    el("div", { class: "row end" }, modelsNote,
      el("button", {
        class: "btn", type: "button", onclick: async () => {
          if (await send("settings/models", { reset: true }, modelsError)) modelsNote.textContent = "Przywrócono domyślne.";
        },
      }, "Przywróć domyślne"),
      saveModels),

    el("h3", { class: "form-section" }, "Skrzynka z alertami o ofertach"),
    el("p", { class: "hint" }, "pracuj.pl i LinkedIn wysyłają powiadomienia o nowych ofertach mailem. Przekazuj je " +
      "filtrem ze swojej skrzynki na osobną, założoną tylko dla tej aplikacji, i podaj ją tutaj – aplikacja nie " +
      "dostanie dostępu do twojej prywatnej poczty. W Gmailu potrzebne jest hasło do aplikacji (",
      el("a", { href: "https://myaccount.google.com/apppasswords", target: "_blank", rel: "noopener" },
        "myaccount.google.com/apppasswords"),
      ", przy włączonej weryfikacji dwuetapowej). Hasło jest przechowywane na serwerze zaszyfrowane; skrzynka " +
      "jest tylko czytana."),
    mailState,
    el("form", { onsubmit: saveMailbox },
      el("div", { class: "two mail-fields" },
        el("div", {}, el("label", { for: "mail-address" }, "Adres skrzynki"), mailAddress),
        el("div", {}, el("label", { for: "mail-password" }, "Hasło (do aplikacji)"), mailPassword)),
      el("details", { class: "mail-more" }, el("summary", {}, "Serwer i folder"),
        el("div", { class: "two mail-fields" },
          el("div", {}, el("label", { for: "mail-host" }, "Serwer IMAP"), mailHost),
          el("div", {}, el("label", { for: "mail-folder" }, "Folder"), mailFolder))),
      el("div", { class: "row end" }, saveMail),
      mailError));
  dialog.addEventListener("close", () => dialog.remove());
  renderKey();
  renderSwitch();
  renderModelLists();
  renderMailbox();
  document.body.append(dialog);
  dialog.showModal();
}
