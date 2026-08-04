"""
留出集切分 — 用固定随机种子 42 将 test_cases.json 按 8:2 切分，
80% → train_cases.json，20% → holdout_cases.json。
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

# 固定种子打乱
random.seed(SEED)
shuffled = list(all_cases)
random.shuffle(shuffled)

# 切分
split_idx = int(len(shuffled) * TRAIN_RATIO)
train_cases = sorted(shuffled[:split_idx], key=lambda x: x["id"])
holdout_cases = sorted(shuffled[split_idx:], key=lambda x: x["id"])

# 写入
with open(BASE_DIR / "train_cases.json", "w", encoding="utf-8") as f:
    json.dump(train_cases, f, ensure_ascii=False, indent=2)

with open(BASE_DIR / "holdout_cases.json", "w", encoding="utf-8") as f:
    json.dump(holdout_cases, f, ensure_ascii=False, indent=2)

# 统计
def stats(cases: list, name: str) -> None:
    cats = {}
    acts = {}
    for c in cases:
        cats[c["category"]] = cats.get(c["category"], 0) + 1
        acts[c["expected_action"]] = acts.get(c["expected_action"], 0) + 1
    print(f"  {name} ({len(cases)} 条): 分类={cats}  行动={acts}")

print(f"全量: {len(all_cases)} → train: {len(train_cases)}, holdout: {len(holdout_cases)}")
stats(train_cases, "train")
stats(holdout_cases, "holdout")
print(f"\n文件已写入:")
print(f"  {BASE_DIR / 'train_cases.json'}")
print(f"  {BASE_DIR / 'holdout_cases.json'}")
