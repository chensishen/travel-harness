import hashlib
import json
import re
import time
from pathlib import Path

from jsonschema import validate
from PIL import Image

from backend.harness.schemas import ImageRef
from backend.tools.mcp_search import MCPToolError


async def generated_image(mcp, city: str, subject: str, folder: Path, name: str):
    """Generate once; pending calls only poll the existing generation ID."""
    prompt = (
        f"为{city}文旅资料绘制主题为“{subject}”的编辑插画。"
        "横向构图，雅致水彩与扁平设计，青绿色、暖米白，主体清晰，留白适度。"
        "不要文字、标志或水印，不要伪造历史照片或具体文物细节。"
        "这是文化意象插画，不是景点实拍或建筑测绘，避免声称精确还原实景。"
    )
    digest = hashlib.sha256(prompt.encode()).hexdigest()[:12]
    path = (folder / f"{name}-{digest}.png").resolve()
    if not path.exists():
        async with mcp.session(mcp.config.mcp_image_server) as session:
            tools = {t.name: t for t in (await session.list_tools()).tools}
            if not {"generate_image", "generation_result"} <= tools.keys():
                raise MCPToolError("生图 MCP 缺少 generate_image / generation_result 工具")
            args = {"prompt": prompt, "path": str(path), "size": "1536x1024", "n": 1}
            validate(args, tools["generate_image"].inputSchema)
            result = await session.call_tool("generate_image", args)
            deadline = time.monotonic() + 600
            while True:
                text = "\n".join(b.text for b in result.content if b.type == "text")
                if result.isError:
                    # Only expose the model configuration failure, never arbitrary service text/URLs.
                    if "knows no model" in text:
                        raise MCPToolError(
                            "生图模型未配置或不存在，请在 Magpie Settings → Images 中选择可用模型"
                        )
                    raise MCPToolError("MCP 生图失败，请检查 Magpie 生图模型、额度与连接")
                if path.exists():
                    break
                payload = getattr(result, "structuredContent", None)
                if not isinstance(payload, dict):
                    try:
                        payload = json.loads(text)
                    except (ValueError, TypeError):
                        payload = {}
                generation_id = payload.get("id")
                if not generation_id:
                    match = re.search(r'\bid(?:\s*[:=]\s*|\s+)["\x27]?([\w-]+)', text, re.I)
                    generation_id = match.group(1) if match else None
                if not generation_id or time.monotonic() >= deadline:
                    raise MCPToolError("生图 MCP 未返回目标文件或有效任务编号，无法取得配图")
                poll = {"id": str(generation_id)}
                validate(poll, tools["generation_result"].inputSchema)
                result = await session.call_tool("generation_result", poll)
    with Image.open(path) as image:
        image.verify()
    return ImageRef(
        url="https://usemagpie.ai",
        source_page="https://usemagpie.ai",
        author="Magpie MCP",
        license="AI 生成插画 · 非景点实拍",
        local_path=str(path),
        generated=True,
    )
