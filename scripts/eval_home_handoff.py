"""Read Models fixtures; run Agent with NPU and virtual Provider only."""

import argparse
import asyncio
import json
import time
from pathlib import Path

from dotenv import load_dotenv
from eidolon_sdk.biz.interpretation import Action, Proposal, Slot
from eidolon_sdk.biz.smarthome import Registry
from eval_home_cooperation import (
    MODEL_REVISION,
    OWNER,
    FakeDirectory,
    FakeExecutor,
    HomeContext,
    LayaHomeContinuation,
    LayaInterpreter,
    MeasuredFallback,
    RecordedLLM,
    RulesInterpreter,
    SmartHomeCommand,
)

from eidolon_agent.app.runtime.bootstrap import _build_llm_router
from eidolon_agent.config.settings import load_settings

# Fixture conversion only. Production uses its current SDK registry.
GROUPS = {
    "light": "台灯 吊灯 吸顶灯 夜灯 庭院灯 感应灯 护眼台灯 灯带 筒灯 落地灯 灯",
    "climate": "中央空调 地暖 壁挂空调 恒温酒柜 挂机 柜机 空调 立式空调",
    "fan": "加湿器 新风系统 暖风机 油烟机 浴霸 空气净化器 除湿机",
    "cover": "晾衣架 电动天窗 电动晾衣架 电动窗帘 车库门",
    "water_heater": "燃气热水器 电热水器",
    "media": "投影仪 电视 背景音乐 背景音乐系统 音箱",
    "appliance": "扫地机器人 扫拖机器人 洗烘一体机 洗碗机 洗衣机 烘干机 电饭煲 蒸烤一体机 蒸箱",
    "switch": "充电桩 冰箱 净水器 喂食器 安防报警系统 智能插座 智能马桶 灌溉水阀 饮水机",
    "camera": "摄像头 猫眼摄像头",
    "sensor": "温湿度传感器 燃气报警器",
    "lock": "智能门锁 门锁",
}
TYPES = {t: k for k, vs in GROUPS.items() for t in vs.split()}
EXITS = {"多个设备或整屋", "没有对应的设备", "取消", "重新理解"}


def registry(criteria):
    areas = {}
    devices = []
    for label, desc in criteria.items():
        if label in EXITS:
            continue
        room, typ = desc.split("·", 1)
        area = areas.setdefault(room, f"room{len(areas)}")
        # Duplicate-name options include room suffixes; preserve underlying name.
        name = label.removesuffix("（" + room + "）")
        devices.append(
            dict(
                device_id=f"d{len(devices)}",
                name=name,
                type=TYPES[typ],
                area_id=area,
                provider="virtual",
            )
        )
    return Registry.model_validate(
        dict(revision=1, areas=[dict(area_id=v, name=k) for k, v in areas.items()], devices=devices)
    )


async def main(args):
    root = args.models_root
    cases = [
        json.loads(s)
        for s in (root / "evals/smart-home-continuation/c4-handoff/known-errors.jsonl")
        .read_text()
        .splitlines()
    ]
    load_dotenv(args.env_file)
    llm = _build_llm_router(load_settings(yaml_path=args.settings))
    recorded = RecordedLLM(llm)
    laya = LayaInterpreter(args.endpoint)
    rows = []
    try:
        for c in cases:
            record = next(
                json.loads(s)
                for s in (root / c["record_file"]).read_text().splitlines()
                if json.loads(s)["id"] == c["id"]
            )
            criteria = record["questions"]["device" if c["task"] == "single" else "pick"][
                "criteria"
            ]
            reg = registry(criteria)
            directory = FakeDirectory(reg)
            executor = FakeExecutor(directory)
            context = HomeContext()
            fallback = MeasuredFallback(recorded)
            if c["task"] == "pick":
                state = record["state"]["context"]
                phrase = state["待执行"]
                action = (
                    Action(trait="level", command="step", slots=(Slot(name="delta", value=10),))
                    if phrase == "调亮"
                    else Action(trait="on_off", command={"打开": "on", "关闭": "off"}[phrase])
                )
                context.remember(
                    state["上一句"],
                    Proposal(
                        intent="control",
                        target_status="ambiguous",
                        targets=tuple(d.device_id for d in reg.devices),
                        action=action,
                    ),
                    question=state["Agent"],
                    pending_action=phrase,
                )
            command = SmartHomeCommand(
                directory=directory,
                executor=executor,
                interpreter=laya,
                fallback=fallback,
                independent_interpreter=RulesInterpreter(require_complete=True),
                min_confidence=0.8,
                continuation=LayaHomeContinuation(laya, revision=MODEL_REVISION),
            )
            before = context.snapshot()
            start = time.perf_counter()
            result = await command.handle(
                OWNER,
                None,
                c["id"].replace("/", ":"),
                record["state"]["utterance"],
                context=context,
            )
            row = {
                "id": c["id"],
                "task": c["task"],
                "utterance": record["state"]["utterance"],
                "expected": c["expected_full_chain"],
                "gold": c["gold"],
                "registry": reg.model_dump(mode="json"),
                "context_before": before,
                "result": result.model_dump(mode="json"),
                "ms": round((time.perf_counter() - start) * 1000, 2),
                "llm_calls": fallback.calls,
                "commands": executor.commands,
                "context_after": context.snapshot(),
            }
            rows.append(row)
            print(
                json.dumps(
                    {k: row[k] for k in ("id", "utterance", "result", "llm_calls", "commands")},
                    ensure_ascii=False,
                ),
                flush=True,
            )
    finally:
        await laya.aclose()
        await llm.close()
    args.output.write_text(
        json.dumps(
            {
                "fixture_note": "Model product types mapped to SDK capability types; not byte-identical model-only inputs. Full mapped registries retained. Gold not sent to models.",
                "type_mapping": TYPES,
                "rows": rows,
                "llm_exchanges": recorded.exchanges,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-root", type=Path, required=True)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
