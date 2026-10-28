import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    API_BASE_URL: str = "http://10.7.16.145:8002"
    DEEPSEEK_API_KEY: str = "sk-8fb96a37a11a4ce7bdc34c5864ea2412" #os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"

settings = Settings()
