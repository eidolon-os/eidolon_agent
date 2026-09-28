"""Deterministic Chinese rules interpreter for smart-home utterances (production v0).

In-process, a few milliseconds, no model. It decides only what it can read
without guessing:

- target: device name/alias first; then an Area name + device word; then the
  device word in the speaker's room (``origin.area_id``); several matches are
  ``ambiguous`` (never a pick), 全部/所有 + word takes them all, and a device
  word the home lacks (投影仪) is ``none``;
- intent: 开着吗 / 多少度 / 状态 is ``query``; no command verb and no device
  mention is ``unrelated``;
- anything it should not decide alone is ``abstained`` with a diagnostic reason:
  negation (别开), several commands in one sentence, implicit wishes (有点热),
  a room this home does not have, numbers it cannot place.

A scene is proposed as ``Action(trait=SCENE_TRAIT, command=SCENE_COMMAND)`` on
its candidate; the Runtime expands it at execution. A ``none`` target carries the
word the home lacks in ``Proposal.mention``.
"""

from __future__ import annotations

import re

from eidolon_sdk.biz.interpretation import (
    ERROR_INVALID_REQUEST,
    Action,
    Area,
    Candidate,
    InterpretationError,
    InterpretationRequest,
    InterpretationResult,
    Proposal,
)
from eidolon_sdk.biz.smarthome import SCENE_KIND

from eidolon_agent.infra.interpretation.adapters.lexicon import (
    DEVICE_WORDS,
    IMPLICIT,
    IMPLIED_BY_DIMENSION,
    LOCK_COMPOUNDS,
    NEGATION,
    NOISE,
    QUANTIFIERS,
    QUERY,
    QUERY_TAIL,
    ROOM_WORDS,
    SCENE_SUFFIXES,
    SCENE_VERBS,
    SENSOR_QUERY,
    SENSOR_WORD,
    VERBS,
    WEATHER,
    DeviceWord,
    Hit,
    Values,
    Verb,
    command_for,
    generic_command,
    normalize,
    read_values,
    take,
    take_pattern,
)

POLICY_VERSION = "rules-zh-v1"
MODEL_VERSION = "rules-zh-v1"
MAX_TARGETS = 64
MAX_MENTION = 32  # Proposal.mention

_UNRELATED = Proposal(intent="unrelated", target_status="none")

Resolution = tuple[str, tuple[Candidate, ...], str | None]


class RulesInterpreter:
    """Implements the InteractionInterpretation port for ``domain="smarthome"``."""

    def __init__(
        self,
        *,
        policy_version: str = POLICY_VERSION,
        model_version: str = MODEL_VERSION,
    ) -> None:
        self._policy_version = policy_version
        self._model_version = model_version

    async def interpret(self, request: InterpretationRequest) -> InterpretationResult:
        if request.domain != "smarthome":
            raise InterpretationError(ERROR_INVALID_REQUEST, f"domain {request.domain!r}")
        reading = read(request)
        if isinstance(reading, Proposal) and reading.intent not in request.allowed_intents:
            reading = "intent_not_allowed"
        if isinstance(reading, str):
            return InterpretationResult(
                interpretation_id=request.interpretation_id,
                status="abstained",
                policy_version=self._policy_version,
                model_version=self._model_version,
                diagnostics={"reason": reading},
            )
        return InterpretationResult(
            interpretation_id=request.interpretation_id,
            status="decided",
            proposal=reading,
            policy_version=self._policy_version,
            model_version=self._model_version,
        )


def read(request: InterpretationRequest) -> Proposal | str:
    """The proposal for this request, or the reason to abstain."""
    original = normalize(request.utterance)
    if not original:
        return _UNRELATED
    if IMPLICIT.search(original):
        return "implicit"
    devices = tuple(c for c in request.candidates if c.kind != SCENE_KIND)
    scenes = tuple(c for c in request.candidates if c.kind == SCENE_KIND)

    # Most specific first; every step blanks what it explained. Lock compounds,
    # device names, noise and device words share one longest-first pass, so
    # 电视剧 beats the name 电视, 车库门 beats the noise 开车, and 床头灯 beats 灯.
    text, scene_hits = _take_scenes(original, scenes)
    text, spans = take(text, _spans_table(devices))
    negated = NEGATION.search(text) is not None
    text, quantified = take(text, dict.fromkeys(QUANTIFIERS))
    text, area_hits = take(text, {normalize(a.name): a for a in request.areas})
    text, rooms = take(text, dict.fromkeys(ROOM_WORDS))
    text, values = read_values(text)
    text, queries = take_pattern(text, QUERY)
    asked = bool(queries) or QUERY_TAIL.search(text) is not None
    text, verb_hits = take(text, VERBS)

    named = [Hit(h.start, h.end, h.text, h.value[1]) for h in spans if h.value[0] == "name"]
    words: list[DeviceWord] = []
    verbs = {hit.value for hit in verb_hits}
    for hit in spans:
        kind, value = hit.value
        if kind == "word":
            words.append(value)
        elif kind == "lock":
            verbs.add(value[0])
            words.append(value[1])
    commanded = bool(verbs) or values.quantity is not None or values.mode is not None
    if not (named or words or scene_hits) and commanded and not asked:
        # 调亮一点 / 升温: the verb names the device kind; the room decides which.
        implied = IMPLIED_BY_DIMENSION.get(next(iter(verbs), Verb("set")).dimension or "")
        implied = implied or IMPLIED_BY_DIMENSION.get(values.dimension or "")
        if implied is not None:
            words.append(implied)
    mentioned = bool(named or words or scene_hits)

    if negated:
        return "negation" if mentioned or commanded else _UNRELATED
    if len(verbs) > 1:
        return "multiple_commands"
    if rooms:
        return "unknown_area"
    areas = {hit.value.area_id: hit.value for hit in area_hits}
    if len(areas) > 1:
        return "multiple_areas"
    if values.problem is not None:
        return values.problem
    area = next(iter(areas.values()), None)
    verb = next(iter(verbs), Verb("set"))

    if scene_hits:
        return _scene(scene_hits, bool(named or words), asked, verb, values)
    if not mentioned:
        if asked and SENSOR_QUERY.search(original) and (area or not WEATHER.search(original)):
            words = [SENSOR_WORD]
        elif commanded:
            return "no_target"
        else:
            return _UNRELATED

    resolved = _resolve(request, devices, named, words, bool(quantified), area)
    if isinstance(resolved, str):
        return resolved
    status, targets, mention = resolved
    if len(targets) > MAX_TARGETS:
        return "too_many_targets"
    refs = tuple(c.ref for c in targets)
    mention = mention[:MAX_MENTION] if mention else None
    if asked:
        return Proposal(intent="query", target_status=status, targets=refs, mention=mention)
    if not commanded:
        return "no_command"
    action = _action(targets, verb, values)
    if action is None:
        return "unsupported_command"
    return Proposal(
        intent="control", target_status=status, targets=refs, action=action, mention=mention
    )


def _spans_table(devices: tuple[Candidate, ...]) -> dict[str, tuple[str, object]]:
    """Lock compounds, then device names, then noise, then device words; the first
    entry for a phrase wins, so an Owner's own alias beats the generic word."""
    table: dict[str, tuple[str, object]] = {
        phrase: ("lock", value) for phrase, value in LOCK_COMPOUNDS.items()
    }
    names: dict[str, tuple[Candidate, ...]] = {}
    for candidate in devices:
        for name in (candidate.name, *candidate.aliases):
            key = normalize(name)
            if key and candidate not in names.get(key, ()):
                names[key] = (*names.get(key, ()), candidate)
    for key, candidates in names.items():
        table.setdefault(key, ("name", candidates))
    for phrase in NOISE:
        table.setdefault(phrase, ("noise", None))
    for phrase, word in DEVICE_WORDS.items():
        table.setdefault(phrase, ("word", word))
    return table


def _take_scenes(text: str, scenes: tuple[Candidate, ...]) -> tuple[str, list[Hit[Candidate]]]:
    """A scene counts only as 回家模式 / 回家场景 or right after 打开/执行…; 我回家了 is not one."""
    suffixed: dict[str, Candidate] = {}
    bare: dict[str, Candidate] = {}
    for scene in scenes:
        for name in (scene.name, *scene.aliases):
            key = normalize(name)
            if not key:
                continue
            bare[key] = scene
            if key.endswith(SCENE_SUFFIXES):
                suffixed[key] = scene
            for suffix in SCENE_SUFFIXES:
                suffixed[key + suffix] = scene
    text, hits = take(text, suffixed)
    verbs = "|".join(SCENE_VERBS)
    for key in sorted(bare, key=len, reverse=True):
        # Blank only the name: the verb stays for verb reading.
        for match in re.finditer(rf"(?:{verbs})({re.escape(key)})", text):
            start, end = match.span(1)
            hits.append(Hit(start, end, key, bare[key]))
            text = text[:start] + "#" * (end - start) + text[end:]
    return text, hits


def _scene(
    hits: list[Hit[Candidate]], other_targets: bool, asked: bool, verb: Verb, values: Values
) -> Proposal | str:
    if len({hit.value.ref for hit in hits}) > 1 or other_targets:
        return "multiple_targets"
    if asked:
        return "scene_query"
    action = command_for(SCENE_KIND, verb, values)
    if action is None:
        return "unsupported_command"
    return Proposal(
        intent="control",
        target_status="resolved",
        targets=(hits[0].value.ref,),
        action=action,
    )


def _resolve(
    request: InterpretationRequest,
    devices: tuple[Candidate, ...],
    named: list[Hit[tuple[Candidate, ...]]],
    words: list[DeviceWord],
    quantified: bool,
    area: Area | None,
) -> Resolution | str:
    origin = request.origin.area_id
    if named:
        if len({hit.value for hit in named}) > 1:
            return "multiple_targets"
        pool = named[0].value
        # A device word restating the named device (扫地机器人…开始扫地) is fine.
        if any(not any(word.matches(c) for c in pool) for word in words):
            return "multiple_targets"
        if area is not None:
            pool = tuple(c for c in pool if c.area_id == area.area_id)
            if not pool:
                return "area_conflict"
        return _narrow(pool, origin)

    pools = {tuple(c for c in devices if word.matches(c)) for word in words}
    if len(pools) > 1:
        return "multiple_targets"
    pool = pools.pop()
    word = words[0].word
    if area is not None:
        pool = tuple(c for c in pool if c.area_id == area.area_id)
        if not pool:
            return "none", (), f"{area.name}的{word}"
    elif not pool:
        return "none", (), word
    if quantified:
        return "resolved", pool, None
    if area is not None:
        return ("resolved" if len(pool) == 1 else "ambiguous"), pool, None
    return _narrow(pool, origin)


def _narrow(pool: tuple[Candidate, ...], origin: str | None) -> Resolution:
    """One match is the target; otherwise prefer the speaker's room, never pick."""
    if len(pool) == 1:
        return "resolved", pool, None
    local = tuple(c for c in pool if origin is not None and c.area_id == origin)
    if len(local) == 1:
        return "resolved", local, None
    return "ambiguous", (local if len(local) > 1 else pool), None


def _action(targets: tuple[Candidate, ...], verb: Verb, values: Values) -> Action | None:
    if not targets:
        return generic_command(verb, values)
    actions = {command_for(c.kind, verb, values) for c in targets}
    if len(actions) != 1:
        return None  # the targets would need different commands
    return actions.pop()
