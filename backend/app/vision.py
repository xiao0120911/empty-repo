"""Local image storage and DeepSeek-backed visual inspection tools."""
import base64
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .store import dumps, now


ALLOWED_MIME_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
SCENE_CONFIG = {
    "vision_rodent_board": {
        "scene": "rodent_board",
        "statuses": ("installed", "missing", "misaligned", "uncertain"),
        "instruction": "Classify the rodent guard board as installed, missing, misaligned, or uncertain.",
    },
    "vision_clean_door": {
        "scene": "clean_door",
        "statuses": ("closed", "open", "uncertain"),
        "instruction": "Classify the clean-area door as closed, open, or uncertain. Do not infer elapsed time from one image.",
    },
}


class VisionError(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.message, self.code = message, status_code
        self.status_code = status_code


class ModelObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=500)


class VisionService:
    def __init__(self, settings, store, transport=None):
        self.settings = settings
        self.store = store
        self.image_dir = settings.data_dir / "vision-images"
        self.client = httpx.AsyncClient(
            base_url=settings.deepseek_base_url.rstrip("/"),
            timeout=settings.llm_timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def aclose(self):
        await self.client.aclose()

    @property
    def available(self):
        return bool(self.settings.deepseek_api_key)

    async def save_upload(self, owner, content, original_name, mime_type, camera_id=None, captured_at=None):
        if mime_type not in ALLOWED_MIME_TYPES:
            raise VisionError("仅支持 JPEG、PNG 和 WebP 图片")
        if not content:
            raise VisionError("图片不能为空")
        if len(content) > self.settings.vision_max_image_bytes:
            raise VisionError("图片超过上传大小限制", 413)
        try:
            Image.MAX_IMAGE_PIXELS = 40_000_000
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise VisionError("图片内容无效或像素数量超限") from exc

        image_id = uuid4().hex
        digest = hashlib.sha256(content).hexdigest()
        suffix = ALLOWED_MIME_TYPES[mime_type]
        self.image_dir.mkdir(parents=True, exist_ok=True)
        target = self.image_dir / f"{image_id}.{suffix}"
        temporary = self.image_dir / f".{image_id}.upload"
        temporary.write_bytes(content)
        temporary.replace(target)
        captured = self._parse_time(captured_at) if captured_at else now()
        created = now()
        await self.store.execute(
            "INSERT INTO vision_images VALUES(?,?,?,?,?,?,?,?,?,?)",
            (image_id, owner, digest, mime_type, len(content), str(target), original_name[:255], camera_id, captured, created),
        )
        await self.store.event(
            "vision:" + image_id,
            "vision.image.uploaded",
            {"image_id": image_id, "owner_id": owner, "sha256": digest, "mime_type": mime_type,
             "byte_size": len(content), "camera_id": camera_id, "captured_at": captured},
        )
        return self.public_image(image_id, digest, mime_type, len(content), camera_id, captured, created)

    async def analyze(self, run_id, owner, tool_name, arguments):
        if tool_name not in SCENE_CONFIG:
            raise VisionError("未知视觉工具")
        if not self.available:
            raise VisionError("未配置 DEEPSEEK_API_KEY，视觉识别不可用", 503)
        config = SCENE_CONFIG[tool_name]
        image = await self.store.one("SELECT * FROM vision_images WHERE id=? AND owner_id=?", (arguments["imageId"], owner))
        if not image:
            raise VisionError("图片不存在或不属于当前账号", 404)
        camera_id = arguments.get("cameraId") or image.get("camera_id")
        if config["scene"] == "clean_door" and not camera_id:
            raise VisionError("清洁区门识别必须提供 cameraId")
        payload, crop_hash = self._image_payload(image, arguments["roi"])
        observed = await self._call_model(config, payload)
        if observed.status not in config["statuses"]:
            raise VisionError("模型返回了不支持的状态值")

        captured_at = image["captured_at"]
        duration = None
        open_since = None
        alert = observed.status in ("missing", "misaligned")
        if config["scene"] == "clean_door":
            duration, open_since, alert = await self._door_timing(
                owner, camera_id, captured_at, observed.status, arguments["openThresholdSeconds"]
            )
        observation_id = uuid4().hex
        await self.store.execute(
            "INSERT INTO vision_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (observation_id, image["id"], run_id, owner, config["scene"], camera_id, observed.status,
             observed.confidence, int(alert), observed.reason, dumps(arguments["roi"]), captured_at,
             open_since, duration, self.settings.deepseek_model, now()),
        )
        result = {
            "code": 0,
            "msg": "视觉识别完成",
            "data": {
                "observationId": observation_id, "imageId": image["id"], "imageHash": image["sha256"], "scene": config["scene"],
                "status": observed.status, "confidence": observed.confidence, "alert": alert,
                "reason": observed.reason, "cameraId": camera_id, "capturedAt": captured_at,
                "durationSeconds": duration, "openSince": open_since, "model": self.settings.deepseek_model,
            },
        }
        await self.store.event(
            run_id,
            "vision.analyzed",
            {"observation_id": observation_id, "image_id": image["id"], "image_hash": image["sha256"],
             "scene": config["scene"], "camera_id": camera_id, "roi": arguments["roi"],
             "crop_hash": crop_hash, "model": self.settings.deepseek_model, "result": result["data"]},
        )
        return result

    async def capture_snapshot(self, run_id, owner, channel_id, upstream, access_token=None):
        """Store an upstream allow-listed JPEG snapshot without placing image bytes in audit events."""
        token=access_token or await self.store.owner_token(owner)
        content,mime_type=await upstream.snapshot(channel_id,token)
        image=await self.save_upload(owner,content,f'channel-{channel_id}.jpg',mime_type,channel_id,now())
        data={
            'imageId':image['id'],'imageHash':image['sha256'],'channelId':channel_id,
            'mimeType':image['mime_type'],'byteSize':image['byte_size'],'capturedAt':image['captured_at'],
        }
        await self.store.event(run_id,'video.snapshot.captured',{
            'channel_id':channel_id,'image_id':image['id'],'image_hash':image['sha256'],
            'mime_type':mime_type,'byte_size':image['byte_size'],'captured_at':image['captured_at'],
        })
        return {'code':0,'msg':'视频截帧已保存','data':data}

    async def _door_timing(self, owner, camera_id, captured_at, status, threshold):
        if status == "uncertain":
            return None, None, False
        if status == "closed":
            return 0, None, False
        previous = await self.store.one(
            "SELECT status,open_since,captured_at FROM vision_observations WHERE owner_id=? AND scene='clean_door' AND camera_id=? AND captured_at<=? ORDER BY captured_at DESC,created_at DESC LIMIT 1",
            (owner, camera_id, captured_at),
        )
        open_since = previous["open_since"] if previous and previous["status"] == "open" and previous["open_since"] else captured_at
        elapsed = max(0, int((self._as_datetime(captured_at) - self._as_datetime(open_since)).total_seconds()))
        return elapsed, open_since, elapsed >= threshold

    def _image_payload(self, image, roi):
        source = Path(image["path"])
        if not source.is_file():
            raise VisionError("图片原文件不存在", 410)
        try:
            with Image.open(source) as opened:
                opened.load()
                width, height = opened.size
                left = int(width * roi["x"])
                top = int(height * roi["y"])
                right = max(left + 1, min(width, int(width * (roi["x"] + roi["width"]))))
                bottom = max(top + 1, min(height, int(height * (roi["y"] + roi["height"]))))
                cropped = opened.crop((left, top, right, bottom)).convert("RGB")
                cropped.thumbnail((2048, 2048))
                output = io.BytesIO()
                cropped.save(output, format="JPEG", quality=90, optimize=True)
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise VisionError("无法读取图片 ROI") from exc
        encoded = output.getvalue()
        return base64.b64encode(encoded).decode("ascii"), hashlib.sha256(encoded).hexdigest()

    async def _call_model(self, config, image_base64):
        system = (
            "You are a food-safety visual inspection classifier. Return only JSON with status, confidence, and reason. "
            f"Allowed status values: {', '.join(config['statuses'])}. "
            "confidence must be a number from 0 to 1. Use uncertain when the ROI does not provide enough evidence. "
            "Never infer events outside the visible ROI."
        )
        body = {
            "model": self.settings.deepseek_model,
            "temperature": 0,
            # This provider may spend part of the completion budget on a separate
            # reasoning field.  The application only accepts the final content.
            "max_tokens": 1024,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": [
                    {"type": "text", "text": config["instruction"]},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image_base64, "detail": "high"}},
                ]},
            ],
        }
        try:
            response = await self.client.post(
                "/chat/completions",
                headers={"Authorization": "Bearer " + self.settings.deepseek_api_key, "Content-Type": "application/json"},
                json=body,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise VisionError("视觉模型调用失败，请检查 DeepSeek 配置和网络", 503) from exc
        try:
            cleaned = content.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            return ModelObservation.model_validate_json(cleaned)
        except (ValidationError, ValueError, IndexError) as exc:
            raise VisionError("视觉模型未返回符合契约的识别结果", 502) from exc

    @staticmethod
    def public_image(image_id, digest, mime_type, byte_size, camera_id, captured_at, created_at):
        return {"id": image_id, "sha256": digest, "mime_type": mime_type, "byte_size": byte_size,
                "camera_id": camera_id, "captured_at": captured_at, "created_at": created_at}

    @staticmethod
    def _parse_time(value):
        return VisionService._as_datetime(value).isoformat()

    @staticmethod
    def _as_datetime(value):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise VisionError("captured_at 必须是 ISO-8601 时间") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
