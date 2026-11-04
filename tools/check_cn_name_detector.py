"""Measure a Chinese personal-name detector.

The classifier cannot see a name: "张三负责华东区" has no pronoun, no
PII token, no marker word, so it classifies as commons and lands in the
shared tier with the name intact.

A surname list alone is far too noisy -- 方法, 方案, 方面, 目标, 时间
all start with a one-character surname. The name has to be a surname
followed by characters that actually occur in given names.
"""
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, r"D:\AI\astor-memory")
from astor_memory.nest.visibility_classifier import (
    AUTO_COMMONS_KINDS, has_pii, has_emotion, has_geographic, has_first_person,
)

RT = Path(r"D:\AI\Astor-Memory-Runtime")

# one-character surnames that are common enough to collide with ordinary
# words -- kept, but the given-name characters below do the real work
SURNAMES = (
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐费廉岑"
    "薛雷贺倪汤滕殷罗毕郝邬安常乐傅皮卞齐康伍余元卜顾孟平黄穆萧尹姚邵"
    "汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强"
    "贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯管卢莫房"
    "解应宗丁宣邓郁单杭洪包左石崔吉龚程嵇邢裴陆荣翁荀羊甄曲封芮储靳汲"
    "糜松井段富巫乌焦巴牧山谷车侯班秋仲伊宫宁仇栾甘厉祖武符刘景詹龙叶幸"
    "韶郜黎宿白怀蒲鄂索赖卓蔺屠蒙池乔翟谭姬申冉雍桑桂牛通边燕浦尚农温庄"
    "柴瞿阎充慕连茹艾鱼容向易慎戈廖步都耿满弘国文寇广师巩聂晁敖冷辛阚简"
    "饶沙鞠丰巢关相查荆红游权盖桓"
)
# characters that actually turn up in Chinese given names
GIVEN = (
    "伟芳娜秀英敏静丽强磊洋艳勇军杰娟涛明超霞平刚桂香彬颖悦婷宁康健"
    "俊峰旭博宇轩浩然凯文倩雪莉薇琳嘉怡欣悦萍红梅兰凤云燕秋冬雪冰洁"
    "佳琴桂兰建华建国建军春燕赵丽张伟王芳李娜刘洋陈静杨磊黄敏周涛吴强"
)

CN_NAME_RE = re.compile(
    r'(?<![一-鿿])([' + SURNAMES + r'][一-鿿]{1,2})(?![一-鿿])')


def is_name(text):
    for m in CN_NAME_RE.finditer(text or ''):
        s = m.group(1)
        if s[1:] and all(c in GIVEN for c in s[1:]):
            return s
    return None


def main():
    print("=== 检测器自检 ===")
    should_match = ["张三负责华东区", "李雷主持复盘", "王芳做了周报", "欧阳修的笔记"]
    should_not = ["方法要写成步骤", "方案已经定了", "方面都要考虑", "目标很明确",
                   "时间到了就做", "问题在于分层", "任务完成了", "我们把结果写进文件",
                   "共享层保持干净", "方式很简单", "流程走一遍"]
    bad = 0
    for t in should_match:
        got = is_name(t)
        ok = got is not None
        bad += (not ok)
        print(f"  {'OK  ' if ok else 'MISS'} {t!r} -> {got}")
    print()
    for t in should_not:
        got = is_name(t)
        ok = got is None
        bad += (not ok)
        print(f"  {'OK  ' if ok else 'FP  '} {t!r} -> {got}")

    print("\n=== 真实语料 ===")
    rows = []
    for db in sorted(RT.glob("**/*.db")):
        try:
            con = sqlite3.connect(db)
            if not con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                               "AND name='memory_canonical'").fetchone():
                con.close()
                continue
            cols = {r[1] for r in con.execute("PRAGMA table_info(memory_canonical)")}
            if "content" not in cols:
                con.close()
                continue
            for kind, vis, content in con.execute(
                    "SELECT kind, visibility, content FROM memory_canonical"):
                if content:
                    rows.append((kind or "", vis or "", content))
            con.close()
        except Exception:
            pass
    hits = []
    for k, v, c in rows:
        n = is_name(c)
        if n:
            hits.append((k, v, n, c))
    print(f"全库 {len(rows)} fact, 命中疑似人名 {len(hits)} 条")
    cnt = Counter(n for _k, _v, n, _c in hits)
    print("  最常命中:", cnt.most_common(12))
    commons = [(k, v, n, c) for k, v, n, c in hits
               if v == "commons" and k in AUTO_COMMONS_KINDS]
    print(f"\n共享层里含疑似人名的: {len(commons)} 条")
    for k, v, n, c in commons[:10]:
        print(f"   - [{k}] {n} :: {c[:80]}")

    print("\n=== 若把它当 private 信号: 共享层会被推回私有的条数 ===")
    push = 0
    for k, v, c in rows:
        if v != "commons" or k not in AUTO_COMMONS_KINDS:
            continue
        if (has_pii(c) or has_emotion(c) or has_geographic(c)
                or has_first_person(c)):
            continue
        if is_name(c):
            push += 1
    print(f"  {push} 条")


if __name__ == "__main__":
    main()
