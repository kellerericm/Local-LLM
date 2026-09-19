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
    pdf_urls: list[str] = field(default_factory=list)            # every open-access PDF location OpenAlex lists
    abstract: str | None = None                                  # shown to the model when it screens candidates
    venue: str | None = None

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


def abstract_from_inverted(index: dict | None) -> str | None:
    """OpenAlex stores abstracts as {word: [positions]}; put the words back in order."""
    if not index:
        return None
    words: list[tuple[int, str]] = [(pos, word) for word, positions in index.items() for pos in (positions or [])]
    return " ".join(w for _, w in sorted(words)[:400]) or None


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
    pdf_urls = []
    for loc in [best] + list(d.get("locations") or []):
        url = (loc or {}).get("pdf_url")
        if url and url not in pdf_urls and ((loc or {}).get("is_oa", True)):
            pdf_urls.append(url)
    if arxiv and f"https://arxiv.org/pdf/{arxiv}" not in pdf_urls:
        pdf_urls.append(f"https://arxiv.org/pdf/{arxiv}")
    return Work(
        pdf_urls=pdf_urls,
        openalex_id=_short_id(d.get("id")),
        title=d.get("display_name") or d.get("title") or "(untitled)",
        year=d.get("publication_year"),
        authors=[(a.get("author") or {}).get("display_name", "") for a in (d.get("authorships") or [])][:8],
        doi=(ids.get("doi") or d.get("doi") or "").removeprefix("https://doi.org/") or None,
        arxiv_id=arxiv,
        oa_pdf_url=oa_pdf,
        referenced_works=[_short_id(w) for w in (d.get("referenced_works") or []) if w],
        cited_by_count=d.get("cited_by_count"),
        abstract=abstract_from_inverted(d.get("abstract_inverted_index")),
        venue=((d.get("primary_location") or {}).get("source") or {}).get("display_name"),
    )


# Hosts that have served PDFs to a script in testing. Only a ranking hint for search results: `ScholarClient.locate`
# is the authority, because it fetches the first bytes and looks at them. Publisher sites return 403 to scripts even
# for open-access articles (final test: 9 of 12 seeds lost), and nature.com and jneurosci.org were both verified to
# refuse us on 2026-09-17 despite hosting the OA PDF.
OPEN_HOSTS = ("arxiv.org", "biorxiv.org", "medrxiv.org", "ncbi.nlm.nih.gov", "europepmc.org", "ebi.ac.uk",
              "plos.org", "frontiersin.org", "mdpi.com", "elifesciences.org",
              "openreview.net", "jmlr.org", "aaai.org", "neurips.cc", "mlr.press", "peerj.com",
              "springeropen.com", "biomedcentral.com", "hindawi.com", "cogitatiopress.com", "osf.io")


def obtainable(w: Work) -> bool:
    """True when some open-access location is on a host that has served scripted downloads. A hint for ordering
    search results; whether a paper can really be read is decided by fetching it (`locate`)."""
    urls = [u for u in ([w.oa_pdf_url] + list(w.pdf_urls)) if u]
    return any(any(h in u.lower() for h in OPEN_HOSTS) for u in urls)


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def jats_to_markdown(xml: bytes | str) -> str:
    """Convert a JATS article (PMC full text) to markdown: title, abstract, body sections as headings, figure captions,
    and a References section with one entry per line."""
    import xml.etree.ElementTree as ET

    if isinstance(xml, bytes):
        xml = xml.decode("utf-8", errors="replace")
    xml = re.sub(r"<!DOCTYPE[^>]*>", "", xml, count=1)
    xml = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)\w+;", " ", xml)   # DTD entities we can't resolve
    root = ET.fromstring(xml)
    out: list[str] = []
    title = next((e for e in root.iter() if _local(e.tag) == "article-title"), None)
    if title is not None:
        out += [f"# {_text(title)}", ""]
    for abstract in [e for e in root.iter() if _local(e.tag) == "abstract"][:1]:
        out += ["## Abstract", ""] + [_text(p) + "\n" for p in abstract.iter() if _local(p.tag) == "p"]

    def walk(el, depth: int) -> None:
        for child in el:
            tag = _local(child.tag)
            if tag == "sec":
                head = next((c for c in child if _local(c.tag) == "title"), None)
                if head is not None and _text(head):
                    out.extend([f"{'#' * min(depth, 4)} {_text(head)}", ""])
                walk(child, depth + 1)
            elif tag == "p":
                out.extend([_text(child), ""])
            elif tag in ("list", "list-item", "boxed-text"):
                walk(child, depth)
            elif tag in ("fig", "table-wrap"):
                label = next((_text(c) for c in child if _local(c.tag) == "label"), "")
                caption = next((_text(c) for c in child if _local(c.tag) == "caption"), "")
                if caption:
                    out.extend([f"{label or 'Figure'}: {caption}", ""])

    body = next((e for e in root.iter() if _local(e.tag) == "body"), None)
    if body is not None:
        walk(body, 2)
    refs = [e for e in root.iter() if _local(e.tag) == "ref"]
    if refs:
        out += ["## References", ""]
        for ref in refs:
            ids = {c.get("pub-id-type"): _text(c) for c in ref.iter() if _local(c.tag) == "pub-id"}
            for c in list(ref.iter()):
                for g in [g for g in c if _local(g.tag) in ("pub-id", "label")]:
                    g.text, g.tail = "", (g.tail or "")
                    g.clear()
            entry = _text(ref) + (f" doi:{ids['doi']}" if ids.get("doi") else "")
            out.append(re.sub(r"\s+([,.;:])", r"\1", entry))
    return "\n".join(out).strip() + "\n"


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

    def search(self, query: str, n: int = 10, open_access: bool = False, max_pages: int = 4,
               start_page: int = 1) -> list[Work]:
        """Ranked search. With open_access, ask OpenAlex for open works only. `start_page` continues a search the
        caller has already walked, so selection can keep asking for more results instead of re-reading the top."""
        filters = "type:article|preprint|review" + (",open_access.is_oa:true" if open_access else "")
        out: list[Work] = []
        for page in range(start_page, start_page + max_pages):
            data = self._get(f"{OPENALEX}/works", {"search": query, "per-page": 50, "page": page, "filter": filters})
            results = data.get("results", [])
            if not results:
                break
            works = [work_from_openalex(d) for d in results]
            out += [w for w in works if not open_access or obtainable(w)] if open_access else works
            if len(out) >= n:
                break
        return out[:n]

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

    def candidate_pdf_urls(self, paper: dict) -> list[str]:
        """Legal open-access PDF locations for a paper, best first:
        OpenAlex OA locations, Europe PMC (open-access full text), Semantic Scholar openAccessPdf, arXiv title match."""
        urls: list[str] = []

        def add(u):
            if u and u not in urls:
                urls.append(u)

        oa_id = paper.get("openalex_id")
        if oa_id:
            try:
                w = work_from_openalex(self._get(f"{OPENALEX}/works/{oa_id}"))
                for u in w.pdf_urls:
                    add(u)
            except Exception:
                pass
        add(paper.get("oa_pdf_url"))
        doi = paper.get("doi")
        if doi:
            try:
                data = self._get("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                                 {"query": f'DOI:"{doi}"', "format": "json", "resultType": "lite"})
                for r in (data.get("resultList") or {}).get("result", []):
                    if r.get("pmcid") and r.get("isOpenAccess") == "Y":
                        add(f"https://europepmc.org/articles/{r['pmcid']}?pdf=render")
            except Exception:
                pass
            try:
                data = self._get(f"https://api.semanticscholar.org/graph/v1/paper/DOI:{urllib.parse.quote(doi)}",
                                 {"fields": "openAccessPdf"})
                add(((data or {}).get("openAccessPdf") or {}).get("url"))
            except Exception:
                pass
        if paper.get("arxiv_id"):
            add(f"https://arxiv.org/pdf/{paper['arxiv_id']}")
        elif paper.get("title"):
            try:
                q = urllib.parse.quote(f'ti:"{normalize_title(paper["title"])[:150]}"')
                req = urllib.request.Request(f"http://export.arxiv.org/api/query?search_query={q}&max_results=3",
                                             headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=30) as r:
                    feed = r.read().decode("utf-8", errors="replace")
                for entry in re.findall(r"<entry>(.*?)</entry>", feed, re.S):
                    title = re.search(r"<title>(.*?)</title>", entry, re.S)
                    ident = re.search(r"<id>https?://arxiv\.org/abs/([^<]+?)(?:v\d+)?</id>", entry)
                    if title and ident and normalize_title(title.group(1)) == normalize_title(paper["title"]):
                        add(f"https://arxiv.org/pdf/{ident.group(1)}")
            except Exception:
                pass
        return urls

    def pmc_fulltext_urls(self, paper: dict) -> list[str]:
        """Europe PMC REST full-text URLs for a paper (open-access records only), without fetching them."""
        doi = paper.get("doi")
        query = f'DOI:"{doi}"' if doi else (f'TITLE:"{paper["title"]}"' if paper.get("title") else None)
        if not query:
            return []
        try:
            data = self._get("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                             {"query": query, "format": "json", "resultType": "lite"})
        except Exception:
            return []
        out = []
        for r in (data.get("resultList") or {}).get("result", []):
            if not (r.get("pmcid") and r.get("isOpenAccess") == "Y"):
                continue
            if not doi and normalize_title(r.get("title", "")) != normalize_title(paper["title"]):
                continue
            out.append(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{r['pmcid']}/fullTextXML")
        return out

    def full_text(self, paper: dict) -> tuple[str, str] | None:
        """Open-access full text as markdown from Europe PMC's REST API (JATS XML), for papers whose PDF hosts refuse
        scripted downloads. Returns (markdown, url) or None."""
        for url in self.pmc_fulltext_urls(paper):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    xml = resp.read(MAX_PDF_BYTES + 1)
                if len(xml) > MAX_PDF_BYTES:
                    continue
                text = jats_to_markdown(xml)
            except Exception:
                continue
            if len(text) > 2000:
                return text, url
        return None

    def probe(self, url: str, kind: str = "pdf", timeout: int = 20) -> bool:
        """Fetch the first bytes of a URL and say whether they are the kind of document we expect. This is what
        decides whether a source is real: publishers answer 403, or serve an HTML landing page where OpenAlex
        promised a PDF, and an `is_oa` flag never shows it."""
        accept = "application/pdf,*/*;q=0.8" if kind == "pdf" else "application/xml,*/*;q=0.8"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept,
                                                   "Range": "bytes=0-8191"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                head = r.read(8192)
        except Exception:
            return False
        if kind == "pdf":
            return head.startswith(b"%PDF")
        return b"<article" in head[:8192].lower()

    def locate(self, paper: dict) -> tuple[str, str] | None:
        """The first location that actually hands us this paper: (url, kind), kind being 'pdf' or 'pmc'.
        Every candidate is fetched far enough to see what it is, so an unreachable or non-paper URL is never
        passed on as a source. Returns None when nothing serves it."""
        for url in self.candidate_pdf_urls(paper):
            if self.probe(url, "pdf"):
                return url, "pdf"
        for url in self.pmc_fulltext_urls(paper):
            if self.probe(url, "xml"):
                return url, "pmc"
        return None

    def wikipedia_references(self, article: str, limit: int = 0) -> list[dict]:
        """The works an encyclopedia article cites: a curated reading list for a topic. Returns dicts with doi, title,
        year and arxiv, in article order, for resolving against the scholarly index."""
        data = self._get("https://en.wikipedia.org/w/api.php", {
            "action": "query", "format": "json", "prop": "revisions", "rvprop": "content", "rvslots": "main",
            "titles": article, "redirects": 1})
        pages = (data.get("query") or {}).get("pages") or {}
        page = next(iter(pages.values()), {})
        if "revisions" not in page:
            return []
        text = page["revisions"][0]["slots"]["main"]["*"]
        out: list[dict] = []
        seen: set[str] = set()
        for block in re.findall(r"\{\{\s*(?:cite|citation)[^{}]*(?:\{\{[^{}]*\}\}[^{}]*)*\}\}", text, re.I | re.S):
            def field(name: str) -> str | None:
                m = re.search(rf"\|\s*{name}\s*=\s*([^|}}\n]+)", block, re.I)
                return m.group(1).strip() or None if m else None

            doi, title = field("doi"), field("title")
            arxiv = field("arxiv") or field("eprint")
            year = field("year") or (field("date") or "")
            y = re.search(r"(19|20)\d{2}", year or "")
            key = (doi or "").lower() or normalize_title(title or "") or (arxiv or "")
            if not key or key in seen or not (doi or title or arxiv):
                continue
            seen.add(key)
            out.append({"doi": doi, "title": re.sub(r"\[\[|\]\]|''", "", title) if title else None,
                        "arxiv": arxiv, "year": int(y.group(0)) if y else None})
            if limit and len(out) >= limit:
                break
        return out

    def download_pdf(self, url: str, dest: Path) -> bool:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*;q=0.8"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read(MAX_PDF_BYTES + 1)
        if len(data) > MAX_PDF_BYTES or not data.startswith(b"%PDF"):
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True
