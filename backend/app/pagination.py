"""分页证据与模型观察分层：保留可核验记录，永不切开JSON字符串。
模型窗口可省略明细/逐页哈希，但total、实际读取数、完整性与统计范围独立保留。
"""
from collections import Counter
from copy import deepcopy
import time
from .store import redact,dumps,digest

SCAN_TO_LIST={'shop_scan':'shop_list','camera_scan':'camera_list','warning_scan':'warning_list'}
PAGE_TOOLS=set(SCAN_TO_LIST)|set(SCAN_TO_LIST.values())
FIELDS={
    'shop_list':('id','shopName','shopTypeName','businessSubTypeDesc','shopAddress','creditScore'),
    'camera_list':('id','shopId','shopName','equipmentNumber','cameraNumber','equipmentName','status','live'),
    'warning_list':('id','organizationId','organizationName','warnTypeMsg','subTypeMsg','status','statusName','updateTime'),
}

def page_identity(args,data):
    """文档请求决定页身份；可选响应页字段只能核对，不能静默改写请求。"""
    page=args.get('pageNum')
    size=args.get('pageSize')
    response={key:data[key] for key in ('pageNum','pageSize','returned') if key in data}
    consistent=all(type(response[key]) is int and response[key]==expected for key,expected in
        (('pageNum',page),('pageSize',size),('returned',len(data['list']))) if key in response)
    return page,size,response,consistent

def project_record(tool,record):
    base=SCAN_TO_LIST.get(tool,tool)
    keys=FIELDS.get(base,tuple(record))
    output={}
    shortened=[]
    for key in keys:
        if key not in record:continue
        value=record[key]
        if isinstance(value,str) and key!='id' and not key.endswith('Id') and len(value)>160:
            value=value[:160]+'…'
            shortened.append(key)
        elif isinstance(value,(dict,list)):
            continue
        output[key]=value
    if shortened:output['shortenedFields']=shortened
    return output

def record_stats(tool,records):
    base=SCAN_TO_LIST.get(tool,tool)
    grouping={'shop_list':('shopTypeName',),'camera_list':('equipmentName','status'),'warning_list':('warnTypeMsg','statusName')}.get(base,())
    groups={}
    for field in grouping:
        counter=Counter(str(row.get(field) if row.get(field) is not None else '未提供') for row in records)
        top=counter.most_common(20)
        groups[field]={'top':dict(top),'otherRecords':sum(counter.values())-sum(count for _,count in top),'distinctGroups':len(counter)}
    stats={'recordCount':len(records),'groups':groups}
    if base=='shop_list':
        scores=[row['creditScore'] for row in records if isinstance(row.get('creditScore'),(int,float)) and not isinstance(row.get('creditScore'),bool)]
        stats['creditScore']={'knownCount':len(scores),'missingCount':len(records)-len(scores),'minimum':min(scores) if scores else None,'maximum':max(scores) if scores else None,'average':round(sum(scores)/len(scores),2) if scores else None}
    return stats

def observation(tool,args,result,budget=24000):
    """有效JSON对象；只压缩阅读窗口，绝不修改加密操作证据。预算单位为字符。"""
    if budget<1800:raise ValueError('分页观察预算至少1800字符')
    data=result.get('data')
    if tool in PAGE_TOOLS and result.get('code')==0 and isinstance(data,dict) and isinstance(data.get('list'),list):
        records=data['list']
        scan=tool in SCAN_TO_LIST
        if scan:
            coverage=deepcopy(data.get('coverage') or {})
        else:
            page,size,response_meta,consistent=page_identity(args,data)
            total=data.get('total')
            valid=type(page) is int and type(size) is int and type(total) is int
            coverage={'kind':'page','pageNum':page,'pageSize':size,'total':total,'returned':len(records),
                'nextPage':page+1 if valid and consistent and page*size<total else None,
                'complete':valid and consistent and page==1 and len(records)==total and len(records)<=size,
                'metadataConsistent':consistent,'scope':'当前账号/当前检索条件的指定页；非事务快照'}
            if response_meta:
                coverage['responseMetadata']={key:value if type(value) is int else str(value)[:40] for key,value in response_meta.items()}
        meta={key:deepcopy(value) for key,value in data.items() if key in ('total','outcome','pagesScanned','recordsScanned','complete','reason','scope')}
        if not scan:meta.update(pageNum=page,pageSize=size,returned=len(records))
        # 全部逐页哈希仍在审计/操作结果；200页哈希不能挤掉total和统计。
        coverage.pop('pages',None)
        for target in (meta,coverage):
            for key in ('reason','scope'):
                if isinstance(target.get(key),str):target[key]=target[key][:320]
        meta['coverage']=coverage
        # 普通上游列表的任意extra.statistics不可信；页统计仅由实际记录计算。
        stats=deepcopy(data.get('statistics') if scan and isinstance(data.get('statistics'),dict) else record_stats(tool,records))
        limit=160 if budget>=12000 else 24
        for group in stats.get('groups',{}).values():
            if any(len(label)>limit for label in group.get('top',{})):
                group['topEntries']=[{'label':label[:limit],'count':count,'labelHash':digest(label)} for label,count in group.pop('top').items()]
        meta['statistics']=stats
        meta['list']=[]
        meta['modelView']={'projectedFields':True,'sourceRecords':len(records),'visibleRecords':0,'omittedRecords':len(records),'allCoreRecordsVisible':False,'rawEvidencePreserved':True,'perPageAuditInEvidence':scan,'note':'仅模型阅读窗口；成功列表页/扫描已纳入记录保存在任务证据。异常或预算排除页仅留页元数据及哈希；total不受摘录影响'}
        result_view={'code':result['code'],'msg':str(result.get('msg',''))[:160],'data':meta,'evidence_hash':digest(result)}
        # 小历史窗口优先保留范围与所有记录的数值统计；省略类别汇入其他，明确标识。
        for group in reversed(list(stats.get('groups',{}).values())):
            while len(dumps(result_view))>budget:
                if group.get('topEntries'):
                    count=group['topEntries'].pop()['count']
                elif group.get('top'):
                    count=group['top'].pop(next(reversed(group['top'])))
                else:break
                group['otherRecords']+=count
                group['modelOmittedGroupCount']=group.get('modelOmittedGroupCount',0)+1
        if len(dumps(result_view))>budget:raise ValueError('分页范围元数据超过阅读窗口预算')
        for row in records:
            meta['list'].append(project_record(tool,row))
            view=meta['modelView']
            view.update(visibleRecords=len(meta['list']),omittedRecords=len(records)-len(meta['list']),allCoreRecordsVisible=len(records)==len(meta['list']))
            if len(dumps(result_view))>budget:
                meta['list'].pop()
                view.update(visibleRecords=len(meta['list']),omittedRecords=len(records)-len(meta['list']),allCoreRecordsVisible=len(records)==len(meta['list']))
                break
        return redact(result_view)
    safe=redact(result)
    if len(dumps(safe))<=budget:return safe
    return {'code':result.get('code'),'msg':str(result.get('msg',''))[:500],
        'data':None,'modelView':{'rawEvidencePreserved':True,'note':'大响应明细保存在操作证据中，未传入模型；不能推断业务内容'},'evidence_hash':digest(result)}

async def scan_pages(client,spec,request,token,audit=None,max_bytes=8*1024*1024):
    """调用已有列表，校验页身份/ID/total，完整页纳入累计证据；异常不声称查全。"""
    from .catalog import TOOLS
    from .upstream import UpstreamError
    if not token:raise UpstreamError(401,'平台会话无效，请重新登录',401)
    base=SCAN_TO_LIST[spec.name]
    # 状态/精确匹配过滤只在服务端本地执行：上游列表无对应检索，逐页查全后精确比较，不新增上游路由。
    status_filter=getattr(request,'status',None)
    match_filter=getattr(request,'match',None)
    start=time.monotonic()
    collected=[]
    seen=set()
    pages=[]
    total=None
    last_total=None
    retained_bytes=0
    complete=False
    next_page=1
    reason='达到扫描页数上限，未查全'
    for page in range(1,request.maxPages+1):
        next_page=page
        if time.monotonic()-start>=90:
            reason='达到90秒扫描循环预算，未查全'
            break
        args={'pageNum':page,'pageSize':request.pageSize}
        if request.keyword is not None:args['keyword']=request.keyword
        try:
            response=await client.call(TOOLS[base],args,token)
        except UpstreamError as error:
            if audit:await audit({'pageNum':page,'status':'failed','code':error.code,'reason':error.message})
            if error.code==401:raise
            reason=f'第{page}页查询失败：{error.message}；未查全'
            break
        data=response.get('data')
        if not isinstance(data,dict):
            reason=f'第{page}页无有效分页数据，未查全'
            if audit:await audit({'pageNum':page,'status':'invalid','reason':reason})
            break
        rows=data['list']
        last_total=data['total']
        if total is None:total=last_total
        _,_,response_meta,consistent=page_identity(args,data)
        page_meta={'pageNum':page,'pageSize':request.pageSize,'total':last_total,'returned':len(rows),'evidence_hash':digest(response),'retained':False,'metadataConsistent':consistent}
        if response_meta:page_meta['responseMetadata']=response_meta
        pages.append(page_meta)
        ids=[row['id'] for row in rows]
        if not consistent:
            reason=f'第{page}页响应页码/条数与请求矛盾，未查全'
        elif total!=last_total:
            reason=f'第{page}页total变化，分页非快照；未查全'
        elif len(set(ids))!=len(ids) or seen.intersection(ids):
            reason=f'第{page}页出现重复记录/分页漂移；未查全'
        elif len(rows)>request.pageSize or len(seen)+len(ids)>total:
            reason=f'第{page}页条数与total/页大小矛盾；未查全'
        else:
            if match_filter is not None:
                matched=[row for row in rows if any(row.get(field)==value for field,value in match_filter.items())]
            elif status_filter is not None:
                matched=[row for row in rows if row.get('status')==status_filter]
            else:
                matched=rows
            if retained_bytes+len(dumps(matched).encode('utf-8'))>max_bytes:
                reason='达到8MB累计证据预算，未查全；当前页未纳入已保留记录'
            else:
                retained_bytes+=len(dumps(matched).encode('utf-8'))
                collected.extend(matched)
                seen.update(ids)
                page_meta['retained']=True
                if status_filter is not None or match_filter is not None:page_meta['matched']=len(matched)
                next_page=page+1
        if audit:await audit({**page_meta,'ids':ids})
        if not page_meta['retained']:break
        if match_filter is not None and matched:
            # 精确匹配命中即达到目的；完整字符串比较，命中后不再扫描其余页。
            if len(seen)==total:complete=True
            next_page=None
            reason='已按完整字符串本地精确匹配命中；命中后不再扫描其余页'
            break
        if len(seen)==total:
            complete=True
            next_page=None
            reason='已覆盖本次扫描返回总数，所有纳入记录已保留；当前账号可见范围，非事务快照'
            break
        if len(rows)<request.pageSize:
            reason=f'第{page}页提前结束，实际条数未达到total；未查全'
            break
    coverage={'kind':'scan','total':total,'lastObservedTotal':last_total,'pageSize':request.pageSize,'pagesScanned':len(pages),
        'recordsScanned':len(collected),'complete':complete,'nextPage':next_page,'reason':reason,'scope':'当前账号/当前检索条件；非事务快照','pages':pages}
    filters={}
    if status_filter is not None:filters['status']=status_filter
    if match_filter is not None:filters['match']=match_filter
    if filters:
        coverage['filter']=filters
        coverage['recordsRead']=len(seen)
    response={'code':0,'msg':reason,'data':{'total':total,'list':collected,'outcome':'complete' if complete else 'incomplete',
        'pagesScanned':len(pages),'recordsScanned':len(collected),'complete':complete,'reason':reason,'scope':coverage['scope'],
        'coverage':coverage,'statistics':record_stats(spec.name,collected)}}
    return spec.response_model.model_validate(response).model_dump(mode='json')
