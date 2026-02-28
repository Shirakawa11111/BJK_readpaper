# Paper Daily Agent

Automate daily paper discovery and maintain a living research system for:
- multiphysics coupling in materials
- molecular dynamics simulation
- phase-field crystal modeling
- metal fatigue simulation
- tensile/deformation simulation

## What it does

1. Fetches recent arXiv papers by your configured topic queries.
2. Scores relevance and selects the top `daily_limit` unseen papers (default: 5/day).
3. Creates a note file per paper under `data/notes/`.
4. Updates database `data/paper_db.json`.
5. Rebuilds `reports/knowledge_system.md` as your continuously updated topic system.
6. Writes a daily report under `reports/daily/YYYY-MM-DD.md`.
7. Generates Chinese per-paper briefs ("what it did / why it matters to your work").
8. Sends a daily Chinese reading reminder to Telegram via `clawdbot` (configurable).
9. Extracts keywords with explanations per paper and stores them in notes/database.
10. Rebuilds `reports/focus_year_summary.md` to summarize research focus and year trends.

## Quick start

```bash
python3 paper_agent.py init --root . --config config.json
python3 paper_agent.py update --root . --config config.json
```

Dry-run notification test (no actual Telegram send):

```bash
python3 paper_agent.py update --root . --config config.json --notify --notify-dry-run
```

## Add your known papers

Prepare CSV with headers:

```text
title,authors,year,link,tags,notes
```

`authors` uses `;` as separator, and `tags` should use internal topic keys:
- `multiphysics_coupling`
- `molecular_dynamics`
- `phase_field_crystal`
- `metal_fatigue`
- `tensile_simulation`

Then run:

```bash
python3 paper_agent.py ingest-known --root . --config config.json --csv ./known_papers_template.csv
```

## Optional LLM summary

If `OPENAI_API_KEY` is set, the script attempts structured Chinese LLM summaries via `llm.endpoint`.
Otherwise it falls back to deterministic Chinese abstract-based summaries.
The script also auto-loads env vars from `~/.clawdbot/.env` for cron/headless runs.

```bash
export OPENAI_API_KEY="YOUR_KEY"
python3 paper_agent.py update --root . --config config.json
```

## Daily scheduling (macOS/Linux cron)

Make script executable:

```bash
chmod +x ./run_daily.sh
```

Open crontab:

```bash
crontab -e
```

Run every day at 09:00:

```cron
0 9 * * * /Users/bojingkai/Desktop/Read_paper/run_daily.sh >> /Users/bojingkai/Desktop/Read_paper/reports/cron.log 2>&1
```

## Telegram reminder via clawbot

The reminder uses:
- `notify.enabled`
- `notify.clawbot.binary`
- `notify.clawbot.channel`
- `notify.clawbot.target`

Default target example is numeric chat id `5717971233` (recommended).
Reminder content includes Chinese brief for each paper:
- 做了什么
- 对你的意义
- 关键词（精简）
- 研究侧重点（精简）

Manual test:

```bash
python3 paper_agent.py update --root . --config config.json --limit 5 --notify
```

## Folder layout

```text
.
├── config.json
├── paper_agent.py
├── run_daily.sh
├── data
│   ├── notes
│   └── paper_db.json
└── reports
    ├── daily
    ├── focus_year_summary.md
    └── knowledge_system.md
```
