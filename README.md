# CV Aggregator – job offers that fit a candidate

A web app that gathers everything a candidate's CVs say into one profile,
and searches popular job portals for offers that fit it and what the
candidate is looking for.

Users log in and upload up to six CVs (PDF, DOCX, ODT, DOC, RTF or
TXT). A language model reads each into its parts – jobs, education,
skills, languages, courses and certificates, projects, publications,
awards, interests – and the app merges them into one profile: CVs tailored
to different offers complement each other, and newer ones update older
ones. The user can correct the profile, add to it and write about
themselves; a model then describes the candidate: a summary, strengths,
weaknesses and possible career paths, with the roles and keywords to look
for.

On the offers page ("Oferty") they say what they look for: roles,
keywords, cities and ways of working, the least pay, contracts, levels,
words to avoid, portals, and in their own words. The app searches
[JustJoin.it](https://justjoin.it), [RocketJobs](https://rocketjobs.pl),
[No Fluff Jobs](https://nofluffjobs.com) and
[Bulldogjob](https://bulldogjob.pl) every few hours, keeps the offers that
may fit, and a model rates the most promising (0–100, with what fits and
what's missing), as many a day as the user allows. Offers can be marked as
interesting, applied for or rejected. (pracuj.pl and theprotocol.it turn
programs away, and LinkedIn has no public job search; see "Portals".)

Models think; the program does whatever it can do itself (see "How a
profile is made"). Only the standard library is needed, plus `pdftotext`
(poppler-utils) for PDFs and, for DOC and RTF only, LibreOffice. Runs on
Python 3.8+. The models are [OpenRouter](https://openrouter.ai)'s, with
every user's own key (given in the page's settings), or any other
OpenAI-compatible API's.
Each user chooses whether paid models may be used (on until they choose)
and whether free ones may (off until they choose: their providers may keep
what they're sent, the CVs, so with them off no CV ever goes to a free
model, even when the paid ones fail).

## Running

```bash
sudo apt install poppler-utils     # pdftotext
cp .env.example .env               # all of it is optional
python3 app.py                     # http://<host>:8075
```

Settings (`.env`, see [.env.example](.env.example)):

| Setting | Default | |
|---|---|---|
| `OPENROUTER_API_KEY` | | not used by requests (every user gives their own key in the page's settings); `app.py --set-key USER` hands this one to a user |
| `OPENROUTER_MODEL` | `openrouter/free` | the last model on the ladder of everyone using free models, which users can't remove; `openrouter/free` picks some free one per request |
| `OPENROUTER_MODELS` | | the default list of free models for users who haven't set their own, comma-separated, one attempt each before `OPENROUTER_MODEL`; a model that's down fails fast and one that's rate-limited is asked once more after a few seconds, then the next one is tried; a model still rate-limited then, or one that didn't answer in time, is left out for three minutes, and one that refused (a paid model when the funds have run out, a model that's gone) for an hour |
| `OPENROUTER_PAID_MODELS` | | the default list of paid models, tried before the free ones for users who want them; `~…` is OpenRouter's alias for the latest model of a family (e.g. `~anthropic/claude-haiku-latest`); Anthropic's models get the system prompt marked for caching |
| `OPENROUTER_PAID_DEFAULT` | 1 | whether users get the paid models until they choose |
| `OPENROUTER_FREE_DEFAULT` | 0 | whether users get the free models (`OPENROUTER_MODELS`, `OPENROUTER_MODEL`) until they choose; without them, the first paid model is asked `OPENROUTER_ATTEMPTS` times in all instead of `OPENROUTER_MODEL` |
| `OPENROUTER_LOW_FUNDS` | 0.5 | dollars left on a key below which the settings warn |
| `OPENROUTER_ATTEMPTS` | 3 | attempts with `OPENROUTER_MODEL` (or, with free models off, the first paid one) before a request fails |
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
| `PORTALS` | all there are | keeps only these portals (their ids, comma-separated) |
| `PORTALS_DIR` | `data/portals` | more portals, a JSON file each, beside those in `portals/` (one of the same id replaces it); see "Portals" |
| `OFFERS_INTERVAL` | 6 | hours between searches for a user (a portal searched for a term less than this ago, for anyone, isn't asked again) |
| `PORTAL_DELAY` | 3 | seconds at least between two requests to the same portal |
| `OFFERS_DAILY_REVIEWS` | 30 | offers a model rates for a user a day, until they set their own number in the settings |
| `OFFERS_MAX_REVIEWS` | 200 | the most a user may set |
| `OFFER_DAYS` | 7 | an offer not seen on its portal for this long is taken as gone (and forgotten after twice that, unless marked) |
| `OFFER_MAX_CHARS` | 12000 | an offer's text is cut to this many characters for the model |
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
python3 app.py --set-key USER         # gives USER an OpenRouter key: asked for,
                                      # read from stdin, or .env's when empty
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

## How offers are found

[offers.py](offers.py) runs in the background; for each user whose offers
weren't looked for in `OFFERS_INTERVAL` hours, or who asked ("Szukaj
teraz", or saving "Czego szukam"):

1. **Searching** ([portals.py](portals.py)): every chosen portal is
   searched for every term – each of the user's keywords and roles, or,
   until they write any, those of the analysis' best career paths (a
   term takes a request on each portal, `PORTAL_DELAY` seconds apart). Results are shared
   by all users: a portal searched for a term lately isn't asked again.
2. **Sifting** ([matching.py](matching.py), no model): an offer is ruled
   out by a contract the user didn't choose or a word to avoid in its
   title or company. Other wishes it doesn't meet cost points instead
   (unless the user wants them to rule out too): a place farther than the
   user's distance (30 km unless they set another; 0: the very cities)
   from their cities – by the coordinates the portal gives or, without
   them, its cities' (from OpenStreetMap's Nominatim, [geo.py](geo.py), a
   request a second at most, each place asked about once) – 5 for a
   remote job, 20 for a hybrid one (or one that doesn't say), 40 for one
   in the office; a way of working not chosen 5 (remote), 15 (hybrid), 25
   (office); a level next to the ones sought 10, one farther 25; no pay
   given when the user wants it given 10; pay whose top is below the
   user's least a point a percent short (other currencies roughly in
   złoty), 40 at most.
   What a portal doesn't say passes. The rest get a score 0..1: the title
   against the roles sought (the share of their words it has, whatever
   the inflection), the share of the offer's skills the candidate has,
   keywords in the title, less the points. Promising ones are kept for
   the user, once even when two portals list the same job; ones kept
   earlier and not marked are judged again by the wishes as they are,
   rated ones too.
3. **Rating** ([agents.py](agents.py), a model): the most promising not
   rated since the user last changed what they look for, with their full
   text from the portal, get a score 0–100, a summary, what fits and what
   doesn't – up to the user's number a day (counted anew when they change
   what they look for), plus any they asked for ("Oceń", "Oceń 10
   kolejnych"). The model judges how the offer fits the candidate and
   what they want to do, not place, pay or contract: the program takes
   the points for unmet wishes off its score, and the offer shows which
   and how many. Offers scoring below 50 are shown only on request
   ("Pokaż słabiej dopasowane oferty"). The candidate's profile is the system prompt, the
   same for all their offers, so models that cache read it for a tenth.

## What to learn

The profile page's "Kierunki rozwoju" ([market.py](market.py), no model):
the skills the offers kept for the user ask for, each offer weighing as
much as it fits (its score less unmet wishes, or its prescore until
rated; skills only wished for count half), split into the ones the
candidate lacks (asked by two offers at least) and the ones they have.
Forms of one skill count as one ("NI TestStand" as "TestStand"), tags
that name no skill ("Testing", "Software") are left out, the candidate's
languages count as theirs. A model then writes 3–5 steps to take
(`agents.advise`), from those numbers and what it found missing in the
best offers – on request, and once a day after offers are rated.

## Portals

Every portal is a JSON file in [portals/](portals/) – or in `PORTALS_DIR`
(`data/portals/` by default, kept on the server, not in the repository),
where a file adds a portal or replaces the shipped one of the same `id`.
A portal marked `"default": true` is searched for users who choose none;
the others only for those who tick them in "Czego szukam". Users get the
offers of their portals only.

Portals are read as their sites allow – their APIs, or pages as a browser
gets them, nothing `robots.txt` disallows – by a client that says who it
is (`CVAggregator/0.1 (...)`), a request every `PORTAL_DELAY` seconds (or
the portal's `delay`) at most:

| Portal | Country | How | List | An offer |
|---|---|---|---|---|
| JustJoin.it, RocketJobs (one platform) | PL | parser `justjoin` | `?keyword=` search; 50 offers with company, pay, contracts, level, skills, in the page's Next.js flight data | JSON-LD `JobPosting` |
| No Fluff Jobs | PL | parser `nofluffjobs` | `?criteria=keyword=` search; 20 offers with company, pay, level, skills, in the page's state | the page's state: requirements, description, tasks |
| Bulldogjob | PL | parser `bulldogjob` | `/s/skills,<term>`: by skill only (a term that isn't one of its skills finds nothing); 50 offers in `__NEXT_DATA__` | JSON-LD `JobPosting` |
| Bundesagentur für Arbeit (Jobsuche) | DE | described | its public API (v6), 50 offers with place, coordinates, pay | the API's job details |
| Arbeitnow | DE | described | its free public API: the 300 latest offers, with their text, kept if they have every word of the term (it can't be searched) | in the list |

Sites that need code have a parser in [sites.py](sites.py); the file gives
it what it takes (e.g. the site's addresses, so that RocketJobs is
JustJoin.it's parser at other addresses). Portals whose lists come as JSON
– an API, or JSON in the page – are described in the file alone, with no
code (see [portals.py](portals.py) for every option):

```json
{
  "id": "arbeitsagentur", "name": "Bundesagentur für Arbeit",
  "country": "DE", "site": "https://www.arbeitsagentur.de/jobsuche/",
  "default": false, "delay": 2,
  "search": {
    "url": "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs",
    "params": {"was": "{term}", "size": "50"},
    "headers": {"X-API-Key": "jobboerse-jobsuche"},
    "format": "json",
    "items": "ergebnisliste[]"
  },
  "fields": {
    "key": "referenznummer",
    "url": {"template": "https://www.arbeitsagentur.de/jobsuche/jobdetail/{referenznummer}"},
    "title": "stellenangebotsTitel",
    "company": "firma",
    "cities": "stellenlokationen[].adresse.ort",
    "points": {"pairs": ["stellenlokationen[].breite", "stellenlokationen[].laenge"]},
    "salary_min": "gehaltsspanneVon",
    "salary_max": "gehaltsspanneBis",
    "period": {"path": "verguetungsangabe", "map": {"JAHRESGEHALT": "year", "MONATSGEHALT": "month"}},
    "currency": {"value": "EUR"},
    "posted": "datumErsteVeroeffentlichung"
  },
  "details": {
    "url": "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/{key|base64}",
    "headers": {"X-API-Key": "jobboerse-jobsuche"},
    "text": ["stellenangebotsBeschreibung"]
  }
}
```

- `search`: the list's address and query (`{term}` is the search term),
  headers, where its JSON is (`format`: `json` – the answer is JSON,
  `next_data` – Next.js' `__NEXT_DATA__`, `script:<id>` – the JSON in a
  `<script id>`, `json_ld`), the path to its offers (`items`), and
  `"filter": true` for a portal that can't be searched (its offers are
  kept if they have every word of the term).
- `fields`: for each of an offer's fields (`key`, `url`, `title`,
  `company`, `cities`, `mode`, `salary_min`, `salary_max`, `period`,
  `currency`, `contracts`, `level`, `skills`, `nice`, `posted`, `points`,
  `text`), a path in an item (`a.b`, `a[].b` for each of a list), or
  `{"path"}`, `{"template": "…{path}…"}`, `{"value": …}` or `{"pairs":
  [lat path, lon path]}`, with `"split"` (a separator) and `"map"`
  ({portal's value: ours}; values not in it are dropped) if needed. Ours
  are `remote`/`hybrid`/`office`, `uop`/`b2b`/`other`,
  `intern`/`junior`/`mid`/`senior`/`lead`, a `period` of
  `hour`/`day`/`month`/`year` (pay is made a month's).
- `details` (optional): where an offer's text is (an address template;
  `{key|base64}`, `{…|url}` encode) and the paths to it; without it the
  offer's page is read for JSON-LD `JobPosting` – or the list's `text` is
  used, if it has it.

A file that can't be read, or has neither `parser` nor `search`, is left
out with an error in the log.

Not searched: pracuj.pl (and theprotocol.it) answer programs with a
Cloudflare challenge (HTTP 403), which isn't to be got round; LinkedIn has
no public job search API (its Job Posting API is for partners only) and
its terms forbid scraping. Their offers come from the user's browser
instead (see below); their files in portals/ have only `import`.

### Offers sent from the browser

The offers page gives a bookmarklet, "→ CV Aggregator", to drag to the
bookmarks bar ([web/importer.js](web/importer.js)). Clicked on a page, it
scrolls the list of offers from top to bottom and back, so that a portal
drawing tiles as they come into view (LinkedIn) draws them all, then
takes what the browser shows – the page's text, its JSON-LD
`JobPosting`s, and for each offer (a link matching a portal's
`import.link`, or an element with one of its `import.ids` attributes)
its tile: the largest element around the link with no other offer, or
the element with the id – packs it (gzip, base64url) and opens the app with it in the
address' fragment, which reaches no server but the app's own page; that
page posts it to `/api/offers/import` and shows what comes of it.
[importer.py](importer.py), on the offers thread, then:

- on a results page, has a model read the tiles (`agents.read_cards`);
  the offers are sifted by the user's wishes like any others – the ones
  ruled out are left out – and the 20 most promising rated at once, from
  their tiles alone (the offer says so);
- on an offer's page (its address is an offer's, or, on a site no
  portal describes, it has a `JobPosting`) – its "similar offers" left
  out – or for the offer LinkedIn shows beside its list (`currentJobId`),
  has a model read the offer with its full text (`agents.read_page`) and
  rates it at once, whatever it misses.

Neither counts in the day's number of ratings. It works on other sites'
offer pages too.

## Data

`data/cv.db` (SQLite) and `data/cvs/<user id>/` (the uploaded files):

| Table | Holds |
|---|---|
| `users` | username and PBKDF2-SHA256 password hash; the user's own OpenRouter key, encrypted, their lists of paid and free models and whether each may be used |
| `logins` | login sessions (a 30-day cookie, stored as its SHA-256) |
| `cvs` | a CV: file name, kind, size, SHA-256 (the same file can't be uploaded twice), its text and language, what the model read from it (JSON), status (`pending`, `working`, `done`, `failed`) and error, which model read it |
| `profiles` | the user's own description, what they look for (`wishes`, JSON, and `goals`, their words), their changes to the profile (JSON), the analysis (JSON) with its status, model, time and the fingerprint of the profile it was written from, when their offers were last looked for and whether they asked for it |
| `offers` | offers found, shared by all users: portal, its id, URL, what the list said (JSON), first and last seen, the full text once fetched |
| `searches` | when a portal was last searched for a term, how many offers it gave or what went wrong |
| `places` | a town's coordinates (from Nominatim; none if not found) |
| `imports` | a page sent from the browser: its address, what it showed (until read), status, what came of it or what went wrong |
| `matches` | an offer kept for a user: the program's score, the wishes it doesn't meet and their points, the user's mark (`new`, `interested`, `applied`, `rejected`), whether they asked for a rating, the model's score and review (JSON), who wrote it and when |

### API

| Request | |
|---|---|
| `POST /api/register`, `POST /api/login` | `{username, password}`: logs in (cookie) |
| `POST /api/logout` | |
| `GET /api/me` | the user, whether there's a key for them and whether any models are on |
| `GET /api/schema` | the profile's fields and sections, whether accounts can be made |
| `GET /api/settings` | whether the user has their own key (and its last characters), whether the server has one, funds, lists of models, whether paid and free ones may be used, the defaults |
| `POST /api/settings/key` | `{key}`: checked with OpenRouter, then kept; empty removes it |
| `POST /api/settings/models` | `{paid, free}`: the user's own lists; `{reset: true}`: the defaults |
| `POST /api/settings/use` | `{use_paid, use_free}` (either): whether paid, free models may be used |
| `POST /api/settings/reviews` | `{daily}`: offers rated a day |
| `GET /api/models` | OpenRouter's models with their prices |
| `GET /api/profile` | CVs, the merged profile, what was counted, the user's description, the analysis (and whether it's out of date), whether anything is being worked on |
| `POST /api/profile/about` | `{about}` |
| `POST /api/profile/personal` | `{field: value}`; `null` goes back to the CVs' |
| `POST /api/profile/items/<section>` | `{id, fields}`: the user's version of an item; no `id`: a new one |
| `POST /api/profile/items/<section>/<id>/delete` | |
| `POST /api/profile/items/<section>/<id>/revert` | the CVs' version of an edited item |
| `POST /api/profile/removed/<section>/<id>/restore` | |
| `POST /api/profile/analysis` | has the analysis written again |
| `POST /api/cvs?name=<file name>` | the file as the body: uploads a CV |
| `GET /api/cvs/<id>/file`, `GET /api/cvs/<id>/text` | the file, the text read from it |
| `POST /api/cvs/<id>/delete`, `POST /api/cvs/<id>/retry` | |
| `GET /api/offers` | what the user looks for and the form's choices, the terms searched, what's going on, the day's ratings, the offers kept with their reviews |
| `POST /api/offers/wishes` | `{wishes, goals}`; offers are looked for again |
| `POST /api/offers/search` | looks for offers now |
| `POST /api/offers/more` | has the next 10 offers rated (not counted in the day's number) |
| `POST /api/offers/import` | `{url, title, text, ld, cards}`: a page sent from the browser; returns its `{id}` |
| `GET /api/offers/import/<id>` | as `GET /api/offers`, with `import` (status, result), its offers only |
| `POST /api/offers/<id>/status` | `{status}`: `new`, `interested`, `applied`, `rejected` |
| `POST /api/offers/<id>/review` | has the offer rated now (not counted in the day's number) |

Every `POST` on the profile and on CVs answers with the profile, as
`GET /api/profile` does; on offers, as `GET /api/offers` does.

## Licence

GPL v3 ([LICENSE](LICENSE)).
