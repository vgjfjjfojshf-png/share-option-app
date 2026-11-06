# Share Option: go live

The site is one FastAPI service (serves the pages and the API) plus one worker that reads your Telegram channel.
Opening `platform.html` as a plain file only shows the screens: accounts and signals need the running server.

## Fastest path: any host that runs Docker + PostgreSQL
(Railway, Render, Fly.io, a VPS: anything with a Dockerfile deploy and a Postgres add-on.)

1. Upload this folder (or connect it from a Git repository) as a **web service** using the `Dockerfile`.
2. Add a **PostgreSQL** database and set `DATABASE_URL` on the web service to its URL.
3. Set the web service variables (see `.env.example`): `ADMIN_SECRET`, `USDT_TRC20_ADDRESS`, `POCKET_MASTER_ID`,
   your broker links `LINK_*` (registration) and `TRADE_*` (trading terminal), optional `GOOGLE_CLIENT_ID`.
4. Add a second service from the same image as a **worker** with start command `python telegram_listener.py` and variables
   `TG_API_ID`, `TG_API_HASH`, `TG_CHANNEL_IDS`, `ADMIN_SECRET`, `BACKEND_URL` (your web service URL), `TG_SESSION_STRING`.
   Get the session string once, on any computer or Termux: `TG_API_ID=... TG_API_HASH=... python make_session.py`.
5. Open your site URL. Admin console: `YOUR-URL/#/admin` (enter `ADMIN_SECRET`). **Site settings** there lets you paste your broker registration and trading links, WhatsApp/Telegram links, Pocket Option master ID and USDT address, with a go-live checklist. Changes apply instantly, no redeploy.
6. Create a test account, link a broker account, verify it in admin, post a test message in your channel.

## Your own server (docker compose + automatic HTTPS)
`cp .env.example .env`, edit it, run `docker compose run --rm listener` once for the Telegram login, then `docker compose up -d --build`.

## Logos
`static/brokers/*.svg` are neutral placeholders. Replace them with official logos (same file names) if you are licensed to use them.

## Styles
The page uses a built stylesheet (no Tailwind CDN, so no console warning). After editing `platform.html` classes or `tools/app.src.css`:
`npm i tailwindcss && node tools/build_css.js`.

## Legal
`#/legal` holds Terms, Privacy and Risk Disclosure text written as a starting template; have a lawyer review and adapt it to your company and countries. Registration requires accepting it.

## Before real users
Run the test suite on a staging copy: `ADMIN_SECRET=... AUTO_CONFIRM_PAYMENTS=true RATE_LIMIT_DISABLED=true python e2e_test.py`.
Back up the database. Get a legal review: binary options are restricted in many countries.
