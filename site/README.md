# receivecoaching.com landing page

Single self-contained `index.html` — no build step, no dependencies, dark/light via `prefers-color-scheme`.

## Before going live

1. Publish the .exe somewhere durable (GitHub Releases is the obvious free choice) and replace the `#download` href on the "Download for Windows" button with the real URL.
2. Optional: add an OG image (`<meta property="og:image" ...>`) for link previews.

## Deploy (pick one)

**Cloudflare Pages** (free, fast):
```
npx wrangler pages deploy site --project-name receivecoaching
```
Then add receivecoaching.com as a custom domain in the CF dashboard.

**Netlify:**
```
npx netlify-cli deploy --dir site --prod
```

**Vercel:**
```
npx vercel site --prod
```

All three give HTTPS automatically once the domain's DNS points at them
(CNAME per their instructions — done in wherever receivecoaching.com is registered).
