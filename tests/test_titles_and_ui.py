import re
from pathlib import Path

import pytest

from localagent.server.titles import make_title

WEB = Path(__file__).resolve().parent.parent / "localagent" / "web"


@pytest.mark.parametrize("text,expected", [
    ("Can you please summarize the PDFs in my research folder and write a report?", "Summarize the PDFs in my research folder and…"),
    ("Hi, could you fix the failing tests in mathlib.py", "Fix the failing tests in mathlib.py"),
    ("I want you to rename all the photos by date. Use EXIF data.", "Rename all the photos by date"),
    ("help me plan a garden", "Plan a garden"),
    ("What is 2+2?", "What is 2+2"),
    ("   ", "New chat"),
])
def test_make_title(text, expected):
    assert make_title(text) == expected


def test_titles_stay_short():
    assert len(make_title("word " * 100)) <= 49


def test_hidden_attribute_wins_over_display_rules():
    # Regression: `#composer { display: flex }` overrode [hidden], showing a composer with no chat behind it.
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important", css)


def test_landing_page_composer_starts_a_chat():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'api("POST", "/api/chats", { project_id: null })' in js
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert '<form id="composer">' in html          # visible on the landing page
