import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_CSRF_META_RE = re.compile(r'<meta name="csrf-token" content="([^"]+)">')


def get_csrf_token(client, path: str = "/"):
    """GET a page to mint (or read) this TestClient's session CSRF token and
    return it, for use in POSTs to routes protected by require_csrf_form /
    require_csrf_header. Every page extends base.html, which always renders the
    <meta name="csrf-token"> tag, so any route works as the source page.
    """
    response = client.get(path)
    match = _CSRF_META_RE.search(response.text)
    assert match is not None, f"no csrf-token meta tag found in response for {path}"
    return match.group(1)
