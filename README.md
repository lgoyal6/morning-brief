# Morning Brief 🗞️

A GitHub Actions bot that generates Laksh's **daily AI / tech-infrastructure / markets / geopolitics** morning brief, commits it to this repo, uploads it as a workflow artifact, and DMs the PDF to Discord.

Every morning (Pacific time) it:

1. Pulls current articles from credible RSS feeds + topic-targeted Google News searches (no paid API, no hallucinated news).
2. Optionally snapshots watchlist quotes via `yfinance`.
3. Asks an **OpenAI-compatible LLM** to synthesize a structured Markdown brief from **only** those sources.
4. Renders a phone-readable **PDF** (clickable links, tables, sections).
5. Commits `briefs/YYYY-MM-DD-*.md` + `.pdf` and updates `briefs/latest-*.pdf`.
6. Uploads all outputs as workflow artifacts.
7. Sends the dated PDF to your Discord DM (or a channel).

---

## Why GMI Cloud (zero extra billing)

The generator talks to any endpoint that speaks the **OpenAI Chat Completions API**. By default it points at **GMI Cloud** (`https://api.gmi-serving.com/v1`) with an open-weight model (`deepseek-ai/DeepSeek-V4-Flash`). Using your GMI key means an open-source model with essentially free/near-zero cost — no OpenAI or Anthropic billing required.

You can repoint it at OpenAI, or any other compatible provider, purely via env/variables (see below).

---

## Repo structure

```
morning-brief/
  README.md
  requirements.txt
  .gitignore
  .github/workflows/morning-brief.yml
  briefs/                 # generated .md / .pdf are committed here
  scripts/
    common.py             # shared paths, date, state
    generate_brief.py     # fetch sources -> LLM -> Markdown
    render_pdf.py          # Markdown -> PDF
    send_discord.py        # PDF -> Discord
```

---

## Required secrets

Configure these under **Settings → Secrets and variables → Actions → Secrets**:

| Secret | Purpose |
| --- | --- |
| `GMI_API_KEY` | GMI Cloud inference key (**recommended**). *Or* set `OPENAI_API_KEY` instead. |
| `DISCORD_BOT_TOKEN` | Discord **bot** token (from the Discord Developer Portal). |
| `DISCORD_TARGET` | Where to send. `user:912900101087854603` for a DM, or `channel:<channel_id>`. |

Optional **Variables** (Settings → Variables), to override defaults without touching code:

| Variable | Default | Notes |
| --- | --- | --- |
| `LLM_BASE_URL` | `https://api.gmi-serving.com/v1` | OpenAI-compatible base URL. |
| `LLM_MODEL` | `deepseek-ai/DeepSeek-V4-Flash` | Any model your endpoint serves. |

> **Which model?** List what your key can access:
> ```bash
> curl -s https://api.gmi-serving.com/v1/models -H "Authorization: Bearer $GMI_API_KEY" | python3 -m json.tool
> ```
> Then set the `LLM_MODEL` variable to one of the returned IDs. Good open-weight choices: `deepseek-ai/DeepSeek-V4-Flash` (cheapest), a `Qwen/...` chat model, or a `zai-org/GLM-...` model.

### Discord bot setup (once)

1. https://discord.com/developers/applications → **New Application** → **Bot** → copy the **token** → set it as `DISCORD_BOT_TOKEN`.
2. To DM yourself: the bot must **share a server** with you. Invite it to any server you're both in (OAuth2 URL, scope `bot`). Then set `DISCORD_TARGET=user:912900101087854603` (your user ID). Make sure your server privacy settings allow DMs from server members.
3. To post to a channel instead: enable Developer Mode in Discord, right-click the channel → **Copy Channel ID**, set `DISCORD_TARGET=channel:<id>`, and give the bot access + "Send Messages" + "Attach Files" in that channel.

---

## Run it manually

- GitHub UI: **Actions → Morning Brief → Run workflow**. A manual dispatch always regenerates and re-sends (even if today's brief already exists).

## Run locally

```bash
cd morning-brief
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export GMI_API_KEY=...                # or OPENAI_API_KEY=...
export DISCORD_BOT_TOKEN=...
export DISCORD_TARGET=user:912900101087854603
# optional: export LLM_MODEL=deepseek-ai/DeepSeek-V4-Flash

python scripts/generate_brief.py
python scripts/render_pdf.py
python scripts/send_discord.py
```

You can also create a `.env` file (git-ignored) with those variables instead of exporting them.

### Test without any API key

```bash
BRIEF_DRY_RUN=1 python scripts/generate_brief.py   # builds a stub brief from live RSS, no LLM
python scripts/render_pdf.py                        # produces the PDF
DISCORD_DRY_RUN=1 python scripts/send_discord.py    # logs instead of sending
```

---

## Behavior details

- **Date**: computed in `America/Los_Angeles`.
- **Duplicate prevention**: on a *scheduled* run, if today's `.md` + `.pdf` already exist, the pipeline skips generation, PDF render, and the Discord send — so the two DST cron times never produce a duplicate brief or double message. Manual dispatch or `FORCE_REGENERATE=1` overrides this.
- **No hallucinated news**: if zero sources are fetched, generation aborts rather than inventing headlines. The model is instructed to use only the supplied sources and to link every story.
- **Discord failure**: the Discord send runs *last*, so even if it fails, the brief has already been committed and uploaded. The workflow then fails with a clear Discord error.
- **Provenance**: the raw fetched sources/quotes are saved to `briefs/YYYY-MM-DD-sources.json` (uploaded as an artifact, not committed).

## Configuration knobs (env vars)

| Var | Default | Meaning |
| --- | --- | --- |
| `LLM_BASE_URL` | GMI Cloud | OpenAI-compatible endpoint. |
| `LLM_MODEL` | `deepseek-ai/DeepSeek-V4-Flash` | Model ID. |
| `LLM_MAX_TOKENS` | `7000` | Max output tokens. |
| `LLM_TEMPERATURE` | `0.4` | Sampling temperature. |
| `ENABLE_QUOTES` | `1` | Pull watchlist quotes via yfinance (best-effort). |
| `BRIEF_LOOKBACK_HOURS` | `48` | How far back to keep articles. |
| `BRIEF_MAX_ITEMS` | `70` | Max sources sent to the model. |
| `FORCE_REGENERATE` | — | Regenerate even on a scheduled duplicate. |
| `BRIEF_DRY_RUN` | — | Build a stub brief without calling the LLM. |
| `DISCORD_DRY_RUN` | — | Log instead of sending to Discord. |

## Outputs

- `briefs/YYYY-MM-DD-ai-tech-market-brief.md` — the brief (committed)
- `briefs/YYYY-MM-DD-ai-tech-market-brief.pdf` — the PDF (committed)
- `briefs/latest-ai-tech-market-brief.pdf` — always the newest PDF (committed)
- Workflow **Artifacts** — the above plus the sources JSON, per run.
