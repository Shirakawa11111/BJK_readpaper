#!/usr/bin/env python3
"""Daily paper ingestion and knowledge-system builder.

This script is designed for materials simulation domains, with defaults for:
- multiphysics coupling
- molecular dynamics
- phase-field crystal
- metal fatigue simulation
- tensile simulation
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ARXIV_API = "https://export.arxiv.org/api/query"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


DEFAULT_CONFIG = {
    "daily_limit": 5,
    "arxiv_max_results_per_topic": 40,
    "request_timeout_sec": 30,
    "llm": {
        "enabled": True,
        "api_key_env": "OPENAI_API_KEY",
        "model": "gpt-4.1-mini",
        "endpoint": "https://api.openai.com/v1/responses",
        "temperature": 0.2,
    },
    "topics": [
        {
            "name": "multiphysics_coupling",
            "query": 'all:"multiphysics" AND (all:"materials" OR all:"metal")',
        },
        {
            "name": "molecular_dynamics",
            "query": 'all:"molecular dynamics" AND (all:"materials" OR all:"deformation")',
        },
        {
            "name": "phase_field_crystal",
            "query": 'all:"phase field crystal" OR all:"phase-field crystal"',
        },
        {
            "name": "metal_fatigue",
            "query": 'all:"metal fatigue" AND (all:"simulation" OR all:"modeling")',
        },
        {
            "name": "tensile_simulation",
            "query": 'all:"tensile" AND (all:"simulation" OR all:"deformation")',
        },
    ],
    "keyword_weights": {
        "multiphysics": 3.0,
        "coupled": 2.0,
        "molecular dynamics": 3.0,
        "phase-field crystal": 3.0,
        "fatigue": 3.0,
        "tensile": 2.0,
        "dislocation": 1.5,
        "crack": 1.5,
        "deformation": 1.3,
        "metal": 1.2,
        "materials": 1.2,
        "simulation": 1.0,
        "modeling": 1.0,
    },
    "taxonomy": {
        "multiphysics_coupling": [
            "multiphysics",
            "coupled",
            "thermo-mechanical",
            "electrochemical",
            "fluid-structure",
        ],
        "molecular_dynamics": [
            "molecular dynamics",
            "atomistic",
            "interatomic potential",
            "LAMMPS",
            "melt-quench",
        ],
        "phase_field_crystal": [
            "phase-field crystal",
            "phase field crystal",
            "pfc",
            "amplitude expansion",
        ],
        "metal_fatigue": [
            "fatigue",
            "cyclic loading",
            "crack growth",
            "high-cycle",
            "low-cycle",
        ],
        "tensile_simulation": [
            "tensile",
            "uniaxial",
            "stress-strain",
            "strain hardening",
            "elongation",
        ],
    },
    "topic_labels": {
        "multiphysics_coupling": "Multiphysics Coupling",
        "molecular_dynamics": "Molecular Dynamics",
        "phase_field_crystal": "Phase-Field Crystal",
        "metal_fatigue": "Metal Fatigue Simulation",
        "tensile_simulation": "Tensile / Deformation Simulation",
    },
}


@dataclass
class Paper:
    paper_id: str
    title: str
    summary: str
    authors: list[str]
    published: str
    updated: str
    link: str
    source_topic: str
    tags: list[str] = field(default_factory=list)
    score: float = 0.0


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def slugify(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("_")
    return clean or "paper"


def ensure_dirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: Path, payload: Any) -> None:
    ensure_dirs(path.parent)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def load_config(config_path: Path) -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if config_path.exists():
        user_cfg = load_json(config_path, default={})
        deep_merge(config, user_cfg)
    return config


def deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            deep_merge(dst[key], value)
        else:
            dst[key] = value


def parse_arxiv_id(entry_id: str) -> str:
    # e.g. http://arxiv.org/abs/2501.12345v1 -> 2501.12345v1
    return entry_id.rstrip("/").split("/")[-1]


def fetch_arxiv_entries(query: str, max_results: int, timeout_sec: int) -> list[Paper]:
    params = {
        "search_query": query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = f"{ARXIV_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "paper-agent/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        payload = resp.read()

    root = ET.fromstring(payload)
    entries: list[Paper] = []
    for node in root.findall("atom:entry", namespaces=ATOM_NS):
        entry_id = node.findtext("atom:id", default="", namespaces=ATOM_NS).strip()
        if not entry_id:
            continue
        paper_id = parse_arxiv_id(entry_id)
        title = compact_whitespace(node.findtext("atom:title", default="", namespaces=ATOM_NS))
        summary = compact_whitespace(node.findtext("atom:summary", default="", namespaces=ATOM_NS))
        published = node.findtext("atom:published", default="", namespaces=ATOM_NS).strip()
        updated = node.findtext("atom:updated", default="", namespaces=ATOM_NS).strip()
        authors = [
            compact_whitespace(a.findtext("atom:name", default="", namespaces=ATOM_NS))
            for a in node.findall("atom:author", namespaces=ATOM_NS)
        ]
        links = node.findall("atom:link", namespaces=ATOM_NS)
        abs_link = ""
        for ln in links:
            if ln.attrib.get("rel") == "alternate":
                abs_link = ln.attrib.get("href", "")
                break
        if not abs_link:
            abs_link = f"https://arxiv.org/abs/{paper_id}"
        entries.append(
            Paper(
                paper_id=paper_id,
                title=title,
                summary=summary,
                authors=[a for a in authors if a],
                published=published,
                updated=updated,
                link=abs_link,
                source_topic="",
            )
        )
    return entries


def compact_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def score_paper(
    paper: Paper, keyword_weights: dict[str, float], today: dt.datetime
) -> float:
    text = f"{paper.title}\n{paper.summary}".lower()
    score = 0.0
    for key, weight in keyword_weights.items():
        if key.lower() in text:
            score += float(weight)
    published_dt = parse_datetime(paper.published)
    if published_dt:
        age_days = max(0, (today - published_dt).days)
        recency = max(0.0, 1.0 - (age_days / 90.0))
        score += 3.0 * recency
    return round(score, 4)


def parse_datetime(value: str) -> dt.datetime | None:
    if not value:
        return None
    # arXiv uses RFC3339 like 2024-03-01T00:00:00Z
    normalized = value.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None


def extract_tags(text: str, taxonomy: dict[str, list[str]]) -> list[str]:
    lowered = text.lower()
    tags: list[str] = []
    for topic, keywords in taxonomy.items():
        for keyword in keywords:
            if keyword.lower() in lowered:
                tags.append(topic)
                break
    return sorted(set(tags))


def dedupe_entries(entries: list[Paper]) -> list[Paper]:
    merged: dict[str, Paper] = {}
    for paper in entries:
        existing = merged.get(paper.paper_id)
        if existing is None or paper.score > existing.score:
            merged[paper.paper_id] = paper
    return list(merged.values())


def select_new_papers(
    all_entries: list[Paper], db: dict[str, Any], daily_limit: int
) -> list[Paper]:
    unseen = [p for p in all_entries if p.paper_id not in db]
    unseen.sort(
        key=lambda p: (p.score, parse_datetime(p.published) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)),
        reverse=True,
    )
    return unseen[:daily_limit]


def build_fallback_summary(paper: Paper, labels: dict[str, str]) -> str:
    topics = [labels.get(tag, tag) for tag in paper.tags]
    if not topics:
        topics = ["General simulation / materials modeling"]
    abstract = paper.summary.strip()
    abstract_preview = abstract[:1000] + ("..." if len(abstract) > 1000 else "")
    return textwrap.dedent(
        f"""\
        1) Core Question
        - {paper.title}

        2) Why It Matters
        - Likely relevant to: {", ".join(topics)}.

        3) Method Signals (from abstract)
        - {infer_method_signal(abstract)}

        4) What To Track
        - Data/benchmark used
        - Boundary conditions and loading assumptions
        - Transferability to your material system

        5) Abstract Snapshot
        - {abstract_preview}
        """
    ).strip()


def infer_method_signal(abstract: str) -> str:
    lowered = abstract.lower()
    hints = []
    patterns = [
        ("molecular dynamics", "Atomistic MD workflow"),
        ("phase-field crystal", "Phase-field-crystal formulation"),
        ("finite element", "Finite-element modeling"),
        ("crack", "Crack initiation/growth focus"),
        ("fatigue", "Fatigue loading analysis"),
        ("tensile", "Tensile/deformation setup"),
        ("multiphysics", "Multiphysics coupled simulation"),
    ]
    for needle, label in patterns:
        if needle in lowered:
            hints.append(label)
    if hints:
        return "; ".join(hints)
    return "Method cues are weak; check equations/setup section directly."


def maybe_summarize_with_llm(
    paper: Paper, cfg: dict[str, Any], labels: dict[str, str]
) -> str:
    llm_cfg = cfg.get("llm", {})
    if not llm_cfg.get("enabled", True):
        return build_fallback_summary(paper, labels)

    api_key_env = llm_cfg.get("api_key_env", "OPENAI_API_KEY")
    api_key = os.getenv(api_key_env, "")
    if not api_key:
        return build_fallback_summary(paper, labels)

    endpoint = llm_cfg.get("endpoint", "https://api.openai.com/v1/responses")
    model = llm_cfg.get("model", "gpt-4.1-mini")
    temperature = llm_cfg.get("temperature", 0.2)
    prompt = textwrap.dedent(
        f"""\
        You are helping maintain a research map for materials simulation.
        Summarize this paper in concise English, using exactly these sections:
        1) Core Question
        2) Key Method
        3) Main Findings
        4) Relevance To My Topics
        5) Next Actions

        Paper title: {paper.title}
        Published: {paper.published}
        Authors: {", ".join(paper.authors)}
        Suggested tags: {", ".join(labels.get(tag, tag) for tag in paper.tags) or "N/A"}
        Abstract:
        {paper.summary}
        """
    ).strip()

    payload = {
        "model": model,
        "temperature": temperature,
        "input": [
            {
                "role": "system",
                "content": [
                    {"type": "input_text", "text": "Return plain text only. Be precise and concise."}
                ],
            },
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        ],
    }

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "paper-agent/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
            output_text = parse_response_text(raw)
            if output_text:
                return output_text.strip()
    except Exception:
        # Fallback if the API call fails.
        pass
    return build_fallback_summary(paper, labels)


def parse_response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return payload["output_text"]
    for out in payload.get("output", []):
        for content in out.get("content", []):
            maybe_text = content.get("text")
            if isinstance(maybe_text, str) and maybe_text.strip():
                return maybe_text
    return ""


def write_paper_note(path: Path, paper: Paper, summary_text: str) -> None:
    ensure_dirs(path.parent)
    lines = [
        f"# {paper.title}",
        "",
        f"- ID: `{paper.paper_id}`",
        f"- Published: {paper.published or 'N/A'}",
        f"- Updated: {paper.updated or 'N/A'}",
        f"- Authors: {', '.join(paper.authors) if paper.authors else 'N/A'}",
        f"- Link: {paper.link}",
        f"- Tags: {', '.join(paper.tags) if paper.tags else 'N/A'}",
        f"- Relevance score: {paper.score}",
        "",
        "## Structured Summary",
        summary_text.strip(),
        "",
        "## Raw Abstract",
        paper.summary.strip() or "N/A",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_daily_report(
    report_path: Path, selected: list[Paper], db: dict[str, Any], labels: dict[str, str]
) -> None:
    ensure_dirs(report_path.parent)
    date_label = dt.date.today().isoformat()
    lines = [f"# Daily Paper Update - {date_label}", ""]
    if not selected:
        lines.extend(["No new unseen papers selected today.", ""])
    else:
        for idx, paper in enumerate(selected, start=1):
            note_rel = db[paper.paper_id]["note_path"]
            label_list = [labels.get(tag, tag) for tag in paper.tags]
            lines.extend(
                [
                    f"## {idx}. {paper.title}",
                    f"- ID: `{paper.paper_id}`",
                    f"- Tags: {', '.join(label_list) if label_list else 'N/A'}",
                    f"- Score: {paper.score}",
                    f"- Link: {paper.link}",
                    f"- Note: {note_rel}",
                    "",
                ]
            )
    report_path.write_text("\n".join(lines), encoding="utf-8")


def rebuild_knowledge_map(
    map_path: Path, db: dict[str, Any], topic_labels: dict[str, str]
) -> None:
    ensure_dirs(map_path.parent)
    all_records = sorted(
        db.values(),
        key=lambda r: parse_datetime(r.get("published", "")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        reverse=True,
    )
    lines = ["# Materials Simulation Paper System", ""]
    lines.extend(
        [
            "This map is rebuilt automatically from `data/paper_db.json`.",
            "",
            "## Overview",
            f"- Total papers in library: {len(all_records)}",
            f"- Last rebuilt: {now_utc().isoformat()}",
            "",
        ]
    )

    for topic in topic_labels:
        label = topic_labels[topic]
        topic_records = [r for r in all_records if topic in r.get("tags", [])]
        lines.append(f"## {label}")
        if not topic_records:
            lines.append("- No papers tagged yet.")
            lines.append("")
            continue
        for r in topic_records[:40]:
            pub = r.get("published", "N/A")
            status = r.get("status", "auto")
            title = r.get("title", "Untitled")
            link = r.get("link", "")
            note_path = r.get("note_path", "")
            lines.append(
                f"- [{title}]({link}) | {pub} | status: {status} | note: `{note_path}`"
            )
        lines.append("")
    map_path.write_text("\n".join(lines), encoding="utf-8")


def entry_to_db_record(
    paper: Paper, note_path: Path, status: str = "auto", summary_source: str = "fallback"
) -> dict[str, Any]:
    return {
        "id": paper.paper_id,
        "title": paper.title,
        "summary": paper.summary,
        "authors": paper.authors,
        "published": paper.published,
        "updated": paper.updated,
        "link": paper.link,
        "tags": paper.tags,
        "score": paper.score,
        "status": status,
        "source_topic": paper.source_topic,
        "note_path": str(note_path),
        "summary_source": summary_source,
        "last_seen_at": now_utc().isoformat(),
    }


def load_db(db_path: Path) -> dict[str, Any]:
    db = load_json(db_path, default={})
    if not isinstance(db, dict):
        return {}
    return db


def save_db(db_path: Path, db: dict[str, Any]) -> None:
    save_json(db_path, db)


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    ensure_dirs(root / "data", root / "data" / "notes", root / "reports" / "daily")
    config_path = root / args.config
    if config_path.exists() and not args.force:
        print(f"Config already exists: {config_path}")
    else:
        save_json(config_path, DEFAULT_CONFIG)
        print(f"Initialized config: {config_path}")

    db_path = root / "data" / "paper_db.json"
    if not db_path.exists():
        save_json(db_path, {})
        print(f"Initialized DB: {db_path}")

    csv_path = root / "known_papers_template.csv"
    if not csv_path.exists():
        csv_path.write_text(
            "title,authors,year,link,tags,notes\n"
            'Example Paper,"Author A; Author B",2024,https://arxiv.org/abs/2401.00001,'
            '"molecular_dynamics;tensile_simulation",Optional notes\n',
            encoding="utf-8",
        )
        print(f"Created template CSV: {csv_path}")

    print("Initialization complete.")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    config_path = root / args.config
    db_path = root / "data" / "paper_db.json"
    notes_dir = root / "data" / "notes"
    daily_dir = root / "reports" / "daily"
    map_path = root / "reports" / "knowledge_system.md"

    cfg = load_config(config_path)
    db = load_db(db_path)
    ensure_dirs(notes_dir, daily_dir, map_path.parent)

    all_entries: list[Paper] = []
    timeout_sec = int(cfg.get("request_timeout_sec", 30))
    per_topic = int(cfg.get("arxiv_max_results_per_topic", 40))
    today = now_utc()
    taxonomy = cfg.get("taxonomy", {})
    labels = cfg.get("topic_labels", {})
    kw = cfg.get("keyword_weights", {})

    for topic in cfg.get("topics", []):
        topic_name = topic.get("name", "unknown_topic")
        query = topic.get("query", "")
        if not query:
            continue
        try:
            topic_entries = fetch_arxiv_entries(query=query, max_results=per_topic, timeout_sec=timeout_sec)
        except urllib.error.URLError as err:
            print(f"[WARN] fetch failed for topic={topic_name}: {err}")
            continue
        except Exception as err:
            print(f"[WARN] unexpected fetch error for topic={topic_name}: {err}")
            continue

        for paper in topic_entries:
            paper.source_topic = topic_name
            paper.tags = extract_tags(f"{paper.title}\n{paper.summary}", taxonomy)
            if not paper.tags:
                paper.tags = [topic_name]
            paper.score = score_paper(paper, keyword_weights=kw, today=today)
        all_entries.extend(topic_entries)

    if not all_entries:
        print("No entries fetched from arXiv. Knowledge map will still be rebuilt.")
        rebuild_knowledge_map(map_path=map_path, db=db, topic_labels=labels)
        return 0

    deduped = dedupe_entries(all_entries)
    daily_limit = int(args.limit or cfg.get("daily_limit", 5))
    selected = select_new_papers(deduped, db, daily_limit=daily_limit)

    for paper in selected:
        note_path = notes_dir / f"{slugify(paper.paper_id)}.md"
        summary_text = maybe_summarize_with_llm(paper, cfg=cfg, labels=labels)
        summary_source = "llm" if os.getenv(cfg.get("llm", {}).get("api_key_env", "OPENAI_API_KEY")) else "fallback"
        write_paper_note(note_path=note_path, paper=paper, summary_text=summary_text)
        db[paper.paper_id] = entry_to_db_record(
            paper=paper,
            note_path=note_path,
            status="auto",
            summary_source=summary_source,
        )

    # Refresh last_seen timestamp for already-known entries that appeared today.
    deduped_by_id = {p.paper_id: p for p in deduped}
    for paper_id in sorted(set(db.keys()) & set(deduped_by_id.keys())):
        db[paper_id]["last_seen_at"] = now_utc().isoformat()
        if "score" in db[paper_id]:
            db[paper_id]["score"] = max(float(db[paper_id]["score"]), float(deduped_by_id[paper_id].score))

    save_db(db_path, db)
    rebuild_knowledge_map(map_path=map_path, db=db, topic_labels=labels)
    report_path = daily_dir / f"{dt.date.today().isoformat()}.md"
    write_daily_report(report_path=report_path, selected=selected, db=db, labels=labels)

    print(f"Fetched entries: {len(all_entries)}")
    print(f"Deduped entries: {len(deduped)}")
    print(f"New papers added today: {len(selected)}")
    print(f"DB path: {db_path}")
    print(f"Knowledge map: {map_path}")
    print(f"Daily report: {report_path}")
    return 0


def derive_id_from_link_or_text(link: str, text: str) -> str:
    if link and "arxiv.org/abs/" in link:
        return parse_arxiv_id(link)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"known-{digest}"


def cmd_ingest_known(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    config_path = root / args.config
    db_path = root / "data" / "paper_db.json"
    notes_dir = root / "data" / "notes"
    map_path = root / "reports" / "knowledge_system.md"
    csv_path = Path(args.csv).resolve()

    cfg = load_config(config_path)
    db = load_db(db_path)
    ensure_dirs(notes_dir, map_path.parent)

    if not csv_path.exists():
        print(f"CSV file not found: {csv_path}")
        return 1

    labels = cfg.get("topic_labels", {})
    imported = 0
    with csv_path.open("r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            title = compact_whitespace(row.get("title", ""))
            if not title:
                continue
            authors_raw = row.get("authors", "")
            authors = [compact_whitespace(x) for x in authors_raw.split(";") if x.strip()]
            year = compact_whitespace(row.get("year", ""))
            link = compact_whitespace(row.get("link", ""))
            tags_raw = compact_whitespace(row.get("tags", ""))
            notes = compact_whitespace(row.get("notes", ""))
            paper_id = derive_id_from_link_or_text(link, f"{title}-{authors_raw}-{year}")
            tags = [compact_whitespace(t) for t in tags_raw.split(";") if t.strip()]
            if not tags:
                tags = ["multiphysics_coupling"]

            published = f"{year}-01-01T00:00:00+00:00" if re.match(r"^\d{4}$", year) else ""
            paper = Paper(
                paper_id=paper_id,
                title=title,
                summary=notes or "Imported known paper.",
                authors=authors,
                published=published,
                updated="",
                link=link or "N/A",
                source_topic="known_import",
                tags=tags,
                score=0.0,
            )
            note_path = notes_dir / f"{slugify(paper.paper_id)}.md"
            summary_text = textwrap.dedent(
                f"""\
                1) Core Question
                - Imported from known-paper list.

                2) Why Kept In System
                - This paper was marked as already known by user.

                3) Notes
                - {notes or "No extra notes provided."}
                """
            ).strip()
            write_paper_note(note_path, paper, summary_text=summary_text)
            db[paper.paper_id] = entry_to_db_record(
                paper=paper,
                note_path=note_path,
                status="known",
                summary_source="known_import",
            )
            imported += 1

    save_db(db_path, db)
    rebuild_knowledge_map(map_path=map_path, db=db, topic_labels=labels)
    print(f"Imported known papers: {imported}")
    print(f"DB path: {db_path}")
    print(f"Knowledge map: {map_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Daily paper update and research-system builder for materials simulation."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", default=".", help="Project root directory.")
        p.add_argument("--config", default="config.json", help="Relative path to config JSON.")

    p_init = sub.add_parser("init", help="Initialize config and data folders.")
    add_common(p_init)
    p_init.add_argument("--force", action="store_true", help="Overwrite config if it exists.")
    p_init.set_defaults(func=cmd_init)

    p_update = sub.add_parser("update", help="Fetch and process today's papers.")
    add_common(p_update)
    p_update.add_argument("--limit", type=int, default=0, help="Override daily paper limit.")
    p_update.set_defaults(func=cmd_update)

    p_known = sub.add_parser("ingest-known", help="Import known papers from CSV.")
    add_common(p_known)
    p_known.add_argument("--csv", required=True, help="Path to known papers CSV file.")
    p_known.set_defaults(func=cmd_ingest_known)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
