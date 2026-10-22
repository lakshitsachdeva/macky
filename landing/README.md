# Landing Page

This landing page is a Vite app backed by a Vercel serverless route at `/api/waitlist`.
The browser no longer writes to `public.waitlist_signups` directly.

## Emergency Fix

If bots are already filling your table:

1. Run [`supabase/waitlist.sql`](./supabase/waitlist.sql) in Supabase SQL Editor right now.
   This revokes all direct `anon` access to `public.waitlist_signups`.
2. Run [`supabase/security_hardening.sql`](./supabase/security_hardening.sql) if you use
   `public.whitelist`, `public.usage`, `public.access`, or `public.profiles`.
   This locks admin tables and access RPCs to `service_role`.
3. Deploy this landing app update so `/api/waitlist` becomes the only write path.
4. Delete or export the obvious spam rows from Supabase Table Editor after the lock-down is live.

## Environment

Create a local env file:

```bash
cp .env.example .env.local
```

Fill in:

```env
SUPABASE_URL=https://YOUR_PROJECT_REF.supabase.co
SUPABASE_SERVICE_ROLE_KEY=YOUR_SUPABASE_SERVICE_ROLE_KEY
VITE_SUPABASE_URL=https://YOUR_PROJECT_REF.supabase.co
VITE_SUPABASE_ANON_KEY=YOUR_SUPABASE_ANON_KEY
VITE_WAITLIST_API_URL=/api/waitlist
ADMIN_EMAILS=you@example.com
VITE_TURNSTILE_SITE_KEY=YOUR_CLOUDFLARE_TURNSTILE_SITE_KEY
TURNSTILE_SECRET_KEY=YOUR_CLOUDFLARE_TURNSTILE_SECRET_KEY
WAITLIST_ALLOWED_ORIGIN_HOSTS=recurdo.lakshitsachdeva.tech,localhost:5173,127.0.0.1:5173
WAITLIST_BLOCKED_EMAIL_DOMAINS=mailinator.com,tempmail.com,guerrillamail.com
```

`SUPABASE_SERVICE_ROLE_KEY` is server-only. Do not expose it in client-side code.
`TURNSTILE_SECRET_KEY` is also server-only. `VITE_TURNSTILE_SITE_KEY` is safe for the browser.

## Supabase Setup

1. Create a new Supabase project.
2. In Supabase, open the SQL Editor.
3. Run the SQL in [`supabase/waitlist.sql`](./supabase/waitlist.sql).
4. Confirm the `waitlist_signups` table exists with:
   - `email`
   - `email_normalized`
   - `source`
   - `status`
   - `metadata`
   - `ip_hash`
   - `created_at`
5. Confirm `anon` no longer has `select` or `insert` access on `public.waitlist_signups`.
6. If you use access-control tables, run [`supabase/security_hardening.sql`](./supabase/security_hardening.sql).
7. In Cloudflare Turnstile, create a widget for your landing domain and copy the site key + secret.

## Local Development

`npm run dev` now serves a local `/api/waitlist` middleware through Vite.
It reads env vars from:

- `memory-engine/landing/.env.local`
- `memory-engine/.env`

For local waitlist testing you need server-side Supabase credentials available in one of those files.

To run only the frontend visuals:

```bash
npm install
npm run dev
```

## Vercel Deploy

1. Push the repo to GitHub.
2. In Vercel, click `Add New -> Project`.
3. Import the repo.
4. Set the project `Root Directory` to `memory-engine/landing`.
5. Add environment variables in Vercel Project Settings:
   - `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `VITE_SUPABASE_URL`
- `VITE_SUPABASE_ANON_KEY`
- `ADMIN_EMAILS` with the comma-separated emails allowed into `/admin.html`
- `VITE_WAITLIST_API_URL` with value `/api/waitlist`
- `VITE_TURNSTILE_SITE_KEY`
- `TURNSTILE_SECRET_KEY`
   - `WAITLIST_ALLOWED_ORIGIN_HOSTS` with your production host plus local dev hosts if needed
   - optional `WAITLIST_BLOCKED_EMAIL_DOMAINS`
6. Deploy.
7. After deploy:
   - submit the form once
   - confirm the row appears in `public.waitlist_signups`
   - confirm `/api/waitlist` returns a JSON payload
   - confirm direct browser writes to `/rest/v1/waitlist_signups` are denied
   - confirm a `curl` POST to `/api/waitlist` without a Turnstile token is rejected

## Notes

- The waitlist count is now served from `/api/waitlist`.
- Rate limiting happens on the server using a hashed client IP.
- The form includes a honeypot field and minimum submit time check to filter basic bots.
- If `TURNSTILE_SECRET_KEY` is set, the API requires a valid Cloudflare Turnstile token.
- If another app or desktop client needs waitlist access, point it at the same API route instead of exposing table writes again.
- `/admin.html` uses Supabase sign-in in the browser, then calls the server-side admin APIs:
  - `GET /api/admin/requests`
  - `POST /api/admin/access`
- Runtime access should come from `public.access`. `public.waitlist_signups` is only the request inbox.
