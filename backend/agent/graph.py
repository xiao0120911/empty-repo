from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage
from agent.state import AgentState
from agent.tools import AGENT_TOOLS
from core.config import settings

# Initialize DeepSeek v4 flash model
llm = ChatOpenAI(
    model="deepseek-v4-flash",
    api_key=settings.DEEPSEEK_API_KEY or "sk-dummy",
    base_url=settings.DEEPSEEK_BASE_URL,
    max_tokens=2048,
    temperature=0.1
)

# Bind tools to the model
model_with_tools = llm.bind_tools(AGENT_TOOLS)

system_prompt = """你是一个市场监管业务平台自主执行智能体。
你的主要职责是：
1. 理解用户的自然语言检查任务。
2. 规划执行步骤。
3. 调用平台工具进行非现场监管检查。
4. 评估结果，并在未达标时自行调整纠错。
5. 最终形成完整的检查报告。

请尽量使用中文进行回复。使用提供的工具来获取数据、进行视频分析以及处理预警事件。"""

def call_model(state: AgentState):
    messages = state['messages']
    if not messages or not isinstance(messages[0], SystemMessage):
        messages = [SystemMessage(content=system_prompt)] + messages
    response = model_with_tools.invoke(messages)
    return {"messages": [response]}

def should_continue(state: AgentState):
    messages = state['messages']
    last_message = messages[-1]
    # If there are tool calls, we continue to ToolNode
    if last_message.tool_calls:
        return "tools"
    # Otherwise, we end
    return END

# Define the graph
workflow = StateGraph(AgentState)

# Add nodes
workflow.add_node("agent", call_model)
tool_node = ToolNode(AGENT_TOOLS)
workflow.add_node("tools", tool_node)

# Add edges
workflow.set_entry_point("agent")
workflow.add_conditional_edges(
    "agent",
    should_continue,
    {
        "tools": "tools",
        END: END
    }
)
workflow.add_edge("tools", "agent")

# Compile graph
app_graph = workflow.compile()
