# Putting the demo online (Streamlit Community Cloud — free, public)

The demo needs **no database account and no secrets**. It builds its own SQLite database with fictional data and
rebuilds it in the background once a day, so there is nothing to reset by hand. Only right after the app wakes up
does a visitor wait (about 20–30 seconds, with a progress bar).

## 1. Push the repository to GitHub
- Create a new repository, e.g. `quantum-bd-hub-demo` (public if you want visitors to see the code; the app can be
  public either way).
- Before the first push, run `pytest` — the guard tests fail if anything from the real company is in the code, docs or
  data.

```bash
git init
git add .
git commit -m "BD & Marketing Hub — portfolio demo"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/quantum-bd-hub-demo.git
git push -u origin main
```

## 2. Create the app
1. Go to **share.streamlit.io**, sign in with GitHub, **Create app → Deploy a public app from GitHub**.
2. Repository `YOUR-USERNAME/quantum-bd-hub-demo`, branch `main`, main file `streamlit_app.py`.
3. App URL: something short, e.g. `bd-hub-demo` → `https://bd-hub-demo.streamlit.app`.
4. **Advanced settings → Python 3.12**. Secrets are optional — only for the credits on the welcome and About pages:
   ```toml
   DEMO_AUTHOR = "Mekhala Ganguly"
   DEMO_AUTHOR_URL = "https://your-portfolio.example"
   DEMO_REPO_URL = "https://github.com/YOUR-USERNAME/quantum-bd-hub-demo"
   ```
5. **Deploy.** The first open after a deploy or a wake-up shows *Preparing the demo data* with a progress bar
   (about 20–30 seconds); after that pages are instant, and the daily refresh happens in the background.

## 3. Check it
- Open the link in a private window → the welcome page → **Explore as Riya**.
- Today shows overdue and due tasks; Dashboard, Weekly report, Quotes and GTM charts have data.
- Your name (top right) → **Switch persona** → Leadership: editing buttons are gone (view only).

## Putting it on your portfolio site
- **Link** to the app with a short line: *Live demo — pick a persona and click anything; all data is fictional.*
- **Embed** in a page with an iframe: `https://bd-hub-demo.streamlit.app/?embed=true` (height 800–900 px).
- Free apps **sleep** after a few days without visitors; the next visitor clicks *Yes, get this app back up* and waits
  about a minute. So lead with screenshots and a short video on the portfolio page and put the live link underneath.

## Everyday notes
- Every `git push` to `main` redeploys within a minute or two.
- The data resets daily and on every reboot (⋮ → Reboot app), so visitors' edits never pile up.
- News & signals reads live public news feeds; the fictional organisations won't appear in real headlines, which is why
  the demo's signals are seeded by the story.
