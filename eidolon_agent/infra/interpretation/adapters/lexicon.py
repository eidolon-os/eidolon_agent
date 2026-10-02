"""Chinese smart-home lexicon shared by the interpretation adapters.

Text normalization, device/room/verb words, number and mode extraction, and the
mapping from a verb to an SDK ``biz.smarthome`` trait command for a device type.

Everything here reads only what the request DTO carries plus the SDK
vocabulary: modules in this package import nothing else from the Agent, so they
can move into a separate service unchanged (``tests/unit/test_layering.py``).

A scene is a candidate of kind ``SCENE_KIND``, activated with
``Action(trait=SCENE_TRAIT, command=SCENE_COMMAND)`` (all from ``biz.smarthome``);
the Runtime expands it when the caller executes it.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

from eidolon_sdk.biz.interpretation import Action, Candidate, InterpretationRequest, Slot
from eidolon_sdk.biz.smarthome import (
    DEVICE_TYPES,
    SCENE_COMMAND,
    SCENE_KIND,
    SCENE_TRAIT,
    TRAIT_COMMANDS,
)

_BLANK = "#"


# --- text -----------------------------------------------------------------------

_PUNCT = re.compile(r"[\s，。！？、；：,!?;:~～…\"'“”‘’（）()【】\[\]<>《》·]+")
_STRAY_DOT = re.compile(r"(?<!\d)\.|\.(?!\d)")
SINGULAR_REFERENCE = re.compile(r"它(?!们|俩|两)|(?:这|那)(?:一)?(?:台|个|盏|扇)|其中(?:一|某)(?:台|个|盏|扇)")


def normalize(text: str) -> str:
    """NFKC (full-width digits and ％), lower case, no spaces or punctuation."""
    text = unicodedata.normalize("NFKC", text).lower()
    return _STRAY_DOT.sub("", _PUNCT.sub("", text))


@dataclass(frozen=True, slots=True)
class Hit[T]:
    start: int
    end: int
    text: str
    value: T


def take[T](text: str, table: Mapping[str, T]) -> tuple[str, list[Hit[T]]]:
    """Find the table's phrases longest-first without overlap, and blank them out.

    Blanking keeps positions stable and stops a shorter, more generic word (灯)
    from matching inside a longer, more specific one already explained (床头灯).
    """
    hits: list[Hit[T]] = []
    for phrase in sorted(table, key=len, reverse=True):
        if not phrase:
            continue
        start = text.find(phrase)
        while start != -1:
            end = start + len(phrase)
            hits.append(Hit(start, end, phrase, table[phrase]))
            text = text[:start] + _BLANK * len(phrase) + text[end:]
            start = text.find(phrase, end)
    hits.sort(key=lambda hit: hit.start)
    return text, hits


def take_pattern(text: str, pattern: re.Pattern[str]) -> tuple[str, list[re.Match[str]]]:
    matches = list(pattern.finditer(text))
    for match in matches:
        text = text[: match.start()] + _BLANK * (match.end() - match.start()) + text[match.end() :]
    return text, matches


# --- devices, rooms, scenes ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceWord:
    """A spoken device word and the candidates it can denote.

    A candidate matches when its own name or alias contains the word, or when
    its type is one of ``kinds`` (and, for types that cover several products
    such as ``fan``, its name carries one of ``keywords``). A word with no kinds
    names something the SDK vocabulary has no type for: only a candidate the
    Owner named that way can match it.
    """

    word: str
    kinds: frozenset[str] = frozenset()
    keywords: tuple[str, ...] = ()

    def matches(self, candidate: Candidate) -> bool:
        if candidate.kind == SCENE_KIND:
            return False
        names = (candidate.name, *candidate.aliases)
        if any(self.word in name for name in names):
            return True
        if candidate.kind not in self.kinds:
            return False
        return not self.keywords or any(key in name for key in self.keywords for name in names)


def _word(word: str, kinds: tuple[str, ...] = (), keywords: tuple[str, ...] = ()) -> DeviceWord:
    return DeviceWord(word, frozenset(kinds), keywords)


_LOCK = _word("门锁", ("lock",))

UNTYPED_DEVICE_WORDS = (
    "投影仪", "投影", "冰箱", "洗碗机", "烤箱", "微波炉", "油烟机", "浴霸", "地暖",
    "新风", "车库门", "门铃", "充电桩", "喂食器", "空气炸锅", "咖啡机", "饮水机", "暖气",
    "电暖器", "除湿机", "电热毯", "路由器", "电脑", "净水器", "洗地机",
)  # fmt: skip

DEVICE_WORDS: dict[str, DeviceWord] = {
    item.word: item
    for item in (
        _word("灯", ("light",)),
        _word("灯光", ("light",)),
        _word("台灯", ("light",), ("台灯",)),
        _word("开关", ("switch",)),
        _word("插座", ("switch",), ("插座",)),
        _word("空调", ("climate",)),
        _word("窗帘", ("cover",), ("帘",)),
        _word("晾衣架", ("cover",), ("晾衣", "衣架")),
        _word("净化器", ("fan",), ("净化",)),
        _word("加湿器", ("fan",), ("加湿",)),
        _word("风扇", ("fan",), ("扇",)),
        _word("电扇", ("fan",), ("扇",)),
        _word("电视", ("media",), ("电视",)),
        _word("音箱", ("media",), ("音箱", "音响")),
        _word("音响", ("media",), ("音箱", "音响")),
        _word("扫地机", ("appliance",), ("扫地", "扫拖")),
        _word("扫地", ("appliance",), ("扫地", "扫拖")),
        _word("洗衣机", ("appliance",), ("洗衣",)),
        _word("电饭煲", ("appliance",), ("电饭", "饭煲")),
        _word("电饭锅", ("appliance",), ("电饭", "饭锅")),
        _word("热水器", ("water_heater",)),
        _LOCK,
        _word("门", ("lock",)),
        _word("摄像头", ("camera",)),
        _word("监控", ("camera",)),
        _word("温湿度计", ("sensor",)),
        _word("温湿度", ("sensor",)),
        _word("温度计", ("sensor",)),
        _word("湿度计", ("sensor",)),
        _word("传感器", ("sensor",)),
        # No SDK type: only a candidate the Owner named this way can match.
        *map(_word, UNTYPED_DEVICE_WORDS),
    )
}

# A command that names no device but whose verb only fits one kind (调亮一点,
# 升温, 温度调到26度) means that kind, resolved like the spoken word would be.
IMPLIED_BY_DIMENSION: dict[str, DeviceWord] = {
    "brightness": DEVICE_WORDS["灯"],
    "temperature": DEVICE_WORDS["空调"],
}

# Asked about without naming a device: 现在多少度 -> the thermometers.
SENSOR_QUERY = re.compile(r"温度|湿度|多少度|几度|室温")
SENSOR_WORD = _word("温湿度", ("sensor",))

# Room words; one that is not an Area of this home must not fall back to the
# speaker's default room (卧室 when the home only has 主卧).
ROOM_WORDS = (
    "客厅", "卧室", "主卧", "次卧", "书房", "餐厅", "厨房", "卫生间", "洗手间", "浴室",
    "厕所", "阳台", "玄关", "车库", "儿童房", "客房", "走廊", "过道", "门口", "花园",
    "院子", "地下室", "阁楼", "衣帽间", "楼上", "楼下",
)  # fmt: skip

QUANTIFIERS = ("全部", "所有", "全屋", "整屋", "全家", "全都", "都")

SCENE_SUFFIXES = ("模式", "场景")
SCENE_VERBS = ("打开", "开启", "启动", "执行", "切换到", "切到", "进入", "激活", "开")


# --- verbs --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Verb:
    """What to do, before it is bound to a device type.

    ``op``: on / off / stop / pause / dock / mute / lock / unlock / up / down / set.
    ``dimension``: brightness / temperature / volume / speed / position, if said.
    """

    op: str
    dimension: str | None = None


_ON, _OFF, _SET, _UP, _DOWN = Verb("on"), Verb("off"), Verb("set"), Verb("up"), Verb("down")

# Verb phrases that also name the lock ("把门锁上" is 门 + 锁上, not 门锁 + 上).
LOCK_COMPOUNDS: dict[str, tuple[Verb, DeviceWord]] = {
    "门锁上": (Verb("lock"), _LOCK),
    "门锁好": (Verb("lock"), _LOCK),
    "锁门": (Verb("lock"), _LOCK),
    "开门": (Verb("unlock"), _LOCK),
}

_VERB_WORDS: tuple[tuple[str, Verb], ...] = (
    ("调到 调成 调为 调至 设置为 设置成 设置到 设为 设成 设到 定到 开到 开成 升到 降到 "
     "调高到 调低到 切换到 切到", _SET),
    ("静音", Verb("mute")),
    ("上锁 锁上 锁好 反锁", Verb("lock")),
    ("开锁 解锁", Verb("unlock")),
    ("回充 回去充电 去充电 充电 回座", Verb("dock")),
    ("暂停", Verb("pause")),
    ("停止 停下 停掉 停一下", Verb("stop")),
    ("调亮 亮一点 亮一些 亮点", Verb("up", "brightness")),
    ("调暗 暗一点 暗一些 暗点", Verb("down", "brightness")),
    ("升温", Verb("up", "temperature")),
    ("降温", Verb("down", "temperature")),
    ("大声", Verb("up", "volume")),
    ("小声", Verb("down", "volume")),
    ("调高 调大 升高 加大 高一点 大一点 高一些 大一些 高点 大点", _UP),
    ("调低 调小 降低 减小 低一点 小一点 低一些 小一些 低点 小点", _DOWN),
    ("拉开 打开 开启 启动 开始 运行 开", _ON),
    ("拉上 合上 关闭 关掉 关上 关", _OFF),
)  # fmt: skip
VERBS: dict[str, Verb] = {
    phrase: verb for phrases, verb in _VERB_WORDS for phrase in phrases.split()
}

# Words whose 开/关/别/电视 are not about devices. The rules adapter blanks them
# in the same longest-first pass as device names and words.
NOISE = (
    "开心", "开玩笑", "开会", "开车", "开学", "开饭", "开发", "开放", "开水", "开销",
    "开口", "离开", "公开", "关心", "关系", "关于", "关注", "关键", "电视剧", "电视台",
    "电视节目", "特别", "区别", "分别", "识别", "别人", "别的", "告别",
)  # fmt: skip

NEGATION = re.compile(r"别|不要|不用|不必|甭|无需|不需要|没必要|不(?=[开关调打启锁拉])")
# "有点热", "太暗了": a wish, not a command; the LLM fallback decides.
IMPLICIT = re.compile(
    r"(有点|有些|太|好|真|很|特别|挺|这么|怎么这么)(热|冷|暗|亮|吵|闷|干|燥|潮|黑|晒|刺眼|凉)"
    r"|(热|冷|暗|吵|闷|干)死了|看不清|睡不着"
)
QUERY = re.compile(
    r"(开|关|亮|锁)(着|没\1|了吗|了没有?|的吗)|(有没有|是不是)(开|关|锁|亮)"
    r"|多少|几度|几档|状态|怎么样|什么模式"
)
QUERY_TAIL = re.compile(r"(了吗|了没有?)$")
WEATHER = re.compile(r"天气|气温|外面|室外|下雨|下雪|今天|明天|后天")

MODES: dict[str, str] = {
    **dict.fromkeys(("制冷", "冷风"), "cool"),
    **dict.fromkeys(("制热", "暖风", "热风"), "heat"),
    **dict.fromkeys(("除湿", "抽湿"), "dry"),
    **dict.fromkeys(("送风", "通风"), "fan"),
    "自动": "auto",
}


# --- numbers ------------------------------------------------------------------------

_CN_DIGITS = {ch: i for i, ch in enumerate("零一二三四五六七八九")} | {"〇": 0, "两": 2}
_CN = "零〇一二两三四五六七八九"
NUMBER = rf"(?:\d+(?:\.\d+)?|[{_CN}十百]+(?:点[{_CN}]+)?)"

_DIMENSION_WORDS = {
    **dict.fromkeys(("亮度",), "brightness"),
    **dict.fromkeys(("音量", "声音"), "volume"),
    **dict.fromkeys(("风速", "风量"), "speed"),
    **dict.fromkeys(("温度",), "temperature"),
    **dict.fromkeys(("开合度", "位置"), "position"),
    # No v1 trait: named so 色温调到4000 abstains instead of reading as brightness.
    **dict.fromkeys(("色温",), "color_temperature"),
}
_DIMENSION = "|".join(_DIMENSION_WORDS)
_SET_WORDS = "调到|调成|调为|调至|设置为|设置到|设为|设到|设成|开到|定到|升到|降到"
_QUANTITY_PATTERNS: tuple[tuple[re.Pattern[str], str | None], ...] = (
    (re.compile(rf"百分之({NUMBER})"), "percent"),
    (re.compile(rf"({NUMBER})%"), "percent"),
    (re.compile(rf"({NUMBER})(?:度|°c|°)"), "celsius"),
    (re.compile(r"(一半)"), "percent"),
    (re.compile(rf"(?:{_DIMENSION})(?:{_SET_WORDS}|到|为|是)?({NUMBER})"), None),
    (re.compile(rf"(?:{_SET_WORDS})({NUMBER})"), None),
)
_DIGIT = re.compile(r"\d")


def parse_number(token: str) -> int | float | None:
    """``26`` / ``25.5`` / ``二十六`` / ``两`` / ``一百`` / ``二十五点五`` / ``一半``."""
    if token == "一半":
        return 50
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        return float(token) if "." in token else int(token)
    whole, _, fraction = token.partition("点")
    value = _parse_cn_int(whole)
    if value is None:
        return None
    if not fraction:
        return value
    digits = [_CN_DIGITS.get(ch) for ch in fraction]
    if any(d is None for d in digits):
        return None
    return float(f"{value}.{''.join(str(d) for d in digits)}")


def _parse_cn_int(text: str) -> int | None:
    if not text:
        return None
    total, current, last_digit = 0, 0, False
    for ch in text:
        if ch in "零〇":  # a gap after a unit (一百零五), never a digit of its own
            if last_digit:
                return None
            current = 0
        elif ch in _CN_DIGITS:
            if last_digit:  # "二六" is not a number we read
                return None
            current, last_digit = _CN_DIGITS[ch], True
        elif ch == "十":
            total += (current or 1) * 10
            current, last_digit = 0, False
        elif ch == "百":
            total += (current or 1) * 100
            current, last_digit = 0, False
        else:
            return None
    return total + current


@dataclass(frozen=True, slots=True)
class Quantity:
    value: int | float
    unit: str | None  # "celsius" | "percent" | None
    raw_span: str


@dataclass(frozen=True, slots=True)
class Values:
    """Numbers and modes said in an utterance. ``problem`` means do not guess."""

    quantity: Quantity | None = None
    mode: str | None = None
    dimension: str | None = None
    problem: str | None = None


def utterance_values(request: InterpretationRequest) -> Values:
    """Read amounts without treating digits in Owner names/aliases as values."""
    names = {normalize(n): None for c in request.candidates for n in (c.name, *c.aliases)}
    text, _ = take(normalize(request.utterance), names)
    return read_values(text)[1]


def read_values(text: str) -> tuple[str, Values]:
    """Lift at most one number and one mode from normalized text, and blank them.

    More than one number, an unreadable one, or a stray digit left over is a
    ``problem``: the caller abstains rather than pick one.
    """
    dimensions = {_DIMENSION_WORDS[w] for w in _DIMENSION_WORDS if w in text}
    dimension = dimensions.pop() if len(dimensions) == 1 else None
    text, modes = take(text, MODES)
    mode_ids = {hit.value for hit in modes}
    if len(mode_ids) > 1:
        return text, Values(dimension=dimension, problem="multiple_modes")
    found: list[Quantity] = []
    for pattern, unit in _QUANTITY_PATTERNS:
        text, matches = take_pattern(text, pattern)
        for match in matches:
            value = parse_number(match.group(1))
            if value is None:
                return text, Values(dimension=dimension, problem="unreadable_number")
            found.append(Quantity(value, unit, match.group(0)[:64]))
    if len(found) > 1 or _DIGIT.search(text):
        return text, Values(dimension=dimension, problem="multiple_values")
    quantity = found[0] if found else None
    if quantity is not None and quantity.unit is None and dimension == "temperature":
        quantity = Quantity(quantity.value, "celsius", quantity.raw_span)
    return text, Values(
        quantity=quantity,
        mode=mode_ids.pop() if mode_ids else None,
        dimension=dimension,
    )


# --- verb -> SDK trait command --------------------------------------------------------

_SIMPLE: dict[str, tuple[tuple[str, str], ...]] = {
    "on": (("on_off", "on"), ("position", "open"), ("operational", "start")),
    "off": (("on_off", "off"), ("position", "close"), ("operational", "stop")),
    "stop": (("position", "stop"), ("operational", "stop"), ("on_off", "off")),
    "pause": (("operational", "pause"),),
    "dock": (("operational", "dock"),),
    "lock": (("lock", "lock"),),
    "unlock": (("lock", "unlock"),),
    "mute": (("volume", "mute"),),
}
# trait, dimensions it answers to, default step, units a spoken amount may carry
_STEPS: tuple[tuple[str, tuple[str, ...], int, tuple[str | None, ...]], ...] = (
    ("level", ("brightness",), 10, ("percent", None)),
    ("thermostat", ("temperature",), 1, ("celsius", None)),
    ("volume", ("volume",), 10, ("percent", None)),
)
_SET_BY_DIMENSION = {
    "brightness": "level",
    "volume": "volume",
    "speed": "fan_speed",
    "position": "position",
}
_PERCENT_TRAITS = ("level", "position", "fan_speed", "volume")
_ALL_TRAITS = tuple(TRAIT_COMMANDS)


def command_for(kind: str, verb: Verb, values: Values) -> Action | None:
    """The one command ``verb`` means for a candidate of this kind, or None."""
    if kind == SCENE_KIND:
        plain = values.quantity is None and values.mode is None
        if not plain or verb.op not in ("on", "set"):
            return None
        return Action(trait=SCENE_TRAIT, command=SCENE_COMMAND)
    spec = DEVICE_TYPES.get(kind)
    if spec is None:
        return None
    return _command(spec.traits, spec.modes, verb, values)


def generic_command(verb: Verb, values: Values) -> Action | None:
    """An action for a device the home does not have; only its wording matters."""
    return _command(_ALL_TRAITS, None, verb, values)


def _command(
    traits: tuple[str, ...], modes: tuple[str, ...] | None, verb: Verb, values: Values
) -> Action | None:
    if values.problem is not None:
        return None
    quantity, mode = values.quantity, values.mode
    dimension = verb.dimension or values.dimension
    op = verb.op
    if op == "on" and (quantity is not None or mode is not None):
        op = "set"  # 开到26度, 窗帘开一半
    if op == "set":
        return _set(traits, modes, dimension, quantity, mode)
    if mode is not None:
        return None
    if op in ("up", "down"):
        return _step(traits, dimension, quantity, 1 if op == "up" else -1)
    if quantity is not None:
        return None
    for trait, command in _SIMPLE.get(op, ()):
        if trait in traits:
            slots = (Slot(name="muted", value=True),) if command == "mute" else ()
            return Action(trait=trait, command=command, slots=slots)
    return None


def _set(
    traits: tuple[str, ...],
    modes: tuple[str, ...] | None,
    dimension: str | None,
    quantity: Quantity | None,
    mode: str | None,
) -> Action | None:
    if mode is not None:
        if quantity is not None or "thermostat" not in traits:
            return None
        if modes is not None and mode not in modes:
            return None
        return Action(
            trait="thermostat", command="set_mode", slots=(Slot(name="mode", value=mode),)
        )
    if quantity is None:
        return None
    if quantity.unit == "celsius" or dimension == "temperature":
        if "thermostat" not in traits or quantity.unit == "percent":
            return None
        return _valued("thermostat", "set_target", "celsius", quantity.value, "°C", quantity)
    if dimension in _SET_BY_DIMENSION:
        trait = _SET_BY_DIMENSION[dimension]
        trait = trait if trait in traits else None
    elif dimension is not None:
        return None  # a setting the vocabulary has no trait for
    else:
        trait = next((t for t in _PERCENT_TRAITS if t in traits), None)
    if trait is None:
        if quantity.unit is None and dimension is None and "thermostat" in traits:
            return _valued("thermostat", "set_target", "celsius", quantity.value, "°C", quantity)
        return None
    if not isinstance(quantity.value, int):
        return None
    unit = "%" if quantity.unit == "percent" else None
    return _valued(trait, "set", "value", quantity.value, unit, quantity)


def _step(
    traits: tuple[str, ...], dimension: str | None, quantity: Quantity | None, sign: int
) -> Action | None:
    for trait, dimensions, default, units in _STEPS:
        if trait not in traits or (dimension is not None and dimension not in dimensions):
            continue
        if quantity is None:
            amount: int | float = default
        elif quantity.unit in units:
            amount = quantity.value
        else:
            return None
        if trait != "thermostat" and not isinstance(amount, int):
            return None
        unit = "°C" if trait == "thermostat" else ("%" if trait == "level" else None)
        return _valued(trait, "step", "delta", sign * amount, unit, quantity)
    return None


def _valued(
    trait: str,
    command: str,
    name: str,
    value: int | float,
    unit: str | None,
    quantity: Quantity | None,
) -> Action:
    slot = Slot(
        name=name,
        value=value,
        unit=unit,
        raw_span=quantity.raw_span if quantity is not None else None,
    )
    return Action(trait=trait, command=command, slots=(slot,))
