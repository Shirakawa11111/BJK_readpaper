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
import shutil
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
        "provider": "anthropic",
        "api_key_env": "ANTHROPIC_API_KEY",
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 2500,
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
    "group_style": {
        "max_parameters_per_paper": 6,
        "daily_report_params_chars": 160,
        "telegram_params_chars": 90,
    },
    "pdf_parsing": {
        "enabled": True,
        "cache_dir": "data/pdf_cache",
        "download_timeout_sec": 60,
        "ghostscript_timeout_sec": 180,
        "max_param_items": 10,
        "max_pdf_pages": 30,
        "max_existing_parse_per_run": 3,
        "refresh_existing": False,
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
        "telegram": {
            "bot_token_env": "TELEGRAM_BOT_TOKEN",
            "chat_id_env": "TELEGRAM_CHAT_ID",
        },
    },
    "visualization": {
        "dashboard_html": True,
        "mermaid_graph": True,
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


def guess_pdf_url(paper: Paper) -> str:
    if "arxiv.org/abs/" in paper.link:
        url = paper.link.replace("/abs/", "/pdf/")
        if not url.endswith(".pdf"):
            url += ".pdf"
        return url
    if paper.paper_id:
        return f"https://arxiv.org/pdf/{paper.paper_id}.pdf"
    return ""


def download_pdf(url: str, output_path: Path, timeout_sec: int) -> tuple[bool, str]:
    if output_path.exists() and output_path.stat().st_size > 0:
        return True, "cache_pdf"
    ensure_dirs(output_path.parent)
    if not url:
        return False, "empty_pdf_url"
    req = urllib.request.Request(url, headers={"User-Agent": "paper-agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = resp.read()
        if not data:
            return False, "empty_pdf_bytes"
        output_path.write_bytes(data)
        return True, "downloaded_pdf"
    except urllib.error.HTTPError as err:
        return False, f"http_{err.code}"
    except urllib.error.URLError as err:
        return False, f"url_error:{err}"
    except Exception as err:
        return False, f"download_error:{err}"


def extract_pdf_text_with_pypdf(pdf_path: Path, max_pages: int = 0) -> tuple[str, str]:
    reader = None
    source = ""
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(pdf_path))
        source = "pypdf"
    except Exception:
        try:
            from PyPDF2 import PdfReader  # type: ignore

            reader = PdfReader(str(pdf_path))
            source = "PyPDF2"
        except Exception:
            return "", "no_python_pdf_reader"

    if reader is None:
        return "", "no_python_pdf_reader"
    chunks: list[str] = []
    try:
        pages = reader.pages
        limit = max_pages if max_pages and max_pages > 0 else len(pages)
        for idx, page in enumerate(pages):
            if idx >= limit:
                break
            txt = page.extract_text() or ""
            if txt.strip():
                chunks.append(txt)
    except Exception as err:
        return "", f"{source}_extract_error:{err}"
    return "\n".join(chunks), source


def extract_pdf_text_with_pdftotext(pdf_path: Path) -> tuple[str, str]:
    binary = shutil.which("pdftotext")
    if not binary:
        return "", "pdftotext_not_found"
    cmd = [binary, "-layout", str(pdf_path), "-"]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception as err:
        return "", f"pdftotext_error:{err}"
    if proc.returncode != 0:
        return "", f"pdftotext_exit_{proc.returncode}"
    return proc.stdout or "", "pdftotext"


def extract_pdf_text_with_ghostscript(
    pdf_path: Path, max_pages: int = 0, timeout_sec: int = 180
) -> tuple[str, str]:
    binary = shutil.which("gs")
    if not binary:
        return "", "ghostscript_not_found"
    cmd = [
        binary,
        "-q",
        "-dNOPAUSE",
        "-dBATCH",
        "-sDEVICE=txtwrite",
        "-dFirstPage=1",
    ]
    if max_pages and max_pages > 0:
        cmd.append(f"-dLastPage={max_pages}")
    cmd.extend(["-sOutputFile=-", str(pdf_path)])
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(30, int(timeout_sec)),
            check=False,
        )
    except Exception as err:
        return "", f"ghostscript_error:{err}"

    text = proc.stdout or ""
    if proc.returncode != 0 and not text.strip():
        return "", f"ghostscript_exit_{proc.returncode}"
    if not text.strip():
        return "", "ghostscript_empty"
    return text, "ghostscript_txtwrite"


def extract_pdf_text(
    pdf_path: Path, max_pages: int = 0, ghostscript_timeout_sec: int = 180
) -> tuple[str, str]:
    attempts: list[str] = []

    text, source = extract_pdf_text_with_pypdf(pdf_path, max_pages=max_pages)
    attempts.append(source)
    if text.strip():
        return text, source

    text2, source2 = extract_pdf_text_with_pdftotext(pdf_path)
    attempts.append(source2)
    if text2.strip():
        return text2, source2

    text3, source3 = extract_pdf_text_with_ghostscript(
        pdf_path, max_pages=max_pages, timeout_sec=ghostscript_timeout_sec
    )
    attempts.append(source3)
    if text3.strip():
        return text3, source3

    reason = "; ".join([x for x in attempts if x])
    return "", reason or "extract_failed"


def collect_keyword_context(text: str, keywords: list[str], window: int = 800) -> str:
    lowered = text.lower()
    snippets: list[str] = []
    seen_ranges: list[tuple[int, int]] = []
    for keyword in keywords:
        k = keyword.lower().strip()
        if not k:
            continue
        start = 0
        count = 0
        while True:
            idx = lowered.find(k, start)
            if idx < 0:
                break
            left = max(0, idx - window)
            right = min(len(text), idx + len(k) + window)
            start = idx + len(k)
            if any(not (right < a or left > b) for a, b in seen_ranges):
                continue
            seen_ranges.append((left, right))
            snippets.append(text[left:right])
            count += 1
            if count >= 3:
                break
    return "\n".join(snippets)


def extract_method_or_experimental_sections(
    text: str, max_chars: int = 20000
) -> str:
    lines = text.splitlines()
    if not lines:
        return ""

    heading_method = re.compile(
        r"^\s*(?:\d+(?:\.\d+)*\s*)?"
        r"(methods?|methodology|materials?\s+and\s+methods?|"
        r"simulation\s+details?|computational\s+details?|"
        r"experimental(?:\s+setup|\s+details|\s+procedure|\s+procedures)?|"
        r"numerical\s+method(?:s)?|"
        r"model(?:\s+(?:setup|implementation|configuration|formulation))?|"
        r"governing\s+equation(?:s)?)"
        r"(?:\s*[:\-]\s*[A-Za-z0-9\-\s,()]{0,60})?\s*$",
        flags=re.IGNORECASE,
    )
    heading_numbered = re.compile(
        r"^\s*\d+(?:\.\d+)*\s+[A-Z][A-Za-z0-9\-\s,/:()]{2,90}\s*$"
    )
    heading_named = re.compile(
        r"^\s*(abstract|introduction|results(?:\s+and\s+discussion)?|discussion|conclusions?|appendix)\s*$",
        flags=re.IGNORECASE,
    )
    section_end_markers = re.compile(
        r"^\s*(references|acknowledg(?:e)?ments?)\s*$", flags=re.IGNORECASE
    )

    candidates: list[str] = []
    starts = [idx for idx, line in enumerate(lines) if heading_method.match(line)]
    for start in starts:
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if section_end_markers.match(lines[j]):
                end = j
                break
            # stop at next top-level heading to avoid swallowing full paper
            if heading_numbered.match(lines[j]) or heading_named.match(lines[j]):
                if not heading_method.match(lines[j]):
                    end = j
                    break
        block = "\n".join(lines[start:end]).strip()
        if len(block) >= 120:
            candidates.append(block)

    if not candidates:
        return ""

    merged = "\n\n".join(candidates)
    if max_chars > 0 and len(merged) > max_chars:
        return merged[:max_chars]
    return merged


def extract_parameter_lines(text: str, max_items: int = 10) -> list[str]:
    normalized = compact_whitespace(text)
    if not normalized:
        return []
    clauses = re.split(r"(?<=[\.\;\n])\s+", normalized)
    clauses = [c.strip() for c in clauses if c.strip()]

    category_patterns = [
        (
            "边界条件",
            [
                r"\bboundary\s+condition(?:s)?\b",
                r"\bperiodic\b",
                r"\bpbc\b",
                r"\bdirichlet\b",
                r"\bneumann\b",
                r"\bfree\s+surface\b",
                r"\bfixed\s+boundary\b",
            ],
        ),
        (
            "网格/离散",
            [
                r"\bmesh(?:es)?\b",
                r"\bgrid\b",
                r"\belement(?:s)?\b",
                r"\bcell\s+size\b",
                r"\bresolution\b",
                r"\bnode(?:s)?\b",
                r"\blattice\s+spacing\b",
                r"\bfinite\s+element\b",
            ],
        ),
        (
            "时间步/积分",
            [
                r"\btime[\s-]?step\b",
                r"\btimestep\b",
                r"\bintegration(?:\s+step)?\b",
                r"\bdelta\s*t\b",
                r"\bvelocity[\s-]?verlet\b",
                r"\bexplicit\s+scheme\b",
                r"\bimplicit\s+scheme\b",
            ],
        ),
        (
            "势函数/模型版本",
            [
                r"\bpotential\b",
                r"\breaxff\b",
                r"\beam\b",
                r"\bmeam\b",
                r"\btersoff\b",
                r"\bstillinger(?:-|\s)?weber\b",
                r"\blennard(?:-|\s)?jones\b",
                r"\bgap\b",
                r"\bmtp\b",
                r"\bforce\s+field\b",
            ],
        ),
        (
            "载荷路径",
            [
                r"\bstrain\s+rate\b",
                r"\bloading\b",
                r"\bcyclic\b",
                r"\buniaxial\b",
                r"\bdisplacement(?:-|\s)?controlled\b",
                r"\bstress(?:-|\s)?controlled\b",
                r"\btensile\b",
                r"\bcompression\b",
                r"\bloading\s+path\b",
            ],
        ),
        (
            "热力学条件",
            [
                r"\btemperature\b",
                r"\bpressure\b",
                r"\bthermostat\b",
                r"\bbarostat\b",
                r"\bnvt\b",
                r"\bnpt\b",
                r"\banneal(?:ing)?\b",
            ],
        ),
        (
            "尺寸/样本规模",
            [
                r"\batom(?:s)?\b",
                r"\bparticle\b",
                r"\bdiameter\b",
                r"\bradius\b",
                r"\bthickness\b",
                r"\bsample(?:s)?\b",
                r"\bcycle(?:s)?\b",
            ],
        ),
    ]
    number_required = {"网格/离散", "时间步/积分", "热力学条件", "尺寸/样本规模"}

    candidates: list[str] = []
    for clause in clauses:
        lc = clause.lower()
        if len(clause) < 20:
            continue
        has_number = bool(re.search(r"\d", clause))
        for cat, pats in category_patterns:
            if any(re.search(p, lc, flags=re.IGNORECASE) for p in pats):
                if cat in number_required and not has_number:
                    continue
                candidates.append(f"{cat}: {truncate_text(clause, 170)}")
                break

    numeric_patterns = [
        r"\b\d+(?:\.\d+)?(?:e[+-]?\d+)?\s*(?:-|to|–|~)\s*\d+(?:\.\d+)?(?:e[+-]?\d+)?\s*(K|nm|um|μm|eV|GPa|MPa|Pa|Hz|kHz|MHz|GHz|V|mV|A|mA|s|ms|us|ns|ps|fs|%|s\^-1|1/s)\b",
        r"\b\d+(?:\.\d+)?(?:e[+-]?\d+)?\s*(K|nm|um|μm|eV|GPa|MPa|Pa|Hz|kHz|MHz|GHz|V|mV|A|mA|s|ms|us|ns|ps|fs|%|s\^-1|1/s)\b",
        r"\b\d+\s*(atoms|cycles|steps|samples)\b",
    ]
    for pat in numeric_patterns:
        for m in re.finditer(pat, normalized, flags=re.IGNORECASE):
            token = compact_whitespace(m.group(0))
            if token:
                candidates.append(f"数值参数: {token}")

    out: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        key = c.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= max(1, max_items):
            break
    return out


def extract_pdf_parameter_details(
    paper: Paper, root: Path, cfg: dict[str, Any]
) -> dict[str, Any]:
    pdf_cfg = cfg.get("pdf_parsing", {})
    if not bool(pdf_cfg.get("enabled", True)):
        return {"status": "skip_disabled", "param_details": [], "method_excerpt": "", "source": "disabled"}

    cache_dir = root / str(pdf_cfg.get("cache_dir", "data/pdf_cache"))
    timeout_sec = int(pdf_cfg.get("download_timeout_sec", 60) or 60)
    gs_timeout_sec = int(pdf_cfg.get("ghostscript_timeout_sec", 180) or 180)
    refresh = bool(pdf_cfg.get("refresh_existing", False))
    max_items = int(pdf_cfg.get("max_param_items", 10) or 10)
    max_pages = int(pdf_cfg.get("max_pdf_pages", 30) or 30)

    pdf_url = guess_pdf_url(paper)
    if not pdf_url:
        return {"status": "skip_no_pdf_url", "param_details": [], "method_excerpt": "", "source": "no_url"}

    pdf_path = cache_dir / f"{slugify(paper.paper_id)}.pdf"
    txt_path = cache_dir / f"{slugify(paper.paper_id)}.txt"

    ok, download_status = download_pdf(pdf_url, pdf_path, timeout_sec=timeout_sec)
    if not ok:
        return {
            "status": f"download_failed:{download_status}",
            "param_details": [],
            "method_excerpt": "",
            "source": "download_failed",
            "pdf_url": pdf_url,
        }

    raw_text = ""
    source = "cache_text"
    if txt_path.exists() and not refresh:
        raw_text = txt_path.read_text(encoding="utf-8", errors="ignore")
    else:
        raw_text, source = extract_pdf_text(
            pdf_path,
            max_pages=max_pages,
            ghostscript_timeout_sec=gs_timeout_sec,
        )
        if raw_text.strip():
            ensure_dirs(txt_path.parent)
            txt_path.write_text(raw_text, encoding="utf-8")

    if not raw_text.strip():
        return {
            "status": "extract_failed",
            "param_details": [],
            "method_excerpt": "",
            "source": source,
            "pdf_url": pdf_url,
            "pdf_path": str(pdf_path),
        }

    method_excerpt = extract_method_or_experimental_sections(raw_text, max_chars=22000)
    key_context = [
        "method",
        "methods",
        "methodology",
        "simulation details",
        "experimental",
        "materials and methods",
        "computational details",
        "boundary condition",
        "time step",
        "strain rate",
        "potential",
        "mesh",
        "grid",
    ]
    if not method_excerpt.strip():
        method_excerpt = collect_keyword_context(raw_text, key_context, window=900)
    if not method_excerpt.strip():
        method_excerpt = raw_text[:8000]

    param_details = extract_parameter_lines(method_excerpt, max_items=max_items)
    if not param_details:
        param_details = extract_parameter_lines(raw_text[:12000], max_items=max_items)

    return {
        "status": "ok",
        "source": source,
        "download_status": download_status,
        "pdf_url": pdf_url,
        "pdf_path": str(pdf_path),
        "text_path": str(txt_path),
        "param_details": param_details,
        "method_excerpt": truncate_text(compact_whitespace(method_excerpt), 1600),
    }


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

    if not merged:
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


def default_focus_from_tags(tags: list[str]) -> list[str]:
    mapping = {
        "multiphysics_coupling": "多物理耦合与跨场耦合机制",
        "molecular_dynamics": "分子动力学与原子尺度机制",
        "phase_field_crystal": "相场晶体与组织演化",
        "metal_fatigue": "疲劳损伤与断裂演化",
        "tensile_simulation": "拉伸响应与本构行为",
    }
    out: list[str] = []
    for tag in tags:
        maybe = mapping.get(tag)
        if maybe and maybe not in out:
            out.append(maybe)
    return out or ["通用材料模拟与数值建模"]


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
    return "可作为材料模拟通用参考，帮助你补全「模型-参数-结果解释」的知识链条。"


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


def infer_model_setups_cn(abstract: str) -> list[str]:
    lowered = abstract.lower()
    setup_map = [
        ("molecular dynamics", "分子动力学（MD）"),
        ("ab initio molecular dynamics", "第一性原理分子动力学（AIMD）"),
        ("density functional theory", "密度泛函理论（DFT）"),
        ("finite element", "有限元模型（FEM）"),
        ("phase-field crystal", "相场晶体模型（PFC）"),
        ("phase field crystal", "相场晶体模型（PFC）"),
        ("phase field", "相场模型"),
        ("grand canonical monte carlo", "巨正则蒙特卡罗（GCMC）"),
        ("cellular automata", "元胞自动机（CA）"),
        ("dislocation dynamics", "位错动力学模型"),
        ("coarse-grained", "粗粒化模型"),
        ("machine learning potential", "机器学习势函数"),
        ("interatomic potential", "原子间势函数模型"),
    ]
    out: list[str] = []
    for needle, label in setup_map:
        if needle in lowered and label not in out:
            out.append(label)
    if not out:
        out.append("摘要未明确给出模型名称，建议查看 Methods/Model 部分确认。")
    return out


def extract_parameter_candidates_cn(abstract: str, limit: int = 8) -> list[str]:
    text = compact_whitespace(abstract)
    if not text:
        return []

    unit_pattern = (
        r"(K|°C|nm|um|μm|mm|eV|meV|GPa|MPa|Pa|Hz|kHz|MHz|GHz|V|mV|A|mA|"
        r"s|ms|ns|ps|fs|%|wt%|at%|mol%|T|mT|atoms?|cycles?|steps?|samples?)"
    )
    patterns = [
        rf"\b\d+(?:\.\d+)?\s*(?:-|to|–|~)\s*\d+(?:\.\d+)?\s*{unit_pattern}\b",
        rf"\b\d+(?:\.\d+)?\s*{unit_pattern}\b",
    ]

    raw: list[str] = []
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            raw.append(compact_whitespace(m.group(0)))

    contextual_patterns = [
        r"(?i)(temperature|strain rate|loading rate|electric field|frequency|porosity|particle diameter|radius|time step|boundary condition|pressure|composition)[^.;]{0,60}",
        r"(?i)(charge rate|cyclic life|elastic constants|yield stress|curvature)[^.;]{0,60}",
    ]
    for pat in contextual_patterns:
        for m in re.finditer(pat, text):
            raw.append(compact_whitespace(m.group(0)))

    seen: set[str] = set()
    out: list[str] = []
    for x in raw:
        k = x.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
        if len(out) >= max(1, limit):
            break
    return out


def pick_evidence_sentence(abstract: str) -> str:
    sentences = split_sentences(abstract)
    cues = ("we find", "results show", "results indicate", "demonstrate", "reveal", "outperform")
    for s in sentences:
        ls = s.lower()
        if any(c in ls for c in cues):
            return s
    return sentences[0] if sentences else ""


def build_group_style_cn(
    paper: Paper,
    labels: dict[str, str],
    keywords: list[dict[str, Any]] | None = None,
    max_params: int = 6,
    pdf_param_details: list[str] | None = None,
    method_excerpt: str = "",
) -> dict[str, str]:
    abstract = paper.summary.strip()
    topics = [labels.get(tag, tag) for tag in paper.tags] or ["通用材料模拟"]
    setup_text = f"{method_excerpt}\n{abstract}" if method_excerpt.strip() else abstract
    model_setups = infer_model_setups_cn(setup_text)
    params = list(pdf_param_details or [])
    for item in extract_parameter_candidates_cn(abstract, limit=max(1, max_params * 2)):
        if item not in params:
            params.append(item)
    params = params[: max(1, max_params)]
    evidence = pick_evidence_sentence(setup_text)

    keywords = keywords or []
    kw_names = [str(x.get("keyword", "")) for x in keywords if str(x.get("keyword", "")).strip()]
    kw_text = ", ".join(kw_names[:5]) if kw_names else "N/A"

    problem = (
        f"针对{', '.join(topics)}方向，论文聚焦「{paper.title}」相关机理/性能问题，"
        "目标是建立可解释的模型并量化关键影响因素。"
    )
    model_setup = "；".join(model_setups[:4])
    model_params = "；".join(params[: max(1, max_params)]) if params else "摘要未明确给出可直接复现的数值参数。"
    conclusion = infer_finding_signal_cn(abstract)
    if evidence:
        conclusion = f"{conclusion} 摘要证据：{truncate_text(evidence, 140)}"

    return {
        "problem": truncate_text(problem, 220),
        "model_setup": truncate_text(model_setup, 220),
        "model_params": truncate_text(model_params, 260),
        "conclusion": truncate_text(conclusion, 240),
        "keyword_context": truncate_text(kw_text, 180),
    }


def refine_group_style_with_summary(group_style: dict[str, str], summary_text: str) -> dict[str, str]:
    sections = parse_cn_sections(summary_text)
    if not sections:
        return group_style
    out = dict(group_style)
    if sections.get("what_done"):
        out["problem"] = truncate_text(str(sections["what_done"]), 220)
    if sections.get("method"):
        out["model_setup"] = truncate_text(str(sections["method"]), 220)
    if sections.get("finding"):
        out["conclusion"] = truncate_text(str(sections["finding"]), 240)
    return out


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
    what_done = f"该论文围绕「{paper.title}」展开，针对{', '.join(topics)}相关问题进行建模与分析。"
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
    abstract_preview = truncate_text(abstract, 1500)
    method = brief["method"]
    model_setups = infer_model_setups_cn(abstract)
    params = extract_parameter_candidates_cn(abstract, limit=10)
    params_text = "；".join(params) if params else "摘要中未找到具体数值参数，需查阅全文 Methods 章节。"
    return textwrap.dedent(
        f"""\
        1) 研究问题与背景
        - {brief["what_done"]}
        - 主题归类: {brief["topics"]}

        2) 核心方法与模型细节
        - 检测到的方法信号: {method}
        - 模型类别: {"；".join(model_setups[:4])}
        - 摘要中提取的参数线索: {params_text}
        - 注意：以上基于摘要自动提取，完整参数需查阅全文。

        3) 关键结果
        - {brief["finding"]}

        4) 对你研究的意义
        - {brief["meaning"]}

        5) 可复现性与阅读建议
        - 建议重点精读 Methods/Simulation Details 章节，提取边界条件、势函数、时间步长等关键参数。
        - 如有实验对比数据，注意验证方法和误差范围。

        6) 摘要原文
        - {abstract_preview}
        """
    ).strip()


def _build_summary_prompt(paper: Paper, labels: dict[str, str]) -> str:
    return textwrap.dedent(
        f"""\
        你是材料模拟领域（多尺度模拟、分子动力学、相场晶体、有限元、金属疲劳与拉伸模拟）的资深研究助理。
        请对以下论文进行深入技术分析，用中文输出，严格按如下结构：

        1) 研究问题与背景（3-5句）
        - 该论文解决什么科学/工程问题？在什么背景下提出？
        - 现有方法/模型的局限性是什么？该工作的创新点在哪里？

        2) 核心方法与模型细节（5-8句，这是最重要的部分）
        - 使用了什么模型/方法？（如 MD、PFC、FEM、DFT、机器学习势函数等）
        - 关键控制方程或理论框架是什么？
        - 势函数类型/版本（如 EAM、MEAM、ReaxFF、Tersoff、GAP、MTP 等）
        - 边界条件设置（周期性、自由表面、固定边界等）
        - 加载方式（单轴拉伸、循环加载、应变率、温度梯度等）
        - 模拟尺寸、原子数、网格规模、时间步长等关键参数
        - 使用了什么软件平台（LAMMPS、VASP、ABAQUS、自研代码等）

        3) 关键结果与定量结论（3-5句）
        - 主要发现是什么？给出具体数值（应力值、温度范围、缺陷密度等）
        - 与已有实验/模拟结果的对比情况
        - 模型/方法的精度和适用范围

        4) 对我研究的直接价值（3-5句）
        - 我的方向：多物理耦合场模拟、分子动力学、相场晶体模型(XPFC)、金属疲劳模拟、拉伸变形模拟
        - 该论文的方法/参数/结论中，哪些可以直接被我借鉴或复用？
        - 对我当前工作的具体启发（参数选取、模型验证、边界条件设计等）

        5) 可复现性评估与阅读建议（2-3句）
        - 论文是否提供了足够的参数细节来复现？缺少哪些关键信息？
        - 建议重点精读哪些章节？

        要求：
        - 必须提取具体数值和参数，不要泛泛而谈。
        - 如果摘要中某些信息不足，明确标注"摘要未提及，需查阅全文"。
        - 每个部分都要有实质性内容，不允许出现"该论文研究了XX"之类的废话。

        论文标题: {paper.title}
        发布时间: {paper.published}
        作者: {", ".join(paper.authors)}
        主题标签: {", ".join(labels.get(tag, tag) for tag in paper.tags) or "N/A"}
        摘要:
        {paper.summary}
        """
    ).strip()


def _call_anthropic(api_key: str, model: str, prompt: str, cfg: dict[str, Any]) -> str:
    """Call Anthropic Messages API (Claude)."""
    max_tokens = int(cfg.get("max_tokens", 1024) or 1024)
    temperature = float(cfg.get("temperature", 0.2))
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": "只输出中文纯文本，不要使用 Markdown 代码块。",
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": "paper-agent/2.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    # Parse Anthropic response: {"content": [{"type": "text", "text": "..."}]}
    for block in result.get("content", []):
        if block.get("type") == "text" and block.get("text", "").strip():
            return block["text"].strip()
    return ""


def _call_openai(api_key: str, model: str, prompt: str, cfg: dict[str, Any]) -> str:
    """Call OpenAI-compatible API (legacy fallback)."""
    endpoint = cfg.get("endpoint", "https://api.openai.com/v1/responses")
    temperature = float(cfg.get("temperature", 0.2))
    payload = json.dumps({
        "model": model,
        "temperature": temperature,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": "只输出中文纯文本，不要使用 Markdown 代码块。"}]},
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "paper-agent/2.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    # Parse OpenAI Responses API format
    if isinstance(raw.get("output_text"), str) and raw["output_text"].strip():
        return raw["output_text"].strip()
    for out in raw.get("output", []):
        for content in out.get("content", []):
            maybe_text = content.get("text")
            if isinstance(maybe_text, str) and maybe_text.strip():
                return maybe_text.strip()
    return ""


def maybe_summarize_with_llm(
    paper: Paper, cfg: dict[str, Any], labels: dict[str, str]
) -> tuple[str, str]:
    llm_cfg = cfg.get("llm", {})
    if not llm_cfg.get("enabled", True):
        return build_fallback_summary(paper, labels), "fallback"

    api_key_env = llm_cfg.get("api_key_env", "ANTHROPIC_API_KEY")
    api_key = os.getenv(api_key_env, "")
    if not api_key:
        return build_fallback_summary(paper, labels), "fallback_no_key"

    provider = llm_cfg.get("provider", "anthropic")
    model = llm_cfg.get("model", "claude-sonnet-4-20250514")
    prompt = _build_summary_prompt(paper, labels)

    try:
        if provider == "anthropic":
            text = _call_anthropic(api_key, model, prompt, llm_cfg)
        else:
            text = _call_openai(api_key, model, prompt, llm_cfg)
        if text:
            return text, f"llm_{provider}"
        return build_fallback_summary(paper, labels), f"fallback_empty_{provider}"
    except urllib.error.HTTPError as err:
        return build_fallback_summary(paper, labels), f"fallback_http_{err.code}"
    except urllib.error.URLError:
        return build_fallback_summary(paper, labels), "fallback_network"
    except Exception as err:
        return build_fallback_summary(paper, labels), f"fallback_error:{err}"


def truncate_text(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    return value[: max_len - 3].rstrip() + "..."


def build_daily_reminder_messages(
    *,
    selected: list[Paper],
    daily_limit: int,
    labels: dict[str, str],
    report_path: Path,
    max_items: int,
    db: dict[str, Any] | None = None,
    keyword_limit: int = 5,
    params_chars: int = 200,
) -> list[str]:
    """Build a list of Telegram messages -- one overview + one per paper with full analysis."""
    date_label = dt.date.today().isoformat()
    messages: list[str] = []

    # Message 1: Overview
    overview_lines = [
        f"📚 今日论文阅读提醒（{date_label}）",
        f"━━━━━━━━━━━━━━━━━━━━",
    ]
    if selected:
        overview_lines.append(f"今天新增待读：{len(selected)} 篇（目标 {daily_limit} 篇）\n")
        for idx, paper in enumerate(selected[:max_items], start=1):
            tags = [labels.get(t, t) for t in paper.tags]
            overview_lines.append(f"{idx}. {paper.title}")
            overview_lines.append(f"   🏷 {', '.join(tags[:3])}")
            overview_lines.append(f"   🔗 {paper.link}\n")
        overview_lines.append("⬇️ 详细分析见后续消息")
    else:
        overview_lines.extend([
            "今天没有筛选到新的未读论文。",
            "建议：复盘最近 3 篇笔记，更新你的「模型-参数-结论」体系表。",
        ])
    messages.append("\n".join(overview_lines))

    if not selected:
        return messages

    # Messages 2-N: One message per paper with full analysis
    for idx, paper in enumerate(selected[:max_items], start=1):
        record: dict[str, Any] = {}
        if db and paper.paper_id in db:
            record = db[paper.paper_id]

        brief = record.get("brief_cn", {}) if record else {}
        if not isinstance(brief, dict):
            brief = {}
        if not brief:
            brief = build_cn_brief(paper, labels)

        keyword_items = record.get("keyword_details", []) if record else []
        focus_areas = record.get("focus_areas", []) if record else []
        group_style = record.get("group_style_cn", {}) if record else {}

        keyword_names = [str(x.get("keyword", "")) for x in keyword_items if str(x.get("keyword", "")).strip()]
        keyword_preview = ", ".join(keyword_names[: max(1, keyword_limit)])
        focus_preview = "；".join(str(x) for x in focus_areas[:3]) if focus_areas else "N/A"

        problem = str(group_style.get("problem", brief.get("what_done", "N/A")))
        model_setup = str(group_style.get("model_setup", brief.get("method", "N/A")))
        model_params = str(group_style.get("model_params", "N/A"))
        conclusion = str(group_style.get("conclusion", brief.get("finding", "N/A")))

        # Get the full LLM summary if available
        summary_text = ""
        note_path = record.get("note_path", "")
        if note_path:
            try:
                np = Path(note_path)
                if not np.exists():
                    # Try relative to current working directory
                    np = Path(".") / "data" / "notes" / f"{slugify(paper.paper_id)}.md"
                if np.exists():
                    raw = np.read_text(encoding="utf-8", errors="ignore")
                    # Extract the structured summary section
                    marker_start = "## Structured Summary"
                    marker_end = "## Raw Abstract"
                    if marker_start in raw and marker_end in raw:
                        start_idx = raw.index(marker_start) + len(marker_start)
                        end_idx = raw.index(marker_end)
                        summary_text = raw[start_idx:end_idx].strip()
            except Exception:
                pass

        paper_lines = [
            f"📄 [{idx}/{len(selected[:max_items])}] {paper.title}",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"👤 {', '.join(paper.authors[:3])}{'...' if len(paper.authors) > 3 else ''}",
            f"📅 {paper.published[:10] if paper.published else 'N/A'}",
            f"🏷 {', '.join([labels.get(t, t) for t in paper.tags[:3]])}",
            f"⭐ 相关度评分：{paper.score}",
            "",
            f"🔬 研究问题",
            f"{truncate_text(problem, 300)}",
            "",
            f"🛠 模型/方法",
            f"{truncate_text(model_setup, 300)}",
            "",
            f"📐 关键参数",
            f"{truncate_text(model_params, max(100, params_chars))}",
            "",
            f"📊 主要结论",
            f"{truncate_text(conclusion, 300)}",
            "",
            f"💡 对我的价值",
            f"{truncate_text(str(brief.get('meaning', 'N/A')), 300)}",
        ]

        if keyword_preview:
            paper_lines.extend(["", f"🔑 关键词：{keyword_preview}"])
        if focus_preview != "N/A":
            paper_lines.extend([f"📌 侧重点：{focus_preview}"])

        # Include LLM detailed analysis if available
        if summary_text and len(summary_text) > 100:
            # Truncate to fit Telegram's limit
            detail = truncate_text(summary_text, 2000)
            paper_lines.extend([
                "",
                "━━━ Claude 深度分析 ━━━",
                detail,
            ])

        paper_lines.extend(["", f"🔗 {paper.link}"])

        msg = "\n".join(paper_lines)
        messages.append(msg)

    # Final message: reading advice
    messages.append(
        f"📖 阅读建议\n━━━━━━━━━━━━━━━━━━━━\n"
        f"今天优先精读前 2 篇，重点关注：\n"
        f"  1. 方法/模型章节的参数设置\n"
        f"  2. 边界条件与载荷路径\n"
        f"  3. 与你工作最相关的结论\n\n"
        f"笔记和完整分析已保存到 GitHub 仓库。"
    )

    return messages


def send_via_telegram(
    *,
    message: str,
    tg_cfg: dict[str, Any],
    dry_run: bool,
    timeout_sec: int = 30,
) -> tuple[bool, str]:
    """Send message via Telegram Bot API (no third-party binaries needed)."""
    token_env = str(tg_cfg.get("bot_token_env", "TELEGRAM_BOT_TOKEN"))
    chat_id_env = str(tg_cfg.get("chat_id_env", "TELEGRAM_CHAT_ID"))
    bot_token = os.getenv(token_env, "").strip()
    chat_id = os.getenv(chat_id_env, "").strip()
    if not bot_token:
        return False, f"env {token_env} is empty"
    if not chat_id:
        return False, f"env {chat_id_env} is empty"

    if dry_run:
        return True, f"dry_run: would send {len(message)} chars to chat_id={chat_id}"

    # Telegram has a 4096 char limit per message; split if needed
    chunks: list[str] = []
    if len(message) <= 4000:
        chunks = [message]
    else:
        lines = message.split("\n")
        buf: list[str] = []
        buf_len = 0
        for line in lines:
            if buf_len + len(line) + 1 > 3900 and buf:
                chunks.append("\n".join(buf))
                buf = []
                buf_len = 0
            buf.append(line)
            buf_len += len(line) + 1
        if buf:
            chunks.append("\n".join(buf))

    api_base = f"https://api.telegram.org/bot{bot_token}"
    sent_count = 0
    last_error = ""
    for chunk in chunks:
        payload = json.dumps({
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{api_base}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "paper-agent/2.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("ok"):
                    sent_count += 1
                else:
                    last_error = str(result.get("description", "unknown_error"))
        except urllib.error.HTTPError as err:
            body = ""
            try:
                body = err.read().decode("utf-8", errors="ignore")[:200]
            except Exception:
                pass
            last_error = f"http_{err.code}: {body}"
        except urllib.error.URLError as err:
            last_error = f"url_error: {err}"
        except Exception as err:
            last_error = f"send_error: {err}"

    if sent_count == len(chunks):
        return True, f"sent {sent_count} message(s)"
    if sent_count > 0:
        return True, f"partial: sent {sent_count}/{len(chunks)}, last_error={last_error}"
    return False, last_error or "send_failed"


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
    telegram_kw = int(cfg.get("keyword_settings", {}).get("telegram_keywords", 5) or 5)
    params_chars = int(cfg.get("group_style", {}).get("telegram_params_chars", 200) or 200)
    messages = build_daily_reminder_messages(
        selected=selected,
        daily_limit=daily_limit,
        labels=labels,
        report_path=report_path,
        max_items=max_items,
        db=db,
        keyword_limit=max(1, telegram_kw),
        params_chars=max(60, params_chars),
    )
    tg_cfg = notify_cfg.get("telegram", {})
    # Send each message separately to avoid Telegram's 4096 char limit
    sent = 0
    last_err = ""
    for msg in messages:
        ok, info = send_via_telegram(message=msg, tg_cfg=tg_cfg, dry_run=dry_run)
        if ok:
            sent += 1
        else:
            last_err = info
    if sent == len(messages):
        return True, f"sent {sent} messages"
    if sent > 0:
        return True, f"partial: {sent}/{len(messages)}, last_error={last_err}"
    return False, last_err or "all_messages_failed"


def write_paper_note(
    path: Path,
    paper: Paper,
    summary_text: str,
    brief_cn: dict[str, str] | None = None,
    keyword_details: list[dict[str, Any]] | None = None,
    focus_areas: list[str] | None = None,
    group_style_cn: dict[str, str] | None = None,
    pdf_parse: dict[str, Any] | None = None,
) -> None:
    ensure_dirs(path.parent)
    brief_cn = brief_cn or {}
    keyword_details = keyword_details or []
    focus_areas = focus_areas or []
    group_style_cn = group_style_cn or {}
    pdf_parse = pdf_parse or {}
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
            "## 课题组模板拆解",
            f"- 研究问题: {group_style_cn.get('problem', 'N/A')}",
            f"- 模型设置: {group_style_cn.get('model_setup', 'N/A')}",
            f"- 模型配置参数: {group_style_cn.get('model_params', 'N/A')}",
            f"- 主要结论: {group_style_cn.get('conclusion', 'N/A')}",
            "",
            "## PDF 正文参数证据（方法/实验）",
            f"- 解析状态: {pdf_parse.get('status', 'N/A')}",
            f"- 解析来源: {pdf_parse.get('source', 'N/A')}",
        "",
            "## Structured Summary",
            summary_text.strip(),
            "",
            "## Raw Abstract",
            paper.summary.strip() or "N/A",
            "",
        ]
    )
    param_lines = pdf_parse.get("param_details", [])
    if isinstance(param_lines, list) and param_lines:
        insert_at = lines.index("## Structured Summary")
        details = [f"- {str(x)}" for x in param_lines[:12]]
        lines[insert_at:insert_at] = details + [""]
    excerpt = str(pdf_parse.get("method_excerpt", "")).strip()
    if excerpt:
        insert_at = lines.index("## Structured Summary")
        lines[insert_at:insert_at] = [
            "- 方法/实验章节片段:",
            f"  {truncate_text(excerpt, 500)}",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_daily_report(
    report_path: Path,
    selected: list[Paper],
    db: dict[str, Any],
    labels: dict[str, str],
    keyword_limit: int = 5,
    params_chars: int = 160,
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
            group_style = db.get(paper.paper_id, {}).get("group_style_cn", {})
            pdf_parse = db.get(paper.paper_id, {}).get("pdf_parse", {})
            keyword_names = [str(x.get("keyword", "")) for x in keyword_details if str(x.get("keyword", "")).strip()]
            done_cn = truncate_text(str(brief.get("what_done", "N/A")), 160)
            meaning_cn = truncate_text(str(brief.get("meaning", "N/A")), 160)
            kw_preview = truncate_text(", ".join(keyword_names[: max(1, keyword_limit)]), 200) if keyword_names else "N/A"
            focus_preview = truncate_text("；".join(focus_areas[:3]), 200) if focus_areas else "N/A"
            problem = truncate_text(str(group_style.get("problem", done_cn)), 200)
            model_setup = truncate_text(str(group_style.get("model_setup", "N/A")), 200)
            model_params = truncate_text(str(group_style.get("model_params", "N/A")), max(60, params_chars))
            conclusion = truncate_text(str(group_style.get("conclusion", "N/A")), 200)
            pdf_status = str(pdf_parse.get("status", "N/A"))
            pdf_source = str(pdf_parse.get("source", "N/A"))
            lines.extend(
                [
                    f"## {idx}. {paper.title}",
                    f"- ID: `{paper.paper_id}`",
                    f"- Tags: {', '.join(label_list) if label_list else 'N/A'}",
                    f"- Score: {paper.score}",
                    f"- Link: {paper.link}",
                    f"- Note: {note_rel}",
                    f"- 研究问题: {problem}",
                    f"- 模型设置: {model_setup}",
                    f"- 模型参数: {model_params}",
                    f"- 参数证据来源: {pdf_status} / {pdf_source}",
                    f"- 主要结论: {conclusion}",
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
        research_focus = [x for x in focus_areas if "（主题归类）" not in x]
        if not research_focus:
            research_focus = default_focus_from_tags(tags)

        for focus in research_focus:
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


def build_dashboard_html(
    dashboard_path: Path, db: dict[str, Any], topic_labels: dict[str, str]
) -> None:
    """Generate a self-contained interactive HTML dashboard."""
    papers = list(db.values())
    total_papers = len(papers)
    last_update = now_utc().strftime("%Y-%m-%d %H:%M UTC")

    # Timeline by month
    timeline: dict[str, int] = {}
    for p in papers:
        month = (p.get("published") or "")[:7]
        if month:
            timeline[month] = timeline.get(month, 0) + 1
    tl_sorted = sorted(timeline.items())
    tl_labels = json.dumps([x[0] for x in tl_sorted])
    tl_data = json.dumps([x[1] for x in tl_sorted])

    # Topic distribution
    tc: dict[str, int] = {k: 0 for k in topic_labels}
    for p in papers:
        for tag in p.get("tags", []):
            if tag in tc:
                tc[tag] += 1
    tn = json.dumps([topic_labels.get(k, k) for k in tc])
    tv = json.dumps(list(tc.values()))

    # Keyword frequency top 20
    kf: dict[str, int] = {}
    for p in papers:
        for kw in p.get("keywords", []):
            kf[kw] = kf.get(kw, 0) + 1
    top_kw = sorted(kf.items(), key=lambda x: x[1], reverse=True)[:20]
    kw_n = json.dumps([x[0] for x in top_kw])
    kw_c = json.dumps([x[1] for x in top_kw])

    # Year-Focus heatmap
    years = sorted(set(p.get("published", "")[:4] for p in papers if p.get("published")))
    all_focus = sorted(set(fa for p in papers for fa in p.get("focus_areas", []) if "（主题归类）" not in fa))
    hm: dict[str, dict[str, int]] = {y: {fa: 0 for fa in all_focus} for y in years}
    for p in papers:
        y = (p.get("published") or "")[:4]
        for fa in p.get("focus_areas", []):
            if y in hm and fa in hm.get(y, {}):
                hm[y][fa] += 1

    # Heatmap rows
    hm_rows = ""
    for y in years:
        cells = ""
        vals = list(hm[y].values())
        mx = max(vals) if vals else 1
        for fa in all_focus:
            v = hm[y][fa]
            intensity = min(4, int(v / max(mx, 1) * 4)) if v > 0 else 0
            cells += f'<td class="i{intensity}">{v}</td>'
        hm_rows += f"<tr><th>{y}</th>{cells}</tr>\n"
    hm_headers = "".join(f"<th>{fa[:12]}</th>" for fa in all_focus)

    # Network nodes/edges (papers clustered by topic, edges by shared keywords)
    colors_list = ["#0066cc", "#00cc99", "#ff6b6b", "#ffd93d", "#6c5ce7"]
    topic_keys = list(topic_labels.keys())
    nodes_js: list[dict[str, Any]] = []
    pid_idx: dict[str, int] = {}
    for idx, (pid, p) in enumerate(db.items()):
        tags = p.get("tags", [])
        ti = next((i for i, t in enumerate(topic_keys) if t in tags), 0)
        nodes_js.append({"l": p.get("title", "")[:28], "c": colors_list[ti % len(colors_list)], "t": ti})
        pid_idx[pid] = idx
    edges_js: list[dict[str, int]] = []
    pids = list(db.keys())
    for i, p1 in enumerate(pids):
        kw1 = set(db[p1].get("keywords", []))
        for p2 in pids[i + 1:]:
            shared = len(kw1 & set(db[p2].get("keywords", [])))
            if shared >= 2:
                edges_js.append({"s": i, "t": pid_idx[p2], "w": shared})

    legend_html = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:5px;margin-right:14px;">'
        f'<span style="width:12px;height:12px;border-radius:50%;background:{colors_list[i % len(colors_list)]};display:inline-block;"></span>'
        f'{topic_labels[k]}</span>'
        for i, k in enumerate(topic_keys)
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>材料模拟论文知识体系仪表盘</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:linear-gradient(135deg,#f5f7fa,#c3cfe2);padding:20px;color:#333}}
.ct{{max-width:1400px;margin:0 auto}}
.hd{{background:linear-gradient(135deg,#0066cc,#00cc99);color:#fff;padding:28px;border-radius:10px;margin-bottom:24px;box-shadow:0 4px 15px rgba(0,102,204,.2)}}
.hd h1{{font-size:2.2em;margin-bottom:8px}}.hd p{{font-size:1.1em;opacity:.9}}
.gr{{display:grid;grid-template-columns:repeat(auto-fit,minmax(480px,1fr));gap:20px;margin-bottom:20px}}
.cd{{background:#fff;border-radius:10px;padding:20px;box-shadow:0 2px 10px rgba(0,0,0,.08);transition:transform .2s}}
.cd:hover{{transform:translateY(-3px);box-shadow:0 6px 16px rgba(0,0,0,.12)}}
.cd h2{{color:#0066cc;font-size:1.2em;margin-bottom:12px;padding-bottom:8px;border-bottom:2px solid #00cc99}}
.fw{{grid-column:1/-1}}
.cc{{position:relative;height:380px}}
table{{width:100%;border-collapse:collapse;font-size:.85em}}
th,td{{padding:6px 8px;text-align:center;border:1px solid #ddd}}
th{{background:#0066cc;color:#fff}}
.i0{{background:#f5f5f5}}.i1{{background:#b3e5fc}}.i2{{background:#4fc3f7}}.i3{{background:#0066cc;color:#fff}}.i4{{background:#003d99;color:#fff}}
#net{{width:100%;height:460px;border:1px solid #ddd;border-radius:8px;background:#fafafa}}
</style>
</head>
<body>
<div class="ct">
<div class="hd"><h1>📚 材料模拟论文知识体系仪表盘</h1><p>总论文数: {total_papers} | 最后更新: {last_update}</p></div>
<div class="gr">
<div class="cd"><h2>📅 论文时间线</h2><div class="cc"><canvas id="c1"></canvas></div></div>
<div class="cd"><h2>🎯 主题分布</h2><div class="cc"><canvas id="c2"></canvas></div></div>
<div class="cd fw"><h2>🔑 高频关键词 (Top 20)</h2><div class="cc" style="height:440px"><canvas id="c3"></canvas></div></div>
<div class="cd fw"><h2>📊 年份-侧重点分布</h2><table><tr><th>年份</th>{hm_headers}</tr>{hm_rows}</table></div>
<div class="cd fw"><h2>🔗 论文关联网络</h2><canvas id="net"></canvas><div style="margin-top:10px;font-size:.9em">{legend_html}</div></div>
</div></div>
<script>
new Chart(document.getElementById('c1'),{{type:'bar',data:{{labels:{tl_labels},datasets:[{{label:'论文数',data:{tl_data},backgroundColor:'#0066cc'}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},scales:{{y:{{beginAtZero:true}}}}}}}});
new Chart(document.getElementById('c2'),{{type:'doughnut',data:{{labels:{tn},datasets:[{{data:{tv},backgroundColor:['#0066cc','#00cc99','#ff6b6b','#ffd93d','#6c5ce7']}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{position:'bottom'}}}}}}}});
new Chart(document.getElementById('c3'),{{type:'bar',data:{{labels:{kw_n},datasets:[{{label:'频次',data:{kw_c},backgroundColor:'#00cc99'}}]}},options:{{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},scales:{{x:{{beginAtZero:true}}}}}}}});
(function(){{
const cv=document.getElementById('net'),cx=cv.getContext('2d');
cv.width=cv.offsetWidth;cv.height=cv.offsetHeight;
const N={json.dumps(nodes_js)},E={json.dumps(edges_js)};
const P=N.map((_,i)=>({{x:Math.random()*(cv.width-80)+40,y:Math.random()*(cv.height-80)+40,vx:0,vy:0}}));
for(let it=0;it<60;it++){{N.forEach((_,i)=>{{let fx=0,fy=0;N.forEach((_,j)=>{{if(i!==j){{const dx=P[j].x-P[i].x,dy=P[j].y-P[i].y,d=Math.hypot(dx,dy)+.01;fx-=dx/d*120/(d*d);fy-=dy/d*120/(d*d);}}}});E.forEach(e=>{{const o=e.s===i?e.t:e.t===i?e.s:-1;if(o>=0){{const dx=P[o].x-P[i].x,dy=P[o].y-P[i].y,d=Math.hypot(dx,dy)+.01;fx+=dx/d*d*.08*e.w;fy+=dy/d*d*.08*e.w;}}}});P[i].x+=fx*.01;P[i].y+=fy*.01;P[i].x=Math.max(15,Math.min(cv.width-15,P[i].x));P[i].y=Math.max(15,Math.min(cv.height-15,P[i].y));}})}}
cx.fillStyle='#fff';cx.fillRect(0,0,cv.width,cv.height);
E.forEach(e=>{{cx.strokeStyle='rgba(100,150,200,.25)';cx.lineWidth=1+e.w*.4;cx.beginPath();cx.moveTo(P[e.s].x,P[e.s].y);cx.lineTo(P[e.t].x,P[e.t].y);cx.stroke();}});
N.forEach((n,i)=>{{cx.fillStyle=n.c;cx.beginPath();cx.arc(P[i].x,P[i].y,5,0,Math.PI*2);cx.fill();}});
}})();
</script>
</body></html>"""
    ensure_dirs(dashboard_path.parent)
    dashboard_path.write_text(html, encoding="utf-8")


def _mermaid_escape(text: str) -> str:
    """Escape characters that break Mermaid syntax."""
    out = text.replace('"', "'").replace("(", "（").replace(")", "）")
    out = out.replace("[", "【").replace("]", "】").replace("{", "").replace("}", "")
    out = out.replace(":", "").replace(";", " ").replace("`", "'")
    out = out.replace("<", "＜").replace(">", "＞").replace("#", "")
    return re.sub(r"\s+", " ", out).strip()


def build_mermaid_knowledge_graph(
    mermaid_path: Path, db: dict[str, Any], topic_labels: dict[str, str]
) -> None:
    """Generate a Markdown file with Mermaid mindmap and timeline."""
    records = list(db.values())
    total = len(records)
    update_time = now_utc().strftime("%Y-%m-%d %H:%M UTC")

    # Build mindmap
    mm_lines = ["mindmap", "  root((材料模拟论文体系))"]
    for topic_key, topic_label in topic_labels.items():
        topic_papers = sorted(
            [r for r in records if topic_key in r.get("tags", [])],
            key=lambda r: float(r.get("score", 0)),
            reverse=True,
        )[:5]
        safe_label = _mermaid_escape(topic_label)
        count = len([r for r in records if topic_key in r.get("tags", [])])
        mm_lines.append(f"    {safe_label} [{count}篇]")
        for r in topic_papers:
            title = _mermaid_escape(truncate_text(r.get("title", "Untitled"), 32))
            mm_lines.append(f"      {title}")
            kws = [str(k) for k in r.get("keywords", [])[:3] if str(k).strip()]
            for kw in kws:
                mm_lines.append(f"        {_mermaid_escape(kw)}")
    mindmap_block = "\n".join(mm_lines)

    # Build timeline
    month_papers: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        month = (r.get("published") or "")[:7]
        if month:
            month_papers.setdefault(month, []).append(r)
    recent_months = sorted(month_papers.keys(), reverse=True)[:12]
    tl_lines = ["timeline", "    title 论文发布时间线"]
    for month in sorted(recent_months):
        papers = sorted(month_papers[month], key=lambda r: float(r.get("score", 0)), reverse=True)[:3]
        titles = " : ".join(_mermaid_escape(truncate_text(p.get("title", "?"), 28)) for p in papers)
        tl_lines.append(f"    {month} : {titles}")
    timeline_block = "\n".join(tl_lines)

    # Cross-reference table
    table_lines = ["| 主题方向 | 论文数 | 主要侧重点 | 代表论文 |", "|----------|--------|------------|----------|"]
    for tk, tl in topic_labels.items():
        tp = [r for r in records if tk in r.get("tags", [])]
        cnt = len(tp)
        focus_count: dict[str, int] = {}
        for r in tp:
            for fa in r.get("focus_areas", []):
                if "（主题归类）" not in fa:
                    focus_count[fa] = focus_count.get(fa, 0) + 1
        top_focus = ", ".join(f for f, _ in sorted(focus_count.items(), key=lambda x: x[1], reverse=True)[:3])
        sample = truncate_text(tp[0].get("title", "N/A"), 35) if tp else "N/A"
        table_lines.append(f"| {tl} | {cnt} | {top_focus or 'N/A'} | {sample} |")
    table_block = "\n".join(table_lines)

    content = f"""# 论文知识体系图谱

> 自动生成 | 总论文数: {total} | 更新时间: {update_time}

## 知识体系思维导图

```mermaid
{mindmap_block}
```

## 论文发布时间线（近12个月）

```mermaid
{timeline_block}
```

## 主题-侧重点交叉分析

{table_block}
"""
    ensure_dirs(mermaid_path.parent)
    mermaid_path.write_text(content, encoding="utf-8")


def entry_to_db_record(
    paper: Paper,
    note_path: Path,
    status: str = "auto",
    summary_source: str = "fallback",
    brief_cn: dict[str, str] | None = None,
    keyword_details: list[dict[str, Any]] | None = None,
    focus_areas: list[str] | None = None,
    group_style_cn: dict[str, str] | None = None,
    pdf_parse: dict[str, Any] | None = None,
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
        "group_style_cn": group_style_cn or {},
        "pdf_parse": pdf_parse or {},
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
    dashboard_path = root / "reports" / "dashboard.html"
    mermaid_path = root / "reports" / "knowledge_graph.md"

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
    max_params = int(cfg.get("group_style", {}).get("max_parameters_per_paper", 6) or 6)

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

    daily_limit = int(args.limit or cfg.get("daily_limit", 5))
    vis_cfg = cfg.get("visualization", {})

    if not all_entries:
        print("No entries fetched from arXiv. Knowledge map will still be rebuilt.")
        rebuild_knowledge_map(map_path=map_path, db=db, topic_labels=labels)
        rebuild_focus_year_summary(summary_path=focus_summary_path, db=db, topic_labels=labels)
        if vis_cfg.get("dashboard_html", True):
            build_dashboard_html(dashboard_path=dashboard_path, db=db, topic_labels=labels)
        if vis_cfg.get("mermaid_graph", True):
            build_mermaid_knowledge_graph(mermaid_path=mermaid_path, db=db, topic_labels=labels)
        report_path = daily_dir / f"{dt.date.today().isoformat()}.md"
        daily_kw = int(cfg.get("keyword_settings", {}).get("daily_report_keywords", 5) or 5)
        daily_param_chars = int(cfg.get("group_style", {}).get("daily_report_params_chars", 160) or 160)
        write_daily_report(
            report_path=report_path,
            selected=[],
            db=db,
            labels=labels,
            keyword_limit=max(1, daily_kw),
            params_chars=max(60, daily_param_chars),
        )
        ok, notify_msg = maybe_send_daily_reminder(
            cfg=cfg,
            db=db,
            selected=[],
            daily_limit=daily_limit,
            labels=labels,
            report_path=report_path,
            force_notify=args.notify,
            dry_run=bool(args.notify_dry_run),
        )
        print(f"Notification: {'ok' if ok else 'failed'} - {notify_msg}")
        return 0

    deduped = dedupe_entries(all_entries)
    selected = select_new_papers(deduped, db, daily_limit=daily_limit)

    for paper in selected:
        note_path = notes_dir / f"{slugify(paper.paper_id)}.md"
        summary_text, summary_source = maybe_summarize_with_llm(paper, cfg=cfg, labels=labels)
        keyword_details = extract_keywords_with_explanations(paper=paper, cfg=cfg, labels=labels)
        pdf_parse = extract_pdf_parameter_details(paper=paper, root=root, cfg=cfg)
        focus_areas = infer_focus_areas(
            tags=paper.tags,
            keywords=keyword_details,
            text=f"{paper.title}\n{paper.summary}",
            topic_labels=labels,
        )
        brief_cn = refine_brief_with_summary(build_cn_brief(paper, labels), summary_text)
        group_style_cn = refine_group_style_with_summary(
            build_group_style_cn(
                paper=paper,
                labels=labels,
                keywords=keyword_details,
                max_params=max_params,
                pdf_param_details=list(pdf_parse.get("param_details", [])),
                method_excerpt=str(pdf_parse.get("method_excerpt", "")),
            ),
            summary_text=summary_text,
        )
        write_paper_note(
            path=note_path,
            paper=paper,
            summary_text=summary_text,
            brief_cn=brief_cn,
            keyword_details=keyword_details,
            focus_areas=focus_areas,
            group_style_cn=group_style_cn,
            pdf_parse=pdf_parse,
        )
        db[paper.paper_id] = entry_to_db_record(
            paper=paper,
            note_path=note_path,
            status="auto",
            summary_source=summary_source,
            brief_cn=brief_cn,
            keyword_details=keyword_details,
            focus_areas=focus_areas,
            group_style_cn=group_style_cn,
            pdf_parse=pdf_parse,
        )

    # Refresh last_seen timestamp for already-known entries that appeared today.
    deduped_by_id = {p.paper_id: p for p in deduped}
    parsed_existing = 0
    max_existing_parse = int(cfg.get("pdf_parsing", {}).get("max_existing_parse_per_run", 3) or 3)
    for paper_id in sorted(set(db.keys()) & set(deduped_by_id.keys())):
        record = db[paper_id]
        paper_obj = deduped_by_id[paper_id]
        record["last_seen_at"] = now_utc().isoformat()
        if "score" in record:
            record["score"] = max(float(record["score"]), float(paper_obj.score))

        existing_keyword_details = extract_keywords_with_explanations(
            paper=paper_obj,
            cfg=cfg,
            labels=labels,
        )
        record["keyword_details"] = existing_keyword_details
        record["keywords"] = [
            str(x.get("keyword", ""))
            for x in existing_keyword_details
            if str(x.get("keyword", "")).strip()
        ]
        record["focus_areas"] = infer_focus_areas(
            tags=paper_obj.tags,
            keywords=existing_keyword_details,
            text=f"{paper_obj.title}\n{paper_obj.summary}",
            topic_labels=labels,
        )
        pdf_cfg = cfg.get("pdf_parsing", {})
        refresh_pdf = bool(pdf_cfg.get("refresh_existing", False))
        old_pdf = record.get("pdf_parse", {})
        old_status = str(old_pdf.get("status", ""))
        should_parse_pdf = refresh_pdf or not old_pdf or old_status in ("", "extract_failed", "skip_no_pdf_url")
        if should_parse_pdf and parsed_existing < max(0, max_existing_parse):
            record["pdf_parse"] = extract_pdf_parameter_details(paper=paper_obj, root=root, cfg=cfg)
            parsed_existing += 1
        elif should_parse_pdf and not old_pdf:
            record["pdf_parse"] = {"status": "deferred", "source": "rate_limit", "param_details": [], "method_excerpt": ""}
        else:
            record["pdf_parse"] = old_pdf
        record["group_style_cn"] = build_group_style_cn(
            paper=paper_obj,
            labels=labels,
            keywords=existing_keyword_details,
            max_params=max_params,
            pdf_param_details=list(record["pdf_parse"].get("param_details", [])),
            method_excerpt=str(record["pdf_parse"].get("method_excerpt", "")),
        )

    save_db(db_path, db)
    rebuild_knowledge_map(map_path=map_path, db=db, topic_labels=labels)
    rebuild_focus_year_summary(summary_path=focus_summary_path, db=db, topic_labels=labels)
    if vis_cfg.get("dashboard_html", True):
        build_dashboard_html(dashboard_path=dashboard_path, db=db, topic_labels=labels)
        print(f"Dashboard: {dashboard_path}")
    if vis_cfg.get("mermaid_graph", True):
        build_mermaid_knowledge_graph(mermaid_path=mermaid_path, db=db, topic_labels=labels)
        print(f"Knowledge graph: {mermaid_path}")
    report_path = daily_dir / f"{dt.date.today().isoformat()}.md"
    daily_kw = int(cfg.get("keyword_settings", {}).get("daily_report_keywords", 5) or 5)
    daily_param_chars = int(cfg.get("group_style", {}).get("daily_report_params_chars", 160) or 160)
    write_daily_report(
        report_path=report_path,
        selected=selected,
        db=db,
        labels=labels,
        keyword_limit=max(1, daily_kw),
        params_chars=max(60, daily_param_chars),
    )

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
                - 你已明确标记为"已了解论文"，用于建立连续知识图谱。

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
            pdf_parse = extract_pdf_parameter_details(paper=paper, root=root, cfg=cfg)
            group_style_cn = {
                "problem": "该条目来自你已读论文清单，问题定义需结合原文补充。",
                "model_setup": "待补充：建议记录模型类别、控制方程与边界条件。",
                "model_params": notes or "待补充：建议记录关键参数（温度、尺寸、载荷、时间步、势函数等）。",
                "conclusion": "待补充：建议写入可复现实验/仿真结论与误差范围。",
                "keyword_context": ", ".join(str(x.get("keyword", "")) for x in keyword_details[:5]),
            }
            if pdf_parse.get("param_details"):
                group_style_cn["model_params"] = "；".join(
                    str(x)
                    for x in list(pdf_parse.get("param_details", []))[
                        : max(1, int(cfg.get("group_style", {}).get("max_parameters_per_paper", 6) or 6))
                    ]
                )
            write_paper_note(
                path=note_path,
                paper=paper,
                summary_text=summary_text,
                brief_cn=brief_cn,
                keyword_details=keyword_details,
                focus_areas=focus_areas,
                group_style_cn=group_style_cn,
                pdf_parse=pdf_parse,
            )
            db[paper.paper_id] = entry_to_db_record(
                paper=paper,
                note_path=note_path,
                status="known",
                summary_source="known_import",
                brief_cn=brief_cn,
                keyword_details=keyword_details,
                focus_areas=focus_areas,
                group_style_cn=group_style_cn,
                pdf_parse=pdf_parse,
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
