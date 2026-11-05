# Aurora Studio site fixture

Bring-your-own-website fixture shared by five independent feature tasks:

| Task | Prompt | Test |
|---|---|---|
| `feat-02-dark-mode.yaml` | Add a dark/light theme toggle, on every page | `tests/dark-mode.test.js` |
| `feat-03-search-filter.yaml` | Add live search/filter to the services page | `tests/search.test.js` |
| `feat-04-favorites.yaml` | Add a persistent favorites toggle per card | `tests/favorites.test.js` |
| `feat-05-form-validation.yaml` | Add client-side validation to the contact form | `tests/form-validation.test.js` |
| `feat-06-accessible-nav.yaml` | Make the mobile nav keyboard-accessible (focus trap) | `tests/accessible-nav.test.js` |

Each task's `success:` command runs only its own `tests/*.test.js` file, so
they don't interfere with each other even though they all live in the same
fixture and `tests/` directory - a harness working on FEAT-03 is graded only
on `search.test.js`, regardless of what state the other features are in.
`npm test` runs everything together (useful for checking the whole site after
replacing it with your own), but no single task's grading depends on that.

**All of `index.html`/`about.html`/`services.html`/`contact.html`,
`css/style.css`, and `js/app.js` are a throwaway placeholder** - a small
four-page static site ("Aurora Studio": home, about, services, contact) with
a shared nav/footer repeated on every page, some real layout (hero, card
grid, team grid, a contact form), and a bit of non-theme JS (mobile nav
toggle, dynamic footer year, a fake form submit handler). Replace it with
your own vibecoded site before running the real benchmark - any structure is
fine (more pages, fewer pages, totally different content), this is
deliberately not prescriptive about how the site is built. If you do replace
it, keep the stable hooks the other tasks' tests rely on if you want to keep
using them: the three service cards' `data-id` attributes (`new-site`,
`redesign`, `maintenance`) and the contact form's `#name`/`#email`/`#message`
field ids.

All five tests are black-box: they don't care how your site implements the
behavior internally (framework, file layout, build step - though note the
harness itself currently targets a **plain static site**, no build step),
only the observable contract each task's prompt spells out. Pages are loaded
and their actual `<script src="...">` files run via jsdom (a lightweight DOM
emulator, not a real browser - same `node --test` + no-heavy-deps style as
the sibling `mini-web` fixture), so no Playwright/Chromium install is needed.

## feat-02-dark-mode

1. A clickable element with `id="theme-toggle"` on **every page of the
   site**, not just the homepage.
2. Clicking it sets `document.documentElement.dataset.theme` to `"dark"` or
   `"light"` (i.e. a `data-theme="dark"`/`"light"` attribute on `<html>`),
   flipping each click.
3. The chosen theme is written to `localStorage` under the key `"theme"`.
4. On a fresh page load, a previously-stored theme is applied immediately -
   real persistence, not just a value nobody reads back.

`dark-mode.test.js` auto-discovers every `*.html` file in this directory and
runs the same four checks against each one, so a harness that only adds dark
mode to `index.html` and forgets the rest of the site will fail.

## feat-03-search-filter

An `#service-search` input on `services.html` that hides (`card.hidden =
true`) any `.card` in `#service-cards` whose text doesn't match the typed
query, case-insensitively; clearing the query shows every card again.

## feat-04-favorites

A `.favorite-toggle` button inside each service card, toggling that card's
`data-id` in `localStorage["favorites"]` (a JSON array), reflected via
`aria-pressed` on the button and a live count in `#favorites-count` -
including on a fresh load that already has favorites saved.

## feat-05-form-validation

Required `#name`/`#message`, a real email-shape check on `#email`, blocking
the existing "Thanks" success message in `#form-status` until all three are
valid, with per-field errors in `#name-error`/`#email-error`/`#message-error`.

## feat-06-accessible-nav

A real keyboard focus trap for the mobile nav (`#nav-toggle`/`#site-nav`):
opening moves focus to the first link, Escape closes and returns focus to
the toggle, and Tab/Shift+Tab wrap focus between the toggle and the last
link rather than escaping the menu.

## Running

Directly:

```bash
npm install
npm test                                  # everything
node --test tests/search.test.js          # one feature only
```

Or through the benchmark harness:

```bash
cd ../../..   # harness-benchmarking repo root
python3 scripts/run_task.py FEAT-02 --harness loop --provider openrouter --model <model>
python3 scripts/run_task.py FEAT-03 --harness deepseek-harness --model <model>
python3 scripts/run_task.py FEAT-04 --harness goose --provider openrouter --model <model>
python3 scripts/run_task.py FEAT-05 --harness pi --provider openrouter --model <model>
```
