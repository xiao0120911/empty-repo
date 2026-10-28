"""跨API、执行与展示共用的窄意图规则；不能把新条件查询当作历史排版。
只保留正确性关键的窄意图：精确预警ID、精确摄像头/设备编号（强制match、禁止把ID/编号当keyword）、
已有结果排版复用。其余各种查询表述一律交给模型与工具描述处理，不用固定正则路由；
模型空答时由fallback_read_call做只读兜底推断，绝不推断写入。
"""
import re

FORMAT_ONLY=re.compile(r'^(?:请|请帮我|帮我)?(?:将|把)?(?:上面|上述|刚才|这些|它们|结果|查询结果|视频信息|设备信息)?(?:用|以|按|改成|改为)?(?:列表|清单)(?:形式|的形式)?(?:列出|列出来|展示|显示|呈现|输出)?[。.!！?？]*$')
EXACT_WARNING=re.compile(r'^(?:请|请帮我|帮我)?(?:查询|查找|查一下|找出|查看)(?:一下)?(?:事件|预警事件|预警)?(?:ID|id|编号)?(?:是|为|[:：])?\s*([0-9]{10,30})(?:的)?(?:预警|预警事件|事件|信息|详情)?[。.!！?？]*$')
SCENES='加工烹饪|售卖区域|生产过程控制|库房|洗消间|就餐区'
# 编号/ID精确查询：编号走scan的match本地精确匹配，禁止当keyword。
CAMERA_NUMBER=re.compile(r'^(?:请|请帮我|帮我)?(?:重新|再次)?(?:(?:查询|查找|查一下|找出|查看|获取)(?:一下)?)?(摄像头|设备)(?:编号|号)?(?:是|为|[:：])?\s*([A-Za-z0-9_-]{4,40})(?:的)?(?:摄像头|设备)?(?:详细信息|详情|信息|元数据)?[。.!！?？]*$')
STATUS_WORDS={'待反馈':1,'已申诉':2,'已处理':3,'已驳回':4}
STATUS_WORD_PATTERN=re.compile(r'待反馈|已申诉|已处理|已驳回')
WRITE_HINT=re.compile(r'新增|删除|修改|更新|审核|审批|处理|申诉|处罚')
# 兜底推断使用的连接词/多子句信号：复合条件不猜测，交回模型纠偏路径。
COMPOUND_HINT=re.compile(r'[，。；,;]|然后|同时|并且|接着|另外|以及')
CONJUNCTION_HINT=re.compile(r'和|与|及|并|或')
WARNING_OBJECT=re.compile(r'预警事件|预警|事件')
WARNING_KEYWORD=re.compile(r'([\u4e00-\u9fa5A-Za-z0-9]{2,30}?)(?:的)?(?:预警事件|预警|事件)(?:信息|列表|数据)?[。.!！?？]*$')
LEADING_NOISE=re.compile(r'^(?:请帮我|请|帮我|麻烦|查询|查找|查一下|找出|查看|获取|列出|看一下|看看|一下|所有|全部|全量|最新)+')

def is_format_only(prompt):
    return bool(FORMAT_ONLY.fullmatch(prompt.strip()))

def exact_warning_id(prompt):
    match=EXACT_WARNING.fullmatch(prompt.strip())
    return match.group(1) if match else None

def camera_number_query(prompt):
    """按摄像头/设备编号精确查询的窄意图；复合条件/写入不匹配，交由模型与安全网关处理。
    编号可能存于cameraNumber或equipmentNumber任一字段，两键同传，任一相等即命中。"""
    match=CAMERA_NUMBER.fullmatch(prompt.strip())
    if not match:return None
    number=match.group(2)
    return ('camera_scan',{'match':{'cameraNumber':number,'equipmentNumber':number}})

def write_like(prompt):
    """提示词包含写入/处理类动作；「已处理/已申诉」等状态词不算写入动作。"""
    return bool(WRITE_HINT.search(STATUS_WORD_PATTERN.sub('',prompt)))

def _warning_keyword_of(prompt):
    """从“…的预警信息”类短语提取关键词；状态词/写入词/噪声词不作keyword。"""
    match=WARNING_KEYWORD.search(prompt.strip())
    if not match:return None
    keyword=LEADING_NOISE.sub('',match.group(1)).strip()
    if not keyword or STATUS_WORD_PATTERN.search(keyword) or WRITE_HINT.search(keyword):return None
    return keyword

def fallback_read_call(prompt,evidence=None):
    """模型空答后的只读兜底推断：仅猜工具与粗参数，写入、复合条件与精确ID/编号一律不推断。
    推断结果仍走安全网关与证据约束，不会绕过审批或冒充证据。"""
    core=re.sub(r'[。.!！?？]+$','',prompt.strip())
    if write_like(core):return None
    if COMPOUND_HINT.search(core) or CONJUNCTION_HINT.search(core):return None
    status=STATUS_WORD_PATTERN.search(core)
    if status:return ('warning_scan',{'status':STATUS_WORDS[status.group(0)]})
    if WARNING_OBJECT.search(core):
        keyword=_warning_keyword_of(core)
        return ('warning_scan',{'keyword':keyword}) if keyword else ('warning_scan',{})
    scenes=set(re.findall(SCENES,core))
    if len(scenes)>1:return None
    scene=next(iter(scenes)) if scenes else None
    has_camera_hint=bool(re.search(r'摄像头|设备|视频|场景',core))
    has_camera_evidence=any(row.get('tool') in ('camera_list','camera_scan') for row in (evidence or []))
    if scene and (has_camera_hint or has_camera_evidence):
        return ('camera_scan',{'keyword':scene})
    if re.search(r'摄像头|设备|视频',core):return ('camera_scan',{})
    if re.search(r'单位|门店|店铺',core):return ('shop_scan',{})
    return None

def expected_read_tools(prompt):
    """可识别数据对象的最低证据约束；不把复合写入误判为纯查询。"""
    if WRITE_HINT.search(STATUS_WORD_PATTERN.sub('',prompt)):return []
    if not re.search(r'查|找|获取|列出|列表|统计|核对',prompt):return []
    groups=[]
    if re.search(r'摄像头|设备|视频',prompt):groups.append({'camera_list','camera_scan'})
    if re.search(r'预警|事件',prompt):groups.append({'warning_list','warning_scan','warning_status_count'})
    if re.search(r'单位(?:信息|列表|数据)|所有单位|全部单位|查全单位',prompt):groups.append({'shop_list','shop_scan'})
    return groups

def has_relevant_evidence(prompt,rows):
    names={row.get('tool') for row in rows}
    groups=expected_read_tools(prompt)
    valid=bool(rows) and all(names.intersection(group) for group in groups)
    scenes=set(re.findall(SCENES,prompt))
    if {'camera_list','camera_scan'} in groups and len(scenes)==1 and not re.search(r'排除|除外|不要|不包含|非',prompt):
        scene=next(iter(scenes))
        valid=valid and any(row.get('tool') in ('camera_list','camera_scan') and row.get('arguments',{}).get('keyword')==scene for row in rows)
    return valid
