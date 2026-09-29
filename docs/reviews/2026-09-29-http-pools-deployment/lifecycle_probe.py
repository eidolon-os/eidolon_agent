"""Read-only deployed Laya probe, with synthetic inference and caller cancellation."""
import asyncio, json, time, os
from pathlib import Path
from dotenv import load_dotenv
from eidolon_sdk.core.http import create_async_client
from eidolon_sdk.biz.smarthome.samples import apartment
from eidolon_agent.domain.smarthome.command import interpretation_request
from eidolon_agent.infra.interpretation import LayaInterpreter
for f in os.environ.get('PROBE_ENV_FILES','').split(':'):
    if f: load_dotenv(f)
async def main():
    events=[]
    async def trace(name,info): events.append(name)
    async def hook(request): request.extensions['trace']=trace
    async with create_async_client(timeout=5,trust_env=False,event_hooks={'request':[hook]}) as http:
        statuses=[]
        for _ in range(5):
            r=await http.get('http://127.0.0.1:8771/readyz'); statuses.append(r.status_code)
        initial_connects=events.count('connection.connect_tcp.started')
        await asyncio.sleep(6)
        statuses.append((await http.get('http://127.0.0.1:8771/readyz')).status_code)
        after_idle_connects=events.count('connection.connect_tcp.started')
    laya=LayaInterpreter('http://127.0.0.1:8771')
    req=interpretation_request(apartment(),interpretation_id='synthetic-pool-recovery',utterance='打开窗帘',device_ref=None,timeout_ms=800)
    cancelled=False; rows=[]
    try:
        try:
            async with asyncio.timeout(0): await laya.interpret(req)
        except TimeoutError: cancelled=True
        await asyncio.sleep(1)
        for _ in range(3):
            t=time.perf_counter(); result=await laya.interpret(req)
            rows.append({'ms':round((time.perf_counter()-t)*1000,1),'status':result.status,'diagnostics':result.diagnostics})
    finally: await laya.aclose()
    print(json.dumps({'health_statuses':statuses,'initial_connections':initial_connects,'connections_after_idle':after_idle_connects,'closed':http.is_closed,'cancelled':cancelled,'recovery':rows},ensure_ascii=False))
asyncio.run(main())
