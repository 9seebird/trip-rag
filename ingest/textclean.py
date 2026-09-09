"""TourAPI 텍스트 정리.

overview 에는 <br>, &nbsp; 같은 게 섞여 온다. homepage 는 <a href="..."> 통째로 온다.
임베딩 품질과도 직결되므로 저장 전에 턴다.
"""
import html
import re

TAG = re.compile(r"<[^>]+>")
BR = re.compile(r"<\s*br\s*/?\s*>", re.I)
P_END = re.compile(r"</\s*(p|div|li)\s*>", re.I)
WS = re.compile(r"[ \t ]+")
NL = re.compile(r"\n{3,}")
HREF = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.I)


def clean(s):
    if not s:
        return None
    s = BR.sub("\n", s)
    s = P_END.sub("\n", s)
    s = TAG.sub("", s)
    s = html.unescape(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = WS.sub(" ", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    s = NL.sub("\n\n", s).strip()
    return s or None


def first_url(s):
    """homepage 필드에서 URL 만 뽑는다."""
    if not s:
        return None
    m = HREF.search(s)
    if m:
        return m.group(1).strip()
    c = clean(s)
    if c and c.startswith("http"):
        return c.split()[0]
    return c
