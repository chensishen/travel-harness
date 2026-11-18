"""Licensed real photographs, with embedding + visual checks before inclusion."""

import hashlib
import html
import io
import json
import math
import re

import httpx
from PIL import Image

from backend.harness.schemas import ImageRef
from backend.tools.fetch import public_url


def plain(value):
    return html.unescape(re.sub(r"<[^>]*>", "", value)).strip()


def phash(path):
    image = Image.open(path).convert("L").resize((32, 32))
    pixels = list(image.getdata())
    cosine = [[math.cos((2 * x + 1) * u * math.pi / 64) for x in range(32)] for u in range(8)]
    rows = [
        [sum(pixels[y * 32 + x] * cosine[u][x] for x in range(32)) for u in range(8)]
        for y in range(32)
    ]
    values = [sum(rows[y][u] * cosine[v][y] for y in range(32)) for v in range(8) for u in range(8)]
    middle = sorted(values[1:])[31]
    return f"{sum((v > middle) << i for i, v in enumerate(values)):016x}"


def license_allowed(value):
    return bool(
        re.fullmatch(
            r"(?:CC BY(?:-SA)? ?(?:[1-4]\.0)?|CC0(?: 1\.0)?|Public domain|PDM)", value, re.I
        )
    )


def subject_supported(candidate):
    if candidate.get("subject_evidence") == "wikidata_p18":
        return True

    city = re.sub(r"[\W_]+", "", candidate.get('subject_city', '').casefold()).removesuffix('市')

    def normalized(value):
        value = re.sub(r"[\W_]+", "", value.casefold())
        return value.replace(city + '市', city) if len(city) >= 2 else value

    metadata = normalized(
        " ".join(candidate.get(k, "") for k in ["title", "description", "categories"])
    )
    return any(
        len(key := normalized(name)) >= 4 and key in metadata
        for name in candidate.get("subject_names", [])
    )


def wrong_branch(candidate, poi):
    target = poi.name + ' ' + ' '.join(f.value for f in poi.facts if f.field == 'address')
    title = candidate.get('title', '').casefold()
    if '人民广场' in target or '人民大道201' in target.replace(' ', '') or '/visit-west' in poi.official_url:
        return bool(re.search(r'东馆|east[_ ]*museum|museum[_ ]*east', title))
    if '东馆' in poi.name:
        return '人民广场馆' in title or "people's square" in title
    return False


class Photos:
    def __init__(self, config, store, models):
        self.config, self.store, self.models = config, store, models
        self.hashes = []

    async def api(self, url, params, provider):
        from urllib.parse import urlencode

        key = self.store.key("image-api-v13", url + "?" + urlencode(params))
        cached = self.store.cached(key, 7)
        if cached is not None:
            self.audit.append(
                {
                    "provider": provider,
                    "status_code": None,
                    "cache_hit": True,
                    "hits": len(cached.get("query", {}).get("pages", {}))
                    or len(cached.get("search", [])),
                }
            )
            return cached
        status = None
        try:
            async with httpx.AsyncClient(
                timeout=15, headers={"User-Agent": self.config.http_user_agent}
            ) as client:
                response = await client.get(url, params=params)
                status = response.status_code
                response.raise_for_status()
                data = response.json()
            transport = "http"
        except (httpx.HTTPError, ValueError) as error:
            self.audit.append(
                {
                    "provider": provider,
                    "status_code": status,
                    "hits": 0,
                    "error": type(error).__name__,
                }
            )
            if not getattr(self, "mcp", None):
                raise
            data = await self.mcp.fetch_json(url + "?" + urlencode(params))
            transport = "mcp"
        self.audit.append(
            {
                "provider": provider,
                "status_code": status if transport == "http" else None,
                "transport": transport,
                "hits": len(data.get("query", {}).get("pages", {}))
                or len(data.get("entities", {}))
                or len(data.get("results", []))
                or len(data.get("search", [])),
            }
        )
        self.store.put_cache(key, data)
        return data

    @staticmethod
    def commons_rows(data):
        rows = []
        for page in data.get("query", {}).get("pages", {}).values():
            for info in page.get("imageinfo", []):
                meta = info.get("extmetadata", {})
                rows.append(
                    {
                        "url": info.get("thumburl", info["url"]),
                        "page": info["descriptionurl"],
                        "author": plain(meta.get("Artist", {}).get("value", "")),
                        "license": plain(meta.get("LicenseShortName", {}).get("value", "")),
                        "license_url": plain(meta.get("LicenseUrl", {}).get("value", "")),
                        "title": page.get("title", ""),
                        "description": plain(meta.get("ImageDescription", {}).get("value", "")),
                        "categories": plain(meta.get("Categories", {}).get("value", ""))
                        + " "
                        + " ".join(x.get("title", "") for x in page.get("categories", [])),
                    }
                )
        return rows

    async def search(self, city, name, poi=None):
        self.audit = []
        names = [city + " " + name, name, *(poi.aliases if poi else [])]
        commons = "https://commons.wikimedia.org/w/api.php"
        base = {
            "action": "query",
            "format": "json",
            "prop": "imageinfo|categories",
            "cllimit": 50,
            "iiprop": "url|extmetadata|size",
            "iiurlwidth": 1600,
            "iiextmetadatalanguage": "zh",
        }
        if poi and not poi.wikidata:
            try:
                term = name
                for term in [name, *poi.aliases][:3]:
                    result = await self.api(
                        "https://www.wikidata.org/w/api.php",
                        {
                            "action": "wbsearchentities",
                            "format": "json",
                            "search": term,
                            "language": "zh",
                            "limit": 5,
                        },
                        "Wikidata names",
                    )
                    if result.get("search"):
                        break
                matches = []
                if result.get("search"):
                    from pydantic import BaseModel

                    class SelectedEntity(BaseModel):
                        id: str = ""

                    selection = await self.models.json(
                        "仅从给定Wikidata检索结果中选择目标城市的实际景点实体ID；同名论文、藏品、名录不是场馆。按坐标区分分馆及同名异地地点；城市中文与英语名称可对应。不能确认则返回空id，不得编造ID。",
                        {"city": city, "poi": name, "lat": poi.lat, "lng": poi.lng, "results": result["search"]},
                        SelectedEntity,
                    )
                    matches = [
                        x
                        for x in result["search"]
                        if x["id"] == selection.id and term in x.get("match", {}).get("text", "")
                    ]
                if len(matches) == 1:
                    poi.wikidata = matches[0]["id"]
                    names.append(matches[0].get("label", name))
            except (httpx.HTTPError, ValueError, RuntimeError) as error:
                self.audit.append(
                    {"provider": "Wikidata names", "hits": 0, "error": type(error).__name__}
                )
        if poi and poi.wikidata:
            try:
                data = await self.api(
                    "https://www.wikidata.org/w/api.php",
                    {
                        "action": "wbgetentities",
                        "ids": poi.wikidata,
                        "format": "json",
                        "languages": "zh|en",
                        "props": "labels|aliases|claims",
                    },
                    "Wikidata P18",
                )
                entity = data.get("entities", {}).get(poi.wikidata, {})
                names.extend(x["value"] for x in entity.get("labels", {}).values())
                names.extend(
                    x["value"] for group in entity.get("aliases", {}).values() for x in group[:3]
                )
                files = [
                    x["mainsnak"]["datavalue"]["value"]
                    for x in entity.get("claims", {}).get("P18", [])
                    if "datavalue" in x.get("mainsnak", {})
                ]
                if files:
                    rows = self.commons_rows(
                        await self.api(
                            commons,
                            {**base, "titles": "|".join("File:" + x for x in files[:3])},
                            "Commons P18",
                        )
                    )
                    if rows:
                        for row in rows:
                            row["subject_names"] = names.copy()
                            row["subject_evidence"] = "wikidata_p18"
                        yield rows
            except (httpx.HTTPError, ValueError, RuntimeError) as error:
                self.audit.append(
                    {"provider": "Wikidata P18", "hits": 0, "error": type(error).__name__}
                )
        if poi and poi.lat is not None:
            try:
                rows = self.commons_rows(
                    await self.api(
                        commons,
                        {
                            **base,
                            "generator": "geosearch",
                            "ggscoord": f"{poi.lat}|{poi.lng}",
                            "ggsradius": 500,
                            "ggsnamespace": 6,
                            "ggslimit": 8,
                        },
                        "Commons geosearch",
                    )
                )
                if rows:
                    for row in rows:
                        row["subject_names"] = names.copy()
                    yield rows
            except (httpx.HTTPError, ValueError, RuntimeError) as error:
                self.audit.append(
                    {"provider": "Commons geosearch", "hits": 0, "error": type(error).__name__}
                )
        for query in list(dict.fromkeys(names))[:4]:
            for mode in ["text", "category"]:
                try:
                    args = (
                        {
                            "generator": "search",
                            "gsrsearch": f"{query} filetype:bitmap",
                            "gsrnamespace": 6,
                            "gsrlimit": 6,
                        }
                        if mode == "text"
                        else {
                            "generator": "categorymembers",
                            "gcmtitle": "Category:" + query,
                            "gcmnamespace": 6,
                            "gcmlimit": 6,
                        }
                    )
                    rows = self.commons_rows(
                        await self.api(commons, {**base, **args}, "Commons " + mode)
                    )
                    if rows:
                        for row in rows:
                            row["subject_names"] = names.copy()
                        yield rows
                except (httpx.HTTPError, ValueError, RuntimeError) as error:
                    self.audit.append(
                        {
                            "provider": "Commons " + mode,
                            "query": query,
                            "hits": 0,
                            "error": type(error).__name__,
                        }
                    )
        try:
            data = await self.api(
                "https://api.openverse.org/v1/images/",
                {
                    "q": city + " " + name,
                    "page_size": 8,
                    "license": "by,by-sa,cc0,pdm",
                    "mature": "false",
                },
                "Openverse",
            )
            labels = {"by": "CC BY", "by-sa": "CC BY-SA", "cc0": "CC0", "pdm": "Public domain"}
            yield [
                {
                    "url": x["url"],
                    "page": x["foreign_landing_url"],
                    "author": x.get("creator", ""),
                    "license": labels.get(x.get("license"), "")
                    + " "
                    + x.get("license_version", ""),
                    "license_url": x.get("license_url", ""),
                    "title": x.get("title", ""),
                    "description": " ".join(t.get("name", "") for t in x.get("tags", [])),
                    "subject_names": names.copy(),
                }
                for x in data.get("results", [])
            ]
        except (httpx.HTTPError, ValueError, RuntimeError) as error:
            self.audit.append({"provider": "Openverse", "hits": 0, "error": type(error).__name__})
        return

    async def obtain(self, city, poi, folder):
        from urllib.parse import urlparse

        from backend.tools.fetch import same_host_get

        audit, images, seen = [], [], set()
        blocked_hosts = set()
        async for candidates in self.search(city, poi.name, poi):
            for candidate in candidates:
                if candidate["url"] in seen:
                    continue
                seen.add(candidate["url"])
                if wrong_branch(candidate, poi):
                    audit.append({'stage': 'venue_branch', 'page': candidate['page'], 'accepted': False,
                                  'reason': '图片属于其他馆区，不能代替当前参观馆区'})
                    continue
                if not subject_supported({**candidate, 'subject_city': city}):
                    audit.append(
                        {
                            "stage": "subject_metadata",
                            "page": candidate["page"],
                            "accepted": False,
                            "reason": "源页面缺少景点名称或别名关联，不能用周边街景替代",
                            "title": candidate.get("title", ""),
                        }
                    )
                    continue
                host = urlparse(candidate["url"]).hostname
                if host in blocked_hosts:
                    audit.append(
                        {
                            "provider": "image download",
                            "page": candidate["page"],
                            "status_code": None,
                            "hits": 0,
                            "skipped": True,
                            "reason": "同一主机已明确拒绝当前User-Agent，等待配置有效联系方式或代理",
                        }
                    )
                    continue
                if (
                    not candidate["author"]
                    or not license_allowed(candidate["license"])
                    or not candidate.get("license_url")
                ):
                    audit.append({"stage": "license", "page": candidate["page"], "accepted": False})
                    continue
                name = (
                    "photo-" + hashlib.sha256(candidate["url"].encode()).hexdigest()[:16] + ".jpg"
                )
                path = folder / name
                try:
                    await public_url(
                        candidate["url"], proxy_fake_ip=self.config.fetch_proxy_fake_ip
                    )
                    if not path.exists():
                        async with httpx.AsyncClient(
                            timeout=30, headers={"User-Agent": self.config.http_user_agent}
                        ) as client:
                            response = await same_host_get(
                                client,
                                candidate["url"],
                                proxy_fake_ip=self.config.fetch_proxy_fake_ip,
                                label="图片",
                            )
                            if (
                                response.status_code == 403
                                and "user-agent" in response.text[:2000].lower()
                                and "robot policy" in response.text[:2000].lower()
                            ):
                                blocked_hosts.add(host)
                            response.raise_for_status()
                            if len(response.content) > 12_000_000:
                                raise ValueError("候选图片超过下载限额")
                            im = Image.open(io.BytesIO(response.content)).convert("RGB")
                            if max(im.size) < 1200:
                                raise ValueError("候选图片长边不足1200px")
                            im.thumbnail((1920, 1920))
                            im.save(path, quality=90)
                    digest = phash(path)
                    if any((int(digest, 16) ^ int(h, 16)).bit_count() < 8 for h in self.hashes):
                        audit.append({"stage": "duplicate", "page": candidate["page"]})
                        continue
                    cache_key = self.store.key("photo-vl-v13-subject", city + poi.name + digest)
                    checked = self.store.cached(cache_key, 90)
                    if checked is None:
                        valid, score = await self.models.image_relevance(
                            city,
                            poi.name,
                            path,
                            metadata={
                                k: candidate.get(k, "")
                                for k in ["title", "description", "categories"]
                            },
                        )
                        checked = {"valid": valid, "score": score}
                        self.store.put_cache(cache_key, checked)
                    audit.append({"page": candidate["page"], **checked})
                    if not checked["valid"]:
                        continue
                    images.append(
                        ImageRef(
                            url=candidate["url"],
                            source_page=candidate["page"],
                            author=candidate["author"],
                            license=candidate["license"],
                            license_url=candidate["license_url"],
                            local_path=str(path.resolve()),
                            relevance_verified=True,
                            subject_verified=True,
                            similarity=checked["score"],
                            perceptual_hash=digest,
                        )
                    )
                    self.hashes.append(digest)
                    break
                except (httpx.HTTPError, ValueError, OSError) as error:
                    record = {
                        "provider": "image download",
                        "page": candidate["page"],
                        "error": type(error).__name__,
                        "hits": 0,
                        "status_code": error.response.status_code
                        if isinstance(error, httpx.HTTPStatusError)
                        else None,
                    }
                    audit.append(record)
                    self.store.event(
                        folder.name,
                        node="Illustrator",
                        status="running",
                        kind="provider",
                        provider=record,
                        message=f"图片下载：HTTP {record['status_code']}，{record['error']}",
                    )
            if images:
                break
        audit = [*self.audit, *audit]
        (folder / f"photo-review-{poi.id}.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2)
        )
        for record in self.audit:
            self.store.event(
                folder.name,
                node="Illustrator",
                status="running",
                kind="provider",
                provider=record,
                message=f"{record['provider']}：HTTP {record.get('status_code', '—')}，命中{record.get('hits', 0)}；{record.get('error', record.get('transport', ''))}",
            )
        if not images:
            self.store.event(
                folder.name,
                node="Illustrator",
                status="running",
                message=poi.name + " 实拍命中为0，详见photo-review报告；需要上传或人工处理",
                qa_severity="red" if poi.nationally_rated else "warning",
            )
        poi.images = images
        poi.photo_status = "实拍已核对" if images else "暂无合规实拍图"
        return images
