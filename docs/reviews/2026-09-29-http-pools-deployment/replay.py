import asyncio,json,time,sys,dataclasses,os
from pathlib import Path
ROOT=Path(os.environ.get("PROBE_ROOT", "/Users/manson/ai/eidolon"))
sys.path.insert(0,str(ROOT/'eidolon_agent'))
from dotenv import load_dotenv
from eidolon_agent.config.settings import load_settings
from eidolon_agent.app.runtime.bootstrap import _build_llm_router
from eidolon_agent.infra.smarthome.llm_fallback import LlmHomeFallback
from eidolon_agent.infra.interpretation import LayaInterpreter,RulesInterpreter
from eidolon_agent.domain.smarthome.command import SmartHomeCommand
from eidolon_agent.domain.smarthome.context import HomeContext
from probe_fakes import FakeDirectory,FakeExecutor,home_registry,OWNER
for env_file in os.environ.get("PROBE_ENV_FILES",str(ROOT/".eidolon/mac-product/config/product-source.env")).split(":"):load_dotenv(env_file)
SCENARIOS=[
 ('curtain',[('打开窗帘','executed','living.curtain','open'),('关闭窗帘','executed','living.curtain','close'),('打开它','executed','living.curtain','open')]),
 ('selection',[('关闭主灯','ambiguous',None,None),('客厅那个','executed','living.main_light','off'),('再暗一点','executed','living.main_light','step')]),
 ('cancel',[('关闭主灯','ambiguous',None,None),('算了','answered',None,None),('打开它','clarification',None,None)]),
 ('replace',[('关闭主灯','ambiguous',None,None),('打开主卧灯','executed','master.light','on'),('不要关闭主卧灯','answered',None,None)]),
 ('missing',[('打开它','clarification',None,None)]),
 ('correction',[('打开客厅灯，不，关掉','executed','living.main_light','off')]),
 ('new_topic',[('打开窗帘','executed','living.curtain','open'),('今天天气怎么样','unrelated',None,None),('打开它','clarification',None,None)]),
]
class Measured:
 def __init__(self,port):self.port=port;self.calls=[]
 async def interpret(self,request):
  t=time.perf_counter()
  try:
   r=await self.port.interpret(request);self.calls.append({'ms':(time.perf_counter()-t)*1000,'status':r.status,'diagnostics':r.diagnostics});return r
  except Exception as e:self.calls.append({'ms':(time.perf_counter()-t)*1000,'error':type(e).__name__});raise
 async def propose(self,request,*,context=None):
  t=time.perf_counter()
  try:
   r=await self.port.propose(request,context=context);self.calls.append({'ms':(time.perf_counter()-t)*1000,'context':context is not None,'result_type':type(r).__name__});return r
  except Exception as e:self.calls.append({'ms':(time.perf_counter()-t)*1000,'error':type(e).__name__});raise
async def main():
 llm=_build_llm_router(load_settings(yaml_path=Path(os.environ.get("PROBE_SETTINGS",str(ROOT/".eidolon/mac-product/config/settings/agent.yaml")))))
 laya=LayaInterpreter('http://127.0.0.1:8771'); small=Measured(laya); big=Measured(LlmHomeFallback(llm));rows=[]
 try:
  for name,turns in SCENARIOS:
   d=FakeDirectory(home_registry());e=FakeExecutor(d);context=HomeContext()
   command=SmartHomeCommand(directory=d,executor=e,interpreter=small,fallback=big,min_confidence=.8,independent_interpreter=RulesInterpreter(require_complete=True))
   for i,(text,outcome,target,verb) in enumerate(turns):
    ns,nb,nc=len(small.calls),len(big.calls),len(e.commands);t=time.perf_counter()
    r=await command.handle(OWNER,'unplaced',f'coop-{name}-{i}',text,context=context)
    commands=e.commands[nc:];correct=r.outcome==outcome and (not commands if target is None else len(commands)==1 and commands[0][0]==target and commands[0][2]==verb)
    row={'scenario':name,'utterance':text,'expected':{'outcome':outcome,'target':target,'verb':verb},'result':r.model_dump(mode='json'),'commands':commands,'correct':correct,'elapsed_ms':round((time.perf_counter()-t)*1000,1),'laya':small.calls[ns:],'llm':big.calls[nb:]}
    rows.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
 finally:await laya.aclose();await llm.close()
 Path(os.environ["PROBE_OUTPUT"]).write_text(json.dumps({'real_models':True,'production_device_execution':False,'results':rows},ensure_ascii=False,indent=2))
 print('passed',sum(x['correct'] for x in rows),'/',len(rows))
asyncio.run(main())
