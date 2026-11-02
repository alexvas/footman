"""Check lifecycle-guide file links and ATX-heading anchors, with negative controls."""

from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import pytest


GUIDES = ("filesystem-lifecycle.md", "process-lifecycle.md")
DOCS = Path(__file__).resolve().parents[1] / "docs"


def prose(source):
    """Ignore fenced examples, which can contain non-document links/headings."""
    lines = []
    fence = None
    for line in source.splitlines():
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence):
                fence = None
        elif match:
            fence = match[1]
        else:
            lines.append(line)
    return "\n".join(lines)


def heading_anchors(source):
    anchors = set()
    for heading in re.findall(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", prose(source), re.M):
        slug = re.sub(r"[^\w\-\s]", "", heading.lower()).replace(" ", "-")
        candidate, suffix = slug, 0
        while candidate in anchors:
            suffix += 1
            candidate = f"{slug}-{suffix}"
        anchors.add(candidate)
    return anchors


def assert_local_links(source, guide):
    """Resolve text relative to its logical guide path, including scratch copies."""
    for target in re.findall(r'\]\(([^\s)]+)(?:\s+"[^"]*")?\)', prose(source)):
        url = urlsplit(target)
        if url.scheme or url.netloc:
            continue
        path = (guide.parent / unquote(url.path)).resolve() if url.path else guide.resolve()
        assert path.is_file(), f"{guide.name}: broken link {target}"
        if url.fragment:
            assert path.suffix == ".md", f"{guide.name}: unsupported fragment {target}"
            linked_source = source if path == guide.resolve() else path.read_text()
            assert unquote(url.fragment) in heading_anchors(linked_source), (
                f"{guide.name}: broken anchor {target}"
            )


@pytest.mark.parametrize("name", GUIDES)
def test_local_guide_links_resolve(name):
    guide = DOCS / name
    assert_local_links(guide.read_text(), guide)


@pytest.mark.parametrize("name", GUIDES)
@pytest.mark.parametrize("target, message", [
    ("missing-lifecycle-probe.md", "broken link"),
    ("#nonexistent-probe-heading", "broken anchor"),
    ("process-lifecycle.md#nonexistent-probe-heading", "broken anchor"),
])
def test_scratch_guide_rejects_injected_link(tmp_path, name, target, message):
    guide = DOCS / name
    scratch = tmp_path / name
    scratch.write_text(guide.read_text())
    # A scratch copy must first pass with its original relative-link base.
    assert_local_links(scratch.read_text(), guide)
    scratch.write_text(scratch.read_text() + f"\n[Injected probe]({target})\n")
    with pytest.raises(AssertionError, match=message):
        assert_local_links(scratch.read_text(), guide)


def test_heading_slug_and_fenced_code_rules(tmp_path):
    guide = tmp_path / "guide.md"
    other = tmp_path / "other.md"
    other.write_text("# Cross Heading\n")
    source = """# Heading `API`!
## Heading `API`!
```markdown
# Not a heading
[x](missing.md)
```
[x](#heading-api)
[x](#heading-api-1)
[x](other.md#cross-heading)
"""
    guide.write_text(source)
    assert heading_anchors(source) == {"heading-api", "heading-api-1"}
    assert_local_links(source, guide)
    with pytest.raises(AssertionError, match="broken anchor"):
        assert_local_links(source + "\n[x](#not-a-heading)\n", guide)
