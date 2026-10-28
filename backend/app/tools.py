"""工具层：与参考实现 agent/tools.py 相同的形态——每个白名单工具一个 @tool 函数。
docstring 含逐参数说明（类型/必填/默认/枚举语义），模型经 bind_tools 绑定，
图内由 ToolNode（tool_node 节点）统一执行；函数体统一经安全网关，
保留人工审批闸门、幂等账本与审计约束。run_id/Token/审批字段不允许模型提供。
"""
from contextvars import ContextVar
from dataclasses import dataclass
from langchain_core.tools import tool
from .catalog import TOOLS
from .store import dumps

@dataclass(frozen=True)
class ExecutionContext:
    run_id: str
    owner: str
    operation_key: str

CONTEXT=ContextVar('trusted_tool_execution',default=None)

def _type_label(annotation):
    """把类型注解压成模型可读的短标签，去掉 typing 噪声与可选 None。"""
    label=str(annotation)
    for token in ('typing.','<class ','>'):
        label=label.replace(token,'')
    label=label.replace('NoneType','null').replace(' | None','').replace('None | ','')
    return label.replace("'",'').replace('"','')

def describe_input(model):
    """逐字段给出类型/必填/默认/语义，让模型不靠试错就能正确构造查询与修改入参。
    说明全部来自既有契约，避免双份维护。"""
    fields=getattr(model,'model_fields',None) or {}
    if not fields:
        return '无入参。'
    lines=[]
    for name,field in fields.items():
        if field.is_required():
            requirement='必填'
        else:
            requirement='可选' if field.default is None else f'可选，默认{field.default}'
        note=field.description or ''
        lines.append(f'{name}（{_type_label(field.annotation)}，{requirement}）{note}')
    return '入参说明：'+'；'.join(lines)+'。'

def build_agent_tools(gateway):
    """为全部可执行白名单工具生成 @tool 函数；认证工具仅登录页使用，未开放工具不注册。"""
    def make(spec):
        async def run(**arguments):
            context=CONTEXT.get()
            if context is None:
                raise ValueError('工具仅可在已认证的服务端执行上下文使用')
            result=await gateway.execute(context.run_id,context.owner,spec.name,arguments,context.operation_key)
            return dumps(result)
        run.__name__=spec.name
        run.__doc__=spec.description+' '+describe_input(spec.input_model)
        return tool(spec.name,description=run.__doc__,args_schema=spec.input_model)(run)
    return {s.name:make(s) for s in TOOLS.values() if s.available and s.risk!='auth'}
