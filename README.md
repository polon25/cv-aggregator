# CV Aggregator – a candidate profile from all their CVs

A web app that gathers everything a candidate's CVs say into one profile,
and (next) checks job offers on popular job portals for ones that fit it.

For now: users log in and upload up to six CVs (PDF, DOCX, ODT, DOC, RTF or
TXT). A language model reads each into its parts – jobs, education,
skills, languages, courses and certificates, projects, publications,
awards, interests – and the app merges them into one profile: CVs tailored
to different offers complement each other, and newer ones update older
ones. The user can correct the profile, add to it and write about
themselves and what they look for; a model then describes the candidate:
a summary, strengths, weaknesses and possible career paths, with the roles
and keywords to look for.

Models think; the program does whatever it can do itself (see "How a
profile is made"). Only the standard library is needed, plus `pdftotext`
(poppler-utils) for PDFs and, for DOC and RTF only, LibreOffice. Runs on
Python 3.8+. The models are [OpenRouter](https://openrouter.ai)'s (the
server's key, or the user's own), or any other OpenAI-compatible API's.

## Running

```bash
sudo apt install poppler-utils     # pdftotext
cp .env.example .env               # and put the OpenRouter key in it
python3 app.py                     # http://<host>:8075
```

Settings (`.env`, see [.env.example](.env.example)):

| Setting | Default | |
|---|---|---|
| `OPENROUTER_API_KEY` | | the key used for users who haven't given their own in the page's settings; empty: everyone must give one |
| `OPENROUTER_MODEL` | `openrouter/free` | the last model on everyone's ladder, which users can't remove; `openrouter/free` picks some free one per request |
| `OPENROUTER_MODELS` | | the default list of free models for users who haven't set their own, comma-separated, one attempt each before `OPENROUTER_MODEL`; a model that's down fails fast and one that's rate-limited is asked once more after a few seconds, then the next one is tried; a model still rate-limited then, or one that didn't answer in time, is left out for three minutes, and one that refused (a paid model when the funds have run out, a model that's gone) for an hour |
| `OPENROUTER_PAID_MODELS` | | the default list of paid models, tried before the free ones for users who want them; `~…` is OpenRouter's alias for the latest model of a family (e.g. `~anthropic/claude-haiku-latest`); Anthropic's models get the system prompt marked for caching |
| `OPENROUTER_PAID_DEFAULT` | 1 | whether users get the paid models first until they choose |
| `OPENROUTER_LOW_FUNDS` | 0.5 | dollars left on a key below which the settings warn |
| `OPENROUTER_ATTEMPTS` | 3 | attempts with `OPENROUTER_MODEL` before a request fails |
| `OPENROUTER_TIMEOUT` | 180 | seconds one request may take |
| `OPENROUTER_TEMPERATURE` | | the model's default when empty |
| `OPENROUTER_URL` | OpenRouter's | any OpenAI-compatible chat completions endpoint |
| `SECRET_FILE` | `secret.key` | the secret users' own keys are encrypted with in the database (made on first use; keep it, and keep it out of backups of `data/`) |
| `WEB_HOST`, `WEB_PORT` | `0.0.0.0`, 8075 | where the page listens |
| `ALLOW_REGISTRATION` | 1 | 0: accounts are made only with `app.py --add-user` |
| `DATA_DIR` | `./data` | the database and the uploaded files |
| `MAX_CVS` | 6 | CVs a user may keep |
| `MAX_CV_MB` | 5 | largest file accepted |
| `MAX_CV_CHARS` | 40000 | a CV's text is cut to this many characters for the model |
| `WORKERS` | 2 | threads reading CVs and writing analyses |
| `LOG_LEVEL` | `INFO` | |

`.env`, `secret.key` and `data/` stay on the server: they aren't in the
repository.

### Running at boot (systemd)

`systemd/cv-aggregator.service`; install it from the project directory,
filling in its path and the user to run as:

```bash
sed "s|/opt/cv-aggregator|$PWD|g; s|^User=cv-aggregator|User=$USER|" \
  systemd/cv-aggregator.service | sudo tee /etc/systemd/system/cv-aggregator.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now cv-aggregator.service
```

Accounts and keys from the command line:

```bash
python3 app.py --add-user USER        # also with ALLOW_REGISTRATION=0
python3 app.py --set-password USER    # asks for it (or reads it from stdin)
python3 app.py --set-key USER         # the user's own OpenRouter key; empty:
                                      # back to the server's
```

Logs: `journalctl -u cv-aggregator -f`; every model request is logged with
the model that answered, how long it took and why an answer was rejected.

## How a profile is made

1. **The text** ([documents.py](documents.py), no model): `pdftotext
   -layout` for PDFs (it keeps a line's dates beside what they date), the
   XML of DOCX and ODT, LibreOffice for DOC and RTF. Page numbers, lines
   repeated on every page and the clause consenting to data processing
   are taken out; the CV's language is guessed; e-mail addresses, phone
   numbers and links are found.
2. **Reading** ([agents.py](agents.py), a model): the text becomes JSON
   in the profile's schema ([candidate.py](candidate.py)), in Polish
   whatever the CV's language. The model is given what the profile holds
   from the user's other CVs and told to name the same things the same
   way, so that a Polish and an English CV can be matched. What it wrote
   is checked and fitted to the schema: dates made `YYYY-MM`, an e-mail,
   phone or link kept only if the CV's text has it. A user's CVs are read
   one at a time.
3. **Merging** ([candidate.py](candidate.py), no model): items of
   different CVs that describe the same thing become one – jobs and
   schools by the employer or school and agreeing dates, skills,
   languages and interests by name (`C++` isn't `C`, `English` is
   `angielski`), courses, projects and publications by alike names. Each
   field comes from the newest CV that has it; achievements are joined,
   each said once. Every item shows which CVs it's from.
4. **Counting** (no model): years of work (overlapping jobs counted
   once), the first job, the current ones, the highest degree.
5. **The analysis** (a model): the merged profile as text, with what was
   counted and the user's own words, becomes a summary, strengths,
   weaknesses and career paths (how well each fits, why, what's missing,
   roles and search keywords). It's written again once newly uploaded
   CVs are read or a CV is deleted, and on request; the page says when
   the profile has changed since.

The user's changes are kept apart from what the CVs say, so that the
profile can be merged anew whenever a CV comes or goes:

- an edited item replaces the CVs' items it was made of, as the user
  wrote it ("↺" brings the CVs' version back); items of CVs uploaded
  later that match it join it;
- a deleted item stays out, also when a later CV has it again, until
  it's restored from the section's "Usunięte";
- personal details the user changed win over the CVs' ("przywróć z CV"
  goes back).

## Data

`data/cv.db` (SQLite) and `data/cvs/<user id>/` (the uploaded files):

| Table | Holds |
|---|---|
| `users` | username and PBKDF2-SHA256 password hash; the user's own OpenRouter key, encrypted, their lists of paid and free models and whether the paid ones come first |
| `logins` | login sessions (a 30-day cookie, stored as its SHA-256) |
| `cvs` | a CV: file name, kind, size, SHA-256 (the same file can't be uploaded twice), its text and language, what the model read from it (JSON), status (`pending`, `working`, `done`, `failed`) and error, which model read it |
| `profiles` | the user's own description and what they look for, their changes to the profile (JSON), the analysis (JSON) with its status, model, time and the fingerprint of the profile it was written from |

### API

| Request | |
|---|---|
| `POST /api/register`, `POST /api/login` | `{username, password}`: logs in (cookie) |
| `POST /api/logout` | |
| `GET /api/me` | the user, whether there's a key for them |
| `GET /api/schema` | the profile's fields and sections, whether accounts can be made |
| `GET /api/settings` | whether the user has their own key (and its last characters), whether the server has one, funds, lists of models, whether paid ones come first, the defaults |
| `POST /api/settings/key` | `{key}`: checked with OpenRouter, then kept; empty removes it |
| `POST /api/settings/models` | `{paid, free}`: the user's own lists; `{reset: true}`: the defaults |
| `POST /api/settings/paid` | `{use_paid}` |
| `GET /api/models` | OpenRouter's models with their prices |
| `GET /api/profile` | CVs, the merged profile, what was counted, the user's description, the analysis (and whether it's out of date), whether anything is being worked on |
| `POST /api/profile/about` | `{about, goals}` |
| `POST /api/profile/personal` | `{field: value}`; `null` goes back to the CVs' |
| `POST /api/profile/items/<section>` | `{id, fields}`: the user's version of an item; no `id`: a new one |
| `POST /api/profile/items/<section>/<id>/delete` | |
| `POST /api/profile/items/<section>/<id>/revert` | the CVs' version of an edited item |
| `POST /api/profile/removed/<section>/<id>/restore` | |
| `POST /api/profile/analysis` | has the analysis written again |
| `POST /api/cvs?name=<file name>` | the file as the body: uploads a CV |
| `GET /api/cvs/<id>/file`, `GET /api/cvs/<id>/text` | the file, the text read from it |
| `POST /api/cvs/<id>/delete`, `POST /api/cvs/<id>/retry` | |

Every `POST` on the profile and on CVs answers with the profile, as
`GET /api/profile` does.

## Licence

GPL v3 ([LICENSE](LICENSE)).
