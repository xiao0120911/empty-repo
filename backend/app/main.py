"""FastAPI 应用：账号登录、工具目录、运行、人工审批、工作流及审计导出。"""
import asyncio
import hashlib
import hmac
import json
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from uuid import uuid4
from fastapi import FastAPI,APIRouter,Depends,HTTPException,Request,Response,UploadFile,File,Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel,ConfigDict,Field,ValidationError
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from .config import Settings
from .catalog import TOOLS,catalog_json
from .contracts import AlertAcknowledge,VideoChannelQuery,VideoMonitorCreate,VideoMonitorUpdate,VideoSnapshot
from .upstream import UpstreamClient,UpstreamError
from .store import Store,dumps,now,redact,digest
from .security import password_check
from .gateway import Gateway
from .agent import Engine,initial_state
from .intents import exact_warning_id,is_format_only,camera_number_query
from .reports import display_result
from .workflows import WorkflowDefinition,list_workflows,find_workflow,save_workflow
from .vision import VisionService,VisionError
from .monitoring import MonitorService

class LoginRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    account:str=Field(min_length=1,max_length=128)
    password:str=Field(min_length=1,max_length=256)
    key:str=Field(min_length=1,max_length=256)
    verifyCode:str=Field(min_length=4,max_length=4)
class RunRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    prompt:str=Field(min_length=1,max_length=8000)
    workflow_id:str|None=None
    conversation_id:str|None=Field(None,pattern=r'^[a-f0-9]{32}$')
    parameters:dict=Field(default_factory=dict)
    idempotency_key:str=Field(min_length=8,max_length=128)
class ToolRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    arguments:dict=Field(default_factory=dict)
    idempotency_key:str=Field(min_length=8,max_length=128)
class ResumeRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    decision:str=Field(pattern='^(approve|reject)$')
    approval_id:str
    payload_hash:str
    note:str=Field(min_length=2,max_length=2000)


def create_app(settings=None,upstream_transport=None,vision_transport=None):
    settings=settings or Settings()
    store=Store(settings)
    upstream=UpstreamClient(settings,upstream_transport)
    vision=VisionService(settings,store,vision_transport)
    monitor=MonitorService(settings,store,upstream,vision)
    tasks=set()
    locks=defaultdict(asyncio.Lock)
    attempts=defaultdict(list)

    @asynccontextmanager
    async def lifespan(app):
        await store.open(recover=True)
        async with AsyncSqliteSaver.from_conn_string(str(settings.data_dir/'checkpoints.sqlite3')) as saver:
            await saver.setup()
            app.state.engine=Engine(settings,store,Gateway(store,upstream,vision),saver)
            if settings.video_monitor_enabled:
                task=asyncio.create_task(monitor.loop())
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            yield
            if tasks:
                for task in list(tasks):
                    task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
        await upstream.aclose()
        await vision.aclose()
        await store.close()

    app=FastAPI(title='市场监管自主执行智能体',version='1.4.0',lifespan=lifespan,
                docs_url=settings.api_prefix+'/docs',openapi_url=settings.api_prefix+'/openapi.json')
    app.state.store=store
    app.state.vision=vision
    app.state.monitor=monitor
    app.add_middleware(CORSMiddleware,allow_origins=settings.origins,allow_credentials=True,
                       allow_methods=['GET','POST'],allow_headers=['Content-Type','X-CSRF-Token'])

    @app.middleware('http')
    async def protection(request,call_next):
        forwarded_proto=request.headers.get('x-forwarded-proto',request.url.scheme).split(',',1)[0].strip().lower()
        if settings.require_https and request.url.path!=settings.api_prefix+'/health' and forwarded_proto!='https':
            return JSONResponse({'detail':'此环境仅接受 HTTPS 请求'},426)
        # Multipart includes boundary and form fields beyond the image bytes; save_upload enforces the exact file limit.
        limit=settings.vision_max_image_bytes+64*1024 if request.url.path==settings.api_prefix+'/vision/images' else settings.max_request_bytes
        if request.headers.get('content-length','0').isdigit() and int(request.headers.get('content-length','0'))>limit:
            return JSONResponse({'detail':'请求超过大小限制'},413)
        # Browser write requests must originate from an explicitly allowed origin.
        # Non-browser clients still need the session-bound CSRF token in identity().
        origin=request.headers.get('origin')
        if request.method in ('POST','PUT','PATCH','DELETE') and origin and origin not in settings.origins:
            return JSONResponse({'detail':'来源不在允许列表'},403)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Cache-Control']='no-store'
        response.headers['Referrer-Policy']='same-origin'
        if settings.require_https:
            response.headers['Strict-Transport-Security']='max-age=31536000; includeSubDomains'
        return response

    @app.exception_handler(UpstreamError)
    async def upstream_error(request,e):
        return JSONResponse({'detail':e.message,'code':e.code,'uncertain':e.uncertain},status_code=e.http_status)

    async def identity(request:Request):
        session=await store.session(request.cookies.get('agent_session',''))
        if not session:
            raise HTTPException(401,'请登录；会话可能已过期')
        if request.method=='POST':
            csrf=request.headers.get('x-csrf-token','')
            if not csrf or not hmac.compare_digest(csrf,session['csrf']):
                raise HTTPException(403,'CSRF校验失败，请刷新登录状态')
        return session

    def user_view(s):
        return {'id':s['user_id'],'account':s['account'],'nickname':s['nickname'],'role':s['role']}

    async def visible_run(rid,s):
        run=await store.run(rid)
        if not run:
            raise HTTPException(404,'任务不存在')
        if run['owner_id']!=s['user_id'] and s['role'] not in ('admin','reviewer'):
            raise HTTPException(403,'无权访问其他用户任务')
        return run

    async def background(rid,inputs=None,resume=None):
        async with locks[rid]:
            try:
                if inputs is None and resume is None:
                    snapshot=await app.state.engine.graph.aget_state({'configurable':{'thread_id':rid}})
                    if snapshot.interrupts:
                        payload=snapshot.interrupts[0].value
                        approval=await store.one("SELECT id,status FROM approvals WHERE id=? AND run_id=? AND payload_hash=? AND status IN ('approved','rejected')",(payload['approval_id'],rid,payload['payload_hash']))
                        if approval:
                            resume={'approval_id':approval['id'],'decision':'approve' if approval['status']=='approved' else 'reject'}
                    if not snapshot.values:
                        row=await store.one('SELECT * FROM runs WHERE id=?',(rid,))
                        saved=json.loads(row['input'])
                        steps=[]
                        params=saved.get('parameters',{})
                        if row['mode']=='tool':
                            steps=[{'tool':saved['tool'],'arguments':saved['arguments']}]
                        elif row['mode']=='workflow':
                            workflow=await find_workflow(store,row['owner_id'],saved['workflow_id'])
                            if not workflow:
                                raise ValueError('恢复所需工作流不存在')
                            steps=workflow['steps']
                            params={**workflow['parameters'],**params}
                        inputs=initial_state(rid,row['owner_id'],row['prompt'],row['mode'],steps,params,await store.load_run_context(rid))
                await store.set_run(rid,'running')
                await app.state.engine.invoke(inputs,rid,resume)
            except asyncio.CancelledError:
                await store.set_run(rid,'interrupted',error='服务停止，等待人工恢复')
                raise
            except Exception as e:
                # 不输出密钥、HTTP原始错误或模型隐藏推理内容。
                message=str(e) if isinstance(e,(ValueError,UpstreamError)) else '执行失败，请检查服务配置或查看脱敏审计记录'
                await store.set_run(rid,'failed',error=message)
                await store.event(rid,'task.failed',{'reason':message,'exception_type':type(e).__name__})
    def schedule(rid,inputs=None,resume=None):
        task=asyncio.create_task(background(rid,inputs,resume))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    router=APIRouter(prefix=settings.api_prefix)
    @router.get('/health')
    async def health():
        return {'status':'ok','environment':settings.app_env,'model':settings.deepseek_model,
                'mode':'mock-integration' if settings.upstream_mock else 'platform-integration',
                'features':{'llm_configured':bool(settings.deepseek_api_key),'vision_enabled':vision.available,
                            'video_monitoring':settings.video_monitor_enabled,'durable_checkpoints':True,'human_review':True}}
    @router.post('/auth/captcha')
    async def captcha(request:Request):
        key='captcha:'+str(request.client.host)
        stamp=time.monotonic()
        attempts[key]=[v for v in attempts[key] if stamp-v<60]
        if len(attempts[key])>=20:
            raise HTTPException(429,'验证码请求过于频繁')
        attempts[key].append(stamp)
        result=await upstream.call(TOOLS['get_graphic_code'],{})
        if not settings.upstream_mock and result.get('data'):
            result['data'].pop('_mockCode',None)
        return result
    @router.post('/auth/login')
    async def login(body:LoginRequest,request:Request,response:Response):
        limiter=str(request.client.host)+':'+body.account
        stamp=time.monotonic()
        attempts[limiter]=[v for v in attempts[limiter] if stamp-v<300]
        if len(attempts[limiter])>=5:
            raise HTTPException(429,'登录失败过多，5分钟后再试')
        user=await store.one('SELECT * FROM users WHERE account=?',(body.account,))
        if not user or not password_check(body.password,user['password_hash']):
            attempts[limiter].append(stamp)
            await store.event('auth','auth.failed',{'account_hash':hashlib.sha256(body.account.encode()).hexdigest()})
            raise HTTPException(401,'账号或密码不正确；需先由管理员创建本应用账号')
        if settings.enforce_password_policy:
            from .security import password_policy_error
            error=password_policy_error(body.password)
            if error:
                await store.event('auth','auth.password_policy_rejected',{'user_id':user['id']})
                raise HTTPException(403,'当前密码不符合安全策略，请由管理员重置后重新登录')
        upstream_password=hashlib.sha256(body.password.encode()).hexdigest() if settings.upstream_auth_mode=='sha256' else body.password
        result=await upstream.call(TOOLS['account_login'],dict(account=body.account,password=upstream_password,key=body.key,verifyCode=body.verifyCode,loginChannel=4))
        sid,csrf=await store.session_create(user['id'],result['data']['accessToken'])
        attempts.pop(limiter,None)
        response.set_cookie('agent_session',sid,httponly=True,secure=settings.cookie_secure,samesite='strict',max_age=settings.session_hours*3600,path=settings.api_prefix)
        await store.event('auth','auth.login',{'user_id':user['id'],'role':user['role']})
        return {'user':{'id':user['id'],'account':user['account'],'nickname':user['nickname'],'role':user['role']},'csrf_token':csrf}
    @router.get('/auth/me')
    async def me(s=Depends(identity)):
        return {'user':user_view(s),'csrf_token':s['csrf']}
    @router.post('/auth/logout')
    async def logout(response:Response,s=Depends(identity)):
        await store.execute('DELETE FROM sessions WHERE id_hash=?',(s['id_hash'],))
        response.delete_cookie('agent_session',path=settings.api_prefix)
        await store.event('auth','auth.logout',{'user_id':s['user_id']})
        return {'ok':True}
    @router.get('/tools')
    async def tools(s=Depends(identity)):
        return catalog_json()
    @router.post('/vision/images')
    async def vision_image_upload(file:UploadFile=File(...),camera_id:str|None=Form(None),captured_at:str|None=Form(None),s=Depends(identity)):
        content=await file.read(settings.vision_max_image_bytes+1)
        try:
            return await vision.save_upload(s['user_id'],content,file.filename or 'image',file.content_type or '',camera_id,captured_at)
        except VisionError as e:
            raise HTTPException(e.status_code,e.message) from e
        finally:
            await file.close()
    @router.post('/video/channels')
    async def video_channels(body:VideoChannelQuery,s=Depends(identity)):
        args=body.model_dump(exclude_none=True)
        token=await store.owner_token(s['user_id'])
        result=await upstream.call(TOOLS['video_channel_list'],args,token)
        data=result.get('data') if isinstance(result,dict) else {}
        channels=data.get('list',[]) if isinstance(data,dict) else []
        await store.event('video:channels:'+s['user_id'],'video.channels.queried',{
            'owner_id':s['user_id'],'arguments':args,'total':data.get('total') if isinstance(data,dict) else None,
            'returned_count':len(channels),'result_hash':digest(result),
        })
        return result
    @router.post('/video/snapshot')
    async def video_snapshot(body:VideoSnapshot,s=Depends(identity)):
        return await vision.capture_snapshot('video:snapshot:'+uuid4().hex,s['user_id'],body.channelId,upstream)
    @router.get('/video/monitors')
    async def video_monitors(s=Depends(identity)):
        return await monitor.list(s['user_id'])
    @router.post('/video/monitors')
    async def video_monitor_create(body:VideoMonitorCreate,s=Depends(identity)):
        if s['role']=='reviewer':
            raise HTTPException(403,'审核员不能创建持续监测任务')
        try:
            return await monitor.create(s['user_id'],body.model_dump())
        except ValueError as e:
            raise HTTPException(422,str(e)) from e
    @router.post('/video/monitors/{monitor_id}')
    async def video_monitor_update(monitor_id:str,body:VideoMonitorUpdate,s=Depends(identity)):
        if s['role']=='reviewer':
            raise HTTPException(403,'审核员不能修改持续监测任务')
        try:
            return await monitor.update(s['user_id'],monitor_id,body.model_dump(exclude_unset=True))
        except ValueError as e:
            raise HTTPException(404,str(e)) from e
    @router.post('/video/monitors/{monitor_id}/refresh-token')
    async def video_monitor_refresh_token(monitor_id:str,s=Depends(identity)):
        if s['role']=='reviewer':
            raise HTTPException(403,'审核员不能刷新监测令牌')
        try:
            return await monitor.refresh_token(s['user_id'],monitor_id)
        except ValueError as e:
            raise HTTPException(422,str(e)) from e
    @router.get('/video/alerts')
    async def video_alerts(active_only:bool=False,s=Depends(identity)):
        return await monitor.alerts(s['user_id'],active_only)
    @router.post('/video/alerts/{alert_id}/acknowledge')
    async def video_alert_acknowledge(alert_id:str,body:AlertAcknowledge,s=Depends(identity)):
        try:
            return await monitor.acknowledge(s['user_id'],alert_id,body.note)
        except ValueError as e:
            raise HTTPException(409,str(e)) from e
    @router.post('/tools/{name}/invoke')
    async def invoke_tool(name:str,body:ToolRequest,s=Depends(identity)):
        if name not in TOOLS:
            raise HTTPException(404,'工具不存在')
        spec=TOOLS[name]
        if spec.risk=='auth':
            raise HTTPException(403,'认证工具请在登录页面使用')
        if not spec.available:
            raise HTTPException(404 if name=='unknown_api' else 501,spec.description)
        if s['role']=='reviewer':
            raise HTTPException(403,'审核员仅负责人工确认，不发起执行任务')
        try:
            spec.input_model.model_validate(body.arguments)
            inputs={'tool':name,'arguments':body.arguments}
            async with locks['create:'+s['user_id']]:
                rid,created=await store.create_run(s['user_id'],spec.title,'tool',body.idempotency_key,inputs)
        except (ValueError,ValidationError) as e:
            raise HTTPException(422,str(e))
        if created:
            schedule(rid,initial_state(rid,s['user_id'],spec.title,'tool',[{'tool':name,'arguments':body.arguments}]))
        return await store.run(rid)
    @router.post('/runs')
    async def run_create(body:RunRequest,s=Depends(identity)):
        if s['role']=='reviewer':
            raise HTTPException(403,'审核员不能发起执行任务')
        mode='workflow' if body.workflow_id else 'agent'
        steps=[]
        parameters=body.parameters
        if body.workflow_id and body.conversation_id:
            raise HTTPException(422,'工作流独立执行，不可混入对话')
        if body.workflow_id:
            workflow=await find_workflow(store,s['user_id'],body.workflow_id)
            if not workflow:
                raise HTTPException(404,'工作流不存在或不属于当前账号')
            steps=workflow['steps']
            parameters={**workflow['parameters'],**parameters}
        inputs=body.model_dump()
        context={}
        try:
            async with locks['create:'+s['user_id']]:
                existing=await store.one('SELECT id,input FROM runs WHERE owner_id=? AND request_key=?',(s['user_id'],body.idempotency_key))
                if existing:
                    if json.loads(existing['input'])!=inputs:raise ValueError('幂等键已用于不同请求')
                    return await store.run(existing['id'])
                if mode=='agent' and not settings.deepseek_api_key and not (exact_warning_id(body.prompt) or is_format_only(body.prompt) or camera_number_query(body.prompt)):
                    raise HTTPException(503,'未配置DEEPSEEK_API_KEY；精确预警ID查询、精确摄像头/设备编号查询、已有结果列表排版、手动工具及预置工作流不依赖模型，其他任务需要模型')
                if mode=='agent' and body.conversation_id:
                    if not await store.conversation(body.conversation_id,s['user_id']):
                        raise HTTPException(404,'会话不存在或不属于当前账号')
                    if not existing and await store.conversation_busy(body.conversation_id):
                        raise HTTPException(409,'当前会话上一轮尚未结束或待人工确认/恢复/核对，请先处理后继续')
                    context=await store.conversation_context(body.conversation_id,s['user_id'])
                if mode=='agent' and not settings.deepseek_api_key and is_format_only(body.prompt) and not context.get('evidence'):
                    raise HTTPException(409,'当前会话没有可复用结果，请先查询或选择已有会话')
                if mode=='agent':
                    rid,created=await store.create_conversation_run(s['user_id'],body.prompt,body.idempotency_key,inputs,context)
                else:
                    rid,created=await store.create_run(s['user_id'],body.prompt,mode,body.idempotency_key,inputs)
        except ValueError as e:
            raise HTTPException(409,str(e))
        if created:
            schedule(rid,initial_state(rid,s['user_id'],body.prompt,mode,steps,parameters,context))
        return await store.run(rid)
    @router.get('/runs')
    async def runs(s=Depends(identity)):
        query='SELECT r.id,r.owner_id,r.prompt,r.mode,r.status,r.created_at,r.updated_at,r.error,c.conversation_id FROM runs r LEFT JOIN conversation_runs c ON c.run_id=r.id'
        rows=await store.fetch(query+('' if s['role'] in ('reviewer','admin') else ' WHERE r.owner_id=?')+' ORDER BY r.created_at DESC LIMIT 100',() if s['role'] in ('reviewer','admin') else (s['user_id'],))
        return rows
    @router.get('/conversations')
    async def conversations(s=Depends(identity)):
        return await store.fetch('SELECT * FROM conversations WHERE owner_id=? ORDER BY updated_at DESC LIMIT 100',(s['user_id'],))
    @router.get('/conversations/{cid}')
    async def conversation_get(cid:str,s=Depends(identity)):
        row=await store.conversation(cid,s['user_id'])
        if not row:
            raise HTTPException(404,'会话不存在或不属于当前账号')
        turns=await store.fetch('SELECT r.id,r.owner_id,r.prompt,r.mode,r.status,r.created_at,r.updated_at,r.result,r.error,c.conversation_id FROM runs r JOIN conversation_runs c ON c.run_id=r.id WHERE c.conversation_id=? ORDER BY c.ordinal DESC LIMIT 100',(cid,))
        for turn in turns:
            turn['result']=json.loads(turn['result']) if turn['result'] else None
            display_result(turn)
        return {**row,'runs':list(reversed(turns))}
    @router.get('/runs/{rid}')
    async def run_get(rid:str,s=Depends(identity)):
        return await visible_run(rid,s)
    @router.get('/runs/{rid}/export')
    async def run_export(rid:str,s=Depends(identity)):
        run=await visible_run(rid,s)
        await store.event(rid,'audit.exported',{'actor':s['user_id']})
        return JSONResponse(redact(run),headers={'Content-Disposition':f'attachment; filename="run-{rid}.json"'})
    @router.post('/runs/{rid}/resume')
    async def resume(rid:str,body:ResumeRequest,s=Depends(identity)):
        if s['role'] not in ('reviewer','admin'):
            raise HTTPException(403,'仅审核员/管理员可确认')
        async with locks[rid]:
            run=await visible_run(rid,s)
            if run['status']!='waiting_approval':
                raise HTTPException(409,'任务不在待审批状态')
            approval=await store.one('SELECT * FROM approvals WHERE id=? AND run_id=?',(body.approval_id,rid))
            if not approval or approval['status']!='pending':
                raise HTTPException(409,'审批不存在或已被处理')
            if settings.reviewer_must_differ and approval['owner_id']==s['user_id']:
                raise HTTPException(403,'发起者不能自审；请使用独立审核账号')
            if not hmac.compare_digest(approval['payload_hash'],body.payload_hash):
                raise HTTPException(409,'审批载荷哈希不匹配')
            if body.decision=='approve':
                try:
                    await store.owner_token(run['owner_id'])
                except ValueError as e:
                    raise HTTPException(409,str(e))
            status='approved' if body.decision=='approve' else 'rejected'
            changed=await store.execute("UPDATE approvals SET status=?,decided_by=?,decision_note=? WHERE id=? AND status='pending'",(status,s['user_id'],body.note,body.approval_id))
            if changed!=1:
                raise HTTPException(409,'审批已处理')
            await store.event(rid,'approval.decided',{'approval_id':body.approval_id,'actor':s['user_id'],'decision':body.decision,'note':body.note,'payload_hash':body.payload_hash})
            await store.set_run(rid,'running')
            schedule(rid,resume={'approval_id':body.approval_id,'decision':body.decision})
        return await store.run(rid)
    @router.post('/runs/{rid}/recover')
    async def recover(rid:str,s=Depends(identity)):
        run=await visible_run(rid,s)
        if run['owner_id']!=s['user_id']:
            raise HTTPException(403,'只有发起者可以恢复任务')
        async def recoverable(current):
            if current['status']=='interrupted':
                return True
            if current['status']=='waiting_approval':
                snapshot=await app.state.engine.graph.aget_state({'configurable':{'thread_id':rid}})
                if snapshot.interrupts:
                    payload=snapshot.interrupts[0].value
                    return bool(await store.one("SELECT id FROM approvals WHERE id=? AND run_id=? AND payload_hash=? AND status IN ('approved','rejected')",(payload['approval_id'],rid,payload['payload_hash'])))
            return False
        if not await recoverable(run):
            raise HTTPException(409,'只允许恢复中断任务或已决定但未继续的审批；不确定写入必须线下核对')
        async with locks[rid]:
            current=await store.run(rid)
            if not await recoverable(current):
                raise HTTPException(409,'任务已恢复或仍需人工确认')
            await store.set_run(rid,'running')
            await store.event(rid,'task.recover',{'actor':s['user_id']})
            schedule(rid,inputs=None)
        return await store.run(rid)
    @router.get('/approvals')
    async def approvals(s=Depends(identity)):
        rows=await store.approvals()
        return rows if s['role'] in ('admin','reviewer') else [r for r in rows if r['owner_id']==s['user_id']]
    @router.get('/audit/verify')
    async def verify(s=Depends(identity)):
        return await store.verify()
    @router.get('/workflows')
    async def workflows(s=Depends(identity)):
        return await list_workflows(store,s['user_id'])
    @router.post('/workflows')
    @router.post('/workflows/import')
    async def workflow_create(body:WorkflowDefinition,s=Depends(identity)):
        if s['role']=='reviewer':
            raise HTTPException(403,'审核员不能创建执行流程')
        try:
            return await save_workflow(store,s['user_id'],body)
        except ValueError as e:
            raise HTTPException(422,str(e))
    @router.get('/workflows/{wid}/export')
    async def workflow_export(wid:str,s=Depends(identity)):
        workflow=await find_workflow(store,s['user_id'],wid)
        if not workflow:
            raise HTTPException(404,'工作流不存在')
        return JSONResponse({k:workflow[k] for k in ('name','description','steps','parameters')},headers={'Content-Disposition':f'attachment; filename="workflow-{wid}.json"'})
    app.include_router(router)
    return app

app=create_app()
