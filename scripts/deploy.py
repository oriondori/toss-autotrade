"""
TossAutoTrade 서버 배포 스크립트
- 변경된 파일만 업로드 (MD5 비교)
- requirements.txt 변경 시 pip install 자동 실행
- 서비스 재시작 후 목표가 확인
"""
import getpass
import hashlib, os, sys, time, stat
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import paramiko
except ImportError:
    print("paramiko 설치 중...")
    os.system(f'"{sys.executable}" -m pip install paramiko -q')
    import paramiko

# ── 서버 설정 (비밀번호는 하드코딩 금지 — 환경변수 없으면 매번 입력받음) ──
HOST     = "211.222.4.174"
PORT     = 2159
USER     = "appadm"
PW       = os.environ.get("TOSS_SERVER_PASSWORD") or getpass.getpass(f"{USER}@{HOST} 서버 비밀번호: ")
REMOTE   = "/opt/toss-autotrade"
SERVICE  = "toss-autotrade"

# ── 동기화 대상 (로컬 기준 상대경로) ───────────────
SYNC_DIRS = ["core", "data", "engine", "monitor", "risk", "scanner", "scripts", "strategy", "webapp"]
SYNC_FILES = ["main.py", "config.yaml", "requirements.txt", "quickstart.py"]
EXCLUDE_NAMES = {".venv", "__pycache__", ".env", "autotrade.db", "paper_state.json"}
EXCLUDE_EXTS  = {".pyc", ".db", ".log"}

ROOT = Path(__file__).parent.parent


def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_local_files() -> list[tuple[Path, str]]:
    """동기화할 로컬 파일 목록 (Path, 상대경로str)"""
    files = []
    for rel in SYNC_FILES:
        p = ROOT / rel
        if p.exists():
            files.append((p, rel))
    for d in SYNC_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if any(part in EXCLUDE_NAMES for part in p.parts):
                continue
            if p.suffix in EXCLUDE_EXTS:
                continue
            rel = p.relative_to(ROOT).as_posix()
            files.append((p, rel))
    return files


def remote_md5(sftp, remote_path: str) -> str | None:
    try:
        with sftp.open(remote_path, "rb") as f:
            h = hashlib.md5()
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
            return h.hexdigest()
    except Exception:
        return None


def ensure_remote_dir(sftp, remote_dir: str):
    parts = remote_dir.split("/")
    path = ""
    for part in parts:
        if not part:
            continue
        path = f"{path}/{part}"
        try:
            sftp.stat(path)
        except FileNotFoundError:
            sftp.mkdir(path)


def run(client, cmd, sudo=False, timeout=60):
    if sudo:
        cmd = f"echo '{PW}' | sudo -S bash -c \"{cmd}\" 2>/dev/null"
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    o = stdout.read().decode(errors="replace").strip()
    e = stderr.read().decode(errors="replace").strip()
    if o:
        print("  " + o[:600])
    if e and "password" not in e.lower() and "sudo:" not in e.lower()[:20]:
        clean = e.lower()
        if "warning" not in clean and "notice" not in clean:
            print("  ERR:", e[:300])
    return o


def main():
    print("=" * 50)
    print("  TossAutoTrade 배포 시작")
    print("=" * 50)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"서버 접속 중 ({HOST}:{PORT})...")
    client.connect(HOST, port=PORT, username=USER, password=PW, timeout=15)
    sftp = client.open_sftp()
    print("  접속 완료\n")

    # 1. 변경 파일 감지 및 업로드
    local_files = collect_local_files()
    uploaded, skipped = [], []
    req_changed = False

    print(f"파일 비교 중 ({len(local_files)}개)...")
    for local_path, rel in local_files:
        remote_path = f"{REMOTE}/{rel}"
        local_hash  = md5(local_path)
        remote_hash = remote_md5(sftp, remote_path)

        if local_hash == remote_hash:
            skipped.append(rel)
            continue

        ensure_remote_dir(sftp, remote_path.rsplit("/", 1)[0])
        sftp.put(str(local_path), remote_path)
        uploaded.append(rel)
        if rel == "requirements.txt":
            req_changed = True
        print(f"  [업로드] {rel}")

    print(f"\n  변경: {len(uploaded)}개 / 동일: {len(skipped)}개")

    if not uploaded:
        print("\n변경된 파일이 없습니다. 배포 불필요.")
        sftp.close()
        client.close()
        return

    sftp.close()

    # 2. requirements.txt 변경 시 pip install
    if req_changed:
        print("\npip install 실행 중...")
        run(client, f"{REMOTE}/.venv/bin/pip install -q -r {REMOTE}/requirements.txt", timeout=120)
        print("  완료")

    # 3. 서비스 재시작
    print("\n서비스 재시작...")
    run(client, f"systemctl restart {SERVICE}", sudo=True)
    time.sleep(6)

    # 4. 상태 확인
    status = run(client, f"systemctl is-active {SERVICE}", sudo=True)
    if "active" in status:
        print("  서비스: running OK")
    else:
        print(f"  서비스: {status} FAIL")
        run(client, f"journalctl -u {SERVICE} -n 10 --no-pager", sudo=True)

    # 5. cron 등록 (없으면 추가)
    cron_line = (
        f"5 16 * * 1-5 {REMOTE}/.venv/bin/python3 "
        f"{REMOTE}/scripts/collect_daily.py "
        f">> {REMOTE}/logs/collect.log 2>&1"
    )
    print("\ncron 확인...")
    existing = run(client, f"crontab -l 2>/dev/null || true")
    if "collect_daily" in existing:
        print("  cron 이미 등록됨")
    else:
        # 기존 crontab + 새 라인 등록
        run(client,
            f"(crontab -l 2>/dev/null; echo '{cron_line}') | crontab -")
        print(f"  cron 등록 완료: 평일 16:05(KST) 자동 수집")
        # logs 디렉터리 생성
        run(client, f"mkdir -p {REMOTE}/logs")

    # 6. 목표가 확인
    time.sleep(3)
    print("\n=== 목표가 현황 ===")
    run(client, f"""cd {REMOTE} && {REMOTE}/.venv/bin/python3 -c "
import sys; sys.path.insert(0,'.')
from data import db
logs = db.recent('logs', 15)
for l in logs:
    if 'SIGNAL' in l['category']:
        name = l['message'].split()[1] if len(l['message'].split()) > 1 else ''
        target = 'target=' + l['message'].split('target=')[-1] if 'target=' in l['message'] else ''
        print(l['ts'][5:19], name, target)
" 2>&1""")

    client.close()
    print("\n배포 완료!")


if __name__ == "__main__":
    main()
