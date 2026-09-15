"""Open scholarly index client for deep research: OpenAlex (metadata, references, open-access locations) and arXiv.

Only legal open-access copies are downloaded (OpenAlex OA locations, arXiv). Every response is untrusted data.
Tests inject a fake client with the same methods.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

OPENALEX = "https://api.openalex.org"
USER_AGENT = "LocalAgent-deep-research/0.1 (open-access research assistant)"
MAX_PDF_BYTES = 60 * 2**20


@dataclass
class Work:
    openalex_id: str | None
    title: str
    year: int | None = None
    authors: list[str] = field(default_factory=list)
    doi: str | None = None
    arxiv_id: str | None = None
    oa_pdf_url: str | None = None
    referenced_works: list[str] = field(default_factory=list)   # OpenAlex ids (W...)
    cited_by_count: int | None = None

    def key(self) -> str:
        return work_key(self.openalex_id, self.doi, self.arxiv_id, self.title, self.year)


def normalize_title(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (title or "").lower()))[:120]


def work_key(openalex_id=None, doi=None, arxiv_id=None, title=None, year=None) -> str:
    if openalex_id:
        return "oa:" + openalex_id.rsplit("/", 1)[-1]
    if doi:
        return "doi:" + doi.lower().removeprefix("https://doi.org/")
    if arxiv_id:
        return "arxiv:" + re.sub(r"v\d+$", "", arxiv_id)
    return "t:" + normalize_title(title or "") + (f":{year}" if year else "")


def _short_id(openalex_url: str | None) -> str | None:
    return openalex_url.rsplit("/", 1)[-1] if openalex_url else None


def work_from_openalex(d: dict) -> Work:
    ids = d.get("ids") or {}
    arxiv = None
    for loc in d.get("locations") or []:
        url = ((loc or {}).get("landing_page_url") or "") + " " + ((loc or {}).get("pdf_url") or "")
        m = re.search(r"arxiv\.org/(?:abs|pdf)/([\w.\-/]+?)(?:v\d+)?(?:\.pdf)?(?:\s|$)", url)
        if m:
            arxiv = m.group(1)
            break
    best = d.get("best_oa_location") or {}
    oa_pdf = best.get("pdf_url") or ((d.get("open_access") or {}).get("oa_url") if str(
        (d.get("open_access") or {}).get("oa_url") or "").lower().endswith(".pdf") else None)
    if not oa_pdf and arxiv:
        oa_pdf = f"https://arxiv.org/pdf/{arxiv}"
    return Work(
        openalex_id=_short_id(d.get("id")),
        title=d.get("display_name") or d.get("title") or "(untitled)",
        year=d.get("publication_year"),
        authors=[(a.get("author") or {}).get("display_name", "") for a in (d.get("authorships") or [])][:8],
        doi=(ids.get("doi") or d.get("doi") or "").removeprefix("https://doi.org/") or None,
        arxiv_id=arxiv,
        oa_pdf_url=oa_pdf,
        referenced_works=[_short_id(w) for w in (d.get("referenced_works") or []) if w],
        cited_by_count=d.get("cited_by_count"),
    )


class ScholarClient:
    def __init__(self, mailto: str | None = None, min_interval_s: float = 0.2):
        self.mailto = mailto
        self.min_interval_s = min_interval_s
        self._last = 0.0

    def _get(self, url: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        if self.mailto:
            params["mailto"] = self.mailto
        full = url + ("?" + urllib.parse.urlencode(params) if params else "")
        wait = self.min_interval_s - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            self._last = time.time()
            return json.loads(r.read().decode("utf-8"))

    def search(self, query: str, n: int = 10) -> list[Work]:
        data = self._get(f"{OPENALEX}/works", {"search": query, "per-page": min(n, 50),
                                                 "filter": "type:article|preprint|review"})
        return [work_from_openalex(d) for d in data.get("results", [])]

    def get_by_ids(self, openalex_ids: list[str]) -> list[Work]:
        out = []
        for i in range(0, len(openalex_ids), 50):
            chunk = "|".join(openalex_ids[i:i + 50])
            data = self._get(f"{OPENALEX}/works", {"filter": f"openalex_id:{chunk}", "per-page": 50})
            out += [work_from_openalex(d) for d in data.get("results", [])]
        return out

    def get_by_doi(self, doi: str) -> Work | None:
        try:
            return work_from_openalex(self._get(f"{OPENALEX}/works/https://doi.org/{urllib.parse.quote(doi)}"))
        except Exception:
            return None

    def find_by_title(self, title: str, year: int | None = None) -> Work | None:
        params = {"filter": f"title.search:{normalize_title(title)[:200]}", "per-page": 5}
        results = [work_from_openalex(d) for d in self._get(f"{OPENALEX}/works", params).get("results", [])]
        want = normalize_title(title)
        for w in results:
            if normalize_title(w.title) == want and (not year or not w.year or abs(w.year - year) <= 1):
                return w
        return results[0] if results and normalize_title(results[0].title).startswith(want[:40]) else None

    def download_pdf(self, url: str, dest: Path) -> bool:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read(MAX_PDF_BYTES + 1)
        if len(data) > MAX_PDF_BYTES or not data.startswith(b"%PDF"):
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True
