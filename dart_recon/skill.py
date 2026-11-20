"""skills/dart-recon/SKILL.<버전>.md 중 하나를 활성 스킬로 복사한다.

    python3 -m dart_recon.skill use v0     # SKILL.md와 .claude/skills/dart-recon/SKILL.md 갱신, CURRENT 기록
    python3 -m dart_recon.skill sync       # CURRENT가 가리키는 버전을 다시 복사
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "skills" / "dart-recon"
TARGETS = (SRC / "SKILL.md", ROOT / ".claude" / "skills" / "dart-recon" / "SKILL.md")


def use(version: str) -> None:
    src = SRC / f"SKILL.{version}.md"
    if not src.exists():
        raise SystemExit(f"없는 버전: {src.name}")
    (SRC / "CURRENT").write_text(version + "\n")
    for dst in TARGETS:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def main(argv: list[str]) -> None:
    if len(argv) == 2 and argv[0] == "use":
        use(argv[1])
    elif argv == ["sync"]:
        use((SRC / "CURRENT").read_text().strip())
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
