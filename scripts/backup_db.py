"""autotrade.db 매일 백업 — SQLite 온라인 백업 API 사용(운영 중에도 안전).
30일 지난 백업은 자동 삭제."""
import glob
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
SRC = ROOT / "autotrade.db"
BACKUP_DIR = ROOT / "backup"
KEEP_DAYS = 30


def main() -> None:
    BACKUP_DIR.mkdir(exist_ok=True)
    dst = BACKUP_DIR / f"autotrade_{time.strftime('%Y%m%d')}.db"

    src_con = sqlite3.connect(str(SRC))
    dst_con = sqlite3.connect(str(dst))
    with dst_con:
        src_con.backup(dst_con)
    src_con.close()
    dst_con.close()
    print(f"백업 완료: {dst}")

    now = time.time()
    for f in glob.glob(str(BACKUP_DIR / "autotrade_*.db")):
        if now - os.path.getmtime(f) > KEEP_DAYS * 86400:
            os.remove(f)
            print(f"오래된 백업 삭제: {f}")


if __name__ == "__main__":
    main()
