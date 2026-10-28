"""业务成果文本：确定性归纳已验证证据；原始响应仍留在审计和导出中。
不新增模型调用，不把原始JSON作为结果，也不以格式转换推断新事实。
"""
import json
import re
from .store import redact
from .intents import is_format_only,has_relevant_evidence
from .pagination import SCAN_TO_LIST,PAGE_TOOLS,record_stats,page_identity

STATUS={1:'待反馈',2:'已申诉',3:'已处理',4:'已驳回'}
TITLES={'shop_list':'单位查询','camera_list':'设备查询','warning_list':'预警查询','warning_status_count':'预警状态统计','current_agencies':'监管机构','permission_menus':'菜单权限','regulatory_domains':'监管域','warning_types':'预警类型','warning_subtypes':'预警子类型','shop_create':'新增单位','shop_update':'修改单位','shop_delete':'删除单位','camera_create':'新增设备','camera_update':'修改设备','camera_delete':'删除设备','warning_apply':'预警反馈','warning_approve':'预警申诉审核','video_channel_list':'视频转发通道查询','video_snapshot':'视频实时截帧','vision_rodent_board':'防鼠板状态识别','vision_clean_door':'清洁区门状态识别'}
MATCH_LABELS={'id':'事件ID','warnId':'事件ID','cameraNumber':'摄像头编号','equipmentNumber':'设备编号','shopName':'单位名称','shopId':'单位ID'}

def text(value):
    """仅用于摘要内标量；换行/Markdown标记不能改变生成报告结构。"""
    if value is None:return '未提供'
    if isinstance(value,(dict,list)):return '详见审计证据'
    if isinstance(value,bool):return '是' if value else '否'
    return re.sub(r'[\r\n\t`*#|]', ' ',str(value))[:500]

def record_line(tool,record):
    tool=SCAN_TO_LIST.get(tool,tool)
    if not isinstance(record,dict):return text(record)
    if tool=='warning_list':
        return '；'.join([f"事件ID：{text(record.get('id'))}",f"单位：{text(record.get('organizationName'))}",f"类型：{text(record.get('warnTypeMsg'))}",f"子类型：{text(record.get('subTypeMsg'))}",f"状态：{text(record.get('statusName') or STATUS.get(record.get('status')))}",f"更新时间：{text(record.get('updateTime'))}"])
    if tool=='camera_list':
        return '；'.join([f"单位：{text(record.get('shopName'))}",f"场景：{text(record.get('equipmentName'))}",f"摄像头编号：{text(record.get('cameraNumber'))}",f"设备编号：{text(record.get('equipmentNumber'))}",f"设备ID：{text(record.get('id'))}",f"状态：{ {0:'离线',1:'在线'}.get(record.get('status'),'未知')}"])
    if tool=='shop_list':
        return '；'.join([f"单位：{text(record.get('shopName'))}",f"单位ID：{text(record.get('id'))}",f"类型：{text(record.get('shopTypeName'))}",f"地址：{text(record.get('shopAddress'))}",f"信用分：{text(record.get('creditScore'))}"])
    fields=[('name','名称'),('id','标识'),('code','类别代码')]
    return '；'.join(f'{label}：{text(record[key])}' for key,label in fields if record.get(key) is not None) or '已获取一条业务记录，详细字段保存在审计证据中。'

def statistics_lines(stats):
    lines=[]
    labels={'shopTypeName':'单位类型','equipmentName':'设备场景','status':'在线状态','warnTypeMsg':'预警类型','statusName':'预警状态'}
    for field,group in stats.get('groups',{}).items():
        entries=list(group.get('top',{}).items()) if 'top' in group else [(item['label'],item['count']) for item in group.get('topEntries',[])]
        items=[]
        for label,count in entries:
            if field=='status':label={'0':'离线','1':'在线'}.get(str(label),label)
            items.append(f'{text(label)} {count} 条')
        if group.get('otherRecords'):items.append(f"其他类别 {group['otherRecords']} 条")
        if items:lines.append(labels.get(field,'分类')+'汇总：'+'；'.join(items)+'。')
    score=stats.get('creditScore')
    if score and score.get('knownCount'):
        lines.append(f"信用分统计：有效 {score['knownCount']} 条，均值 {score['average']}，最低 {score['minimum']}，最高 {score['maximum']}；缺失 {score['missingCount']} 条。")
    return lines

def section(row):
    tool=row.get('tool','')
    title=TITLES.get(tool,'业务结果')
    lines=[f'## {title}']
    if row.get('code')!=0:
        lines.append('人工已拒绝此操作，未执行对应工具。' if row.get('human_rejected') is True else '此步骤未成功完成：'+text(row.get('msg') or '请查看脱敏审计记录'))
        return lines
    data=row.get('data')
    args=row.get('arguments') or {}
    if tool in SCAN_TO_LIST and isinstance(data,dict):
        noun={'shop_scan':'单位','camera_scan':'设备','warning_scan':'预警'}[tool]
        coverage=data.get('coverage') if isinstance(data.get('coverage'),dict) else {}
        status_filter=coverage.get('filter') or {}
        match_filter=args.get('match') or status_filter.get('match')
        if match_filter:
            # 通用精确匹配：命中/未命中/未完成三态，结论只由本次扫描证据决定。
            pairs=[f'{MATCH_LABELS.get(key,key)}为 {text(value)}' for key,value in match_filter.items()]
            target='；'.join(pairs)
            lines=[f'## {noun}精确查询']
            records=data.get('list',[])
            if records:
                lines.append(f'已找到{target}的记录。')
                lines.extend(f'{i}. '+record_line(tool,record) for i,record in enumerate(records[:100],1))
            elif data.get('complete') is True:
                lines.append(f'本次可见范围扫描未匹配{target}；这不证明其他权限范围或数据变化后不存在。')
            else:
                lines.append(f'{target}的查询尚未完成，不能据此判断记录不存在。')
                lines.append('原因：'+text(data.get('reason')))
            lines.append(f"已扫描 {text(data.get('pagesScanned'))} 页、{text(coverage.get('recordsRead'))} 条不同记录；平台返回总数 {text(data.get('total'))} 条。")
            lines.append('匹配方式：服务端有界查全后本地精确比较完整字符串，未新增上游路由；范围：当前平台账号可见数据，分页不是事务快照。')
            if tool=='camera_scan':lines.append('仅设备元数据及在线状态，不是可播放视频，未执行视频识别。')
            return lines
        lines=[f'## {noun}分页汇总']
        keyword=args.get('keyword')
        if keyword:lines.append('检索条件：'+text(keyword)+'。')
        filter_status=args.get('status') if args.get('status') is not None else status_filter.get('status')
        if filter_status is not None:
            lines.append(f'过滤条件：状态为{STATUS.get(filter_status,str(filter_status))}；由服务端查全后本地精确过滤，未新增上游路由。')
        if data.get('complete') is True:
            if status_filter:
                lines.append(f"已完成本次可见范围扫描：读取全部 {text(coverage.get('recordsRead'))} 条，与平台返回总数 {text(data.get('total'))} 条一致，共 {text(data.get('pagesScanned'))} 页；其中符合状态过滤条件的有 {text(data.get('recordsScanned'))} 条。")
            else:
                lines.append(f"已完成本次可见范围扫描：读取 {text(data.get('recordsScanned'))} 条，与平台返回总数 {text(data.get('total'))} 条一致，共 {text(data.get('pagesScanned'))} 页。")
        else:
            if status_filter:
                lines.append(f"本次扫描尚未完成：已读取 {text(coverage.get('recordsRead'))} 条，平台总数 {text(data.get('total'))}，已请求 {text(data.get('pagesScanned'))} 个有效响应页，匹配并保留 {text(data.get('recordsScanned'))} 条；不能宣称查全。")
            else:
                lines.append(f"本次扫描尚未完成：已保留 {text(data.get('recordsScanned'))} 条，平台总数 {text(data.get('total'))}，已请求 {text(data.get('pagesScanned'))} 个有效响应页；不能宣称查全。")
            lines.append('原因：'+text(data.get('reason')))
        lines.extend(statistics_lines(data.get('statistics') or record_stats(tool,data.get('list',[]))))
        lines.append('统计依据是服务端实际保留记录，不是模型摘录；分页非事务快照，不代表其他账号权限范围。')
        records=data.get('list',[])
        lines.append(f'关键记录展示 {min(len(records),100)} 条；已读取记录均可在审计导出中核对。')
        lines.extend(f'{i}. '+record_line(tool,record) for i,record in enumerate(records[:100],1))
        if data.get('contextTruncated') or data.get('modelView',{}).get('omittedRecords'):lines.append('本次为历史证据窗口的关键记录，窗口未包含全部明细，统计/扫描范围仍保留。')
        if tool=='camera_scan':lines.append('仅设备元数据及在线状态，不是可播放视频，未执行视频识别。')
    elif isinstance(data,dict) and isinstance(data.get('list'),list):
        records=data['list']
        keyword=args.get('keyword')
        scope=f'检索条件：{text(keyword)}。' if keyword else ''
        source_count=data.get('modelView',{}).get('sourceRecords',len(records))
        lines.append(scope+f"匹配总数 {text(data.get('total'))} 条，本次第 {text(args.get('pageNum','未记录'))} 页实际读取 {source_count} 条；本页不代表全量。")
        stats=data.get('statistics') if row.get('source_run_id') else record_stats(tool,records)
        lines.extend(statistics_lines(stats or record_stats(tool,records)))
        identity_ok=data.get('coverage',{}).get('metadataConsistent',True) if row.get('source_run_id') else page_identity(args,data)[3]
        if not identity_ok:lines.append('响应页码/条数与请求不一致，不能推断页范围或查全。')
        if source_count and len(records)<source_count:lines.append(f'当前展示/历史窗口列出 {len(records)} 条关键记录，不影响已读取总数；完整页保存在原任务证据。')
        if not records:lines.append('当前窗口没有可列出的记录。' if source_count else '本次返回页面没有匹配记录。')
        for i,record in enumerate(records[:100],1):lines.append(f'{i}. '+record_line(tool,record))
        if len(records)>100 or data.get('contextTruncated'):lines.append('展示内容已截取；完整查询依据请查看审计证据。')
        if tool=='camera_list':lines.append('上述为设备元数据和在线状态，不是可播放视频；未执行视频识别。')
    elif tool=='warning_status_count' and isinstance(data,dict):
        lines.append('；'.join(f'{label} {text(data.get(key))} 条' for key,label in [('pendingCount','待反馈'),('appealedCount','已申诉'),('processedCount','已处理'),('rejectCount','已驳回'),('totalCount','合计')])+'。')
        lines.append('统计为平台返回的当前账号可见范围，不代表处罚数量。')
    elif tool=='current_agencies' and isinstance(data,dict):
        lines.append(f"当前监管机构：{text(data.get('name'))}；机构标识：{text(data.get('id'))}。")
    elif tool=='permission_menus' and isinstance(data,list):
        lines.append(f'返回 {len(data)} 个顶级菜单：'+'、'.join(text(item.get('name')) for item in data if isinstance(item,dict))+'。菜单树不替代实际授权校验。')
    elif tool in ('warning_apply','warning_approve') and isinstance(data,dict):
        lines.append(f"平台已返回预警 {text(data.get('warnId'))} 的更新结果：{text(data.get('statusName') or STATUS.get(data.get('status')))}。")
        lines.append('该状态来自平台响应，不证明现场整改完成，也不构成处罚决定。')
    elif tool in ('vision_rodent_board','vision_clean_door') and isinstance(data,dict):
        result=data.get('data') if isinstance(data.get('data'),dict) else data
        scene='防鼠板' if tool=='vision_rodent_board' else '清洁区门'
        status_labels={'installed':'已安装','missing':'缺失','misaligned':'未归位','closed':'关闭','open':'开启','uncertain':'不确定'}
        lines.append(f"{scene}识别结果：{status_labels.get(result.get('status'),text(result.get('status')))}；置信度 {text(result.get('confidence'))}。")
        lines.append('识别依据：'+text(result.get('reason'))+'。')
        if tool=='vision_clean_door':
            if result.get('durationSeconds') is not None:lines.append(f"同一摄像头连续开启时长：{text(result.get('durationSeconds'))} 秒。")
            if result.get('alert'):lines.append('已达到配置的门开启时长阈值，需要人工核验现场状态。')
        elif result.get('alert'):
            lines.append('识别结果提示异常，需要人工核验现场状态。')
        lines.append('审计记录保存图片哈希、ROI、模型版本和识别结论；不包含图片二进制内容。')
    elif tool=='video_snapshot' and isinstance(data,dict):
        lines.append(f"已从脱敏视频转发通道 {text(data.get('channelId'))} 获取 JPEG 截帧。")
        lines.append(f"图片标识：{text(data.get('imageId'))}；图片哈希：{text(data.get('imageHash'))}。")
        lines.append('可将该图片标识用于视觉识别；审计记录不包含截帧二进制内容。')
    elif tool.endswith('_delete') and isinstance(data,dict):
        lines.append(f"平台确认删除 {text(data.get('deleted'))} 条记录；操作经白名单直接执行并全程留痕。")
    elif tool.endswith('_update') and isinstance(data,dict):
        labels={'shopName':'单位名称','shopTel':'联系电话','legalPersonName':'法人姓名','registeredAddress':'注册地址','shopAddress':'经营地址','businessAddress':'经营地址','creditScore':'信用分','equipmentLabel':'设备场景','cameraLabelList':'摄像头标签','status':'在线状态','businessType':'单位类型'}
        changed=data.get('updated') or []
        lines.append(f"平台确认更新对象 {text(data.get('id'))}；变更项："+'、'.join(labels.get(key,'业务字段') for key in changed)+'。')
    elif tool.endswith('_create') and isinstance(data,dict):
        lines.append(f"平台确认创建对象 {text(data.get('id'))}。"+('单位名称：'+text(data['shopName'])+'。' if data.get('shopName') else ''))
        if data.get('cameraNumber'):lines.append('摄像头编号：'+text(data['cameraNumber'])+'；设备编号：'+text(data.get('equipmentNumber'))+'。')
    elif isinstance(data,list):
        lines.append(f'已获取 {len(data)} 条业务条目。')
        lines.extend(f'{i}. '+record_line(tool,item) for i,item in enumerate(data[:100],1))
    elif data is None:
        lines.append('平台响应未包含可展示的业务数据，请核对审计证据。')
    else:
        lines.append('已取得业务响应，详细字段保留在审计证据中；未对未知字段作推断。')
    return lines

def summarize_rows(prompt,rows,run_id=None,status=None,reused=False,source=None):
    rows=redact(rows)
    lines=['# 查询结果' if reused else '# 任务结果总结',f'任务：{text(prompt)}']
    success=sum(row.get('code')==0 for row in rows)
    failed=len(rows)-success
    if status=='needs_reconciliation':lines.append('写入结果尚待核对，不能宣称操作成功；禁止自动重试。')
    elif not rows:lines.append('尚无已确认的工具结果可用于总结，请查看任务状态。')
    else:lines.append(f'本次已记录 {len(rows)} 项业务结果，成功 {success} 项'+(f'，未完成或被拒绝 {failed} 项。' if failed else '。'))
    if reused:lines.append('以下内容复用已有查询结果，仅调整展示格式；未重新查询或执行写操作，状态以历史查询时为准。')
    for row in rows:lines.extend(['']+section(row))
    if run_id:lines.extend(['','## 依据与边界',f'任务依据：{text(run_id)}。'])
    if source:lines.append(f'历史查询依据：{text(source)}。')
    lines.append('以上仅辅助监管，不构成审批或处罚决定；单页或展示摘录不能用于判断查全，以已核验扫描范围为准。')
    return '\n'.join(lines)

def has_raw_payload(report):
    if not isinstance(report,str):return True
    try:
        json.loads(report.strip())
        return True
    except ValueError:
        pass
    return bool(re.search(r'```|^[\s]*[\[{]|[\{,]\s*"[^"\n]+"\s*:|^\s*"[^"\n]+"\s*:',report,re.M))

def no_current_evidence(prompt,run_id=None,reason=None):
    lines=['# 本次任务未取得新查询结果',f'任务：{text(prompt)}',
        '本次缺少可核验的当前任务工具证据，不能宣称已查询成功；未使用之前的查询结果替代本次结果。',
        '只有明确要求对已有结果改格式时才复用历史证据；请重新执行当前条件查询或使用接口工具。']
    if reason:lines.append('执行提示：'+text(reason))
    if run_id:lines.append('任务依据：'+text(run_id)+'。')
    return '\n'.join(lines)

def public_report(report,prompt,rows,run_id=None,status=None,reused=False,source=None):
    """仅明确排版可复用；新查询绝不把历史证据包装成当前成功结果。"""
    if reused and not is_format_only(prompt):return no_current_evidence(prompt,run_id,'历史结果不能满足本次新查询，旧成果仅保留供审计核对')
    # 带match参数的扫描行是精确查询（等同旧lookup语义），模型文本可透传；全量扫描行仍强制结构化范围报告。
    page_rows=[row for row in rows if row.get('tool') in PAGE_TOOLS and not (row.get('tool') in SCAN_TO_LIST and (row.get('arguments') or {}).get('match'))]
    # 分页最终范围由完整服务端证据决定，不能继承模型因摘录而产生的误判。
    if page_rows:return summarize_rows(prompt,rows,run_id,status,reused,source)
    if isinstance(report,str) and report.strip() and not has_raw_payload(report):
        return report
    if rows:return summarize_rows(prompt,rows,run_id,status,reused,source)
    # 无证据的JSON型模型输出不转换成事实，避免把幻觉包装成结论。
    return '# 结果提示\n原结果为结构化内容，但缺少可核验的业务证据，未直接展示。请查看审计详情或重新查询。'

def display_result(run):
    """旧任务按读取时生成展示摘要，绝不修改原记录/哈希链。"""
    result=run.get('result')
    if not isinstance(result,dict) or not result.get('report'):return run
    rows=result.get('steps') or []
    if not rows:
        rows=[{'tool':event['payload'].get('tool',''),'arguments':event['payload'].get('arguments',{}),**event['payload']['result']} for event in run.get('events',[]) if event['kind']=='tool.completed' and isinstance(event.get('payload',{}).get('result'),dict)]
    prompt=run.get('prompt','')
    stale=not is_format_only(prompt) and (result.get('context_reused') is True or (bool(rows) and all(row.get('source_run_id') and row.get('source_run_id')!=run.get('id') for row in rows)))
    if stale:
        # 只改变返回的展示层，不篡改旧任务报告、状态、证据或哈希链，不偷偷重跑。
        run['result']={**result,'display_report':no_current_evidence(prompt,run.get('id'),'此历史任务错误复用了旧查询，原报告仍在审计导出中；请重新提交当前查询'),
            'context_reused':False,'fresh_query_required':True,'current_evidence_count':0,'legacy_stale_result':True}
        return run
    if run.get('mode')=='agent' and not is_format_only(prompt) and result.get('fresh_query_required') is True and not has_relevant_evidence(prompt,rows):
        result['display_report']=no_current_evidence(prompt,run.get('id'),run.get('error'))
    else:
        result['display_report']=public_report(result['report'],prompt,rows,run.get('id'),run.get('status'),result.get('context_reused',False),result.get('source_run_id'))
    return run
