import time
import json
import hashlib
import hmac
import requests
from typing import Any, Dict, Optional
from core.config import settings

class MockAPIClient:
    def __init__(self):
        self.base_url = settings.API_BASE_URL
        self.access_token = None

    def login(self):
        # 1. Get graphic code
        url1 = f"{self.base_url}/api/cas/user/client/login/getGraphicCode"
        resp1 = requests.post(url1).json()
        if resp1.get("code") != 0:
            raise Exception("Failed to get graphic code")
        key = resp1["data"]["key"]
        
        # 2. Login
        url2 = f"{self.base_url}/api/cas/user/client/login/account-psw"
        payload = {
            "account": "admin",
            "password": "password",
            "loginChannel": 4,
            "key": key,
            "verifyCode": "1234"
        }
        resp2 = requests.post(url2, json=payload).json()
        if resp2.get("code") != 0:
            raise Exception(f"Login failed: {resp2.get('msg')}")
        self.access_token = resp2["data"]["accessToken"]

    def _generate_signature_headers(self, path: str, method: str, body: Optional[Dict] = None) -> Dict[str, str]:
        ak = "MOCKAK2026"
        secret = "mock-secret-key-2026"
        ts = str(int(time.time() * 1000))
        rs = "12345678901234567890123456789012"
        
        if body:
            body_bytes = json.dumps(body, separators=(',', ':')).encode('utf-8')
            content_md5 = hashlib.md5(f"{len(body_bytes)}:{ts}".encode('utf-8')).hexdigest()
        else:
            content_md5 = hashlib.md5(f":{ts}".encode('utf-8')).hexdigest()
            
        msg = f"{path};{ts};{method.upper()};{rs};{content_md5};{self.access_token}"
        sign = hmac.new(secret.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).hexdigest()
        
        return {
            "ak": ak,
            "ts": ts,
            "rs": rs,
            "Content-MD5": content_md5,
            "sign": sign,
            "Authorization": self.access_token or "",
            "Content-Type": "application/json"
        }

    def request(self, method: str, path: str, json_data: Optional[Dict] = None) -> Dict[str, Any]:
        if not self.access_token:
            self.login()
            
        url = f"{self.base_url}{path}"
        headers = self._generate_signature_headers(path, method, json_data)
        
        response = requests.request(method, url, json=json_data, headers=headers)
        if response.status_code == 401:
            # Token might have expired or server restarted, try login again
            self.login()
            headers = self._generate_signature_headers(path, method, json_data)
            response = requests.request(method, url, json=json_data, headers=headers)
            
        return response.json()

api_client = MockAPIClient()
