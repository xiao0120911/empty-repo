import io
import json

import asyncio
import httpx
from fastapi.testclient import TestClient
from PIL import Image

from app.main import create_app
from test_system import login, settings, setup_users, transport, wait


def image_bytes():
    image = Image.new("RGB", (64, 48), "white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def upload_image(client, csrf, camera_id, captured_at):
    response = client.post(
        "/agent-api/vision/images",
        headers={"X-CSRF-Token": csrf},
        files={"file": ("inspection.png", image_bytes(), "image/png")},
        data={"camera_id": camera_id, "captured_at": captured_at},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_uploaded_image_vision_tools_record_hashes_and_door_duration(tmp_path):
    config = settings(tmp_path)
    config.deepseek_api_key = "test-vision-key"
    asyncio.run(setup_users(config))
    calls = []

    def vision_response(request):
        calls.append(request)
        body = json.loads(request.content)
        assert body["model"] == config.deepseek_model
        assert request.headers["authorization"] == "Bearer test-vision-key"
        assert body["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        status = "installed" if "rodent" in body["messages"][1]["content"][0]["text"] else "open"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"status":"%s","confidence":0.92,"reason":"visible inspection evidence"}' % status}}]})

    app = create_app(config, transport([]), httpx.MockTransport(vision_response))
    with TestClient(app) as client:
        csrf, _ = login(client, "operator")
        rodent_image = upload_image(client, csrf, "CAM-RODENT", "2026-10-26T08:00:00Z")
        rodent = client.post(
            "/agent-api/tools/vision_rodent_board/invoke",
            headers={"X-CSRF-Token": csrf},
            json={"arguments": {"imageId": rodent_image["id"], "roi": {"x": 0, "y": 0, "width": 1, "height": 1}}, "idempotency_key": "vision-rodent-001"},
        )
        assert rodent.status_code == 200, rodent.text
        rodent_run = wait(client, rodent.json()["id"], {"completed", "failed"})
        assert rodent_run["status"] == "completed", rodent_run.get("error")
        result = rodent_run["result"]["steps"][0]["data"]
        assert result["status"] == "installed" and result["imageId"] == rodent_image["id"]
        assert "防鼠板识别结果" in rodent_run["result"]["display_report"]
        analyzed = next(event for event in rodent_run["events"] if event["kind"] == "vision.analyzed")
        assert analyzed["payload"]["image_hash"] == rodent_image["sha256"]
        assert "data:image" not in str(analyzed["payload"])

        first_image = upload_image(client, csrf, "CAM-DOOR", "2026-10-26T09:00:00Z")
        second_image = upload_image(client, csrf, "CAM-DOOR", "2026-10-26T09:02:00Z")
        for number, image in enumerate((first_image, second_image), 1):
            response = client.post(
                "/agent-api/tools/vision_clean_door/invoke",
                headers={"X-CSRF-Token": csrf},
                json={"arguments": {"imageId": image["id"], "cameraId": "CAM-DOOR", "roi": {"x": 0, "y": 0, "width": 1, "height": 1}, "openThresholdSeconds": 60}, "idempotency_key": f"vision-door-{number:03d}"},
            )
            assert response.status_code == 200, response.text
            door_run = wait(client, response.json()["id"], {"completed", "failed"})
            assert door_run["status"] == "completed", door_run
        door = door_run["result"]["steps"][0]["data"]
        assert door["status"] == "open" and door["durationSeconds"] == 120 and door["alert"] is True
        assert client.get("/agent-api/audit/verify").json()["valid"]
    assert len(calls) == 3
