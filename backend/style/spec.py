from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Palette(BaseModel):
    bg: str = "#FFFFFF"
    surface: str = "#FFFFFF"
    text: str = "#111318"
    text_muted: str = "#667085"
    primary: str = "#2E63EB"
    accent: str = "#F59E0B"
    line: str = "#E5E7EB"


class Typography(BaseModel):
    title: list[str] = Field(default_factory=lambda: ["Arial", "Noto Sans SC"])
    body: list[str] = Field(default_factory=lambda: ["Arial", "Noto Sans SC"])
    caption: list[str] = Field(default_factory=lambda: ["Arial", "Noto Sans SC"])
    license: Literal["system", "OFL", "commercial"] = "system"


class Grid(BaseModel):
    canvas: str = "1920x1080"
    align: Literal["symmetric", "asymmetric", "left"] = "symmetric"
    margin: int = 96
    whitespace: tuple[float, float] = (0.2, 0.45)


class StyleSpec(BaseModel):
    id: str
    name: str
    preset: str
    intensity: float = Field(default=0.7, ge=0, le=1)
    scenes: list[str] = Field(default_factory=list)
    palette: Palette = Field(default_factory=Palette)
    typography: Typography = Field(default_factory=Typography)
    grid: Grid = Field(default_factory=Grid)
    layouts: list[str] = Field(default_factory=list)
    motifs: list[str] = Field(default_factory=list)
    imagery: dict = Field(default_factory=dict)
    tone: dict = Field(default_factory=dict)
    rules: dict[str, list[str]] = Field(default_factory=lambda: {"must": [], "must_not": []})
    export_hints: dict = Field(default_factory=dict)

    def to_design_md(self) -> str:
        must = "\n".join(f"- {item}" for item in self.rules.get("must", [])) or "- 无"
        must_not = "\n".join(f"- {item}" for item in self.rules.get("must_not", [])) or "- 无"
        return f"""# {self.name}\n\n- ID: `{self.id}`\n- 预设: `{self.preset}`\n- 适用场景: {', '.join(self.scenes) or '通用'}\n- 风格强度: {self.intensity}\n\n## 色彩\n\n- 背景: `{self.palette.bg}`\n- 正文: `{self.palette.text}`\n- 主色: `{self.palette.primary}`\n- 强调色: `{self.palette.accent}`\n\n## 栅格\n\n- 画布: `{self.grid.canvas}`\n- 对齐: `{self.grid.align}`\n- 边距: `{self.grid.margin}px`\n\n## 必须满足\n\n{must}\n\n## 禁止项\n\n{must_not}\n"""
