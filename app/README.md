# Better Call Sol site

This folder contains the static product page for Agent Hotline, branded as “Better Call Sol.”
There is no build step.

```powershell
python -m http.server 8123 --directory app
```

Then open <http://localhost:8123>.

## Files

- `index.html` — product copy and semantic structure
- `sol.css` — paper, typewriter, CRT, ticket, emboss, and glow styling
- `sol.js` — ransom-letter composition, call simulation, and copy buttons
- `typer.js` — progressive text-reveal behavior
- `assets/ransom/` — cut-out letter sprites

## Attribution

The visual treatment adapts MIT-licensed examples from the
[arlan.me vault](https://www.arlan.me/vault), including the seeded ransom-letter composer,
typing effect, CRT treatment, emboss recipe, and chromatic glow. The cut-out letter sprites
come from Resource Boy's royalty-free “Ransom Note Letters” pack.
