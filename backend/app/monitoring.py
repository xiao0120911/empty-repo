"""Persistent periodic video snapshot inspection and internal alert lifecycle."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from cryptography.fernet import InvalidToken

from .store import dumps, now
from .upstream import UpstreamError
from .vision import VisionError


SCENE_TO_TOOL = {
    'rodent_board': 'vision_rodent_board',
    'clean_door': 'vision_clean_door',
}


class MonitorService:
    def __init__(self, settings, store, upstream, vision):
        self.settings = settings
        self.store = store
        self.upstream = upstream
        self.vision = vision

    @staticmethod
    def _next_capture(interval):
        return (datetime.now(timezone.utc) + timedelta(seconds=interval)).isoformat()

    @staticmethod
    def public_monitor(row):
        return {
            'id': row['id'], 'channelId': row['channel_id'], 'scene': row['scene'],
            'roi': json.loads(row['roi']), 'intervalSeconds': row['interval_seconds'],
            'openThresholdSeconds': row['open_threshold_seconds'], 'minConfidence': row['min_confidence'],
            'enabled': bool(row['enabled']), 'createdAt': row['created_at'], 'updatedAt': row['updated_at'],
            'nextCaptureAt': row['next_capture_at'], 'lastCaptureAt': row['last_capture_at'],
            'lastStatus': row['last_status'], 'lastError': row['last_error'],
        }

    @staticmethod
    def public_alert(row):
        return {
            'id': row['id'], 'monitorId': row['monitor_id'], 'observationId': row['observation_id'],
            'channelId': row['channel_id'], 'scene': row['scene'], 'status': row['status'],
            'confidence': row['confidence'], 'reason': row['reason'], 'imageId': row['image_id'],
            'imageHash': row['image_hash'], 'state': row['state'], 'firstDetectedAt': row['first_detected_at'],
            'lastDetectedAt': row['last_detected_at'], 'occurrenceCount': row['occurrence_count'],
            'acknowledgedBy': row['acknowledged_by'], 'acknowledgedAt': row['acknowledged_at'],
            'note': row['note'], 'resolvedAt': row['resolved_at'],
        }

    async def create(self, owner, arguments):
        token = await self.store.owner_token(owner)
        monitor_id = uuid4().hex
        stamp = now()
        await self.store.execute(
            'INSERT INTO video_monitors VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (monitor_id, owner, arguments['channelId'], arguments['scene'], dumps(arguments['roi']),
             arguments['intervalSeconds'], arguments['openThresholdSeconds'], arguments['minConfidence'], 1,
             self.store.fernet.encrypt(token.encode()).decode(), stamp, stamp, stamp, None, None, None),
        )
        await self.store.event('monitor:' + monitor_id, 'monitor.created', {
            'monitor_id': monitor_id, 'owner_id': owner, 'channel_id': arguments['channelId'],
            'scene': arguments['scene'], 'roi': arguments['roi'], 'interval_seconds': arguments['intervalSeconds'],
            'open_threshold_seconds': arguments['openThresholdSeconds'], 'min_confidence': arguments['minConfidence'],
        })
        return self.public_monitor(await self.store.one('SELECT * FROM video_monitors WHERE id=?', (monitor_id,)))

    async def list(self, owner):
        rows = await self.store.fetch('SELECT * FROM video_monitors WHERE owner_id=? ORDER BY created_at DESC', (owner,))
        return [self.public_monitor(row) for row in rows]

    async def update(self, owner, monitor_id, arguments):
        current = await self.store.one('SELECT * FROM video_monitors WHERE id=? AND owner_id=?', (monitor_id, owner))
        if not current:
            raise ValueError('监测任务不存在或不属于当前账号')
        values = {
            'enabled': current['enabled'], 'interval_seconds': current['interval_seconds'],
            'open_threshold_seconds': current['open_threshold_seconds'], 'min_confidence': current['min_confidence'],
            'roi': current['roi'], 'next_capture_at': current['next_capture_at'],
        }
        if 'enabled' in arguments:
            values['enabled'] = int(arguments['enabled'])
            if arguments['enabled']:
                values['next_capture_at'] = now()
        if 'intervalSeconds' in arguments:
            values['interval_seconds'] = arguments['intervalSeconds']
        if 'openThresholdSeconds' in arguments:
            values['open_threshold_seconds'] = arguments['openThresholdSeconds']
        if 'minConfidence' in arguments:
            values['min_confidence'] = arguments['minConfidence']
        if 'roi' in arguments:
            values['roi'] = dumps(arguments['roi'])
        stamp = now()
        await self.store.execute(
            'UPDATE video_monitors SET enabled=?,interval_seconds=?,open_threshold_seconds=?,min_confidence=?,roi=?,next_capture_at=?,updated_at=? WHERE id=?',
            (values['enabled'], values['interval_seconds'], values['open_threshold_seconds'], values['min_confidence'],
             values['roi'], values['next_capture_at'], stamp, monitor_id),
        )
        await self.store.event('monitor:' + monitor_id, 'monitor.updated', {
            'monitor_id': monitor_id, 'owner_id': owner, 'changed_fields': sorted(arguments),
            'enabled': bool(values['enabled']),
        })
        return self.public_monitor(await self.store.one('SELECT * FROM video_monitors WHERE id=?', (monitor_id,)))

    async def refresh_token(self, owner, monitor_id):
        token = await self.store.owner_token(owner)
        row = await self.store.one('SELECT id FROM video_monitors WHERE id=? AND owner_id=?', (monitor_id, owner))
        if not row:
            raise ValueError('监测任务不存在或不属于当前账号')
        await self.store.execute(
            'UPDATE video_monitors SET token_cipher=?,next_capture_at=?,last_error=NULL,updated_at=? WHERE id=?',
            (self.store.fernet.encrypt(token.encode()).decode(), now(), now(), monitor_id),
        )
        await self.store.event('monitor:' + monitor_id, 'monitor.token_refreshed', {'monitor_id': monitor_id, 'owner_id': owner})
        return self.public_monitor(await self.store.one('SELECT * FROM video_monitors WHERE id=?', (monitor_id,)))

    async def alerts(self, owner, active_only=False):
        sql = 'SELECT * FROM vision_alerts WHERE owner_id=?'
        args = [owner]
        if active_only:
            sql += " AND state IN ('open','acknowledged')"
        sql += ' ORDER BY last_detected_at DESC LIMIT 200'
        rows = await self.store.fetch(sql, tuple(args))
        return [self.public_alert(row) for row in rows]

    async def acknowledge(self, owner, alert_id, note):
        alert = await self.store.one('SELECT * FROM vision_alerts WHERE id=? AND owner_id=?', (alert_id, owner))
        if not alert:
            raise ValueError('预警不存在或不属于当前账号')
        if alert['state'] == 'resolved':
            raise ValueError('已恢复的预警不能确认')
        stamp = now()
        await self.store.execute('UPDATE vision_alerts SET state=?,acknowledged_by=?,acknowledged_at=?,note=? WHERE id=?',
                                 ('acknowledged', owner, stamp, note, alert_id))
        await self.store.event('monitor:' + alert['monitor_id'], 'monitor.alert.acknowledged', {
            'alert_id': alert_id, 'monitor_id': alert['monitor_id'], 'actor': owner,
        })
        return self.public_alert(await self.store.one('SELECT * FROM vision_alerts WHERE id=?', (alert_id,)))

    async def run_due(self):
        if not self.settings.video_monitor_enabled:
            return 0
        stamp = now()
        due = await self.store.fetch(
            'SELECT * FROM video_monitors WHERE enabled=1 AND next_capture_at<=? ORDER BY next_capture_at LIMIT 20', (stamp,)
        )
        count = 0
        for monitor in due:
            next_capture = self._next_capture(monitor['interval_seconds'])
            claimed = await self.store.execute(
                'UPDATE video_monitors SET next_capture_at=?,updated_at=? WHERE id=? AND enabled=1 AND next_capture_at<=?',
                (next_capture, stamp, monitor['id'], stamp),
            )
            if claimed:
                count += 1
                await self._inspect(monitor)
        return count

    async def _inspect(self, monitor):
        run_id = 'monitor:' + monitor['id'] + ':' + uuid4().hex
        try:
            token = self.store.fernet.decrypt(monitor['token_cipher'].encode()).decode()
        except InvalidToken:
            await self._failed(monitor, run_id, 'stored_token_invalid')
            return
        try:
            snapshot = await self.vision.capture_snapshot(run_id, monitor['owner_id'], monitor['channel_id'], self.upstream, token)
            arguments = {
                'imageId': snapshot['data']['imageId'], 'roi': json.loads(monitor['roi']),
            }
            tool = SCENE_TO_TOOL[monitor['scene']]
            if monitor['scene'] == 'clean_door':
                arguments.update(cameraId=monitor['channel_id'], openThresholdSeconds=monitor['open_threshold_seconds'])
            result = await self.vision.analyze(run_id, monitor['owner_id'], tool, arguments)
            data = result['data']
            stamp = now()
            await self.store.execute('UPDATE video_monitors SET last_capture_at=?,last_status=?,last_error=NULL,updated_at=? WHERE id=?',
                                     (data['capturedAt'], data['status'], stamp, monitor['id']))
            abnormal = bool(data['alert']) and data['confidence'] >= monitor['min_confidence']
            await self._update_alert(monitor, run_id, data, abnormal)
            await self.store.event(run_id, 'monitor.inspection.completed', {
                'monitor_id': monitor['id'], 'channel_id': monitor['channel_id'], 'scene': monitor['scene'],
                'status': data['status'], 'confidence': data['confidence'], 'alert': abnormal,
                'image_id': data['imageId'], 'image_hash': data['imageHash'], 'observation_id': data['observationId'],
            })
        except (UpstreamError, VisionError, ValueError) as exc:
            await self._failed(monitor, run_id, type(exc).__name__)

    async def _failed(self, monitor, run_id, reason):
        await self.store.execute('UPDATE video_monitors SET last_error=?,updated_at=? WHERE id=?', (reason, now(), monitor['id']))
        await self.store.event(run_id, 'monitor.inspection.failed', {
            'monitor_id': monitor['id'], 'channel_id': monitor['channel_id'], 'error_type': reason,
        })

    async def _update_alert(self, monitor, run_id, data, abnormal):
        active = await self.store.one("SELECT * FROM vision_alerts WHERE monitor_id=? AND state IN ('open','acknowledged')", (monitor['id'],))
        stamp = now()
        if abnormal:
            if active:
                await self.store.execute(
                    'UPDATE vision_alerts SET observation_id=?,status=?,confidence=?,reason=?,image_id=?,image_hash=?,last_detected_at=?,occurrence_count=occurrence_count+1 WHERE id=?',
                    (data['observationId'], data['status'], data['confidence'], data['reason'], data['imageId'], data['imageHash'], stamp, active['id']),
                )
                await self.store.event(run_id, 'monitor.alert.repeated', {'alert_id': active['id'], 'monitor_id': monitor['id']})
                return
            alert_id = uuid4().hex
            await self.store.execute(
                'INSERT INTO vision_alerts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (alert_id, monitor['id'], monitor['owner_id'], data['observationId'], monitor['channel_id'], monitor['scene'],
                 data['status'], data['confidence'], data['reason'], data['imageId'], data['imageHash'], 'open', stamp, stamp, 1,
                 None, None, None, None),
            )
            await self.store.event(run_id, 'monitor.alert.raised', {
                'alert_id': alert_id, 'monitor_id': monitor['id'], 'channel_id': monitor['channel_id'],
                'scene': monitor['scene'], 'status': data['status'], 'confidence': data['confidence'],
                'image_id': data['imageId'], 'image_hash': data['imageHash'], 'observation_id': data['observationId'],
            })
        elif data['status'] != 'uncertain' and active:
            await self.store.execute('UPDATE vision_alerts SET state=?,resolved_at=?,last_detected_at=? WHERE id=?',
                                     ('resolved', stamp, stamp, active['id']))
            await self.store.event(run_id, 'monitor.alert.resolved', {'alert_id': active['id'], 'monitor_id': monitor['id']})

    async def loop(self):
        while True:
            try:
                await self.run_due()
            except Exception:
                await self.store.event('monitor:scheduler', 'monitor.scheduler.failed', {'error_type': 'unexpected'})
            await asyncio.sleep(self.settings.video_monitor_tick_seconds)
