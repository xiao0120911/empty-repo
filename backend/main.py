"""v2 统一入口（端口8000）：
1. /api 前缀下的完整监管智能体 REST API——登录、智能任务、接口工具、可复制工作流、人工确认、审计与溯源；
2. /api/chat 参考内核聊天端点——agent/tools.py 的 @tool 函数 + agent/graph.py 的 ToolNode（tool_node）图。
前端 vite 开发代理统一指向 /api。
"""
import os
import uuid
import uvicorn
from pydantic import BaseModel
from langchain_core.messages import HumanMessage
from app.config import Settings
from app.main import create_app
from agent.graph import app_graph

# 前端约定 /api 前缀；本地开发走 http，Secure Cookie 默认关闭，可用环境变量覆盖。
# 测试仍使用 app.main.create_app 的默认 /agent-api 前缀，不受本入口影响。
settings=Settings(api_prefix='/api',
                  cookie_secure=os.environ.get('COOKIE_SECURE','false').strip().lower() in ('true','1','yes'),
                  allowed_origins=os.environ.get('ALLOWED_ORIGINS','http://127.0.0.1:5173,http://localhost:5173,https://localhost'))
app=create_app(settings)

class ChatRequest(BaseModel):
    message: str
    thread_id: str = str(uuid.uuid4())

class ChatResponse(BaseModel):
    response: str
    thread_id: str

# In-memory thread storage for simplicity
threads = {}

@app.post("/api/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    thread_id = request.thread_id

    if thread_id not in threads:
        threads[thread_id] = {"messages": []}

    state = threads[thread_id]

    # Add human message
    state["messages"].append(HumanMessage(content=request.message))

    # Run the graph
    result = app_graph.invoke(state)

    # Save the updated state
    threads[thread_id] = result

    # The last message is from AI
    ai_response = result["messages"][-1].content

    return ChatResponse(
        response=ai_response,
        thread_id=thread_id
    )

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000)
