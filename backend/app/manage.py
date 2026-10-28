"""运维CLI：生成密钥、显式创建本地角色；不提供公开自注册或默认超级账号。"""
import argparse
import asyncio
import getpass
import os
from uuid import uuid4
from cryptography.fernet import Fernet, InvalidToken
from .config import Settings
from .store import Store
from .security import password_hash, password_policy_error


def read_password():
    password=os.environ.get('BOOTSTRAP_PASSWORD') or getpass.getpass('本应用密码：')
    error=password_policy_error(password)
    if error:
        raise ValueError(error)
    return password

async def create_user(args):
    settings=Settings()
    store=Store(settings)
    await store.open()
    password=read_password()
    if await store.one('SELECT id FROM users WHERE account=?',(args.account,)):
        raise ValueError('账号已存在，不会覆盖原角色/密码')
    user_id=uuid4().hex
    await store.execute('INSERT INTO users VALUES(?,?,?,?,?)',(user_id,args.account,password_hash(password),args.role,args.nickname or args.account))
    await store.event('admin','user.created',{'user_id':user_id,'role':args.role})
    await store.close()
    print('账号已创建：'+args.account+'；角色：'+args.role)


async def set_password(args):
    settings=Settings()
    store=Store(settings)
    await store.open()
    try:
        user=await store.one('SELECT id FROM users WHERE account=?',(args.account,))
        if not user:
            raise ValueError('账号不存在')
        password=read_password()
        await store.execute('UPDATE users SET password_hash=? WHERE id=?',(password_hash(password),user['id']))
        # Password rotation invalidates existing browser and upstream platform sessions.
        await store.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
        await store.event('admin','user.password_rotated',{'user_id':user['id']})
    finally:
        await store.close()
    print('密码已更新，所有既有会话已失效：'+args.account)


async def rotate_fernet_key(args):
    """Re-encrypt all encrypted columns before changing FERNET_KEY in the environment."""
    new_key=os.environ.get('ROTATED_FERNET_KEY','')
    if not new_key:
        raise ValueError('请通过 ROTATED_FERNET_KEY 环境变量提供新密钥')
    try:
        replacement=Fernet(new_key.encode())
    except (ValueError, TypeError) as exc:
        raise ValueError('ROTATED_FERNET_KEY 不是有效的 Fernet 密钥') from exc

    settings=Settings()
    if new_key==settings.fernet_key:
        raise ValueError('新旧 Fernet 密钥不能相同')
    store=Store(settings)
    await store.open()
    fields=(
        ('sessions','id_hash','token_cipher'),
        ('approvals','id','arguments'),
        ('operations','key','result_cipher'),
        ('conversation_turns','run_id','context_cipher'),
        ('run_context','run_id','context_cipher'),
    )
    try:
        async with store.lock:
            await store.db.execute('BEGIN IMMEDIATE')
            for table,key,column in fields:
                rows=await (await store.db.execute(f'SELECT {key},{column} FROM {table} WHERE {column} IS NOT NULL')).fetchall()
                for row in rows:
                    try:
                        plaintext=store.fernet.decrypt(row[column].encode())
                    except InvalidToken as exc:
                        raise ValueError(f'无法使用当前密钥解密 {table}.{column}') from exc
                    await store.db.execute(f'UPDATE {table} SET {column}=? WHERE {key}=?',(replacement.encrypt(plaintext).decode(),row[key]))
            await store.db.commit()
            store.fernet=replacement
        await store.event('admin','security.fernet_rotated',{'actor':'local-admin'})
    except Exception:
        await store.db.rollback()
        raise
    finally:
        await store.close()
    print('密钥轮换完成。请立即将 .env 中的 FERNET_KEY 替换为新值，然后重启服务。')

def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('key')
    create=sub.add_parser('create-user')
    create.add_argument('--account',required=True)
    create.add_argument('--role',choices=['operator','reviewer','admin'],default='operator')
    create.add_argument('--nickname')
    reset=sub.add_parser('set-password')
    reset.add_argument('--account',required=True)
    sub.add_parser('rotate-fernet-key')
    args=parser.parse_args()
    if args.command=='key':
        print(Fernet.generate_key().decode())
    elif args.command=='create-user':
        asyncio.run(create_user(args))
    elif args.command=='set-password':
        asyncio.run(set_password(args))
    else:
        asyncio.run(rotate_fernet_key(args))
if __name__=='__main__':
    main()
