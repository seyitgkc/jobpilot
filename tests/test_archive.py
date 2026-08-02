import asyncio
import base64

from jobpilot.browserd import archive

SITE = {
    "https://site.test/style.css": (
        b"body { background: url('bg.png'); color: black; }",
        "text/css",
    ),
    "https://site.test/bg.png": (b"PNGBYTES", "image/png"),
    "https://site.test/logo.png": (b"LOGOBYTES", "image/png"),
}


async def fake_fetch(url):
    return SITE.get(url)


def run(html, base_url="https://site.test/job", fetch=fake_fetch):
    return asyncio.run(archive.inline_html(html, base_url, fetch))


def test_stylesheet_inlined_with_nested_urls():
    html = '<html><head><link rel="stylesheet" href="/style.css"></head><body>x</body></html>'
    result = run(html)
    assert "<link" not in result
    assert "<style>" in result
    encoded = base64.b64encode(b"PNGBYTES").decode()
    assert f"data:image/png;base64,{encoded}" in result


def test_images_become_data_uris_and_srcset_dropped():
    html = '<img src="logo.png" srcset="logo.png 1x, logo@2x.png 2x">'
    result = run(html)
    encoded = base64.b64encode(b"LOGOBYTES").decode()
    assert f"data:image/png;base64,{encoded}" in result
    assert "srcset" not in result


def test_scripts_and_preloads_dropped():
    html = (
        '<head><script src="app.js"></script>'
        '<link rel="preload" href="font.woff2" as="font"></head>'
        "<body><noscript>enable js</noscript>text</body>"
    )
    result = run(html)
    assert "<script" not in result
    assert "preload" not in result
    assert "noscript" not in result
    assert "text" in result


def test_failed_fetch_leaves_reference():
    html = '<img src="missing.png">'
    result = run(html)
    assert 'src="missing.png"' in result


def test_oversized_resource_skipped():
    big = b"x" * (archive.MAX_RESOURCE_BYTES + 1)

    async def fetch(url):
        return big, "image/png"

    result = asyncio.run(
        archive.inline_html('<img src="big.png">', "https://site.test/", fetch)
    )
    assert 'src="big.png"' in result


def test_data_and_anchor_refs_untouched():
    html = '<img src="data:image/png;base64,AAAA"><a href="#top">top</a>'
    result = run(html)
    assert "data:image/png;base64,AAAA" in result
