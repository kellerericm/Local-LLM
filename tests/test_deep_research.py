import pytest

from localagent.jobs.scholar import Work, work_from_openalex, work_key
from localagent.jobs.templates.deep_research import cfg, pdf_path
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

    def download_pdf(self, url, dest):
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


def reader(text_for_paper):
    """Scripted reading: write the paper summary, save a note, record no references (OpenAlex has them)."""
    def respond(msgs):
        system = msgs[0]["content"]
        import re
        md = re.search(r"Write (papers/\S+\.md)", system).group(1)
        src = re.search(r"Read (papers/pdf/\S+\.pdf)", system).group(1)
        return (call("write_file", path=md, content="# X\n## Section summaries\n### Intro\nok\n## Key claims\n- c [n1]\n"
                                                    "## Value of this paper\nuseful")
                + call("add_note", claim="The paper's text", quote="Text of https", source=src))
    return respond


def test_query_seeds_gate_rounds_convergence_and_report_phase(env_factory, workspace):
    fake = FakeScholar(workspace)
    responses = []
    for _ in range(4):                                    # A, B read in round 0 (C unavailable -> skipped); F1, F2 in round 1
        responses += [reader(None), call("complete_task", summary="Summarized the paper."), call("report_review", verdict="pass")]
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
    assert "a0_1" in keys and "p0_3" in keys and "cite_r0" in keys

    # Paper C has no open-access copy: its acquire task asks the user; answer skip.
    assert tick_until(env, job["id"], lambda: any(t["key"] == "a0_3" and t["status"] == "waiting_user"
                                                  for t in env.jobs.list_tasks(job["id"])))
    a03 = next(t for t in env.jobs.list_tasks(job["id"]) if t["key"] == "a0_3")
    assert "No open-access copy" in a03["question"]
    env.runner.answer(job["id"], "skip", a03["id"])

    assert tick_until(env, job["id"], lambda: any(t["key"] == "cite_r0" and t["status"] == "done"
                                                  for t in env.jobs.list_tasks(job["id"])))
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

    def read_local(msgs):
        refs = ref_lists.pop(0)
        import re
        system = msgs[0]["content"]
        md = re.search(r"Write (papers/\S+\.md)", system).group(1)
        src = re.search(r"Read (papers/\S+\.pdf)", system).group(1)
        return (call("write_file", path=md, content="## Value of this paper\nx [n1]") +
                call("add_note", claim="replay", quote="about hippocampal replay", source=src) +
                call("record_references", references=refs))

    responses = []
    for _ in range(2):
        responses += [read_local, call("complete_task", summary="Summarized it."), call("report_review", verdict="pass")]
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


def test_config_defaults_match_decisions():
    c = cfg({"inputs": {}})
    assert (c["max_papers"], c["max_rounds"], c["min_citations"], c["min_fraction"]) == (60, 4, 3, 0.15)
    assert pdf_path("oa:W1").startswith("papers/pdf/")
