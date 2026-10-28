"""环境配置：上游平台与本应用端口明确分离，密钥仅在服务器保存。"""
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / '.env', extra='ignore', populate_by_name=True)
    app_env: Literal['development', 'test', 'staging', 'production'] = Field('development', validation_alias='APP_ENV')
    public_base_url: str = Field('http://127.0.0.1:8080', validation_alias='PUBLIC_BASE_URL')
    require_https: bool = Field(False, validation_alias='REQUIRE_HTTPS')
    upstream_base_url: str = ''
    upstream_ak: str = ''
    upstream_secret: str = ''
    upstream_timeout: float = Field(20, gt=0, le=120)
    upstream_strict_tls: bool = True
    upstream_auth_mode: str = 'mock'  # production 使用 sha256
    upstream_mock: bool = False
    # Keep the existing internal names while accepting generic OpenAI-compatible provider variables.
    deepseek_api_key: str = Field('', validation_alias=AliasChoices('OPENAI_API_KEY', 'DEEPSEEK_API_KEY'))
    deepseek_base_url: str = Field('https://api.deepseek.com', validation_alias=AliasChoices('OPENAI_BASE_URL', 'DEEPSEEK_BASE_URL'))
    deepseek_model: str = Field('deepseek-flash', validation_alias=AliasChoices('MODEL_NAME', 'DEEPSEEK_MODEL'))
    llm_timeout: float = 90
    max_iterations: int = Field(20, ge=1, le=20)
    data_dir: Path = Path('data')
    fernet_key: str = Field('', validation_alias='FERNET_KEY')
    cookie_secure: bool = True
    session_hours: int = Field(8, ge=1, le=24)
    allowed_origins: str = 'https://localhost'
    api_prefix: str = '/agent-api'
    max_request_bytes: int = 1048576
    vision_max_image_bytes: int = Field(8388608, ge=1048576, le=33554432)
    video_monitor_enabled: bool = Field(True, validation_alias='VIDEO_MONITOR_ENABLED')
    video_monitor_tick_seconds: int = Field(5, ge=1, le=60, validation_alias='VIDEO_MONITOR_TICK_SECONDS')
    reviewer_must_differ: bool = True
    enforce_password_policy: bool = Field(False, validation_alias='ENFORCE_PASSWORD_POLICY')

    @property
    def origins(self):
        return [x.strip() for x in self.allowed_origins.split(',') if x.strip()]

    @model_validator(mode='after')
    def production_security_baseline(self):
        if not self.fernet_key:
            raise ValueError('FERNET_KEY is required; generate it with python -m app.manage key')
        if self.app_env not in ('staging', 'production'):
            return self

        if self.upstream_mock:
            raise ValueError('UPSTREAM_MOCK must be false outside development/test')
        if self.upstream_auth_mode != 'sha256':
            raise ValueError('UPSTREAM_AUTH_MODE must be sha256 outside development/test')
        if not self.upstream_ak or not self.upstream_secret:
            raise ValueError('UPSTREAM_AK and UPSTREAM_SECRET are required outside development/test')
        if urlparse(self.upstream_base_url).scheme != 'https' or not urlparse(self.upstream_base_url).netloc:
            raise ValueError('UPSTREAM_BASE_URL must be an HTTPS URL outside development/test')
        if not self.cookie_secure or not self.require_https:
            raise ValueError('COOKIE_SECURE and REQUIRE_HTTPS must be true outside development/test')
        if urlparse(self.public_base_url).scheme != 'https' or not urlparse(self.public_base_url).netloc:
            raise ValueError('PUBLIC_BASE_URL must be an HTTPS URL outside development/test')
        if not self.enforce_password_policy:
            raise ValueError('ENFORCE_PASSWORD_POLICY must be true outside development/test')
        if not self.origins or any(urlparse(origin).scheme != 'https' for origin in self.origins):
            raise ValueError('ALLOWED_ORIGINS must contain only HTTPS origins outside development/test')
        return self
