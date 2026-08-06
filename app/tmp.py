# JUST FOR HUMAN MATUAL TEST, AGENT MUST NOT READ

"""
"""
##
# Agent
##

# from langchain core messages, import humanMessage, system Message
from langchain_core.messages import HumanMessage, SystemMessage
# from langufuse decorators, import langfuse context, observe
from langfuse.decorators import langfuse_context, observe

# from app graph, import SYSTEM_PROMT, build_graph
from app.graph import SYSTEM_PROMPT, build_graph

# try except import langfuse callback's CallbackHandler
try:
    from langfuse.callback import CallbackHandler
except Exception:
    CallbackHandler = None

# declare _grapp node, _seed set(tr)
_graph = None
_seed: set[str]= set()

# fun get graph (): global _graph if none buidl graph else return
def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph

# func  _callbacks(session id str), if CallbackHandler none return [] , then try [CallbackHandler(session_id)] except return []
def _callbacks(session_id: str):
    if CallbackHandler is None:
        return []
    try:
        return [CallbackHandler(session_id=session_id)]
    except Exception:
        return []

# func _ ensure seeded(graph,thread_id str)None, if thread id in seed return, graph update_state({configuratble:{thread id}},{messages:[SystemMessage(content=SYSTEM_PROMPT)]}), _seeded.add(thread_id)
def _ensure_seeded(graph,thread_id: str) -> None:
    if thread_id in _seed:
        return
    graph.update_state(
        {"configurable":{"thread_id": thread_id}},
        {"messages":[SystemMessage(content=SYSTEM_PROMPT)]},
    )
    _seed.add(thread_id)

# func _ config(thread id str)dict , return {"configurable":{"threadId"},"callbacks":_callback(threadid),"recursion_limit":12}
def _config(thread_id: str) -> dict:
    return {
        "configurable":{"thread_id":thread_id},
        "call_backs": _callback(thread_id),
        "recursion_limit" 12,
    }

# func with @observe(name chat-trun-stream), stream_turn(textStr,threa_id), 
# langfuse context.update curent trace(session_id =thread_id), graph = get_graph(), _ensure_seeded(graph,threa_id)
# printed= "", for state in graph.stream({"message":[HumanMessage(context=text)]},config=_config(thread_id),stream_mode="values",):
# msg = last state[messaged], if msg.type == 'ai' and isinstacne(msg.content,str) and msg.contet: delta = msg.content[len(printted):] printed=msg.context yeild deltal

@observe(name="chat-turn-stream")
def stream_turn(text: str,thread_id: str):
    langfuse_context.update_current_trace(session_id=thread_id)
    graph = get_graph()
    _ensure_seeded(graph,threa_id)
    printed=""
    for state in graph.stream(
        {"message":[HumanMessage(context=text)]},
        "config":_config(thread_id),
        "stream_mode":"values"
    ):
        msg = state["messages"][-1]
        if msg.type == 'ai' and isinstance(msg.content,str) and msg.content:
            delta = msg.content[len(printed):]
            printed=msg.content
            yield delta

# @observe chat turn, fun answer(text,thread_id) str, langfuse context. update curret trace(session_id)
# get graph, _ensure seeded(graph,thread_id), resutl = graph.invkoke({message:[HumanceMes(text)]},config=_config) return result last message .content
@observe(name="chat-turn")
def anwser(text: str,thread_id: str):
    langfuse_context.update_current_trace(session=thread_id)
    graph= _get_graph()
    _ensure_seeded(graph,thread_id)
    result = graph.invoke(
        {"message":[HumanMessage(content=text)]},
        config=_config(thread_id)
        )
    return result["messages"][-1].content


# async astream_events_trun(text,thread_id): get graph, _ensure_seeded, trace=None, callbacks=[],
# if CallbackHandler is not none, try from langfuse import Langfuse, lf = Lf, trace=lf.trace(name="",sessesion_id="",input=text)
# callback =[CallbackHandler(trace_id=trace.id,session_id=thread_id)] except pas
# cfg = {"configuratble":{thread},"callbacks",recursion_limit:12}, final_anser=[]
# try/except aysnc for event in graph.astream_events({"messages":[HumanMessage(conetnt=text)]},config=cfg,version="v2")
# kind = event["event"], if kind == "on_chat_model_tream" chunk = event["data"]["chunk"] content=chunk.content, if isinstace(content,list): 
# content="".join(p.get("text","") if isinstance(p,dict) else str(p) for in content)
# if content: final_anwser.append(content) yield {"type":"token","content":content}
# elif kind == "on_tool_start": yield {"type":"tool_start","tool":event["name"],"args":event["data"].get("input",{})}
# elif kind == "on_tool_end": yield {"type":"tool_end","tool":event["name"]}
# except Exception as exe: if trace: trace.upate(output=f"ERROR exc",level="ERROR") yield {"type":"error","content":str(exc)}
# else: if trace: trace.update(output="".join(final_anser))
# finnally: if trace: try: from langfuse import Langufse Langfuse().flush() except pass 
# yield {"type":"done"}

async def astream_events_turn(text: str,thread_id: str):
    graph= get_graph()
    _ensure_seeded(graph,thread_id)
    trace = None
    callbacks=[]
    if CallbackHandler is not None:
        try:
            from langfuse import Langfuse
            lf = Langfuse()
            trace =lf.trace(name="trace_async-trun",session_id=thread_id,input=text)
            callbacks=[CallbackHandler(trace_id=trace.id,session_id=thread_id)]
        except Exception:
            pass
    cfg={"configurable":{"thread":thread_id},"callbacks":callbacks,"recursion_limit":12}
    try:
        async for event in graph.astream_events({"messages":[HumanMessage(content=text)]},config=cfg,version="v2")
            kind = event["event"]
            if kind == "on_chat_model_stream":
                chunk = event["data"]["chunk"]
                content= chunk.content 
                if isinstance(content,list):
                    content = "".join(p.get(text,"") if p isinstance(p,dict) else str(p) for p in content)
                if content:
                    final_anwser.append(content)
                yield {"type":"token","contetn":content}
            elif kind == "on_tool_start":
                yield {"type":"tool_start","tool":event["name"],"args":event["data"].get("input",{})}
            elif kind == "on_tool_end":
                yield {"type":"tool_end":"tool":event["name"]}
    except Exception exc:
        if trace:
            trace.update(output=f"Error {exe}",level="ERROR")
        yield {"type":"error","content":str(exe)}
    else:
        if trace:
            trace.update(output="".join(final_answer))
    finally: 
        if trace:
            try
                from langfuse import Langfuse
                Langfuse().flush()
            except Exception:
                pass
    yield {"type":"done"}

@asynccontextmanager
async def _langfuse_trace(name: str, session_id: str, input_text: str):
    """
    """
    trace = None
    callbacks: list = []
    if CallbacHandler is not None:
        try:
            from langfuse import Langfuse
            lf = Langfuse()
            trace=lf.trace(name="name",session_id=session_id,input= input_text)
            callbacks=[Callbackhandler(trace_id=trace.id,session_id=session_id,)]
        except Exception:
            pass
    try:
        yield trace, callbacks
    finally:
        if trace:
            try:
                from langfuse import Langfuse
                Langfuse().flush()
            except Exception:
                pass

async def _asteam_event_trun_ctx(text: str, thread_id: str):
    """
    """
    graph = get_graph()
    _ensure_seeded(graph,thread_id)

    final_anwser: list[str] =[]
    async with _langfuse_trace("chat-truern",thread_id,text) as (trace,callbacks):
        cfg={
            "configurable":{"thread_id":thread_id}
            "callbacks":callbacks,
            "recursion_limit":12,
        }

        try:
            async for event in graph.astream_events(
                {"message":[HumanMessage(content=text)]},
                config=cfg,
                version="v2",
            ):
                kind = event["event"] 
                if kind == "on_chat_model_stream":
                    chunk = event["data"]["chunk"]
                    content=chunk.content
                    if isinstance(content,list):
                        content=
                            "".join(
                                p.get("text","") if p isinstance(p,dict) else str(p)
                                for p in content
                            )
                    if content:
                        final_answer.append(content)
                        yield {"type":"token","content":content}               
                elif kind == "on_tool_start":
                    yeild {"type":"tool_start","tool":event["name"],"args":event["data"].get("input",{})}
                elif kind == "on_tool_end":
                    yield {"type":"tool_end","tool": event["name"]}
        except Exception as exc
            if trace:
                trace.update(output=f"errof {exc}",level="ERROR")
            yield {"type":"error","contnent":str(exe)}
            return
        
        if trace:
            trace.update(output="".join(final_answer))

    yield {"type":"done"}

# import json, import FastAPI , import StreamingResponse from fastapi.responses,
# import answer, astream_event_turn from app.agent
# import ChatRequest, ChatResponse, HealthResponse from app schemas
import json
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from app.agent import anwser, astream_event_turn
from app.schemas import ChatRequest,ChatResponse

# app fast api with title version
app = FastAPI(title="",version="123")

# app.get /helth, res_mo=Heal..
@app.get("/health",response_model=HealthResponse)
def heald()->HealResponse:
    return HealthReponse()

# app.post /chat, resp_mo=ChatR..,chat(req:ChatRq)->ChatRes 
# text = answer(req.msg,req.thread_id) return ChatRes(thread_id=req.treh,answer=tex)
@app.post("/chat",response_model=ChatRespone)
def chat(req: ChatRequest) -> ChatResponse:
    text=answer(req.msg,req.thread_id)
    return ChatResponse(thread_id=req.thread_id,answer=text)

# app.post chat/stream , chat_stream(chatRequest)StreamingREspone, 
# async def generate() async for (req.msg,req.thread_id) yield f data json.dumps(event)\n\n
# return SteamingResponse(generate(),media_type="text/event_stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.post("chat/stream",response_model=StreamingResponse)
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    async def generate():
        async for event in async_event_trun(req.msg,req.thread_id):
            yield f"data {json.dumps(even)}\n\n"

    return StreamingResponse(getnerate(),media_type="text/event_stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

# import asyncio, sys, uuid
# import langfuse_context from langfuse.decorators
# import astream_events_turn, stream_turn

import asyncio
import sys
import uuid

from langfuse.decorators import langfuse_context
from app.agent import astream_events_turn, stream_turn

# main()None, thread_id=str(uuid.uuid4()) print("local Rag ready type to quie") while True 
# try text = input("you> ").strip() ecept(EOFErro,KeyboardInterupt): break
# if text lower in {"exit","quite"}: break
# if not text: continue, print("bot> ",end="",flush=True)
# for delta in stream_trun(text,thread_id): print(delta) print(delta,end="",flush=True) print()
# try langfuse_context.flush() excep Exceptoin: pass

def main() -> None:
    thread_id = str(uuid.uuidv4())
    print("local ra gread")
    while True:
        try:
            text=input("you> ").strip()
        except(EOFError,KeyboardInterrupt):
            break
        if text.lower() in {"exit","quit"}:
            break
        if not text:
            continue
        print("bot> ",end="",flush=True)
        for delta in stream_trun(text,thread_id):
            print(delta,end="",flush=True)
        print()

    try:
        langfuse_context.flush()
    except Exception:
        pass
    print("Bye!")

    _DIM ="\033[2m]"
    _RESET = ""

async def _async_turn(text: str,thread_id:str)->None:
    print("bot> ",end="",flush=True)
    async for event in astream_event_turn(text,thread_id):
        t = event["type"]
        if t=="token":
            print(event["content"],end="",flush=Trye)
        elif t == "tool_start":
            args_str=", ".join(f"{k}={v!r}" for k,v in event.get("args",{}).items())
            print(f"\n{DIM} [ {event['tool']}({args_tr}){_REset}",flush=True)
        elif t=="tool_end":
            print()

async def async_main() -> None:
    """""""
    print()
    while True:
        try:
            text=input("you> ").strip()
        except (EOFError,KeyboardInterrupt):
            break
        if text.lower() in {"exit","quite"}:
            break
        if not text:
            continue
        await _aysnc_trun(text,thread_id)

    try:
        langfuse_context.flust()
    except Exception:
        pass
    print()

if __name__ == "__main__":
    if "--stream" in sys.argv:
        asyncio.run(async_main())
    else:
        main()