"""M0 capability probes. Never print keys, credentials or credential-bearing URLs."""

import asyncio
import base64
import json
import platform
import sys
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.config import settings  # noqa: E402
from backend.app.store import Store  # noqa: E402
from backend.harness.graph import safe_error  # noqa: E402
from backend.kb.ingest import KnowledgeBase  # noqa: E402
from backend.tools.llm import ConfigurationError, Models  # noqa: E402
from backend.tools.mcp_search import MCPProvider  # noqa: E402


class Probe(BaseModel):
    ok: bool


async def main():
    config = settings()
    models = Models(config)
    provider = MCPProvider(config, models)
    results = []

    async def probe(name, action, optional=False):
        try:
            detail = await asyncio.wait_for(action(), timeout=100)
            results.append({"name": name, "status": "pass", "detail": detail, "optional": optional})
        except Exception as error:
            results.append(
                {
                    "name": name,
                    "status": "blocked" if isinstance(error, ConfigurationError) else "fail",
                    "detail": safe_error(error, config),
                    "optional": optional,
                }
            )
        print(f"{results[-1]['status']:7} {name}: {results[-1]['detail']}", flush=True)

    async def python():
        if platform.python_version() != "3.12.11":
            raise ValueError("请使用 Python 3.12.11")
        return platform.python_version()

    async def llm():
        answer = await models.json('输出 {"ok": true}', {}, Probe)
        if not answer.ok:
            raise ValueError("LLM 结构化输出内容不符合要求")
        return "文本与 JSON Schema 通过"

    async def embedding():
        vectors = await models.embed(["文旅研究连通性测试"])
        if not vectors or not vectors[0]:
            raise ValueError("Embedding 返回空向量")
        kb = KnowledgeBase(config, models)
        await asyncio.to_thread(kb.ensure, len(vectors[0]))
        return {"dimension": len(vectors[0]), "collection": "travel_kb"}

    async def rerank():
        result = await models.rerank("园林", ["江南园林", "高山滑雪"])
        if not result:
            raise ValueError("Reranker 返回空结果")
        return "精排返回有效结果"

    async def image_embedding():
        buffer = BytesIO()
        Image.new("RGB", (64, 64), "red").save(buffer, format="PNG")
        data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
        result = await models.post(
            config.embed_base_url,
            "/embeddings",
            config.siliconflow_api_key.get_secret_value(),
            {"model": config.embed_model, "input": {"image": data_url}},
        )
        vector = result["data"][0]["embedding"]
        if not vector:
            raise ValueError("图片 Embedding 返回空向量")
        return {"dimension": len(vector), "input": "64x64 PNG base64"}

    async def discover():
        tools = await provider.discover(config.mcp_search_server)
        if not tools:
            raise ValueError("搜索服务没有返回任何工具")
        docs = Path("docs/mcp_tools.md")
        docs.write_text(
            "# MCP 工具发现记录\n\n由 scripts/smoke.py 实际 tools/list 生成。仅含工具元数据，不含配置或密钥。\n\n```json\n"
            + json.dumps(tools, ensure_ascii=False, indent=2)
            + "\n```\n"
        )
        return [t["name"] for t in tools]

    async def fetch_discover():
        tools = await provider.discover(config.mcp_search_server)
        fetchers = [t["name"] for t in tools if "fetch" in t["name"].lower()]
        if fetchers:
            return fetchers
        tools = await provider.discover(config.mcp_fetch_server)
        return [t["name"] for t in tools]

    async def vision():
        buffer = BytesIO()
        Image.new("RGB", (64, 64), "red").save(buffer, format="PNG")
        data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
        result = await models.post(
            config.llm_base_url,
            "/chat/completions",
            config.llm_api_key.get_secret_value(),
            {
                "model": config.llm_model,
                "temperature": 0.2,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "这张图片主要是什么颜色？"},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
            },
        )
        text = result["choices"][0]["message"]["content"]
        if "红" not in text and "red" not in text.lower():
            raise ValueError("图片颜色识别未通过；使用文本元数据核验降级模式")
        return "图片输入与颜色识别通过"

    async def url_check(url, params=None):
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.get(
                url, params=params, headers={"User-Agent": config.http_user_agent}
            )
            response.raise_for_status()
            return {"http_status": response.status_code}

    await probe("Python", python)
    await probe("LLM text + JSON", llm)
    await probe("Embedding + Milvus dimension", embedding)
    await probe("Embedding image input", image_embedding, True)
    await probe("Reranker", rerank)
    await probe("MCP search tools/list", discover)
    await probe("MCP fetch tools/list (HTTP fallback available)", fetch_discover, True)
    await probe("LLM vision (metadata fallback available)", vision, True)
    await probe(
        "Nominatim",
        lambda: url_check(
            config.nominatim_base_url + "/search", {"q": "上海", "format": "json", "limit": 1}
        ),
    )
    await probe(
        "OSRM",
        lambda: url_check(config.osrm_base_url + "/route/v1/driving/121.47,31.23;121.48,31.24"),
    )
    await models.aclose()
    root = Store(config.data_dir).root
    (root / "smoke.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(r["status"] == "pass" for r in results if not r["optional"]) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
