# Spike: sourcing social discussion with Grok X Search

**Question:** Can xAI's Grok, with its built-in X (Twitter) search, source the
social-conviction data for a "Social" tab (heated topics with bull/bear angles
and source tweets) — without us standing up an X API integration?

**Answer:** Yes. Grok runs the search server-side (`x_search` tool on the
Responses API) and returns synthesized output + per-post citations. Approach,
prompt style, and cost/quality numbers were settled by live comparison —
see `FINDINGS.md`. Only an `XAI_API_KEY` is needed (env, `~/.env`, or repo
`.env`).

## Files
- `social_topics.py` — **the keeper**: ticker → heated topics JSON
  (topic kind/heat, bull/bear angles in house voice, source tweets verified
  against API citations by status ID).
- `grok_client.py` — httpx wrapper around `POST https://api.x.ai/v1/responses`
  with the `x_search` tool.
- `test_extract.py` — offline test of the response parser (no API key needed).
- `FINDINGS.md` — live-run results: approach comparison (free-form vs
  structured vs multi-agent), prompt-style iterations V0–V7, verifiability,
  cost model, production sketch.

## Run it
```bash
uv run python spikes/grok-social/social_topics.py MSTR --name "MicroStrategy"
uv run python spikes/grok-social/social_topics.py MU --days 7

# Offline (no key) — sanity-check the parser:
uv run python spikes/grok-social/test_extract.py
```

## Key facts (docs.x.ai, verified live 2026-06-03)
- Model: `grok-4.20-0309-reasoning` — $1.25/M in, $2.50/M out, 1M ctx.
- `x_search` tool params: `from_date`/`to_date`, `allowed_x_handles`/
  `excluded_x_handles` (≤20, mutually exclusive), image/video understanding.
- The legacy Live Search API (`search_parameters`) was retired 2026-01-12 —
  ignore older tutorials that use it.
- Exact per-call cost comes back in `usage.cost_in_usd_ticks` (×1e-10 USD);
  ~$0.056/ticker/refresh.

## Sources
- [Models — xAI Docs](https://docs.x.ai/developers/models)
- [X Search — xAI Docs](https://docs.x.ai/developers/tools/x-search)
