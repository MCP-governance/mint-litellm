"""기반 이미지의 설치 목록(pip freeze)과 잠금 제약을 합친다.

기반 이미지에 이미 있는 패키지는 그 판본으로 고정하고, 새로 설치될 패키지만 잠금 판본을 쓴다.
MINT가 기반 판본과 맞지 않는 판본을 요구하면 pip가 충돌로 실패해 빌드가 멈춘다.
"""
import re
import sys

freeze_path, lock_path = sys.argv[1:3]


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


base = {}
for line in open(freeze_path, encoding="utf-8"):
    m = re.match(r"([A-Za-z0-9_.-]+)==(\S+)", line)
    if m:
        base[norm(m[1])] = line.strip()
for line in open(lock_path, encoding="utf-8"):
    m = re.match(r"([A-Za-z0-9_.-]+)==", line)
    if m and norm(m[1]) not in base:
        print(line.strip())
print("\n".join(base.values()))
