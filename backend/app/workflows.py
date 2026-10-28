"""可复制工作流：版本化JSON、只读模板、类型化参数及安全引用，无eval/代码执行。"""
import re
import json
from uuid import uuid4
from pydantic import BaseModel,ConfigDict,Field,model_validator
from .catalog import TOOLS
from .store import dumps

class Step(BaseModel):
    model_config=ConfigDict(extra='forbid')
    tool: str = Field(description='白名单工具名')
    arguments: dict = Field(default_factory=dict,description='工具参数，可引用parameters和前序steps')
    @model_validator(mode='after')
    def valid_tool(self):
        if self.tool not in TOOLS or TOOLS[self.tool].risk in ('auth','unavailable'):
            raise ValueError('流程只能使用已开放的业务工具')
        return self

class WorkflowDefinition(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name: str = Field(min_length=1,max_length=100)
    description: str = Field(default='',max_length=2000)
    steps: list[Step] = Field(min_length=1,max_length=20)
    parameters: dict = Field(default_factory=dict)

BUILTINS=[
 {'id':'warning_overview','name':'预警概览与监管台账','description':'自动获取机构、预警统计、事件列表与字典，形成可溯源台账；没有处罚决定。','version':1,'owner_id':None,
 'parameters':{'keyword':'','pageSize':20},'steps':[
 {'tool':'current_agencies','arguments':{}},{'tool':'warning_status_count','arguments':{}},
 {'tool':'warning_list','arguments':{'pageNum':1,'pageSize':'${parameters.pageSize}','keyword':'${parameters.keyword}'}},
 {'tool':'warning_types','arguments':{}}]},
 {'id':'camera_inventory','name':'设备巡检元数据台账','description':'查询单位与摄像头元数据，核查在线状态；不是视频AI识别，不宣称取到视频。','version':1,'owner_id':None,
 'parameters':{'keyword':'','pageSize':20},'steps':[
 {'tool':'shop_list','arguments':{'pageNum':1,'pageSize':'${parameters.pageSize}','keyword':'${parameters.keyword}'}},
 {'tool':'camera_list','arguments':{'pageNum':1,'pageSize':'${parameters.pageSize}','keyword':'${parameters.keyword}'}}]},
]

REFERENCE=re.compile(r'^\$\{((?:parameters|steps)(?:\.[A-Za-z0-9_]+)+)\}$')
def resolve(value,parameters,results):
    """整个参数值占位保留原始类型；仅允许字典键/列表索引访问，禁止属性和表达式。"""
    if isinstance(value,dict):
        return {k:resolve(v,parameters,results) for k,v in value.items()}
    if isinstance(value,list):
        return [resolve(v,parameters,results) for v in value]
    if isinstance(value,str) and value.startswith('${'):
        match=REFERENCE.fullmatch(value)
        if not match:
            raise ValueError('非法引用表达式；仅支持 ${parameters.key} / ${steps.0.data.id}')
        parts=match.group(1).split('.')
        current=parameters if parts.pop(0)=='parameters' else results
        for part in parts:
            if isinstance(current,list) and part.isdigit():
                current=current[int(part)]
            elif isinstance(current,dict) and part in current:
                current=current[part]
            else:
                raise ValueError('引用不存在或访问了尚未执行的步骤')
        return current
    return value

async def list_workflows(store,owner):
    rows=await store.fetch('SELECT * FROM workflows WHERE owner_id=?',(owner,))
    return BUILTINS+[dict(json.loads(r['definition']),id=r['id'],owner_id=r['owner_id'],version=r['version']) for r in rows]

async def find_workflow(store,owner,wid):
    return next((w for w in await list_workflows(store,owner) if w['id']==wid),None)

async def save_workflow(store,owner,definition):
    wid=uuid4().hex
    obj=definition.model_dump()
    # 创建时对无引用的输入直接校验，引用参数在运行前再验证。
    for step in obj['steps']:
        if '${' not in dumps(step['arguments']):
            TOOLS[step['tool']].input_model.model_validate(step['arguments'])
    await store.execute('INSERT INTO workflows VALUES(?,?,?,?,?,?)',(wid,owner,obj['name'],obj['description'],1,dumps(obj)))
    await store.event('workflow:'+wid,'workflow.created',{'owner_id':owner,'definition':obj,'version':1})
    return dict(obj,id=wid,owner_id=owner,version=1)
