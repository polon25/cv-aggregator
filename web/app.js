"use strict";
// The app's page: logging in and the user's profile (profile.js). Talks to
// the JSON API in web_server.py.

const app = document.getElementById("app");
const account = document.getElementById("account");
let schema = null;
let me = null;
let view = null; // the open page with something to stop: ProfileView

// --- helpers ------------------------------------------------------------

async function api(method, path, body, signal) {
  const options = { method, headers: {}, signal };
  if (body instanceof Blob) {
    options.headers["Content-Type"] = "application/octet-stream";
    options.body = body;
  } else if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch("api/" + path, options);
  } catch (e) {
    throw Object.assign(new Error("Brak połączenia z serwerem."), { status: 0 });
  }
  let data = {};
  try { data = await response.json(); } catch (e) { /* not JSON */ }
  if (!response.ok) {
    throw Object.assign(new Error(data.error || "Błąd " + response.status),
                        { status: response.status });
  }
  return data;
}

// el("p", {class: "x", onclick: f}, "text", child, [more, children])
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else if (key in node && typeof value !== "string") node[key] = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

// node's children replaced, leaving out null, undefined and false (as el()).
function fill(node, ...children) {
  node.replaceChildren(...children.flat(Infinity).filter(
    (c) => c !== null && c !== undefined && c !== false));
}

function show(...nodes) {
  fill(app, ...nodes);
}

const fmtTime = (epoch) => new Date(epoch * 1000).toLocaleString("pl-PL", {
  day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
});

// "2019-03" -> "03.2019", "2019" stays
const fmtDate = (date) => (date && date.length > 4 ? date.slice(5, 7) + "." + date.slice(0, 4) : date || "");

function fmtSpan(item) {
  const start = fmtDate(item.start);
  const end = item.current ? "obecnie" : fmtDate(item.end);
  if (start && end && end !== start) return start + " – " + end;
  return start || end;
}

function renderAccount() {
  if (!me) return account.replaceChildren();
  account.replaceChildren(
    el("span", {}, me.user.username),
    el("button", {
      type: "button", onclick: settingsDialog,
      title: me.has_key ? null : "Podaj klucz API OpenRoutera, żeby CV mogły być odczytywane",
    }, me.has_key ? "Ustawienia" : "⚠ Ustawienia"),
    el("button", { type: "button", onclick: logout }, "Wyloguj"));
}

async function logout() {
  try { await api("POST", "logout"); } catch (e) { /* logged out anyway */ }
  me = null;
  renderAccount();
  route();
}

function showLogin(mode) {
  const register = mode === "register" && schema.registration;
  const error = el("p", { class: "error", role: "alert" });
  const username = el("input", {
    type: "text", id: "username", autocomplete: "username", maxlength: "32",
    autocapitalize: "none", spellcheck: "false", required: true,
  });
  const password = el("input", {
    type: "password", id: "password", required: true,
    autocomplete: register ? "new-password" : "current-password",
  });
  const password2 = register && el("input", {
    type: "password", id: "password2", autocomplete: "new-password", required: true,
  });
  const submit = el("button", { class: "btn primary big", type: "submit" },
                    register ? "Załóż konto" : "Zaloguj się");

  async function onSubmit(event) {
    event.preventDefault();
    error.textContent = "";
    if (register && password.value !== password2.value) {
      error.textContent = "Hasła się różnią.";
      return;
    }
    submit.disabled = true;
    try {
      await api("POST", register ? "register" : "login",
                { username: username.value, password: password.value });
      await loadMe();
    } catch (e) {
      error.textContent = e.message;
      submit.disabled = false;
    }
  }

  const tab = (name, label) => el("button", {
    type: "button", class: (register ? "register" : "login") === name ? "active" : "",
    onclick: () => showLogin(name),
  }, label);

  show(el("section", { class: "panel auth" },
    schema.registration && el("div", { class: "tabs" }, tab("login", "Logowanie"), tab("register", "Nowe konto")),
    el("p", { class: "muted" }, register
      ? "Wystarczy nazwa użytkownika i hasło."
      : "Zaloguj się, żeby zebrać swoje CV w jeden profil i znaleźć pasujące oferty."),
    el("form", { onsubmit: onSubmit },
      el("label", { for: "username" }, "Nazwa użytkownika"), username,
      register && el("p", { class: "hint" }, "3–32 znaki: litery, cyfry, kropka, myślnik, podkreślnik."),
      el("label", { for: "password" }, "Hasło"), password,
      register && el("label", { for: "password2" }, "Powtórz hasło"), password2,
      register && el("p", { class: "hint" }, "Co najmniej 6 znaków."),
      submit, error)));
  username.focus();
}

// --- start --------------------------------------------------------------

async function start() {
  window.addEventListener("hashchange", route);
  try {
    schema = await api("GET", "schema");
    await loadMe();
  } catch (e) {
    show(el("p", { class: "loading" }, e.message));
  }
}

async function loadMe() {
  try {
    me = await api("GET", "me");
  } catch (e) {
    if (e.status !== 401) throw e;
    me = null;
  }
  renderAccount();
  route();
}

function route() {
  if (view) view.stop();
  view = null;
  if (!me) return showLogin("login");
  view = new ProfileView();
  view.start();
}

start();
