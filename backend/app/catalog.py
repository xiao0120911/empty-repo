"""声明式工具目录：同一份契约驱动模型工具、校验、前端展示及离线文档。"""
from dataclasses import dataclass
from .contracts import *

@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    category: str
    path: str
    input_model: type[BaseModel]
    response_model: type[BaseModel]
    risk: str
    description: str
    example: dict
    method: str = 'POST'
    available: bool = True

SPECS = [
 ToolSpec('get_graphic_code','获取验证码','认证','/api/cas/user/client/login/getGraphicCode',Empty,ApiResponse[GraphicCode],'auth','取得验证码key及PNG；免签名免token，仅用于登录页面，不暴露给大模型。',{}),
 ToolSpec('account_login','账号密码登录','认证','/api/cas/user/client/login/account-psw',Login,ApiResponse[UserData],'auth','获取平台访问令牌；令牌只在服务器加密保存，真实密码使用SHA256。',{'account':'账号','password':'服务端处理','loginChannel':4,'key':'验证码key','verifyCode':'1234'}),
 ToolSpec('permission_menus','当前菜单树','认证','/api/cas/user/client/permission/menus',Empty,ApiResponse[list[Menu]],'read','查询当前用户可见菜单；不是授权依据，本应用权限由本地账号角色控制。',{}),
 ToolSpec('shop_list','单位分页查询','基础数据','/api/basicData/shop/list',PageQuery,ApiResponse[Page[Shop]],'read','按单位名称模糊查询；仅返回指定页，不能将本页误认全部单位。',{'pageNum':1,'pageSize':10}),
 ToolSpec('shop_scan','单位自动分页查全','基础数据','/api/basicData/shop/list',ScanQuery,ApiResponse[ScanResult[Shop]],'read','本地组合只读工具：查询所有/查全单位及跨页统计时优先使用。自动翻页，保存total、实际保留条数、统计及逐页元数据/哈希；异常或预算排除页不保留明细；预算/重复/总数变化/失败返回incomplete，不谎称全量。精确单位ID用match参数（如{"match":{"id":"9000000000000000000"}}）本地精确比较完整字符串。模型摘录不等于平台响应截断。每页最多100条，累计8MB记录/90秒扫描循环预算，默认100页；非事务快照。',{'pageSize':100,'maxPages':100}),
 ToolSpec('shop_create','新增单位','基础数据','/api/basicData/shop/devops/settleByBuilder',ShopCreate,ApiResponse[CreatedShop],'write','新增单位，重名报400；经营地址映射shopAddress。白名单内直接执行，全程留痕。',{'shopName':'联调测试单位','businessType':2}),
 ToolSpec('shop_update','修改单位','基础数据','/api/basicData/shop/devops/update',ShopUpdate,ApiResponse[Updated],'write','按ID修改明确提供的非空字段，禁止无变更提交；白名单内直接执行，全程留痕。',{'id':'9000000000000000000','shopTel':'19900000000'}),
 ToolSpec('shop_delete','删除单位','基础数据','/api/basicData/shop/delShopById',ShopDelete,ApiResponse[Deleted],'write','单个/批量删除单位，破坏性操作；应列明ID和影响范围，白名单内直接执行，全程留痕。',{'id':'9000000000000000000'}),
 ToolSpec('current_agencies','当前监管机构','基础数据','/api/basicData/agencies/management/findCurrentUserAgencies',Empty,ApiResponse[Agency],'read','获取当前用户所属监管机构ID及名称，用于任务范围归属。',{}),
 ToolSpec('warning_list','预警事件分页查询','预警','/api/earlyWarn/earlyWarning/event/list',PageQuery,ApiResponse[Page[Warning]],'read','关键字仅匹配单位名/类型/子类型，不匹配事件ID。精确事件ID用warning_scan的match参数；本工具只返回指定页，不代表全量。',{'pageNum':1,'pageSize':10}),
 ToolSpec('warning_scan','预警自动分页查全','预警','/api/earlyWarn/earlyWarning/event/list',WarningScanQuery,ApiResponse[ScanResult[Warning]],'read','查询所有可见预警、跨页状态/类型统计及精确事件ID定位的组合只读工具。服务端有界翻页，保存total、全部纳入的有效页记录与汇总；异常排除页仅留元数据/哈希。精确事件ID用match参数（如{"match":{"id":"9300000000000001602"}}），查全后本地精确比较完整字符串，命中即返回，禁止把ID当keyword。支持可选status状态过滤（1待反馈/2已申诉/3已处理/4已驳回），查全后本地精确过滤，不能用keyword检索状态。模型只读摘录，完整性以coverage为准。失败/重复/漂移/预算不足返回incomplete；未查全不能宣称记录不存在；非快照。',{'match':{'id':'9300000000000001602'},'pageSize':100,'maxPages':100}),
 ToolSpec('warning_apply','申诉或处理预警','预警','/api/earlyWarn/earlyWarning/event/apply',WarningApply,ApiResponse[WarningChanged],'write','仅待反馈可申诉/处理；处理结论必须基于证据，白名单内直接执行，全程留痕。',{'warnId':'9300000000000002237','type':1,'detail':'提交人工复核材料'}),
 ToolSpec('warning_approve','审核预警申诉','预警','/api/earlyWarn/earlyWarning/event/approve',WarningApprove,ApiResponse[WarningChanged],'sensitive','仅已申诉可审核；同意/驳回均属审批决定，强制停止自动执行，独立审核员人工确认后才执行。mock记录ID等于warnId，正式环境必须用真实审核记录ID。',{'approveRecodeId':'9300000000000002237','accept':False,'suggestion':'待核实证据'}),
 ToolSpec('warning_status_count','预警状态统计','预警','/api/earlyWarn/earlyWarning/event/statusCount',Empty,ApiResponse[StatusCount],'read','汇总待反馈/申诉/处理/驳回/总数；可核对列表与报告，不等于处罚统计。',{}),
 ToolSpec('regulatory_domains','监管域字典','预警','/api/earlyWarn/earlyWarning/type/regulatoryDomainList',Empty,ApiResponse[list[Dictionary]],'read','查询经营监管/AI监管/设备预警的ID、代码与名称。',{}),
 ToolSpec('warning_subtypes','预警子类型字典','预警','/api/earlyWarn/earlyWarning/type/subList',Empty,ApiResponse[list[Dictionary]],'read','查询六个文档定义子类型及所属warnType；未包含挡鼠板和洁净区门识别。',{}),
 ToolSpec('warning_types','预警类型字典','预警','/api/earlyWarn/earlyWarning/type/warnList',Empty,ApiResponse[list[Dictionary]],'read','查询三类预警类型ID及名称。',{}),
 ToolSpec('camera_list','摄像头分页查询','设备','/api/facility/camera/management/page',PageQuery,ApiResponse[Page[Camera]],'read','按单位名/设备场景查询元数据和在线状态；keyword不匹配摄像头/设备编号，精确编号用camera_scan的match参数；不取得直播流或视频内容。',{'pageNum':1,'pageSize':10}),
 ToolSpec('camera_scan','设备自动分页查全','设备','/api/facility/camera/management/page',ScanQuery,ApiResponse[ScanResult[Camera]],'read','查询所有设备、指定场景全部设备及按编号精确定位的组合只读工具：按原列表关键字自动翻页，保留总数、实际记录和跨页状态/场景统计。精确摄像头/设备编号用match参数（如{"match":{"cameraNumber":"99990000001320000011"}}），编号可能存于cameraNumber或equipmentNumber，可同时传两键，任一字段完整字符串相等即命中，命中即返回，禁止把编号当keyword；未查全不能宣称记录不存在。不取得视频。有限预算且校验漂移，incomplete不得宣称查全。',{'match':{'cameraNumber':'99990000001320000011','equipmentNumber':'99990000001320000011'},'pageSize':100,'maxPages':100}),
 ToolSpec('camera_create','新增设备','设备','/api/facility/camera/management/device/devops/add',CameraCreate,ApiResponse[CreatedCamera],'write','新增设备及摄像头标签，可关联单位；设备编号重名失败；白名单内直接执行，全程留痕。',{'equipmentLabel':'加工烹饪','equipmentType':1}),
 ToolSpec('camera_update','修改宇视设备','设备','/api/facility/yushi/camera/platform/stream/update',CameraUpdate,ApiResponse[Updated],'write','按ID或设备编号修改场景、摄像头编号或在线状态；不设置ROI；白名单内直接执行，全程留痕。',{'id':'9100000000000000000','status':0}),
 ToolSpec('camera_delete','删除宇视设备','设备','/api/facility/yushi/camera/platform/stream/delete',CameraDelete,ApiResponse[Deleted],'write','按ID/摄像头编号/批量ID删除；破坏性操作，应列明ID和影响范围，白名单内直接执行，全程留痕。',{'id':'9100000000000000000'}),
 ToolSpec('video_channel_list','视频转发通道查询','视频转发','/api/video/channels',VideoChannelQuery,ApiResponse[Page[VideoChannel]],'read','查询上游脱敏视频转发通道。返回通道元数据，不返回视频内容；选定通道后可用“获取视频实时截帧”取得单张 JPEG 图片。',{'pageNum':1,'pageSize':20}),
 ToolSpec('video_snapshot','获取视频实时截帧','视频转发','/api/video/snapshot/{channelId}',VideoSnapshot,VideoSnapshotResponse,'read','从上游脱敏视频转发通道取得一张 JPEG 截帧，保存为当前账号私有图片并记录通道号、图片哈希和采集时间；随后可将返回的 imageId 用于图片视觉识别。',{'channelId':'2052272233530789889'}),
 ToolSpec('vision_rodent_board','防鼠板状态识别','视觉','local://vision/rodent-board',RodentBoardVision,VisionResponse,'read','分析已上传图片指定 ROI 内的防鼠板状态。仅输出安装、缺失、未归位或不确定；图片内容不会写入审计日志，但会记录哈希、ROI、模型版本和结果。',{'imageId':'已上传图片ID','roi':{'x':0.1,'y':0.6,'width':0.8,'height':0.3}}),
 ToolSpec('vision_clean_door','清洁区门状态识别','视觉','local://vision/clean-door',CleanDoorVision,VisionResponse,'read','分析已上传图片指定 ROI 内的清洁区门开闭状态。开启时长只根据同一摄像头的连续已识别图片时间戳计算，单张图片不推断时长。',{'imageId':'已上传图片ID','cameraId':'CAMERA-001','roi':{'x':0.1,'y':0.1,'width':0.8,'height':0.8},'openThresholdSeconds':60}),
 ToolSpec('monitor_start','开启监控（未开放）','视频','/api/facility/monitor-center/start',UnavailableVideo,ApiResponse[Any],'unavailable','文档明确HTTP501，未给入参；本工具明确返回不可用，不猜测、不发网络请求。',{},available=False),
 ToolSpec('monitor_stop','停止监控（未开放）','视频','/api/facility/monitor-center/stop',UnavailableVideo,ApiResponse[Any],'unavailable','文档/stop为monitor-center下相对路径，HTTP501，未给入参。',{},available=False),
 ToolSpec('live_gbs_get','直播/回放GET族（未开放）','视频','/api/facility/liveGbs/management/*',UnavailableVideo,ApiResponse[Any],'unavailable','覆盖文档GET通配族，具体路由和参数未提供，明确501，禁止任意URL请求。',{},method='GET',available=False),
 ToolSpec('live_gbs_post','直播/回放POST族（未开放）','视频','/api/facility/liveGbs/management/*',UnavailableVideo,ApiResponse[Any],'unavailable','覆盖文档POST通配族，具体路由和参数未提供，明确501。',{},available=False),
 ToolSpec('unknown_api','未实现路径诊断','诊断','/api/*',UnknownPath,ApiResponse[Any],'unavailable','兜底返回404；仅本地诊断，禁止通过任意路径绕过工具白名单。',{'path':'/api/not-implemented'},available=False),
]
TOOLS = {s.name:s for s in SPECS}

def catalog_json():
    return [dict(name=s.name,title=s.title,category=s.category,path=s.path,method=s.method,risk=s.risk,
                 description=s.description,example=s.example,available=s.available,
                 input_schema=s.input_model.model_json_schema(),response_schema=s.response_model.model_json_schema()) for s in SPECS]
