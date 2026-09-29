# Synthetic-only latency probe. The outbound catalog comes exclusively from
# eidolon_sdk.biz.smarthome.samples.apartment, not Hub/Data or user history.
# Utterances and contexts below are hand-authored evaluation fixtures.
import asyncio, json, time, sys, dataclasses, statistics, logging, os
from pathlib import Path
import argparse

parser = argparse.ArgumentParser(
    description="Synthetic-only home LLM latency probe; never executes devices."
)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--repeats", type=int, default=2)
parser.add_argument(
    "--http1", action="store_true", help="HTTP/1.1 comparison in a separate process"
)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=False)
logging.basicConfig(
    filename=args.output / "transport.log",
    level=logging.WARNING,
    format="%(asctime)s %(name)s %(message)s",
)
logging.getLogger("eidolon_agent.infra.llm").setLevel(logging.INFO)
from contextvars import ContextVar

active_measurement = ContextVar("active_measurement", default=None)
ROOT = Path(os.environ.get("PROBE_ROOT", "/Users/manson/ai/eidolon"))
sys.path.insert(0, str(ROOT / "eidolon_agent"))
from dotenv import load_dotenv
from eidolon_agent.config.settings import load_settings
from eidolon_agent.app.runtime.bootstrap import _build_llm_router
from eidolon_agent.infra.smarthome.llm_fallback import LlmHomeFallback
from eidolon_agent.domain.smarthome.context import HomeContext, HomeCancellation, HomeClarification
from eidolon_agent.domain.smarthome.command import interpretation_request
from eidolon_sdk.biz.smarthome.samples import apartment
from eidolon_sdk.biz.interpretation import Action, Proposal

for env_file in os.environ.get("PROBE_ENV_FILES", str(ROOT / ".eidolon/mac-product/config/product-source.env")).split(":"):
    load_dotenv(env_file)


def ctx(ref, command, pending=False):
    c = HomeContext()
    c.remember(
        "关闭主灯" if pending else "打开设备",
        Proposal(
            intent="control",
            target_status="ambiguous" if pending else "resolved",
            targets=ref,
            action=Action(trait="on_off", command=command),
        ),
        question="客厅还是主卧？" if pending else None,
    )
    return c.snapshot()


pending = ctx(("living.main_light", "master.light"), "off", True)
CASES = [
    ("客厅那个", pending, "control", ("living.main_light",), "off"),
    ("主卧那个", pending, "control", ("master.light",), "off"),
    ("算了", pending, "cancel", (), None),
    ("打开主卧灯", pending, "control", ("master.light",), "on"),
    ("不要关闭主卧灯", ctx(("master.light",), "on"), "cancel", (), None),
    ("打开它", ctx(("living.main_light",), "off"), "control", ("living.main_light",), "on"),
    ("打开它", None, "clarification", (), None),
    ("关闭主灯", None, "ambiguous", ("living.main_light", "master.light"), "off"),
    ("今天天气怎么样", pending, "unrelated", (), None),
    ("打开客厅灯，不，关掉", None, "control", ("living.main_light",), "off"),
]


class Measured:
    def __init__(self, llm):
        self.llm = llm
        self.rows = []

    async def stream(self, messages, **kwargs):
        row = {"input_chars": sum(len(m.content) for m in messages), "usage": None}
        self.rows.append(row)
        t = time.perf_counter()
        active_measurement.set((row, t))
        async for d in self.llm.stream(messages, **kwargs):
            if d.usage:
                row["usage"] = dataclasses.asdict(d.usage)
            if (d.tool_call or d.text_delta) and "first_effective_ms" not in row:
                row["first_effective_ms"] = round((time.perf_counter() - t) * 1000, 1)
            yield d


async def main():
    from eidolon_agent.infra.llm.providers.litellm_provider import _ensure_shared_client

    if args.http1:
        import httpx
        from eidolon_agent.infra.llm.providers import litellm_provider as transport

        transport._shared_http_client = httpx.AsyncClient(
            http2=False,
            limits=httpx.Limits(
                max_connections=100, max_keepalive_connections=20, keepalive_expiry=60
            ),
            follow_redirects=True,
        )
    client = _ensure_shared_client()

    async def request_hook(request):
        current = active_measurement.get()
        if current is None:
            return
        row, t = current
        attempt = {"start_ms": round((time.perf_counter() - t) * 1000, 1), "events": []}
        row.setdefault("http_attempts", []).append(attempt)

        async def trace(event, info):
            # No URL, body, headers or connection details enter the evidence.
            attempt["events"].append(
                {"event": event, "elapsed_ms": round((time.perf_counter() - t) * 1000, 1)}
            )

        request.extensions["trace"] = trace

    async def response_hook(response):
        current = active_measurement.get()
        if current is not None:
            row, t = current
            row["http_attempts"][-1].update(
                status=response.status_code,
                http_version=response.http_version,
                headers_ms=round((time.perf_counter() - t) * 1000, 1),
            )

    client.event_hooks["request"].append(request_hook)
    client.event_hooks["response"].append(response_hook)
    llm = _build_llm_router(
        load_settings(yaml_path=Path(os.environ.get("PROBE_SETTINGS", str(ROOT / ".eidolon/mac-product/config/settings/agent.yaml"))))
    )
    measured = Measured(llm)
    variants = {"production": LlmHomeFallback(measured)}
    rows = []
    try:
        for rep in range(args.repeats):
            for i, (u, context, intent, targets, verb) in enumerate(CASES):
                for name in ["production"]:
                    req = interpretation_request(
                        apartment(),
                        interpretation_id=f"catalog-{name}-{rep}-{i}",
                        utterance=u,
                        device_ref=None,
                        timeout_ms=800,
                    )
                    t = time.perf_counter()
                    error = None
                    p = None
                    measurement_index = len(measured.rows)
                    try:
                        async with asyncio.timeout(8):
                            p = await variants[name].propose(req, context=context)
                    except Exception as e:
                        error = type(e).__name__
                    if isinstance(p, HomeCancellation):
                        actual = "cancel"
                        out = {"type": actual}
                        correct = intent == "cancel"
                    elif isinstance(p, HomeClarification):
                        actual = "clarification"
                        out = {
                            "question": p.question,
                            "targets": p.targets,
                            "action": p.action.model_dump(mode="json") if p.action else None,
                        }
                        correct = intent == "clarification" or (
                            intent == "ambiguous"
                            and set(p.targets) == set(targets)
                            and p.action
                            and p.action.command == verb
                        )
                    elif isinstance(p, Proposal):
                        out = p.model_dump(mode="json")
                        actual = p.intent
                        correct = (
                            (
                                actual == intent
                                or intent == "ambiguous"
                                and p.target_status == "ambiguous"
                            )
                            and set(p.targets) == set(targets)
                            and (p.action.command if p.action else None) == verb
                        )
                    else:
                        out = None
                        correct = False
                    row = {
                        "variant": name,
                        "repeat": rep,
                        "case": i,
                        "utterance": u,
                        "elapsed_ms": round((time.perf_counter() - t) * 1000, 1),
                        "correct": bool(correct),
                        "result": out,
                        "error": error,
                        **(measured.rows[-1] if len(measured.rows) > measurement_index else {}),
                    }
                    rows.append(row)
                    print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        await llm.close()
    (args.output / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    for name in variants:
        a = [x for x in rows if x["variant"] == name]
        v = sorted(x["elapsed_ms"] for x in a)
        print(
            name,
            "passed",
            sum(x["correct"] for x in a),
            "/",
            len(a),
            "p50",
            statistics.median(v),
            "p95",
            v[int(0.95 * len(v)) - 1],
            "max",
            max(v),
        )


asyncio.run(main())
