# JUST CALL SOL — site

A one-page site for the agent-hotline plugin, staged as a hostage situation for
coding agents: when Codex or Claude gets blocked, it doesn't guess — it phones Sol.

No build step. Serve the folder and open it:

```powershell
python -m http.server 8123 --directory app
```

Then visit <http://localhost:8123>.

## Files

- `index.html` — content (the note, the situation, the call on tape, Sol's rules, the kit, the drill)
- `sol.css` — the paper/typewriter world, CRT glass, ticket + emboss, chroma glow
- `sol.js` — ransom-note composer, phosphor call simulation, copy buttons
- `typer.js` — the character-reveal engine
- `assets/ransom/` — cut-out letter sprites (Resource Boy "Ransom Note Letters" pack, royalty-free)

## Stolen goods

Effects lifted directly from the [arlan.me vault](https://www.arlan.me/vault)
(MIT → free to copy): the ransom-note cutouts and its seeded composer, the typer
(engine + pill-merge CSS, verbatim), the CRT old-TV treatment from the Midjourney
ASCII entry, the emboss recipe as the ticket seal, and the chromatic glow in the
footer.
