"""唯一工具执行安全网关；模型、手动工具、工作流共享不可绕过的闸门。
仅审批/处罚决定类工具（sensitive）需人工确认；其余白名单写入直接执行并全程留痕。"""
import json
from uuid import uuid4
from .catalog import TOOLS
from .store import digest,dumps,now
from .upstream import UpstreamError
from .vision import VisionError

class PendingApproval(Exception):
    def __init__(self,payload):
        self.payload=payload
class ReconciliationRequired(Exception):
    pass

WARNING_REQUIRED_STATUS={'warning_apply':1,'warning_approve':2}
WARNING_STATUS_NAME={1:'待反馈',2:'已申诉',3:'已处理',4:'已驳回'}

class Gateway:
    def __init__(self,store,upstream,vision=None):
        self.store,self.upstream,self.vision=store,upstream,vision
    async def precheck_warning_state(self,rid,owner,name,args):
        """创建审批前核验上游状态；仅确定性状态冲突时阻断，未找到/未查全/异常交由上游裁定。"""
        required=WARNING_REQUIRED_STATUS.get(name)
        if required is None:return
        try:
            token=await self.store.owner_token(owner)
        except ValueError:
            return
        target=str(args.get('warnId') if name=='warning_apply' else args.get('approveRecodeId'))
        try:
            result=await self.upstream.call(TOOLS['warning_scan'],{'match':{'id':target}},token)
        except Exception:
            return
        records=(result.get('data') or {}).get('list') or []
        if not records:return
        status=records[0].get('status')
        if status==required:return
        await self.store.event(rid,'tool.precheck_blocked',{'tool':name,'warn_id':target,'current_status':status,'required_status':required})
        action='申诉/处理' if name=='warning_apply' else '审核'
        raise ValueError(f'前置状态校验失败：预警事件{target}当前状态为「{WARNING_STATUS_NAME.get(status,status)}」，仅「{WARNING_STATUS_NAME[required]}」事件可{action}；未进入人工审批，未发送写入')
    async def prepare(self,rid,owner,name,args,op_key):
        if name not in TOOLS:
            raise ValueError('工具不在白名单内')
        spec=TOOLS[name]
        if spec.risk=='auth':
            raise ValueError('认证工具只能在登录流程使用，禁止模型获取凭据')
        args=spec.input_model.model_validate(args).model_dump(exclude_none=True)
        await self.precheck_warning_state(rid,owner,name,args)
        if spec.risk=='sensitive':
            hashed=digest({'run_id':rid,'owner_id':owner,'operation_key':op_key,'tool':name,'arguments':args})
            approval=await self.store.one('SELECT * FROM approvals WHERE operation_key=?',(op_key,))
            if not approval:
                approval_id=uuid4().hex
                reason='审批/处罚决定必须人工确认'
                await self.store.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(approval_id,rid,op_key,owner,name,self.store.fernet.encrypt(dumps(args).encode()).decode(),reason,hashed,'pending',now(),None,None))
                await self.store.event(rid,'approval.requested',{'approval_id':approval_id,'tool':name,'arguments_hash':digest(args),'payload_hash':hashed,'reason':reason})
                approval=await self.store.one('SELECT * FROM approvals WHERE id=?',(approval_id,))
            if approval['payload_hash']!=hashed:
                raise ValueError('审批载荷已发生变化，禁止复用旧批准')
            if approval['status']=='rejected':
                return spec,args,False
            if approval['status']!='approved':
                raise PendingApproval(dict(approval_id=approval['id'],tool_name=name,arguments=args,reason=approval['reason'],payload_hash=hashed))
        return spec,args,True
    async def execute(self,rid,owner,name,args,op_key):
        """在批准完成后再次校验闸门；账本阻止崩溃/重放造成重复写入。"""
        spec,args,allowed=await self.prepare(rid,owner,name,args,op_key)
        if not allowed:
            return {'code':403,'msg':'人工拒绝，未执行工具','data':None,'human_rejected':True}
        args_hash=digest(args)
        old=await self.store.one('SELECT * FROM operations WHERE key=?',(op_key,))
        if old:
            if old['args_hash']!=args_hash:
                raise ValueError('操作幂等键参数不一致')
            if old['status']=='completed':
                return json.loads(self.store.fernet.decrypt(old['result_cipher'].encode()))
            if old['status'] in ('started','uncertain') and spec.risk in ('write','sensitive'):
                raise ReconciliationRequired('上次写请求可能已执行；禁止重试，请到平台核对并联系管理员')
            if old['status']=='failed':
                raise UpstreamError(422,old['error'] or '工具已失败，不自动重复',422)
        # 平台会话读取在发请求账本之前，过期不会被误标不确定。
        if not old:
            await self.store.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?)',(op_key,rid,name,args_hash,'started',None,None))
        await self.store.event(rid,'tool.started',{'tool':name,'path':spec.path,'arguments_hash':args_hash,'operation_key':op_key})
        try:
            if name=='video_snapshot':
                if not self.vision:
                    raise VisionError('视觉服务未初始化',503)
                result=await self.vision.capture_snapshot(rid,owner,args['channelId'],self.upstream)
            elif name.startswith('vision_'):
                if not self.vision:
                    raise VisionError('视觉服务未初始化',503)
                result=await self.vision.analyze(rid,owner,name,args)
            else:
                token=await self.store.owner_token(owner)
                async def page_audit(payload):
                    await self.store.event(rid,'tool.lookup.page',{'tool':name,'operation_key':op_key,**payload})
                result=await self.upstream.call(spec,args,token,audit=page_audit)
        except (UpstreamError,VisionError) as e:
            uncertain=getattr(e,'uncertain',False)
            status='uncertain' if uncertain else 'failed'
            await self.store.execute('UPDATE operations SET status=?,error=? WHERE key=?',(status,e.message,op_key))
            await self.store.event(rid,'tool.'+status,{'tool':name,'code':e.code,'reason':e.message})
            if uncertain:
                raise ReconciliationRequired(e.message) from e
            raise
        encrypted=self.store.fernet.encrypt(dumps(result).encode()).decode()
        await self.store.execute("UPDATE operations SET status='completed',result_cipher=? WHERE key=?",(encrypted,op_key))
        await self.store.event(rid,'tool.completed',{'tool':name,'arguments':args,'result':result,'evidence_hash':digest(result),'operation_key':op_key})
        return result
