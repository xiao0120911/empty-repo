import asyncio
import io
import json

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from app.main import create_app
from test_system import login, settings, setup_users, transport


def jpeg_bytes():
    image = Image.new('RGB', (48, 32), 'white')
    output = io.BytesIO()
    image.save(output, format='JPEG')
    return output.getvalue()


def test_periodic_snapshot_monitor_raises_deduplicates_and_resolves_alert(tmp_path):
    config = settings(tmp_path)
    config.deepseek_api_key = 'monitor-model-key'
    config.video_monitor_tick_seconds = 60
    asyncio.run(setup_users(config))
    fallback = transport([])
    model_statuses = iter(('missing', 'installed'))

    def upstream_response(request):
        if request.url.path == '/api/video/snapshot/CHANNEL-1':
            return httpx.Response(200, content=jpeg_bytes(), headers={'content-type': 'image/jpeg'})
        return fallback.handle_request(request)

    def vision_response(request):
        body = json.loads(request.content)
        assert body['messages'][1]['content'][0]['text'].startswith('Classify the rodent')
        status = next(model_statuses)
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
            'status': status, 'confidence': 0.95, 'reason': 'test evidence',
        })}}]})

    app = create_app(config, httpx.MockTransport(upstream_response), httpx.MockTransport(vision_response))
    with TestClient(app) as client:
        csrf, _ = login(client, 'operator')
        headers = {'X-CSRF-Token': csrf}
        created = client.post('/agent-api/video/monitors', headers=headers, json={
            'channelId': 'CHANNEL-1', 'scene': 'rodent_board',
            'roi': {'x': 0, 'y': 0, 'width': 1, 'height': 1},
            'intervalSeconds': 30, 'openThresholdSeconds': 60, 'minConfidence': 0.8,
        })
        assert created.status_code == 200, created.text
        monitor_id = created.json()['id']
        assert created.json()['enabled'] is True

        assert client.portal.call(app.state.monitor.run_due) == 1
        alerts = client.get('/agent-api/video/alerts?active_only=true', headers=headers)
        assert alerts.status_code == 200
        assert len(alerts.json()) == 1
        alert = alerts.json()[0]
        assert alert['state'] == 'open' and alert['status'] == 'missing' and alert['occurrenceCount'] == 1

        acknowledged = client.post('/agent-api/video/alerts/' + alert['id'] + '/acknowledge', headers=headers, json={'note': '已通知现场核查'})
        assert acknowledged.status_code == 200
        assert acknowledged.json()['state'] == 'acknowledged'

        disabled = client.post('/agent-api/video/monitors/' + monitor_id, headers=headers, json={'enabled': False})
        assert disabled.status_code == 200
        enabled = client.post('/agent-api/video/monitors/' + monitor_id, headers=headers, json={'enabled': True})
        assert enabled.status_code == 200
        assert client.portal.call(app.state.monitor.run_due) == 1

        active = client.get('/agent-api/video/alerts?active_only=true', headers=headers).json()
        assert active == []
        all_alerts = client.get('/agent-api/video/alerts', headers=headers).json()
        assert all_alerts[0]['state'] == 'resolved' and all_alerts[0]['resolvedAt']
        kinds = client.portal.call(app.state.store.fetch, "SELECT kind FROM events WHERE kind LIKE 'monitor.alert.%' ORDER BY id")
        assert [row['kind'] for row in kinds] == ['monitor.alert.raised', 'monitor.alert.acknowledged', 'monitor.alert.resolved']
        assert client.get('/agent-api/audit/verify').json()['valid']
