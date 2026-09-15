"""deep_research: literature research by citation snowballing (design §6.4).

Flow:
  seed (code) → [seed gate, for query/list seeds] → round 0: per paper acquire (code: get the file, split it into
  parts at section headings, set the reference list aside) → one short read task per part → write-up (agent, reviewed)
  → citations_r0 (code): count how often read papers cite each unread work; stop or pick the next round
  → ... → layout (agent, reviewed) → layout gate → sections (agent, reviewed) → abstract → compile (code)
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from ..scholar import ScholarClient, Work, normalize_title, work_key
from ..sessions import JobApprover
from . import register
from .base import HandlerResult, Template, is_approval
from .research_report import compile_report, find_sources, slug, workspace_of

DEFAULTS = {"seed_mode": "query", "seeds": "", "max_papers": 60, "max_rounds": 4, "min_citations": 3,
            "min_fraction": 0.15, "per_round": 8, "seed_count": 10, "max_parts": 12, "format": "md"}
NET_KEY = "net:open-access"
PDF_DIR = "papers/pdf"

PART = """Read {part} with read_file. It is part {k} of {n} of the paper "{title}" (the full paper is {source}; you
don't need to open it). Write {summary} once, containing, for each section that appears in this part, '### <section
name>' followed by a 3-6 sentence summary of what it says. Then, for up to 5 important claims relevant to the research
question ({question}), call add_note with source "{part}", the section as location, and a quote copied character for
character from {part} (one sentence or a shorter phrase is best). If a quote is rejected, copy a shorter phrase from the
part or move on to the next claim. Work only on this part, then call complete_task."""

ASSEMBLE = """Assemble the write-up of "{title}". Steps, each done once:
1. Read the part summaries: {summaries}.
2. Call search_notes once with source "{source}", brief true, limit 50, to list the notes saved from this paper.
3. Write {md} containing:
- '# {title}'
- '## Section summaries': the part summaries in order, lightly edited into one flow
- '## Key claims': 5-10 bullets for the paper's most important claims, each citing a note from step 2 like [n12]
- '## Value of this paper': its contribution, methods, strength of evidence, limitations, and how it bears on the
  research question: {question}
4. {references}
5. Call complete_task. Its checks (sections present, citations valid) run automatically; you don't need to verify
   them yourself. If {md} already exists from an earlier attempt, read it once, fix what's missing, and go to step 5."""

REFS_FROM_FILE = ("Read {refs} and call record_references with its entries (title, first author, year, and DOI or "
                  "arXiv id when shown).")
REFS_NONE_FOUND = "No reference list was found in the text; call record_references with an empty list."
REFS_KNOWN = "The reference list is already known from the scholarly index; skip this step."

PART_CHARS = 12_000

LAYOUT = """Plan the report answering: {question}
Read citation_graph.md (the most-cited works) and the paper summaries in papers/*.md, and use search_notes. Write outline.md:
- '# <report title>'
- '## Abstract' (a placeholder line; it's written last)
- '## Introduction and scope'
- '## Literature review' with '### <theme>' subsections grouping the papers, bullets citing notes [n12]
- '## Foundational works': the most-cited papers that were read and why they matter, citing notes
- '## Synthesis: agreements, conflicts, and gaps', citing notes on both sides of each disagreement
- '## Conclusion and summary'"""

SECTION = """Write the report section "{heading}" to {path}, following its part of outline.md (including any '###' \
subsections). Start the file with '## {heading}'. Use the paper summaries in papers/*.md and search_notes; support \
every factual claim with note citations like [n12]. Present disagreements between papers as disagreements."""

ABSTRACT = """Read all section files in sections/ and write sections/00-abstract.md: '## Abstract', then 150-250 words \
covering the question, the scope of the literature (how many papers, how they were selected by citation), the main \
findings, the key disagreements, and the conclusion. Cite the most important notes like [n12]."""


def cfg(job: dict) -> dict:
    c = {**DEFAULTS, **{k: v for k, v in (job.get("inputs") or {}).items() if v not in (None, "")}}
    for k in ("max_papers", "max_rounds", "min_citations", "per_round", "seed_count", "max_parts"):
        c[k] = int(c[k])
    c["min_fraction"] = float(c["min_fraction"])
    return c


def scholar(runner) -> ScholarClient:
    client = getattr(runner, "scholar", None)
    if client is None:
        client = runner.scholar = ScholarClient()
    return client


def require_network(runner, job, task, why: str) -> None:
    """Raises ApprovalPending (task parks) unless open-access network use is pre-approved or approved."""
    JobApprover(runner.approvals, runner, job, task["id"]).request(
        None, job["project_id"], [NET_KEY], "Search open scholarly indexes and download open-access papers", why)


def paper_md(key: str) -> str:
    return f"papers/{slug(key.replace(':', '-'))[:60]}.md"


def pdf_path(key: str) -> str:
    return f"{PDF_DIR}/{slug(key.replace(':', '-'))[:60]}.pdf"


def text_path(key: str) -> str:
    return f"papers/text/{slug(key.replace(':', '-'))[:60]}.md"


def register_work(runner, job, w: Work, round_: int, status: str = "queued", cited_by: int = 0) -> dict:
    return runner.jobs.upsert_paper(job["id"], w.key(), title=w.title, year=w.year, authors=w.authors, doi=w.doi,
                                    openalex_id=w.openalex_id, arxiv_id=w.arxiv_id, oa_pdf_url=w.oa_pdf_url,
                                    meta_references=w.referenced_works, round=round_, status=status,
                                    cited_by_read=cited_by)


def write_seeds_md(ws: Path, papers: list[dict]) -> None:
    lines = ["# Seed papers", "", "| # | Title | Year | Status | Source |", "|---|---|---|---|---|"]
    for i, p in enumerate(papers, 1):
        where = p["file_path"] or (p["doi"] and f"doi:{p['doi']}") or (p["openalex_id"] and f"OpenAlex {p['openalex_id']}") or "-"
        lines.append(f"| {i} | {p['title']} | {p['year'] or ''} | {p['status']} | {where} |")
    (ws / "seeds.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- plan
def _initial_plan(runner, job):
    c = cfg(job)
    if c["seed_mode"] not in ("folder", "list", "query"):
        raise ValueError("seed_mode must be folder, list, or query")
    tasks = [{"key": "seed", "title": "Find the starting papers", "kind": "code", "handler": "seed",
              "instructions": f"Seed mode: {c['seed_mode']}", "done_when": "seed papers registered"}]
    if c["seed_mode"] in ("list", "query"):
        tasks.append({"key": "seed_gate", "title": "Your review of the seed papers", "kind": "gate", "depends_on": ["seed"],
                      "params": {"prompt": "Review seeds.md (the starting papers). Reply 'approve', 'drop 2, 5' to remove "
                                           "papers, or type a different search query."}})
    return tasks


def expand_round(runner, job, round_: int) -> int:
    ws = workspace_of(runner, job)
    papers = [p for p in runner.jobs.list_papers(job["id"], "queued") if p["round"] == round_]
    if not papers:
        return 0
    group = f"round{round_}"
    tasks = [{"key": group, "title": f"Round {round_}: read {len(papers)} paper(s)", "instructions": "-", "done_when": "-"}]
    for i, p in enumerate(papers, 1):
        # Reading tasks are added once the text is available and split into parts (see expand_paper).
        tasks.append({"key": f"a{round_}_{i}", "parent_key": group, "title": f"Get: {p['title'][:70]}", "kind": "code",
                      "handler": "acquire", "params": {"paper": p["key"], "round": round_, "index": i},
                      "instructions": "-", "done_when": "paper text available, split into parts, or skipped"})
        runner.jobs.upsert_paper(job["id"], p["key"], status="reading")
    tasks.append({"key": f"cite_r{round_}", "title": f"Round {round_}: follow the citations", "kind": "code",
                  "handler": "citations", "depends_on": [group], "params": {"round": round_}, "instructions": "-",
                  "done_when": "citation counts computed and next step decided"})
    runner.jobs.append_tasks(job["id"], tasks)
    runner.jobs.journal(job["id"], "plan", f"Round {round_}: added tasks to get and read {len(papers)} paper(s).")
    return len(papers)


_HEADING = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?\s+)?(abstract|introduction|background|related work|methods?|materials and "
                      r"methods|experimental procedures|results(?: and discussion)?|discussion|conclusions?|summary|"
                      r"general discussion|references|bibliography|literature cited|acknowledg(?:e)?ments?)\s*$", re.I)
_MD_HEADING = re.compile(r"^#{1,6}\s+")
_NUMBERED = re.compile(r"^\s*\d{1,2}(?:\.\d{1,2})*\.?\s+[A-Z][^.!?\d()]{2,80}$")    # not page headers like "194 Journal (2012)"


def split_paper(text: str) -> tuple[list[tuple[str, str]], str]:
    """Split extracted paper text into parts of about PART_CHARS at section headings; return (parts, references)."""
    lines = text.splitlines()
    markdown = sum(1 for l in lines if _MD_HEADING.match(l)) >= 3          # e.g. full text converted from PMC XML
    ref_start = None
    for i in range(len(lines) - 1, len(lines) // 3, -1):
        m = _HEADING.match(_MD_HEADING.sub("", lines[i]))
        if m and m.group(1).lower() in ("references", "bibliography", "literature cited"):
            ref_start = i
            break
    body_lines = lines if ref_start is None else lines[:ref_start]
    references = "" if ref_start is None else "\n".join(lines[ref_start:])
    sections: list[tuple[str, list[str]]] = [("Beginning", [])]
    for line in body_lines:
        if _MD_HEADING.match(line) if markdown else (_HEADING.match(line) or _NUMBERED.match(line)):
            sections.append((_MD_HEADING.sub("", line).strip()[:80], [line]))
        else:
            sections[-1][1].append(line)
    parts: list[tuple[str, str]] = []
    names: list[str] = []
    buf: list[str] = []
    for name, sec_lines in sections:
        sec_text = "\n".join(sec_lines)
        if not sec_text.strip():
            continue
        if buf and len("\n".join(buf)) + len(sec_text) > PART_CHARS:
            parts.append((", ".join(names), "\n".join(buf)))
            names, buf = [], []
        while len(sec_text) > PART_CHARS * 1.3:                    # a very long section: cut at a paragraph break
            cut = sec_text.rfind("\n\n", 0, PART_CHARS)
            if cut <= PART_CHARS // 2:
                cut = sec_text.rfind("\n", 0, PART_CHARS)
            if cut <= PART_CHARS // 2:
                cut = PART_CHARS
            parts.append((name, sec_text[:cut]))
            sec_text = sec_text[cut:]
            name = name.removesuffix(" (continued)") + " (continued)"
        names.append(name)
        buf.append(sec_text)
    if buf:
        parts.append((", ".join(names), "\n".join(buf)))
    return parts, references


def prepare_parts(runner, job, p: dict) -> int:
    from ..documents import extract

    ws = workspace_of(runner, job)
    doc = extract(ws / p["file_path"], Path(runner.settings_getter().data_dir) / "doc_cache")
    parts, references = split_paper(doc.text)
    folder = ws / "papers" / slug(p["key"].replace(":", "-"))[:60]
    folder.mkdir(parents=True, exist_ok=True)
    for k, (names, text) in enumerate(parts, 1):
        (folder / f"part-{k:02d}.md").write_text(f"<!-- part {k} of {len(parts)}: {names} -->\n{text}\n", encoding="utf-8")
    if references.strip():
        (folder / "references.txt").write_text(references, encoding="utf-8")
    runner.jobs.upsert_paper(job["id"], p["key"], provenance={**(p.get("provenance") or {}), "parts": len(parts),
                                                              "folder": folder.relative_to(ws).as_posix(),
                                                              "references_file": bool(references.strip()),
                                                              "extraction_warning": doc.warning})
    return len(parts)


def expand_paper(runner, job, acquire_task: dict) -> None:
    p = runner.jobs.get_paper(job["id"], acquire_task["params"]["paper"])
    prov = p.get("provenance") or {}
    if p["status"] in ("skipped", "unavailable") or not prov.get("parts"):
        return
    r, i = acquire_task["params"]["round"], acquire_task["params"]["index"]
    folder, n = prov["folder"], prov["parts"]
    group = acquire_task["parent_key"]
    tasks, part_keys, summaries = [], [], []
    for k in range(1, n + 1):
        key = f"p{r}_{i}_{k}"
        part, summary = f"{folder}/part-{k:02d}.md", f"{folder}/summary-{k:02d}.md"
        part_keys.append(key)
        summaries.append(summary)
        tasks.append({"key": key, "parent_key": group, "title": f"Read part {k}/{n}: {p['title'][:60]}",
                      "depends_on": [acquire_task["key"]],
                      "params": {"paper": p["key"], "part": k, "part_file": part, "paper_source": p["file_path"]},
                      "instructions": PART.format(part=part, k=k, n=n, title=p["title"], source=p["file_path"],
                                                  summary=summary, question=job["goal"]),
                      "done_when": f"{summary} exists with section summaries",
                      # "## " also matches "### ": accept either heading level (run 6 failed 3 times on "## Abstract")
                      "checks": [{"type": "file_contains", "path": summary, "text": "## "}]})
    md = paper_md(p["key"])
    if p["meta_references"]:
        refs = REFS_KNOWN
    elif prov.get("references_file"):
        refs = REFS_FROM_FILE.format(refs=f"{folder}/references.txt")
    else:
        refs = REFS_NONE_FOUND
    tasks.append({"key": f"w{r}_{i}", "parent_key": group, "title": f"Write-up and value: {p['title'][:60]}",
                  "depends_on": part_keys, "review": True, "params": {"paper": p["key"], "assemble": True},
                  "instructions": ASSEMBLE.format(title=p["title"], summaries=", ".join(summaries), source=p["file_path"],
                                                  md=md, question=job["goal"], references=refs),
                  "done_when": f"{md} has section summaries, key claims citing notes, and a value assessment; "
                               "references known",
                  "checks": [{"type": "file_contains", "path": md, "text": "## Value of this paper"},
                             {"type": "citations_valid", "path": md},
                             {"type": "references_recorded", "paper": p["key"]}]})
    runner.jobs.append_tasks(job["id"], tasks)


def expand_report(runner, job, reason: str) -> None:
    runner.jobs.append_tasks(job["id"], [
        {"key": "layout", "title": "Plan the report layout", "instructions": LAYOUT.format(question=job["goal"]),
         "done_when": "outline.md has abstract, introduction, literature review, foundational works, synthesis, "
                      "and conclusion sections", "review": True,
         "checks": [{"type": "file_contains", "path": "outline.md", "text": "## Abstract"},
                    {"type": "file_contains", "path": "outline.md", "text": "## Literature review"},
                    {"type": "file_contains", "path": "outline.md", "text": "## Conclusion and summary"},
                    {"type": "citations_valid", "path": "outline.md"}]},
        {"key": "layout_gate", "title": "Your review of the report layout", "kind": "gate", "depends_on": ["layout"],
         "params": {"prompt": f"Reading finished ({reason}). Review outline.md. Reply 'approve' to write the report, "
                              "or describe what to change."}},
    ])
    runner.jobs.journal(job["id"], "plan", f"Literature search finished: {reason}. Added report planning.")


def expand_sections(runner, job) -> None:
    ws = workspace_of(runner, job)
    headings = [h.strip() for h in re.findall(r"^##\s+(.+)$", (ws / "outline.md").read_text(encoding="utf-8"), re.M)]
    body = [h for h in headings if h.lower() != "abstract"]
    tasks = [{"key": "write", "title": "Write the report", "instructions": "-", "done_when": "-", "depends_on": ["layout_gate"]}]
    keys = []
    for i, heading in enumerate(body, 1):
        path = f"sections/{i:02d}-{slug(heading)}.md"
        keys.append(f"s{i}")
        tasks.append({"key": f"s{i}", "parent_key": "write", "title": f"Write section: {heading}", "review": True,
                      "instructions": SECTION.format(heading=heading, path=path),
                      "done_when": f"{path} exists, starts with the heading, and cites valid notes",
                      "checks": [{"type": "file_contains", "path": path, "text": f"## {heading}"},
                                 {"type": "citations_valid", "path": path}]})
    tasks.append({"key": "abstract", "parent_key": "write", "title": "Write the abstract", "instructions": ABSTRACT,
                  "depends_on": keys, "review": True, "done_when": "sections/00-abstract.md exists with a cited abstract",
                  "checks": [{"type": "file_contains", "path": "sections/00-abstract.md", "text": "## Abstract"},
                             {"type": "citations_valid", "path": "sections/00-abstract.md"}]})
    tasks.append({"key": "compile", "title": "Compile the report", "kind": "code", "handler": "compile",
                  "depends_on": ["write"], "instructions": "-", "done_when": "report exists"})
    runner.jobs.append_tasks(job["id"], tasks)


# ---------------------------------------------------------------- handlers
def handle_seed(runner, job, task) -> HandlerResult:
    c = cfg(job)
    ws = workspace_of(runner, job)
    mode = c["seed_mode"]
    if mode == "folder":
        folder = c["seeds"] or "papers"
        files = find_sources(ws, folder)
        if not files:
            return HandlerResult(False, f"No papers found in {folder}/", retry_guidance=f"Add papers to {folder}/")
        for f in files[:c["max_papers"]]:
            rel = f.relative_to(ws).as_posix()
            runner.jobs.upsert_paper(job["id"], "local:" + rel, title=f.stem.replace("_", " "), file_path=rel, round=0,
                                     status="queued", provenance={"source": "local file"})
    else:
        require_network(runner, job, task, f"Find seed papers for: {c['seeds'] or job['goal']}")
        client = scholar(runner)
        works: list[Work] = []
        if mode == "query":
            works = client.search(c["seeds"] or job["goal"], c["seed_count"])
        else:
            for line in [l.strip() for l in str(c["seeds"]).splitlines() if l.strip()]:
                doi = re.search(r"10\.\d{4,9}/\S+", line)
                arxiv = re.search(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:)\s*([\w.\-/]+?)(?:v\d+)?(?:\.pdf)?$", line, re.I)
                w = client.get_by_doi(doi.group(0).rstrip(".,")) if doi else None
                if w is None and not arxiv:
                    w = client.find_by_title(line)
                if w is None:
                    w = Work(None, line, arxiv_id=arxiv.group(1) if arxiv else None,
                             oa_pdf_url=f"https://arxiv.org/pdf/{arxiv.group(1)}" if arxiv else None)
                works.append(w)
        if not works:
            return HandlerResult(False, "The search found no papers.", retry_guidance="Try a different query.")
        for w in works:
            register_work(runner, job, w, 0)
    papers = [p for p in runner.jobs.list_papers(job["id"]) if p["round"] == 0]
    write_seeds_md(ws, papers)
    return HandlerResult(True, f"Registered {len(papers)} seed paper(s); listed in seeds.md.")


def _ready(runner, job, task, p: dict, summary: str, answer: str) -> HandlerResult:
    try:
        n = prepare_parts(runner, job, runner.jobs.get_paper(job["id"], p["key"]))
    except Exception as e:
        runner.jobs.upsert_paper(job["id"], p["key"], status="unavailable")
        return HandlerResult(True, f"{summary} But the text couldn't be extracted ({e}); skipping this paper.")
    limit = cfg(job)["max_parts"]
    if n > limit and "read all" not in answer:
        return HandlerResult(False, f"Long paper ({n} parts)", wait_question=(
            f"\"{p['title']}\" is long: {n} parts (about {n * PART_CHARS // 3000} pages), more than the {limit}-part "
            "limit per paper. Reply 'read all' to read it section by section anyway, or 'skip'."))
    warning = (runner.jobs.get_paper(job["id"], p["key"]).get("provenance") or {}).get("extraction_warning")
    return HandlerResult(True, f"{summary} Split into {n} part(s)." + (f" Warning: {warning}" if warning else ""))


def handle_acquire(runner, job, task) -> HandlerResult:
    import datetime as dt

    ws = workspace_of(runner, job)
    p = runner.jobs.get_paper(job["id"], task["params"]["paper"])
    answers = [m.group(1).lower() for g in task["guidance"] for m in [re.search(r'They answered: "(.*)"$', g, re.S)] if m]
    answer = answers[-1] if answers else ""
    if "skip" in answer:
        runner.jobs.upsert_paper(job["id"], p["key"], status="skipped")
        return HandlerResult(True, "Skipped at your request.")
    if p["file_path"] and (ws / p["file_path"]).is_file():
        runner.jobs.upsert_paper(job["id"], p["key"], status="reading")
        return _ready(runner, job, task, p, f"Using {p['file_path']}.", answer)
    target = pdf_path(p["key"])
    if (ws / target).is_file():
        runner.jobs.upsert_paper(job["id"], p["key"], file_path=target, status="reading",
                                 provenance={"source": "added by user"})
        return _ready(runner, job, task, p, f"Using {target} (added by you).", answer)
    require_network(runner, job, task, f"Find and download an open-access copy of \"{p['title']}\"")
    client = scholar(runner)
    tried = []
    for url in client.candidate_pdf_urls(p):
        tried.append(url)
        try:
            if client.download_pdf(url, ws / target):
                runner.jobs.upsert_paper(job["id"], p["key"], file_path=target, status="reading", provenance={
                    "source": "open-access download", "url": url, "retrieved": dt.datetime.now().isoformat()})
                return _ready(runner, job, task, p, f"Downloaded the open-access PDF from {url}.", answer)
        except Exception as e:
            runner.jobs.journal(job["id"], "acquire", f"Download failed from {url}: {e}", task["key"])
    full = client.full_text(p)
    if full:
        text, url = full
        rel = text_path(p["key"])
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text(text, encoding="utf-8")
        runner.jobs.upsert_paper(job["id"], p["key"], file_path=rel, status="reading", provenance={
            "source": "open-access full text (Europe PMC)", "url": url, "retrieved": dt.datetime.now().isoformat()})
        return _ready(runner, job, task, p, f"Saved the open-access full text from Europe PMC to {rel}.", answer)
    if tried:
        runner.jobs.journal(job["id"], "acquire", f"No usable PDF or full text for \"{p['title']}\" "
                                                  f"({len(tried)} PDF location(s) tried).", task["key"])
    runner.jobs.upsert_paper(job["id"], p["key"], status="unavailable")
    year = f" ({p['year']})" if p["year"] else ""
    return HandlerResult(False, "No open-access copy", wait_question=(
        f"No open-access copy found for \"{p['title']}\"{year}. Add the PDF to the workspace as {target} and reply "
        "'added', or reply 'skip'."))


def reference_keys(p: dict) -> list[tuple[str, dict]]:
    if p["meta_references"]:
        return [(f"oa:{w}", {"openalex_id": w}) for w in p["meta_references"]]
    return [(r["key"], r) for r in p["extracted_references"]]


def handle_citations(runner, job, task) -> HandlerResult:
    c = cfg(job)
    ws = workspace_of(runner, job)
    round_ = int(task["params"]["round"])
    papers = runner.jobs.list_papers(job["id"])
    read = [p for p in papers if p["status"] == "read"]
    if not read:
        answers = [m.group(1).lower() for g in task["guidance"]
                   for m in [re.search(r'They answered: "(.*)"$', g, re.S)] if m]
        if not (answers and "continue" in answers[-1]):
            missing = [p["title"] for p in papers if p["status"] in ("unavailable", "skipped")]
            return HandlerResult(False, "No papers were read", wait_question=(
                f"No papers could be read so far ({len(missing)} unavailable or skipped; see the job journal). "
                "A report now would have no sources. Stop the job and start again with a folder of PDFs or a "
                "different query, or reply 'continue' to go on anyway."))
    known = {p["key"] for p in papers}
    counts: dict[str, int] = {}
    info: dict[str, dict] = {}
    for p in read:
        for key, meta in {k: m for k, m in reference_keys(p)}.items():
            counts[key] = counts.get(key, 0) + 1
            info.setdefault(key, meta)
    for p in papers:                                   # how often read papers cite papers we have
        if p["key"] in counts:
            runner.jobs.upsert_paper(job["id"], p["key"], cited_by_read=counts[p["key"]])
    threshold = max(c["min_citations"], math.ceil(c["min_fraction"] * len(read)))
    candidates = sorted(((k, n) for k, n in counts.items() if k not in known and n >= threshold),
                        key=lambda kv: -kv[1])
    reason = None
    if len(read) >= c["max_papers"]:
        reason = f"reached the {c['max_papers']}-paper limit"
    elif round_ >= c["max_rounds"]:
        reason = f"reached the {c['max_rounds']}-round limit"
    elif not candidates:
        reason = f"converged: no unread work is cited by {threshold} or more of the {len(read)} papers read"
    rounds = (job.get("inputs") or {}).get("citation_rounds") or []
    entry = {"round": round_, "read": len(read), "threshold": threshold, "candidates": len(candidates)}
    by_id: dict[str, Work] = {}
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:GRAPH_ROWS]
    lookup = [k[3:] for k, _ in ([] if reason else candidates[:50]) + top if k.startswith("oa:") and k not in known]
    title_lookups = not reason and any(k.startswith(("doi:", "t:")) for k, _ in candidates[:c["per_round"]])
    if lookup or title_lookups:
        require_network(runner, job, task, f"Look up cited papers for round {round_ + 1}")
    if lookup:
        try:
            by_id = {f"oa:{w.openalex_id}": w for w in scholar(runner).get_by_ids(list(dict.fromkeys(lookup)))}
        except Exception as e:
            runner.jobs.journal(job["id"], "citations", f"Couldn't look up cited works: {e}", task["key"])
    # Ties are common (every work cited by both of two papers): prefer works cited more widely overall.
    candidates.sort(key=lambda kv: (-kv[1], -((by_id.get(kv[0]) and by_id[kv[0]].cited_by_count) or 0)))
    if reason:
        entry["stop"] = reason
    else:
        take = candidates[:min(c["per_round"], c["max_papers"] - len(read))]
        client = scholar(runner)
        added = 0
        for k, n in take:
            meta = info.get(k, {})
            w = by_id.get(k)
            if w is None and k.startswith("doi:"):
                w = client.get_by_doi(k[4:])
            if w is None and meta.get("title"):
                w = client.find_by_title(meta["title"], meta.get("year"))
            if w is None:
                w = Work(None, meta.get("title") or k, meta.get("year"), doi=meta.get("doi"), arxiv_id=meta.get("arxiv"),
                         oa_pdf_url=f"https://arxiv.org/pdf/{meta['arxiv']}" if meta.get("arxiv") else None)
            # keep the counted key so later rounds recognize the work as known
            runner.jobs.upsert_paper(job["id"], k, title=w.title, year=w.year, authors=w.authors, doi=w.doi,
                                     openalex_id=w.openalex_id, arxiv_id=w.arxiv_id, oa_pdf_url=w.oa_pdf_url,
                                     meta_references=w.referenced_works, round=round_ + 1, status="queued",
                                     cited_by_read=n)
            added += 1
        entry["selected"] = added
    titles = {k: {"title": w.title, "year": w.year, "cited_by_count": w.cited_by_count} for k, w in by_id.items()}
    merged = {k: {**info.get(k, {}), **titles.get(k, {})} for k in set(info) | set(titles)}
    _write_graph(ws, counts, merged, runner.jobs.list_papers(job["id"]), threshold)
    rounds.append(entry)
    runner.jobs.update_job(job["id"], inputs={**(job.get("inputs") or {}), "citation_rounds": rounds})
    if reason:
        return HandlerResult(True, f"Stopping the literature search: {reason}. See citation_graph.md.")
    return HandlerResult(True, f"Round {round_}: {len(read)} papers read; {len(candidates)} unread works cited by ≥{threshold}; "
                               f"reading the top {entry['selected']} next. See citation_graph.md.")


GRAPH_ROWS = 40


def _write_graph(ws: Path, counts, info, papers, threshold) -> None:
    by_key = {p["key"]: p for p in papers}
    lines = ["# Citation graph", "", f"Works cited by the papers read so far (threshold for following: {threshold}). "
             "Ties are ordered by total citations.", "",
             "| Cited by (papers read) | Work | Year | Total citations | Status |", "|---|---|---|---|---|"]
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], -(info.get(kv[0], {}).get("cited_by_count") or 0)))
    for k, n in rows[:GRAPH_ROWS]:
        p, meta = by_key.get(k), info.get(k, {})
        title = (p and p["title"]) or meta.get("title") or k
        year = (p and p["year"]) or meta.get("year") or ""
        total = meta.get("cited_by_count")
        lines.append(f"| {n} | {title} | {year} | {total if total is not None else ''} | {(p and p['status']) or 'not read'} |")
    (ws / "citation_graph.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def handle_compile(runner, job, task) -> HandlerResult:
    papers = runner.jobs.list_papers(job["id"])
    read = sorted((p for p in papers if p["status"] == "read"), key=lambda p: -p["cited_by_read"])
    missing = [p for p in papers if p["status"] in ("unavailable", "skipped")]
    rounds = (job.get("inputs") or {}).get("citation_rounds") or []
    lines = ["## Bibliography", ""]
    for p in read:
        authors = ", ".join(p["authors"][:3]) + (" et al." if len(p["authors"]) > 3 else "")
        ident = f" doi:{p['doi']}" if p["doi"] else (f" arXiv:{p['arxiv_id']}" if p["arxiv_id"] else "")
        cited = f" — cited by {p['cited_by_read']} of the papers read" if p["cited_by_read"] else ""
        lines.append(f"- {authors + '. ' if authors else ''}{p['title']} ({p['year'] or 'n.d.'}).{ident}{cited}")
    lines += ["", "## How the literature was gathered", "",
              f"{len(read)} papers were read over {len(rounds)} citation round(s)."]
    for r in rounds:
        lines.append(f"- Round {r['round']}: {r['read']} read, follow threshold {r['threshold']}, "
                     + (f"stopped: {r['stop']}" if r.get("stop") else f"{r.get('selected', 0)} cited works selected next"))
    if missing:
        lines += ["", "Papers that couldn't be obtained or were skipped:"] + [f"- {p['title']} ({p['year'] or 'n.d.'})"
                                                                            for p in missing]
    return compile_report(runner, job, task, title_default="Literature review", front=["sections/00-abstract.md"],
                          extra_appendix="\n".join(lines))


# ---------------------------------------------------------------- template
class DeepResearch(Template):
    def initial_plan(self, runner, job):
        return _initial_plan(runner, job)

    def on_task_done(self, runner, job, task):
        c = cfg(job)
        key = task["key"]
        if key == "seed" and c["seed_mode"] == "folder" or key == "seed_gate":
            expand_round(runner, job, 0)
        elif task["kind"] == "code" and task["handler"] == "acquire":
            expand_paper(runner, job, task)
        elif task["params"].get("assemble"):
            runner.jobs.upsert_paper(job["id"], task["params"]["paper"], status="read")
        elif key.startswith("cite_r"):
            rounds = (runner.jobs.get_job(job["id"])["inputs"] or {}).get("citation_rounds") or []
            last = rounds[-1] if rounds else {}
            if last.get("stop"):
                expand_report(runner, job, last["stop"])
            elif not expand_round(runner, job, int(task["params"]["round"]) + 1):
                expand_report(runner, job, "no further papers could be queued")
        elif key == "layout_gate":
            expand_sections(runner, job)

    def on_gate(self, runner, job, task, answer):
        if is_approval(answer):
            return "approve"
        tasks = {t["key"]: t for t in runner.jobs.list_tasks(job["id"])}
        if task["key"] == "seed_gate":
            ws = workspace_of(runner, job)
            drop = re.match(r"^\s*(?:drop|remove)\s+([\d,\s]+)$", answer, re.I)
            seeds = [p for p in runner.jobs.list_papers(job["id"]) if p["round"] == 0]
            if drop:
                for n in {int(x) for x in re.findall(r"\d+", drop.group(1))}:
                    if 1 <= n <= len(seeds):
                        runner.jobs.upsert_paper(job["id"], seeds[n - 1]["key"], status="skipped")
                write_seeds_md(ws, runner.jobs.list_papers(job["id"]))
            else:
                for p in seeds:
                    runner.jobs.upsert_paper(job["id"], p["key"], status="skipped", round=-1)
                inputs = {**(job.get("inputs") or {}), "seeds": answer, "seed_mode": "query"}
                runner.jobs.update_job(job["id"], inputs=inputs)
                runner.jobs.update_task(tasks["seed"]["id"], status="pending", attempts=0)
            return "revise"
        if task["key"] == "layout_gate":
            runner.jobs.add_guidance(tasks["layout"]["id"], f"The user reviewed your outline and asked for changes: "
                                                            f"\"{answer}\". Revise outline.md accordingly.")
            runner.jobs.update_task(tasks["layout"]["id"], status="pending", attempts=0)
        return "revise"


DEEP_RESEARCH = register(DeepResearch(
    name="deep_research",
    label="Deep research (citation snowballing)",
    description="Finds starting papers (folder, list, or search), reads each section by section with verified notes, "
                "follows the most-cited references round by round until citations converge, then writes a report "
                "with an abstract, literature review, and conclusion.",
    inputs_schema={
        "seed_mode": {"enum": ["query", "folder", "list"], "label": "Start from", "default": "query"},
        "seeds": {"type": "string", "label": "Search query, folder, or list of titles/DOIs", "default": ""},
        "max_papers": {"type": "string", "label": "Max papers read", "default": "60"},
        "max_rounds": {"type": "string", "label": "Max citation rounds", "default": "4"},
        "min_citations": {"type": "string", "label": "Follow works cited by at least", "default": "3"},
        "min_fraction": {"type": "string", "label": "…or by at least this fraction of papers read", "default": "0.15"},
        "seed_count": {"type": "string", "label": "Seed papers from a search", "default": "10"},
        "per_round": {"type": "string", "label": "Cited papers to add per round", "default": "8"},
        "max_parts": {"type": "string", "label": "Ask before reading papers longer than (parts of ~4 pages)",
                      "default": "12"},
        "format": {"enum": ["md", "docx"], "label": "Report format", "default": "md"},
    },
    handlers={"seed": handle_seed, "acquire": handle_acquire, "citations": handle_citations, "compile": handle_compile},
))
