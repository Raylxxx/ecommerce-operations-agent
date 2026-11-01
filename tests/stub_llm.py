"""本地模拟的 OpenAI 兼容接口，用来在没有可用 Key 时走通页面流程。回答内容是固定的，不代表模型效果。

python -m uvicorn tests.stub_llm:app --port 8001
"""
import json
import time

from fastapi import FastAPI, Request

app = FastAPI()


def reply(message):
    return {"id": "stub", "object": "chat.completion", "created": int(time.time()), "model": "stub",
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


@app.post("/chat/completions")
async def chat(request: Request):
    body = await request.json()
    messages, tools = body["messages"], body.get("tools") or []
    names = [t["function"]["name"] for t in tools]
    last = messages[-1]
    if not tools:
        return reply({"role": "assistant", "content": "（模拟回答）活动价不得高于报名前 30 天的最低成交价 [1]。"})
    if last["role"] == "tool":
        return reply({"role": "assistant", "content":
                      f"## 结论（模拟回答）\n\n已调用工具，结果摘要：\n\n- **长度**：{len(last['content'])} 字符\n\n"
                      "| 项目 | 说明 |\n|---|---|\n| 下一步 | 人工确认 |"})
    if "propose_price" in names and "提交" in last["content"]:
        name, args = "propose_price", {"sku": "KIT-001", "new_price": 339, "reason": "模拟：库存周转正常，略低于竞品"}
    elif "propose_price" in names:
        name, args = "dynamic_price", {"sku": "EAR-001"}
    elif "recommend_candidates" in names:
        name, args = "recommend_candidates", {"top_n": 3}
    elif "创建" in last["content"]:
        name, args = "create_ab_test", {"name": "防晒衣标题测试", "variant_a": "冰感防晒外套", "variant_b": "轻薄透气防晒外套", "sku": "OUT-002"}
    else:
        name, args = "audience_analysis", {"sku": "PET-001"}
    return reply({"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]})
