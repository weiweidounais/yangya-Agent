"""Shared deterministic crisis rules."""

import re

_CRISIS_KEYS = (
    "想死", "不想活", "活着没意思", "活不下去", "自杀", "轻生", "结束生命", "了结自己",
    "伤害自己", "自残", "割腕", "跳楼", "跳下去", "不如死了", "去死", "撑不下去了",
    "结束这一切", "结束一切", "一了百了", "解脱了", "离开这个世界", "不想醒来",
    "活着有什么意义", "活着没意义", "活着好累", "不想再活", "安眠药", "遗书", "告别了",
    "没有我", "没我大家", "拖累", "累赘", "负担", "交代", "后事", "不用麻烦大家",
    "不会再麻烦", "不用再醒", "不再醒", "醒不过来", "睡着了就不", "永远睡", "最后一次",
    "再见了大家", "对不起大家", "撑不住", "坚持不下去",
)
_CRISIS_EN = re.compile(r"(end (it|it all|my life)|kill myself|suicid|want to die|don'?t want to (live|wake up)|better off without me|self[- ]?harm|hurt myself|no reason to live)", re.I)

_CRISIS_RE = re.compile(r"(一切|活着|人生|生活|活下去).{0,6}(没意义|没有意义|没什么意义|无意义).{0,12}(结束|消失|离开|不想|算了|死)")



# Everyday phrases that contain a lexicon key but are not risk signals.
# Only these exact compounds are discounted; any other use of the key still fires.
_BENIGN_COMPOUNDS = (
    "工作负担", "经济负担", "学习负担", "课业负担", "财务负担", "减轻负担", "负担得起", "负担不起",
    "工作交代", "交代工作", "交代清楚", "交代一下",
    "最后一次会议", "最后一次尝试", "最后一次修改", "最后一次提交",
    "项目负担", "负担太重", "负担很重", "负担有点重", "工作交接", "交接工作",
)
# Entrusting pets / family to someone is a classic farewell sign (「把猫托付给你」).
_ENTRUST = re.compile(r"(托付|拜托[^，。,.]{0,4}照顾|以后[^，。,.]{0,4}帮我照顾|帮我照顾好).{0,8}(猫|狗|宠物|孩子|爸妈|父母|家人)|(猫|狗|宠物|孩子|爸妈|父母|家人).{0,6}(托付|拜托给)")
_SELF_BURDEN = re.compile(r"(我|自己)(就|只)?(是|成了|成为|变成|算是)[^，。,.!！?？]{0,8}(负担|累赘|拖累)")


_DISAPPEAR_OBJECT = re.compile(
    r"(?:朋友圈|动态|粉丝|账号|消息|图片|内容|bug|问题|报错|异常|红点|弹窗)[的都了]*$", re.I
)
_DISAPPEAR_CUE = re.compile(r"我|自己|想|算了|永远|离开|再见|告别")


def _self_disappearance(text: str) -> bool:
    # Check each occurrence so object subjects cannot mask a later self-reference.
    for clause in re.split(r"[，。！？；,.!?;\n]", text):
        for match in re.finditer("消失", clause):
            if _DISAPPEAR_OBJECT.search(clause[:match.start()]):
                continue
            if _DISAPPEAR_CUE.search(clause[max(0, match.start() - 12):match.end() + 12]):
                return True
    return False


def has_crisis_signal(msg: str) -> bool:
    t = (msg or "").replace(" ", "")
    keyed = t
    for c in _BENIGN_COMPOUNDS:
        keyed = keyed.replace(c, "")
    return (
        bool(_SELF_BURDEN.search(t))
        or bool(_ENTRUST.search(t))
        or _self_disappearance(t)
        or any(k in keyed for k in _CRISIS_KEYS)
        or bool(_CRISIS_RE.search(t))
        or bool(_CRISIS_EN.search(msg or ""))
    )


