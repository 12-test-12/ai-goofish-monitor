# Build after `npm run build` in web-ui. Bundle only public default resources.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH)
if not (root / "dist" / "index.html").is_file():
    raise RuntimeError("Build web-ui before packaging the desktop executable")

analysis = Analysis(
    [str(root / "desktop_launcher.py")], pathex=[str(root)],
    datas=[(str(root / "dist"), "dist"),
           (str(root / "prompts" / "base_prompt.txt"), "prompts"),
           (str(root / "prompts" / "macbook_criteria.txt"), "prompts"),
           (str(root / ".env.example"), ".")],
    hiddenimports=["src.app", "spider_v2", *collect_submodules("uvicorn")],
    excludes=["pytest", "coverage"],
)
archive = PYZ(analysis.pure)
exe = EXE(archive, analysis.scripts, analysis.binaries, analysis.datas,
          name="GoofishMonitor", console=True)
