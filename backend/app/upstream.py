"""上游 HTTP 适配器。签名必须基于实际发送的 UTF-8 字节，禁止修改后重新序列化。
此模块不对外提供路由；唯一调用入口为服务端安全网关。
"""
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import quote
import httpx
from pydantic import ValidationError
from .contracts import Empty, ScanQuery
from .store import digest

class UpstreamError(Exception):
    def __init__(self, code, message, http_status=502, uncertain=False):
        super().__init__(message)
        self.code,self.message,self.http_status,self.uncertain=code,message,http_status,uncertain

class UpstreamClient:
    def __init__(self, settings, transport=None):
        self.settings=settings
        self.client=httpx.AsyncClient(base_url=settings.upstream_base_url.rstrip('/'),timeout=settings.upstream_timeout,
            verify=settings.upstream_strict_tls,follow_redirects=False,trust_env=False,transport=transport)

    async def aclose(self):
        await self.client.aclose()

    def signed_headers(self, path, method, body, access_token):
        if not access_token:
            raise UpstreamError(401,'平台会话无效，请重新登录',401)
        ts=str(time.time_ns()//1000000)
        rs=secrets.token_hex(16)
        content_md5=hashlib.md5(f'{len(body) if body else ""}:{ts}'.encode()).hexdigest()
        message=f'{path};{ts};{method};{rs};{content_md5};{access_token}'
        return {'ak':self.settings.upstream_ak,'ts':ts,'rs':rs,'Authorization':access_token,
                'Content-MD5':content_md5,'sign':hmac.new(self.settings.upstream_secret.encode(),message.encode(),hashlib.sha256).hexdigest()}

    async def call(self, spec, args, access_token='', audit=None):
        """校验入参→签名→单次发送→校验响应。写请求永不盲目重试；超时标记结果不确定。"""
        try:
            request=spec.input_model.model_validate(args)
        except ValidationError as e:
            raise UpstreamError(400,str(e),422) from e
        if not spec.available:
            code=404 if spec.name=='unknown_api' else 501
            raise UpstreamError(code,'mock 未实现该接口' if code==404 else '直播视频接口暂不开放',code)
        if isinstance(request,ScanQuery):
            from .pagination import scan_pages
            return await scan_pages(self,spec,request,access_token,audit)
        body=b'' if isinstance(request,Empty) else json.dumps(request.model_dump(exclude_none=True),ensure_ascii=False,separators=(',',':')).encode('utf-8')
        headers={'Content-Type':'application/json'}
        if spec.risk!='auth':
            headers.update(self.signed_headers(spec.path,spec.method,body,access_token))
        try:
            async with self.client.stream(spec.method,spec.path,content=body,headers=headers) as response:
                chunks=[]
                size=0
                async for chunk in response.aiter_bytes():
                    size+=len(chunk)
                    if size>4*1024*1024:
                        raise UpstreamError(502,'上游响应超过4MB安全限制',502,spec.risk in ('write','sensitive'))
                    chunks.append(chunk)
                raw=b''.join(chunks)
                status=response.status_code
            # HTTP鉴权失败优先于可能矛盾/非JSON的响应体，绝不包装成部分成功。
            if status==401:raise UpstreamError(401,'平台会话失效，请重新登录',401)
            data=json.loads(raw)
            if status>=400 or data.get('code')!=0:
                raise UpstreamError(data.get('code',status),data.get('msg','上游HTTP错误'),401 if data.get('code')==401 else (status if status>=400 else 422))
            validated=spec.response_model.model_validate(data)
            return validated.model_dump(by_alias=True,mode='json')
        except httpx.HTTPError as e:
            raise UpstreamError(503,'上游连接失败或超时，请检查内网可达性；写入结果须核对',503,spec.risk in ('write','sensitive')) from e
        except (ValueError,ValidationError) as e:
            raise UpstreamError(502,'上游响应不符合文档契约',502,spec.risk in ('write','sensitive')) from e

    async def snapshot(self, channel_id, access_token):
        """Fetch one JPEG frame from the documented, allow-listed video forwarding endpoint."""
        path='/api/video/snapshot/'+quote(channel_id,safe='')
        headers=self.signed_headers(path,'GET',b'',access_token)
        try:
            async with self.client.stream('GET',path,headers=headers) as response:
                status=response.status_code
                content_type=response.headers.get('content-type','').split(';',1)[0].lower()
                chunks=[]
                size=0
                async for chunk in response.aiter_bytes():
                    size+=len(chunk)
                    if size>self.settings.vision_max_image_bytes:
                        raise UpstreamError(502,'视频截帧超过图片安全大小限制',502)
                    chunks.append(chunk)
            if status==401:
                raise UpstreamError(401,'平台会话失效，请重新登录',401)
            if status>=400:
                raise UpstreamError(status,'上游视频截帧请求失败',status)
            if content_type not in ('image/jpeg','image/jpg'):
                raise UpstreamError(502,'上游截帧响应不是 JPEG 图片',502)
            content=b''.join(chunks)
            if not content:
                raise UpstreamError(502,'上游截帧为空',502)
            return content,'image/jpeg'
        except httpx.HTTPError as e:
            raise UpstreamError(503,'上游视频截帧连接失败或超时',503) from e
