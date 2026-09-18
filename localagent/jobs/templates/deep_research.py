"""deep_research: literature research by citation snowballing (design §6.4).

Sources are chosen in a loop the coordinator owns and the model judges:

  find (code): search, or pull an article's or a paper's reference list, and rank the results into a candidate pool
  → screen (agent): the model is shown a numbered list with titles and abstracts and calls keep_sources([2, 5, 9])
  → for each kept number the coordinator fetches the paper to prove it exists, drops what it can't reach, dedupes,
    and hands back a fresh numbered list, until the round's quota is filled or the candidates run out.

The model never sees or emits a URL, and never decides whether something is downloadable; the coordinator never
decides whether something is relevant. Then:

  round k: per paper acquire (code: get the file, split it into parts at section headings, set the reference list
  aside) → one short read task per part → write-up (agent, reviewed) → next_r{k} (code): take the reference lists of
  the papers just read, drop everything already seen, rank what is left, and start round k+1's screen
  → ... → layout (agent, reviewed) → layout gate → sections (agent, reviewed) → abstract → compile (code)
"""
from __future__ import annotations

import re
from pathlib import Path

from ..scholar import ScholarClient, Work, normalize_title, obtainable
from ..sessions import JobApprover
from . import register
from .base import HandlerResult, Template, is_approval
from .research_report import compile_report, find_sources, slug, workspace_of

DEFAULTS = {"seed_mode": "query", "seeds": "", "max_papers": 60, "max_rounds": 4, "per_round": 8, "seed_count": 10,
            "candidates_per_round": 60, "screen_batch": 15, "max_parts": 12, "open_access_only": "yes",
            "format": "md"}
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
5. Call check_citations on {md} once and fix any ids it lists. Then call complete_task; its other checks run
   automatically, so you don't need to verify them yourself. If {md} already exists from an earlier attempt, read it once, fix what's missing, and go to step 5."""

REFS_FROM_FILE = ("Read {refs} and call record_references with its entries (title, first author, year, and DOI or "
                  "arXiv id when shown).")
REFS_NONE_FOUND = "No reference list was found in the text; call record_references with an empty list."
REFS_KNOWN = "The reference list is already known from the scholarly index; skip this step."

PART_CHARS = 12_000

LAYOUT = """Plan the report answering: {question}
Read literature_digest.md (every paper read: its value and key claims with note ids, plus the most-cited works). It
is built to fit your context; don't open the full papers/*.md write-ups. Only cite note ids that appear in the digest.
Write outline.md once:
- '# <report title>'
- '## Abstract' (a placeholder line; it's written last)
- '## Introduction and scope'
- '## Literature review' with '### <theme>' subsections grouping the papers, bullets citing notes [n12]
- '## Foundational works': the most-cited works and why they matter. For works that weren't read (status other than
  read in the digest's table), say so, and cite a note only if that note is from a read paper and actually discusses
  the work; otherwise give no citation rather than borrowing an unrelated note
- '## Synthesis: agreements, conflicts, and gaps', citing notes on both sides of each disagreement
- '## Conclusion and summary'
Then call check_citations on outline.md once, fix any ids it lists, and call complete_task (the section checks run
automatically; don't verify them yourself)."""

SECTION = """Write the report section "{heading}" to {path}. Start the file with '## {heading}'. Your part of outline.md
(follow it, including any '###' subsections; you don't need to open outline.md):

{part}

For an overview of the papers, read literature_digest.md; for detail on a point, call search_notes with a few keywords
(brief true) instead of opening the full papers/*.md write-ups. If a note the outline cites doesn't support its
claim, find the right note with a keyword search or leave the claim out; don't get stuck on it. Some works (for
example most-cited works that weren't read) have no notes of their own: describe them through the papers that cite
them and say they weren't read, rather than searching for evidence that isn't there. Support every
factual claim with note citations like [n12]. Present disagreements between papers as disagreements. Write only this
section: one '##' heading at the top and '###' for anything below it. Write the file
once, call check_citations on it and fix any ids it lists, then call complete_task (its checks run automatically)."""

ABSTRACT = """Read sections/_digest.md (the opening and key points of every section, built to fit your context) and write
sections/00-abstract.md: '## Abstract', then 150-250 words covering the question, the scope of the literature (how many
papers, how they were selected by citation), the main findings, the key disagreements, and the conclusion. Cite the most
important notes like [n12], using only note ids that appear in the digest. Call check_citations on the file, fix any
ids it lists, then call complete_task.
Facts about the scope, from the job's records (use these numbers exactly; don't count notes as papers): {facts}"""

REVIEWED_ATTEMPTS = 5
DIGEST_CHARS = 24_000          # about 6k tokens: fits a 20k-token context with room for the task and the answer
SECTION_DIGEST_CHARS = 16_000


def cfg(job: dict) -> dict:
    c = {**DEFAULTS, **{k: v for k, v in (job.get("inputs") or {}).items() if v not in (None, "")}}
    for k in ("max_papers", "max_rounds", "per_round", "seed_count", "candidates_per_round", "screen_batch",
              "max_parts"):
        c[k] = int(c[k])
    return c


def quota_for(c: dict, pass_: int) -> int:
    """Sources wanted from a round: the seed count for round 0, then the per-round count, never more than the job's
    total paper limit allows."""
    return max(1, min(c["seed_count"] if pass_ == 0 else c["per_round"], c["max_papers"]))


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


def resolve_references(client, refs: list[dict], want: int, open_access: bool) -> list[Work]:
    """Turn reference entries (from an encyclopedia article or a reference list) into works, keeping the ones we can
    actually read when open_access is set. Stops once `want` are found."""
    out: list[Work] = []
    for r in refs:
        w = client.get_by_doi(r["doi"]) if r.get("doi") else None
        if w is None and r.get("title"):
            w = client.find_by_title(r["title"], r.get("year"))
        if w is None and r.get("arxiv"):
            w = Work(None, r.get("title") or r["arxiv"], r.get("year"), arxiv_id=r["arxiv"],
                     oa_pdf_url=f"https://arxiv.org/pdf/{r['arxiv']}")
        if w is None or (open_access and not obtainable(w)):
            continue
        out.append(w)
        if len(out) >= want:
            break
    return out


def register_work(runner, job, w: Work, round_: int, status: str = "queued", cited_by: int = 0) -> dict:
    return runner.jobs.upsert_paper(job["id"], w.key(), title=w.title, year=w.year, authors=w.authors, doi=w.doi,
                                    openalex_id=w.openalex_id, arxiv_id=w.arxiv_id, oa_pdf_url=w.oa_pdf_url,
                                    meta_references=w.referenced_works, round=round_, status=status,
                                    cited_by_read=cited_by)


# ---------------------------------------------------------------- source selection (model screens, code verifies)
SCREEN = """Choose which of these papers to read, for this question: {question}

{list}

Call keep_sources with the numbers of the ones worth reading, like keep_sources(keep=[2, 5, 9]). Judge from the title
and abstract: keep a paper if it would give evidence, methods, or results bearing on the question, and leave out
anything off-topic, duplicated, or too general to help. A few good papers beat a padded list.

Each call returns a fresh numbered list with more candidates, until {quota} sources are gathered or the candidates run
out. Numbers never change, so you can still keep an earlier one. Don't think about links, files, or whether a paper can
be downloaded: that is handled for you, and a paper that turns out to be unavailable is replaced automatically. If
nothing in a list is relevant, call keep_sources with an empty list to see the next one. When the tool tells you the
search is finished, call complete_task with a sentence on what you kept and why."""

MAX_BATCHES = 8          # lists a screening task may be shown, so an indecisive run still ends


def pool(runner, job, pass_: int, status: str | None = None) -> list[dict]:
    """This pass's candidates, in the order the coordinator ranked them."""
    out = [p for p in runner.jobs.list_papers(job["id"]) if (p.get("provenance") or {}).get("pass") == pass_
           and (status is None or p["status"] == status)]
    return sorted(out, key=lambda p: (p.get("provenance") or {}).get("number") or 0)


def selected_in(runner, job, pass_: int) -> list[dict]:
    return [p for p in pool(runner, job, pass_) if p["status"] in ("queued", "reading", "read")]


def register_candidates(runner, job, works: list, pass_: int, found_by: str = "") -> int:
    """Add works to a pass's candidate pool, skipping anything the job has already seen. Deduplication is the
    coordinator's job: the same paper arrives under different ids (OpenAlex, DOI, arXiv) and from several reference
    lists at once, and the model must never be asked about it twice."""
    papers = runner.jobs.list_papers(job["id"])
    seen_keys = {p["key"] for p in papers}
    seen_titles = {normalize_title(p["title"]) for p in papers if p["title"]}
    number = max([(p.get("provenance") or {}).get("number") or 0 for p in papers] or [0])
    added = 0
    for w in works:
        title = normalize_title(w.title or "")
        if w.key() in seen_keys or (title and title in seen_titles):
            continue
        seen_keys.add(w.key())
        if title:
            seen_titles.add(title)
        number += 1
        added += 1
        register_work(runner, job, w, pass_, status="candidate")
        runner.jobs.upsert_paper(job["id"], w.key(), provenance={
            "pass": pass_, "number": number, "found_by": found_by, "abstract": (w.abstract or "")[:1200],
            "venue": w.venue, "cited_by_count": w.cited_by_count})
    return added


def candidate_list(batch: list[dict], pass_: int, have: int, quota: int) -> str:
    first, last = batch[0]["provenance"]["number"], batch[-1]["provenance"]["number"]
    lines = [f"## Candidate papers {first}-{last} (round {pass_}; {have} of {quota} sources gathered so far)", ""]
    for p in batch:
        prov = p.get("provenance") or {}
        authors = ", ".join(p["authors"][:3]) + (" et al." if len(p["authors"]) > 3 else "")
        bits = [b for b in (authors, str(p["year"] or ""), prov.get("venue") or "") if b]
        if prov.get("cited_by_count"):
            bits.append(f"cited {prov['cited_by_count']:,} times")
        found_by = f" [{prov['found_by']}]" if prov.get("found_by") else ""
        lines.append(f"{prov['number']}. **{p['title']}** — {'; '.join(bits)}{found_by}")
        abstract = re.sub(r"\s+", " ", prov.get("abstract") or "").strip()
        lines.append("   " + (abstract[:600] + ("…" if len(abstract) > 600 else "") if abstract
                              else "(no abstract available)"))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def next_batch(runner, job, pass_: int, size: int) -> list[dict]:
    """The next candidates to show, marked shown so a retry or a later batch never repeats them."""
    batch = [p for p in pool(runner, job, pass_, "candidate") if not (p.get("provenance") or {}).get("shown")][:size]
    for p in batch:
        runner.jobs.upsert_paper(job["id"], p["key"], provenance={**p["provenance"], "shown": True})
    return [runner.jobs.get_paper(job["id"], p["key"]) for p in batch]


def show_batch(runner, job, pass_: int, size: int, quota: int) -> str | None:
    """Write the next numbered list to candidates.md and return it; None when the pool is used up."""
    batch = next_batch(runner, job, pass_, size)
    if not batch:
        return None
    text = candidate_list(batch, pass_, len(selected_in(runner, job, pass_)), quota)
    (workspace_of(runner, job) / "candidates.md").write_text(text, encoding="utf-8")
    return text


def write_sources_md(runner, job) -> None:
    """The coordinator's record of every source: where it came from, what was decided, and the URL it was fetched
    from. The model never emits or reads URLs; this file is for the user and for later runs."""
    papers = sorted(runner.jobs.list_papers(job["id"]),
                    key=lambda p: ((p.get("provenance") or {}).get("pass") or 0,
                                   (p.get("provenance") or {}).get("number") or 0))
    lines = ["# Sources", "",
             "Every paper the model was shown and what happened when the coordinator went to fetch the ones it kept. "
             "Papers with no reachable copy were dropped and replaced automatically.", "",
             "| # | Round | Title | Year | Decision | Where it came from |", "|---|---|---|---|---|---|"]
    verdict = {"queued": "kept, waiting to be read", "reading": "kept, being read", "read": "read",
               "rejected": "not chosen by the model", "unavailable": "dropped: no reachable copy",
               "skipped": "skipped", "candidate": "not shown"}
    for p in papers:
        prov = p.get("provenance") or {}
        where = prov.get("url") or prov.get("source") or prov.get("found_by") or ""
        title = str(p["title"]).replace("|", "\\|")[:110]
        lines.append(f"| {prov.get('number') or ''} | {prov.get('pass', '')} | {title} | {p['year'] or ''} | "
                     f"{verdict.get(p['status'], p['status'])} | {where} |")
    kept = sum(1 for p in papers if p["status"] in ("queued", "reading", "read"))
    dropped = sum(1 for p in papers if p["status"] == "unavailable")
    shown = sum(1 for p in papers if (p.get("provenance") or {}).get("shown"))
    lines += ["", f"{shown} candidates shown to the model; {kept} kept and reachable; {dropped} dropped as unreachable."]
    (workspace_of(runner, job) / "sources.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def verify_source(runner, job, task, p: dict) -> tuple[bool, str]:
    """Go and get the paper the model chose. A source counts only once a URL actually hands us the document:
    OpenAlex's is_oa flag says nothing about whether a publisher answers a script (it usually doesn't)."""
    if p["file_path"]:
        runner.jobs.upsert_paper(job["id"], p["key"], status="queued")
        return True, "local file"
    try:
        found = scholar(runner).locate(p)
    except Exception as e:
        runner.jobs.journal(job["id"], "sources", f"Lookup failed for \"{p['title']}\": {e}", task["key"])
        found = None
    if not found:
        runner.jobs.upsert_paper(job["id"], p["key"], status="unavailable",
                                 provenance={**(p.get("provenance") or {}), "unreachable": True})
        return False, "no reachable copy"
    url, how = found
    runner.jobs.upsert_paper(job["id"], p["key"], status="queued",
                             provenance={**(p.get("provenance") or {}), "url": url, "how": how})
    return True, url


def screen_keep(runner, job, task, keep: list[int], note: str = "") -> str:
    """One turn of the selection loop, run by the keep_sources tool.

    The model sends list numbers only. The coordinator resolves them to papers, fetches each one to prove it exists,
    drops what it can't reach, and hands back a fresh list until the quota is filled or the candidates run out.
    """
    c = cfg(job)
    pass_ = int(task["params"]["pass"])
    quota = int(task["params"]["quota"])
    batches = int((task["params"] or {}).get("batches") or 1)
    by_number = {(p.get("provenance") or {}).get("number"): p for p in pool(runner, job, pass_)}
    wanted = list(dict.fromkeys(int(n) for n in keep))
    require_network(runner, job, task, "Fetch the papers chosen from the candidate list")

    taken, unreachable, ignored = [], [], []
    for n in wanted:
        p = by_number.get(n)
        if p is None or p["status"] not in ("candidate", "rejected"):
            ignored.append(n)
            continue
        ok, where = verify_source(runner, job, task, runner.jobs.get_paper(job["id"], p["key"]))
        (taken if ok else unreachable).append(p)
        runner.jobs.journal(job["id"], "sources",
                            f"{'Kept' if ok else 'Dropped'} #{n} \"{p['title'][:70]}\": {where}", task["key"])
    for p in pool(runner, job, pass_, "candidate"):          # everything shown and not kept is decided
        if (p.get("provenance") or {}).get("shown"):
            runner.jobs.upsert_paper(job["id"], p["key"], status="rejected")
    write_sources_md(runner, job)
    runner._changed(job["id"])

    have = len(selected_in(runner, job, pass_))
    lines = []
    if taken:
        lines.append(f"Kept {len(taken)}: " + "; ".join(f"#{(p['provenance'] or {}).get('number')} {p['title'][:60]}"
                                                        for p in taken))
    if unreachable:
        lines.append(f"Dropped {len(unreachable)} with no reachable copy (replaced below): "
                     + "; ".join(f"#{(p['provenance'] or {}).get('number')}" for p in unreachable))
    if ignored:
        lines.append(f"Not in the current list, ignored: {', '.join('#' + str(n) for n in ignored)}.")
    lines.append(f"{have} of {quota} sources gathered.")

    if have >= quota:
        return "\n".join(lines) + "\nThe search is finished. Call complete_task now."
    if batches >= MAX_BATCHES:
        return "\n".join(lines) + (f"\nThat was the last list for this round ({MAX_BATCHES} shown). "
                                   "Call complete_task now.")
    runner.jobs.update_task(task["id"], params={**task["params"], "batches": batches + 1})
    text = show_batch(runner, job, pass_, int(task["params"].get("batch_size") or c["screen_batch"]), quota)
    if text is None:
        return "\n".join(lines) + "\nNo candidates are left for this round. Call complete_task now."
    return "\n".join(lines) + "\n\n" + text


# ---------------------------------------------------------------- plan
def _initial_plan(runner, job):
    c = cfg(job)
    if c["seed_mode"] not in ("folder", "list", "query", "wikipedia"):
        raise ValueError("seed_mode must be folder, list, query, or wikipedia")
    return [{"key": "find0", "title": "Find candidate papers", "kind": "code", "handler": "find",
             "params": {"pass": 0}, "instructions": f"Seed mode: {c['seed_mode']}",
             "done_when": "candidate papers found and ranked"}]


def expand_screen(runner, job, pass_: int, after: str) -> bool:
    """Add the round's screening task, with the first numbered list in its instructions."""
    c = cfg(job)
    quota = quota_for(c, pass_)
    text = show_batch(runner, job, pass_, c["screen_batch"], quota)
    if text is None:
        return False
    runner.jobs.append_tasks(job["id"], [{
        "key": f"screen{pass_}", "title": f"Round {pass_}: choose which papers to read", "depends_on": [after],
        "params": {"screen": True, "pass": pass_, "quota": quota, "batch_size": c["screen_batch"], "batches": 1},
        "instructions": SCREEN.format(question=job["goal"], list=text, quota=quota),
        "done_when": f"up to {quota} relevant papers kept, each with a copy the coordinator could fetch",
        "checks": [{"type": "sources_screened", "pass": pass_}]}])
    return True


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
    tasks.append({"key": f"next_r{round_}", "title": f"Round {round_}: follow the citations", "kind": "code",
                  "handler": "next_pass", "depends_on": [group], "params": {"round": round_}, "instructions": "-",
                  "done_when": "cited works ranked into the next round's candidates, or the search stopped"})
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
        {"key": "digest", "title": "Build the literature digest", "kind": "code", "handler": "digest",
         "instructions": "-", "done_when": "literature_digest.md exists"},
        {"key": "layout", "title": "Plan the report layout", "instructions": LAYOUT.format(question=job["goal"]),
         "depends_on": ["digest"],
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
    outline = (ws / "outline.md").read_text(encoding="utf-8")
    headings = [h.strip() for h in re.findall(r"^##\s+(.+)$", outline, re.M)]
    body = [h for h in headings if h.lower() != "abstract"]
    tasks = [{"key": "write", "title": "Write the report", "instructions": "-", "done_when": "-", "depends_on": ["layout_gate"]}]
    keys = []
    for i, heading in enumerate(body, 1):
        path = f"sections/{i:02d}-{slug(heading)}.md"
        keys.append(f"s{i}")
        tasks.append({"key": f"s{i}", "parent_key": "write", "title": f"Write section: {heading}", "review": True,
                      # Reviewed writing converges slowly: each pass fixes the named issue and often adds another.
                      "max_attempts": REVIEWED_ATTEMPTS,
                      # The outline part goes in the instructions, which context fitting never shortens.
                      "instructions": SECTION.format(heading=heading, path=path,
                                                     part=(_md_section(outline, heading) or "(no notes in the outline)")[:4000]),
                      "done_when": f"{path} exists, starts with the heading, and cites valid notes",
                      "checks": [{"type": "file_contains", "path": path, "text": f"## {heading}"},
                                 {"type": "one_section", "path": path},
                                 # min 0: a section about works that weren't read may honestly cite nothing
                                 {"type": "citations_valid", "path": path, "min": 0}]})
    tasks.append({"key": "section_digest", "parent_key": "write", "title": "Build the section digest", "kind": "code",
                  "handler": "section_digest", "depends_on": keys, "instructions": "-",
                  "done_when": "sections/_digest.md exists"})
    papers = runner.jobs.list_papers(job["id"])
    rounds = (job.get("inputs") or {}).get("citation_rounds") or []
    read = [p for p in papers if p["status"] == "read"]
    facts = (f"{len(read)} papers read over {len(rounds)} citation round(s), starting from "
             f"{sum(1 for p in papers if p['round'] == 0)} seed paper(s) ({cfg(job)['seed_mode']} seeds); "
             f"{len(runner.jobs.list_notes(job['id']))} verified notes; "
             f"{sum(1 for p in papers if p['status'] in ('unavailable', 'skipped'))} papers couldn't be obtained or "
             f"were skipped. Search stop reason: {(rounds[-1].get('stop') if rounds else None) or 'not recorded'}. "
             f"Papers read: " + "; ".join(f"{p['title']} ({p['year'] or 'n.d.'})" for p in read[:20]))
    tasks.append({"key": "abstract", "parent_key": "write", "title": "Write the abstract",
                  "max_attempts": REVIEWED_ATTEMPTS,
                  "instructions": ABSTRACT.format(facts=facts),
                  "depends_on": ["section_digest"], "review": True, "done_when": "sections/00-abstract.md exists with a cited abstract",
                  "checks": [{"type": "file_contains", "path": "sections/00-abstract.md", "text": "## Abstract"},
                             {"type": "citations_valid", "path": "sections/00-abstract.md"}]})
    tasks.append({"key": "compile", "title": "Compile the report", "kind": "code", "handler": "compile",
                  "depends_on": ["write"], "instructions": "-", "done_when": "report exists"})
    runner.jobs.append_tasks(job["id"], tasks)


def _md_section(text: str, heading: str) -> str:
    m = re.search(rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
    return m.group(1).strip() if m else ""


def build_digest(ws: Path, papers: list[dict], budget: int = DIGEST_CHARS) -> str:
    """Compact view of the literature for report planning: per paper, its value assessment and key claims (with note
    ids), capped so the whole file fits a small model's context however many papers were read."""
    read = sorted((p for p in papers if p["status"] == "read"), key=lambda p: (-p["cited_by_read"], p["round"]))
    head = ["# Literature digest", "", f"{len(read)} papers read. Cite notes by their ids, like [n12].", ""]
    graph = ws / "citation_graph.md"
    if graph.is_file():
        rows = [l for l in graph.read_text(encoding="utf-8").splitlines() if l.startswith("| ") and "---" not in l][:13]
        head += ["## Most-cited works (from citation_graph.md)", ""] + rows + [""]
    # Entries thin out as papers are added (at 60 papers: title, a sentence of value, a claim or two).
    per = max(250, (budget - len("\n".join(head))) // max(1, len(read)))
    out = head + ["## Papers read", ""]
    for p in read:
        md = ws / paper_md(p["key"])
        text = md.read_text(encoding="utf-8", errors="replace") if md.is_file() else ""
        cited = f", cited by {p['cited_by_read']} of the papers read" if p["cited_by_read"] else ""
        entry = [f"### {p['title']} ({p['year'] or 'n.d.'}{cited})", f"Write-up: {paper_md(p['key'])}"]
        value = re.sub(r"\s+", " ", _md_section(text, "Value of this paper"))
        if value:
            room = per - sum(len(l) + 1 for l in entry)
            cut = value[:max(80, int(room * 0.45))]
            entry.append("Value: " + (cut if len(cut) == len(value) else cut.rsplit(" ", 1)[0] + " …"))
        room = per - sum(len(l) + 1 for l in entry)
        for line in _md_section(text, "Key claims").splitlines():
            line = line.strip()
            if not line.startswith(("-", "*")):
                continue
            if len(line) + 1 > room:
                break
            entry.append(line)
            room -= len(line) + 1
        out += entry + [""]
    return "\n".join(out).strip() + "\n"


def handle_digest(runner, job, task) -> HandlerResult:
    ws = workspace_of(runner, job)
    papers = runner.jobs.list_papers(job["id"])
    text = build_digest(ws, papers)
    (ws / "literature_digest.md").write_text(text, encoding="utf-8")
    n = sum(1 for p in papers if p["status"] == "read")
    return HandlerResult(True, f"Wrote literature_digest.md ({n} papers, {len(text):,} characters).")


def handle_section_digest(runner, job, task) -> HandlerResult:
    ws = workspace_of(runner, job)
    files = sorted(f for f in (ws / "sections").glob("*.md") if not f.name.startswith(("_", "00-")))
    per = max(600, SECTION_DIGEST_CHARS // max(1, len(files)))
    out = ["# Section digest", ""]
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace").strip()
        out += [text[:per].rsplit("\n", 1)[0] if len(text) > per else text, ""]
    (ws / "sections" / "_digest.md").write_text("\n".join(out), encoding="utf-8")
    return HandlerResult(True, f"Wrote sections/_digest.md from {len(files)} sections.")


# ---------------------------------------------------------------- handlers
def search_pool(runner, job, task) -> list[tuple]:
    """Round 0's candidates: search results, an encyclopedia article's reference list, or a list the user pasted.
    Returns (works, how they were found). One source per line, each filling its own share of the pool: a single long
    sentence matches common words rather than the topic, and per-source quotas keep a topic's sides balanced (say
    five biology queries and five machine-learning ones)."""
    c = cfg(job)
    client = scholar(runner)
    mode, want = c["seed_mode"], c["candidates_per_round"]
    oa_only = str(c["open_access_only"]).strip().lower() not in ("no", "false", "0", "")
    if mode == "list":
        works = []
        for line in [l.strip() for l in str(c["seeds"]).splitlines() if l.strip()]:
            doi = re.search(r"10\.\d{4,9}/\S+", line)
            arxiv = re.search(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:)\s*([\w.\-/]+?)(?:v\d+)?(?:\.pdf)?$", line, re.I)
            w = client.get_by_doi(doi.group(0).rstrip(".,")) if doi else None
            if w is None and not arxiv:
                w = client.find_by_title(line)
            if w is None:
                w = Work(None, line, arxiv_id=arxiv.group(1) if arxiv else None,
                         oa_pdf_url=f"https://arxiv.org/pdf/{arxiv.group(1)}" if arxiv else None)
            works.append((w, "your list"))
        return works
    lines = [q.strip() for q in str(c["seeds"] or job["goal"]).splitlines() if q.strip()]
    per_source = max(1, -(-want // len(lines)))
    results, labels = [], []
    for line in lines:
        if mode == "query":
            results.append(client.search(line, per_source, open_access=oa_only))
            labels.append(f'search: "{line[:50]}"')
        else:
            results.append(resolve_references(client, client.wikipedia_references(line), per_source, oa_only))
            labels.append(f"cited by the article \u201c{line[:50]}\u201d")
    works = []
    for rank in range(per_source):                     # round robin, so every query or article contributes
        for found, label in zip(results, labels):
            if rank < len(found):
                works.append((found[rank], label))
    return works


def handle_find(runner, job, task) -> HandlerResult:
    """Build round 0's candidate pool. Nothing is chosen here: the model does that in the screening task."""
    c = cfg(job)
    ws = workspace_of(runner, job)
    if c["seed_mode"] == "folder":                     # local PDFs: nothing to search for or fetch
        folder = c["seeds"] or "papers"
        files = find_sources(ws, folder)
        if not files:
            return HandlerResult(False, f"No papers found in {folder}/", retry_guidance=f"Add papers to {folder}/")
        for f in files[:c["max_papers"]]:
            rel = f.relative_to(ws).as_posix()
            runner.jobs.upsert_paper(job["id"], "local:" + rel, title=f.stem.replace("_", " "), file_path=rel, round=0,
                                     status="queued", provenance={"source": "local file", "pass": 0})
        write_sources_md(runner, job)
        return HandlerResult(True, f"Using {len(files)} paper(s) from {folder}/.")
    require_network(runner, job, task, f"Find candidate papers for: {c['seeds'] or job['goal']}")
    works = search_pool(runner, job, task)
    if not works:
        return HandlerResult(False, "The search found no papers.", retry_guidance="Try a different query.")
    added = 0
    for w, found_by in works:
        added += register_candidates(runner, job, [w], 0, found_by)
    write_sources_md(runner, job)
    return HandlerResult(True, f"Found {added} candidate paper(s) for round 0; the model chooses from them next.")


def _ready(runner, job, task, p: dict, summary: str) -> HandlerResult:
    try:
        n = prepare_parts(runner, job, runner.jobs.get_paper(job["id"], p["key"]))
    except Exception as e:
        runner.jobs.upsert_paper(job["id"], p["key"], status="unavailable")
        return HandlerResult(True, f"{summary} But the text couldn't be extracted ({e}); skipping this paper.")
    limit = cfg(job)["max_parts"]
    if n > limit:
        # The job runs unattended, so length decides itself: reading half a paper and calling it read would be worse
        # than leaving it out and saying so. It stays in sources.md and in the report's gathering record.
        runner.jobs.upsert_paper(job["id"], p["key"], status="skipped",
                                 provenance={**(runner.jobs.get_paper(job["id"], p["key"]).get("provenance") or {}),
                                             "skipped_reason": f"too long: {n} parts (limit {limit})"})
        runner.jobs.journal(job["id"], "acquire", f"Skipped \"{p['title']}\": {n} parts, over the {limit}-part limit "
                                                  f"(about {n * PART_CHARS // 3000} pages).", task["key"])
        return HandlerResult(True, f"{summary} It is {n} parts, over the {limit}-part limit, so it was left unread.")
    warning = (runner.jobs.get_paper(job["id"], p["key"]).get("provenance") or {}).get("extraction_warning")
    return HandlerResult(True, f"{summary} Split into {n} part(s)." + (f" Warning: {warning}" if warning else ""))


def handle_acquire(runner, job, task) -> HandlerResult:
    """Fetch a paper the selection loop already proved reachable, starting from the URL that answered then.

    Nothing here asks the user: a paper that can't be fetched after all is dropped with a record, and the next
    round's screening makes up the numbers.
    """
    import datetime as dt

    ws = workspace_of(runner, job)
    p = runner.jobs.get_paper(job["id"], task["params"]["paper"])
    prov = p.get("provenance") or {}
    if p["file_path"] and (ws / p["file_path"]).is_file():
        runner.jobs.upsert_paper(job["id"], p["key"], status="reading")
        return _ready(runner, job, task, p, f"Using {p['file_path']}.")
    target = pdf_path(p["key"])
    if (ws / target).is_file():
        runner.jobs.upsert_paper(job["id"], p["key"], file_path=target, status="reading",
                                 provenance={**prov, "source": "added by you"})
        return _ready(runner, job, task, p, f"Using {target} (added by you).")
    require_network(runner, job, task, f"Download the open-access copy of \"{p['title']}\"")
    client = scholar(runner)
    verified = [prov["url"]] if prov.get("url") and prov.get("how") == "pdf" else []
    tried = []
    for url in dict.fromkeys(verified + list(client.candidate_pdf_urls(p))):
        tried.append(url)
        try:
            if client.download_pdf(url, ws / target):
                runner.jobs.upsert_paper(job["id"], p["key"], file_path=target, status="reading", provenance={
                    **prov, "source": "open-access download", "url": url, "retrieved": dt.datetime.now().isoformat()})
                return _ready(runner, job, task, p, f"Downloaded the open-access PDF from {url}.")
        except Exception as e:
            runner.jobs.journal(job["id"], "acquire", f"Download failed from {url}: {e}", task["key"])
    full = client.full_text(p)
    if full:
        text, url = full
        rel = text_path(p["key"])
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text(text, encoding="utf-8")
        runner.jobs.upsert_paper(job["id"], p["key"], file_path=rel, status="reading", provenance={
            **prov, "source": "open-access full text (Europe PMC)", "url": url,
            "retrieved": dt.datetime.now().isoformat()})
        return _ready(runner, job, task, p, f"Saved the open-access full text from Europe PMC to {rel}.")
    runner.jobs.upsert_paper(job["id"], p["key"], status="unavailable", provenance={**prov, "unreachable": True})
    runner.jobs.journal(job["id"], "acquire", f"Dropped \"{p['title']}\": no copy could be downloaded "
                                              f"({len(tried)} location(s) tried) although it answered during "
                                              "selection.", task["key"])
    write_sources_md(runner, job)
    return HandlerResult(True, f"No copy could be downloaded for \"{p['title']}\"; dropped it and moved on.")


def reference_keys(p: dict) -> list[tuple[str, dict]]:
    if p["meta_references"]:
        return [(f"oa:{w}", {"openalex_id": w}) for w in p["meta_references"]]
    return [(r["key"], r) for r in p["extracted_references"]]


def handle_next_pass(runner, job, task) -> HandlerResult:
    """Follow the citations: take the reference lists of the papers read so far, drop everything the job has already
    seen, rank what is left by how many of those papers cite it, and put the top works up for the next round's
    screening. Stops when nothing new comes back, or a limit is reached."""
    c = cfg(job)
    ws = workspace_of(runner, job)
    round_ = int(task["params"]["round"])
    papers = runner.jobs.list_papers(job["id"])
    read = [p for p in papers if p["status"] == "read"]
    if not read:
        gathered = sum(1 for p in papers if p["status"] in ("unavailable", "skipped"))
        return HandlerResult(False, "No papers could be read", retry_guidance=(
            f"None of the papers chosen so far could be read ({gathered} unavailable or skipped). The job cannot "
            "write a report without sources: stop it and start again with a folder of PDFs, or a query whose "
            "results are open access."))
    known = {p["key"] for p in papers}
    known_titles = {normalize_title(p["title"]) for p in papers if p["title"]}
    counts: dict[str, int] = {}
    info: dict[str, dict] = {}
    for p in read:
        for key, meta in {k: m for k, m in reference_keys(p)}.items():
            counts[key] = counts.get(key, 0) + 1
            info.setdefault(key, meta)
    for p in papers:                                   # how often the papers read cite papers we have
        if p["key"] in counts:
            runner.jobs.upsert_paper(job["id"], p["key"], cited_by_read=counts[p["key"]])
    novel = sorted(((k, n) for k, n in counts.items() if k not in known
                    and normalize_title(info.get(k, {}).get("title") or "") not in known_titles),
                   key=lambda kv: -kv[1])
    reason = None
    if len(read) >= c["max_papers"]:
        reason = f"reached the {c['max_papers']}-paper limit"
    elif round_ + 1 > c["max_rounds"]:
        reason = f"reached the {c['max_rounds']}-round limit"
    elif not novel:
        reason = f"no works cited by the {len(read)} papers read are new to this job"
    added = 0
    by_id: dict[str, Work] = {}
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:GRAPH_ROWS]
    # Look up more than the pool holds: ties are broken by total citations, which only the lookup knows, so the
    # shortlist can't be cut before that.
    considered = novel[:max(c["candidates_per_round"], 50)]
    lookup = [k[3:] for k, _ in ([] if reason else considered) + top if k.startswith("oa:") and k not in known]
    if lookup or (not reason and any(k.startswith(("doi:", "t:")) for k, _ in considered)):
        require_network(runner, job, task, f"Look up works cited by the papers read, for round {round_ + 1}")
    if lookup:
        try:
            by_id = {f"oa:{w.openalex_id}": w for w in scholar(runner).get_by_ids(list(dict.fromkeys(lookup)))}
        except Exception as e:
            runner.jobs.journal(job["id"], "citations", f"Couldn't look up cited works: {e}", task["key"])
    if not reason:
        # Ties are common (every work cited by both of two papers): prefer works cited more widely overall.
        considered.sort(key=lambda kv: (-kv[1], -((by_id.get(kv[0]) and by_id[kv[0]].cited_by_count) or 0)))
        client = scholar(runner)
        for k, n in considered[:c["candidates_per_round"]]:
            meta = info.get(k, {})
            w = by_id.get(k)
            if w is None and k.startswith("doi:"):
                w = client.get_by_doi(k[4:])
            if w is None and meta.get("title"):
                w = client.find_by_title(meta["title"], meta.get("year"))
            if w is None:
                w = Work(None, meta.get("title") or k, meta.get("year"), doi=meta.get("doi"), arxiv_id=meta.get("arxiv"),
                         oa_pdf_url=f"https://arxiv.org/pdf/{meta['arxiv']}" if meta.get("arxiv") else None)
            added += register_candidates(runner, job, [w], round_ + 1,
                                         f"cited by {n} of the {len(read)} papers read")
        if not added:
            reason = "the works cited by the papers read are all ones this job has already seen"
    titles = {k: {"title": w.title, "year": w.year, "cited_by_count": w.cited_by_count} for k, w in by_id.items()}
    merged = {k: {**info.get(k, {}), **titles.get(k, {})} for k in set(info) | set(titles)}
    _write_graph(ws, counts, merged, runner.jobs.list_papers(job["id"]))
    write_sources_md(runner, job)
    rounds = (job.get("inputs") or {}).get("citation_rounds") or []
    entry = {"round": round_, "read": len(read), "novel": len(novel), "selected": added}
    if reason:
        entry["stop"] = reason
    rounds.append(entry)
    runner.jobs.update_job(job["id"], inputs={**(job.get("inputs") or {}), "citation_rounds": rounds})
    if reason:
        return HandlerResult(True, f"Stopping the literature search: {reason}. See citation_graph.md.")
    return HandlerResult(True, f"Round {round_}: {len(read)} papers read cite {len(novel)} works this job hasn't seen; "
                               f"put the top {added} up for round {round_ + 1}. See citation_graph.md.")


GRAPH_ROWS = 40


def _write_graph(ws: Path, counts, info, papers) -> None:
    by_key = {p["key"]: p for p in papers}
    lines = ["# Citation graph", "", "Works cited by the papers read so far, most cited first (ties ordered by total "
             "citations). The most cited of the ones this job hasn't seen are offered to the model each round.", "",
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
        lead = (authors if authors.endswith(".") else authors + ".") + " " if authors else ""
        lines.append(f"- {lead}{p['title']} ({p['year'] or 'n.d.'}).{ident}{cited}")
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
        key, params = task["key"], (task["params"] or {})
        if task["kind"] == "code" and task["handler"] == "find":
            if c["seed_mode"] == "folder":
                expand_round(runner, job, 0)
            elif not expand_screen(runner, job, 0, key):
                runner.jobs.journal(job["id"], "plan", "No candidate papers to screen.")
        elif params.get("screen"):
            pass_ = int(params["pass"])
            write_sources_md(runner, job)
            if not expand_round(runner, job, pass_):
                # The model kept nothing it could get: let the citation step decide honestly what that means.
                runner.jobs.append_tasks(job["id"], [
                    {"key": f"next_r{pass_}", "title": f"Round {pass_}: follow the citations", "kind": "code",
                     "handler": "next_pass", "params": {"round": pass_}, "instructions": "-",
                     "done_when": "next round's candidates chosen, or the search stopped"}])
        elif task["kind"] == "code" and task["handler"] == "acquire":
            expand_paper(runner, job, task)
        elif params.get("assemble"):
            runner.jobs.upsert_paper(job["id"], params["paper"], status="read")
        elif key.startswith("next_r"):
            rounds = (runner.jobs.get_job(job["id"])["inputs"] or {}).get("citation_rounds") or []
            last = rounds[-1] if rounds else {}
            if last.get("stop"):
                expand_report(runner, job, last["stop"])
            elif not expand_screen(runner, job, int(params["round"]) + 1, key):
                expand_report(runner, job, "no further candidate papers")
        elif key == "layout_gate":
            expand_sections(runner, job)

    def on_keep_sources(self, runner, job, task, keep, note=""):
        return screen_keep(runner, job, task, keep, note)

    def on_gate(self, runner, job, task, answer):
        if is_approval(answer):
            return "approve"
        tasks = {t["key"]: t for t in runner.jobs.list_tasks(job["id"])}
        if task["key"] == "layout_gate":
            runner.jobs.add_guidance(tasks["layout"]["id"], f"The user reviewed your outline and asked for changes: "
                                                            f"\"{answer}\". Revise outline.md accordingly.")
            runner.jobs.update_task(tasks["layout"]["id"], status="pending", attempts=0)
        return "revise"


DEEP_RESEARCH = register(DeepResearch(
    name="deep_research",
    label="Deep research (citation snowballing)",
    description="Searches for papers and has the model choose which to read from numbered lists of titles and "
                "abstracts, fetching each one to confirm it exists and replacing what it can't reach. Reads them "
                "section by section with verified notes, then follows their reference lists into the next round "
                "until nothing new comes back, and writes a report with an abstract, literature review, and "
                "conclusion.",
    inputs_schema={
        "seed_mode": {"enum": ["query", "folder", "list", "wikipedia"], "label": "Start from", "default": "query"},
        "seeds": {"type": "string", "label": "Search queries or Wikipedia articles (one per line), folder, or list "
                           "of titles/DOIs", "default": ""},
        "open_access_only": {"enum": ["yes", "no"], "label": "Search only for papers we can download",
                             "default": "yes"},
        "max_papers": {"type": "string", "label": "Max papers read", "default": "60"},
        "max_rounds": {"type": "string", "label": "Max citation rounds", "default": "4"},
        "seed_count": {"type": "string", "label": "Sources to gather in the first round", "default": "10"},
        "per_round": {"type": "string", "label": "Sources to gather in each later round", "default": "8"},
        "candidates_per_round": {"type": "string", "label": "Candidates to offer the model per round",
                                 "default": "60"},
        "screen_batch": {"type": "string", "label": "Candidates per numbered list", "default": "15"},
        "max_parts": {"type": "string", "label": "Ask before reading papers longer than (parts of ~4 pages)",
                      "default": "12"},
        "format": {"enum": ["md", "docx"], "label": "Report format", "default": "md"},
    },
    handlers={"find": handle_find, "acquire": handle_acquire, "next_pass": handle_next_pass, "digest": handle_digest,
              "section_digest": handle_section_digest, "compile": handle_compile},
))
