import json
import os
import tomllib
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from anyio import BrokenResourceError
from jsonschema import validate
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED
from pydantic import BaseModel
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from backend.app.config import Settings
from backend.harness.metrics import measure, record_operation
from backend.tools.llm import ConfigurationError, Models


class ToolCall(BaseModel):
    name: str
    arguments: dict


class MCPToolError(RuntimeError):
    pass


def transport_failure(error):
    if isinstance(error, BaseExceptionGroup):
        return all(transport_failure(item) for item in error.exceptions)
    if isinstance(error, McpError):
        return error.error.code in {httpx.codes.REQUEST_TIMEOUT, CONNECTION_CLOSED}
    return isinstance(error, (httpx.TransportError, BrokenResourceError))


def search_unavailable(error):
    if isinstance(error, BaseExceptionGroup):
        return all(search_unavailable(item) for item in error.exceptions)
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {429, 502, 503, 504}
    return isinstance(error, MCPToolError) or transport_failure(error)


network_retry = retry(
    retry=retry_if_exception(transport_failure),
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=4),
    reraise=True,
)


class MCPProvider:
    """Discover capabilities and schema before choosing any tool name or arguments."""

    def __init__(self, config: Settings, models: Models):
        self.config, self.models = config, models
        self.http_clients = {}

    async def aclose(self):
        for client in self.http_clients.values():
            await client.aclose()
        self.http_clients.clear()

    @asynccontextmanager
    async def session(self, server: str):
        path = self.config.mcp_config_path.expanduser()
        if not path.exists():
            raise ConfigurationError("找不到 MCP 配置，请设置 MCP_CONFIG_PATH")
        conf = tomllib.loads(path.read_text()).get("mcp_servers", {}).get(server)
        if not conf:
            raise ConfigurationError(f"MCP 服务未配置：{server}")
        if "url" in conf:
            headers = dict(conf.get("http_headers", {}))
            for name, env_name in conf.get("env_http_headers", {}).items():
                if os.environ.get(env_name):
                    headers[name] = os.environ[env_name]
            token_env = conf.get("bearer_token_env_var")
            if token_env and os.environ.get(token_env):
                headers["Authorization"] = "Bearer " + os.environ[token_env]
            key = (conf["url"], tuple(sorted(headers.items())))
            if key not in self.http_clients:
                self.http_clients[key] = httpx.AsyncClient(
                    headers=headers, timeout=httpx.Timeout(30, read=300),
                    limits=httpx.Limits(keepalive_expiry=60),
                )
            transport = streamable_http_client(conf["url"], http_client=self.http_clients[key])
        else:
            transport = stdio_client(
                StdioServerParameters(
                    command=conf["command"],
                    args=conf.get("args", []),
                    env={**os.environ, **conf.get("env", {})},
                )
            )
        async with transport as streams:
            async with ClientSession(
                streams[0], streams[1], read_timeout_seconds=timedelta(seconds=90)
            ) as session:
                await session.initialize()
                yield session

    @network_retry
    async def discover(self, server: str):
        async with self.session(server) as session:
            response = await session.list_tools()
            return [
                {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                for t in response.tools
            ]

    async def call(self, server: str, request: str, *, search_query: str | None = None):
        with measure("调用资料工具", "mcp"):
            try:
                return await self._call(server, request, search_query=search_query)
            except BaseExceptionGroup as error:
                from backend.tools.fetch import recoverable_mcp_fetch_error

                if recoverable_mcp_fetch_error(error):
                    raise MCPToolError("MCP 资料服务连续失败（连接中断或超时），请检查 MCP 服务配置与网络后重试") from error
                raise

    async def fetch_json(self, url):
        try:
            return await self._fetch_json(url)
        except BaseExceptionGroup as error:
            from backend.tools.fetch import recoverable_mcp_fetch_error

            if recoverable_mcp_fetch_error(error):
                raise MCPToolError("远程MCP API抓取不可用，详情见底层连接诊断") from error
            raise

    async def _fetch_json(self, url):
        """Read public API JSON through the discovered remote crawler when local access fails."""
        from backend.tools.fetch import public_url

        await public_url(url, proxy_fake_ip=self.config.fetch_proxy_fake_ip)
        async with self.session(self.config.mcp_search_server) as session:
            listed = (await session.list_tools()).tools
            tool = next(
                (
                    t
                    for t in listed
                    if "urls" in t.inputSchema.get("properties", {})
                    and "maxCharacters" in t.inputSchema.get("properties", {})
                ),
                None,
            )
            if not tool:
                raise MCPToolError("已连接的MCP没有可读取API正文的抓取工具")
            args = {"urls": [url], "maxCharacters": 150000}
            validate(args, tool.inputSchema)
            result = await session.call_tool(tool.name, args)
            text = "\n".join(b.text for b in result.content if b.type == "text")
            if result.isError or "{" not in text:
                raise MCPToolError("远程MCP未返回可读取的API JSON")
            try:
                value, _ = json.JSONDecoder().raw_decode(text[text.index("{") :])
            except ValueError as error:
                raise MCPToolError("远程API JSON不完整，不能作为已获取的数据") from error
            if not isinstance(value, dict):
                raise MCPToolError("远程API没有返回JSON对象")
            return value

    @network_retry
    async def _call(self, server: str, request: str, *, search_query: str | None = None):
        async with self.session(server) as session:
            response = await session.list_tools()
            tools = {tool.name: tool for tool in response.tools}
            try:
                selection = await self.models.json(
                    "选择一个只读搜索或网页抓取工具完成请求，按照该工具实际 inputSchema 构造参数。"
                    "不得选择写入、删除或执行代码类工具。",
                    {"request": request, "tools": [t.model_dump() for t in response.tools]},
                    ToolCall,
                )
            except ValueError as error:
                raise MCPToolError("模型未返回有效的只读工具参数") from error
            if selection.name not in tools:
                raise ValueError("模型选择了未发现的 MCP 工具")
            if search_query is not None:
                if "query" not in tools[selection.name].inputSchema.get("properties", {}):
                    raise ValueError("所选MCP搜索工具不支持query参数")
                # Tool choice is model-assisted; exact names and domain filters are caller-owned.
                selection.arguments["query"] = search_query
            validate(selection.arguments, tools[selection.name].inputSchema)
            result = await session.call_tool(selection.name, selection.arguments)
            if result.isError:
                raise MCPToolError("MCP 工具调用失败；请检查服务配置和网络")
            return "\n".join(block.text for block in result.content if block.type == "text")


class SearchProvider:
    def __init__(self, mcp: MCPProvider, store):
        self.mcp, self.store = mcp, store

    async def search(self, query: str):
        key = self.store.key("search-exact-v2", query)
        if (value := self.store.cached(key, 7)) is not None:
            record_operation("复用搜索结果", "search", cached=True)
            return value
        value = await self.mcp.call(
            self.mcp.config.mcp_search_server,
            f"搜索网页，优先官方与百科，返回最多 6 条含标题和网址的结果：{query}",
            search_query=query,
        )
        self.store.put_cache(key, value)
        return value
