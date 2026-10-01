"""사이트·키 <-> URL 상호 변환.

기존 hvst 프로젝트(hvstURL.py)의 url_templates/site_pattern 로직을 이식·확장.
사이트 이름은 gallery-dl 카테고리와 정렬한다. (표준 라이브러리 re만 사용)
"""

import re

URL_TEMPLATES: dict[str, list[str]] = {
    "twitter": [
        "https://x.com/{key}",
        "https://twitter.com/{key}",
    ],
    "pixiv": ["https://www.pixiv.net/users/{key}"],
    "tumblr": ["https://www.tumblr.com/{key}"],
    "deviantart": ["https://www.deviantart.com/{key}"],
    "bluesky": ["https://bsky.app/profile/{key}"],
    "baraag": ["https://baraag.net/@{key}"],
    "instagram": ["https://www.instagram.com/{key}/"],
    "naver": ["https://blog.naver.com/{key}"],
    "naverwebtoon": [
        "https://comic.naver.com/webtoon/list?titleId={key}",
        "https://comic.naver.com/bestChallenge/list?titleId={key}",
    ],
    "artstation": ["https://www.artstation.com/{key}"],
    "furaffinity": ["https://www.furaffinity.net/user/{key}/"],
    "e621": ["https://e621.net/posts?tags={key}"],
    "gelbooru": ["https://gelbooru.com/index.php?page=post&s=list&tags={key}"],
    "danbooru": ["https://danbooru.donmai.us/posts?tags={key}"],
    "rule34": ["https://rule34.xxx/index.php?page=post&s=list&tags={key}"],
    "rule34.paheal": ["https://rule34.paheal.net/post/list/{key}"],
    "sankaku": ["https://chan.sankakucomplex.com/?tags={key}"],
    "fanbox": ["https://www.pixiv.net/fanbox/creator/{key}"],
    "nijie": ["https://nijie.info/members.php?id={key}"],
    "kemono": ["https://kemono.cr/{key}"],
}

SITE_PATTERNS: dict[str, list[str]] = {
    "twitter": ["x.com", "twitter.com"],
    "pixiv": ["pixiv.net"],
    "tumblr": ["tumblr.com"],
    "deviantart": ["deviantart.com"],
    "bluesky": ["bsky.app"],
    "baraag": ["baraag.net"],
    "instagram": ["instagram.com"],
    "naverwebtoon": ["comic.naver.com"],
    "naver": ["blog.naver"],
    "artstation": ["artstation.com"],
    "furaffinity": ["furaffinity.net"],
    "e621": ["e621.net", "e926.net"],
    "gelbooru": ["gelbooru.com"],
    "danbooru": ["danbooru.donmai.us"],
    "rule34.paheal": ["rule34.paheal.net"],
    "rule34": ["rule34.xxx"],
    "sankaku": ["chan.sankakucomplex.com"],
    "fanbox": ["fanbox.cc"],
    "nijie": ["nijie.info"],
    "kemono": ["kemono.cr", "kemono.su", "kemono.party"],
}

SITE_HOME_URLS: dict[str, str] = {
    "twitter": "https://x.com",
    "pixiv": "https://www.pixiv.net",
    "tumblr": "https://www.tumblr.com",
    "deviantart": "https://www.deviantart.com",
    "bluesky": "https://bsky.app",
    "baraag": "https://baraag.net",
    "instagram": "https://www.instagram.com",
    "naver": "https://blog.naver.com",
    "naverwebtoon": "https://comic.naver.com",
    "artstation": "https://www.artstation.com",
    "furaffinity": "https://www.furaffinity.net",
    "e621": "https://e621.net",
    "gelbooru": "https://gelbooru.com",
    "danbooru": "https://danbooru.donmai.us",
    "rule34": "https://rule34.xxx",
    "rule34.paheal": "https://rule34.paheal.net",
    "sankaku": "https://chan.sankakucomplex.com",
    "fanbox": "https://www.fanbox.cc",
    "nijie": "https://nijie.info",
    "kemono": "https://kemono.cr",
}

SITE_ALIASES: dict[str, str] = {
    "x": "twitter",
    "x.com": "twitter",
    "twitter.com": "twitter",
    "bsky": "bluesky",
    "bsky.app": "bluesky",
    "kemono.cr": "kemono",
    "kemono.su": "kemono",
    "kemono.party": "kemono",
    "r34": "rule34",
    "rule34.xxx": "rule34",
}

supported_sites = list(URL_TEMPLATES.keys())


def normalize_site(site) -> str:
    name = (site or "").strip().lower()
    return SITE_ALIASES.get(name, name)


def site_home_url(site) -> str | None:
    site = normalize_site(site)
    if not site:
        return None
    if site in SITE_HOME_URLS:
        return SITE_HOME_URLS[site]
    patterns = SITE_PATTERNS.get(site)
    if patterns:
        return "https://" + patterns[0].split("/")[0]
    return None


def make_url(site, key) -> str | None:
    site = normalize_site(site)
    key = (key or "").strip()
    if key.startswith("@"):
        key = key[1:].strip()
    if not site:
        return None
    if not key:
        return site_home_url(site)
    templates = URL_TEMPLATES.get(site)
    if not templates:
        return site_home_url(site)
    if site == "twitter" and not key.isdigit():
        return "https://x.com/{key}".format(key=key)
    if site == "bluesky":
        if key.startswith("did:") or "." in key:
            return "https://bsky.app/profile/{key}".format(key=key)
        if len(key) == 24 and re.fullmatch(r"[a-z0-9]+", key):
            return "https://bsky.app/profile/did:plc:{key}".format(key=key)
        return "https://bsky.app/profile/{key}.bsky.social".format(key=key)
    return templates[0].format(key=key)


def _extract_key(site: str, url: str) -> str | None:
    for template in URL_TEMPLATES.get(site, []):
        pattern = re.escape(template)
        pattern = pattern.replace(r"\{any\}", r"[^/]+")
        if site == "kemono":
            pattern = pattern.replace(r"\{key\}", r"(?P<key>.+)")
        else:
            pattern = pattern.replace(r"\{key\}", r"(?P<key>[^/?#]+)")
        match = re.search(pattern, url)
        if match:
            return match.group("key")
    return None


def url_to_site_key(url) -> tuple[str | None, str | None]:
    text = (url or "").strip()
    if not text:
        return None, None
    # 1) 템플릿 정밀 매칭 (사이트 + 키)
    for site in URL_TEMPLATES:
        key = _extract_key(site, text)
        if key:
            return site, key
    # 2) 도메인 폴백 (사이트만)
    low = text.lower()
    best: tuple[str, str] | None = None
    for site, patterns in SITE_PATTERNS.items():
        for pattern in patterns:
            if pattern in low and (best is None or len(pattern) > len(best[1])):
                best = (site, pattern)
    if best:
        return best[0], None
    return None, None


def resolve(url) -> dict:
    site, key = url_to_site_key(url)
    return {"site": site, "key": key}


def build(site, key) -> dict:
    return {"url": make_url(site, key)}
