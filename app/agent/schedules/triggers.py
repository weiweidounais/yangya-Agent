"""Five-field numeric cron, interval, and timezone-aware one-shot triggers."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("时区无效，请提供有效的时区名称") from None


def field(expr, low, high):
    values = set()
    try:
        for part in expr.split(','):
            base, *steps = part.split('/')
            if len(steps) > 1:
                raise ValueError()
            step = int(steps[0]) if steps else 1
            if step < 1:
                raise ValueError()
            if base == '*':
                start, end = low, high
            elif '-' in base:
                start, end = map(int, base.split('-'))
            else:
                start = int(base)
                end = high if steps else start
            if not low <= start <= end <= high:
                raise ValueError()
            values.update(range(start, end + 1, step))
    except (ValueError, TypeError):
        raise ValueError("时间表达式无效：支持数字、星号、逗号、范围和步长") from None
    return values


def cron_fields(expr):
    parts = str(expr).split()
    if len(parts) != 5:
        raise ValueError("时间表达式必须包含分、时、日、月、星期五段")
    return parts, [field(p, lo, hi) for p, (lo, hi) in zip(parts, [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)])]


def next_run(trigger, tz, after):
    local_zone = zone(tz)
    after = utc(after)
    if not isinstance(trigger, dict):
        raise ValueError("请提供明确的触发时间")
    kind = trigger.get('type')
    if kind == 'once':
        try:
            value = datetime.fromisoformat(trigger['run_at'].replace('Z', '+00:00'))
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ValueError("请提供一次性提醒的完整日期和时间") from None
        if value.tzinfo is None:
            value = value.replace(tzinfo=local_zone)
            if value.astimezone(timezone.utc).astimezone(local_zone).replace(tzinfo=None) != value.replace(tzinfo=None):
                raise ValueError("该本地时间不存在，请换一个时间")
        return utc(value)
    if kind == 'interval':
        seconds = trigger.get('every_seconds')
        if type(seconds) is not int or not 60 <= seconds <= 366 * 86400:
            raise ValueError("执行间隔须为 60 秒至 366 天之间的整数秒")
        return after + timedelta(seconds=seconds)
    if kind != 'cron':
        raise ValueError("仅支持定时、固定间隔和一次性提醒")
    parts, (minutes, hours, days, months, weekdays) = cron_fields(trigger.get('expr', ''))
    # Iterate calendar dates, then convert candidate wall times to UTC. Validate
    # round trips to omit DST gaps; both folds are valid chronological instants.
    first = after.astimezone(local_zone).date()
    for offset in range(366 * 8):
        date = first + timedelta(days=offset)
        dow = (date.weekday() + 1) % 7
        dom_ok, dow_ok = date.day in days, dow in weekdays or (dow == 0 and 7 in weekdays)
        # A leading wildcard keeps cron's AND semantics even with a step.
        # Explicit ranges remain restricted fields, including full ranges.
        day_ok = (dom_ok and dow_ok) if parts[2].startswith('*') or parts[4].startswith('*') else (dom_ok or dow_ok)
        if date.month not in months or not day_ok:
            continue
        candidates = set()
        for hour in sorted(hours):
            for minute in sorted(minutes):
                naive = datetime(date.year, date.month, date.day, hour, minute)
                for fold in (0, 1):
                    value = naive.replace(tzinfo=local_zone, fold=fold).astimezone(timezone.utc)
                    if value > after and value.astimezone(local_zone).replace(tzinfo=None) == naive:
                        candidates.add(value)
        if candidates:
            return min(candidates)
    raise ValueError("未来八年内没有符合条件的运行时间")
