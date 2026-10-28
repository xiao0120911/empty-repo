from langchain_core.tools import tool
from core.api_client import api_client
from typing import Optional, List, Dict

# ==========================================
# 1. cas 认证服务 (1.1, 1.2 已在 api_client.py 内部处理)
# ==========================================

@tool
def get_current_user_menus() -> dict:
    """
    1.3 获取当前用户菜单树。
    无请求体。
    返回: 用户的权限菜单节点列表（包含节点id, name, path, type, code, children等）。
    """
    return api_client.request("POST", "/api/cas/user/client/permission/menus")


# ==========================================
# 2. basicData 基础数据服务
# ==========================================

@tool
def get_shop_list(page_num: int = 1, page_size: int = 10, keyword: str = "") -> dict:
    """
    2.1 获取单位分页列表。
    入参:
        page_num (int): 页码，从 1 开始
        page_size (int): 每页条数（1~100）
        keyword (str, optional): 按单位名称模糊过滤
    返回:
        dict: 包含 total 和 list 的分页数据
    """
    payload = {"pageNum": page_num, "pageSize": page_size}
    if keyword:
        payload["keyword"] = keyword
    return api_client.request("POST", "/api/basicData/shop/list", payload)

@tool
def add_shop(shop_name: str, business_type: int = 2, shop_tel: str = "", legal_person_name: str = "", business_address: str = "", registered_address: str = "", credit_score: int = 80) -> dict:
    """
    2.2 新增单位（施工人员）。
    入参:
        shop_name (str): 单位名称（必填）
        business_type (int): 单位类型(1学校食堂/2社会餐饮/3养老机构/4单位食堂/5中央厨房)，默认2
        shop_tel (str): 联系电话
        legal_person_name (str): 法人
        business_address (str): 经营地址
        registered_address (str): 注册地址
        credit_score (int): 信用分，默认80
    """
    payload = {"shopName": shop_name, "businessType": business_type, "creditScore": credit_score}
    if shop_tel: payload["shopTel"] = shop_tel
    if legal_person_name: payload["legalPersonName"] = legal_person_name
    if business_address: payload["businessAddress"] = business_address
    if registered_address: payload["registeredAddress"] = registered_address
    return api_client.request("POST", "/api/basicData/shop/devops/settleByBuilder", payload)

@tool
def update_shop(id: str, shop_name: str = "", shop_tel: str = "", legal_person_name: str = "", registered_address: str = "", credit_score: int = -1, business_address: str = "", business_type: int = -1) -> dict:
    """
    2.3 修改单位（运维平台）。
    入参:
        id (str): 单位 id（必填）
        其他参数均可选，传哪个改哪个。credit_score 和 business_type 传 -1 表示不修改。
    """
    payload = {"id": id}
    if shop_name: payload["shopName"] = shop_name
    if shop_tel: payload["shopTel"] = shop_tel
    if legal_person_name: payload["legalPersonName"] = legal_person_name
    if registered_address: payload["registeredAddress"] = registered_address
    if credit_score != -1: payload["creditScore"] = credit_score
    if business_address: payload["businessAddress"] = business_address
    if business_type != -1: payload["businessType"] = business_type
    return api_client.request("POST", "/api/basicData/shop/devops/update", payload)

@tool
def delete_shop(id: str = "", ids: List[str] = None) -> dict:
    """
    2.4 删除单位（单个/批量）。
    入参:
        id (str): 单个删除时的单位id
        ids (List[str]): 批量删除时的单位id列表
    说明：id 和 ids 二选一即可。
    """
    payload = {}
    if id: 
        payload["id"] = id
    elif ids: 
        payload["ids"] = ids
    return api_client.request("POST", "/api/basicData/shop/delShopById", payload)

@tool
def get_current_user_agencies() -> dict:
    """
    2.5 获取当前用户所属监管机构。
    无请求体。
    """
    return api_client.request("POST", "/api/basicData/agencies/management/findCurrentUserAgencies")


# ==========================================
# 3. earlyWarn 预警服务
# ==========================================

@tool
def get_early_warning_list(page_num: int = 1, page_size: int = 10, keyword: str = "") -> dict:
    """
    3.1 获取预警事件分页列表。
    入参:
        page_num (int): 页码，从 1 开始
        page_size (int): 每页条数
        keyword (str, optional): 匹配单位名/预警类型/子类型
    """
    payload = {"pageNum": page_num, "pageSize": page_size}
    if keyword:
        payload["keyword"] = keyword
    return api_client.request("POST", "/api/earlyWarn/earlyWarning/event/list", payload)

@tool
def apply_early_warning(warn_id: str, apply_type: int, reason: int = 0, detail: str = "", image: str = "") -> dict:
    """
    3.2 申诉或处理预警事件。
    入参:
        warn_id (str): 预警事件 id (必填)
        apply_type (int): 1=申诉（待反馈→已申诉）；2=处理（待反馈→已处理） (必填)
        reason (int): 原因 (可选)
        detail (str): 详情 (可选)
        image (str): 图片 base64 或 url (可选)
    说明: 仅「待反馈」事件可操作。
    """
    payload = {"warnId": warn_id, "type": apply_type}
    if reason: payload["reason"] = reason
    if detail: payload["detail"] = detail
    if image: payload["image"] = image
    return api_client.request("POST", "/api/earlyWarn/earlyWarning/event/apply", payload)

@tool
def approve_early_warning(approve_recode_id: str, accept: bool, suggestion: str = "") -> dict:
    """
    3.3 审核预警事件（申诉）。
    入参:
        approve_recode_id (str): mock 中即 warnId (必填)
        accept (bool): true=同意（已申诉→已处理）；false=拒绝（已申诉→已驳回） (必填)
        suggestion (str): 拒绝原因 (可选)
    说明: 仅「已申诉」事件可审核。
    """
    payload = {"approveRecodeId": approve_recode_id, "accept": accept}
    if suggestion:
        payload["suggestion"] = suggestion
    return api_client.request("POST", "/api/earlyWarn/earlyWarning/event/approve", payload)

@tool
def get_early_warning_status_count() -> dict:
    """
    3.4 获取预警事件各状态数量。
    无请求体。
    """
    return api_client.request("POST", "/api/earlyWarn/earlyWarning/event/statusCount")

@tool
def get_regulatory_domain_list() -> dict:
    """
    3.5 获取预警监管域类型字典。
    无请求体。
    """
    return api_client.request("POST", "/api/earlyWarn/earlyWarning/type/regulatoryDomainList")

@tool
def get_early_warning_sub_list() -> dict:
    """
    3.5 获取预警子类型字典。
    无请求体。
    """
    return api_client.request("POST", "/api/earlyWarn/earlyWarning/type/subList")

@tool
def get_early_warning_type_list() -> dict:
    """
    3.5 获取预警大类字典。
    无请求体。
    """
    return api_client.request("POST", "/api/earlyWarn/earlyWarning/type/warnList")


# ==========================================
# 4. facility 设备服务
# ==========================================

@tool
def get_camera_list(page_num: int = 1, page_size: int = 10, keyword: str = "") -> dict:
    """
    4.1 获取摄像头分页列表。
    入参:
        page_num (int): 页码，从 1 开始
        page_size (int): 每页条数
        keyword (str, optional): 匹配单位名/设备场景名
    """
    payload = {"pageNum": page_num, "pageSize": page_size}
    if keyword:
        payload["keyword"] = keyword
    return api_client.request("POST", "/api/facility/camera/management/page", payload)

@tool
def add_camera_device(equipment_number: str = "", equipment_label: str = "加工烹饪", business_id: str = "", shop_id: str = "", equipment_type: int = 1, camera_label_list: List[Dict[str, str]] = None) -> dict:
    """
    4.2 新增设备[运维]。
    入参:
        equipment_number (str): 设备编号
        equipment_label (str): 场景名
        business_id (str): 关联单位id
        shop_id (str): 关联单位id
        equipment_type (int): 默认1
        camera_label_list (List[Dict]): 形如 [{"cameraNumber": "xxx", "label": "yyy"}]
    """
    payload = {"equipmentLabel": equipment_label, "equipmentType": equipment_type}
    if equipment_number: payload["equipmentNumber"] = equipment_number
    if business_id: payload["businessId"] = business_id
    if shop_id: payload["shopId"] = shop_id
    if camera_label_list: payload["cameraLabelList"] = camera_label_list
    return api_client.request("POST", "/api/facility/camera/management/device/devops/add", payload)

@tool
def update_camera_device(id: str = "", equipment_number: str = "", equipment_label: str = "", status: int = -1, camera_label_list: List[Dict[str, str]] = None) -> dict:
    """
    4.3 修改设备[宇视]。
    入参:
        id (str): 设备 id (与 equipment_number 二选一)
        equipment_number (str): 设备编号 (与 id 二选一)
        equipment_label (str): 修改场景名
        status (int): 0/1 (同步live)，传-1表示不修改
        camera_label_list (List[Dict]): 形如 [{"cameraNumber": "xxx", "label": "yyy"}]
    """
    payload = {}
    if id: payload["id"] = id
    elif equipment_number: payload["equipmentNumber"] = equipment_number
    if equipment_label: payload["equipmentLabel"] = equipment_label
    if status != -1: payload["status"] = status
    if camera_label_list: payload["cameraLabelList"] = camera_label_list
    return api_client.request("POST", "/api/facility/yushi/camera/platform/stream/update", payload)

@tool
def delete_camera_device(id: str = "", camera_number: str = "", ids: List[str] = None) -> dict:
    """
    4.4 删除设备[宇视]（单个/批量）。
    入参:
        id (str): 按 id 删除
        camera_number (str): 按摄像头编号删除
        ids (List[str]): 批量删除
    说明: 三个参数选填其一即可。
    """
    payload = {}
    if id: payload["id"] = id
    elif camera_number: payload["cameraNumber"] = camera_number
    elif ids: payload["ids"] = ids
    return api_client.request("POST", "/api/facility/yushi/camera/platform/stream/delete", payload)


# ==========================================
# 额外：视觉分析辅助工具
# ==========================================

@tool
def analyze_video_vision(video_url: str, roi: dict, target_type: str) -> dict:
    """
    调用视觉算法模型对视频/图片进行识别（状态类视觉识别工具）。
    入参:
        video_url (str): 视频片段或图片地址
        roi (dict): 感兴趣区域配置
        target_type (str): 目标类型，如 "防鼠板", "清洁区门"
    """
    import random
    if target_type == "防鼠板":
        status = random.choice(["安装", "缺失", "未归位"])
        return {
            "status": status,
            "confidence": round(random.uniform(0.85, 0.99), 2),
            "alert": status != "安装"
        }
    elif target_type == "清洁区门":
        status = random.choice(["关闭", "短暂通行", "长时间敞开"])
        duration = random.randint(1, 600) if status != "关闭" else 0
        return {
            "status": status,
            "duration_seconds": duration,
            "confidence": round(random.uniform(0.85, 0.99), 2),
            "alert": duration > 60
        }
    return {"status": "未知", "confidence": 0.0, "alert": False}

# ==========================================
# 注册所有工具到 AGENT_TOOLS 列表
# ==========================================

AGENT_TOOLS = [
    # 1. cas
    get_current_user_menus,
    # 2. basicData
    get_shop_list,
    add_shop,
    update_shop,
    delete_shop,
    get_current_user_agencies,
    # 3. earlyWarn
    get_early_warning_list,
    apply_early_warning,
    approve_early_warning,
    get_early_warning_status_count,
    get_regulatory_domain_list,
    get_early_warning_sub_list,
    get_early_warning_type_list,
    # 4. facility
    get_camera_list,
    add_camera_device,
    update_camera_device,
    delete_camera_device,
    # 其他工具
    analyze_video_vision
]
