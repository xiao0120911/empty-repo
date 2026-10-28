"""食安平台 v1.1 全接口请求/响应契约。大整数 ID 始终以字符串交给前端。
请求拒绝额外字段，防止工具注入；响应允许真实平台增加字段，但校验已知契约。
"""
from typing import Annotated, Generic, TypeVar, Literal, Any, List
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator

T = TypeVar('T')
Identifier = str | int

class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class Empty(Input):
    """无请求体；客户端发送零字节而非 {}。"""

class Login(Input):
    account: str = Field(min_length=1, max_length=128, description='平台账号；不可交给模型')
    password: str = Field(min_length=1, max_length=256, description='mock 任意密码；真实平台 SHA256(明文)')
    loginChannel: Literal[4] = Field(4, description='固定渠道 4')
    key: str = Field(min_length=1, description='验证码 key')
    verifyCode: str = Field(min_length=4, max_length=4, description='四位验证码')

class PageQuery(Input):
    pageNum: int = Field(ge=1, description='页码，从 1 开始')
    pageSize: int = Field(ge=1, le=100, description='每页 1~100 条')
    keyword: str | None = Field(None, max_length=200, description='名称/类型模糊检索；具体范围见工具作用')

MatchKey=Annotated[str,Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,63}$',description='记录字段名，如id/cameraNumber/equipmentNumber')]
MatchValue=Annotated[str,Field(min_length=1,max_length=200,description='完整值字符串；ID必须十进制字符串，禁止浮点数/科学计数法')]

class ScanQuery(Input):
    keyword: str | None = Field(None,max_length=200,description='沿用列表文档的名称/类型检索；上游keyword不匹配ID/编号，精确ID/编号必须用match参数')
    match: dict[MatchKey,MatchValue] | None = Field(None,min_length=1,max_length=4,description='可选本地精确匹配{"字段名":"完整值"}：上游列表没有ID/编号检索，服务端逐页查全后本地比较完整字符串，任一字段相等即命中并提前结束；未覆盖可见范围前不能宣称记录不存在。精确ID/编号查询必须用本参数，禁止把ID/编号当keyword。示例：{"id":"9300000000000001602"}；编号可能存于cameraNumber或equipmentNumber，可同时传两键：{"cameraNumber":"9999...","equipmentNumber":"9999..."}')
    pageSize: int = Field(100,ge=1,le=100,description='上游每页条数，自动从第1页连续读取')
    maxPages: int = Field(100,ge=1,le=200,description='最多读取页数；异常/预算不足明确incomplete')

class WarningScanQuery(ScanQuery):
    status: Literal[1,2,3,4] | None = Field(None, description='可选状态过滤：1待反馈/2已申诉/3已处理/4已驳回；上游列表不支持状态检索，由服务端查全后本地精确过滤，不新增上游路由')

class ShopCreate(Input):
    shopName: str = Field(min_length=1, max_length=200, description='单位名称，不得重名')
    businessType: int | None = Field(None, ge=1, le=5, description='1学校/2社会餐饮默认/3养老/4单位/5中央厨房')
    shopTel: str | None = Field(None, description='联系电话')
    legalPersonName: str | None = Field(None, description='法人姓名')
    businessAddress: str | None = Field(None, description='经营地址，写入 shopAddress')
    registeredAddress: str | None = Field(None, description='注册地址')
    creditScore: int | None = Field(None, description='信用分，上游缺省80')

class ShopUpdate(Input):
    id: Identifier = Field(description='单位ID，推荐字符串以免19位ID精度损失')
    shopName: str | None = Field(None, min_length=1, description='新名称')
    shopTel: str | None = Field(None, description='新电话')
    legalPersonName: str | None = Field(None, description='新法人')
    registeredAddress: str | None = Field(None, description='新注册地址')
    creditScore: int | None = Field(None, description='新信用分')
    businessAddress: str | None = Field(None, description='新经营地址')
    shopAddress: str | None = Field(None, description='新单位地址')
    businessType: int | None = Field(None, ge=1, le=5, description='同步更新 shopTypeName')
    @model_validator(mode='after')
    def changed(self):
        if not any(k != 'id' and getattr(self,k) is not None for k in self.model_fields_set):
            raise ValueError('必须至少提供一个非空可更新字段')
        return self

class ShopDelete(Input):
    id: Identifier | None = Field(None, description='单个单位ID，与ids二选一')
    ids: list[Identifier] | None = Field(None, min_length=1, max_length=100, description='批量单位ID，与id二选一')
    @model_validator(mode='after')
    def selector(self):
        if (self.id is not None) + (self.ids is not None) != 1:
            raise ValueError('id/ids必须且只能提供一个')
        return self

class WarningApply(Input):
    warnId: Identifier = Field(description='待反馈预警事件ID')
    type: Literal[1,2] = Field(description='1申诉(待反馈→已申诉)，2处理(待反馈→已处理)')
    reason: int | None = Field(None, description='原因代码')
    detail: str | None = Field(None, max_length=4000, description='申诉/处理详情，不得捏造现场整改')
    image: str | None = Field(None, description='图片字段，上游文档未定义编码方式')

class WarningApprove(Input):
    approveRecodeId: Identifier = Field(description='审核记录ID；mock等于warnId，真实平台不能混用')
    accept: bool = Field(description='true同意(已申诉→已处理)，false拒绝(已申诉→已驳回)')
    suggestion: str | None = Field(None, max_length=4000, description='审批意见/拒绝原因')

class CameraLabel(Input):
    cameraNumber: str | None = Field(None, description='摄像头编号')
    label: str | None = Field(None, description='场景名称')

class CameraCreate(Input):
    equipmentNumber: str | None = Field(None, description='设备编号；不传则自动生成，禁止重名')
    equipmentLabel: str | None = Field(None, description='场景名称，缺省加工烹饪')
    businessId: Identifier | None = Field(None, description='关联单位ID')
    shopId: Identifier | None = Field(None, description='关联单位ID，同businessId，不同时传')
    equipmentType: int | None = Field(None, description='设备类型，缺省1')
    cameraLabelList: list[CameraLabel] | None = Field(None, description='摄像头编号及场景')
    @model_validator(mode='after')
    def selectors(self):
        if self.businessId is not None and self.shopId is not None:
            raise ValueError('businessId/shopId不同时传')
        return self

class CameraUpdate(Input):
    id: Identifier | None = Field(None, description='设备ID，与equipmentNumber二选一')
    equipmentNumber: str | None = Field(None, description='设备编号定位')
    equipmentLabel: str | None = Field(None, description='修改场景名称')
    cameraLabelList: list[CameraLabel] | None = Field(None, min_length=1, description='修改场景/摄像头编号')
    status: Literal[0,1] | None = Field(None, description='0离线/1在线，同步live')
    @model_validator(mode='after')
    def changed(self):
        if (self.id is not None)+(self.equipmentNumber is not None)!=1:
            raise ValueError('id/equipmentNumber二选一')
        if self.equipmentLabel is None and self.cameraLabelList is None and self.status is None:
            raise ValueError('必须提供更新字段')
        return self

class CameraDelete(Input):
    id: Identifier | None = Field(None, description='设备ID，三个定位方式选一')
    cameraNumber: str | None = Field(None, description='摄像头编号')
    ids: list[Identifier] | None = Field(None, min_length=1, max_length=100, description='批量设备ID')
    @model_validator(mode='after')
    def selector(self):
        if sum(x is not None for x in (self.id,self.cameraNumber,self.ids))!=1:
            raise ValueError('id/cameraNumber/ids三选一')
        return self

class UnavailableVideo(Input):
    """直播接口无入参契约，不猜字段、不发送请求。"""

class VideoChannelQuery(PageQuery):
    """脱敏视频转发通道的分页查询。"""
    scene: str | None = Field(None, min_length=1, max_length=64, description='可选场景筛选')
    orgName: str | None = Field(None, min_length=1, max_length=200, description='可选机构名称筛选')
    status: int | None = Field(None, ge=0, le=9, description='可选通道状态筛选')
    hasVideo: bool | None = Field(None, description='仅返回具备视频流的通道')

class VideoSnapshot(Input):
    channelId: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$', description='视频转发通道标识')

class UnknownPath(Input):
    path: str = Field(pattern=r'^/api/', max_length=300, description='仅诊断未实现路径，不向上游转发')

class Roi(Input):
    """归一化矩形 ROI；(0,0) 是图片左上角，尺寸相对图片宽高。"""
    x: float = Field(ge=0, lt=1, description='左上角 X，范围 [0,1)')
    y: float = Field(ge=0, lt=1, description='左上角 Y，范围 [0,1)')
    width: float = Field(gt=0, le=1, description='宽度比例，范围 (0,1]')
    height: float = Field(gt=0, le=1, description='高度比例，范围 (0,1]')

    @model_validator(mode='after')
    def within_image(self):
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError('ROI 必须位于图片范围内')
        return self

class RodentBoardVision(Input):
    imageId: str = Field(pattern=r'^[a-f0-9]{32}$', description='已上传图片 ID')
    roi: Roi = Field(description='防鼠板所在区域')

class CleanDoorVision(Input):
    imageId: str = Field(pattern=r'^[a-f0-9]{32}$', description='已上传图片 ID')
    cameraId: str = Field(min_length=1, max_length=128, description='摄像头标识，用于跨图片计算开启时长')
    roi: Roi = Field(description='清洁区门所在区域')
    openThresholdSeconds: int = Field(60, ge=1, le=3600, description='超过此秒数的开启状态触发告警')

class VideoMonitorCreate(Input):
    """Persistent periodic snapshot inspection configuration."""
    channelId: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    scene: Literal['rodent_board','clean_door']
    roi: Roi
    intervalSeconds: int = Field(60, ge=30, le=3600)
    openThresholdSeconds: int = Field(300, ge=30, le=86400)
    minConfidence: float = Field(0.80, ge=0.5, le=1)

class VideoMonitorUpdate(Input):
    enabled: bool | None = None
    intervalSeconds: int | None = Field(None, ge=30, le=3600)
    openThresholdSeconds: int | None = Field(None, ge=30, le=86400)
    minConfidence: float | None = Field(None, ge=0.5, le=1)
    roi: Roi | None = None

    @model_validator(mode='after')
    def changed(self):
        if not self.model_fields_set:
            raise ValueError('至少提供一个需要更新的监测字段')
        return self

class AlertAcknowledge(Input):
    note: str = Field(min_length=2, max_length=1000)

class Output(BaseModel):
    model_config = ConfigDict(extra='allow')
    @field_validator('*', mode='before', check_fields=False)
    @classmethod
    def string_ids(cls, value, info):
        if isinstance(value,int) and (info.field_name=='id' or info.field_name.endswith('Id')):
            return str(value)
        return value

class ApiResponse(Output, Generic[T]):
    code: int = Field(description='0成功；400参数/业务；401鉴权；404未实现；501视频未开放')
    msg: str = Field(description='结果说明')
    data: T | None = Field(description='成功数据；失败null')

class GraphicCode(Output):
    key: str = Field(description='验证码key')
    image: str = Field(description='base64 PNG，前端添加data:image/png;base64,')
    # 下划线不能作为Pydantic字段名称，使用alias保持原始响应。
    mockCode: str | None = Field(None, alias='_mockCode', description='仅mock提供验证码答案，正式环境不展示')
class UserData(Output):
    userId: str
    username: str
    nickname: str
    accessToken: str = Field(description='仅服务端加密保存，后续Authorization原样传递')
    refreshToken: str | None = None
    application: str | None = None
    isAdministrator: bool = False
class Menu(Output):
    id: Identifier
    name: str
    path: str | None = None
    type: Identifier | None = None
    code: str | None = None
    children: list['Menu'] = Field(default_factory=list)
class Shop(Output):
    id: str
    shopName: str
    shopType: int | None = None
    shopTypeName: str | None = None
    shopAddress: str | None = None
    legalPersonName: str | None = None
    shopTel: str | None = None
    foodSafetyQrCode: str | None = None
    creditScore: int | None = None
    businessTypeDesc: str | None = None
    businessSubTypeDesc: str | None = None
    registeredAddress: str | None = None
class Page(Output, Generic[T]):
    total: int = Field(ge=0, description='符合条件的总条数，非当前页数')
    list: List[T] = Field(description='本页记录')
class ScanResult(Output,Generic[T]):
    total: int | None = Field(description='平台返回的总数；首请求失败为null而非0')
    list: List[T] = Field(description='服务端已实际读取并保留的记录，不是模型摘录')
    outcome: Literal['complete','incomplete']
    pagesScanned: int = Field(ge=0)
    recordsScanned: int = Field(ge=0)
    complete: bool
    reason: str
    scope: str
    coverage: dict
    statistics: dict

class CreatedShop(Output):
    id: str
    shopName: str
class Updated(Output):
    id: str
    updated: list[str] = Field(description='实际修改的字段名称')
class Deleted(Output):
    deleted: int = Field(ge=0, description='实际删除数量')
class Agency(Output):
    id: str
    name: str
class Warning(Output):
    id: str
    organizationId: str
    organizationName: str
    warnType: int
    warnTypeMsg: str
    subType: int
    subTypeMsg: str
    agenciesId: str
    agenciesName: str
    updateTime: str
    status: int = Field(description='1待反馈/2已申诉/3已处理/4已驳回')
    statusName: str
class WarningChanged(Output):
    warnId: str
    status: int
    statusName: str
class StatusCount(Output):
    pendingCount: int
    appealedCount: int
    processedCount: int
    rejectCount: int
    totalCount: int
    isPush: int | None = None
class Dictionary(Output):
    id: Identifier
    name: str
    code: str | None = None
    warnType: int | None = None
class Camera(Output):
    id: str
    deviceId: str
    shopName: str
    shopId: str
    equipmentNumber: str
    cameraNumber: str
    equipmentType: int
    status: int
    live: int
    equipmentName: str
    orgName: str
    platformType: str
class VideoChannel(Output):
    channelId: str
    name: str
    scene: str | None = None
    orgName: str | None = None
    status: int | None = None
    hasVideo: bool = False
class CreatedCamera(Output):
    id: str
    equipmentNumber: str
    cameraNumber: str

class VisionObservation(Output):
    observationId: str | None = None
    imageId: str
    imageHash: str
    scene: str
    status: str
    confidence: float = Field(ge=0, le=1)
    alert: bool
    reason: str
    cameraId: str | None = None
    capturedAt: str
    durationSeconds: int | None = None
    openSince: str | None = None
    model: str

class VisionResponse(Output):
    code: int
    msg: str
    data: VisionObservation

class VideoSnapshotResult(Output):
    imageId: str
    imageHash: str
    channelId: str
    mimeType: str
    byteSize: int
    capturedAt: str

class VideoSnapshotResponse(Output):
    code: int
    msg: str
    data: VideoSnapshotResult
