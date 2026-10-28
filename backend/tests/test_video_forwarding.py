import asyncio
import io

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from app.main import create_app
from test_system import login, settings, setup_users, transport, wait


def jpeg_bytes():
    image=Image.new('RGB',(48,32),'white')
    output=io.BytesIO()
    image.save(output,format='JPEG')
    return output.getvalue()


def test_video_channels_and_snapshot_are_integrated_without_auditing_bytes(tmp_path):
    config=settings(tmp_path)
    asyncio.run(setup_users(config))
    fallback=transport([])
    calls=[]

    def respond(request):
        calls.append(request)
        if request.url.path=='/api/video/channels':
            return httpx.Response(200,json={'code':0,'msg':'ok','data':{'total':1,'list':[{'channelId':'CHANNEL-1','name':'测试通道','scene':'清洁区','orgName':'测试机构','status':1,'hasVideo':True}]}})
        if request.url.path=='/api/video/snapshot/CHANNEL-1':
            return httpx.Response(200,content=jpeg_bytes(),headers={'content-type':'image/jpeg'})
        return fallback.handle_request(request)

    with TestClient(create_app(config,httpx.MockTransport(respond))) as client:
        csrf,_=login(client,'operator')
        headers={'X-CSRF-Token':csrf}
        channels=client.post('/agent-api/video/channels',headers=headers,json={'pageNum':1,'pageSize':20,'hasVideo':True})
        assert channels.status_code==200,channels.text
        assert channels.json()['data']['list'][0]['channelId']=='CHANNEL-1'
        frame=client.post('/agent-api/video/snapshot',headers=headers,json={'channelId':'CHANNEL-1'})
        assert frame.status_code==200,frame.text
        captured=frame.json()['data']
        assert captured['channelId']=='CHANNEL-1' and len(captured['imageHash'])==64

        invoke=client.post('/agent-api/tools/video_snapshot/invoke',headers=headers,json={'arguments':{'channelId':'CHANNEL-1'},'idempotency_key':'video-snapshot-001'})
        assert invoke.status_code==200,invoke.text
        run=wait(client,invoke.json()['id'],{'completed','failed'})
        assert run['status']=='completed',run
        result=run['result']['steps'][0]['data']
        assert result['channelId']=='CHANNEL-1' and result['imageId']
        event=next(item for item in run['events'] if item['kind']=='video.snapshot.captured')
        assert event['payload']['image_hash']==result['imageHash']
        assert 'ffd8ff' not in str(event['payload']).lower()
        assert 'JPEG 截帧' in run['result']['display_report']
        assert client.get('/agent-api/audit/verify').json()['valid']
    snapshots=[request for request in calls if request.url.path=='/api/video/snapshot/CHANNEL-1']
    assert len(snapshots)==2
    assert all(request.headers.get('authorization')=='token-operator' for request in snapshots)
