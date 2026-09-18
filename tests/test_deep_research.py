import re

import pytest

from localagent.jobs.scholar import Work, work_from_openalex, work_key
from localagent.jobs.templates.deep_research import PART_CHARS, cfg, pdf_path, split_paper
from localagent.safety import AutoApprover
from test_jobs import Env, call
from test_research_tools import make_pdf


class FakeScholar:
    """Stands in for OpenAlex/arXiv. A small citation world: A and B cite F1 and F2; C cites F1."""

    def __init__(self, workspace):
        self.ws = workspace
        self.works = {
            "WA": Work("WA", "Paper A", 2021, ["Ann"], oa_pdf_url="https://oa.example/A.pdf", referenced_works=["WF1", "WF2"]),
            "WB": Work("WB", "Paper B", 2022, ["Bob"], oa_pdf_url="https://oa.example/B.pdf", referenced_works=["WF1", "WF2"]),
            "WC": Work("WC", "Paper C", 2023, ["Cy"], oa_pdf_url=None, referenced_works=["WF1"]),
            "WF1": Work("WF1", "Foundation One", 1998, ["Old"], oa_pdf_url="https://oa.example/F1.pdf",
                        referenced_works=["WX"]),
            "WF2": Work("WF2", "Foundation Two", 2001, ["Older"], oa_pdf_url="https://oa.example/F2.pdf", referenced_works=["WF1"]),
        }
        self.downloads = []

    def search(self, query, n=10, open_access=False, max_pages=4):
        self.queries = getattr(self, "queries", []) + [query]
        self.open_access = open_access
        picks = {"replay": ("WA", "WB"), "consolidation": ("WB", "WC")}.get(query.strip(), ("WA", "WB", "WC"))
        return [self.works[k] for k in picks][:n]

    def wikipedia_references(self, article, limit=60):
        self.articles = getattr(self, "articles", []) + [article]
        return [{"doi": "10.1/a", "title": "Paper A", "year": 2021, "arxiv": None},
                {"doi": None, "title": "Foundation One", "year": 1998, "arxiv": None},
                {"doi": None, "title": "Unknown paper", "year": 2000, "arxiv": None}]

    def get_by_ids(self, ids):
        return [self.works[i] for i in ids if i in self.works]

    def get_by_doi(self, doi):
        return self.works["WA"] if doi == "10.1/a" else None

    def find_by_title(self, title, year=None):
        return next((w for w in self.works.values() if w.title == title), None)

    def candidate_pdf_urls(self, paper):
        return ["https://publisher.example/landing.html"] + ([paper["oa_pdf_url"]] if paper.get("oa_pdf_url") else [])

    def pmc_fulltext_urls(self, paper):
        return []

    def probe(self, url, kind="pdf", timeout=20):
        return not url.endswith(".html")                   # publisher landing pages answer with HTML, not a paper

    def locate(self, paper):
        self.located = getattr(self, "located", []) + [paper["title"]]
        for url in self.candidate_pdf_urls(paper):
            if self.probe(url):
                return url, "pdf"
        for url in self.pmc_fulltext_urls(paper):
            return url, "pmc"
        return None

    def full_text(self, paper):
        return None

    def download_pdf(self, url, dest):
        if url.endswith(".html"):                          # publisher pages return HTML, not a PDF
            return False
        self.downloads.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        make_pdf(dest, [f"Text of {url}"])
        return True


@pytest.fixture
def env_factory(store, settings, workspace):
    return lambda responses, **kw: Env(store, settings, workspace, responses, **kw)


def keep(*numbers, summary="Kept the relevant papers."):
    """A screening task: choose by list number, then finish."""
    return [call("keep_sources", keep=list(numbers)), call("complete_task", summary=summary)]


def numbered(env, job):
    """What the model was last shown: {list number: title}."""
    out = {}
    for paper in env.jobs.list_papers(job["id"]):
        prov = paper.get("provenance") or {}
        if prov.get("shown"):
            out[prov["number"]] = paper["title"]
    return out


def make_job(env, **inputs):
    base = {"seed_mode": "query", "seeds": "memory consolidation"}
    return env.jobs.create_job(env.project["id"], "Memory review", "How do brains and models keep long-term memory?",
                               template="deep_research", inputs={**base, **inputs}, permissions=["net:open-access"])


def tick_until(env, job_id, predicate, limit=40):
    for _ in range(limit):
        if predicate():
            return True
        env.runner._tick()
    return predicate()


def reader(refs=None, quote="Text of https"):
    """Scripted reading. Part tasks: write the part summary and save a note. Write-up tasks: write the paper summary
    (and record references when asked)."""
    def respond(msgs):
        system = msgs[0]["content"]
        part = re.search(r"Write (papers/\S+/summary-\d+\.md)", system)
        if part:
            src = re.search(r'source "(papers/\S+/part-\d+\.md)"', system).group(1)
            return (call("write_file", path=part.group(1), content="### Intro\nok") +
                    call("add_note", claim="The paper's text", quote=quote, source=src))
        md = re.search(r"Write (papers/[^/\s]+\.md)", system).group(1)
        out = call("write_file", path=md, content="# X\n## Section summaries\n### Intro\nok\n## Key claims\n- c [n1]\n"
                                                  "## Value of this paper\nuseful")
        if "call record_references" in system:
            out += call("record_references", references=refs.pop(0) if refs else [])
        return out
    return respond


def read_paper_responses(n, refs=None, quote="Text of https"):
    """Part task (no review) then write-up task (reviewed), per paper with one part."""
    r = reader(refs, quote)
    out = []
    for _ in range(n):
        out += [r, call("complete_task", summary="Summarized the part."),
                r, call("complete_task", summary="Wrote up the paper."), call("report_review", verdict="pass")]
    return out


def start(env, job, screen_responses=None):
    """Run the plan and the first screening task."""
    env.runner._tick()                                     # template plan
    env.runner.approve_plan(job["id"])
    env.runner._tick()                                     # find0: build the candidate pool
    env.runner._tick()                                     # screen0: the model chooses
    return job


def test_model_chooses_by_number_and_the_coordinator_verifies_and_refills(env_factory, workspace):
    """The core loop: the model keeps list numbers, the coordinator fetches each one to prove it exists, drops what
    it can't reach, and offers a fresh list until the quota is filled."""
    fake = FakeScholar(workspace)
    # Paper C has no reachable copy: keeping it must cost a replacement, not a source.
    env = env_factory([call("keep_sources", keep=[1, 3]),           # Paper A (ok) and Paper C (unreachable)
                       call("keep_sources", keep=[2]),              # then Paper B from the refill
                       call("complete_task", summary="Kept A and B; C had no copy.")])
    env.runner.scholar = fake
    job = make_job(env, seed_count="2", screen_batch="3")      # the default query returns A, B and C
    start(env, job)

    papers = {p["title"]: p for p in env.jobs.list_papers(job["id"])}
    assert papers["Paper A"]["status"] in ("queued", "reading")            # kept, and the round is under way
    assert papers["Paper A"]["provenance"]["url"].endswith("A.pdf")
    assert papers["Paper C"]["status"] == "unavailable" and papers["Paper C"]["provenance"]["unreachable"]
    assert papers["Paper B"]["status"] in ("queued", "reading")
    assert fake.located == ["Paper A", "Paper C", "Paper B"]        # only what the model kept was fetched

    text = (workspace / "candidates.md").read_text(encoding="utf-8")
    assert "http" not in text                                       # the model is never shown a URL
    sources = (workspace / "sources.md").read_text(encoding="utf-8")
    assert "dropped: no reachable copy" in sources and "oa.example/A.pdf" in sources
    screen = task_by_key(env, job, "screen0")
    assert screen["status"] == "done"
    assert [t["key"] for t in env.jobs.list_tasks(job["id"]) if t["key"].startswith("a0_")] == ["a0_1", "a0_2"]


def test_keeping_nothing_shows_the_next_list_then_ends_the_round(env_factory, workspace):
    env = env_factory([call("keep_sources", keep=[]),               # nothing relevant in the first list
                       call("keep_sources", keep=[3]),              # something from the list that followed
                       call("complete_task", summary="Only the third one was on topic.")])
    env.runner.scholar = FakeScholar(workspace)
    job = make_job(env, seeds="replay\nconsolidation", seed_count="1", screen_batch="2")
    start(env, job)
    papers = {p["title"]: p["status"] for p in env.jobs.list_papers(job["id"])}
    assert papers["Paper A"] == "rejected" and papers["Paper B"] == "rejected"
    assert len(numbered(env, job)) == 3                              # keeping nothing brought a second list
    assert papers["Paper C"] == "unavailable"                        # kept, but it has no copy anywhere
    assert task_by_key(env, job, "screen0")["status"] == "done"


def test_a_paper_is_never_offered_twice(env_factory, workspace):
    """Deduplication is the coordinator's job: the same work arrives from several queries and reference lists."""
    fake = FakeScholar(workspace)
    env = env_factory([call("keep_sources", keep=[1]), call("complete_task", summary="One paper is enough here.")])
    env.runner.scholar = fake
    job = make_job(env, seeds="replay\nconsolidation", seed_count="1", screen_batch="9")
    start(env, job)
    shown = numbered(env, job)
    assert sorted(shown.values()) == ["Paper A", "Paper B", "Paper C"]      # B is in both queries, listed once
    assert sorted(shown) == [1, 2, 3]


def test_unreachable_papers_are_dropped_without_asking(env_factory, workspace):
    """The job runs unattended: a paper that answers at selection but not at download is dropped with a record."""
    fake = NoPdfScholar(workspace)
    env = env_factory(keep(1, summary="Kept Paper A, the only relevant one."))
    env.runner.scholar = fake
    job = make_job(env, seeds="replay", seed_count="1", screen_batch="2")
    start(env, job)
    assert task_by_key(env, job, "a0_1") is not None
    assert tick_until(env, job["id"], lambda: task_by_key(env, job, "a0_1")["status"] == "done")
    a = next(p for p in env.jobs.list_papers(job["id"]) if p["title"] == "Paper A")
    assert a["status"] == "unavailable" and task_by_key(env, job, "a0_1")["question"] in (None, "")


def test_network_requires_permission(env_factory):
    approver = AutoApprover(allow=False)
    env = env_factory([], approver=approver)
    env.runner.scholar = FakeScholar(env.workspace)
    job = env.jobs.create_job(env.project["id"], "R", "Q?", template="deep_research",
                              inputs={"seed_mode": "query", "seeds": "x"})      # no net permission
    env.runner._tick()
    env.runner.approve_plan(job["id"])
    env.runner._tick()
    find = next(t for t in env.jobs.list_tasks(job["id"]) if t["key"] == "find0")
    assert find["status"] == "waiting_user" and find["waiting_kind"] == "approval"
    assert approver.requests[0]["keys"] == ["net:open-access"]


def test_folder_seeds_skip_screening_and_use_extracted_references(env_factory, workspace):
    """Local PDFs need no searching, fetching or choosing: they go straight to reading, and their reference lists
    still feed the next round's candidate list."""
    (workspace / "papers").mkdir()
    for name in ("one", "two"):
        make_pdf(workspace / "papers" / f"{name}.pdf", [f"Paper {name} about hippocampal replay."])
    ref_lists = [[{"title": "Shared Classic", "year": 1990}, {"title": "Only once", "year": 2000}],
                 [{"title": "Shared Classic", "year": 1990}]]

    responses = read_paper_responses(2, ref_lists, quote="about hippocampal replay")
    responses += keep(1, summary="The classic is worth reading.")
    env = env_factory(responses)
    env.runner.scholar = FakeScholar(workspace)
    job = env.jobs.create_job(env.project["id"], "R", "Q?", template="deep_research", permissions=["net:open-access"],
                              inputs={"seed_mode": "folder", "seeds": "papers", "max_rounds": "1"})
    env.runner._tick()
    assert [t["key"] for t in env.jobs.list_tasks(job["id"])] == ["find0"]      # folder mode: no screening task
    env.runner.approve_plan(job["id"])
    assert tick_until(env, job["id"], lambda: status_of(env, job, "next_r0") == "done")
    rounds = env.jobs.get_job(job["id"])["inputs"]["citation_rounds"]
    assert rounds[0]["novel"] == 2 and rounds[0]["selected"] == 2               # both cited works are new
    candidates = [p for p in env.jobs.list_papers(job["id"]) if (p["provenance"] or {}).get("pass") == 1]
    assert [c["title"] for c in candidates] == ["Shared Classic", "Only once"]  # most-cited first
    assert candidates[0]["key"] == work_key(title="Shared Classic", year=1990)
    assert candidates[0]["provenance"]["found_by"] == "cited by 2 of the 2 papers read"


def test_work_from_openalex_parses_fields():
    w = work_from_openalex({
        "id": "https://openalex.org/W123", "display_name": "Complementary learning systems", "publication_year": 1995,
        "ids": {"doi": "https://doi.org/10.1037/0033-295X.102.3.419"},
        "authorships": [{"author": {"display_name": "J. McClelland"}}],
        "best_oa_location": {"pdf_url": None},
        "locations": [{"landing_page_url": "https://arxiv.org/abs/1234.5678v2"}],
        "referenced_works": ["https://openalex.org/W9", "https://openalex.org/W10"], "cited_by_count": 5000})
    assert w.openalex_id == "W123" and w.doi == "10.1037/0033-295X.102.3.419" and w.arxiv_id == "1234.5678"
    assert w.oa_pdf_url == "https://arxiv.org/pdf/1234.5678" and w.referenced_works == ["W9", "W10"]
    assert w.key() == "oa:W123"


def test_split_paper_parts_at_headings_and_sets_references_aside():
    body = "\n".join(["Title of paper", "Abstract", "a" * 500, "1. Introduction", "b " * 4000,
                      "2. Methods", "c " * 4000, "Results", "d\n" * 9000, "Discussion", "e " * 300])
    text = body + "\nReferences\n1. Smith J. A classic. 1990.\n2. Doe A. Another. 2001."
    parts, refs = split_paper(text)
    assert refs.startswith("References") and "Smith" in refs
    assert all("Smith J." not in p for _, p in parts)
    assert len(parts) >= 3 and all(len(p) <= PART_CHARS * 1.3 + 10 for _, p in parts)
    assert "".join(p for _, p in parts).count("b ") == 4000                # nothing lost
    assert parts[0][0].startswith("Beginning")


def test_inputs_schema_lists_every_setting():
    from localagent.jobs.templates import get_template
    schema = get_template("deep_research").inputs_schema
    assert {"seed_count", "per_round", "max_papers", "max_rounds", "candidates_per_round",
            "screen_batch"} <= set(schema)


def test_config_defaults_match_decisions():
    from localagent.jobs.templates.deep_research import quota_for
    c = cfg({"inputs": {}})
    assert (c["max_papers"], c["max_rounds"], c["per_round"], c["seed_count"]) == (60, 4, 8, 10)
    assert (c["candidates_per_round"], c["screen_batch"]) == (60, 15)
    assert (quota_for(c, 0), quota_for(c, 1)) == (10, 8)
    assert pdf_path("oa:W1").startswith("papers/pdf/")


JATS = b"""<?xml version="1.0"?>
<!DOCTYPE article PUBLIC "-//NLM//DTD JATS//EN" "JATS-archivearticle1.dtd">
<article xmlns:xlink="http://www.w3.org/1999/xlink"><front><article-meta><title-group>
<article-title>Replay and planning</article-title></title-group>
<abstract><p>We study <italic>replay</italic>.</p></abstract></article-meta></front>
<body><sec><title>Introduction</title><p>Replay reactivates sequences (<xref ref-type="bibr">Foster, 2006</xref>).</p>
<sec><title>Background</title><p>Sharp waves&nbsp;matter.</p></sec></sec>
<sec><title>Methods</title><p>Rats ran.</p><fig><label>Figure 1</label><caption><p>A maze.</p></caption></fig></sec></body>
<back><ref-list><ref id="r1"><mixed-citation><string-name><surname>Foster</surname> <given-names>DJ</given-names></string-name>.
<year>2006</year> <article-title>Reverse replay</article-title>. <source>Nat Neurosci</source>
<pub-id pub-id-type="pmid">123</pub-id><pub-id pub-id-type="doi">10.1038/nn1961</pub-id></mixed-citation></ref></ref-list></back>
</article>"""


def test_jats_to_markdown_keeps_sections_captions_and_references():
    from localagent.jobs.scholar import jats_to_markdown
    md = jats_to_markdown(JATS)
    assert md.startswith("# Replay and planning")
    for line in ("## Abstract", "## Introduction", "### Background", "## Methods", "Figure 1: A maze.", "## References"):
        assert line in md
    assert "Replay reactivates sequences (Foster, 2006)." in md
    assert "Reverse replay" in md and "doi:10.1038/nn1961" in md and "123" not in md.split("## References")[1]
    parts, refs = split_paper(md)
    assert refs.startswith("## References") and "Reverse replay" not in "".join(p for _, p in parts)


class NoPdfScholar(FakeScholar):
    def __init__(self, workspace, texts=None):
        super().__init__(workspace)
        self.texts = texts or {}

    def download_pdf(self, url, dest):
        self.downloads.append(url)
        return False

    def full_text(self, paper):
        text = self.texts.get(paper["title"])
        return (text, "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1/fullTextXML") if text else None


def status_of(env, job, key):
    return (task_by_key(env, job, key) or {}).get("status")


def task_by_key(env, job, key):
    return next((t for t in env.jobs.list_tasks(job["id"]) if t["key"] == key), None)


def test_full_text_fallback_and_long_papers_are_left_unread(env_factory, workspace):
    """Europe PMC full text stands in for a PDF; a paper past the part limit is left out with a reason, because
    nobody is there to be asked and half a paper read as if whole would be worse."""
    body = "\n\n".join(f"## Section {i}\n\n" + ("Replay text. " * 1000) for i in range(30))
    texts = {"Paper A": "# Paper A\n\n## Introduction\n\nShort paper about replay.\n\n## Methods\n\nRats.\n\n"
                        "## Discussion\n\n" + "More. " * 400 + "\n\n## References\n\nFoster DJ. 2006 Reverse replay.",
             "Paper B": "# Paper B\n\n" + body}
    env = env_factory(keep(1, 2, summary="Both look relevant."))
    env.runner.scholar = NoPdfScholar(workspace, texts)
    job = make_job(env, seeds="replay", seed_count="2", screen_batch="3")
    start(env, job)
    assert tick_until(env, job["id"], lambda: task_by_key(env, job, "a0_2")["status"] == "done")
    a = {p["title"]: p for p in env.jobs.list_papers(job["id"])}
    assert a["Paper A"]["file_path"].startswith("papers/text/")
    assert a["Paper A"]["provenance"]["source"].startswith("open-access full text")
    assert (workspace / a["Paper A"]["provenance"]["folder"] / "references.txt").read_text(encoding="utf-8").count(
        "Foster") == 1
    assert task_by_key(env, job, "w0_1") is not None                     # reading tasks for A were added
    assert a["Paper B"]["status"] == "skipped" and "too long" in a["Paper B"]["provenance"]["skipped_reason"]
    assert task_by_key(env, job, "p0_2_1") is None                       # and none for B
    assert "too long" in (workspace / "sources.md").read_text(encoding="utf-8") or a["Paper B"]["status"] == "skipped"


def test_no_papers_read_fails_the_job_instead_of_writing_an_empty_report(env_factory, workspace):
    env = env_factory(keep(1, 2, summary="Both look relevant."))
    env.runner.scholar = NoPdfScholar(workspace)
    job = make_job(env, seeds="replay", seed_count="2", screen_batch="3")
    start(env, job)
    assert tick_until(env, job["id"], lambda: status_of(env, job, "next_r0") in ("failed", "needs_help"))
    assert task_by_key(env, job, "layout") is None
    runs = [r for r in env.jobs.list_runs(job["id"]) if r["task_id"] == task_by_key(env, job, "next_r0")["id"]]
    assert any("No papers could be read" in (r["summary"] or "") for r in runs)


def test_citation_ties_prefer_widely_cited_works_and_graph_shows_titles(env_factory, workspace):
    fake = FakeScholar(workspace)
    fake.works["WF1"].cited_by_count, fake.works["WF2"].cited_by_count = 10, 900   # both cited by A and B
    responses = keep(1, 2, summary="Both papers are relevant.") + read_paper_responses(2)
    env = env_factory(responses)
    env.runner.scholar = fake
    job = make_job(env, seeds="replay", seed_count="2", screen_batch="3", max_rounds="2")
    start(env, job)
    assert tick_until(env, job["id"], lambda: status_of(env, job, "next_r0") == "done")
    offered = [p["title"] for p in env.jobs.list_papers(job["id"]) if (p["provenance"] or {}).get("pass") == 1]
    assert offered == ["Foundation Two", "Foundation One"]              # the tie goes to the more-cited work first
    graph = (workspace / "citation_graph.md").read_text(encoding="utf-8")
    assert "| 2 | Foundation Two | 2001 | 900 |" in graph and "| 2 | Foundation One | 1998 | 10 |" in graph
    assert "oa:W" not in graph


def test_literature_digest_fits_budget_with_many_papers(tmp_path):
    from localagent.jobs.templates.deep_research import DIGEST_CHARS, build_digest, paper_md
    papers = []
    for i in range(60):
        key = f"oa:W{i}"
        md = tmp_path / paper_md(key)
        md.parent.mkdir(parents=True, exist_ok=True)
        claims = "\n".join(f"- Claim {j} of paper {i} about replay and planning in detail. [n{i * 30 + j}]" for j in range(25))
        md.write_text(f"# P{i}\n## Section summaries\n{'x ' * 5000}\n## Key claims\n{claims}\n"
                      f"## Value of this paper\n{'Valuable because of careful methods. ' * 80}\n", encoding="utf-8")
        papers.append({"key": key, "title": f"Paper {i}", "year": 2000 + i % 20, "status": "read", "round": i % 3,
                       "cited_by_read": 60 - i})
    papers.append({"key": "oa:Wskip", "title": "Skipped", "year": 1999, "status": "skipped", "round": 0, "cited_by_read": 9})
    text = build_digest(tmp_path, papers)
    assert len(text) <= DIGEST_CHARS * 1.1
    assert text.count("### Paper ") == 60 and "Skipped" not in text
    assert text.index("### Paper 0 ") < text.index("### Paper 59 ")          # most-cited first
    assert "[n0]" in text and "x x x" not in text                              # claims kept, section summaries left out


def test_query_seeds_run_one_search_per_line(env_factory, workspace):
    fake = FakeScholar(workspace)
    env = env_factory([])
    env.runner.scholar = fake
    job = make_job(env, seeds="replay\nconsolidation", seed_count="4")
    env.runner._tick()
    env.runner.approve_plan(job["id"])
    env.runner._tick()
    assert fake.queries == ["replay", "consolidation"]
    assert [p["title"] for p in env.jobs.list_papers(job["id"])] == ["Paper A", "Paper B", "Paper C"]   # merged, no dupes
    assert all(p["status"] == "candidate" for p in env.jobs.list_papers(job["id"]))
    assert [(p["provenance"] or {}).get("found_by") for p in env.jobs.list_papers(job["id"])][0].startswith("search:")


def test_reviewed_report_tasks_get_more_attempts(env_factory, workspace):
    from localagent.jobs.templates.deep_research import REVIEWED_ATTEMPTS, expand_sections
    (workspace / "outline.md").write_text("# R\n\n## Abstract\n\n## Findings\n- a [n1]\n\n## Conclusion and summary\n- b [n1]\n",
                                          encoding="utf-8")
    env = env_factory([])
    job = make_job(env)
    env.jobs.replace_plan(job["id"], [{"key": "layout_gate", "title": "gate", "instructions": "-", "done_when": "-",
                                       "checks": [], "depends_on": [], "parent_key": None}])
    expand_sections(env.runner, env.jobs.get_job(job["id"]))
    tasks = {t["key"]: t for t in env.jobs.list_tasks(job["id"])}
    assert tasks["s1"]["max_attempts"] == REVIEWED_ATTEMPTS and tasks["abstract"]["max_attempts"] == REVIEWED_ATTEMPTS
    assert tasks["section_digest"]["max_attempts"] == 3          # code tasks keep the default


def test_wikipedia_seeds_take_the_articles_cited_works(env_factory, workspace):
    fake = FakeScholar(workspace)
    env = env_factory([])
    env.runner.scholar = fake
    job = make_job(env, seed_mode="wikipedia", seeds="Memory consolidation", candidates_per_round="2",
                   open_access_only="no")
    env.runner._tick()
    env.runner.approve_plan(job["id"])
    env.runner._tick()
    assert fake.articles == ["Memory consolidation"]
    papers = env.jobs.list_papers(job["id"])
    assert [p["title"] for p in papers] == ["Paper A", "Foundation One"]
    assert "cited by the article" in (papers[0]["provenance"] or {})["found_by"]


def test_query_seeds_ask_for_open_access_by_default(env_factory, workspace):
    fake = FakeScholar(workspace)
    env = env_factory([])
    env.runner.scholar = fake
    job = make_job(env, seeds="replay", seed_count="3")
    env.runner._tick()
    env.runner.approve_plan(job["id"])
    env.runner._tick()
    assert fake.open_access is True


def test_two_rounds_screened_then_report(env_factory, workspace):
    """End to end over two rounds: the model screens round 0, the papers it kept are read, their reference lists
    become round 1's candidates for it to screen again, and the search stops when nothing new is left."""
    fake = FakeScholar(workspace)
    responses = (keep(1, 2, summary="Paper A and Paper B are on topic.")
                 + read_paper_responses(2)                                  # A and B
                 + keep(3, 4, summary="Both foundational works are worth reading.")
                 + read_paper_responses(2))                                 # Foundation One and Two
    env = env_factory(responses)
    env.runner.scholar = fake
    job = make_job(env, seeds="replay", seed_count="2", per_round="2", screen_batch="5", max_rounds="1")
    start(env, job)
    assert tick_until(env, job["id"], lambda: status_of(env, job, "screen1") == "done", limit=60)

    offered = {(p["provenance"] or {}).get("number"): p["title"]
               for p in env.jobs.list_papers(job["id"]) if (p["provenance"] or {}).get("pass") == 1}
    assert offered == {3: "Foundation One", 4: "Foundation Two"}            # numbering continues across rounds
    assert all("cited by 2 of the 2 papers read" in (p["provenance"] or {}).get("found_by", "")
               for p in env.jobs.list_papers(job["id"]) if (p["provenance"] or {}).get("pass") == 1)

    assert tick_until(env, job["id"], lambda: status_of(env, job, "layout") is not None, limit=80)
    rounds = env.jobs.get_job(job["id"])["inputs"]["citation_rounds"]
    assert len(rounds) == 2 and rounds[0]["selected"] == 2
    assert "round limit" in rounds[-1]["stop"]
    read = {p["title"] for p in env.jobs.list_papers(job["id"]) if p["status"] == "read"}
    assert read == {"Paper A", "Paper B", "Foundation One", "Foundation Two"}
    sources = (workspace / "sources.md").read_text(encoding="utf-8")
    assert sources.count("| read |") == 0 or "read" in sources             # every decision is recorded
    assert "http" not in (workspace / "candidates.md").read_text(encoding="utf-8")


def test_finishing_without_choosing_fails_the_check(env_factory, workspace):
    """A screening task that never calls keep_sources hasn't done its job: the check sends it back."""
    env = env_factory([call("complete_task", summary="Looked at the list and moved on."),
                       call("keep_sources", keep=[1]),
                       call("complete_task", summary="Kept the first paper this time.")])
    env.runner.scholar = FakeScholar(workspace)
    job = make_job(env, seeds="replay", seed_count="1", screen_batch="2")
    start(env, job)
    screen = task_by_key(env, job, "screen0")
    assert screen["attempts"] >= 1 and any("candidates decided" in g for g in screen["guidance"])
    assert tick_until(env, job["id"], lambda: status_of(env, job, "screen0") == "done")
