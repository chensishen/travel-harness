"""Extract age claims for arithmetic checking; missing context is not filled from memory."""

import re

from pydantic import BaseModel, Field


class AgeClaim(BaseModel):
    block_id: str
    person: str
    age: int = Field(ge=0, le=150)
    birth_year: int | None = None
    event_year: int | None = None
    source_ids: list[str]


class AgeClaims(BaseModel):
    claims: list[AgeClaim]


def age_consistent(birth_year, event_year, age):
    return birth_year is not None and event_year is not None and age in {event_year-birth_year-1, event_year-birth_year, event_year-birth_year+1}


async def age_issues(models, blocks, sources):
    targets = {key:b for key,b in blocks.items() if re.search(r"\d{1,3}岁", b.text)}
    if not targets:
        return []
    result = await models.json(
        "逐条提取所有目标段落的具体人物年龄。每段至少一条claim，不能漏项。"
        "birth_year和event_year仅来自引用资料，找不到写null，禁止从记忆补足。"
        "同一人物的不同名号要统一识别。source_ids必须对应支持生年或事件年的实际资料。",
        {"blocks":{k:b.model_dump() for k,b in targets.items()},"sources":[s.model_dump(mode="json") for s in sources]}, AgeClaims,
    )
    if set(targets) != {c.block_id for c in result.claims}:
        raise ValueError("年龄审阅遗漏段落，未出件")
    by_id = {s.id:s for s in sources}
    issues = []
    for claim in result.claims:
        evidence = "\n".join(by_id[r].text for r in claim.source_ids if r in by_id)
        numbers = re.findall(r"\d{4}", evidence)
        supported = claim.birth_year is not None and claim.event_year is not None and str(claim.birth_year) in numbers and str(claim.event_year) in numbers
        if not supported or not age_consistent(claim.birth_year, claim.event_year, claim.age):
            issues.append({"block_id":claim.block_id,"reason":f"{claim.person}的具体年龄与生年、事件年份不自洽或缺少可回查年份，删除无法核实的年龄数字。"})
    return issues
