"""可重复运行的交付验证。上游只读检查仅适用于文档Mock，不修改业务数据。"""
import argparse
import asyncio
import importlib.metadata
import json
from pathlib import Path
from app.catalog import TOOLS,catalog_json
from app.config import Settings
from app.upstream import UpstreamClient,UpstreamError

ROOT=Path(__file__).resolve().parents[1]
async def readonly_check():
    settings=Settings()
    if not settings.upstream_mock:
        raise ValueError('此自检只用于Mock，正式环境不得用任意联调账号')
    client=UpstreamClient(settings)
    results=[]
    try:
        captcha=await client.call(TOOLS['get_graphic_code'],{})
        login=await client.call(TOOLS['account_login'],{'account':'delivery-readonly-check','password':'readonly-sandbox-check','loginChannel':4,'key':captcha['data']['key'],'verifyCode':captcha['data'].get('_mockCode') or '1234'})
        token=login['data']['accessToken']
        for spec in TOOLS.values():
            if spec.risk!='read':
                continue
            args={'pageNum':1,'pageSize':2} if 'pageNum' in spec.input_model.model_fields else {}
            try:
                result=await client.call(spec,args,token)
                data=result['data']
                entry={'tool':spec.name,'ok':True,'code':result['code'],'total':data.get('total') if isinstance(data,dict) else None,'records':len(data['list']) if isinstance(data,dict) and 'list' in data else (len(data) if isinstance(data,list) else None)}
                if spec.name in ('shop_scan','camera_scan','warning_scan'):
                    entry.update(outcome=data['outcome'],complete=data['complete'],pagesScanned=data['pagesScanned'],recordsScanned=data['recordsScanned'],statistics=data['statistics'],scope=data['scope'])
                    from app.pagination import observation
                    from app.store import dumps
                    view=observation(spec.name,args,result)
                    entry.update(modelWindowRecords=len(view['data']['list']),observationCharacters=len(dumps(view)))
                    entry['ok']=data['complete'] is True and data['recordsScanned']==data['total']==len(data['list']) and view['data']['total']==data['total'] and len(dumps(view))<=24000
                results.append(entry)
            except UpstreamError as e:
                results.append({'tool':spec.name,'ok':False,'code':e.code,'error':e.message})
        filtered=await client.call(TOOLS['camera_scan'],{'keyword':'加工烹饪'},token)
    data=filtered['data']
    results.append({'tool':'camera_scan','keyword':'加工烹饪','ok':data['complete'] is True and data['total']==data['recordsScanned']==len(data['list']),
        'total':data['total'],'recordsScanned':data['recordsScanned'],'pagesScanned':data['pagesScanned'],'complete':data['complete'],'scope':data['scope']})
    # 通用match精确匹配自检：不把ID/编号当keyword，服务端查全后本地精确比较完整字符串。
    exact_warning=await client.call(TOOLS['warning_scan'],{'match':{'id':'9300000000000001602'}},token)
    wdata=exact_warning['data']
    results.append({'tool':'warning_scan','match':{'id':'9300000000000001602'},
        'ok':len(wdata['list'])==1 and wdata['list'][0]['id']=='9300000000000001602',
        'total':wdata['total'],'pagesScanned':wdata['pagesScanned'],'complete':wdata['complete']})
    exact_camera=await client.call(TOOLS['camera_scan'],{'match':{'cameraNumber':'99990000001320000011','equipmentNumber':'99990000001320000011'}},token)
    cdata=exact_camera['data']
    results.append({'tool':'camera_scan','match':{'cameraNumber':'99990000001320000011','equipmentNumber':'99990000001320000011'},
        'ok':len(cdata['list'])==1 or (cdata['complete'] is True and not cdata['list']),
        'matched':len(cdata['list']),'total':cdata['total'],'pagesScanned':cdata['pagesScanned'],'complete':cdata['complete']})
    finally:
        await client.aclose()
    print(json.dumps(results,ensure_ascii=False,indent=2))
    return results

async def current_query_sequence():
    """隔离自检数据库和断点；经实际Engine/Gateway核对每轮上游条件，绝不写业务。"""
    import tempfile
    from cryptography.fernet import Fernet
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from app.agent import Engine,initial_state
    from app.gateway import Gateway
    from app.store import Store
    settings=Settings()
    if not settings.upstream_mock:raise ValueError('仅Mock可执行无生产凭据自检')
    results=[]
    with tempfile.TemporaryDirectory(prefix='regulatory-current-query-') as folder:
        settings=settings.model_copy(update={'data_dir':Path(folder),'fernet_key':Fernet.generate_key().decode(),'deepseek_api_key':''})
        store=Store(settings)
        await store.open()
        client=UpstreamClient(settings)
        requests=[]
        async def record(request):
            if request.url.path.endswith('/camera/management/page'):
                requests.append(json.loads(request.content))
        client.client.event_hooks={'request':[record]}
        try:
            captcha=await client.call(TOOLS['get_graphic_code'],{})
            login=await client.call(TOOLS['account_login'],{'account':'delivery-current-query-check','password':'readonly-sandbox-check','loginChannel':4,'key':captcha['data']['key'],'verifyCode':captcha['data'].get('_mockCode') or '1234'})
            await store.session_create('isolated-readonly-check',login['data']['accessToken'])
            async with AsyncSqliteSaver.from_conn_string(str(Path(folder)/'checkpoints.sqlite3')) as saver:
                await saver.setup()
                engine=Engine(settings,store,Gateway(store,client),saver)
                cid=None
                for index,prompt in enumerate(('查询所有摄像头信息','查询就餐区的摄像头信息','用列表形式列出','那加工烹饪呢','查询就餐区的摄像头信息')):
                    context=await store.conversation_context(cid,'isolated-readonly-check') if cid else {}
                    rid,_=await store.create_conversation_run('isolated-readonly-check',prompt,f'current-query-{index}',{'conversation_id':cid},context)
                    before=len(requests)
                    run=await engine.invoke(initial_state(rid,'isolated-readonly-check',prompt,'agent',context=context),rid)
                    cid=run['conversation_id']
                    output=run['result']
                    steps=output.get('steps',[])
                    data=steps[0]['data'] if steps else {}
                    expected='就餐区' if index in (1,2,4) else ('加工烹饪' if index==3 else None)
                    new_requests=requests[before:]
                    rows=data.get('list',[])
                    ok=run['status']=='completed' and not run['approvals'] and bool(rows)
                    if index==2:
                        ok=ok and output['context_reused'] is True and not new_requests and all(row['equipmentName']=='就餐区' for row in rows)
                    else:
                        ok=ok and output['context_reused'] is False and bool(new_requests) and all(args.get('keyword')==expected for args in new_requests)
                        if expected:ok=ok and all(row['equipmentName']==expected for row in rows)
                    results.append({'prompt':prompt,'ok':ok,'status':run['status'],'context_reused':output['context_reused'],'current_evidence_count':output['current_evidence_count'],
                        'new_camera_requests':new_requests,'total':data.get('total'),'complete':data.get('complete'),'sceneNames':sorted(set(row['equipmentName'] for row in rows)),
                        'raw_business_writes':False,'audit_valid':(await store.verify())['valid']})
        finally:
            await client.aclose()
            await store.close()
    print(json.dumps(results,ensure_ascii=False,indent=2))
    return results

def export_contracts():
    (ROOT/'tool-contracts.json').write_text(json.dumps(catalog_json(),ensure_ascii=False,indent=2),encoding='utf-8')
    versions={name:importlib.metadata.version(name) for name in ['fastapi','uvicorn','pydantic','pydantic-settings','httpx','aiosqlite','cryptography','langgraph','langgraph-checkpoint-sqlite','langchain-openai','pytest','pytest-asyncio']}
    (ROOT/'backend'/'requirements.lock.txt').write_text('\n'.join(f'{name}=={version}' for name,version in versions.items())+'\n',encoding='utf-8')

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--upstream-readonly',action='store_true')
    parser.add_argument('--current-query-sequence',action='store_true')
    args=parser.parse_args()
    export_contracts()
    if args.upstream_readonly:
        results=asyncio.run(readonly_check())
        (ROOT/'upstream-readonly-check.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    if args.current_query_sequence:
        results=asyncio.run(current_query_sequence())
        (ROOT/'current-query-readonly-check.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        if not all(row['ok'] for row in results):raise SystemExit(1)
