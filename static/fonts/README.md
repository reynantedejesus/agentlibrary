# Self-hosting the web fonts

The application currently loads its typefaces from Google Fonts, exactly as the
HTML prototype did:

```html
<link href="https://fonts.googleapis.com/css2?family=Archivo:...&family=Montserrat:...&family=Caveat:...&family=JetBrains+Mono:..." rel="stylesheet">
```

The Content-Security-Policy in `app/security.py` allows precisely two external
hosts for this and nothing else:

```
style-src  'self' https://fonts.googleapis.com 'unsafe-inline';
font-src   'self' https://fonts.gstatic.com data:;
```

## When to self-host instead

Self-host if any of the following apply:

* the CSP must forbid all external origins,
* the application servers have no outbound internet access,
* legal/privacy review objects to browsers contacting Google on page load,
* you have licensed the real brand faces (Europa Grotesk SH and Liana) — those
  are not on Google Fonts at all and *must* be self-hosted.

## How to self-host

1. Put the WOFF2 files in this directory:

   ```
   static/fonts/
     archivo-600.woff2      archivo-700.woff2      archivo-800.woff2
     montserrat-400.woff2   montserrat-500.woff2   montserrat-600.woff2
     jetbrains-mono-400.woff2
     caveat-600.woff2
   ```

2. Delete the three `<link>` tags pointing at Google from
   `templates/index.html` (the two `preconnect` hints and the stylesheet).

3. Add `@font-face` rules at the very top of `static/css/app.css`:

   ```css
   @font-face{
     font-family:'Archivo';
     src:url('/static/fonts/archivo-700.woff2') format('woff2');
     font-weight:700; font-style:normal; font-display:swap;
   }
   /* ...one block per weight/family... */
   ```

   Keep `font-display:swap` so text is readable before the font arrives.

4. Tighten the CSP in `app/security.py` — remove both Google hosts:

   ```python
   "style-src 'self' 'unsafe-inline'; "
   "font-src 'self' data:; "
   ```

5. Restart and verify with `curl -sI https://<host>/ | grep -i content-security`.

## Licensed brand faces

If the licensed Europa Grotesk SH and Liana webfonts become available, drop them
here and change only the two custom properties in `static/css/app.css`:

```css
--font-heading: 'Europa Grotesk SH', 'Archivo', 'Montserrat', sans-serif;
--font-accent:  'Liana', 'Caveat', cursive;
```

Nothing else in the stylesheet references a font family directly, so the swap
is a two-line change. Confirm the licence permits web delivery from your own
servers before deploying the files.
