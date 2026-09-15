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

    def search(self, query, n=10):
        return [self.works[k] for k in ("WA", "WB", "WC")][:n]

    def get_by_ids(self, ids):
        return [self.works[i] for i in ids if i in self.works]

    def get_by_doi(self, doi):
        return None

    def find_by_title(self, title, year=None):
        return None

    def candidate_pdf_urls(self, paper):
        return ["https://publisher.example/landing.html"] + ([paper["oa_pdf_url"]] if paper.get("oa_pdf_url") else [])

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


def make_job(env, **inputs):
    base = {"seed_mode": "query", "seeds": "memory consolidation", "min_citations": "2", "min_fraction": "0.1"}
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
            src = re.search(r'source "(papers/\S+\.pdf)"', system).group(1)
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


def test_query_seeds_gate_rounds_convergence_and_report_phase(env_factory, workspace):
    fake = FakeScholar(workspace)
    responses = read_paper_responses(4)                   # A, B read in round 0 (C unavailable -> skipped); F1, F2 in round 1
    env = env_factory(responses)
    env.runner.scholar = fake
    job = make_job(env)
    env.runner._tick()                                     # template plan
    env.runner.approve_plan(job["id"])
    env.runner._tick()                                     # seed (code)
    seeds = env.jobs.list_papers(job["id"])
    assert [p["title"] for p in seeds] == ["Paper A", "Paper B", "Paper C"]
    assert (workspace / "seeds.md").exists()
    env.runner._tick()                                     # gate opens
    gate = next(t for t in env.jobs.list_tasks(job["id"]) if t["key"] == "seed_gate")
    env.runner.answer(job["id"], "approve", gate["id"])
    keys = [t["key"] for t in env.jobs.list_tasks(job["id"])]
    assert "a0_1" in keys and "a0_3" in keys and "cite_r0" in keys and not any(k.startswith("p0_") for k in keys)

    # Paper C has no open-access copy: its acquire task asks the user; answer skip.
    assert tick_until(env, job["id"], lambda: any(t["key"] == "a0_3" and t["status"] == "waiting_user"
                                                  for t in env.jobs.list_tasks(job["id"])))
    a03 = next(t for t in env.jobs.list_tasks(job["id"]) if t["key"] == "a0_3")
    assert "No open-access copy" in a03["question"]
    env.runner.answer(job["id"], "skip", a03["id"])

    assert tick_until(env, job["id"], lambda: any(t["key"] == "cite_r0" and t["status"] == "done"
                                                  for t in env.jobs.list_tasks(job["id"])))
    tasks = {t["key"]: t for t in env.jobs.list_tasks(job["id"])}
    assert tasks["p0_1_1"]["status"] == "done" and tasks["w0_1"]["depends_on"] == ["p0_1_1"]
    assert "p0_3_1" not in tasks                                           # skipped paper gets no reading tasks
    rounds = env.jobs.get_job(job["id"])["inputs"]["citation_rounds"]
    assert rounds[0]["read"] == 2 and rounds[0]["selected"] == 2           # F1 and F2 cited by both A and B
    assert {p["title"] for p in env.jobs.list_papers(job["id"], "reading")} == {"Foundation One", "Foundation Two"}

    assert tick_until(env, job["id"], lambda: any(t["key"] == "layout" for t in env.jobs.list_tasks(job["id"])))
    rounds = env.jobs.get_job(job["id"])["inputs"]["citation_rounds"]
    assert rounds[-1]["stop"].startswith("converged")
    papers = {p["title"]: p for p in env.jobs.list_papers(job["id"])}
    assert papers["Paper C"]["status"] == "skipped" and papers["Foundation One"]["cited_by_read"] == 3
    assert len(fake.downloads) == 4
    graph = (workspace / "citation_graph.md").read_text(encoding="utf-8")
    assert "Foundation One" in graph


def test_network_requires_permission(env_factory):
    approver = AutoApprover(allow=False)
    env = env_factory([], approver=approver)
    env.runner.scholar = FakeScholar(env.workspace)
    job = env.jobs.create_job(env.project["id"], "R", "Q?", template="deep_research",
                              inputs={"seed_mode": "query", "seeds": "x"})      # no net permission
    env.runner._tick()
    env.runner.approve_plan(job["id"])
    env.runner._tick()
    seed = next(t for t in env.jobs.list_tasks(job["id"]) if t["key"] == "seed")
    assert seed["status"] == "waiting_user" and seed["waiting_kind"] == "approval"
    assert approver.requests[0]["keys"] == ["net:open-access"]


def test_folder_seeds_use_extracted_references(env_factory, workspace):
    (workspace / "papers").mkdir()
    for name in ("one", "two"):
        make_pdf(workspace / "papers" / f"{name}.pdf", [f"Paper {name} about hippocampal replay."])
    ref_lists = [[{"title": "Shared Classic", "year": 1990}, {"title": "Only once", "year": 2000}],
                 [{"title": "Shared Classic", "year": 1990}]]

    responses = read_paper_responses(2, ref_lists, quote="about hippocampal replay")
    env = env_factory(responses)
    env.runner.scholar = FakeScholar(workspace)
    job = env.jobs.create_job(env.project["id"], "R", "Q?", template="deep_research", permissions=["net:open-access"],
                              inputs={"seed_mode": "folder", "seeds": "papers", "min_citations": "2", "max_rounds": "1"})
    env.runner._tick()
    assert [t["key"] for t in env.jobs.list_tasks(job["id"])] == ["seed"]       # folder mode: no seed gate
    env.runner.approve_plan(job["id"])
    assert tick_until(env, job["id"], lambda: any(t["key"] == "cite_r0" and t["status"] == "done"
                                                  for t in env.jobs.list_tasks(job["id"])))
    rounds = env.jobs.get_job(job["id"])["inputs"]["citation_rounds"]
    assert rounds[0]["candidates"] == 1 and rounds[0]["selected"] == 1          # only "Shared Classic" meets 2
    queued = [p for p in env.jobs.list_papers(job["id"]) if p["round"] == 1]
    assert queued[0]["title"] == "Shared Classic" and queued[0]["key"] == work_key(title="Shared Classic", year=1990)


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
    assert {"seed_count", "min_fraction", "per_round", "max_papers", "max_rounds", "min_citations"} <= set(schema)


def test_config_defaults_match_decisions():
    c = cfg({"inputs": {}})
    assert (c["max_papers"], c["max_rounds"], c["min_citations"], c["min_fraction"]) == (60, 4, 3, 0.15)
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


def start_query_job(env, **inputs):
    job = make_job(env, **inputs)
    env.runner._tick()
    env.runner.approve_plan(job["id"])
    env.runner._tick()
    env.runner._tick()
    gate = next(t for t in env.jobs.list_tasks(job["id"]) if t["key"] == "seed_gate")
    env.runner.answer(job["id"], "approve", gate["id"])
    return job


def task_by_key(env, job, key):
    return next((t for t in env.jobs.list_tasks(job["id"]) if t["key"] == key), None)


def test_full_text_fallback_and_long_paper_question(env_factory, workspace):
    body = "\n\n".join(f"## Section {i}\n\n" + ("Replay text. " * 1000) for i in range(30))
    texts = {"Paper A": "# Paper A\n\n## Introduction\n\nShort paper about replay.\n\n## Methods\n\nRats.\n\n"
                        "## Discussion\n\n" + "More. " * 400 + "\n\n## References\n\nFoster DJ. 2006 Reverse replay.",
             "Paper B": "# Paper B\n\n" + body}
    env = env_factory([])
    env.runner.scholar = NoPdfScholar(workspace, texts)
    job = start_query_job(env, seed_count="2")
    assert tick_until(env, job["id"], lambda: task_by_key(env, job, "a0_2")["status"] == "waiting_user")
    a = {p["title"]: p for p in env.jobs.list_papers(job["id"])}
    assert a["Paper A"]["file_path"].startswith("papers/text/") and a["Paper A"]["provenance"]["source"].startswith(
        "open-access full text")
    assert (workspace / a["Paper A"]["provenance"]["folder"] / "references.txt").read_text(encoding="utf-8").count(
        "Foster") == 1
    assert task_by_key(env, job, "w0_1") is not None                     # reading tasks for A were added
    q = task_by_key(env, job, "a0_2")["question"]
    assert "is long" in q and "read all" in q
    env.runner.answer(job["id"], "read all", task_by_key(env, job, "a0_2")["id"])
    assert tick_until(env, job["id"], lambda: task_by_key(env, job, "w0_2") is not None)
    n = a["Paper B"]["provenance"] and env.jobs.get_paper(job["id"], a["Paper B"]["key"])["provenance"]["parts"]
    assert n > 12 and task_by_key(env, job, f"p0_2_{n}") is not None


def test_no_papers_read_asks_before_writing_an_empty_report(env_factory, workspace):
    env = env_factory([])
    env.runner.scholar = NoPdfScholar(workspace)
    job = start_query_job(env, seed_count="2")
    for key in ("a0_1", "a0_2"):
        assert tick_until(env, job["id"], lambda: task_by_key(env, job, key)["status"] == "waiting_user")
        env.runner.answer(job["id"], "skip", task_by_key(env, job, key)["id"])
    assert tick_until(env, job["id"], lambda: task_by_key(env, job, "cite_r0")["status"] == "waiting_user")
    assert "No papers could be read" in task_by_key(env, job, "cite_r0")["question"]
    assert task_by_key(env, job, "layout") is None
    env.runner.answer(job["id"], "continue", task_by_key(env, job, "cite_r0")["id"])
    assert tick_until(env, job["id"], lambda: task_by_key(env, job, "layout") is not None)
