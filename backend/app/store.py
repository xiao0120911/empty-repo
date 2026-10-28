"""SQLite 持久化：用户隔离、加密会话、运行状态、不可静默重放的操作账本及哈希链审计。
SQLite 单进程部署；不宣称能抵御数据库管理员篡改，生产建议异地保存链头。
"""
import asyncio
import hashlib
import json
import secrets
from datetime import datetime, timezone, timedelta
from uuid import uuid4
import aiosqlite
from cryptography.fernet import Fernet


def now():
    return datetime.now(timezone.utc).isoformat()

def dumps(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str)

def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()

def redact(value):
    """日志/模型脱敏；业务响应的完整内容仅存入加密证据库。"""
    if isinstance(value,dict):
        return {k:('[REDACTED]' if any(t in k.lower() for t in ('password','token','secret','authorization','shoptel','legalperson')) or k.lower() in ('image','image_data','image_base64','image_content') else redact(v)) for k,v in value.items()}
    if isinstance(value,list):
        return [redact(v) for v in value]
    return value

class Store:
    def __init__(self,settings):
        self.settings=settings
        self.lock=asyncio.Lock()
        self.db=None
        self.fernet=Fernet(settings.fernet_key.encode()) if settings.fernet_key else None
    async def open(self,recover=False):
        if not self.fernet:
            raise RuntimeError('必须配置FERNET_KEY。运行 python -m app.manage key 生成密钥。')
        self.settings.data_dir.mkdir(parents=True,exist_ok=True)
        self.db=await aiosqlite.connect(self.settings.data_dir/'application.sqlite3')
        self.db.row_factory=aiosqlite.Row
        await self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, account TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL, nickname TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(id_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,token_cipher TEXT NOT NULL,csrf TEXT NOT NULL,expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,prompt TEXT NOT NULL,mode TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,result TEXT,error TEXT,request_key TEXT NOT NULL,input TEXT NOT NULL,UNIQUE(owner_id,request_key));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,created_at TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,prev_hash TEXT NOT NULL,hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,operation_key TEXT UNIQUE NOT NULL,owner_id TEXT NOT NULL,tool_name TEXT NOT NULL,arguments TEXT NOT NULL,reason TEXT NOT NULL,payload_hash TEXT NOT NULL,status TEXT NOT NULL,requested_at TEXT NOT NULL,decided_by TEXT,decision_note TEXT);
        CREATE TABLE IF NOT EXISTS operations(key TEXT PRIMARY KEY,run_id TEXT NOT NULL,tool_name TEXT NOT NULL,args_hash TEXT NOT NULL,status TEXT NOT NULL,result_cipher TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS workflows(id TEXT PRIMARY KEY,owner_id TEXT,name TEXT NOT NULL,description TEXT NOT NULL,version INTEGER NOT NULL,definition TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,title TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS conversation_runs(run_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,ordinal INTEGER NOT NULL,UNIQUE(conversation_id,ordinal));
        CREATE TABLE IF NOT EXISTS conversation_turns(run_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,context_cipher TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS run_context(run_id TEXT PRIMARY KEY,context_cipher TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS vision_images(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,sha256 TEXT NOT NULL,mime_type TEXT NOT NULL,byte_size INTEGER NOT NULL,path TEXT NOT NULL,original_name TEXT NOT NULL,camera_id TEXT,captured_at TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS vision_observations(id TEXT PRIMARY KEY,image_id TEXT NOT NULL,run_id TEXT NOT NULL,owner_id TEXT NOT NULL,scene TEXT NOT NULL,camera_id TEXT,status TEXT NOT NULL,confidence REAL NOT NULL,alert INTEGER NOT NULL,reason TEXT NOT NULL,roi TEXT NOT NULL,captured_at TEXT NOT NULL,open_since TEXT,duration_seconds INTEGER,model TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS video_monitors(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,channel_id TEXT NOT NULL,scene TEXT NOT NULL,roi TEXT NOT NULL,interval_seconds INTEGER NOT NULL,open_threshold_seconds INTEGER NOT NULL,min_confidence REAL NOT NULL,enabled INTEGER NOT NULL,token_cipher TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,next_capture_at TEXT NOT NULL,last_capture_at TEXT,last_status TEXT,last_error TEXT);
        CREATE TABLE IF NOT EXISTS vision_alerts(id TEXT PRIMARY KEY,monitor_id TEXT NOT NULL,owner_id TEXT NOT NULL,observation_id TEXT NOT NULL,channel_id TEXT NOT NULL,scene TEXT NOT NULL,status TEXT NOT NULL,confidence REAL NOT NULL,reason TEXT NOT NULL,image_id TEXT NOT NULL,image_hash TEXT NOT NULL,state TEXT NOT NULL,first_detected_at TEXT NOT NULL,last_detected_at TEXT NOT NULL,occurrence_count INTEGER NOT NULL,acknowledged_by TEXT,acknowledged_at TEXT,note TEXT,resolved_at TEXT);
        CREATE INDEX IF NOT EXISTS conversation_owner ON conversations(owner_id,updated_at);
        CREATE INDEX IF NOT EXISTS conversation_order ON conversation_runs(conversation_id,ordinal);
        CREATE INDEX IF NOT EXISTS vision_image_owner ON vision_images(owner_id,created_at);
        CREATE INDEX IF NOT EXISTS vision_observation_camera ON vision_observations(owner_id,scene,camera_id,captured_at);
        CREATE INDEX IF NOT EXISTS video_monitor_due ON video_monitors(enabled,next_capture_at);
        CREATE INDEX IF NOT EXISTS vision_alert_owner ON vision_alerts(owner_id,state,last_detected_at);
        CREATE UNIQUE INDEX IF NOT EXISTS active_alert_per_monitor ON vision_alerts(monitor_id) WHERE state IN ('open','acknowledged');
        ''')
        await self.db.commit()
        # 仅API服务启动恢复；管理CLI创建账号不得中断正在运行的任务。
        if recover:
            await self.db.execute("UPDATE runs SET status='interrupted' WHERE status IN ('running','created')")
            await self.db.commit()
    async def close(self):
        if self.db:
            await self.db.close()
    async def fetch(self,sql,args=()):
        async with self.lock:
            return [dict(r) for r in await (await self.db.execute(sql,args)).fetchall()]
    async def one(self,sql,args=()):
        rows=await self.fetch(sql,args)
        return rows[0] if rows else None
    async def execute(self,sql,args=()):
        async with self.lock:
            cursor=await self.db.execute(sql,args)
            await self.db.commit()
            return cursor.rowcount
    async def event(self,run_id,kind,payload):
        payload=redact(payload)
        text=dumps(payload)
        stamp=now()
        async with self.lock:
            row=await (await self.db.execute('SELECT hash FROM events ORDER BY id DESC LIMIT 1')).fetchone()
            prev=row['hash'] if row else '0'*64
            hashed=digest({'run_id':run_id,'created_at':stamp,'kind':kind,'payload':payload,'prev_hash':prev})
            await self.db.execute('INSERT INTO events(run_id,created_at,kind,payload,prev_hash,hash) VALUES(?,?,?,?,?,?)',(run_id,stamp,kind,text,prev,hashed))
            await self.db.commit()
    async def verify(self):
        rows=await self.fetch('SELECT * FROM events ORDER BY id')
        prev='0'*64
        for row in rows:
            expected=digest({'run_id':row['run_id'],'created_at':row['created_at'],'kind':row['kind'],'payload':json.loads(row['payload']),'prev_hash':prev})
            if row['prev_hash']!=prev or row['hash']!=expected:
                return {'valid':False,'event_count':len(rows),'head':prev,'bad_event':row['id']}
            prev=expected
        return {'valid':True,'event_count':len(rows),'head':prev}
    async def create_conversation_run(self,owner,prompt,request_key,inputs,context):
        """任务、会话关联及冻结上下文同事务落盘，避免崩溃留下半条对话。"""
        rid=uuid4().hex
        cid=inputs.get('conversation_id') or uuid4().hex
        stamp=now()
        cipher=self.fernet.encrypt(dumps(redact(context)).encode()).decode()
        async with self.lock:
            try:
                await self.db.execute('BEGIN IMMEDIATE')
                existing=await (await self.db.execute('SELECT id,input FROM runs WHERE owner_id=? AND request_key=?',(owner,request_key))).fetchone()
                if existing:
                    if json.loads(existing['input'])!=inputs:
                        raise ValueError('幂等键已用于不同请求')
                    await self.db.commit()
                    return existing['id'],False
                if inputs.get('conversation_id'):
                    conversation=await (await self.db.execute('SELECT id FROM conversations WHERE id=? AND owner_id=?',(cid,owner))).fetchone()
                    if not conversation:
                        raise ValueError('会话不存在或不属于当前账号')
                    blocked=await (await self.db.execute("SELECT r.id FROM runs r JOIN conversation_runs c ON c.run_id=r.id WHERE c.conversation_id=? AND r.status IN ('created','running','waiting_approval','interrupted','needs_reconciliation') LIMIT 1",(cid,))).fetchone()
                    if blocked:
                        raise ValueError('当前会话上一轮尚未结束，请先处理')
                else:
                    await self.db.execute('INSERT INTO conversations VALUES(?,?,?,?,?)',(cid,owner,prompt[:120],stamp,stamp))
                ordinal=await (await self.db.execute('SELECT COALESCE(MAX(ordinal),0)+1 AS ordinal FROM conversation_runs WHERE conversation_id=?',(cid,))).fetchone()
                await self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)',(rid,owner,prompt,'agent','created',stamp,stamp,None,None,request_key,dumps(inputs)))
                await self.db.execute('INSERT INTO conversation_runs VALUES(?,?,?)',(rid,cid,ordinal['ordinal']))
                await self.db.execute('INSERT INTO run_context VALUES(?,?)',(rid,cipher))
                await self.db.execute('UPDATE conversations SET updated_at=? WHERE id=?',(stamp,cid))
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        await self.event(rid,'task.created',{'prompt':prompt,'mode':'agent','input_hash':digest(inputs)})
        await self.event(rid,'conversation.attached',{'conversation_id':cid,'turn':ordinal['ordinal']})
        await self.event(rid,'context.loaded',{'source_run_id':context.get('source_run_id'),'message_count':len(context.get('messages',[])),'evidence_count':len(context.get('evidence',[]))})
        return rid,True

    async def conversation(self,cid,owner):
        return await self.one('SELECT * FROM conversations WHERE id=? AND owner_id=?',(cid,owner))

    async def run_conversation(self,rid):
        row=await self.one('SELECT conversation_id FROM conversation_runs WHERE run_id=?',(rid,))
        return row['conversation_id'] if row else None

    async def attach_conversation(self,rid,owner,prompt,cid=None):
        # 调用者持有账号级创建锁；仅所有者可延续，包括管理员和审核员。
        existing=await self.run_conversation(rid)
        if existing:
            return existing
        stamp=now()
        if cid is None:
            cid=uuid4().hex
            await self.execute('INSERT INTO conversations VALUES(?,?,?,?,?)',(cid,owner,prompt[:120],stamp,stamp))
        elif not await self.conversation(cid,owner):
            raise ValueError('会话不存在或不属于当前账号')
        row=await self.one('SELECT COALESCE(MAX(ordinal),0)+1 AS ordinal FROM conversation_runs WHERE conversation_id=?',(cid,))
        await self.execute('INSERT INTO conversation_runs VALUES(?,?,?)',(rid,cid,row['ordinal']))
        await self.execute('UPDATE conversations SET updated_at=? WHERE id=?',(stamp,cid))
        await self.event(rid,'conversation.attached',{'conversation_id':cid,'turn':row['ordinal']})
        return cid

    async def conversation_busy(self,cid):
        return await self.one("SELECT r.id,r.status FROM runs r JOIN conversation_runs c ON c.run_id=r.id WHERE c.conversation_id=? AND r.status IN ('created','running','waiting_approval','interrupted','needs_reconciliation') ORDER BY c.ordinal DESC LIMIT 1",(cid,))

    async def conversation_context(self,cid,owner):
        if not await self.conversation(cid,owner):
            raise ValueError('会话不存在或不属于当前账号')
        rows=await self.fetch("SELECT t.context_cipher,t.run_id FROM conversation_turns t JOIN conversation_runs c ON c.run_id=t.run_id JOIN runs r ON r.id=t.run_id WHERE t.conversation_id=? AND r.status='completed' ORDER BY c.ordinal DESC LIMIT 6",(cid,))
        history=[]
        evidence=[]
        source=None
        for row in reversed(rows):
            turn=json.loads(self.fernet.decrypt(row['context_cipher'].encode()))
            # 只携带完整问答对及紧凑证据，不带历史工具调用/批准授权。
            history.extend(turn['messages'])
            evidence=turn['evidence']
            source=row['run_id']
        while history and len(dumps(history))>60000:
            history=history[2:]
        latest=await self.one('SELECT r.id,r.status,r.prompt,r.result FROM runs r JOIN conversation_runs c ON c.run_id=r.id WHERE c.conversation_id=? ORDER BY c.ordinal DESC LIMIT 1',(cid,))
        if latest:
            from .intents import is_format_only
            saved=json.loads(latest['result']) if latest['result'] else {}
            saved_rows=saved.get('steps') or []
            stale=not is_format_only(latest['prompt']) and (saved.get('context_reused') is True or (bool(saved_rows) and all(row.get('source_run_id') and row.get('source_run_id')!=latest['id'] for row in saved_rows)))
            # 失败/无证据的最近任务不能使后续“列出”自动跳回更早的成功数据。
            if latest['id']!=source or latest['status']!='completed' or stale:
                evidence=[]
                source=latest['id']
        return {'messages':history,'evidence':evidence,'source_run_id':source}

    async def save_run_context(self,rid,context):
        cipher=self.fernet.encrypt(dumps(redact(context)).encode()).decode()
        await self.execute('INSERT OR REPLACE INTO run_context VALUES(?,?)',(rid,cipher))

    async def load_run_context(self,rid):
        row=await self.one('SELECT context_cipher FROM run_context WHERE run_id=?',(rid,))
        if row:
            return json.loads(self.fernet.decrypt(row['context_cipher'].encode()))
        if await self.run_conversation(rid):
            raise ValueError('该对话任务缺少冻结上下文，禁止静默丢失历史后恢复')
        return {}

    async def complete_conversation_turn(self,rid,prompt,report,evidence):
        cid=await self.run_conversation(rid)
        if not cid:
            return
        from langchain_core.messages import HumanMessage,AIMessage,messages_to_dict
        context={'messages':messages_to_dict([HumanMessage(content=prompt),AIMessage(content=str(report)[:16000])]),'evidence':redact(evidence)}
        cipher=self.fernet.encrypt(dumps(context).encode()).decode()
        await self.execute('INSERT OR REPLACE INTO conversation_turns VALUES(?,?,?)',(rid,cid,cipher))
        await self.execute('UPDATE conversations SET updated_at=? WHERE id=?',(now(),cid))

    async def run(self,run_id):
        row=await self.one('SELECT * FROM runs WHERE id=?',(run_id,))
        if not row:
            return None
        row['result']=json.loads(row['result']) if row['result'] else None
        row.pop('input',None)
        row['conversation_id']=await self.run_conversation(run_id)
        events=await self.fetch('SELECT * FROM events WHERE run_id=? ORDER BY id',(run_id,))
        for e in events:
            e['payload']=json.loads(e['payload'])
        row['events']=events
        row['approvals']=await self.approvals(run_id)
        from .reports import display_result
        return display_result(row)
    async def approvals(self,run_id=None):
        rows=await self.fetch('SELECT * FROM approvals'+(' WHERE run_id=?' if run_id else " WHERE status='pending'")+' ORDER BY requested_at', (run_id,) if run_id else ())
        for r in rows:
            r['arguments']=json.loads(self.fernet.decrypt(r['arguments'].encode()))
        return rows
    async def create_run(self,owner,prompt,mode,request_key,inputs):
        existing=await self.one('SELECT * FROM runs WHERE owner_id=? AND request_key=?',(owner,request_key))
        if existing:
            if json.loads(existing['input'])!=inputs:
                raise ValueError('幂等键已用于不同请求')
            return existing['id'],False
        rid=uuid4().hex
        stamp=now()
        await self.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)',(rid,owner,prompt,mode,'created',stamp,stamp,None,None,request_key,dumps(inputs)))
        await self.event(rid,'task.created',{'prompt':prompt,'mode':mode,'input_hash':digest(inputs)})
        return rid,True
    async def set_run(self,rid,status,result=None,error=None):
        await self.execute('UPDATE runs SET status=?,updated_at=?,result=?,error=? WHERE id=?',(status,now(),dumps(redact(result)) if result is not None else None,error,rid))
    async def session_create(self,user_id,upstream_token):
        sid=secrets.token_urlsafe(32)
        csrf=secrets.token_urlsafe(32)
        expiry=(datetime.now(timezone.utc)+timedelta(hours=self.settings.session_hours)).isoformat()
        await self.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(hashlib.sha256(sid.encode()).hexdigest(),user_id,self.fernet.encrypt(upstream_token.encode()).decode(),csrf,expiry))
        return sid,csrf
    async def session(self,sid):
        if not sid:
            return None
        row=await self.one('SELECT s.*,u.account,u.nickname,u.role FROM sessions s JOIN users u ON s.user_id=u.id WHERE id_hash=?',(hashlib.sha256(sid.encode()).hexdigest(),))
        if not row or row['expires_at']<now():
            return None
        row['token']=self.fernet.decrypt(row.pop('token_cipher').encode()).decode()
        return row
    async def owner_token(self,owner):
        row=await self.one('SELECT token_cipher FROM sessions WHERE user_id=? AND expires_at>? ORDER BY expires_at DESC LIMIT 1',(owner,now()))
        if not row:
            raise ValueError('任务发起者的平台会话已过期，请重新登录再恢复')
        return self.fernet.decrypt(row['token_cipher'].encode()).decode()
