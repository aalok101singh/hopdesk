<p align="center">
  <img src="https://img.shields.io/badge/%F0%9F%90%B8-HopDesk-green?style=for-the-badge" alt="HopDesk"/>
</p>

<h1 align="center">HopDesk</h1>
<h3 align="center">The Smart-Budget AI Support Agent — every query hops to the cheapest model that can handle it.</h3>

<p align="center">
  <a href="https://hopdesk-z6sp.onrender.com/"><b>◉ Live</b></a> ·
  <a href="#powered-by-modelhop">Powered by ModelHop</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/router-modelhop%201.1.0-green?style=flat-square" alt="ModelHop"/>
  <img src="https://img.shields.io/badge/license-MIT-lightgrey?style=flat-square" alt="MIT"/>
</p>

> **Problem:** monolithic AI support setups default to premium models
> (like GPT-4) for trivial questions — exorbitant cost, fragile to outages.
> **HopDesk** answers cheap questions for ~$0.0001, escalates only when
> confidence drops below 0.70, and falls back to a deterministic mock when
> premium APIs 429 — every decision inspectable in a Run Inspection drawer.

## See it

| Simple FAQ → free tier | Heated dispute → escalated | Premium outage → fallback |
|---|---|---|
| Teal dot, $0.0001, conf 0.92 | Amber dot, premium attempt logged | Slate dot, `degraded`, trace kept |

Click any answer's `details` microcopy: model, provider, tier, reasoning,
confidence vs threshold, tokens, money vs GPT-4 baseline, ledger trace.

## Run it (60 seconds, zero keys required)

```bash
pip install -r requirements.txt
uvicorn app:app --reload
# open http://127.0.0.1:8000
```

Optional live answers: `GROQ_API_KEY` ([console.groq.com](https://console.groq.com))
and `GEMINI_API_KEY` ([aistudio.google.com](https://aistudio.google.com/apikey)).
Premium stays honestly simulated unless a paid `OPENAI_API_KEY` is set — the
drawer always labels `live` vs `simulated`.

## Powered by ModelHop

HopDesk is a thin UI over **[ModelHop](https://pypi.org/project/modelhop/)**
(`pip install modelhop`) — an open-source plug-and-play router that sends each
query to the cheapest capable model. Taste the engine on its own:

```bash
pip install modelhop
modelhop route "How do I reset my password?"   # cheapest capable tier + savings printed
```

What HopDesk borrows from it:

- `LearningRouter` + `QueryAnalyzer` — tier choice per query
- `ConfidenceEngine` (threshold 0.70) — escalation brains
- `CascadeFallback` + `HealthRegistry` — outage survival
- `TraceLogger` + signed ledger — the audit trail in the drawer

Built by [Aalok](https://github.com/aalok101singh) — [ModelHop on GitHub](https://github.com/aalok101singh/modelhop) · MIT.

## Deploy

- **Live:** https://hopdesk-z6sp.onrender.com/ (UI + API, same origin — no params, no CORS, no split)
- Build `pip install -r requirements.txt` · Start `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Env `GROQ_API_KEY` / `GEMINI_API_KEY` (optional; keyless still demos fully offline)

## Files

- `app.py` — FastAPI wrapping ModelHop (`/api/chat`, `/api/sessions`, `/api/runs`, `/api/usage`, `/api/incident`, `/api/reset`)
- `index.html` — dark chat + Run Inspection drawer (Geist + JetBrains Mono)
- `seed.json` · `modelhop.yaml` — opening thread + pinned 3-tier routing config
