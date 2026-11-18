import asyncio
import hashlib
import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura

from backend.harness.metrics import measure, record_operation
from backend.tools.mcp_search import MCPToolError, transport_failure


class RedirectLimitError(ValueError):
    pass


def recoverable_mcp_fetch_error(error):
    if isinstance(error, BaseExceptionGroup):
        return all(recoverable_mcp_fetch_error(item) for item in error.exceptions)
    return isinstance(error, MCPToolError) or transport_failure(error)


async def public_url(url: str, *, proxy_fake_ip: bool = False):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("只允许公开 HTTP(S) 网页")
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError("禁止抓取本机、内网或保留地址")
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or 443)
    except socket.gaierror as error:
        raise httpx.ConnectError("来源域名解析失败：" + parsed.hostname) from error
    fake_range = ipaddress.ip_network("198.18.0.0/15")
    if any(
        not (address := ipaddress.ip_address(item[4][0])).is_global
        and not (proxy_fake_ip and address in fake_range)
        for item in addresses
    ):
        raise ValueError("禁止抓取本机、内网或保留地址")


def damaged_text(text):
    return text.count("\ufffd") > max(10, len(text) * 0.01)


def blocked_page(text):
    return bool(re.search(r"(?m)^\s*(?:#{1,6}\s*)?(?:🧩\s*)?请完成拼图验证\s*$", text[:1000]))


async def same_host_get(client, url, *, proxy_fake_ip, label):
    host = urlparse(url).hostname
    for hop in range(6):
        response = await client.get(url)
        if not response.is_redirect:
            return response
        if hop == 5:
            raise RedirectLimitError(f"{label} 重定向次数超限")
        if not response.headers.get("location"):
            raise ValueError(f"{label} 重定向缺少目标地址")
        target = urljoin(url, response.headers["location"])
        if urlparse(target).hostname != host:
            raise ValueError(f"{label} 不允许跨域重定向")
        await public_url(target, proxy_fake_ip=proxy_fake_ip)
        url = target


def missing_time_cards(text):
    return (
        all(re.search(rf"(?:^|\n)\s*{label}\s*(?:\n|$)", text) for label in ("开馆", "闭馆"))
        and len(re.findall(r"\d{1,2}[:：]\d{2}", text)) < 2
    )


def preserve_time_cards(content, text):
    tree = trafilatura.load_html(content)
    if tree is None:
        return text
    panels = {}
    for label in tree.xpath("//*[not(*)]"):
        if label.tag in {"script", "style", "template"} or label.text not in {
            "开馆",
            "闭馆",
            "开园",
            "闭园",
            "停止入馆",
        }:
            continue
        if label.xpath(
            "ancestor-or-self::*[@hidden or @aria-hidden='true' or contains(translate(@style, ' ', ''), 'display:none')]"
        ):
            continue
        for parent in list(label.iterancestors())[:2]:
            line = " ".join(" ".join(parent.itertext()).split())
            if len(line) <= 150 and re.search(r"\d{1,2}[:：]\d{2}", line):
                panel = parent.getparent()
                lines = panels.setdefault(panel, [])
                if line not in lines:
                    lines.append(line)
                break
    compact = "".join(text.split())
    for lines in panels.values():
        panel = "；".join(lines)
        if any(word in panel for word in ("开馆", "开园")) and any(
            word in panel for word in ("闭馆", "闭园")
        ):
            if any("".join(line.split()) not in compact for line in lines):
                text += "\n\n" + panel
    return text


def preserve_publication(content, text):
    from datetime import date

    tree = trafilatura.load_html(content)
    if tree is None:
        return text
    keys = {
        "article:published_time",
        "og:published_time",
        "pubdate",
        "publishdate",
        "publish_date",
        "datepublished",
        "dc.date.issued",
    }
    for meta in tree.xpath("//meta[@content]"):
        name = (meta.get("property") or meta.get("name") or meta.get("itemprop") or "").lower()
        if name not in keys:
            continue
        value = meta.get("content", "")
        match = re.match(r"(20\d{2})-(\d{2})-(\d{2})", value)
        if match:
            try:
                published = date(*map(int, match.groups()))
            except ValueError:
                continue
            return f"来源发布日期：{published.isoformat()}（网页公开发布日期元数据）\n\n" + text
    return text


class FetchProvider:
    def __init__(self, config, store, mcp=None):
        self.config, self.store = config, store
        self.mcp = mcp
        self.fetch_server = None
        self.discovered = False
        self.locks = {}
        self.last = {}
        self.limit = asyncio.Semaphore(config.fetch_concurrency)
        self.discovery_lock = asyncio.Lock()

    def cached_page(self, key):
        text = self.store.cached(key)
        if text is None or damaged_text(text) or blocked_page(text):
            return None
        if (
            missing_time_cards(text)
            and self.store.cached(key + ":cards-v1") != hashlib.sha256(text.encode()).hexdigest()
        ):
            return None
        return text

    async def fetch(self, url: str):
        with measure("读取网页", "fetch"):
            return await self._fetch(url)

    async def _fetch(self, url: str):
        key = self.store.key("fetch", url)
        if (cached := self.cached_page(key)) is not None:
            record_operation("复用网页正文", "fetch", cached=True)
            return cached
        await public_url(url, proxy_fake_ip=self.config.fetch_proxy_fake_ip)
        host = urlparse(url).netloc.lower()
        lock = self.locks.setdefault(host, asyncio.Lock())
        async with lock, self.limit:
            if (cached := self.cached_page(key)) is not None:
                record_operation("复用网页正文", "fetch", cached=True)
                return cached
            await asyncio.sleep(max(0, 1 - (time.monotonic() - self.last.get(host, 0))))
            try:
                async with httpx.AsyncClient(
                    timeout=30,
                    follow_redirects=False,
                    headers={"User-Agent": self.config.http_user_agent},
                ) as client:
                    origin = urlparse(url)
                    robots_url = f"{origin.scheme}://{origin.netloc}/robots.txt"
                    robots_key = self.store.key("robots", robots_url)
                    rules = self.store.cached(robots_key, 1)
                    if rules is None:
                        try:
                            response = await same_host_get(
                                client,
                                robots_url,
                                proxy_fake_ip=self.config.fetch_proxy_fake_ip,
                                label="robots.txt",
                            )
                        except RedirectLimitError:
                            # RFC 9309 §2.3.1.2 permits treating >5 redirects as unavailable.
                            rules = ""
                            record_operation("robots.txt 重定向超限，按不可用处理", "fetch")
                        else:
                            if response.status_code == 404:
                                rules = ""
                            else:
                                response.raise_for_status()
                                rules = response.text
                        self.store.put_cache(robots_key, rules)
                    parser = RobotFileParser()
                    parser.parse(rules.splitlines())
                    if not parser.can_fetch(self.config.http_user_agent, url):
                        raise ValueError("目标网站 robots.txt 不允许抓取")
                    previous = self.store.cached(key)
                    repair_cards = previous is not None and missing_time_cards(previous)
                    if self.mcp and not repair_cards:
                        async with self.discovery_lock:
                            if not self.discovered:
                                for server in dict.fromkeys(
                                    [self.config.mcp_fetch_server, self.config.mcp_search_server]
                                ):
                                    try:
                                        tools = await self.mcp.discover(server)
                                    except Exception as error:
                                        if not recoverable_mcp_fetch_error(error):
                                            raise
                                        continue
                                    if any(
                                        "fetch" in tool["name"].lower()
                                        or "read a webpage"
                                        in (tool.get("description") or "").lower()
                                        for tool in tools
                                    ):
                                        self.fetch_server = server
                                        break
                                self.discovered = True
                        if self.fetch_server:
                            try:
                                text = await self.mcp.call(
                                    self.fetch_server,
                                    f"读取指定网页完整正文，返回最多20000字符，使用网页抓取工具而非搜索：{url}",
                                )
                            except Exception as error:
                                if not recoverable_mcp_fetch_error(error):
                                    raise
                                # The checked public URL can still be read by the HTTP path below.
                            else:
                                if not text.strip():
                                    raise ValueError("MCP 网页抓取没有返回正文")
                                if blocked_page(text):
                                    raise ValueError("来源返回验证页面，未用作资料")
                                if not missing_time_cards(text):
                                    self.store.put_cache(key, text)
                                    return text
                    response = await same_host_get(
                        client, url, proxy_fake_ip=self.config.fetch_proxy_fake_ip, label="网页"
                    )
                    response.raise_for_status()
                    if len(response.content) > 5_000_000:
                        raise ValueError("网页超过 5MB 抓取上限")
                    if response.content.startswith(b"%PDF-"):
                        from io import BytesIO

                        from pypdf import PdfReader
                        from pypdf.errors import PdfReadError

                        try:
                            reader = PdfReader(BytesIO(response.content))
                            text = "\n".join(page.extract_text() for page in reader.pages[:40])[
                                :20000
                            ]
                        except PdfReadError as error:
                            raise ValueError("官方PDF正文无法解析") from error
                        if not text.strip() or damaged_text(text):
                            raise ValueError("PDF没有可提取正文，需要人工核实")
                        self.store.put_cache(key, text)
                        return text
                    text = preserve_time_cards(
                        response.content,
                        trafilatura.extract(response.content, include_tables=True) or "",
                    )
                    if not text or damaged_text(text):
                        raise ValueError("网页没有可用正文或存在编码损坏")
                    text = preserve_publication(response.content, text)
                    metadata = trafilatura.extract_metadata(response.content)
                    if metadata and metadata.title:
                        text = "# " + metadata.title.strip() + "\n\n" + text
                    if blocked_page(text):
                        raise ValueError("来源返回验证页面，未用作资料")
                    self.store.put_cache(key, text)
                    if missing_time_cards(text):
                        # Raw HTML was checked; do not repeatedly refetch a genuine empty panel.
                        self.store.put_cache(
                            key + ":cards-v1", hashlib.sha256(text.encode()).hexdigest()
                        )
                    return text
            finally:
                self.last[host] = time.monotonic()
