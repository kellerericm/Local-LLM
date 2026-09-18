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
            "screen_batch": 15, "open_access_only": "yes", "format": "md"}
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


UNLIMITED = ("all", "none", "no limit", "unlimited", "exhaustive", "0", "-1")


def limit(value) -> int | None:
    """A limit the user set, or None for 'keep going until there is nothing left'. Written as a number, or as
    'all' / 'none' / 'exhaustive'."""
    if value is None or str(value).strip().lower() in UNLIMITED:
        return None
    return max(1, int(value))


def cfg(job: dict) -> dict:
    c = {**DEFAULTS, **{k: v for k, v in (job.get("inputs") or {}).items() if v not in (None, "")}}
    for k in ("max_papers", "max_rounds", "per_round", "seed_count"):
        c[k] = limit(c[k])
    c["screen_batch"] = max(1, int(c["screen_batch"]))            # how many fit in one list, not a limit on work
    return c


def quota_for(c: dict, pass_: int) -> int | None:
    """How many sources a round is after: the seed count for round 0, then the per-round count. None means every
    relevant paper the searches can turn up, and the total paper limit (if the user set one) still applies."""
    want = c["seed_count"] if pass_ == 0 else c["per_round"]
    if want is None or c["max_papers"] is None:
        return want if c["max_papers"] is None else min(want, c["max_papers"])
    return min(want, c["max_papers"])


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
anything off-topic, duplicated, or too general to help.

{target} Each call returns a fresh numbered list, searched out for you, so there is no reason to keep a paper you
doubt: another list is always available. Numbers never change, so you can still keep one from an earlier list. Don't think about links, files, or whether a paper can be downloaded:
that is handled for you, and a paper that turns out to be unavailable is replaced automatically. If nothing in a list
is relevant, call keep_sources with an empty list to see the next one. The newest list is always in candidates.md
if you need to look at it again. When the tool says the round is finished, call complete_task with a sentence on what
you kept and why."""

# A round ends when it has the sources it asked for, or when every search it has is exhausted. Nothing else caps
# it: the job's own budget (hours and steps) is what bounds a run, and max_papers and max_rounds are the user's.
LOOKAHEAD = 2            # extra candidates fetched per slot, to rank a tranche before offering it


def pass_of(p: dict) -> int | None:
    return (p.get("provenance") or {}).get("pass")


def number_of(p: dict) -> int:
    return (p.get("provenance") or {}).get("number") or 0


def round_papers(runner, job, pass_: int, status: str | None = None) -> list[dict]:
    """Every candidate this round has shown the model, in list order."""
    out = [p for p in runner.jobs.list_papers(job["id"]) if pass_of(p) == pass_
           and (status is None or p["status"] == status)]
    return sorted(out, key=number_of)


def selected_in(runner, job, pass_: int) -> list[dict]:
    """Sources this round still has: kept, being read, or read. Papers lost on the way out are not counted, which is
    what makes a round go back for replacements."""
    return [p for p in round_papers(runner, job, pass_) if p["status"] in ("queued", "reading", "read")]


def cursor(job: dict, key: str, default=0):
    return ((job.get("inputs") or {}).get("cursors") or {}).get(key, default)


def set_cursor(runner, job: dict, key: str, value) -> dict:
    inputs = {**(job.get("inputs") or {})}
    inputs["cursors"] = {**(inputs.get("cursors") or {}), key: value}
    runner.jobs.update_job(job["id"], inputs=inputs)
    job["inputs"] = inputs
    return job


def seen_in_job(runner, job) -> tuple[set, set]:
    papers = runner.jobs.list_papers(job["id"])
    return ({p["key"] for p in papers}, {normalize_title(p["title"]) for p in papers if p["title"]})


def seed_lines(c: dict, job: dict) -> list[str]:
    return [l.strip() for l in str(c["seeds"] or job["goal"]).splitlines() if l.strip()]


def search_more(runner, job, c: dict, need: int, seen: tuple[set, set]) -> list[tuple]:
    """Next unseen search results, one page per query line per call, round robin so every query keeps contributing.
    A query whose page comes back empty is marked finished; when all are finished the search is exhausted."""
    client = scholar(runner)
    oa_only = str(c["open_access_only"]).strip().lower() not in ("no", "false", "0", "")
    out: list[tuple] = []
    lines = seed_lines(c, job)
    while len(out) < need:
        alive = [l for l in lines if cursor(job, f"q:{l}", 1) > 0]
        if not alive:
            return out
        for line in alive:
            page = cursor(job, f"q:{line}", 1)
            try:
                works = client.search(line, 50, open_access=oa_only, start_page=page, max_pages=1)
            except Exception as e:
                runner.jobs.journal(job["id"], "sources", f"Search failed for \"{line[:50]}\" page {page}: {e}")
                works = []
            set_cursor(runner, job, f"q:{line}", page + 1 if works else -1)
            fresh = [(w, f'search: "{line[:50]}"') for w in works
                     if w.key() not in seen[0] and normalize_title(w.title or "") not in seen[1]]
            for w, label in fresh:
                seen[0].add(w.key())
                seen[1].add(normalize_title(w.title or ""))
            out += fresh
            if len(out) >= need:
                break
    return out


def wikipedia_more(runner, job, c: dict, need: int, seen: tuple[set, set]) -> list[tuple]:
    """Walk the works each article cites, in article order, resolving them against the index as they are needed."""
    client = scholar(runner)
    oa_only = str(c["open_access_only"]).strip().lower() not in ("no", "false", "0", "")
    out: list[tuple] = []
    for article in seed_lines(c, job):
        refs = cursor(job, f"wiki-refs:{article}", None)
        if refs is None:
            refs = client.wikipedia_references(article, limit=200)
            set_cursor(runner, job, f"wiki-refs:{article}", refs)
        at = cursor(job, f"wiki:{article}", 0)
        while at < len(refs) and len(out) < need:
            chunk = refs[at:at + max(need, 5)]
            at += len(chunk)
            for w in resolve_references(client, chunk, len(chunk), oa_only):
                if w.key() in seen[0] or normalize_title(w.title or "") in seen[1]:
                    continue
                seen[0].add(w.key())
                seen[1].add(normalize_title(w.title or ""))
                out.append((w, f"cited by the article “{article[:50]}”"))
        set_cursor(runner, job, f"wiki:{article}", at)
        if len(out) >= need:
            break
    return out


def list_more(runner, job, c: dict, need: int, seen: tuple[set, set]) -> list[tuple]:
    """Walk the titles, DOIs and arXiv links the user pasted."""
    client = scholar(runner)
    lines = [l.strip() for l in str(c["seeds"]).splitlines() if l.strip()]
    at = cursor(job, "list", 0)
    out: list[tuple] = []
    while at < len(lines) and len(out) < need:
        line = lines[at]
        at += 1
        doi = re.search(r"10\.\d{4,9}/\S+", line)
        arxiv = re.search(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:)\s*([\w.\-/]+?)(?:v\d+)?(?:\.pdf)?$", line, re.I)
        w = client.get_by_doi(doi.group(0).rstrip(".,")) if doi else None
        if w is None and not arxiv:
            w = client.find_by_title(line)
        if w is None:
            w = Work(None, line, arxiv_id=arxiv.group(1) if arxiv else None,
                     oa_pdf_url=f"https://arxiv.org/pdf/{arxiv.group(1)}" if arxiv else None)
        if w.key() in seen[0] or normalize_title(w.title or "") in seen[1]:
            continue
        seen[0].add(w.key())
        seen[1].add(normalize_title(w.title or ""))
        out.append((w, "your list"))
    set_cursor(runner, job, "list", at)
    return out


def ranked_references(runner, job) -> list[tuple[str, int, dict]]:
    """Works cited by the papers read so far and not yet seen by this job, most cited by those papers first."""
    papers = runner.jobs.list_papers(job["id"])
    read = [p for p in papers if p["status"] == "read"]
    seen_keys, seen_titles = seen_in_job(runner, job)
    counts: dict[str, int] = {}
    info: dict[str, dict] = {}
    for p in read:
        for key, meta in {k: m for k, m in reference_keys(p)}.items():
            counts[key] = counts.get(key, 0) + 1
            info.setdefault(key, meta)
    novel = [(k, n, info.get(k, {})) for k, n in counts.items()
             if k not in seen_keys and normalize_title((info.get(k) or {}).get("title") or "") not in seen_titles]
    return sorted(novel, key=lambda t: -t[1])


def references_more(runner, job, c: dict, need: int, seen: tuple[set, set]) -> list[tuple]:
    """Next tranche of cited works for a depth round, resolved against the index. Ties on 'how many papers I read
    cite this' are broken by how widely the work is cited overall, which only the lookup knows, so a tranche is
    resolved and ranked before it is offered."""
    client = scholar(runner)
    # No cursor here: `ranked` already excludes everything the job has seen, and offering a candidate marks it seen,
    # so the top of this list is always the next thing to offer, even as reading adds new reference lists.
    ranked = ranked_references(runner, job)
    at = 0
    read_n = sum(1 for p in runner.jobs.list_papers(job["id"]) if p["status"] == "read")
    out: list[tuple] = []
    while at < len(ranked) and len(out) < need:
        tranche = ranked[at:at + need * LOOKAHEAD]
        at += len(tranche)
        resolved = []
        ids = [k[3:] for k, _, _ in tranche if k.startswith("oa:")]
        by_id = {}
        if ids:
            try:
                by_id = {f"oa:{w.openalex_id}": w for w in client.get_by_ids(list(dict.fromkeys(ids)))}
            except Exception as e:
                runner.jobs.journal(job["id"], "sources", f"Couldn't look up cited works: {e}")
        for k, n, meta in tranche:
            w = by_id.get(k)
            if w is None and k.startswith("doi:"):
                w = client.get_by_doi(k[4:])
            if w is None and meta.get("title"):
                w = client.find_by_title(meta["title"], meta.get("year"))
            if w is None:
                w = Work(None, meta.get("title") or k, meta.get("year"), doi=meta.get("doi"),
                         arxiv_id=meta.get("arxiv"),
                         oa_pdf_url=f"https://arxiv.org/pdf/{meta['arxiv']}" if meta.get("arxiv") else None)
            resolved.append((n, w))
        resolved.sort(key=lambda t: (-t[0], -(t[1].cited_by_count or 0)))
        for n, w in resolved:
            if w.key() in seen[0] or normalize_title(w.title or "") in seen[1]:
                continue
            seen[0].add(w.key())
            seen[1].add(normalize_title(w.title or ""))
            out.append((w, f"cited by {n} of the {read_n} papers read"))
    return out


def fetch_more(runner, job, pass_: int, need: int) -> list[tuple]:
    """Ask this round's source for the next `need` works the job has never seen. Empty means the source is spent:
    every query paged out, every reference followed. There is no candidate pool — lists are searched out as the
    model asks for them, so a round can always go back for more."""
    c = cfg(job)
    seen = seen_in_job(runner, job)
    job = runner.jobs.get_job(job["id"])
    if pass_ > 0:
        return references_more(runner, job, c, need, seen)
    if c["seed_mode"] == "folder":                      # a fixed set of local files: nothing to search for
        return []
    if c["seed_mode"] == "query":
        return search_more(runner, job, c, need, seen)
    if c["seed_mode"] == "wikipedia":
        return wikipedia_more(runner, job, c, need, seen)
    return list_more(runner, job, c, need, seen)


def register_candidates(runner, job, works: list[tuple], pass_: int) -> list[dict]:
    """Record works as this round's next numbered entries. Numbers are unique for the whole job and never reused, so
    the model can keep one it passed over earlier."""
    papers = runner.jobs.list_papers(job["id"])
    number = max([number_of(p) for p in papers] or [0])
    numbered_already = {p["key"] for p in papers if number_of(p)}
    added = []
    for w, found_by in works:
        if w.key() in numbered_already:                 # already offered once; a number is never reused
            continue
        numbered_already.add(w.key())
        number += 1
        register_work(runner, job, w, pass_, status="candidate")
        added.append(runner.jobs.upsert_paper(job["id"], w.key(), provenance={
            "pass": pass_, "number": number, "found_by": found_by, "abstract": (w.abstract or "")[:1200],
            "venue": w.venue, "cited_by_count": w.cited_by_count, "shown": True}))
    return added


def candidate_list(batch: list[dict], pass_: int, have: int, quota: int | None) -> str:
    counted = f"{have} of {quota} sources gathered so far" if quota is not None else f"{have} sources gathered so far"
    lines = [f"## Candidate papers {number_of(batch[0])}-{number_of(batch[-1])} (round {pass_}; {counted})", ""]
    for p in batch:
        prov = p.get("provenance") or {}
        authors = ", ".join(p["authors"][:3]) + (" et al." if len(p["authors"]) > 3 else "")
        bits = [b for b in (authors, str(p["year"] or ""), prov.get("venue") or "") if b]
        if prov.get("cited_by_count"):
            bits.append(f"cited {prov['cited_by_count']:,} times")
        found_by = f" [{prov['found_by']}]" if prov.get("found_by") else ""
        lines.append(f"{number_of(p)}. **{p['title']}** — {'; '.join(bits)}{found_by}")
        abstract = re.sub(r"\s+", " ", prov.get("abstract") or "").strip()
        lines.append("   " + (abstract[:600] + ("…" if len(abstract) > 600 else "") if abstract
                              else "(no abstract available)"))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def next_list(runner, job, pass_: int, size: int, quota: int | None) -> str | None:
    """Search out the next numbered list and write it to candidates.md. None only when the source is spent."""
    works: list[tuple] = []
    keys: set[str] = set()
    while len(works) < size:
        # Nothing is registered until the list is complete, so this loop has to remember what it already holds:
        # otherwise a source that can't fill a whole list keeps handing back the same works.
        fresh = [(w, label) for w, label in fetch_more(runner, job, pass_, size - len(works)) if w.key() not in keys]
        if not fresh:
            break
        keys.update(w.key() for w, _ in fresh)
        works += fresh
    if not works:
        return None
    batch = register_candidates(runner, job, works[:size], pass_)
    text = candidate_list(batch, pass_, len(selected_in(runner, job, pass_)), quota)
    (workspace_of(runner, job) / "candidates.md").write_text(text, encoding="utf-8")
    runner.jobs.journal(job["id"], "sources", f"Round {pass_}: offered {len(batch)} candidate(s) "
                                              f"(#{number_of(batch[0])}-#{number_of(batch[-1])}).")
    return text


def write_sources_md(runner, job) -> None:
    """The coordinator's record of every source: where it came from, what was decided, and the URL it was fetched
    from. The model never emits or reads URLs; this file is for the user and for later runs."""
    papers = sorted(runner.jobs.list_papers(job["id"]), key=lambda p: (pass_of(p) or 0, number_of(p)))
    lines = ["# Sources", "",
             "Every paper the model was shown and what happened when the coordinator went to fetch the ones it kept. "
             "Papers with no reachable copy were dropped and replaced automatically.", "",
             "| # | Round | Title | Year | Decision | Where it came from |", "|---|---|---|---|---|---|"]
    verdict = {"queued": "kept, waiting to be read", "reading": "kept, being read", "read": "read",
               "rejected": "not chosen by the model", "unavailable": "dropped: no reachable copy",
               "skipped": "dropped: skipped", "candidate": "not shown"}
    for p in papers:
        prov = p.get("provenance") or {}
        where = prov.get("url") or prov.get("source") or prov.get("found_by") or ""
        title = str(p["title"]).replace("|", "\\|")[:110]
        lines.append(f"| {number_of(p) or ''} | {prov.get('pass', '')} | {title} | {p['year'] or ''} | "
                     f"{verdict.get(p['status'], p['status'])} | {where} |")
    kept = sum(1 for p in papers if p["status"] in ("queued", "reading", "read"))
    dropped = sum(1 for p in papers if p["status"] in ("unavailable", "skipped"))
    shown = sum(1 for p in papers if (p.get("provenance") or {}).get("shown"))
    lines += ["", f"{shown} candidates shown to the model; {kept} kept and reachable; {dropped} dropped.", ""]
    c = cfg(job)
    lines += ["## What each round was after", "",
              f"- Round 0: {c['seed_count'] if c['seed_count'] is not None else 'every relevant paper found'}",
              f"- Later rounds: {c['per_round'] if c['per_round'] is not None else 'every relevant paper found'}",
              f"- Papers in total: {c['max_papers'] if c['max_papers'] is not None else 'no limit'}",
              f"- Citation rounds: {c['max_rounds'] if c['max_rounds'] is not None else 'no limit'}", ""]
    for r in (job.get("inputs") or {}).get("citation_rounds") or []:
        wanted = r["quota"] if r.get("quota") is not None else "every relevant paper"
        lines.append(f"- Round {r['round']} read {r.get('read_this_round', '?')} of {wanted}"
                     + (f"; search stopped — {r['stop']}" if r.get("stop") else ""))
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


def screen_unfinished(runner, job, task) -> str | None:
    """Called when the model tries to finish a screening round. None lets it finish: the round has its sources, or
    there is nothing left to look at. Otherwise the next list comes back and the round goes on."""
    c = cfg(job)
    pass_ = int(task["params"]["pass"])
    quota = task["params"]["quota"]
    quota = int(quota) if quota is not None else None
    have = len(selected_in(runner, job, pass_))
    if quota is not None and have >= quota:
        return None
    undecided = [p for p in round_papers(runner, job, pass_, "candidate")]
    text = (candidate_list(undecided, pass_, have, quota) if undecided else
            next_list(runner, job, pass_, int(task["params"].get("batch_size") or c["screen_batch"]), quota))
    if text is None:
        return None                    # every search for this round is exhausted; finishing short is honest
    wanted = f"the {quota} papers it needs" if quota is not None else "every relevant paper it can find"
    return (f"Not finished yet: this round has {have} and is after {wanted}. Keep more from this list with "
            f"keep_sources, or send an empty list to see the next one.\n\n{text}")


def screen_keep(runner, job, task, keep: list[int], note: str = "") -> str:
    """One turn of the selection loop, run by the keep_sources tool.

    The model sends list numbers. The coordinator resolves them to papers, fetches each one to prove it exists, drops
    what it can't reach, and searches out a fresh list. It keeps going until the round has as many real sources as it
    asked for, or until the source of candidates is genuinely spent.
    """
    c = cfg(job)
    pass_ = int(task["params"]["pass"])
    quota = task["params"]["quota"]
    quota = int(quota) if quota is not None else None
    by_number = {number_of(p): p for p in round_papers(runner, job, pass_)}
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
    for p in round_papers(runner, job, pass_, "candidate"):        # everything shown and not kept is decided
        runner.jobs.upsert_paper(job["id"], p["key"], status="rejected")
    write_sources_md(runner, job)
    runner._changed(job["id"])

    have = len(selected_in(runner, job, pass_))
    lines = []
    if taken:
        lines.append(f"Kept {len(taken)}: " + "; ".join(f"#{number_of(p)} {p['title'][:60]}" for p in taken))
    if unreachable:
        lines.append(f"Dropped {len(unreachable)} with no reachable copy (being replaced): "
                     + "; ".join(f"#{number_of(p)}" for p in unreachable))
    if ignored:
        lines.append(f"Not in the current list, ignored: {', '.join('#' + str(n) for n in ignored)}.")
    lines.append(f"{have} of {quota} sources gathered." if quota is not None
                 else f"{have} sources gathered so far; this round takes every relevant paper it can find.")

    if quota is not None and have >= quota:
        return "\n".join(lines) + "\nThis round is finished. Call complete_task now."
    text = next_list(runner, job, pass_, int(task["params"].get("batch_size") or c["screen_batch"]), quota)
    if text is None:
        return "\n".join(lines) + ("\nEvery search for this round is now exhausted, so no more candidates exist. "
                                   "Call complete_task now; the job will go on with what it has.")
    return "\n".join(lines) + "\n\n" + text


# ---------------------------------------------------------------- plan
def _initial_plan(runner, job):
    c = cfg(job)
    if c["seed_mode"] not in ("folder", "list", "query", "wikipedia"):
        raise ValueError("seed_mode must be folder, list, query, or wikipedia")
    return [{"key": "find0", "title": "Find candidate papers", "kind": "code", "handler": "find",
             "params": {"pass": 0}, "instructions": f"Seed mode: {c['seed_mode']}",
             "done_when": "candidate papers found and ranked"}]


def expand_screen(runner, job, pass_: int, after: str, topup: int = 0) -> bool:
    """Add a screening task for a round, with the first numbered list in its instructions. `topup` marks a second
    visit to the same round, to replace papers lost after they were chosen."""
    c = cfg(job)
    quota = quota_for(c, pass_)
    text = next_list(runner, job, pass_, c["screen_batch"], quota)
    if text is None:
        return False
    key = f"screen{pass_}_t{topup}" if topup else f"screen{pass_}"
    have = len(selected_in(runner, job, pass_))
    target = (f"This round needs {quota} papers, and has {have} so far." if quota is not None else
              "This round takes every paper relevant to the question that can be found, so keep looking until the "
              "lists run out.")
    runner.jobs.append_tasks(job["id"], [{
        "key": key, "depends_on": [after],
        "title": (f"Round {pass_}: choose replacement paper(s)" if topup
                  else f"Round {pass_}: choose which papers to read"),
        "params": {"screen": True, "pass": pass_, "quota": quota, "batch_size": c["screen_batch"], "lists": 1},
        "instructions": SCREEN.format(question=job["goal"], list=text, target=target),
        "done_when": (f"{quota} papers kept for this round, each with a copy the coordinator could fetch, or the "
                      "searches for it exhausted" if quota is not None else
                      "every relevant paper the searches can find is kept, each with a copy the coordinator "
                      "could fetch")}])
    return True


def expand_round(runner, job, round_: int) -> int:
    """Add reading tasks for the papers a round has just chosen. A round can do this more than once, when it went
    back for replacements, so the keys carry a suffix and the paper numbering continues."""
    papers = [p for p in runner.jobs.list_papers(job["id"], "queued") if p["round"] == round_]
    if not papers:
        return 0
    existing = {t["key"] for t in runner.jobs.list_tasks(job["id"])}
    seq = 0
    while (f"round{round_}" if not seq else f"round{round_}_t{seq}") in existing:
        seq += 1
    suffix = f"_t{seq}" if seq else ""
    group = f"round{round_}{suffix}"
    first = sum(1 for k in existing if k.startswith(f"a{round_}_")) + 1
    tasks = [{"key": group, "title": f"Round {round_}: read {len(papers)} paper(s)", "instructions": "-",
              "done_when": "-"}]
    for i, p in enumerate(papers, first):
        # Reading tasks are added once the text is available and split into parts (see expand_paper).
        tasks.append({"key": f"a{round_}_{i}", "parent_key": group, "title": f"Get: {p['title'][:70]}", "kind": "code",
                      "handler": "acquire", "params": {"paper": p["key"], "round": round_, "index": i},
                      "instructions": "-", "done_when": "paper text available, split into parts, or skipped"})
        runner.jobs.upsert_paper(job["id"], p["key"], status="reading")
    tasks.append({"key": f"next_r{round_}{suffix}", "title": f"Round {round_}: follow the citations", "kind": "code",
                  "handler": "next_pass", "depends_on": [group], "params": {"round": round_}, "instructions": "-",
                  "done_when": "replacements chosen, the next round started, or the search stopped"})
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
    facts = (f"{len(read)} papers read over {len(rounds)} round(s), starting from "
             f"{sum(1 for p in papers if (p.get('provenance') or {}).get('pass') == 0 and p['status'] == 'read')} "
             f"paper(s) found by search ({cfg(job)['seed_mode']} seeds) and the rest followed from their reference "
             f"lists; {len(runner.jobs.list_notes(job['id']))} verified notes; "
             f"{sum(1 for p in papers if p['status'] in ('unavailable', 'skipped'))} chosen papers couldn't be "
             f"obtained. Search stop reason: {(rounds[-1].get('stop') if rounds else None) or 'not recorded'}. "
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
def handle_find(runner, job, task) -> HandlerResult:
    """Start round 0. A folder of local papers goes straight to reading; otherwise there is nothing to do here but
    check the network approval, because candidate lists are searched out one at a time as the model asks for them."""
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
    lines = seed_lines(c, job) if c["seed_mode"] != "list" else [l for l in str(c["seeds"]).splitlines() if l.strip()]
    if not lines:
        return HandlerResult(False, "No search queries, articles or titles were given.",
                             retry_guidance="Set the job's seeds input.")
    return HandlerResult(True, f"Ready to search {len(lines)} source(s) for round 0; "
                               f"the model will choose from numbered lists as they are searched out.")


def _ready(runner, job, task, p: dict, summary: str) -> HandlerResult:
    try:
        n = prepare_parts(runner, job, runner.jobs.get_paper(job["id"], p["key"]))
    except Exception as e:
        runner.jobs.upsert_paper(job["id"], p["key"], status="unavailable")
        return HandlerResult(True, f"{summary} But the text couldn't be extracted ({e}); skipping this paper.")
    # However long the paper is, it gets read: a part is one short task, so length costs time, not correctness.
    if n > 20:
        runner.jobs.journal(job["id"], "acquire", f"\"{p['title']}\" is long: {n} parts, about "
                                                  f"{n * PART_CHARS // 3000} pages.", task["key"])
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
    """After a round's reading: replace any papers the round lost, then follow the citations into the next round.

    A round is only done when it has read as many papers as it asked for, so a paper that couldn't be downloaded
    after all, or was too long to read, sends the round back to choose a replacement. The next round's candidates are
    the works cited by everything read so far, minus everything this job has already seen.
    """
    c = cfg(job)
    ws = workspace_of(runner, job)
    round_ = int(task["params"]["round"])
    papers = runner.jobs.list_papers(job["id"])
    read = [p for p in papers if p["status"] == "read"]

    # 1. Did this round keep what it set out to? Papers lost on the way out are replaced before anything else, even
    # when that leaves nothing read yet: losing a paper must cost the round a replacement, not a source.
    quota = quota_for(c, round_)
    have = len([p for p in round_papers(runner, job, round_) if p["status"] == "read"])
    topups = sum(1 for t in runner.jobs.list_tasks(job["id"]) if t["key"].startswith(f"screen{round_}_t"))
    room = c["max_papers"] is None or len(read) < c["max_papers"]
    # With no target, the round already took everything the searches had, so there is nothing left to replace with.
    if quota is not None and have < quota and room:
        if expand_screen(runner, job, round_, task["key"], topup=topups + 1):
            runner.jobs.journal(job["id"], "sources", f"Round {round_} read {have} of {quota} papers; looking for "
                                                      f"{quota - have} replacement(s).", task["key"])
            write_sources_md(runner, job)
            return HandlerResult(True, f"Round {round_} read {have} of the {quota} papers it wanted; choosing "
                                       f"{quota - have} replacement(s) before following the citations.")
        runner.jobs.journal(job["id"], "sources", f"Round {round_} read {have} of {quota} papers, and its searches "
                                                  "are exhausted, so no replacement exists.", task["key"])

    if not read:
        lost = sum(1 for p in papers if p["status"] in ("unavailable", "skipped"))
        return HandlerResult(False, "No papers could be read", retry_guidance=(
            f"None of the papers chosen so far could be read ({lost} unavailable or skipped), and no further "
            "candidates could be found. The job cannot write a report without sources: stop it and start again with "
            "a folder of PDFs, or queries whose results are open access."))

    # Citation counts over everything read: they order the next round's candidates and fill the graph.
    counts: dict[str, int] = {}
    info: dict[str, dict] = {}
    for p in read:
        for key, meta in {k: m for k, m in reference_keys(p)}.items():
            counts[key] = counts.get(key, 0) + 1
            info.setdefault(key, meta)
    for p in papers:
        if p["key"] in counts:
            runner.jobs.upsert_paper(job["id"], p["key"], cited_by_read=counts[p["key"]])
    known = {p["key"] for p in papers}
    graph_ids = [k[3:] for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:GRAPH_ROWS]
                 if k.startswith("oa:") and k not in known]
    by_id: dict[str, Work] = {}
    if graph_ids:
        require_network(runner, job, task, f"Look up works cited by the papers read, after round {round_}")
        try:
            by_id = {f"oa:{w.openalex_id}": w for w in scholar(runner).get_by_ids(list(dict.fromkeys(graph_ids)))}
        except Exception as e:
            runner.jobs.journal(job["id"], "citations", f"Couldn't look up cited works: {e}", task["key"])
    titles = {k: {"title": w.title, "year": w.year, "cited_by_count": w.cited_by_count} for k, w in by_id.items()}
    _write_graph(ws, counts, {k: {**info.get(k, {}), **titles.get(k, {})} for k in set(info) | set(titles)},
                 runner.jobs.list_papers(job["id"]))
    write_sources_md(runner, job)

    # 2. Follow the citations into the next round.
    novel = ranked_references(runner, job)
    reason = None
    if c["max_papers"] is not None and len(read) >= c["max_papers"]:
        reason = f"your limit: {c['max_papers']} papers"
    elif c["max_rounds"] is not None and round_ + 1 > c["max_rounds"]:
        reason = f"your limit: {c['max_rounds']} citation rounds"
    elif not novel:
        reason = f"the literature converged: no work cited by the {len(read)} papers read is new to this job"
    fresh = runner.jobs.get_job(job["id"]) or job
    rounds = (fresh.get("inputs") or {}).get("citation_rounds") or []
    entry = {"round": round_, "read": len(read), "read_this_round": have, "quota": quota, "novel": len(novel)}
    if reason:
        entry["stop"] = reason
    rounds.append(entry)
    runner.jobs.update_job(job["id"], inputs={**(runner.jobs.get_job(job["id"]).get("inputs") or {}),
                                              "citation_rounds": rounds})
    if reason:
        return HandlerResult(True, f"Stopping the literature search: {reason}. See citation_graph.md.")
    return HandlerResult(True, f"{len(read)} papers read so far; they cite {len(novel)} works this job hasn't seen. "
                               f"Round {round_ + 1} will choose from them. See citation_graph.md.")


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
              f"{len(read)} papers were read over {len(rounds)} round(s). Each round searched for candidates, had "
              "them judged for relevance, and fetched the chosen ones to confirm they could be read."]
    for r in rounds:
        wanted = f"after {r['quota']}" if r.get("quota") is not None else "after every relevant paper"
        lines.append(f"- Round {r['round']}: {wanted}, read {r.get('read_this_round', '?')}; "
                     f"{r.get('novel', 0)} cited works were new to the job"
                     + (f". Search stopped — {r['stop']}." if r.get("stop") else "."))
    lines += ["", "Where a round read fewer papers than it was after, the searches for it were exhausted; where the "
              "search stopped at a limit, the limit is named above."]
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
            if not rounds or rounds[-1].get("round") != int(params["round"]):
                return                      # this round went back for replacements; it decides again afterwards
            if last.get("stop"):
                expand_report(runner, job, last["stop"])
            elif not expand_screen(runner, job, int(params["round"]) + 1, key):
                expand_report(runner, job, "no further candidate papers")
        elif key == "layout_gate":
            expand_sections(runner, job)

    def on_keep_sources(self, runner, job, task, keep, note=""):
        return screen_keep(runner, job, task, keep, note)

    def on_complete_task(self, runner, job, task):
        return screen_unfinished(runner, job, task) if (task["params"] or {}).get("screen") else None

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
        "max_papers": {"type": "string", "label": "Max papers read ('all' for no limit)", "default": "60"},
        "max_rounds": {"type": "string", "label": "Max citation rounds ('all' for no limit)", "default": "4"},
        "seed_count": {"type": "string", "label": "Sources to gather in the first round ('all' for every relevant "
                                                  "paper the searches can find)", "default": "10"},
        "per_round": {"type": "string", "label": "Sources to gather in each later round ('all' for every relevant "
                                                 "paper found)", "default": "8"},
        "screen_batch": {"type": "string", "label": "Candidates per numbered list", "default": "15"},
        "format": {"enum": ["md", "docx"], "label": "Report format", "default": "md"},
    },
    handlers={"find": handle_find, "acquire": handle_acquire, "next_pass": handle_next_pass, "digest": handle_digest,
              "section_digest": handle_section_digest, "compile": handle_compile},
))
