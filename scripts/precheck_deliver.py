#!/usr/bin/env python3
"""交付预检（零残留三查 + 快照 + 临时目录清理的代码化）。

Usage:
    python3 -m scripts.precheck_deliver <技能源库路径> [--deploy <部署副本路径>] [--snapshot-to <工作区>]

四项红绿输出，任一红即 exit 6：
1. 死资产扫描：scripts/references/templates/assets 下每个文件按文件名全库 grep，
   零引用且无目录级消费 → 红（孤儿文件）。
2. 幽灵路径：SKILL.md 与 references 里引用的仓库内路径（references/…、scripts/…、
   templates/…、assets/…）须真实存在 → 不存在即红。
3. 副本同步：--deploy 给出时对源库与部署副本做内容 diff → 有差异即红。
4. quick_validate --strict：通过为绿。
另：--snapshot-to <工作区> 顺带把技能 cp 成工作区 skill-snapshot/（改进现有技能的旧版留档，供「7. 盲比较」用）。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
DIRS = ("scripts", "references", "templates", "assets")


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-delivery checks (zero-residue trio + strict validate)")
    parser.add_argument("skill_path", type=Path, help="源库技能路径")
    parser.add_argument("--deploy", type=Path, default=None, help="部署副本路径（默认 ~/.hermes/skills/<name>）")
    parser.add_argument("--snapshot-to", type=Path, default=None, help="工作区路径：cp 成 <工作区>/skill-snapshot/")
    args = parser.parse_args()

    src = args.skill_path.resolve()
    name = src.name
    deploy = (args.deploy or (Path.home() / ".hermes" / "skills" / name)).resolve()
    reds: list[str] = []

    # 1) 死资产扫描
    texts: dict[Path, str] = {}
    for p in sorted(src.rglob("*")):
        if p.is_file() and p.suffix in (".md", ".py", ".html", ".txt", ".json", ".yaml", ".yml"):
            try:
                texts[p] = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                pass
    all_text = "\n".join(texts.values())
    orphans = []
    for p in texts:
        rel = p.relative_to(src)
        if rel.name in ("SKILL.md", "__init__.py"):
            continue
        # 模块引用形式：scripts/ 子树内的 .py 按点路径（scripts.x、scripts.tests.x）
        stem_ref = None
        if rel.parts[0] == "scripts" and rel.suffix == ".py":
            stem_ref = ".".join(rel.with_suffix("").parts)
        # 文件名在全库他处出现，或以模块路径形式被调用/import → 有消费方
        others = [t for p2, t in texts.items() if p2 != p and (rel.name in t or (stem_ref and stem_ref in t))]
        if not others:
            orphans.append(str(rel))
    if orphans:
        reds.append("死资产（零引用孤儿文件）: " + ", ".join(orphans))

    # 2) 幽灵路径（左边界锚定：引用前不得是路径字符/冒号斜杠——排除 URL 内子串）
    ghosts = []
    for p in [src / "SKILL.md", *sorted((src / "references").glob("*.md"))]:
        if not p.exists():
            continue
        for m in re.finditer(
                r"(?<![\w\-./:])((?:references|scripts|templates|assets)/[\w\-./]*\w[\w\-./]*\.[\w]+)",
                p.read_text(encoding="utf-8", errors="ignore")):
            ref = m.group(1)
            if not (src / ref).exists():
                ghosts.append(f"{p.name} → {ref}")
    if ghosts:
        reds.append("幽灵路径: " + "; ".join(ghosts))

    # 3) 副本同步（白名单模式：只比技能应有文件，__pycache__/.DS_Store 等环境噪音天然免疫）
    if deploy.exists():
        expected = sorted(
            p.relative_to(src) for p in src.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.name != ".DS_Store"
        )
        mismatch = []
        for rel in expected:
            if not (deploy / rel).is_file():
                mismatch.append(f"仅源库有: {rel}")
            elif (src / rel).read_bytes() != (deploy / rel).read_bytes():
                mismatch.append(f"内容不同: {rel}")
        extra = sorted(
            p.relative_to(deploy) for p in deploy.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.name != ".DS_Store"
            and (src / p.relative_to(deploy)).exists() is False
        )
        mismatch += [f"仅部署副本有: {rel}" for rel in extra]
        if mismatch:
            reds.append(f"副本不同步（{src} vs {deploy}）:\n    " + "\n    ".join(mismatch[:6]))
    else:
        print(f"  ? 部署副本 {deploy} 不存在，跳过同步检查")

    # 4) strict 校验
    v = subprocess.run([sys.executable, "-m", "scripts.quick_validate", str(src), "--strict"],
                       capture_output=True, text=True, cwd=_REPO_ROOT)
    if v.returncode != 0:
        detail = (v.stdout + "\n" + v.stderr).strip()
        reds.append("strict 校验未通过:\n    " + "\n    ".join(detail.splitlines()))

    # 快照（可选）
    if args.snapshot_to:
        snap = args.snapshot_to / "skill-snapshot"
        if snap.exists():
            shutil.rmtree(snap)
        shutil.copytree(src, snap)
        print(f"  ✓ 快照已建: {snap}")

    if reds:
        print("交付预检未通过：", file=sys.stderr)
        for r in reds:
            print(f"  ✗ {r}", file=sys.stderr)
        sys.exit(6)
    print("交付预检通过：死资产 0、幽灵路径 0、副本一致、strict 校验绿。")


if __name__ == "__main__":
    main()
