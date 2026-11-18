import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape


def available():
    return bool(shutil.which("soffice") and shutil.which("pdftoppm"))


def render_previews(folder: Path):
    """Convert with an isolated office profile so running desktop Office sessions are untouched."""
    if not available():
        return []
    output = folder / "pdf"
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="travel-office-") as profile:
        font_dir = Path("data/fonts").resolve()
        env = dict(os.environ)
        if font_dir.exists():
            fontconfig = Path(profile) / "fonts.conf"
            fontconfig.write_text(
                '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd">'
                "<fontconfig><dir>"
                + escape(str(font_dir))
                + "</dir><cachedir>"
                + escape(str(Path(profile) / "font-cache"))
                + "</cachedir></fontconfig>"
            )
            env["FONTCONFIG_FILE"] = str(fontconfig)
            env["SAL_FONTPATH"] = str(font_dir)
        def convert(extension, destination):
            destination.mkdir(exist_ok=True)
            pdf = destination / "travel.pdf"
            # A previous conversion must not make an unsuccessful new conversion look valid.
            pdf.unlink(missing_ok=True)
            command = [
                "soffice",
                "-env:UserInstallation=" + Path(profile).as_uri(),
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(destination),
                str(folder / f"travel.{extension}"),
            ]
            result = subprocess.run(command, capture_output=True, timeout=90, check=False, env=env)
            if result.returncode or not pdf.exists():
                raise ValueError(
                    f"LibreOffice {extension} 转换失败，未发布产物；请检查本机 LibreOffice"
                )
        for extension, destination in (("pptx", output), ("docx", folder / "word-pdf")):
            convert(extension, destination)
        from backend.render.toc import update_toc
        for _ in range(3):
            if not update_toc(folder / "travel.docx", folder / "word-pdf/travel.pdf"):
                break
            convert("docx", folder / "word-pdf")
        else:
            raise ValueError("Word 目录页码在实际转换后仍未稳定，未发布产物")
    pdf = output / "travel.pdf"
    for previous in folder.glob("slide-*.png"):
        previous.unlink()
    subprocess.run(
        ["pdftoppm", "-scale-to", "1200", "-png", str(pdf), str(folder / "slide")],
        capture_output=True,
        timeout=90,
        check=True,
    )
    return sorted(folder.glob("slide-*.png"))
