import re
IMG_TAG = re.compile(r'<img[^>]+src="([^"]+)"[^>]*>', re.IGNORECASE)
HTMLY_RE = re.compile(r'</?(table|thead|tbody|tr|td|th|ul|ol|li|div|p|h[1-6]|span)\b', re.IGNORECASE)
# === SPLIT RESPONSE PARTS TOOL ===

def split_response_parts(html: str):
    """
    Split assistant HTML into parts: text, image, and html (tables/divs).
    Images are isolated; chunks that have HTML tags (e.g., <table>) are marked as 'html'
    so the frontend renders them directly (no streaming).
    """
    parts = []
    pos = 0
    html = html or ""

    for m in IMG_TAG.finditer(html):
        start, end = m.start(), m.end()
        if start > pos:
            chunk = html[pos:start].strip()
            if chunk:
                if HTMLY_RE.search(chunk):
                    parts.append({"type": "html", "html": chunk})   # NEW
                else:
                    parts.append({"type": "text", "html": chunk})
        src = m.group(1)
        parts.append({"type": "image", "src": src})
        pos = end

    if pos < len(html):
        tail = html[pos:].strip()
        if tail:
            if HTMLY_RE.search(tail):
                parts.append({"type": "html", "html": tail})        # NEW
            else:
                parts.append({"type": "text", "html": tail})

    return parts