"""离线安全验收：用MockTransport模拟平台，不冒充真实内网联调/视觉准确率验收。"""
import asyncio
import hashlib
import hmac
import json
import time
import pytest
import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.config import Settings
from app.catalog import TOOLS,catalog_json
from app.contracts import ShopDelete,CameraUpdate,WarningApprove,ShopUpdate
from app.upstream import UpstreamClient,UpstreamError
from app.store import Store,digest
from app.security import password_hash,password_check,password_policy_error
from app.main import create_app
from app.workflows import resolve

PASSWORD='TestOnlyStrong123!'

def settings(tmp_path):
    return Settings(data_dir=tmp_path,fernet_key=Fernet.generate_key().decode(),cookie_secure=False,deepseek_api_key='',allowed_origins='http://testserver')

async def setup_users(config):
    store=Store(config)
    await store.open()
    for account,role in [('operator','operator'),('reviewer','reviewer'),('admin','admin')]:
        await store.execute('INSERT INTO users VALUES(?,?,?,?,?)',(account,account,password_hash(PASSWORD),role,account))
    await store.close()

def transport(seen):
    def respond(request):
        seen.append(request)
        path=request.url.path
        args=json.loads(request.content) if request.content else {}
        if path.endswith('getGraphicCode'):
            data={'key':'captcha-key','image':'iVBOR','_mockCode':'1234'}
        elif path.endswith('account-psw'):
            data={'userId':'9900000000000000001','username':args['account'],'nickname':args['account'],'accessToken':'token-'+args['account']}
        elif path.endswith('/approve'):
            data={'warnId':str(args['approveRecodeId']),'status':3 if args['accept'] else 4,'statusName':'已处理' if args['accept'] else '已驳回'}
        elif path.endswith('statusCount'):
            data={'pendingCount':2,'appealedCount':1,'processedCount':0,'rejectCount':0,'totalCount':3,'isPush':1}
        elif path.endswith('findCurrentUserAgencies'):
            data={'id':'AG001','name':'测试机构'}
        elif path.endswith('/warnList'):
            data=[{'id':1,'name':'经营监管'}]
        elif path.endswith('/delShopById') or path.endswith('/stream/delete'):
            data={'deleted':1}
        else:
            data={'total':0,'list':[]}
        return httpx.Response(200,json={'code':0,'msg':'成功','data':data})
    return httpx.MockTransport(respond)

@pytest.fixture
def client_setup(tmp_path):
    config=settings(tmp_path)
    asyncio.run(setup_users(config))
    seen=[]
    app=create_app(config,transport(seen))
    with TestClient(app) as client:
        yield client,seen,config

def login(client,account):
    response=client.post('/agent-api/auth/login',json={'account':account,'password':PASSWORD,'key':'captcha-key','verifyCode':'1234'})
    assert response.status_code==200,response.text
    return response.json()['csrf_token'],client.cookies.get('agent_session')

def wait(client,rid,states):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        result=client.get('/agent-api/runs/'+rid).json()
        if result.get('status') in states:
            return result
    raise AssertionError(result)

def test_catalog_all_routes():
    assert len(TOOLS)==31
    assert len({t.name for t in TOOLS.values()})==31
    for item in catalog_json():
        assert item['description'] and item['input_schema'] and item['response_schema']
    assert TOOLS['warning_approve'].risk=='sensitive'
    assert TOOLS['shop_delete'].risk=='write' and TOOLS['camera_delete'].risk=='write' and TOOLS['warning_apply'].risk=='write'
    assert not TOOLS['live_gbs_get'].available

def test_contracts():
    with pytest.raises(ValidationError):ShopDelete(id='1',ids=['2'])
    with pytest.raises(ValidationError):ShopDelete()
    with pytest.raises(ValidationError):ShopUpdate(id='1')
    with pytest.raises(ValidationError):CameraUpdate(id='1')
    with pytest.raises(ValidationError):CameraUpdate(id='1',status=2)
    with pytest.raises(ValidationError):WarningApprove(approveRecodeId='1',accept='false')
    assert WarningApprove(approveRecodeId='1',accept=False).model_dump()['accept'] is False
    with pytest.raises(ValidationError):TOOLS['shop_list'].input_model(pageNum=0,pageSize=101)
    with pytest.raises(ValidationError):TOOLS['shop_list'].input_model(pageNum=1,pageSize=10,unsafe='x')

def test_password():
    encoded=password_hash(PASSWORD)
    assert password_check(PASSWORD,encoded)
    assert not password_check('wrong',encoded)
    assert password_policy_error('123456')
    with pytest.raises(ValueError):
        password_hash('weak-password')


def test_security_environment_configuration():
    key=Fernet.generate_key().decode()
    with pytest.raises(ValidationError):
        Settings(_env_file=None,fernet_key='')
    with pytest.raises(ValidationError):
        Settings(_env_file=None,app_env='production',fernet_key=key,upstream_mock=True,
                 upstream_base_url='https://platform.example.gov.cn',upstream_ak='ak',upstream_secret='secret',
                 upstream_auth_mode='sha256',cookie_secure=True,require_https=True,
                 public_base_url='https://regulatory.example.gov.cn',allowed_origins='https://regulatory.example.gov.cn',
                 enforce_password_policy=True)
    production=Settings(_env_file=None,app_env='production',fernet_key=key,upstream_mock=False,
                        upstream_base_url='https://platform.example.gov.cn',upstream_ak='ak',upstream_secret='secret',
                        upstream_auth_mode='sha256',cookie_secure=True,require_https=True,
                        public_base_url='https://regulatory.example.gov.cn',allowed_origins='https://regulatory.example.gov.cn',
                        enforce_password_policy=True)
    assert production.app_env=='production'


def test_cross_origin_post_is_rejected(client_setup):
    client,_,_=client_setup
    rejected=client.post('/agent-api/auth/captcha',headers={'Origin':'https://untrusted.example'})
    assert rejected.status_code==403
    allowed=client.post('/agent-api/auth/captcha',headers={'Origin':'http://testserver'})
    assert allowed.status_code==200

def test_workflow_reference():
    assert resolve('${parameters.pageSize}',{'pageSize':10},[])==10
    assert resolve('${steps.0.data.id}',{},[{'data':{'id':'123'}}])=='123'
    with pytest.raises(ValueError):resolve('${parameters.__class__.__base__}',{},[])

@pytest.mark.asyncio
async def test_signature_and_empty_body(tmp_path):
    seen=[]
    config=settings(tmp_path)
    upstream=UpstreamClient(config,transport(seen))
    await upstream.call(TOOLS['warning_approve'],{'approveRecodeId':'123','accept':False,'suggestion':'证据不足'},'raw-token')
    req=seen[-1]
    ts=req.headers['ts']
    expected=hashlib.md5(f'{len(req.content)}:{ts}'.encode()).hexdigest()
    assert req.headers['content-md5']==expected
    msg=f"{req.url.path};{ts};POST;{req.headers['rs']};{expected};raw-token"
    assert req.headers['sign']==hmac.new(config.upstream_secret.encode(),msg.encode(),hashlib.sha256).hexdigest()
    assert req.headers['authorization']=='raw-token'
    assert json.loads(req.content)['accept'] is False
    await upstream.call(TOOLS['warning_status_count'],{},'raw-token')
    assert seen[-1].content==b''
    assert seen[-1].headers['content-md5']==hashlib.md5((':'+seen[-1].headers['ts']).encode()).hexdigest()
    await upstream.aclose()

@pytest.mark.asyncio
async def test_unavailable_and_uncertain(tmp_path):
    config=settings(tmp_path)
    calls=[]
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout('timeout',request=request)
    upstream=UpstreamClient(config,httpx.MockTransport(timeout))
    with pytest.raises(UpstreamError) as e:
        await upstream.call(TOOLS['monitor_start'],{})
    assert e.value.code==501 and not calls
    with pytest.raises(UpstreamError) as e:
        await upstream.call(TOOLS['unknown_api'],{'path':'/api/delete-all'})
    assert e.value.code==404 and not calls
    with pytest.raises(UpstreamError) as e:
        await upstream.call(TOOLS['shop_create'],{'shopName':'测试单位'},'token')
    assert e.value.uncertain and len(calls)==1
    await upstream.aclose()

def test_login_csrf_and_roles(client_setup):
    client,seen,_=client_setup
    assert client.get('/agent-api/tools').status_code==401
    response=client.post('/agent-api/auth/login',json={'account':'unconfigured','password':PASSWORD,'key':'x','verifyCode':'1234'})
    assert response.status_code==401
    csrf,_=login(client,'operator')
    assert client.get('/agent-api/tools').status_code==200
    assert client.post('/agent-api/tools/shop_list/invoke',json={'arguments':{'pageNum':1,'pageSize':10},'idempotency_key':'test-csrf-001'}).status_code==403
    assert client.post('/agent-api/tools/account_login/invoke',headers={'X-CSRF-Token':csrf},json={'arguments':{},'idempotency_key':'test-auth-001'}).status_code==403

def test_sensitive_pause_approve_and_idempotency(client_setup):
    client,seen,_=client_setup
    csrf,owner_cookie=login(client,'operator')
    body={'arguments':{'approveRecodeId':'123','accept':False,'suggestion':'证据不足'},'idempotency_key':'approval-test-001'}
    response=client.post('/agent-api/tools/warning_approve/invoke',headers={'X-CSRF-Token':csrf},json=body)
    assert response.status_code==200,response.text
    rid=response.json()['id']
    result=wait(client,rid,{'waiting_approval'})
    assert not any(r.url.path.endswith('/approve') for r in seen)
    approval=result['approvals'][0]
    decision={'decision':'approve','approval_id':approval['id'],'payload_hash':approval['payload_hash'],'note':'人工已核验证据'}
    assert client.post('/agent-api/runs/'+rid+'/resume',headers={'X-CSRF-Token':csrf},json=decision).status_code==403
    reviewer_csrf,_=login(client,'reviewer')
    bad={**decision,'payload_hash':'bad'}
    assert client.post('/agent-api/runs/'+rid+'/resume',headers={'X-CSRF-Token':reviewer_csrf},json=bad).status_code==409
    response=client.post('/agent-api/runs/'+rid+'/resume',headers={'X-CSRF-Token':reviewer_csrf},json=decision)
    assert response.status_code==200,response.text
    result=wait(client,rid,{'completed','failed'})
    assert result['status']=='completed',result
    requests=[r for r in seen if r.url.path.endswith('/approve')]
    assert len(requests)==1 and requests[0].headers['authorization']=='token-operator'
    assert json.loads(requests[0].content)['accept'] is False
    assert client.post('/agent-api/runs/'+rid+'/resume',headers={'X-CSRF-Token':reviewer_csrf},json=decision).status_code==409
    client.cookies.set('agent_session',owner_cookie)
    repeat=client.post('/agent-api/tools/warning_approve/invoke',headers={'X-CSRF-Token':csrf},json=body)
    assert repeat.json()['id']==rid
    assert client.get('/agent-api/audit/verify').json()['valid']
    exported=client.get('/agent-api/runs/'+rid+'/export').text
    assert 'token-operator' not in exported and PASSWORD not in exported

def test_sensitive_reject_and_workflow(client_setup):
    client,seen,_=client_setup
    csrf,_=login(client,'operator')
    r=client.post('/agent-api/tools/warning_approve/invoke',headers={'X-CSRF-Token':csrf},json={'arguments':{'approveRecodeId':'123','accept':False,'suggestion':'证据不足'},'idempotency_key':'reject-test-001'})
    rid=r.json()['id']
    result=wait(client,rid,{'waiting_approval'})
    approval=result['approvals'][0]
    review_csrf,_=login(client,'reviewer')
    decision={'decision':'reject','approval_id':approval['id'],'payload_hash':approval['payload_hash'],'note':'不应驳回'}
    assert client.post('/agent-api/runs/'+rid+'/resume',headers={'X-CSRF-Token':review_csrf},json=decision).status_code==200
    assert wait(client,rid,{'rejected','failed'})['status']=='rejected'
    assert not any(req.url.path.endswith('/approve') for req in seen)
    csrf,_=login(client,'operator')
    # 非审批/处罚决定的白名单写工具直接执行，不进入人工确认。
    r=client.post('/agent-api/tools/shop_delete/invoke',headers={'X-CSRF-Token':csrf},json={'arguments':{'id':'123'},'idempotency_key':'direct-delete-001'})
    direct=wait(client,r.json()['id'],{'completed','failed','rejected','waiting_approval'})
    assert direct['status']=='completed',direct
    assert direct['approvals']==[]
    assert any(req.url.path.endswith('/delShopById') for req in seen)
    r=client.post('/agent-api/runs',headers={'X-CSRF-Token':csrf},json={'prompt':'预警概览','workflow_id':'warning_overview','parameters':{'pageSize':10,'keyword':''},'idempotency_key':'workflow-test-001'})
    result=wait(client,r.json()['id'],{'completed','failed'})
    assert result['status']=='completed',result
    assert result['result']['evidence_count']==4
    assert client.post('/agent-api/runs',headers={'X-CSRF-Token':csrf},json={'prompt':'自然语言任务','idempotency_key':'llm-test-001'}).status_code==503

@pytest.mark.asyncio
async def test_checkpoint_survives_restart(tmp_path):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from app.agent import Engine,initial_state
    from app.gateway import Gateway
    config=settings(tmp_path)
    await setup_users(config)
    store=Store(config)
    await store.open()
    await store.session_create('operator','token-operator')
    seen=[]
    upstream=UpstreamClient(config,transport(seen))
    rid,_=await store.create_run('operator','审批重启恢复','tool','restart-test-001',{})
    args={'approveRecodeId':'123','accept':False}
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'checkpoint.sqlite')) as saver:
        await saver.setup()
        engine=Engine(config,store,Gateway(store,upstream),saver)
        result=await engine.invoke(initial_state(rid,'operator','审批重启恢复','tool',[{'tool':'warning_approve','arguments':args}]),rid)
        assert result['status']=='waiting_approval'
    approval=result['approvals'][0]
    await store.execute("UPDATE approvals SET status='approved',decided_by='reviewer',decision_note='人工已确认' WHERE id=?",(approval['id'],))
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'checkpoint.sqlite')) as saver:
        await saver.setup()
        engine=Engine(config,store,Gateway(store,upstream),saver)
        result=await engine.invoke(None,rid,{'approval_id':approval['id'],'decision':'approve'})
        assert result['status']=='completed'
        assert len([r for r in seen if r.url.path.endswith('/approve')])==1
    assert (await store.verify())['valid']
    await store.close()
    await upstream.aclose()


def test_business_summary_preserves_scope_errors_and_ids():
    from app.reports import summarize_rows,public_report,has_raw_payload
    found={'tool':'warning_scan','code':0,'arguments':{'match':{'id':TARGET}},
        'data':{'total':5000,'list':[warning_record(TARGET)],'outcome':'incomplete','pagesScanned':3,'recordsScanned':1,'complete':False,
        'reason':'已按完整字符串本地精确匹配命中；命中后不再扫描其余页','scope':'当前账号/当前检索条件；非事务快照',
        'coverage':{'kind':'scan','total':5000,'lastObservedTotal':5000,'pageSize':100,'pagesScanned':3,'recordsScanned':1,'recordsRead':300,
            'complete':False,'nextPage':None,'reason':'命中','scope':'当前账号/当前检索条件；非事务快照','filter':{'match':{'id':TARGET}},'pages':[]},
        'statistics':{'recordCount':1,'groups':{}}}}
    report=summarize_rows('查预警',[found],'run-summary')
    assert TARGET in report and '300' in report and '5000' in report and '已找到' in report
    assert not has_raw_payload(report) and 'organizationName' not in report
    incomplete={**found,'data':{**found['data'],'list':[],'complete':False,'reason':'达到上限'}}
    assert '不能据此判断记录不存在' in summarize_rows('查预警',[incomplete])
    rejected={'tool':'camera_delete','code':403,'data':None,'human_rejected':True}
    assert '未执行' in summarize_rows('删除',[rejected])
    assert '尚待核对' in summarize_rows('写入',[],status='needs_reconciliation')
    assert '已找到' in public_report('```json\n'+json.dumps(found)+'\n```','查预警',[found])
    assert public_report('已核对，状态为待反馈。','查预警',[found])=='已核对，状态为待反馈。'
    assert '缺少可核验' in public_report('{"result":"虚构"}','查询',[])
    assert '缺少可核验' in public_report('"原始标量"','查询',[])
    assert '人工已拒绝' not in summarize_rows('删除',[{'tool':'camera_delete','code':403,'msg':'无权限','data':None}])
    historical=public_report('{"code":0}','用列表形式列出',[found],'new-run',reused=True,source='old-run')
    assert '历史查询时为准' in historical and 'old-run' in historical
    new_query=public_report('{"code":0}','查询最新状态',[found],'new-run',reused=True,source='old-run')
    assert '未取得新查询结果' in new_query and '已找到' not in new_query
    from app.reports import display_result
    untrusted={'id':'old','prompt':'删除','result':{'report':'{"tool":"shop_delete","code":0,"data":{"deleted":7}}'},'events':[]}
    assert '缺少可核验' in display_result(untrusted)['result']['display_report']


def test_legacy_report_read_display_does_not_modify_evidence(client_setup):
    from app.store import dumps
    client,_,config=client_setup
    login(client,'operator')
    row={'tool':'warning_list','arguments':{'pageNum':1,'pageSize':10},'code':0,'data':{'total':5000,'list':[warning_record(TARGET)]}}
    original='# 旧报告\n'+json.dumps(row,ensure_ascii=False)
    async def insert_legacy():
        store=Store(config)
        await store.open()
        rid,_=await store.create_run('operator','旧任务查询','tool','legacy-summary-001',{})
        await store.set_run(rid,'completed',{'report':original,'steps':[row]})
        await store.event(rid,'tool.completed',{'tool':'warning_list','result':{'code':0,'data':row['data']}})
        chain=(await store.verify())['head']
        await store.close()
        return rid,chain
    rid,chain=asyncio.run(insert_legacy())
    response=client.get('/agent-api/runs/'+rid).json()
    assert response['result']['report']==original
    display=response['result']['display_report']
    assert TARGET in display and '本页不代表全量' in display
    assert '"code"' not in display and '"list"' not in display
    assert client.get('/agent-api/audit/verify').json()['head']==chain
    async def assert_unchanged():
        store=Store(config)
        await store.open()
        saved=await store.one('SELECT result FROM runs WHERE id=?',(rid,))
        assert 'display_report' not in json.loads(saved['result'])
        assert json.loads(saved['result'])['report']==original
        await store.close()
    asyncio.run(assert_unchanged())


def test_workflow_export_import(client_setup):
    client,_,_=client_setup
    csrf,_=login(client,'operator')
    definition=client.get('/agent-api/workflows/warning_overview/export').json()
    response=client.post('/agent-api/workflows/import',headers={'X-CSRF-Token':csrf},json=definition)
    assert response.status_code==200,response.text
    assert response.json()['steps']==definition['steps']


@pytest.mark.asyncio
async def test_conversation_creation_transaction_rollback(tmp_path):
    store=Store(settings(tmp_path))
    await store.open()
    await store.execute("CREATE TRIGGER fail_context BEFORE INSERT ON run_context BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(Exception):
        await store.create_conversation_run('operator','测试','atomic-create-001',{'conversation_id':None},{'messages':[]})
    assert await store.fetch('SELECT id FROM runs')==[]
    assert await store.fetch('SELECT id FROM conversations')==[]
    assert await store.fetch('SELECT run_id FROM conversation_runs')==[]
    await store.execute('DROP TRIGGER fail_context')
    rid,created=await store.create_conversation_run('operator','测试','atomic-create-001',{'conversation_id':None},{'messages':[]})
    assert created and await store.run_conversation(rid)
    assert (await store.load_run_context(rid))['messages']==[]
    assert (await store.create_conversation_run('operator','测试','atomic-create-001',{'conversation_id':None},{}))==(rid,False)
    await store.close()


TARGET='9300000000000001602'

def warning_record(identifier):
    return {'id':identifier,'organizationId':9000000000000000001,'organizationName':'测试食堂','warnType':1,'warnTypeMsg':'经营监管','subType':101,'subTypeMsg':'留样','agenciesId':'AG001','agenciesName':'测试机构','updateTime':'2026-10-19 12:00:00','status':1,'statusName':'待反馈'}

@pytest.mark.asyncio
@pytest.mark.parametrize('scenario',['found','missing','limit','duplicate','drift','failure'])
async def test_exact_warning_pagination(tmp_path,scenario):
    calls=[]
    audits=[]
    records=[warning_record(9300000000000000000+i) for i in range(4)]+[warning_record(int(TARGET))]
    def respond(request):
        args=json.loads(request.content)
        calls.append(args)
        page=args['pageNum']
        if scenario=='failure' and page==2:
            return httpx.Response(503,json={'code':503,'msg':'temporarily unavailable','data':None})
        rows=records[(page-1)*2:page*2]
        if scenario=='duplicate' and page==2:
            rows=records[:2]
        total=6 if scenario=='drift' and page==2 else 5
        return httpx.Response(200,json={'code':0,'msg':'成功','data':{'total':total,'list':rows}})
    upstream=UpstreamClient(settings(tmp_path),httpx.MockTransport(respond))
    async def audit(data):audits.append(data)
    try:
        result=await upstream.call(TOOLS['warning_scan'],{'match':{'id':TARGET if scenario!='missing' else '9999999999999999999'},'pageSize':2,'maxPages':1 if scenario=='limit' else 10},'token',audit)
        data=result['data']
        # match只在服务端本地比较，绝不作为上游请求参数；ID不当keyword。
        assert calls and all('keyword' not in args and 'match' not in args and 'id' not in args for args in calls)
        assert audits
        if scenario=='found':
            assert data['list'] and data['list'][0]['id']==TARGET and data['list'][0]['organizationId']=='9000000000000000001'
            assert data['pagesScanned']==3 and data['complete'] is True
        elif scenario=='missing':
            assert data['complete'] is True and data['list']==[] and data['coverage']['recordsRead']==5
        else:
            assert data['complete'] is False and data['outcome']=='incomplete' and data['list']==[]
    finally:
        await upstream.aclose()


def test_lookup_id_precision_and_intent():
    from app.agent import exact_warning_id,is_format_only
    assert exact_warning_id('查询事件ID是'+TARGET+'的预警')==TARGET
    assert exact_warning_id('查询事件ID是'+TARGET+'的预警并处理') is None
    assert is_format_only('用列表形式列出')
    assert not is_format_only('用列表形式列出并删除这些设备')
    # match值必须是完整字符串；整型/浮点一律拒绝，避免大ID精度损失。
    model=TOOLS['warning_scan'].input_model
    for value in (int(TARGET),float(TARGET)):
        with pytest.raises(ValidationError):model(match={'id':value})
    with pytest.raises(ValidationError):model(match={})
    with pytest.raises(ValidationError):model(match={'非法键':TARGET})


def test_conversation_api_reformat_idempotency_and_isolation(tmp_path):
    config=settings(tmp_path)
    asyncio.run(setup_users(config))
    seen=[]
    fallback=transport(seen)
    def respond(request):
        if request.url.path.endswith('/event/list'):
            seen.append(request)
            return httpx.Response(200,json={'code':0,'msg':'成功','data':{'total':1,'list':[warning_record(int(TARGET))]}})
        return fallback.handle_request(request)
    with TestClient(create_app(config,httpx.MockTransport(respond))) as client:
        csrf,cookie=login(client,'operator')
        body={'prompt':'查询事件ID是'+TARGET+'的预警','idempotency_key':'exact-conversation-001'}
        response=client.post('/agent-api/runs',headers={'X-CSRF-Token':csrf},json=body)
        assert response.status_code==200,response.text
        first=wait(client,response.json()['id'],{'completed','failed'})
        assert first['status']=='completed',first
        cid=first['conversation_id']
        assert cid and TARGET in first['result']['report']
        assert '已找到事件ID' in first['result']['display_report']
        assert '"code"' not in first['result']['display_report'] and '"data"' not in first['result']['display_report']
        count=len(seen)
        follow={'prompt':'用列表形式列出','conversation_id':cid,'idempotency_key':'format-conversation-001'}
        second_response=client.post('/agent-api/runs',headers={'X-CSRF-Token':csrf},json=follow)
        assert second_response.status_code==200,second_response.text
        second=wait(client,second_response.json()['id'],{'completed','failed'})
        assert second['status']=='completed' and second['result']['context_reused']
        assert TARGET in second['result']['report'] and len(seen)==count
        repeat=client.post('/agent-api/runs',headers={'X-CSRF-Token':csrf},json=follow)
        assert repeat.json()['id']==second['id']
        assert len(client.get('/agent-api/conversations/'+cid).json()['runs'])==2
        admin_csrf,_=login(client,'admin')
        assert client.get('/agent-api/conversations/'+cid).status_code==404
        assert client.get('/agent-api/conversations').json()==[]
        assert client.post('/agent-api/runs',headers={'X-CSRF-Token':admin_csrf},json={**follow,'idempotency_key':'other-owner-test'}).status_code==404
        client.cookies.set('agent_session',cookie)
        assert client.post('/agent-api/runs',headers={'X-CSRF-Token':csrf},json={'prompt':'用列表形式列出','idempotency_key':'no-context-001'}).status_code==409
        async def set_waiting():
            store=Store(config)
            await store.open()
            await store.set_run(second['id'],'waiting_approval')
            await store.close()
        asyncio.run(set_waiting())
        assert client.post('/agent-api/runs',headers={'X-CSRF-Token':csrf},json={**follow,'idempotency_key':'blocked-followup-001'}).status_code==409
        assert client.get('/agent-api/audit/verify').json()['valid']


def test_recover_persisted_approval_decision(client_setup):
    client,seen,config=client_setup
    csrf,_=login(client,'operator')
    response=client.post('/agent-api/tools/warning_approve/invoke',headers={'X-CSRF-Token':csrf},json={'arguments':{'approveRecodeId':'123','accept':False},'idempotency_key':'decided-recovery-001'})
    rid=response.json()['id']
    result=wait(client,rid,{'waiting_approval'})
    approval=result['approvals'][0]
    async def persist_decision_only():
        store=Store(config)
        await store.open()
        await store.execute("UPDATE approvals SET status='approved',decided_by='reviewer',decision_note='已人工决定' WHERE id=?",(approval['id'],))
        await store.close()
    asyncio.run(persist_decision_only())
    response=client.post('/agent-api/runs/'+rid+'/recover',headers={'X-CSRF-Token':csrf},json={})
    assert response.status_code==200,response.text
    result=wait(client,rid,{'completed','failed'})
    assert result['status']=='completed',result
    assert len([r for r in seen if r.url.path.endswith('/approve')])==1


@pytest.mark.asyncio
async def test_camera_conversation_restart_model_context_and_new_approval(tmp_path):
    from langchain_core.messages import AIMessage,HumanMessage,ToolMessage,messages_from_dict
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from app.agent import Engine,initial_state
    from app.gateway import Gateway
    config=settings(tmp_path)
    await setup_users(config)
    store=Store(config)
    await store.open()
    await store.session_create('operator','token-operator')
    camera={'id':9100000000000000001,'deviceId':9100000000000000001,'shopName':'测试食堂','shopId':9000000000000000001,'equipmentNumber':'DEVICE001','cameraNumber':'CAM001','equipmentType':1,'status':1,'live':1,'equipmentName':'加工烹饪','orgName':'测试机构','platformType':'yushi'}
    calls=[]
    def respond(request):
        calls.append(request)
        if request.url.path.endswith('/stream/delete'):
            return httpx.Response(200,json={'code':0,'msg':'成功','data':{'deleted':1}})
        return httpx.Response(200,json={'code':0,'msg':'成功','data':{'total':1,'list':[camera]}})
    upstream=UpstreamClient(config,httpx.MockTransport(respond))
    class CameraModel:
        async def ainvoke(self,messages):
            if isinstance(messages[-1],ToolMessage):return AIMessage(content='找到加工烹饪设备CAM001，仅元数据。')
            return AIMessage(content='查询加工烹饪设备',tool_calls=[{'name':'camera_list','args':{'pageNum':1,'pageSize':100,'keyword':'加工烹饪'},'id':'camera-query','type':'tool_call'}])
    rid,_=await store.create_run('operator','找出属于加工烹饪的视频信息','agent','camera-turn-one',{})
    cid=await store.attach_conversation(rid,'operator','加工烹饪')
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'conversation-checkpoint.sqlite')) as saver:
        await saver.setup()
        engine=Engine(config,store,Gateway(store,upstream),saver)
        engine.model=CameraModel()
        first=await engine.invoke(initial_state(rid,'operator','找出属于加工烹饪的视频信息','agent'),rid)
        assert first['status']=='completed' and len(calls)==1
    await store.close()
    store=Store(config)
    await store.open(recover=True)
    context=await store.conversation_context(cid,'operator')
    assert 'CAM001' in json.dumps(context,ensure_ascii=False)
    context_rows=await store.fetch('SELECT context_cipher FROM conversation_turns')
    assert 'CAM001' not in context_rows[0]['context_cipher']
    rid2,_=await store.create_run('operator','用列表形式列出','agent','camera-turn-two',{})
    await store.attach_conversation(rid2,'operator','用列表形式列出',cid)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'conversation-checkpoint.sqlite')) as saver:
        engine=Engine(config,store,Gateway(store,upstream),saver)
        second=await engine.invoke(initial_state(rid2,'operator','用列表形式列出','agent',context=context),rid2)
        assert second['status']=='completed' and len(calls)==1
        assert 'CAM001' in second['result']['report'] and '加工烹饪' in second['result']['report']
        assert '摄像头编号' in second['result']['report'] and 'cameraNumber' not in second['result']['report']
        assert '"pageNum"' not in second['result']['report']
        next_context=await store.conversation_context(cid,'operator')
        class FollowupModel:
            async def ainvoke(self,messages):
                if any(isinstance(m,ToolMessage) for m in messages):
                    return AIMessage(content='已通过白名单工具删除设备9100000000000000001，结果以平台回执为准。')
                assert any(isinstance(m,HumanMessage) and '加工烹饪' in str(m.content) for m in messages)
                assert any('CAM001' in str(m.content) for m in messages)
                return AIMessage(content='拟删除历史设备，白名单工具直接执行',tool_calls=[{'name':'camera_delete','args':{'id':'9100000000000000001'},'id':'delete-new-turn','type':'tool_call'}])
        engine.model=FollowupModel()
        rid3,_=await store.create_run('operator','删除刚才的设备','agent','camera-turn-three',{})
        await store.attach_conversation(rid3,'operator','删除刚才的设备',cid)
        third=await engine.invoke(initial_state(rid3,'operator','删除刚才的设备','agent',context=next_context),rid3)
        # camera_delete 非审批决定：白名单直接执行，不产生人工确认。
        assert third['status']=='completed' and len(calls)==2
        assert third['approvals']==[]
        assert any(req.url.path.endswith('/stream/delete') for req in calls)
    with pytest.raises(ValueError):await store.conversation_context(cid,'admin')
    # 即使模型可用，纯排版且无证据也不能回落到可执行工具。
    rid4,_=await store.create_run('operator','用列表形式列出','agent','empty-format-model',{})
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'conversation-checkpoint.sqlite')) as saver:
        engine=Engine(config,store,Gateway(store,upstream),saver)
        engine.model=FollowupModel()
        empty=await engine.invoke(initial_state(rid4,'operator','用列表形式列出','agent'),rid4)
        assert empty['status']=='failed' and len(calls)==2
    assert (await store.verify())['valid']
    await store.close()
    await upstream.aclose()

