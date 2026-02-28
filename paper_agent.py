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
import subprocess
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
DEFAULT_ENV_FILE = Path("~/.clawdbot/.env").expanduser()


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
    "keyword_settings": {
        "max_keywords_per_paper": 8,
        "daily_report_keywords": 5,
        "telegram_keywords": 3,
    },
    "keyword_explanations": {
        "multiphysics": "指热-力-电-化学等多个物理场耦合求解，可用于评估场间耦合效应与失效机制。",
        "molecular dynamics": "基于原子相互作用的时间演化模拟，用于揭示微观机理与参数敏感性。",
        "phase-field crystal": "相场晶体模型，适合描述原子尺度周期结构与缺陷长期演化。",
        "fatigue": "关注循环载荷下损伤累积、裂纹萌生与扩展过程。",
        "tensile": "关注拉伸工况下应力-应变响应、屈服行为和断裂特征。",
        "dislocation": "位错是塑性变形核心载体，位错演化直接影响材料强塑性与疲劳行为。",
        "crack": "裂纹相关研究通常用于评估断裂风险与寿命边界。",
        "interatomic potential": "原子势函数决定 MD 模拟精度与可迁移性，是材料模拟的关键基础。",
        "finite element": "有限元用于连续介质尺度求解，可与微观模型形成多尺度桥接。",
        "machine learning": "机器学习用于势函数构建、代理建模或特征提取，可提高精度与效率。",
        "stress-strain": "应力-应变关系用于量化材料本构、屈服与硬化行为。",
        "cyclic loading": "循环加载用于研究疲劳损伤演化和寿命预测。",
        "thermo-mechanical": "热-力耦合可捕捉温度场对变形和损伤行为的影响。",
        "electrochemical": "电化学耦合常用于电池/腐蚀等场景，研究扩散-应力-反应协同机制。",
        "lammps": "LAMMPS 是常用分子动力学平台，强调可复现的原子尺度模拟流程。",
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
    "notify": {
        "enabled": True,
        "send_when_no_new": True,
        "max_items": 5,
        "clawbot": {
            "binary": "/opt/homebrew/bin/clawdbot",
            "channel": "telegram",
            "target": "5717971233",
        },
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


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception:
        return


def load_default_envs() -> None:
    load_env_file(DEFAULT_ENV_FILE)


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


def normalize_keyword(keyword: str) -> str:
    out = compact_whitespace(keyword.lower())
    out = out.replace("_", " ")
    out = out.replace("phase field", "phase-field")
    return out


def keyword_pattern(keyword: str) -> str:
    escaped = re.escape(normalize_keyword(keyword))
    escaped = escaped.replace(r"\ ", r"\s+")
    escaped = escaped.replace(r"\-", r"[-\s]?")
    return escaped


def count_term_hits(text: str, keyword: str) -> int:
    pattern = keyword_pattern(keyword)
    return len(re.findall(pattern, text, flags=re.IGNORECASE))


def build_keyword_explanation(keyword: str, cfg_map: dict[str, str]) -> str:
    norm = normalize_keyword(keyword)
    if norm in cfg_map:
        return cfg_map[norm]

    if any(k in norm for k in ("fatigue", "cyclic")):
        return "该关键词与循环载荷损伤和寿命评估相关，建议重点关注损伤演化参数。"
    if any(k in norm for k in ("crack", "fracture")):
        return "该关键词对应断裂相关机制，建议关注裂纹判据、扩展路径和失效阈值。"
    if any(k in norm for k in ("molecular dynamics", "atomistic", "interatomic")):
        return "该关键词对应原子尺度模拟方法，建议记录势函数、时间步长和边界条件。"
    if any(k in norm for k in ("phase-field", "pfc")):
        return "该关键词对应相场类模型，建议关注自由能构造和演化方程设置。"
    if any(k in norm for k in ("tensile", "stress", "strain")):
        return "该关键词对应力学响应分析，建议重点提取本构与载荷路径信息。"
    if any(k in norm for k in ("multiphysics", "coupled", "thermo", "electro")):
        return "该关键词对应多场耦合问题，建议关注耦合项、控制方程与场变量映射。"
    if "machine learning" in norm:
        return "该关键词对应数据驱动方法，建议关注训练数据来源和泛化能力。"
    return "该关键词是论文核心技术要素，建议结合方法与结果章节提取可复现实验/仿真设置。"


def extract_keywords_with_explanations(
    paper: Paper, cfg: dict[str, Any], labels: dict[str, str]
) -> list[dict[str, Any]]:
    text = f"{paper.title}\n{paper.summary}"
    max_kw = int(cfg.get("keyword_settings", {}).get("max_keywords_per_paper", 8) or 8)
    max_kw = max(3, max_kw)

    keyword_weights = cfg.get("keyword_weights", {})
    taxonomy = cfg.get("taxonomy", {})
    expl_raw = cfg.get("keyword_explanations", {})
    expl_map = {normalize_keyword(k): str(v) for k, v in expl_raw.items()}

    merged: dict[str, dict[str, Any]] = {}

    def add_candidate(term: str, base_score: float, source: str) -> None:
        term = compact_whitespace(term)
        if not term:
            return
        hits = count_term_hits(text, term)
        if hits <= 0:
            return
        norm = normalize_keyword(term)
        phrase_bonus = min(1.2, 0.15 * max(0, len(norm.split()) - 1))
        score = float(base_score) + 0.5 * min(hits, 4) + phrase_bonus
        current = merged.get(norm)
        if current is None or score > current["score"]:
            merged[norm] = {
                "keyword": term,
                "score": round(score, 4),
                "hits": hits,
                "source": source,
            }

    for term, weight in keyword_weights.items():
        add_candidate(term, float(weight), "keyword_weights")

    for terms in taxonomy.values():
        for term in terms:
            add_candidate(str(term), 1.2, "taxonomy")

    for term in expl_map:
        add_candidate(term, 1.0, "explanation_map")

    for tag in paper.tags:
        label = labels.get(tag, tag)
        norm = normalize_keyword(label)
        if norm not in merged:
            merged[norm] = {
                "keyword": label,
                "score": 1.1,
                "hits": 1,
                "source": "topic_tag",
            }

    items = sorted(
        merged.values(),
        key=lambda x: (float(x["score"]), int(x["hits"]), len(str(x["keyword"]))),
        reverse=True,
    )

    if not items:
        title_tokens = re.findall(r"[A-Za-z][A-Za-z0-9\-]{3,}", paper.title)
        stop = {
            "with",
            "from",
            "into",
            "using",
            "based",
            "model",
            "models",
            "analysis",
            "approach",
            "study",
            "effect",
            "effects",
        }
        token_count: dict[str, int] = {}
        for t in title_tokens:
            n = normalize_keyword(t)
            if n in stop:
                continue
            token_count[n] = token_count.get(n, 0) + 1
        for token, cnt in sorted(token_count.items(), key=lambda kv: kv[1], reverse=True)[:max_kw]:
            items.append(
                {
                    "keyword": token,
                    "score": 1.0,
                    "hits": cnt,
                    "source": "title_token",
                }
            )

    final_items: list[dict[str, Any]] = []
    for item in items[:max_kw]:
        kw = str(item["keyword"])
        final_items.append(
            {
                "keyword": kw,
                "score": float(item["score"]),
                "hits": int(item["hits"]),
                "source": str(item["source"]),
                "explanation": build_keyword_explanation(kw, expl_map),
            }
        )
    return final_items


def infer_focus_areas(
    *, tags: list[str], keywords: list[dict[str, Any]], text: str, topic_labels: dict[str, str]
) -> list[str]:
    lowered = text.lower()
    keyword_text = " ".join(normalize_keyword(str(k.get("keyword", ""))) for k in keywords)
    blob = f"{lowered} {keyword_text}"

    rules = [
        (
            "多物理耦合与跨场耦合机制",
            ["multiphysics", "coupled", "thermo", "electrochemical", "fluid-structure"],
        ),
        ("分子动力学与原子尺度机制", ["molecular dynamics", "atomistic", "interatomic", "lammps"]),
        ("相场晶体与组织演化", ["phase-field", "phase field", "pfc", "amplitude expansion"]),
        ("疲劳损伤与断裂演化", ["fatigue", "cyclic", "crack", "fracture"]),
        ("拉伸响应与本构行为", ["tensile", "stress-strain", "strain hardening", "uniaxial", "deformation"]),
        ("数据驱动与机器学习建模", ["machine learning", "data-driven", "neural", "surrogate"]),
    ]

    focus: list[str] = []
    for label, needles in rules:
        if any(n in blob for n in needles):
            focus.append(label)

    for tag in tags:
        maybe = topic_labels.get(tag)
        if maybe and maybe not in focus:
            focus.append(f"{maybe}（主题归类）")

    if not focus:
        focus = ["通用材料模拟与数值建模"]
    return focus


def published_year(value: str) -> str:
    d = parse_datetime(value)
    if d:
        return str(d.year)
    m = re.search(r"\b(\d{4})\b", value or "")
    return m.group(1) if m else "Unknown"


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


def split_sentences(text: str) -> list[str]:
    normalized = compact_whitespace(text)
    if not normalized:
        return []
    parts = re.split(r"(?<=[\.\!\?。！？;；])\s+", normalized)
    return [p.strip() for p in parts if p.strip()]


def infer_method_signal_cn(abstract: str) -> str:
    lowered = abstract.lower()
    hints = []
    patterns = [
        ("molecular dynamics", "原子尺度分子动力学模拟"),
        ("phase-field crystal", "相场晶体模型"),
        ("phase field crystal", "相场晶体模型"),
        ("finite element", "有限元建模"),
        ("crack", "裂纹起裂/扩展分析"),
        ("fatigue", "疲劳循环加载分析"),
        ("tensile", "拉伸/变形工况"),
        ("multiphysics", "多物理耦合求解"),
        ("dislocation", "位错演化分析"),
    ]
    for needle, label in patterns:
        if needle in lowered:
            hints.append(label)
    if hints:
        return "；".join(hints)
    return "摘要中方法线索较弱，建议优先查看方法与模型假设部分。"


def infer_research_value_cn(tags: list[str]) -> str:
    reason_map = {
        "multiphysics_coupling": "可补充你在多物理耦合建模与场间耦合机制方面的方法库。",
        "molecular_dynamics": "可用于完善你分子动力学参数设定、势函数选择和微观机理解释。",
        "phase_field_crystal": "可补充你在相场晶体框架下的缺陷演化和组织演变建模思路。",
        "metal_fatigue": "可直接服务于金属疲劳寿命预测、裂纹演化与循环载荷分析。",
        "tensile_simulation": "可用于优化拉伸模拟中的本构拟合、边界条件与失效判据。",
    }
    reasons = [reason_map[t] for t in tags if t in reason_map]
    if reasons:
        return " ".join(dict.fromkeys(reasons))
    return "可作为材料模拟通用参考，帮助你补全“模型-参数-结果解释”的知识链条。"


def infer_finding_signal_cn(abstract: str) -> str:
    lowered = abstract.lower()
    if any(k in lowered for k in ("outperform", "improve", "higher accuracy", "better")):
        return "结果显示该方法在精度或稳定性上优于对比基线。"
    if any(k in lowered for k in ("fatigue", "crack", "fracture")):
        return "结果聚焦疲劳/裂纹演化机制，可用于失效分析与寿命评估。"
    if any(k in lowered for k in ("tensile", "stress-strain", "deformation")):
        return "结果给出了拉伸变形响应与关键力学指标变化趋势。"
    if any(k in lowered for k in ("phase-field crystal", "phase field crystal", "pfc")):
        return "结果体现了微观结构或缺陷演化规律，对组织演变建模有参考价值。"
    if any(k in lowered for k in ("molecular dynamics", "atomistic")):
        return "结果提供了原子尺度机理解释，可辅助参数选型和机理验证。"
    return "摘要显示其给出了可复现的模型/仿真结果，建议细读结果与讨论章节获取定量结论。"


def parse_cn_sections(summary_text: str) -> dict[str, str]:
    patterns = {
        "what_done": r"1\)\s*论文做了什么\s*(.*?)(?=\n\s*2\)|\Z)",
        "method": r"2\)\s*用了什么方法\s*(.*?)(?=\n\s*3\)|\Z)",
        "finding": r"3\)\s*得到什么结果\s*(.*?)(?=\n\s*4\)|\Z)",
        "meaning": r"4\)\s*对我研究的意义\s*(.*?)(?=\n\s*5\)|\Z)",
    }
    out: dict[str, str] = {}
    for key, pattern in patterns.items():
        m = re.search(pattern, summary_text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            value = compact_whitespace(m.group(1).strip(" -\n\t"))
            if value:
                out[key] = value
    return out


def build_cn_brief(paper: Paper, labels: dict[str, str]) -> dict[str, str]:
    abstract = paper.summary.strip()
    topics = [labels.get(tag, tag) for tag in paper.tags] or ["通用材料模拟"]
    method = infer_method_signal_cn(abstract)
    what_done = f"该论文围绕“{paper.title}”展开，针对{', '.join(topics)}相关问题进行建模与分析。"
    finding = infer_finding_signal_cn(abstract)
    return {
        "what_done": truncate_text(what_done, 220),
        "method": method,
        "finding": truncate_text(finding, 220),
        "meaning": truncate_text(infer_research_value_cn(paper.tags), 220),
        "topics": ", ".join(topics),
    }


def refine_brief_with_summary(brief: dict[str, str], summary_text: str) -> dict[str, str]:
    sections = parse_cn_sections(summary_text)
    if not sections:
        return brief
    updated = dict(brief)
    for key in ("what_done", "method", "finding", "meaning"):
        value = sections.get(key)
        if value:
            updated[key] = truncate_text(value, 220)
    return updated


def build_fallback_summary(paper: Paper, labels: dict[str, str]) -> str:
    brief = build_cn_brief(paper, labels)
    abstract = paper.summary.strip() or "N/A"
    abstract_preview = truncate_text(abstract, 1000)
    return textwrap.dedent(
        f"""\
        1) 论文做了什么
        - {brief["what_done"]}

        2) 核心方法
        - {brief["method"]}

        3) 关键结果
        - {brief["finding"]}

        4) 对你研究的意义
        - {brief["meaning"]}
        - 主题归类: {brief["topics"]}

        5) 摘要快照
        - {abstract_preview}
        """
    ).strip()


def maybe_summarize_with_llm(
    paper: Paper, cfg: dict[str, Any], labels: dict[str, str]
) -> tuple[str, str]:
    llm_cfg = cfg.get("llm", {})
    if not llm_cfg.get("enabled", True):
        return build_fallback_summary(paper, labels), "fallback"

    api_key_env = llm_cfg.get("api_key_env", "OPENAI_API_KEY")
    api_key = os.getenv(api_key_env, "")
    if not api_key:
        return build_fallback_summary(paper, labels), "fallback"

    endpoint = llm_cfg.get("endpoint", "https://api.openai.com/v1/responses")
    model = llm_cfg.get("model", "gpt-4.1-mini")
    temperature = llm_cfg.get("temperature", 0.2)
    prompt = textwrap.dedent(
        f"""\
        你是材料模拟方向的论文助理，请用中文输出，严格按以下结构总结：
        1) 论文做了什么
        2) 用了什么方法
        3) 得到什么结果
        4) 对我研究的意义
        5) 下一步阅读建议

        要求：
        - 每部分 1-3 条，精炼具体，避免空话。
        - 优先提取模型类型、边界条件、载荷方式、关键变量、可复现实验/仿真线索。
        - 第4部分必须结合我的方向：多物理耦合、分子动力学、相场晶体、金属疲劳、拉伸模拟。

        论文标题: {paper.title}
        发布时间: {paper.published}
        作者: {", ".join(paper.authors)}
        主题标签: {", ".join(labels.get(tag, tag) for tag in paper.tags) or "N/A"}
        摘要:
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
                    {"type": "input_text", "text": "只输出中文纯文本，不要使用 Markdown 代码块。"}
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
                return output_text.strip(), "llm"
    except urllib.error.HTTPError as err:
        return build_fallback_summary(paper, labels), f"fallback_http_{err.code}"
    except urllib.error.URLError:
        return build_fallback_summary(paper, labels), "fallback_network"
    except Exception:
        return build_fallback_summary(paper, labels), "fallback_error"


def parse_response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return payload["output_text"]
    for out in payload.get("output", []):
        for content in out.get("content", []):
            maybe_text = content.get("text")
            if isinstance(maybe_text, str) and maybe_text.strip():
                return maybe_text
    return ""


def truncate_text(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    return value[: max_len - 3].rstrip() + "..."


def build_daily_reminder_message(
    *,
    selected: list[Paper],
    daily_limit: int,
    labels: dict[str, str],
    report_path: Path,
    max_items: int,
    db: dict[str, Any] | None = None,
    keyword_limit: int = 3,
) -> str:
    date_label = dt.date.today().isoformat()
    lines = [f"今日论文阅读提醒（{date_label}）", ""]
    if selected:
        lines.append(f"今天新增待读：{len(selected)} 篇（目标 {daily_limit} 篇）")
        lines.append("")
        for idx, paper in enumerate(selected[:max_items], start=1):
            brief = {}
            record: dict[str, Any] = {}
            if db and paper.paper_id in db:
                record = db[paper.paper_id]
                maybe_brief = record.get("brief_cn", {})
                if isinstance(maybe_brief, dict):
                    brief = maybe_brief
            if not brief:
                brief = build_cn_brief(paper, labels)
            keyword_items = record.get("keyword_details", []) if record else []
            focus_areas = record.get("focus_areas", []) if record else []
            keyword_names = [str(x.get("keyword", "")) for x in keyword_items if str(x.get("keyword", "")).strip()]
            keyword_preview = ", ".join(keyword_names[: max(1, keyword_limit)])
            focus_preview = "；".join(str(x) for x in focus_areas[:2]) if focus_areas else ""
            lines.append(f"{idx}. {truncate_text(paper.title, 120)}")
            lines.append(f"   做了什么：{truncate_text(brief['what_done'], 140)}")
            lines.append(f"   意义：{truncate_text(brief['meaning'], 140)}")
            if keyword_preview:
                lines.append(f"   关键词：{truncate_text(keyword_preview, 140)}")
            if focus_preview:
                lines.append(f"   侧重点：{truncate_text(focus_preview, 140)}")
            lines.append(f"   链接：{paper.link}")
            lines.append("")
        lines.append("建议：今天优先精读前 2 篇，并把关键参数和边界条件补进笔记。")
    else:
        lines.extend(
            [
                "今天没有筛选到新的未读论文。",
                "建议：复盘最近 3 篇笔记，更新你的“模型-参数-结论”体系表。",
            ]
        )
    lines.extend(["", f"今日日报：{report_path}"])
    message = "\n".join(lines).strip()
    if len(message) > 3800:
        message = message[:3700].rstrip() + "\n...\n（消息过长，已截断。完整内容见今日日报）"
    return message


def send_via_clawbot(
    *,
    message: str,
    claw_cfg: dict[str, Any],
    dry_run: bool,
    timeout_sec: int = 60,
) -> tuple[bool, str]:
    binary = str(claw_cfg.get("binary", "clawdbot")).strip()
    channel = str(claw_cfg.get("channel", "telegram")).strip()
    target = str(claw_cfg.get("target", "")).strip()
    if not binary:
        return False, "notify.clawbot.binary is empty"
    if not target:
        return False, "notify.clawbot.target is empty"

    cmd = [
        binary,
        "message",
        "send",
        "--channel",
        channel,
        "--target",
        target,
        "--message",
        message,
    ]
    if dry_run:
        cmd.append("--dry-run")
    cmd.append("--json")

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except FileNotFoundError:
        return False, f"clawbot binary not found: {binary}"
    except subprocess.TimeoutExpired:
        return False, "clawbot send timeout"

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return False, err or out or f"clawbot send failed, exit={proc.returncode}"
    return True, out or "sent"


def maybe_send_daily_reminder(
    *,
    cfg: dict[str, Any],
    db: dict[str, Any],
    selected: list[Paper],
    daily_limit: int,
    labels: dict[str, str],
    report_path: Path,
    force_notify: bool | None,
    dry_run: bool,
) -> tuple[bool, str]:
    notify_cfg = cfg.get("notify", {})
    enabled = bool(notify_cfg.get("enabled", False))
    if force_notify is not None:
        enabled = bool(force_notify)
    if not enabled:
        return True, "notification disabled"

    send_when_no_new = bool(notify_cfg.get("send_when_no_new", True))
    if not selected and not send_when_no_new:
        return True, "no new papers and send_when_no_new=false"

    max_items = int(notify_cfg.get("max_items", 5) or 5)
    max_items = max(1, max_items)
    telegram_kw = int(cfg.get("keyword_settings", {}).get("telegram_keywords", 3) or 3)
    message = build_daily_reminder_message(
        selected=selected,
        daily_limit=daily_limit,
        labels=labels,
        report_path=report_path,
        max_items=max_items,
        db=db,
        keyword_limit=max(1, telegram_kw),
    )
    claw_cfg = notify_cfg.get("clawbot", {})
    return send_via_clawbot(message=message, claw_cfg=claw_cfg, dry_run=dry_run)


def write_paper_note(
    path: Path,
    paper: Paper,
    summary_text: str,
    brief_cn: dict[str, str] | None = None,
    keyword_details: list[dict[str, Any]] | None = None,
    focus_areas: list[str] | None = None,
) -> None:
    ensure_dirs(path.parent)
    brief_cn = brief_cn or {}
    keyword_details = keyword_details or []
    focus_areas = focus_areas or []
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
        "## 中文速览",
        f"- 做了什么: {brief_cn.get('what_done', 'N/A')}",
        f"- 方法: {brief_cn.get('method', 'N/A')}",
        f"- 关键结果: {brief_cn.get('finding', 'N/A')}",
        f"- 对你的意义: {brief_cn.get('meaning', 'N/A')}",
        "",
        "## 关键词与解释",
    ]
    if keyword_details:
        for item in keyword_details:
            kw = str(item.get("keyword", "N/A"))
            expl = str(item.get("explanation", "N/A"))
            score = float(item.get("score", 0.0))
            hits = int(item.get("hits", 0))
            lines.append(f"- {kw} (score={score:.2f}, hits={hits}): {expl}")
    else:
        lines.append("- N/A")

    lines.extend(
        [
            "",
            "## 研究侧重点",
        ]
    )
    if focus_areas:
        for focus in focus_areas:
            lines.append(f"- {focus}")
    else:
        lines.append("- N/A")

    lines.extend(
        [
            "",
        "## Structured Summary",
        summary_text.strip(),
        "",
        "## Raw Abstract",
        paper.summary.strip() or "N/A",
        "",
        ]
    )
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
            brief = db.get(paper.paper_id, {}).get("brief_cn", {})
            keyword_details = db.get(paper.paper_id, {}).get("keyword_details", [])
            focus_areas = db.get(paper.paper_id, {}).get("focus_areas", [])
            keyword_names = [str(x.get("keyword", "")) for x in keyword_details if str(x.get("keyword", "")).strip()]
            done_cn = truncate_text(str(brief.get("what_done", "N/A")), 160)
            meaning_cn = truncate_text(str(brief.get("meaning", "N/A")), 160)
            kw_preview = truncate_text(", ".join(keyword_names[:5]), 200) if keyword_names else "N/A"
            focus_preview = truncate_text("；".join(focus_areas[:3]), 200) if focus_areas else "N/A"
            lines.extend(
                [
                    f"## {idx}. {paper.title}",
                    f"- ID: `{paper.paper_id}`",
                    f"- Tags: {', '.join(label_list) if label_list else 'N/A'}",
                    f"- Score: {paper.score}",
                    f"- Link: {paper.link}",
                    f"- Note: {note_rel}",
                    f"- 做了什么: {done_cn}",
                    f"- 对你的意义: {meaning_cn}",
                    f"- 关键词: {kw_preview}",
                    f"- 研究侧重点: {focus_preview}",
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


def rebuild_focus_year_summary(
    summary_path: Path, db: dict[str, Any], topic_labels: dict[str, str]
) -> None:
    ensure_dirs(summary_path.parent)
    records = list(db.values())

    by_year: dict[str, int] = {}
    by_focus: dict[str, int] = {}
    by_year_focus: dict[str, dict[str, int]] = {}
    keyword_count: dict[str, int] = {}
    topic_count: dict[str, int] = {}

    for r in records:
        year = published_year(str(r.get("published", "")))
        by_year[year] = by_year.get(year, 0) + 1

        tags = [str(x) for x in r.get("tags", []) if str(x).strip()]
        for tag in tags:
            topic_count[tag] = topic_count.get(tag, 0) + 1

        focus_areas = [str(x) for x in r.get("focus_areas", []) if str(x).strip()]
        if not focus_areas:
            for tag in tags:
                if tag in topic_labels:
                    focus_areas.append(f"{topic_labels[tag]}（主题归类）")
        if not focus_areas:
            focus_areas = ["通用材料模拟与数值建模"]

        for focus in focus_areas:
            by_focus[focus] = by_focus.get(focus, 0) + 1
            if year not in by_year_focus:
                by_year_focus[year] = {}
            by_year_focus[year][focus] = by_year_focus[year].get(focus, 0) + 1

        keywords = [str(x) for x in r.get("keywords", []) if str(x).strip()]
        if not keywords:
            for x in r.get("keyword_details", []):
                kw = str(x.get("keyword", "")).strip()
                if kw:
                    keywords.append(kw)
        for kw in keywords:
            keyword_count[kw] = keyword_count.get(kw, 0) + 1

    lines = ["# 论文研究侧重点与年份汇总", ""]
    lines.extend(
        [
            "该汇总由 `data/paper_db.json` 自动重建。",
            "",
            "## 总览",
            f"- 论文总数: {len(records)}",
            f"- 最近更新: {now_utc().isoformat()}",
            "",
        ]
    )

    lines.append("## 按年份统计")
    if by_year:
        for year, count in sorted(by_year.items(), key=lambda kv: kv[0], reverse=True):
            lines.append(f"- {year}: {count}")
    else:
        lines.append("- N/A")
    lines.append("")

    lines.append("## 按研究侧重点统计")
    if by_focus:
        for focus, count in sorted(by_focus.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"- {focus}: {count}")
    else:
        lines.append("- N/A")
    lines.append("")

    lines.append("## 按主题标签统计")
    if topic_count:
        for tag, count in sorted(topic_count.items(), key=lambda kv: kv[1], reverse=True):
            label = topic_labels.get(tag, tag)
            lines.append(f"- {label}: {count}")
    else:
        lines.append("- N/A")
    lines.append("")

    lines.append("## 高频关键词 (Top 30)")
    if keyword_count:
        for kw, count in sorted(keyword_count.items(), key=lambda kv: kv[1], reverse=True)[:30]:
            lines.append(f"- {kw}: {count}")
    else:
        lines.append("- N/A")
    lines.append("")

    lines.append("## 年份-侧重点分布")
    if by_year_focus:
        for year in sorted(by_year_focus.keys(), reverse=True):
            lines.append(f"### {year}")
            focus_map = by_year_focus[year]
            for focus, count in sorted(focus_map.items(), key=lambda kv: kv[1], reverse=True):
                lines.append(f"- {focus}: {count}")
            lines.append("")
    else:
        lines.append("- N/A")
        lines.append("")

    summary_path.write_text("\n".join(lines), encoding="utf-8")


def entry_to_db_record(
    paper: Paper,
    note_path: Path,
    status: str = "auto",
    summary_source: str = "fallback",
    brief_cn: dict[str, str] | None = None,
    keyword_details: list[dict[str, Any]] | None = None,
    focus_areas: list[str] | None = None,
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
        "brief_cn": brief_cn or {},
        "keyword_details": keyword_details or [],
        "keywords": [str(x.get("keyword", "")) for x in (keyword_details or []) if str(x.get("keyword", "")).strip()],
        "focus_areas": focus_areas or [],
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
    focus_summary_path = root / "reports" / "focus_year_summary.md"

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
        rebuild_focus_year_summary(summary_path=focus_summary_path, db=db, topic_labels=labels)
        return 0

    deduped = dedupe_entries(all_entries)
    daily_limit = int(args.limit or cfg.get("daily_limit", 5))
    selected = select_new_papers(deduped, db, daily_limit=daily_limit)

    for paper in selected:
        note_path = notes_dir / f"{slugify(paper.paper_id)}.md"
        summary_text, summary_source = maybe_summarize_with_llm(paper, cfg=cfg, labels=labels)
        keyword_details = extract_keywords_with_explanations(paper=paper, cfg=cfg, labels=labels)
        focus_areas = infer_focus_areas(
            tags=paper.tags,
            keywords=keyword_details,
            text=f"{paper.title}\n{paper.summary}",
            topic_labels=labels,
        )
        brief_cn = refine_brief_with_summary(build_cn_brief(paper, labels), summary_text)
        write_paper_note(
            path=note_path,
            paper=paper,
            summary_text=summary_text,
            brief_cn=brief_cn,
            keyword_details=keyword_details,
            focus_areas=focus_areas,
        )
        db[paper.paper_id] = entry_to_db_record(
            paper=paper,
            note_path=note_path,
            status="auto",
            summary_source=summary_source,
            brief_cn=brief_cn,
            keyword_details=keyword_details,
            focus_areas=focus_areas,
        )

    # Refresh last_seen timestamp for already-known entries that appeared today.
    deduped_by_id = {p.paper_id: p for p in deduped}
    for paper_id in sorted(set(db.keys()) & set(deduped_by_id.keys())):
        db[paper_id]["last_seen_at"] = now_utc().isoformat()
        if "score" in db[paper_id]:
            db[paper_id]["score"] = max(float(db[paper_id]["score"]), float(deduped_by_id[paper_id].score))

    save_db(db_path, db)
    rebuild_knowledge_map(map_path=map_path, db=db, topic_labels=labels)
    rebuild_focus_year_summary(summary_path=focus_summary_path, db=db, topic_labels=labels)
    report_path = daily_dir / f"{dt.date.today().isoformat()}.md"
    write_daily_report(report_path=report_path, selected=selected, db=db, labels=labels)

    ok, notify_msg = maybe_send_daily_reminder(
        cfg=cfg,
        db=db,
        selected=selected,
        daily_limit=daily_limit,
        labels=labels,
        report_path=report_path,
        force_notify=args.notify,
        dry_run=bool(args.notify_dry_run),
    )

    print(f"Fetched entries: {len(all_entries)}")
    print(f"Deduped entries: {len(deduped)}")
    print(f"New papers added today: {len(selected)}")
    print(f"DB path: {db_path}")
    print(f"Knowledge map: {map_path}")
    print(f"Focus-year summary: {focus_summary_path}")
    print(f"Daily report: {report_path}")
    if ok:
        print(f"Notify: {notify_msg}")
    else:
        print(f"[WARN] Notify failed: {notify_msg}")
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
    focus_summary_path = root / "reports" / "focus_year_summary.md"
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
                1) 论文做了什么
                - 该条目来自你的已读论文清单导入。

                2) 为什么纳入体系
                - 你已明确标记为“已了解论文”，用于建立连续知识图谱。

                3) 你的备注
                - {notes or "暂未提供额外备注。"}
                """
            ).strip()
            brief_cn = {
                "what_done": "这篇论文由你的已读清单导入。",
                "method": "待你补充该论文采用的核心模型与参数设置。",
                "finding": notes or "暂无详细结论记录。",
                "meaning": "用于补全你的长期论文体系，并与每日新论文建立关联。",
            }
            keyword_details = extract_keywords_with_explanations(paper=paper, cfg=cfg, labels=labels)
            if not keyword_details:
                keyword_details = [
                    {
                        "keyword": "known_import",
                        "score": 1.0,
                        "hits": 1,
                        "source": "known_import",
                        "explanation": "该条目来自已读论文导入，用于补齐历史知识体系。",
                    }
                ]
            focus_areas = infer_focus_areas(
                tags=paper.tags,
                keywords=keyword_details,
                text=f"{paper.title}\n{paper.summary}",
                topic_labels=labels,
            )
            write_paper_note(
                path=note_path,
                paper=paper,
                summary_text=summary_text,
                brief_cn=brief_cn,
                keyword_details=keyword_details,
                focus_areas=focus_areas,
            )
            db[paper.paper_id] = entry_to_db_record(
                paper=paper,
                note_path=note_path,
                status="known",
                summary_source="known_import",
                brief_cn=brief_cn,
                keyword_details=keyword_details,
                focus_areas=focus_areas,
            )
            imported += 1

    save_db(db_path, db)
    rebuild_knowledge_map(map_path=map_path, db=db, topic_labels=labels)
    rebuild_focus_year_summary(summary_path=focus_summary_path, db=db, topic_labels=labels)
    print(f"Imported known papers: {imported}")
    print(f"DB path: {db_path}")
    print(f"Knowledge map: {map_path}")
    print(f"Focus-year summary: {focus_summary_path}")
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
    p_update.add_argument("--notify", dest="notify", action="store_true", help="Force-enable reminder send.")
    p_update.add_argument("--no-notify", dest="notify", action="store_false", help="Disable reminder send.")
    p_update.add_argument(
        "--notify-dry-run",
        action="store_true",
        help="Build and route reminder in dry-run mode (no actual send).",
    )
    p_update.set_defaults(notify=None, notify_dry_run=False)
    p_update.set_defaults(func=cmd_update)

    p_known = sub.add_parser("ingest-known", help="Import known papers from CSV.")
    add_common(p_known)
    p_known.add_argument("--csv", required=True, help="Path to known papers CSV file.")
    p_known.set_defaults(func=cmd_ingest_known)

    return parser


def main() -> int:
    load_default_envs()
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
