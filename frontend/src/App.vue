<script setup lang="ts">
import { ref, computed, defineComponent, h, onMounted, onUnmounted, watch, type VNode } from 'vue'
import { api, download, upload, setCsrf, requestId, type User, type Tool, type Run, type Conversation, type ConversationDetail, type Workflow, type Approval } from './api'
const user=ref<User|null>(null), page=ref('runs'), busy=ref(false), error=ref(''), success=ref('')
const health=ref<{model:string;environment?:string;upstream?:string;mode:string;features:{llm_configured:boolean;vision_enabled?:boolean;video_monitoring?:boolean}}>({model:'deepseek-flash',environment:'development',mode:'',features:{llm_configured:false}})
const login=ref({account:'',password:'',key:'',verifyCode:''}), captchaImage=ref(''), mockCode=ref('')
const tools=ref<Tool[]>([]), search=ref(''), category=ref('全部'), selectedTool=ref<Tool|null>(null), toolArgs=ref('{}'), schemaTab=ref('purpose')
const runs=ref<Run[]>([]), active=ref<Run|null>(null), prompt=ref(''), workflows=ref<Workflow[]>([]), selectedWorkflow=ref<Workflow|null>(null), parameters=ref('{}')
const conversations=ref<Conversation[]>([]), selectedConversationId=ref<string|null>(null), conversation=ref<ConversationDetail|null>(null), conversationLoading=ref(false)
const approvals=ref<Approval[]>([]), reviewNotes=ref<Record<string,string>>({}), audit=ref<{valid:boolean;event_count:number;head:string}|null>(null)
interface VisionImage { id:string; sha256:string; mime_type:string; byte_size:number; camera_id?:string|null; captured_at:string; created_at:string }
interface VideoChannel { channelId:string; name:string; scene?:string|null; orgName?:string|null; status?:number|null; hasVideo:boolean }
interface VideoMonitor { id:string; channelId:string; scene:'rodent_board'|'clean_door'; roi:{x:number;y:number;width:number;height:number}; intervalSeconds:number; openThresholdSeconds:number; minConfidence:number; enabled:boolean; createdAt:string; updatedAt:string; nextCaptureAt:string; lastCaptureAt?:string|null; lastStatus?:string|null; lastError?:string|null }
interface VisionAlert { id:string; monitorId:string; channelId:string; scene:string; status:string; confidence:number; reason:string; imageId:string; imageHash:string; state:'open'|'acknowledged'|'resolved'; firstDetectedAt:string; lastDetectedAt:string; occurrenceCount:number; acknowledgedAt?:string|null; note?:string|null; resolvedAt?:string|null }
const visionImage=ref<VisionImage|null>(null), visionScene=ref<'rodent_board'|'clean_door'>('rodent_board')
const visionCameraId=ref(''), visionCapturedAt=ref(''), visionRoi=ref({x:0,y:0,width:1,height:1})
const videoChannels=ref<VideoChannel[]>([]), selectedVideoChannel=ref(''), videoChannelsLoaded=ref(false)
const videoMonitors=ref<VideoMonitor[]>([]), videoAlerts=ref<VisionAlert[]>([]), alertNotes=ref<Record<string,string>>({})
const monitorScene=ref<'rodent_board'|'clean_door'>('rodent_board'), monitorRoi=ref({x:0,y:0,width:1,height:1})
const monitorInterval=ref(60), monitorOpenThreshold=ref(300), monitorConfidence=ref(0.8)
const initialWorkflowEditor=JSON.stringify({name:'新监管流程',description:'说明工作流程的目标和范围',parameters:{keyword:''},steps:[{tool:'shop_list',arguments:{pageNum:1,pageSize:10,keyword:'${parameters.keyword}'}}]},null,2)
const workflowEditor=ref(initialWorkflowEditor)
const nav=[{id:'runs',title:'智能任务',symbol:'▦'},{id:'vision',title:'图片识别',symbol:'◈'},{id:'tools',title:'接口工具',symbol:'◇'},{id:'workflows',title:'可复制工作流',symbol:'⇄'},{id:'approvals',title:'人工确认',symbol:'✓'},{id:'audit',title:'审计与溯源',symbol:'≡'}]
nav.splice(1,0,{id:'monitoring',title:'持续监测预警',symbol:'◉'})
const categories=computed(()=>['全部',...new Set(tools.value.map(t=>t.category))])
const filteredTools=computed(()=>tools.value.filter(t=>(category.value==='全部'||t.category===category.value)&&`${t.title} ${t.name} ${t.description}`.toLowerCase().includes(search.value.toLowerCase())))
const runningCount=computed(()=>runs.value.filter(r=>['created','running','interrupted'].includes(r.status)).length)
const canReview=computed(()=>user.value?.role==='reviewer'||user.value?.role==='admin')
const blockedStatuses=['created','running','waiting_approval','interrupted','needs_reconciliation']
const conversationRuns=computed(()=>conversation.value?.id===selectedConversationId.value ? conversation.value?.runs||[] : [])
const blockingRun=computed(()=>selectedConversationId.value ? [...conversationRuns.value,...runs.value.filter(r=>r.owner_id===user.value?.id&&r.conversation_id===selectedConversationId.value)].find(r=>blockedStatuses.includes(r.status)) : undefined)
const pendingApproval=computed(()=>active.value&&active.value.status==='waiting_approval' ? (active.value.approvals||[]).find(a=>a.status==='pending')||approvals.value.find(a=>a.run_id===active.value!.id&&a.status==='pending') : undefined)
const conversationUnavailable=computed(()=>!!selectedConversationId.value&&conversation.value?.id!==selectedConversationId.value)
const states:Record<string,string>={created:'已创建',running:'运行中',waiting_approval:'待人工确认',completed:'已完成',rejected:'人工拒绝',failed:'执行失败',needs_reconciliation:'结果待核对',interrupted:'服务中断'}
const risks:Record<string,string>={read:'只读',write:'写入·直接执行',sensitive:'审批·需确认',auth:'登录专用',unavailable:'未开放'}
const pretty=(v:unknown)=>JSON.stringify(v,null,2)
const legacyReportNotice='此任务保留的是历史结构化结果，暂未提供可读总结。请展开下方执行留痕查看详细证据；新任务将优先展示总结结果。'
function reportInline(text:string):(string|VNode)[]{
  return text.split(/(\*\*[^*]+\*\*)/g).filter(Boolean).map(part=>part.startsWith('**')&&part.endsWith('**') ? h('strong',part.slice(2,-2)) : part)
}
function reportBlocks(text:string):VNode[]{
  if(typeof text!=='string')return [h('p',legacyReportNotice)]
  const lines:string[]=[]
  let fence='', omitted=false
  for(const line of text.replace(/\r\n?/g,'\n').split('\n')){
    const marker=line.match(/^\s*(`{3,}|~{3,})/)
    if(fence){
      if(marker&&marker[1][0]===fence[0]&&marker[1].length>=fence.length&&!line.slice(marker[0].length).trim())fence=''
      continue
    }
    if(marker){fence=marker[1];omitted=true;lines.push('');continue}
    lines.push(line)
  }
  const source=lines.join('\n').trim()
  // Structured legacy output belongs in audit details, not the main summary.
  let structured=false
  try{JSON.parse(source);structured=true}catch{/* Ordinary prose is not JSON. */}
  if(structured||/\{\s*"[^"\n]*"\s*:|\{\s*\}|(?:^|\n)\s*\{/.test(source))return [h('p',legacyReportNotice)]
  const heading=(line:string)=>line.match(/^\s{0,3}(#{1,6})\s+(.+?)(?:\s+#+)?\s*$/)
  const listItem=(line:string)=>line.match(/^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/)
  const cells=(line:string)=>line.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(cell=>cell.trim())
  const tableAt=(index:number)=>{
    if(!lines[index]?.includes('|')||!lines[index+1]?.includes('|'))return false
    const headers=cells(lines[index]), separator=cells(lines[index+1])
    return headers.length>1&&headers.length===separator.length&&separator.every(cell=>/^:?-{3,}:?$/.test(cell))
  }
  const blocks:VNode[]=[]
  for(let i=0;i<lines.length;){
    const line=lines[i]
    if(!line.trim()){i++;continue}
    const title=heading(line)
    if(title){blocks.push(h(`h${title[1].length}`,reportInline(title[2])));i++;continue}
    if(tableAt(i)){
      const headers=cells(line), rows:string[][]=[]
      i+=2
      while(i<lines.length&&lines[i].trim()&&lines[i].includes('|')){
        const row=cells(lines[i])
        if(row.length!==headers.length)break
        rows.push(row);i++
      }
      blocks.push(h('div',{class:'summary-table-scroll',tabindex:0,role:'region','aria-label':'结果表格'},[
        h('table',[
          h('thead',[h('tr',headers.map(cell=>h('th',{scope:'col'},reportInline(cell))))]),
          h('tbody',rows.map(row=>h('tr',row.map(cell=>h('td',reportInline(cell))))))
        ])
      ]))
      continue
    }
    const item=listItem(line)
    if(item){
      const ordered=!!item[2], items:VNode[]=[]
      while(i<lines.length){
        const next=listItem(lines[i])
        if(!next||!!next[2]!==ordered)break
        items.push(h('li',reportInline(next[3])));i++
      }
      blocks.push(h(ordered?'ol':'ul',ordered ? {start:Number(item[2])} : {},items))
      continue
    }
    const paragraph=[line.trim()]
    i++
    while(i<lines.length&&lines[i].trim()&&!heading(lines[i])&&!listItem(lines[i])&&!tableAt(i)){
      paragraph.push(lines[i].trim());i++
    }
    blocks.push(h('p',reportInline(paragraph.join(' '))))
  }
  if(omitted)blocks.push(h('p',{class:'summary-note'},'技术明细已省略，请展开下方执行留痕查看证据。'))
  if(!blocks.length)blocks.push(h('p','此任务暂未提供总结结果，请查看下方执行留痕。'))
  return blocks
}
const ReportSummary=defineComponent({
  props:{text:{type:String,required:true}},
  setup(props){
    const blocks=computed(()=>reportBlocks(props.text))
    return ()=>h('div',{class:'result-summary'},blocks.value)
  }
})
const date=(v:string)=>new Date(v).toLocaleString('zh-CN',{hour12:false})
function resultProvenance(run:Run):string{
  const result=run.result
  if(!result)return ''
  if(result.context_reused===true)return `仅对历史结果进行排版，未发起新查询。${result.source_run_id ? ` 来源任务：${result.source_run_id}。` : ''}`
  if(result.fresh_query_required===true){
    if((result.current_evidence_count??0)>0)return `本次执行已有 ${result.current_evidence_count} 条证据，但不代表查询完整或成功。`
    return run.status==='failed' ? '新查询执行失败，且没有本次执行证据；数据未刷新。' : '已请求新查询，但暂无本次执行证据，无法确认数据已刷新。'
  }
  return '未提供查询来源信息，无法确认数据已刷新。'
}
let promptVersion=0
watch(prompt,()=>{promptVersion++},{flush:'sync'})
const submissionRetry=new Map<string,Record<string,unknown>>()
async function submitOnce(path:string,body:Record<string,unknown>):Promise<Run>{
  const key=JSON.stringify([path,body]), previous=submissionRetry.get(key)
  const request=previous || {...body,idempotency_key:requestId()}
  submissionRetry.set(key,request)
  const identity=identityVersion, owner=user.value?.id
  const result=await api<Run>(path,'POST',request)
  if(typeof body.prompt==='string'&&result.prompt!==body.prompt)throw new Error('服务端返回的任务与提交内容不一致，未加载旧结果。直接重试将沿用同一请求标识。')
  if(typeof body.prompt==='string'&&(result.owner_id!==owner||(typeof body.conversation_id==='string'&&result.conversation_id!==body.conversation_id)))throw new Error('服务端返回的任务与提交时的身份或上下文不一致，未加载该结果。')
  if(identity===identityVersion&&submissionRetry.get(key)===request)submissionRetry.delete(key)
  return result
}
const latestFirst=(items:Conversation[])=>[...items].sort((a,b)=>Date.parse(b.updated_at)-Date.parse(a.updated_at)||Date.parse(b.created_at)-Date.parse(a.created_at))
let identityVersion=0, selectionVersion=0, refreshing:Promise<void>|null=null
let timer:ReturnType<typeof setInterval>|undefined, polling=false, disposed=false

async function action(fn:()=>Promise<void>){
  if(busy.value)return
  const version=identityVersion
  busy.value=true;error.value='';success.value=''
  try{
    if(refreshing)await refreshing.catch(()=>{})
    if(version===identityVersion)await fn()
  }catch(e){if(version===identityVersion)error.value=e instanceof Error?e.message:String(e)}
  finally{if(version===identityVersion)busy.value=false}
}
function clearIdentity(){
  identityVersion++;selectionVersion++;setCsrf('');refreshing=null;submissionRetry.clear()
  user.value=null;page.value='runs';busy.value=false;error.value='';success.value=''
  runs.value=[];active.value=null;prompt.value='';conversations.value=[];selectedConversationId.value=null;conversation.value=null;conversationLoading.value=false
  tools.value=[];selectedTool.value=null;toolArgs.value='{}';schemaTab.value='purpose';search.value='';category.value='全部'
  workflows.value=[];selectedWorkflow.value=null;parameters.value='{}';workflowEditor.value=initialWorkflowEditor
  approvals.value=[];reviewNotes.value={};audit.value=null
  visionImage.value=null;visionScene.value='rodent_board';visionCameraId.value='';visionCapturedAt.value='';visionRoi.value={x:0,y:0,width:1,height:1}
  videoChannels.value=[];selectedVideoChannel.value='';videoChannelsLoaded.value=false
  videoMonitors.value=[];videoAlerts.value=[];alertNotes.value={};monitorScene.value='rodent_board';monitorRoi.value={x:0,y:0,width:1,height:1};monitorInterval.value=60;monitorOpenThreshold.value=300;monitorConfidence.value=.8
  login.value={account:'',password:'',key:'',verifyCode:''};captchaImage.value='';mockCode.value=''
}
function expire(){clearIdentity();error.value='登录状态已过期，请重新登录'}
async function captcha(){await action(async()=>{const data=await api<{data:{key:string;image:string;_mockCode?:string}}>('/auth/captcha','POST',{});login.value.key=data.data.key;captchaImage.value='data:image/png;base64,'+data.data.image;mockCode.value=data.data._mockCode||''})}
async function signIn(){await action(async()=>{const data=await api<{user:User;csrf_token:string}>('/auth/login','POST',login.value);user.value=data.user;setCsrf(data.csrf_token);login.value.password='';login.value.verifyCode='';await refreshAll(true)})}
async function signOut(){await action(async()=>{await api('/auth/logout','POST',{});clearIdentity()})}
function storeConversation(detail:ConversationDetail){
  if(detail.owner_id!==user.value?.id)throw new Error('无权查看此会话')
  conversation.value={...detail,runs:[...detail.runs]}
  const summary:Conversation={id:detail.id,owner_id:detail.owner_id,title:detail.title,created_at:detail.created_at,updated_at:detail.updated_at}
  conversations.value=latestFirst([...conversations.value.filter(c=>c.id!==detail.id),summary])
}
async function openConversation(cid:string, run?:Run){
  const version=++selectionVersion, identity=identityVersion, owner=user.value?.id
  const current=()=>version===selectionVersion&&identity===identityVersion&&owner===user.value?.id&&cid===selectedConversationId.value
  selectedConversationId.value=cid;conversation.value=null;active.value=run||null;conversationLoading.value=true
  try{
    const detail=await api<ConversationDetail>('/conversations/'+encodeURIComponent(cid))
    if(!current())return
    if(detail.id!==cid)throw new Error('返回的任务上下文与所选任务不一致。')
    storeConversation(detail)
    const last=detail.runs.at(-1)
    if(!run&&last){
      const fullRun=await api<Run>('/runs/'+encodeURIComponent(last.id))
      if(current()&&fullRun.id===last.id)active.value=fullRun
    }
  }catch(e){if(current())throw e}
  finally{if(current())conversationLoading.value=false}
}
function newConversation(){
  selectionVersion++;submissionRetry.clear();selectedConversationId.value=null;conversation.value=null;conversationLoading.value=false;active.value=null;prompt.value='';error.value='';success.value=''
}
function showStandaloneRun(run:Run,clearPrompt=true){
  selectionVersion++;selectedConversationId.value=null;conversation.value=null;conversationLoading.value=false;active.value=run
  if(clearPrompt)prompt.value=''
}
async function showRun(run:Run,clearPrompt=true){
  if(run.owner_id===user.value?.id&&run.conversation_id)await openConversation(run.conversation_id,run)
  else showStandaloneRun(run,clearPrompt)
}
async function refreshActivity(restoreLatest=false):Promise<void>{
  if(refreshing)return refreshing
  if(!user.value)return
  const identity=identityVersion, owner=user.value.id, selection=selectionVersion, cid=selectedConversationId.value, runId=active.value?.id
  const task=(async()=>{
    const requests=[
      api<Run[]>('/runs'),api<Conversation[]>('/conversations'),
      cid ? api<ConversationDetail>('/conversations/'+encodeURIComponent(cid)) : Promise.resolve(null),
      runId ? api<Run>('/runs/'+encodeURIComponent(runId)) : Promise.resolve(null),
      api<Approval[]>('/approvals')
    ] as const
    // Failed requests must not release the polling lock while others are still pending.
    await Promise.allSettled(requests)
    if(identity!==identityVersion||owner!==user.value?.id||selection!==selectionVersion||cid!==selectedConversationId.value)return
    const [loadedRuns,loadedConversations,detail,fullRun,pending]=await Promise.all(requests)
    if(identity!==identityVersion||owner!==user.value?.id||selection!==selectionVersion||cid!==selectedConversationId.value)return
    runs.value=loadedRuns;conversations.value=latestFirst(loadedConversations.filter(c=>c.owner_id===owner))
    if(pending)approvals.value=pending
    if(detail&&detail.id===cid)storeConversation(detail)
    if(fullRun&&fullRun.id===runId&&active.value?.id===runId&&fullRun.prompt===active.value.prompt)active.value=fullRun
    if(restoreLatest&&!cid&&conversations.value.length)await openConversation(conversations.value[0].id)
  })()
  refreshing=task
  try{await task}finally{if(refreshing===task)refreshing=null}
}
async function refreshAll(restoreLatest=false){
  const identity=identityVersion
  const [loadedTools,loadedWorkflows,pending]=await Promise.all([api<Tool[]>('/tools'),api<Workflow[]>('/workflows'),api<Approval[]>('/approvals')])
  if(identity!==identityVersion||!user.value)return
  tools.value=loadedTools;workflows.value=loadedWorkflows;approvals.value=pending
  await refreshMonitoring()
  await refreshActivity(restoreLatest)
}
async function selectRun(r:Run){
  const identity=identityVersion, owner=user.value?.id, edit=promptVersion
  let selection=selectionVersion, selected=selectedConversationId.value
  const current=()=>identity===identityVersion&&owner===user.value?.id&&selection===selectionVersion&&selected===selectedConversationId.value
  await action(async()=>{
    if(!current())return
    try{
      const run=await api<Run>('/runs/'+encodeURIComponent(r.id))
      if(!current())return
      if(edit===promptVersion)prompt.value=''
      selection++;selected=run.owner_id===owner ? run.conversation_id : null
      await showRun(run,false)
    }catch(e){if(current())throw e}
  })
}
function selectTool(t:Tool){selectedTool.value=t;toolArgs.value=pretty(t.example);schemaTab.value='purpose'}
async function invokeTool(){if(!selectedTool.value)return;await action(async()=>{const arguments_=JSON.parse(toolArgs.value);if(!arguments_||Array.isArray(arguments_)||typeof arguments_!=='object')throw new Error('入参必须为JSON对象');const run=await submitOnce('/tools/'+selectedTool.value!.name+'/invoke',{arguments:arguments_});showStandaloneRun(run);page.value='runs';await refreshActivity()})}
async function uploadVisionImage(event:Event){
  const input=event.target as HTMLInputElement, file=input.files?.[0]
  if(!file)return
  await action(async()=>{
    if(!['image/jpeg','image/png','image/webp'].includes(file.type))throw new Error('仅支持 JPEG、PNG 和 WebP 图片')
    if(file.size>8*1024*1024)throw new Error('图片不能超过 8MB')
    const form=new FormData()
    form.append('file',file)
    if(visionCameraId.value.trim())form.append('camera_id',visionCameraId.value.trim())
    if(visionCapturedAt.value){
      const captured=new Date(visionCapturedAt.value)
      if(Number.isNaN(captured.getTime()))throw new Error('采集时间无效')
      form.append('captured_at',captured.toISOString())
    }
    visionImage.value=await upload<VisionImage>('/vision/images',form)
    success.value='图片已上传；可选择场景后开始识别'
  })
  input.value=''
}
async function loadVideoChannels(){await action(async()=>{
  const result=await api<{code:number;data:{list:VideoChannel[]}}>('/video/channels','POST',{pageNum:1,pageSize:50})
  videoChannels.value=result.data.list.filter(channel=>channel.hasVideo)
  videoChannelsLoaded.value=true
  if(!selectedVideoChannel.value&&videoChannels.value.length)selectedVideoChannel.value=videoChannels.value[0].channelId
})}
async function captureVideoSnapshot(){
  if(!selectedVideoChannel.value)return
  await action(async()=>{
    const result=await api<{code:number;data:{imageId:string;imageHash:string;channelId:string;mimeType:string;byteSize:number;capturedAt:string}}>('/video/snapshot','POST',{channelId:selectedVideoChannel.value})
    visionImage.value={id:result.data.imageId,sha256:result.data.imageHash,mime_type:result.data.mimeType,byte_size:result.data.byteSize,camera_id:result.data.channelId,captured_at:result.data.capturedAt,created_at:result.data.capturedAt}
    if(!visionCameraId.value)visionCameraId.value=result.data.channelId
    success.value='已获取实时截帧；可直接开始识别'
  })
}
async function refreshMonitoring(){
  const [monitors,alerts]=await Promise.all([api<VideoMonitor[]>('/video/monitors'),api<VisionAlert[]>('/video/alerts?active_only=true')])
  videoMonitors.value=monitors;videoAlerts.value=alerts
}
async function createVideoMonitor(){
  if(!selectedVideoChannel.value)throw new Error('请先加载并选择视频通道')
  await action(async()=>{
    await api<VideoMonitor>('/video/monitors','POST',{
      channelId:selectedVideoChannel.value,scene:monitorScene.value,roi:{...monitorRoi.value},
      intervalSeconds:monitorInterval.value,openThresholdSeconds:monitorOpenThreshold.value,minConfidence:monitorConfidence.value,
    })
    await refreshMonitoring();success.value='持续监测已启动，异常将生成内部预警并写入审计链'
  })
}
async function updateVideoMonitor(monitor:VideoMonitor,enabled:boolean){await action(async()=>{await api('/video/monitors/'+monitor.id,'POST',{enabled});await refreshMonitoring()})}
async function refreshMonitorToken(monitor:VideoMonitor){await action(async()=>{await api('/video/monitors/'+monitor.id+'/refresh-token','POST',{});await refreshMonitoring();success.value='监测令牌已刷新'})}
async function acknowledgeAlert(alert:VisionAlert){await action(async()=>{const note=(alertNotes.value[alert.id]||'').trim();if(note.length<2)throw new Error('请填写至少 2 个字符的处置说明');await api('/video/alerts/'+alert.id+'/acknowledge','POST',{note});await refreshMonitoring();success.value='预警已确认，持续异常仍会更新同一预警'})}
async function runVision(){
  if(!visionImage.value)return
  await action(async()=>{
    const cameraId=visionCameraId.value.trim()
    if(visionScene.value==='clean_door'&&!cameraId)throw new Error('清洁区门识别需要填写摄像头标识')
    const arguments_={imageId:visionImage.value!.id,roi:{...visionRoi.value},...(visionScene.value==='clean_door'?{cameraId,openThresholdSeconds:60}:{})}
    const tool=visionScene.value==='rodent_board'?'vision_rodent_board':'vision_clean_door'
    const run=await submitOnce('/tools/'+tool+'/invoke',{arguments:arguments_})
    showStandaloneRun(run);page.value='runs';await refreshActivity()
  })
}
async function submitTask(){
  if(busy.value||!user.value||!prompt.value.trim()||conversationLoading.value||conversationUnavailable.value||blockingRun.value)return
  const cid=selectedConversationId.value, text=prompt.value, identity=identityVersion, owner=user.value.id, edit=promptVersion
  // Invalidate old refreshes and remove old output at click time, before waiting.
  let selection=++selectionVersion, selected=cid
  active.value=null
  const current=()=>identity===identityVersion&&owner===user.value?.id&&selection===selectionVersion&&selected===selectedConversationId.value
  await action(async()=>{
    if(!current())return
    try{
      if(cid){
        const detail=await api<ConversationDetail>('/conversations/'+encodeURIComponent(cid))
        if(!current())return
        if(detail.id!==cid)throw new Error('返回的任务上下文与所选任务不一致，已停止提交。')
        storeConversation(detail)
        if(detail.runs.some(r=>blockedStatuses.includes(r.status)))throw new Error('当前上下文仍有未结束任务，请先完成执行、人工确认或结果核对后再提交。')
      }
      if(!current())return
      const run=await submitOnce('/runs',{prompt:text,...(cid ? {conversation_id:cid} : {})})
      if(!current())return
      runs.value=[run,...runs.value.filter(r=>r.id!==run.id)];active.value=run
      if(edit===promptVersion&&prompt.value===text)prompt.value=''
      // Focus the accepted ID, never the latest run in its conversation.
      selection++;selected=run.conversation_id
      await showRun(run,false)
      if(!current()||active.value?.id!==run.id)return
      await refreshActivity()
    }catch(e){if(current())throw e}
  })
}
function chooseWorkflow(w:Workflow){selectedWorkflow.value=w;parameters.value=pretty(w.parameters)}
async function executeWorkflow(){if(!selectedWorkflow.value)return;await action(async()=>{const run=await submitOnce('/runs',{prompt:selectedWorkflow.value!.name,workflow_id:selectedWorkflow.value!.id,conversation_id:null,parameters:JSON.parse(parameters.value)});showStandaloneRun(run);page.value='runs';await refreshActivity()})}
async function createWorkflow(){await action(async()=>{await api('/workflows','POST',JSON.parse(workflowEditor.value));workflows.value=await api<Workflow[]>('/workflows');success.value='流程已保存，可导出并复制部署'})}
async function importWorkflow(event:Event){const file=(event.target as HTMLInputElement).files?.[0];if(!file)return;await action(async()=>{const identity=identityVersion;if(file.size>1048576)throw new Error('导入文件不能超过1MB');const text=await file.text();if(identity!==identityVersion)return;await api('/workflows/import','POST',JSON.parse(text));workflows.value=await api<Workflow[]>('/workflows');success.value='工作流导入成功'});(event.target as HTMLInputElement).value=''}
async function decide(a:Approval,decision:'approve'|'reject'){await action(async()=>{const note=reviewNotes.value[a.id]||'';if(note.trim().length<2)throw new Error('请填写人工核验意见，至少2个字符');if(!window.confirm(decision==='approve'?'确认已核验工具作用、完整参数和证据，并授权执行此操作？':'确认拒绝该操作？'))return;const run=await api<Run>('/runs/'+a.run_id+'/resume','POST',{decision,approval_id:a.id,payload_hash:a.payload_hash,note});await showRun(run);approvals.value=await api<Approval[]>('/approvals');await refreshActivity();success.value=decision==='approve'?'人工决定已记录，任务继续执行':'已拒绝，工具不会执行'})}
async function recover(){if(!active.value)return;await action(async()=>{const run=await api<Run>('/runs/'+active.value!.id+'/recover','POST',{});await showRun(run);await refreshActivity()})}
async function checkAudit(){await action(async()=>{audit.value=await api('/audit/verify')})}
async function exportRun(){if(active.value)await action(()=>download('/runs/'+active.value!.id+'/export','run-'+active.value!.id+'.json'))}
async function exportWorkflow(w:Workflow){await action(()=>download('/workflows/'+w.id+'/export','workflow-'+w.id+'.json'))}
watch(page,async p=>{if(!user.value)return;if(p==='approvals')await action(async()=>{approvals.value=await api<Approval[]>('/approvals')});if(p==='audit')await checkAudit()})
watch(page,async p=>{if(p==='monitoring'&&user.value)await action(async()=>{await refreshMonitoring()})})
onMounted(async()=>{
  window.addEventListener('session-expired',expire)
  await action(async()=>{health.value=await api('/health');const data=await api<{user:User;csrf_token:string}>('/auth/me');user.value=data.user;setCsrf(data.csrf_token);await refreshAll(true)})
  if(disposed)return
  timer=setInterval(async()=>{
    if(!user.value||document.hidden||polling||busy.value||conversationLoading.value)return
    const identity=identityVersion
    polling=true
    try{await refreshActivity();if(health.value.features.video_monitoring)await refreshMonitoring()}catch(e){if(identity===identityVersion)error.value=e instanceof Error?e.message:String(e)}finally{polling=false}
  },2500)
})
onUnmounted(()=>{disposed=true;if(timer)clearInterval(timer);window.removeEventListener('session-expired',expire);clearIdentity()})
</script>

<template>
<div v-if="!user" class="login-shell">
  <section class="login-intro"><div class="brand"><span class="logo">安</span><div>怀柔食安<span>REGULATORY AGENT</span></div></div><div class="intro-copy"><span class="eyebrow">可信执行 · 人机协同</span><h1>让监管流程<br>更高效，也更可信。</h1><p>从自然语言任务到可复制工作流，<br>每次调用有证据，每个决定有边界。</p><div class="intro-features"><div><b>01</b>全流程留痕</div><div><b>02</b>审批决定人工确认</div><div><b>03</b>工作流可复制部署</div></div></div><div class="intro-footer">FASTAPI / VUE 3 / LANGGRAPH</div></section>
  <section class="login-form"><div class="login-card"><span class="eyebrow">安全登录</span><h2>欢迎使用监管智能体</h2><p class="muted">请使用管理员配置的本应用与平台一致账号。</p><form @submit.prevent="signIn"><label>账号<input v-model="login.account" required autocomplete="username" placeholder="输入平台账号"></label><label>密码<input v-model="login.password" type="password" required autocomplete="current-password" placeholder="输入账号密码"></label><label>图形验证码<div class="captcha-row"><input v-model="login.verifyCode" required minlength="4" maxlength="4" placeholder="4位验证码"><button v-if="!captchaImage" type="button" class="secondary" @click="captcha">获取验证码</button><img v-else :src="captchaImage" alt="图形验证码，点击刷新" @click="captcha"></div></label><small v-if="mockCode" class="mock-note">联调验证码答案：{{mockCode}}（仅 Mock 环境）</small><div v-if="error" class="alert danger">{{error}}</div><button class="primary full" :disabled="busy||!login.key">{{busy?'正在验证…':'登录工作台'}}</button></form><div class="security-note">会话通过 HttpOnly Cookie 保存。账号权限不依赖 Mock 平台返回的管理员标记。</div></div></section>
</div>
<div v-else class="app-shell">
  <aside class="sidebar"><div class="brand"><span class="logo">安</span><div>怀柔食安<span>监管智能体工作台</span></div></div><div class="sidebar-label">WORKSPACE</div><nav><button v-for="n in nav" :key="n.id" :class="{selected:page===n.id}" @click="page=n.id"><span>{{n.symbol}}</span>{{n.title}}<em v-if="n.id==='approvals'&&approvals.length">{{approvals.length}}</em></button></nav><div class="boundary-card"><span class="boundary-icon">◈</span><strong>执行有边界</strong><p>审批/处罚决定必须提交独立人工确认；<br>其余白名单操作直接执行、全程留痕。</p></div><div class="sidebar-bottom"><span class="status-dot"></span>持久化审计已启用</div></aside>
  <div class="main-shell"><header class="topbar"><div><span class="muted">工作台 / </span><strong>{{nav.find(n=>n.id===page)?.title}}</strong></div><div class="user-block"><span class="avatar">{{user.nickname.slice(0,1)}}</span><div>{{user.nickname}}<small>{{user.role}}</small></div><button class="text-button" @click="signOut">退出</button></div></header>
  <main><div class="page-heading"><div><span class="eyebrow">MARKET SUPERVISION</span><h1>{{nav.find(n=>n.id===page)?.title}}</h1><p>可信执行、全程溯源，让人工操作沉淀为可复用能力。</p></div><span class="environment-badge">{{health.mode==='mock-integration'?'MOCK 联调':'受控运行环境'}}</span></div><div v-if="error" class="alert danger">{{error}}<button @click="error=''">×</button></div><div v-if="success" class="alert positive">{{success}}</div>
  <div class="stats"><div><span>接口工具</span><strong>{{tools.length}}<small>个工具定义</small></strong></div><div><span>执行中 / 中断</span><strong>{{runningCount}}<small>个任务</small></strong></div><div><span>待人工确认</span><strong>{{approvals.length}}<small>个请求</small></strong></div><div><span>模型引擎</span><strong class="model-name">{{health.model}}</strong><small>{{health.features.llm_configured?'模型已配置':'等待服务端配置 API Key'}}</small></div></div>

  <template v-if="page==='runs'">
  <section v-if="user.role!=='reviewer'" class="panel task-compose">
    <div class="panel-title"><h2>创建智能任务</h2><div class="compose-actions"><span class="tag blue">LangGraph</span><button class="secondary new-task-button" :disabled="busy||conversationLoading" @click="newConversation">新任务</button></div></div>
    <p class="muted">描述任务目标与业务范围。智能体将规划、调用工具、核验结果；审批决定类步骤暂停等待人工。</p>
    <div v-if="conversationLoading" class="context-note" role="status">正在读取任务上下文…</div>
    <div v-else-if="conversationUnavailable" class="alert warning" role="status">任务上下文尚未读取成功，请刷新后继续，或点击“新任务”独立执行。</div>
    <div v-if="blockingRun" class="alert warning" role="status">当前上下文仍有任务{{states[blockingRun.status]||blockingRun.status}}。请先完成执行、人工确认或结果核对后再提交任务。</div>
    <textarea v-model="prompt" :disabled="busy||conversationLoading" placeholder="例如：查询学校食堂相关预警，核对各状态数量，生成带工具依据的监管概览。" rows="3"></textarea>
    <div class="compose-footer"><div class="compose-hints"><small class="context-hint">{{selectedConversationId?'上下文仅辅助理解引用；新增查询条件会重新查询，仅纯排版请求复用历史结果。':'新任务：不沿用历史上下文或结果。'}}</small><span>不生成处罚决定 · 图片识别仅依据上传图像</span></div><button class="primary" :disabled="busy||!prompt.trim()||conversationLoading||conversationUnavailable||!!blockingRun" @click="submitTask">执行任务 →</button></div>
    <small v-if="!health.features.llm_configured" class="muted model-note">模型尚未配置：简单查全、常见场景查询（如“查询就餐区的摄像头信息”）、精确预警 ID 与已有结果列表排版可直接提交；其他复杂自然语言任务仍需服务端配置 API Key。接口工具与预置工作流也可独立执行。</small>
  </section>
  <div class="run-grid"><section class="panel history"><div class="panel-title"><h2>任务记录</h2><button class="text-button" :disabled="busy||conversationLoading" @click="action(()=>refreshAll())">刷新</button></div><div v-if="!runs.length" class="empty">还没有任务。从工具或工作流开始。</div><button v-for="r in runs" :key="r.id" class="run-item" :class="{active:active?.id===r.id}" :disabled="busy||conversationLoading" @click="selectRun(r)"><strong>{{r.prompt}}</strong><div><span class="tag" :class="r.status">{{states[r.status]||r.status}}</span><small>{{date(r.created_at)}}</small></div></button></section>
  <section class="panel run-detail"><div v-if="!active" class="empty tall"><span class="empty-symbol">◎</span>选择任务，查看执行证据与结果。</div><template v-else><div class="panel-title"><h2>{{active.prompt}}</h2><span class="tag" :class="active.status">{{states[active.status]||active.status}}</span></div><div class="run-meta">任务ID {{active.id}} · {{active.mode}} <button class="text-button" @click="exportRun">导出审计</button></div><p v-if="active.result" class="context-note result-provenance" role="note">{{resultProvenance(active)}}</p><div v-if="active.error" class="alert danger">{{active.error}}</div><div v-if="active.status==='interrupted'" class="alert warning">服务中断。仅在确认平台会话有效后恢复；不确定写入不会自动重试。<button class="secondary" @click="recover">恢复任务</button></div><div v-if="active.status==='waiting_approval'" class="alert warning">任务已暂停等待人工确认：审批/处罚决定尚未执行。审核员在下方确认后任务将自动继续运行并产出结果；拒绝则终止该操作。</div>
<section v-if="pendingApproval" class="panel approval-card"><div class="panel-title"><h2>待人工确认：{{tools.find(t=>t.name===pendingApproval!.tool_name)?.title||pendingApproval!.tool_name}}</h2><span class="tag sensitive">暂停执行 · 待核验</span></div><p>{{pendingApproval!.reason}}</p><div class="run-meta">请求时间 {{date(pendingApproval!.requested_at)}} · 任务 {{active.id}}</div><pre class="payload">{{pretty(pendingApproval!.arguments)}}</pre><code class="hash-block">确认载荷 SHA256：{{pendingApproval!.payload_hash}}</code><label>人工核验意见<textarea v-model="reviewNotes[pendingApproval!.id]" rows="2" placeholder="记录核验依据与人工决定"></textarea></label><div class="button-row"><span v-if="!canReview" class="muted">当前角色不可审核，请审核员或管理员在“人工确认”页处理</span><span v-else-if="pendingApproval!.owner_id===user.id" class="muted">发起者不能自审，请独立审核员确认</span><template v-else><button class="secondary reject-button" :disabled="busy" @click="decide(pendingApproval!,'reject')">拒绝执行</button><button class="primary" :disabled="busy" @click="decide(pendingApproval!,'approve')">已人工核验，确认执行</button></template></div></section><section v-if="active.result && (active.result.display_report ?? active.result.report) != null" class="report"><h3>总结结果</h3><ReportSummary :text="active.result.display_report ?? active.result.report ?? ''" /></section><h3 class="section-label">全流程执行留痕</h3><div class="timeline"><article v-for="e in active.events" :key="e.id"><span class="timeline-dot"></span><div><strong>{{e.kind}}</strong><small>{{date(e.created_at)}}</small><details><summary>查看脱敏证据与哈希</summary><pre>{{pretty(e.payload)}}</pre><code>SHA256 {{e.hash}}</code></details></div></article></div></template></section></div></template>

  <template v-if="page==='monitoring'"><div class="monitor-layout"><section class="panel monitor-config"><div class="panel-title"><h2>配置持续监测</h2><button class="secondary" :disabled="busy||user.role==='reviewer'" @click="loadVideoChannels">{{videoChannelsLoaded?'刷新通道':'加载通道'}}</button></div><div v-if="videoChannelsLoaded" class="monitor-fields"><label>视频通道<select v-model="selectedVideoChannel" :disabled="busy||user.role==='reviewer'||!videoChannels.length"><option v-if="!videoChannels.length" value="">未找到可用视频通道</option><option v-for="channel in videoChannels" :key="channel.channelId" :value="channel.channelId">{{channel.name}} · {{channel.orgName||'未标注机构'}} · {{channel.channelId}}</option></select></label><label>识别场景<select v-model="monitorScene" :disabled="busy||user.role==='reviewer'"><option value="rodent_board">防鼠板状态</option><option value="clean_door">清洁区门状态</option></select></label><label>抽帧间隔（秒）<input v-model.number="monitorInterval" type="number" min="30" max="3600" :disabled="busy||user.role==='reviewer'"></label><label>最低置信度<input v-model.number="monitorConfidence" type="number" min="0.5" max="1" step="0.05" :disabled="busy||user.role==='reviewer'"></label><label v-if="monitorScene==='clean_door'">开门阈值（秒）<input v-model.number="monitorOpenThreshold" type="number" min="30" max="86400" :disabled="busy||user.role==='reviewer'"></label></div><h3>监测区域</h3><div class="roi-grid"><label>X<input v-model.number="monitorRoi.x" type="number" min="0" max="0.999" step="0.01" :disabled="busy||user.role==='reviewer'"></label><label>Y<input v-model.number="monitorRoi.y" type="number" min="0" max="0.999" step="0.01" :disabled="busy||user.role==='reviewer'"></label><label>宽度<input v-model.number="monitorRoi.width" type="number" min="0.001" max="1" step="0.01" :disabled="busy||user.role==='reviewer'"></label><label>高度<input v-model.number="monitorRoi.height" type="number" min="0.001" max="1" step="0.01" :disabled="busy||user.role==='reviewer'"></label></div><button class="primary full" :disabled="busy||!selectedVideoChannel||user.role==='reviewer'||!health.features.vision_enabled||!health.features.video_monitoring" @click="createVideoMonitor">启动持续监测</button></section><section class="panel"><div class="panel-title"><h2>当前预警</h2><button class="secondary" :disabled="busy" @click="action(()=>refreshMonitoring())">刷新</button></div><div v-if="!videoAlerts.length" class="empty">暂无待处置预警</div><article v-for="alert in videoAlerts" :key="alert.id" class="monitor-item alert-item"><div><strong>{{alert.scene==='rodent_board'?'防鼠板':'清洁区门'}} · {{alert.status}}</strong><span class="tag sensitive">{{alert.state==='open'?'待处置':'已确认'}}</span></div><p>{{alert.reason}}</p><small>通道 {{alert.channelId}} · 置信度 {{Math.round(alert.confidence*100)}}% · 第 {{alert.occurrenceCount}} 次</small><label v-if="alert.state==='open'">处置说明<textarea v-model="alertNotes[alert.id]" rows="2"></textarea></label><button v-if="alert.state==='open'" class="secondary" :disabled="busy" @click="acknowledgeAlert(alert)">确认预警</button></article></section><section class="panel monitor-list"><div class="panel-title"><h2>监测任务</h2><span class="tag blue">{{videoMonitors.length}}</span></div><div v-if="!videoMonitors.length" class="empty">尚未创建持续监测任务</div><article v-for="monitor in videoMonitors" :key="monitor.id" class="monitor-item"><div><strong>{{monitor.scene==='rodent_board'?'防鼠板':'清洁区门'}} · {{monitor.channelId}}</strong><span class="tag" :class="monitor.enabled?'completed':'interrupted'">{{monitor.enabled?'运行中':'已暂停'}}</span></div><small>每 {{monitor.intervalSeconds}} 秒抽帧 · 最低置信度 {{Math.round(monitor.minConfidence*100)}}%</small><p v-if="monitor.lastError" class="warning-text">{{monitor.lastError}}</p><p v-else-if="monitor.lastCaptureAt" class="muted">最近识别：{{monitor.lastStatus||'无'}} · {{date(monitor.lastCaptureAt)}}</p><div class="monitor-actions"><button class="secondary" :disabled="busy||user.role==='reviewer'" @click="updateVideoMonitor(monitor,!monitor.enabled)">{{monitor.enabled?'暂停':'恢复'}}</button><button class="text-button" :disabled="busy||user.role==='reviewer'" @click="refreshMonitorToken(monitor)">刷新令牌</button></div></article></section></div></template>

  <template v-if="page==='vision'"><div class="vision-layout"><section class="panel"><div class="panel-title"><h2>图片与采集信息</h2><span class="tag blue">原图仅服务器保存</span></div><div class="video-source"><div class="panel-title"><h3>脱敏视频通道</h3><button class="secondary" :disabled="busy||user.role==='reviewer'" @click="loadVideoChannels">{{videoChannelsLoaded?'刷新通道':'加载通道'}}</button></div><div v-if="videoChannelsLoaded" class="video-actions"><select v-model="selectedVideoChannel" :disabled="busy||user.role==='reviewer'||!videoChannels.length"><option v-if="!videoChannels.length" value="">未找到可用视频通道</option><option v-for="channel in videoChannels" :key="channel.channelId" :value="channel.channelId">{{channel.name}} · {{channel.orgName||'未标注机构'}} · {{channel.channelId}}</option></select><button class="primary" :disabled="busy||!selectedVideoChannel||user.role==='reviewer'" @click="captureVideoSnapshot">获取截帧</button></div></div><label class="upload-control">选择本地图片<input type="file" accept="image/jpeg,image/png,image/webp" :disabled="busy||user.role==='reviewer'" @change="uploadVisionImage"></label><div v-if="visionImage" class="vision-image-meta"><strong>图片已就绪</strong><code>{{visionImage.id}}</code><span>{{visionImage.mime_type}} · {{Math.ceil(visionImage.byte_size/1024)}} KB</span><span>SHA256 {{visionImage.sha256}}</span></div><div class="vision-fields"><label>摄像头标识<input v-model.trim="visionCameraId" :disabled="busy||user.role==='reviewer'" maxlength="128" placeholder="清洁区门场景必填"></label><label>采集时间<input v-model="visionCapturedAt" :disabled="busy||user.role==='reviewer'" type="datetime-local"></label></div></section><section class="panel"><div class="panel-title"><h2>识别场景与区域</h2><span class="tag read">DeepSeek 视觉</span></div><div class="scene-choice"><button :class="{selected:visionScene==='rodent_board'}" @click="visionScene='rodent_board'">防鼠板状态</button><button :class="{selected:visionScene==='clean_door'}" @click="visionScene='clean_door'">清洁区门状态</button></div><p class="muted">ROI 使用图片宽高的归一化比例；结果和门开启时长在任务记录中查看。</p><div class="roi-grid"><label>X<input v-model.number="visionRoi.x" type="number" min="0" max="0.999" step="0.01" :disabled="busy||user.role==='reviewer'"></label><label>Y<input v-model.number="visionRoi.y" type="number" min="0" max="0.999" step="0.01" :disabled="busy||user.role==='reviewer'"></label><label>宽度<input v-model.number="visionRoi.width" type="number" min="0.001" max="1" step="0.01" :disabled="busy||user.role==='reviewer'"></label><label>高度<input v-model.number="visionRoi.height" type="number" min="0.001" max="1" step="0.01" :disabled="busy||user.role==='reviewer'"></label></div><button class="primary full" :disabled="busy||!visionImage||user.role==='reviewer'||!health.features.vision_enabled" @click="runVision">开始识别</button><small v-if="!health.features.vision_enabled" class="muted">服务端尚未配置视觉模型 API Key。</small></section></div></template>

  <template v-if="page==='tools'"><div class="tool-layout"><section><div class="toolbar"><input v-model="search" placeholder="搜索工具名称、用途或接口…"><select v-model="category"><option v-for="c in categories" :key="c">{{c}}</option></select></div><div class="tool-cards"><button v-for="t in filteredTools" :key="t.name" class="tool-card" :class="{active:selectedTool?.name===t.name}" @click="selectTool(t)"><div><span class="tool-icon">◇</span><span class="tag" :class="t.risk">{{risks[t.risk]}}</span></div><h3>{{t.title}}</h3><p>{{t.description}}</p><footer>{{t.category}}<code>{{t.method}}</code></footer></button></div></section><section class="panel tool-inspector"><div v-if="!selectedTool" class="empty tall">选择一个工具，查看完整参数与响应契约。</div><template v-else><div class="panel-title"><h2>{{selectedTool.title}}</h2><span class="tag" :class="selectedTool.risk">{{risks[selectedTool.risk]}}</span></div><code class="endpoint">{{selectedTool.method}} {{selectedTool.path}}</code><div class="tabs"><button :class="{selected:schemaTab==='purpose'}" @click="schemaTab='purpose'">作用与边界</button><button :class="{selected:schemaTab==='input'}" @click="schemaTab='input'">入参 Schema</button><button :class="{selected:schemaTab==='response'}" @click="schemaTab='response'">Response</button></div><p v-if="schemaTab==='purpose'" class="tool-purpose">{{selectedTool.description}}</p><pre v-else class="schema">{{pretty(schemaTab==='input'?selectedTool.input_schema:selectedTool.response_schema)}}</pre><h3>调用参数 JSON</h3><textarea v-model="toolArgs" rows="10" class="code-input" spellcheck="false"></textarea><p v-if="selectedTool.risk==='sensitive'" class="warning-text">提交仅创建待确认请求，不会立即写入。发起者不能自审。</p><button class="primary full" :disabled="busy||!selectedTool.available||selectedTool.risk==='auth'||user.role==='reviewer'" @click="invokeTool">{{selectedTool.available?(selectedTool.risk==='read'?'执行只读查询':(selectedTool.risk==='sensitive'?'提交人工确认':'直接执行')):'文档明确未开放'}}</button><small v-if="selectedTool.risk==='auth'" class="muted">此工具由登录页面专用，禁止交给模型。</small></template></section></div></template>

  <template v-if="page==='workflows'"><div class="workflow-grid"><section><div class="panel-title"><h2>工作流模板</h2><label class="secondary import-label">导入 JSON<input type="file" accept="application/json,.json" hidden @change="importWorkflow"></label></div><article v-for="w in workflows" :key="w.id" class="panel workflow-card"><div class="panel-title"><h3>{{w.name}}</h3><span class="tag blue">v{{w.version}}</span></div><p>{{w.description}}</p><div class="step-list"><span v-for="(step,i) in w.steps" :key="i">{{i+1}}. {{tools.find(t=>t.name===step.tool)?.title||step.tool}}</span></div><div class="button-row"><button class="secondary" @click="exportWorkflow(w)">导出定义</button><button class="primary" @click="chooseWorkflow(w)">配置执行</button></div></article></section><section><div v-if="selectedWorkflow" class="panel"><h2>{{selectedWorkflow.name}}</h2><p class="muted">参数值占位保持原始类型，不支持任意表达式或脚本。</p><textarea v-model="parameters" rows="8" class="code-input"></textarea><button class="primary full" :disabled="busy||user.role==='reviewer'" @click="executeWorkflow">执行工作流</button></div><div class="panel"><h2>沉淀新流程</h2><p class="muted">JSON 定义驱动执行。仅允许白名单工具、最多20步；仅审批/处罚决定进入人工闸门，其余写操作直接执行并留痕。</p><textarea v-model="workflowEditor" rows="16" class="code-input"></textarea><button class="secondary full" :disabled="busy||user.role==='reviewer'" @click="createWorkflow">保存可复制工作流</button></div></section></div></template>

  <template v-if="page==='approvals'"><div class="alert warning">人工核验工具目的、操作对象、完整参数及证据。审批与处罚等敏感决定不能由模型自动作出；默认禁止发起者自审。</div><section v-if="!approvals.length" class="panel empty tall">暂无待人工确认请求。</section><section v-for="a in approvals" :key="a.id" class="panel approval-card"><div class="panel-title"><h2>{{tools.find(t=>t.name===a.tool_name)?.title||a.tool_name}}</h2><span class="tag sensitive">暂停执行 · 待核验</span></div><p>{{a.reason}}</p><div class="run-meta">请求时间 {{date(a.requested_at)}} · 任务 {{a.run_id}}</div><pre class="payload">{{pretty(a.arguments)}}</pre><code class="hash-block">确认载荷 SHA256：{{a.payload_hash}}</code><button class="text-button" @click="selectRun({id:a.run_id} as Run);page='runs'">查看完整执行证据 →</button><label>人工核验意见<textarea v-model="reviewNotes[a.id]" rows="2" placeholder="记录核验依据与人工决定"></textarea></label><div class="button-row"><span v-if="!canReview" class="muted">当前角色不可审核</span><span v-else-if="a.owner_id===user.id" class="muted">发起者不能自审，请独立审核员确认</span><template v-else><button class="secondary reject-button" :disabled="busy" @click="decide(a,'reject')">拒绝执行</button><button class="primary" :disabled="busy" @click="decide(a,'approve')">已人工核验，确认执行</button></template></div></section></template>

  <template v-if="page==='audit'"><section class="panel"><div class="panel-title"><h2>审计链完整性</h2><button class="primary" :disabled="busy" @click="checkAudit">重新校验</button></div><div v-if="audit" class="audit-status" :class="{invalid:!audit.valid}"><span>{{audit.valid?'✓':'!'}}</span><div><h2>{{audit.valid?'哈希链校验通过':'审计链校验失败'}}</h2><p>{{audit.event_count}} 条事件已核验</p></div></div><code v-if="audit" class="hash-block">当前链头 {{audit.head}}</code><div class="audit-notes"><h3>可溯源的证据边界</h3><ul><li>公开计划、工具参数哈希、工具响应、错误、人工决定、最终结果均关联任务 ID。</li><li>平台 Token 和原始证据在服务器加密保存；展示和导出的日志经过脱敏。</li><li>持久化 LangGraph 断点支持服务中断恢复，写入不确定时禁止重发。</li><li>哈希链用于检测变化，不等同于抵御数据库管理员篡改；生产应异地保存链头与备份。</li><li>图片识别记录图片哈希、ROI、模型和结论，不记录模型隐藏思维链或图片二进制内容。</li></ul></div></section><section class="panel"><h2>连接与部署状态</h2><div class="connection-table"><div><span>上游食安平台</span><code>{{health.upstream}}</code></div><div><span>模型标识</span><code>{{health.model}}</code></div><div><span>应用技术栈</span><code>FastAPI · Vue 3 · LangGraph · SQLite</code></div><div><span>模型状态</span><strong>{{health.features.llm_configured?'已配置（真实调用仍需网络可达）':'尚未配置 API Key'}}</strong></div><div><span>图片视觉识别</span><strong>{{health.features.vision_enabled?'已启用：防鼠板、清洁区门':'未配置 DeepSeek API Key'}}</strong></div></div></section></template>
  <footer class="main-footer"><span>上游 {{health.upstream}} · 本应用与上游独立部署</span><span>全流程留痕 / 人工确认 / 工作流可复制</span></footer>
  </main></div>
</div>
</template>
