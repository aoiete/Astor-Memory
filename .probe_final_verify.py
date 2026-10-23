"""最终验证: 新 server (v1.16.88) 上的中文召回 + 两个新指标。"""
import json
import urllib.request

CASES = [
    ("脚本同步铁律", "脚本同步铁律"),
    ("德州扑克现金局", "德州扑克"),
    ("OpenD 看门狗误杀", "看门狗"),
    ("紫微 命盘 输出", "紫微"),
    ("八字 壬水身强", "身强"),
    ("流天书 协议 客户档案", "流天书"),
    ("批量订阅取当日bar", "batch_bars"),
    ("记忆 召回 污染", "召回"),
    ("相关性闸门", "闸门"),
    ("中文分词召回", "分词"),
    ("历史K线配额打满", "K线"),
    ("旺时 凶时 打牌", "旺时"),
]


def api(q, tier="private", user="admin", top_k=10):
    body = json.dumps({"query": q, "tier": tier, "user": user,
                       "top_k": top_k}).encode()
    req = urllib.request.Request("http://127.0.0.1:7803/v1/read", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("results", []) or []


hit = 0
for q, kw in CASES:
    res = api(q)
    got = any(kw.lower() in ((r.get("content") or "")).lower() for r in res)
    hit += 1 if got else 0
    print(f"  {'OK ' if got else 'MISS'} {q:20s} {len(res):2d} hits")
print()
print(f"live server (v1.16.88) 中文召回: {hit}/{len(CASES)}")
