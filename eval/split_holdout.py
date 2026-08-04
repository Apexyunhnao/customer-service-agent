"""
留出集切分 — 用固定随机种子 42 将 test_cases.json 按 8:2 切分，
80% → train_cases.json，20% → holdout_cases.json。

TC-H 前缀的用例强制归入 train（history 用例仅供开发调试）。
"""

import json
import random
from pathlib import Path

SEED = 42
TRAIN_RATIO = 0.8
BASE_DIR = Path(__file__).parent

# 加载全量
with open(BASE_DIR / "test_cases.json", "r", encoding="utf-8") as f:
    all_cases = json.load(f)

# 读旧 holdout（如果有的话），用于 diff
old_holdout_ids: set[str] = set()
old_holdout_path = BASE_DIR / "holdout_cases.json"
if old_holdout_path.exists():
    with open(old_holdout_path, "r", encoding="utf-8") as f:
        old_holdout_ids = {c["id"] for c in json.load(f)}

# TC-H 用例单独提出
tc_h_cases = [c for c in all_cases if c["id"].startswith("TC-H")]
other_cases = [c for c in all_cases if not c["id"].startswith("TC-H")]

# 固定种子打乱（只打乱非 TC-H 用例）
random.seed(SEED)
shuffled = list(other_cases)
random.shuffle(shuffled)

# 切分（只在非 TC-H 用例中）
split_idx = int(len(shuffled) * TRAIN_RATIO)
train_other = shuffled[:split_idx]
holdout_cases = shuffled[split_idx:]

# TC-H 强制归 train
train_cases = sorted(train_other + tc_h_cases, key=lambda x: x["id"])
holdout_cases = sorted(holdout_cases, key=lambda x: x["id"])

# 断言 holdout 不含 TC-H
assert not any(c["id"].startswith("TC-H") for c in holdout_cases), \
    "BUG: TC-H 用例泄漏到 holdout!"

# 写入
with open(BASE_DIR / "train_cases.json", "w", encoding="utf-8") as f:
    json.dump(train_cases, f, ensure_ascii=False, indent=2)

with open(BASE_DIR / "holdout_cases.json", "w", encoding="utf-8") as f:
    json.dump(holdout_cases, f, ensure_ascii=False, indent=2)

# 打印成员清单
def ids(cases: list) -> list[str]:
    return [c["id"] for c in cases]

def stats(cases: list, name: str) -> None:
    cats = {}
    acts = {}
    for c in cases:
        cats[c["category"]] = cats.get(c["category"], 0) + 1
        acts[c["expected_action"]] = acts.get(c["expected_action"], 0) + 1
    print(f"  {name} ({len(cases)} 条): 分类={cats}  行动={acts}")

print(f"全量: {len(all_cases)} → train: {len(train_cases)}, holdout: {len(holdout_cases)}")
print(f"TC-H 用例: {ids(tc_h_cases)} (强制归 train)")
print()
stats(train_cases, "train")
print(f"  train 成员: {ids(train_cases)}")
print()
stats(holdout_cases, "holdout")
print(f"  holdout 成员: {ids(holdout_cases)}")

# diff holdout 变化
new_holdout_ids = {c["id"] for c in holdout_cases}
if old_holdout_ids and old_holdout_ids != new_holdout_ids:
    removed = old_holdout_ids - new_holdout_ids
    added = new_holdout_ids - old_holdout_ids
    if removed:
        print(f"\n  holdout 移除: {sorted(removed)}")
    if added:
        print(f"  holdout 新增: {sorted(added)}")
elif old_holdout_ids:
    print("\n  holdout 成员未变")

print(f"\n文件已写入:")
print(f"  {BASE_DIR / 'train_cases.json'}")
print(f"  {BASE_DIR / 'holdout_cases.json'}")
