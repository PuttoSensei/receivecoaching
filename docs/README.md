# receivecoaching.com landing page

Single self-contained `index.html` — no build step, no dependencies, dark/light via `prefers-color-scheme`.

Served by **GitHub Pages** from this folder (`/docs` on `main`). Deploying = `git push`.

## Custom domain

`CNAME` in this folder pins the domain. At the registrar for receivecoaching.com, set:

| Type  | Name | Value                    |
|-------|------|--------------------------|
| A     | @    | 185.199.108.153          |
| A     | @    | 185.199.109.153          |
| A     | @    | 185.199.110.153          |
| A     | @    | 185.199.111.153          |
| CNAME | www  | puttosensei.github.io    |

HTTPS auto-provisions a few minutes after DNS propagates (then tick "Enforce HTTPS" in repo Settings → Pages).

## Moving hosts later (optional)

The page is a single static file — `npx wrangler pages deploy docs` (Cloudflare), `npx netlify-cli deploy --dir docs --prod`, or `npx vercel docs --prod` all work as-is.
