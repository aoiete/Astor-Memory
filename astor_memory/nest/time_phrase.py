"""v1.15.x: natural-language time-phrase → date-range parser for /v1/read.

Lossless-memory (aru-labs) lesson (2026-09-22): time is the primary axis —
parse time phrases out of the query and restrict the search range BEFORE
ranking, instead of relying on similarity to surface the right week.

Cheap local parser: Chinese-first + English, no LLM, no deps.

API:
    parse_time_range(text, now=None) -> (since, until, phrase) | None

    since/until are YYYY-MM-DD strings (inclusive). phrase is the matched
    surface text (for response echo / debugging). Returns None when no
    time phrase is present — caller proceeds unscoped.

Ambiguity rules:
  - bare weekday (周二 / Tuesday) = MOST RECENT such weekday (today counts).
  - 上周X / last <weekday> = previous calendar week's that day.
  - 下周X = next calendar week (future ranges are valid — event_date can
    be a planned future date).
  - 昨晚/昨天晚上 = yesterday (day resolution; hour precision is out of
    scope — event_date is day-granular for most facts).
"""
import re
from datetime import datetime, timedelta

_CN_NUM = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
           '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
# weekday → Monday=0
_CN_WD = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}
_EN_WD = {'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
          'friday': 4, 'saturday': 5, 'sunday': 6}


def _d(dt):
    return dt.strftime('%Y-%m-%d')


def _week_start(d):
    """Monday 00:00 of d's calendar week."""
    return d - timedelta(days=d.weekday())


def _shift_months(d, n):
    """d minus n calendar months (day clamped)."""
    y, m = d.year, d.month - n
    while m <= 0:
        m += 12
        y -= 1
    day = min(d.day, 28)  # clamp; month ranges use full-month anyway
    return datetime(y, m, day)


def _month_range(y, m):
    first = datetime(y, m, 1)
    last = datetime(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1) - timedelta(days=1)
    return first, last


def _cn_num(s):
    if s is None:
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    return _CN_NUM.get(s)


def parse_time_range(text, now=None):
    if not text or not isinstance(text, str):
        return None
    now = now or datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    t = text.strip()
    tl = t.lower()

    # ---------- explicit ISO date ----------
    m = re.search(r'(20\d{2})-(\d{1,2})-(\d{1,2})', t)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return (_d(d), _d(d), m.group(0))
        except ValueError:
            pass

    # ---------- Chinese ----------
    # night variants BEFORE their day parents (昨天晚上 contains 昨天)
    if '昨晚' in t or '昨天晚上' in t or '昨夜' in t:
        d = today - timedelta(days=1)
        return (_d(d), _d(d), '昨晚')
    if '前天' in t:
        d = today - timedelta(days=2)
        return (_d(d), _d(d), '前天')
    if '昨天' in t or '昨日' in t:
        d = today - timedelta(days=1)
        return (_d(d), _d(d), '昨天')
    if '今天' in t or '今日' in t or '今晚' in t or '今早' in t:
        return (_d(today), _d(today), '今天')
    if '明天' in t or '明日' in t:
        d = today + timedelta(days=1)
        return (_d(d), _d(d), '明天')

    # 最近/近/过去 N 天 (range)
    m = re.search(r'(?:最近|近|过去)\s*([0-9一二三四五六七八九十两]+)\s*天', t)
    if m:
        n = _cn_num(m.group(1))
        if n:
            return (_d(today - timedelta(days=n)), _d(today), m.group(0))
    # N 天前 (single day)
    m = re.search(r'([0-9一二三四五六七八九十两]+)\s*天前', t)
    if m:
        n = _cn_num(m.group(1))
        if n:
            d = today - timedelta(days=n)
            return (_d(d), _d(d), m.group(0))
    # N 周前 / N 个星期前 → full week of that anchor
    m = re.search(r'([0-9一二三四五六七八九十两]+)\s*(?:个)?(?:周|星期|礼拜)前', t)
    if m:
        n = _cn_num(m.group(1))
        if n:
            anchor = today - timedelta(weeks=n)
            ws = _week_start(anchor)
            return (_d(ws), _d(ws + timedelta(days=6)), m.group(0))
    # N 个月前 → full month
    m = re.search(r'([0-9一二三四五六七八九十两]+)\s*个?月前', t)
    if m:
        n = _cn_num(m.group(1))
        if n:
            anchor = _shift_months(today, n)
            lo, hi = _month_range(anchor.year, anchor.month)
            return (_d(lo), _d(hi), m.group(0))

    # 上周X / 上星期X / 上周 (bare)
    m = re.search(r'上(?:个)?(?:周|星期|礼拜)([一二三四五六日天])', t)
    if m:
        wd = _CN_WD[m.group(1)]
        prev_ws = _week_start(today) - timedelta(weeks=1)
        d = prev_ws + timedelta(days=wd)
        return (_d(d), _d(d), m.group(0))
    if re.search(r'上(?:个)?(?:周|星期|礼拜)', t):
        prev_ws = _week_start(today) - timedelta(weeks=1)
        return (_d(prev_ws), _d(prev_ws + timedelta(days=6)), '上周')
    # 下周X / 下周
    m = re.search(r'下(?:个)?(?:周|星期|礼拜)([一二三四五六日天])', t)
    if m:
        wd = _CN_WD[m.group(1)]
        next_ws = _week_start(today) + timedelta(weeks=1)
        d = next_ws + timedelta(days=wd)
        return (_d(d), _d(d), m.group(0))
    if re.search(r'下(?:个)?(?:周|星期|礼拜)', t):
        next_ws = _week_start(today) + timedelta(weeks=1)
        return (_d(next_ws), _d(next_ws + timedelta(days=6)), '下周')
    # 本周/这周X or bare 这周/本周
    m = re.search(r'(?:本|这)(?:个)?(?:周|星期|礼拜)([一二三四五六日天])', t)
    if m:
        wd = _CN_WD[m.group(1)]
        d = _week_start(today) + timedelta(days=wd)
        return (_d(d), _d(d), m.group(0))
    if re.search(r'(?:本|这)(?:个)?(?:周|星期|礼拜)', t):
        ws = _week_start(today)
        return (_d(ws), _d(today), '本周')
    # bare 周X/星期X → most recent such weekday (today counts)
    m = re.search(r'(?:周|星期|礼拜)([一二三四五六日天])', t)
    if m:
        wd = _CN_WD[m.group(1)]
        delta = (today.weekday() - wd) % 7
        d = today - timedelta(days=delta)
        return (_d(d), _d(d), m.group(0))

    # 上月/上个月 / 本月/这个月
    if re.search(r'上(?:个)?月', t):
        anchor = _shift_months(today, 1)
        lo, hi = _month_range(anchor.year, anchor.month)
        return (_d(lo), _d(hi), '上月')
    if re.search(r'(?:本|这)(?:个)?月', t):
        lo, _ = _month_range(today.year, today.month)
        return (_d(lo), _d(today), '本月')

    # ---------- English ----------
    m = re.search(r'\b(?:past|last)\s+(\d+)\s+days?\b', tl)
    if m:
        n = int(m.group(1))
        return (_d(today - timedelta(days=n)), _d(today), m.group(0))
    m = re.search(r'\b(\d+)\s+days?\s+ago\b', tl)
    if m:
        d = today - timedelta(days=int(m.group(1)))
        return (_d(d), _d(d), m.group(0))
    m = re.search(r'\b(\d+)\s+weeks?\s+ago\b', tl)
    if m:
        anchor = today - timedelta(weeks=int(m.group(1)))
        ws = _week_start(anchor)
        return (_d(ws), _d(ws + timedelta(days=6)), m.group(0))
    m = re.search(r'\b(\d+)\s+months?\s+ago\b', tl)
    if m:
        anchor = _shift_months(today, int(m.group(1)))
        lo, hi = _month_range(anchor.year, anchor.month)
        return (_d(lo), _d(hi), m.group(0))

    if re.search(r'\blast\s+night\b', tl):
        d = today - timedelta(days=1)
        return (_d(d), _d(d), 'last night')
    if re.search(r'\byesterday\b', tl):
        d = today - timedelta(days=1)
        return (_d(d), _d(d), 'yesterday')
    if re.search(r'\btoday\b', tl):
        return (_d(today), _d(today), 'today')
    if re.search(r'\btomorrow\b', tl):
        d = today + timedelta(days=1)
        return (_d(d), _d(d), 'tomorrow')

    # last <weekday> / this <weekday>
    m = re.search(r'\blast\s+(' + '|'.join(_EN_WD) + r')\b', tl)
    if m:
        wd = _EN_WD[m.group(1)]
        prev_ws = _week_start(today) - timedelta(weeks=1)
        d = prev_ws + timedelta(days=wd)
        return (_d(d), _d(d), m.group(0))
    m = re.search(r'\bthis\s+(' + '|'.join(_EN_WD) + r')\b', tl)
    if m:
        wd = _EN_WD[m.group(1)]
        d = _week_start(today) + timedelta(days=wd)
        return (_d(d), _d(d), m.group(0))
    if re.search(r'\blast\s+week\b', tl):
        prev_ws = _week_start(today) - timedelta(weeks=1)
        return (_d(prev_ws), _d(prev_ws + timedelta(days=6)), 'last week')
    if re.search(r'\bthis\s+week\b', tl):
        ws = _week_start(today)
        return (_d(ws), _d(today), 'this week')
    if re.search(r'\blast\s+month\b', tl):
        anchor = _shift_months(today, 1)
        lo, hi = _month_range(anchor.year, anchor.month)
        return (_d(lo), _d(hi), 'last month')
    if re.search(r'\bthis\s+month\b', tl):
        lo, _ = _month_range(today.year, today.month)
        return (_d(lo), _d(today), 'this month')

    return None
