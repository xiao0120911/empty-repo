"""新查询/历史排版隔离回归；必须检查真实发出的本次参数，而不只检查页面文字。"""
import asyncio
from copy import deepcopy
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError
from app.agent import initial_state,compact_evidence
from app.catalog import TOOLS
from app.intents import camera_number_query,fallback_read_call,is_format_only
from app.main import create_app
from app.reports import display_result
from app.store import Store,dumps
from app.upstream import UpstreamClient
from test_system import settings,setup_users,login,wait
from test_pagination import engine_setup,make_records,page_response

@pytest.mark.parametrize('scene',['就餐区','加工烹饪','洗消间','售卖区域','库房','生产过程控制'])
@pytest.mark.parametrize('pattern',['查询{}的摄像头信息','请找出属于{}的视频信息','重新查询{}的所有设备信息'])
def test_current_scene_is_not_history_formatting(scene,pattern):
    prompt=pattern.format(scene)
    assert not is_format_only(prompt)
    # 不做固定路由；模型空答时的只读兜底至少能构造当前场景查询。
    assert fallback_read_call(prompt)==('camera_scan',{'keyword':scene})

@pytest.mark.parametrize('prompt',['查询就餐区的摄像头信息并删除','查询就餐区的摄像头信息，然后修改状态','查询就餐区和库房的摄像头信息','查询就餐区的摄像头信息。随后新增设备'])
def test_fallback_does_not_guess_writes_or_compound_conditions(prompt):
    assert fallback_read_call(prompt) is None and not is_format_only(prompt)


def camera_fixture():
    records=make_records('camera_scan',8)
    for index,row in enumerate(records):
        row['equipmentName']='就餐区' if index>=5 else '加工烹饪'
        row['cameraNumber']='DINING-'+str(index) if index>=5 else 'COOKING-'+str(index)
    return records


class RoutingModel:
    """测试模型：按当前提示词选择只读工具与条件，不继承历史条件；工具结果后只做总结。"""
    def __init__(self):self.calls=0
    async def ainvoke(self,messages):
        self.calls+=1
        if isinstance(messages[-1],ToolMessage):
            return AIMessage(content='按本次工具证据总结')
        prompt=''
        for message in reversed(messages):
            if isinstance(message,HumanMessage):
                prompt=str(message.content);break
        if '就餐区' in prompt:args={'keyword':'就餐区'}
        elif '加工烹饪' in prompt:args={'keyword':'加工烹饪'}
        elif '库房' in prompt:args={'keyword':'库房'}
        else:args={}
        return AIMessage(content='按当前条件查询',tool_calls=[{'name':'camera_scan','args':args,'id':'routing-scan','type':'tool_call'}])


def test_api_all_cameras_then_dining_then_format_then_cooking_and_repeat(tmp_path):
    config=settings(tmp_path)
    config.deepseek_api_key='test-key'
    asyncio.run(setup_users(config))
    records=camera_fixture()
    calls=[]
    async def respond(request):
        if request.url.path.endswith('/account-psw'):
            return httpx.Response(200,json={'code':0,'msg':'成功','data':{'userId':'operator','username':'operator','nickname':'操作员','accessToken':'test-current-token'}})
        assert request.url.path.endswith('/camera/management/page')
        args=json.loads(request.content)
        calls.append(args)
        matching=[row for row in records if not args.get('keyword') or args['keyword'] in row['equipmentName']]
        start=(args['pageNum']-1)*args['pageSize']
        return httpx.Response(200,json=page_response(matching[start:start+args['pageSize']],len(matching)))
    app=create_app(config,httpx.MockTransport(respond))
    with TestClient(app) as client:
        app.state.engine.model=RoutingModel()
        csrf,_=login(client,'operator')
        def query(prompt,key,cid=None):
            body={'prompt':prompt,'idempotency_key':key}
            if cid:body['conversation_id']=cid
            response=client.post('/agent-api/runs',headers={'X-CSRF-Token':csrf},json=body)
            assert response.status_code==200,response.text
            result=wait(client,response.json()['id'],{'completed','failed'})
            assert result['status']=='completed',result
            return result,body
        all_run,_=query('查询所有摄像头信息','all-cameras-current')
        cid=all_run['conversation_id']
        dining,body=query('查询就餐区的摄像头信息','dining-cameras-current',cid)
        assert dining['id']!=all_run['id']
        assert calls[-1]=={'pageNum':1,'pageSize':100,'keyword':'就餐区'}
        result=dining['result']
        assert result['context_reused'] is False and result['fresh_query_required'] is True
        assert result['source_run_id'] is None and result['current_evidence_count']==1
        assert 'DINING-' in result['display_report'] and 'COOKING-' not in result['display_report']
        assert '未重新查询' not in result['display_report']
        assert result['steps'][0]['data']['total']==3
        assert all(row['equipmentName']=='就餐区' for row in result['steps'][0]['data']['list'])
        assert not any(event['kind']=='context.reformatted' for event in dining['events'])
        count=len(calls)
        duplicate=client.post('/agent-api/runs',headers={'X-CSRF-Token':csrf},json=body)
        assert duplicate.json()['id']==dining['id'] and len(calls)==count
        formatted,_=query('用列表形式列出','dining-format-current',cid)
        assert len(calls)==count
        assert formatted['result']['context_reused'] is True
        assert formatted['result']['current_evidence_count']==0
        assert 'DINING-' in formatted['result']['display_report'] and 'COOKING-' not in formatted['result']['display_report']
        assert '未重新查询' in formatted['result']['display_report']
        # 简短场景切换继承设备对象，但不能继承上一轮就餐区的条件。
        cooking,_=query('那加工烹饪呢','cooking-followup-current',cid)
        assert calls[-1]['keyword']=='加工烹饪' and len(calls)==count+1
        assert cooking['result']['context_reused'] is False and 'COOKING-' in cooking['result']['display_report']
        assert 'DINING-' not in cooking['result']['display_report']
        again,_=query('查询就餐区的摄像头信息','dining-again-new-key',cid)
        assert again['id']!=dining['id'] and calls[-1]['keyword']=='就餐区' and len(calls)==count+2
        assert client.get('/agent-api/runs/'+all_run['id']).json()['result']['steps'][0]['data']['total']==8
        assert client.get('/agent-api/conversations/'+cid).json()['runs'][-1]['id']==again['id']
        assert client.get('/agent-api/audit/verify').json()['valid']


@pytest.mark.asyncio
@pytest.mark.parametrize('history',[True,False])
async def test_model_no_tool_answer_cannot_complete_with_old_or_invented_result(tmp_path,history):
    async def respond(request):pytest.fail('模型没有工具调用，不应发任何平台请求')
    records=camera_fixture()
    previous={'tool':'camera_scan','arguments':{},**page_response(records,8)}
    context={'evidence':compact_evidence([previous],'old-all-cameras'),'source_run_id':'old-all-cameras'} if history else {}
    class OldAnswer:
        def __init__(self):self.calls=0
        async def ainvoke(self,messages):
            self.calls+=1
            return AIMessage(content='之前已查到8个摄像头：COOKING-0。无需查询。')
    async with engine_setup(tmp_path,httpx.MockTransport(respond)) as (engine,store):
        model=OldAnswer();engine.model=model
        prompt='查询某学校最新摄像头状态并统计离线数'
        rid,_=await store.create_run('operator',prompt,'agent','model-no-current-evidence',{})
        run=await engine.invoke(initial_state(rid,'operator',prompt,'agent',context=context),rid)
        assert run['status']=='failed' and model.calls==2
        result=run['result']
        assert result['steps']==[] and result['evidence_count']==result['current_evidence_count']==0
        assert result['context_reused'] is False and result['fresh_query_required'] is True
        assert 'COOKING-' not in result['display_report'] and '8个摄像头' not in result['display_report']
        assert '未取得新查询结果' in result['display_report']
        assert any(event['kind']=='query.freshness_retry' for event in run['events'])
        assert any(event['kind']=='query.no_current_evidence' for event in run['events'])


@pytest.mark.asyncio
async def test_model_can_correct_no_tool_answer_once_then_query_current_keyword(tmp_path):
    records=camera_fixture()[5:]
    calls=[]
    async def respond(request):
        args=json.loads(request.content);calls.append(args)
        assert args['keyword']=='就餐区'
        return httpx.Response(200,json=page_response(records,3))
    previous={'tool':'camera_list','arguments':{'pageNum':1,'pageSize':100},**page_response(camera_fixture(),8)}
    class CorrectingModel:
        def __init__(self):self.calls=0
        async def ainvoke(self,messages):
            self.calls+=1
            if self.calls==1:return AIMessage(content='使用历史所有设备记录')
            if self.calls==2:
                assert isinstance(messages[-1],HumanMessage) and '当前任务不是历史结果排版' in messages[-1].content
                return AIMessage(content='按本次条件重新查询',tool_calls=[{'name':'camera_scan','args':{'keyword':'就餐区'},'id':'current-dining','type':'tool_call'}])
            assert isinstance(messages[-1],ToolMessage)
            return AIMessage(content='已按就餐区重新查询')
    async with engine_setup(tmp_path,httpx.MockTransport(respond)) as (engine,store):
        engine.settings.max_iterations=3
        model=CorrectingModel();engine.model=model
        prompt='查询场景属于就餐区的摄像头并核对状态'
        rid,_=await store.create_run('operator',prompt,'agent','correct-model-current',{})
        context={'evidence':compact_evidence([previous],'old-all'),'source_run_id':'old-all'}
        run=await engine.invoke(initial_state(rid,'operator',prompt,'agent',context=context),rid)
        assert run['status']=='completed' and model.calls==3 and len(calls)==1
        assert run['result']['context_reused'] is False and 'COOKING-' not in run['result']['display_report']
        assert 'DINING-' in run['result']['display_report']


@pytest.mark.asyncio
async def test_failed_query_blocks_later_format_from_skipping_back_to_all_cameras(tmp_path):
    async def respond(request):return httpx.Response(401,json={'code':401,'msg':'登录已过期','data':None})
    async with engine_setup(tmp_path,httpx.MockTransport(respond)) as (engine,store):
        old,_=await store.create_run('operator','查询所有摄像头信息','agent','old-before-failure',{})
        cid=await store.attach_conversation(old,'operator','所有摄像头')
        await store.complete_conversation_turn(old,'所有摄像头','历史所有摄像头',compact_evidence([{'tool':'camera_list','arguments':{'pageNum':1,'pageSize':100},**page_response(camera_fixture(),8)}],old))
        await store.set_run(old,'completed',{'report':'历史结果'})
        context=await store.conversation_context(cid,'operator')
        assert context['evidence']
        prompt='查询另一个单位的摄像头'
        failed,_=await store.create_run('operator',prompt,'agent','failed-after-all',{})
        await store.attach_conversation(failed,'operator',prompt,cid)
        class NoTools:
            async def ainvoke(self,messages):return AIMessage(content='只返回历史结果')
        engine.model=NoTools()
        run=await engine.invoke(initial_state(failed,'operator',prompt,'agent',context=context),failed)
        assert run['status']=='failed'
        next_context=await store.conversation_context(cid,'operator')
        assert next_context['evidence']==[] and next_context['source_run_id']==failed
        fmt,_=await store.create_run('operator','用列表形式列出','agent','format-after-failure',{})
        await store.attach_conversation(fmt,'operator','用列表形式列出',cid)
        run=await engine.invoke(initial_state(fmt,'operator','用列表形式列出','agent',context=next_context),fmt)
        assert run['status']=='failed' and 'COOKING-' not in dumps(run['result'])


@pytest.mark.parametrize('code',[401,503])
@pytest.mark.asyncio
async def test_scene_query_error_does_not_fall_back_to_old_evidence(tmp_path,code):
    calls=[]
    async def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(code,json={'code':code,'msg':'current query unavailable','data':None})
    class SceneOnceModel:
        def __init__(self):self.calls=0
        async def ainvoke(self,messages):
            self.calls+=1
            if self.calls==1:
                return AIMessage(content='查询当前场景',tool_calls=[{'name':'camera_scan','args':{'keyword':'就餐区'},'id':'dining-error-scan','type':'tool_call'}])
            return AIMessage(content='查询失败，如实报告')
    async with engine_setup(tmp_path,httpx.MockTransport(respond)) as (engine,store):
        engine.model=SceneOnceModel()
        rid,_=await store.create_run('operator','查询就餐区的摄像头信息','agent','failed-dining-current',{})
        context={'evidence':compact_evidence([{'tool':'camera_list','arguments':{'pageNum':1,'pageSize':100},**page_response(camera_fixture(),8)}],'old-all')}
        run=await engine.invoke(initial_state(rid,'operator','查询就餐区的摄像头信息','agent',context=context),rid)
        assert calls==[{'pageNum':1,'pageSize':100,'keyword':'就餐区'}]
        assert run['result']['context_reused'] is False
        assert 'COOKING-' not in run['result']['display_report'] and 'DINING-' not in run['result']['display_report']
        assert '未重新查询' not in run['result']['display_report']


def test_legacy_wrong_reuse_is_warning_only_without_mutating_original_report():
    raw={'id':'new-query','prompt':'查询就餐区的摄像头信息','mode':'agent','status':'completed','result':{
        'report':'# 查询结果\n历史所有摄像头COOKING-0','context_reused':True,'source_run_id':'old-all',
        'steps':[{'tool':'camera_list','source_run_id':'old-all','arguments':{'pageNum':1,'pageSize':100},**page_response(camera_fixture(),8)}]},'events':[]}
    original=deepcopy(raw)
    view=display_result(raw)
    assert view['result']['report']==original['result']['report'] and view['status']=='completed'
    assert view['result']['legacy_stale_result'] is True
    assert view['result']['context_reused'] is False and view['result']['current_evidence_count']==0
    assert 'COOKING-' not in view['result']['display_report'] and '错误复用' not in view['result']['report']
    assert view['result']['steps']==original['result']['steps']


@pytest.mark.parametrize('prompt,expected',[
    ('未戴口罩的预警信息',('warning_scan',{'keyword':'未戴口罩'})),
    ('查询未戴口罩的预警信息',('warning_scan',{'keyword':'未戴口罩'})),
    ('已申诉的有哪些',('warning_scan',{'status':2})),
    ('状态是已处理的预警信息',('warning_scan',{'status':3})),
    ('所有摄像头列表',('camera_scan',{})),
    ('所有预警',('warning_scan',{})),
])
def test_fallback_covers_production_failure_phrases(prompt,expected):
    # 线上历史失败短语：无动词/省略对象也必须能推断出只读补查，而不是两轮空答后失败。
    assert fallback_read_call(prompt)==expected


def test_ellipsis_requires_camera_context_and_never_reuses_old_keyword():
    assert fallback_read_call('那就餐区呢') is None
    assert fallback_read_call('那就餐区呢',[{'tool':'shop_list'}]) is None
    assert fallback_read_call('那就餐区呢',[{'tool':'camera_scan','arguments':{'keyword':'加工烹饪'}}])==('camera_scan',{'keyword':'就餐区'})


@pytest.mark.asyncio
async def test_unrelated_tool_does_not_validate_new_camera_query(tmp_path):
    async def respond(request):
        if request.url.path.endswith('/menus'):
            return httpx.Response(200,json={'code':0,'msg':'成功','data':[]})
        assert request.url.path.endswith('/camera/management/page')
        return httpx.Response(200,json=page_response(camera_fixture(),8))
    class UnrelatedModel:
        def __init__(self):self.calls=0
        async def ainvoke(self,messages):
            self.calls+=1
            if self.calls==1:return AIMessage(content='读菜单',tool_calls=[{'name':'permission_menus','args':{},'id':'unrelated','type':'tool_call'}])
            return AIMessage(content='使用历史设备COOKING-0，查询成功')
    async with engine_setup(tmp_path,httpx.MockTransport(respond)) as (engine,store):
        engine.settings.max_iterations=4
        model=UnrelatedModel();engine.model=model
        prompt='查询测试学校最新摄像头信息'
        rid,_=await store.create_run('operator',prompt,'agent','unrelated-camera-query',{})
        context={'evidence':compact_evidence([{'tool':'camera_list','arguments':{'pageNum':1,'pageSize':100},**page_response(camera_fixture(),8)}],'old-all')}
        run=await engine.invoke(initial_state(rid,'operator',prompt,'agent',context=context),rid)
        # 无关工具不能验证摄像头查询；空答后由只读兜底补查当前摄像头，而不是复用历史。
        assert run['status']=='completed' and model.calls==3
        assert run['result']['context_reused'] is False
        assert [step['tool'] for step in run['result']['steps']]==['permission_menus','camera_scan']
        assert not any(event['kind'] in ('query.no_current_evidence','context.reformatted') for event in run['events'])
        assert any(event['kind']=='query.fallback_tool' for event in run['events'])


@pytest.mark.asyncio
async def test_model_cannot_hide_current_tool_failure_as_success(tmp_path):
    async def respond(request):return httpx.Response(503,json={'code':503,'msg':'平台失败','data':None})
    class FailureModel:
        def __init__(self):self.calls=0
        async def ainvoke(self,messages):
            self.calls+=1
            if self.calls==1:return AIMessage(content='读机构',tool_calls=[{'name':'current_agencies','args':{},'id':'agency-failure','type':'tool_call'}])
            return AIMessage(content='查询成功，已找到旧机构和历史设备')
    async with engine_setup(tmp_path,httpx.MockTransport(respond)) as (engine,store):
        engine.model=FailureModel()
        rid,_=await store.create_run('operator','查询最新监管机构','agent','model-read-failure',{})
        run=await engine.invoke(initial_state(rid,'operator','查询最新监管机构','agent'),rid)
        assert run['status']=='failed'
        assert '查询成功' not in run['result']['display_report'] and '未成功完成' in run['result']['display_report']


@pytest.mark.asyncio
async def test_old_terminal_no_tool_checkpoint_cannot_be_marked_completed(tmp_path):
    async def respond(request):pytest.fail('终止旧状态不能自动访问平台')
    async with engine_setup(tmp_path,httpx.MockTransport(respond)) as (engine,store):
        prompt='查询测试学校的摄像头信息'
        rid,_=await store.create_run('operator',prompt,'agent','legacy-terminal-no-tool',{})
        state=initial_state(rid,'operator',prompt,'agent',context={'evidence':compact_evidence([{'tool':'camera_list','arguments':{'pageNum':1,'pageSize':100},**page_response(camera_fixture(),8)}],'old-all')})
        state.update(status='completed',output={'report':'旧模型复用COOKING-0'})
        await engine.graph.aupdate_state({'configurable':{'thread_id':rid}},state,as_node='evaluate')
        run=await engine.invoke(None,rid)
        assert run['status']=='failed' and run['result']['steps']==[]
        assert 'COOKING-' not in run['result']['display_report']


@pytest.mark.asyncio
async def test_foreign_source_legacy_turn_is_not_reused_even_without_flag(tmp_path):
    async def respond(request):pytest.fail('不执行平台请求')
    async with engine_setup(tmp_path,httpx.MockTransport(respond)) as (engine,store):
        prompt='查询就餐区的摄像头信息'
        rid,_=await store.create_run('operator',prompt,'agent','foreign-legacy-steps',{})
        cid=await store.attach_conversation(rid,'operator',prompt)
        evidence=compact_evidence([{'tool':'camera_list','arguments':{'pageNum':1,'pageSize':100},**page_response(camera_fixture(),8)}],'old-all')
        await store.complete_conversation_turn(rid,prompt,'错误历史报告',evidence)
        await store.set_run(rid,'completed',{'report':'旧报告','steps':evidence})
        assert (await store.conversation_context(cid,'operator'))['evidence']==[]


def test_format_idempotency_retry_still_returns_accepted_run_after_later_failure(tmp_path):
    config=settings(tmp_path)
    config.deepseek_api_key='test-key'
    asyncio.run(setup_users(config))
    records=camera_fixture()[5:]
    async def respond(request):
        if request.url.path.endswith('/account-psw'):
            return httpx.Response(200,json={'code':0,'msg':'成功','data':{'userId':'operator','username':'operator','nickname':'操作员','accessToken':'test-token'}})
        args=json.loads(request.content)
        if args.get('keyword')=='库房':return httpx.Response(401,json={'code':401,'msg':'过期','data':None})
        return httpx.Response(200,json=page_response(records,3))
    app=create_app(config,httpx.MockTransport(respond))
    with TestClient(app) as client:
        app.state.engine.model=RoutingModel()
        csrf,_=login(client,'operator')
        def post(body):return client.post('/agent-api/runs',headers={'X-CSRF-Token':csrf},json=body)
        initial=post({'prompt':'查询就餐区的摄像头信息','idempotency_key':'initial-idempotent'})
        first=wait(client,initial.json()['id'],{'completed','failed'});cid=first['conversation_id']
        body={'prompt':'用列表形式列出','idempotency_key':'accepted-format-retry','conversation_id':cid}
        response=post(body);formatted=wait(client,response.json()['id'],{'completed','failed'})
        failed=post({'prompt':'查询库房的摄像头信息','idempotency_key':'failure-after-format','conversation_id':cid})
        assert wait(client,failed.json()['id'],{'completed','failed'})['status']=='failed'
        repeated=post(body)
        assert repeated.status_code==200 and repeated.json()['id']==formatted['id']
        # 配置模型时失败轮之后的排版请求会被接受，但执行期因无可复用证据而失败，不会回落旧结果。
        after_failure=post({**body,'idempotency_key':'new-format-after-failure'})
        assert after_failure.status_code==200
        assert wait(client,after_failure.json()['id'],{'completed','failed'})['status']=='failed'
        assert post({**body,'prompt':'其他不同任务'}).status_code==409


def test_current_scene_constraints_cannot_be_satisfied_by_wrong_keyword():
    from app.intents import has_relevant_evidence
    prompt='查询就餐区摄像头并核对数量'
    assert not has_relevant_evidence(prompt,[{'tool':'camera_scan','arguments':{}}])
    assert not has_relevant_evidence(prompt,[{'tool':'camera_scan','arguments':{'keyword':'加工烹饪'}}])
    assert has_relevant_evidence(prompt,[{'tool':'camera_scan','arguments':{'keyword':'就餐区'}}])


@pytest.mark.asyncio
async def test_wrong_current_scene_tool_does_not_promote_old_condition(tmp_path):
    records=camera_fixture()[:5]
    async def respond(request):return httpx.Response(200,json=page_response(records,5))
    class WrongScene:
        def __init__(self):self.calls=0
        async def ainvoke(self,messages):
            self.calls+=1
            if self.calls==1:return AIMessage(content='继承上次条件',tool_calls=[{'name':'camera_scan','args':{'keyword':'加工烹饪'},'id':'wrong-scene','type':'tool_call'}])
            return AIMessage(content='这些就是就餐区摄像头')
    async with engine_setup(tmp_path,httpx.MockTransport(respond)) as (engine,store):
        engine.model=WrongScene()
        prompt='查询就餐区摄像头并核对数量'
        rid,_=await store.create_run('operator',prompt,'agent','wrong-scene-model',{})
        run=await engine.invoke(initial_state(rid,'operator',prompt,'agent'),rid)
        assert run['status']=='failed' and run['result']['context_reused'] is False
        assert 'COOKING-' not in run['result']['display_report']
        assert '未取得新查询结果' in run['result']['display_report']


CAMERA_NUMBER='99990000001320000011'

@pytest.mark.parametrize('prompt,number',[
    ('摄像头编号是'+CAMERA_NUMBER+'的详细信息',CAMERA_NUMBER),
    ('摄像头编号是'+CAMERA_NUMBER,CAMERA_NUMBER),
    ('查询摄像头编号为DINING-7的详情','DINING-7'),
    ('设备编号是DEVICE-0001的信息','DEVICE-0001'),
    ('请帮我查一下设备编号AB-12_X','AB-12_X'),
])
def test_camera_number_intent_matches(prompt,number):
    assert not is_format_only(prompt)
    # 编号可能存于cameraNumber或equipmentNumber，窄意图两键同传，任一相等即命中。
    assert camera_number_query(prompt)==('camera_scan',{'match':{'cameraNumber':number,'equipmentNumber':number}})

@pytest.mark.parametrize('prompt',[
    '摄像头编号是'+CAMERA_NUMBER+'的详细信息并删除',
    '摄像头编号是'+CAMERA_NUMBER+'的详细信息，然后修改状态',
    '摄像头编号是'+CAMERA_NUMBER+'，同时查询就餐区的摄像头',
    '查询所有摄像头编号',
    '摄像头编号是12',
    '查询就餐区的摄像头信息',
])
def test_camera_number_intent_rejects_compound_or_invalid(prompt):
    assert camera_number_query(prompt) is None

def test_scan_match_contract_requires_exact_string_values():
    model=TOOLS['camera_scan'].input_model
    with pytest.raises(ValidationError):model(match={})
    with pytest.raises(ValidationError):model(match={'cameraNumber':int(CAMERA_NUMBER)})
    with pytest.raises(ValidationError):model(match={'cameraNumber':'A','equipmentNumber':'B','id':'C','shopId':'D','status':'E'})
    with pytest.raises(ValidationError):model(match={'非法键':'ABCD-1'})
    assert model(match={'cameraNumber':CAMERA_NUMBER}).match=={'cameraNumber':CAMERA_NUMBER}

@pytest.mark.asyncio
@pytest.mark.parametrize('scenario',['found','missing','limit'])
async def test_camera_scan_match_without_keyword(tmp_path,scenario):
    calls=[]
    records=camera_fixture()
    target='DINING-6' if scenario!='missing' else 'NO-SUCH-9999'
    async def respond(request):
        args=json.loads(request.content)
        calls.append(args)
        rows=records[(args['pageNum']-1)*2:args['pageNum']*2]
        return httpx.Response(200,json=page_response(rows,len(records)))
    upstream=UpstreamClient(settings(tmp_path),httpx.MockTransport(respond))
    try:
        result=await upstream.call(TOOLS['camera_scan'],{'match':{'cameraNumber':target,'equipmentNumber':target},'pageSize':2,'maxPages':1 if scenario=='limit' else 10},'token')
        data=result['data']
        # match只在服务端本地比较，绝不作为上游请求参数；编号不当keyword。
        assert calls and all('keyword' not in args and 'match' not in args for args in calls)
        if scenario=='found':
            assert data['list'] and data['list'][0]['cameraNumber']=='DINING-6' and data['pagesScanned']==4
        if scenario=='missing':
            assert data['complete'] is True and data['list']==[] and data['coverage']['recordsRead']==8
        if scenario=='limit':
            assert data['complete'] is False and data['outcome']=='incomplete'
    finally:
        await upstream.aclose()

@pytest.mark.asyncio
async def test_camera_scan_match_hits_either_number_field(tmp_path):
    records=camera_fixture()
    async def respond(request):
        args=json.loads(request.content)
        rows=records[(args['pageNum']-1)*100:args['pageNum']*100]
        return httpx.Response(200,json=page_response(rows,len(records)))
    upstream=UpstreamClient(settings(tmp_path),httpx.MockTransport(respond))
    try:
        data=(await upstream.call(TOOLS['camera_scan'],{'match':{'cameraNumber':'DEVICE-0003','equipmentNumber':'DEVICE-0003'}},'token'))['data']
        assert data['list'] and data['list'][0]['equipmentNumber']=='DEVICE-0003'
    finally:
        await upstream.aclose()

def test_camera_number_query_end_to_end_found_and_missing(tmp_path):
    config=settings(tmp_path)
    asyncio.run(setup_users(config))
    records=camera_fixture()
    calls=[]
    async def respond(request):
        if request.url.path.endswith('/account-psw'):
            return httpx.Response(200,json={'code':0,'msg':'成功','data':{'userId':'operator','username':'operator','nickname':'操作员','accessToken':'test-number-token'}})
        assert request.url.path.endswith('/camera/management/page')
        args=json.loads(request.content)
        calls.append(args)
        # 与真实上游一致：keyword只匹配单位名/场景名，不匹配编号。
        matching=[row for row in records if not args.get('keyword') or args['keyword'] in row['equipmentName']]
        start=(args['pageNum']-1)*args['pageSize']
        return httpx.Response(200,json=page_response(matching[start:start+args['pageSize']],len(matching)))
    with TestClient(create_app(config,httpx.MockTransport(respond))) as client:
        csrf,_=login(client,'operator')
        response=client.post('/agent-api/runs',headers={'X-CSRF-Token':csrf},json={'prompt':'摄像头编号是DINING-6的详细信息','idempotency_key':'camera-number-found'})
        assert response.status_code==200,response.text
        run=wait(client,response.json()['id'],{'completed','failed'})
        assert run['status']=='completed',run
        assert calls and all('keyword' not in args and 'match' not in args for args in calls)
        result=run['result']
        assert result['steps'][0]['tool']=='camera_scan'
        assert result['steps'][0]['arguments']=={'match':{'cameraNumber':'DINING-6','equipmentNumber':'DINING-6'}}
        assert result['steps'][0]['data']['list'] and result['steps'][0]['data']['list'][0]['cameraNumber']=='DINING-6'
        assert '已找到' in result['display_report'] and 'DINING-6' in result['display_report']
        assert not any(event['kind'] in ('query.freshness_retry','query.no_current_evidence') for event in run['events'])
        response=client.post('/agent-api/runs',headers={'X-CSRF-Token':csrf},json={'prompt':'摄像头编号是NO-SUCH-9999的详细信息','idempotency_key':'camera-number-missing'})
        assert response.status_code==200,response.text
        run=wait(client,response.json()['id'],{'completed','failed'})
        assert run['status']=='completed',run
        result=run['result']
        assert result['steps'][0]['tool']=='camera_scan'
        assert result['steps'][0]['data']['list']==[] and result['steps'][0]['data']['complete'] is True
        assert '未匹配' in result['display_report'] and '不证明' in result['display_report']
        assert '已找到' not in result['display_report']
