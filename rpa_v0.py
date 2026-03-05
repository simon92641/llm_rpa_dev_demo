import os, json, uuid, datetime
from typing import TypedDict, List, Dict, Any

from dotenv import load_dotenv
from langgraph.graph import StateGraph, START, END
from playwright.sync_api import sync_playwright
from google import genai

load_dotenv()  # 从 .env 加载到环境变量  :contentReference[oaicite:10]{index=10}

class State(TypedDict, total=False):
    url: str
    run_id: str
    artifacts_dir: str
    items: List[Dict[str, Any]]
    page_title: str
    report_md: str
    error: str

def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def audit_append(artifacts_dir: str, event: Dict[str, Any]) -> None:
    os.makedirs(artifacts_dir, exist_ok=True)
    with open(os.path.join(artifacts_dir, "audit.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

def init_run(state: State) -> Dict[str, Any]:
    run_id = uuid.uuid4().hex[:10]
    out_dir = os.getenv("OUT_DIR", "artifacts")
    artifacts_dir = os.path.join(os.getcwd(), out_dir, run_id)
    url = state.get("url") or os.getenv("TARGET_URL")
    audit_append(artifacts_dir, {"ts": now_iso(), "node": "init_run", "status": "ok", "url": url})
    return {"run_id": run_id, "artifacts_dir": artifacts_dir, "url": url}

def rpa_scrape(state: State) -> Dict[str, Any]:
    artifacts_dir = state["artifacts_dir"]
    url = state["url"]
    screenshot_path = os.path.join(artifacts_dir, "page.png")
    trace_path = os.path.join(artifacts_dir, "trace.zip")
    items_path = os.path.join(artifacts_dir, "items.json")

    audit_append(artifacts_dir, {"ts": now_iso(), "node": "rpa_scrape", "status": "start", "url": url})

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            # Playwright tracing start/stop（证据链） :contentReference[oaicite:11]{index=11}
            context.tracing.start(screenshots=True, snapshots=True)

            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=60_000)
            title = page.title()
            page.screenshot(path=screenshot_path, full_page=True)

            data = []
            for q in page.locator(".quote").all():
                data.append({
                    "text": q.locator(".text").inner_text(),
                    "author": q.locator(".author").inner_text(),
                    "tags": q.locator(".tag").all_inner_texts(),
                })

            context.tracing.stop(path=trace_path)
            browser.close()

        with open(items_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        audit_append(artifacts_dir, {"ts": now_iso(), "node": "rpa_scrape", "status": "ok", "count": len(data)})
        return {"items": data, "page_title": title}
    except Exception as e:
        err = f"RPA failed: {e!r}"
        audit_append(artifacts_dir, {"ts": now_iso(), "node": "rpa_scrape", "status": "error", "error": err})
        return {"error": err}

def gemini_summarize(state: State) -> Dict[str, Any]:
    artifacts_dir = state["artifacts_dir"]
    audit_append(artifacts_dir, {"ts": now_iso(), "node": "gemini_summarize", "status": "start"})

    api_key = os.getenv("GEMINI_API_KEY")
    model = os.getenv("GEMINI_MODEL", "gemini-3.1-pro-preview")
    client = genai.Client(api_key=api_key)  # Google GenAI SDK 推荐用法 :contentReference[oaicite:12]{index=12}

    if state.get("error"):
        prompt = f"Playwright 报错：{state['error']}\n请给最多8条排查与修复建议（中文）。"
        resp = client.models.generate_content(model=model, contents=prompt)  # :contentReference[oaicite:13]{index=13}
        return {"report_md": f"# 运行失败\n\n{state['error']}\n\n## 排查建议\n\n{resp.text}\n"}

    sample = state["items"][:20]
    prompt = (
        "你是业务分析助手。请生成中文 Markdown 报告，包含：\n"
        "1) 页面标题 2) 条目数量 3) 高频标签/主题 4) 作者Top 5\n"
        "5) RPA改进建议（selector、等待、失败重试、trace/screenshot）\n\n"
        f"输入JSON：{json.dumps({'page_title': state.get('page_title'), 'count': len(state['items']), 'sample': sample}, ensure_ascii=False)}"
    )
    resp = client.models.generate_content(model=model, contents=prompt)  # :contentReference[oaicite:14]{index=14}
    audit_append(artifacts_dir, {"ts": now_iso(), "node": "gemini_summarize", "status": "ok"})
    return {"report_md": resp.text}

def write_report(state: State) -> Dict[str, Any]:
    artifacts_dir = state["artifacts_dir"]
    report_path = os.path.join(artifacts_dir, "report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(state.get("report_md", "# Empty\n"))
    audit_append(artifacts_dir, {"ts": now_iso(), "node": "write_report", "status": "ok", "report_path": report_path})
    return {"report_path": report_path}

def build_graph():
    g = StateGraph(State)
    g.add_node("init_run", init_run)
    g.add_node("rpa_scrape", rpa_scrape)
    g.add_node("gemini_summarize", gemini_summarize)
    g.add_node("write_report", write_report)

    g.add_edge(START, "init_run")
    g.add_edge("init_run", "rpa_scrape")
    g.add_edge("rpa_scrape", "gemini_summarize")
    g.add_edge("gemini_summarize", "write_report")
    g.add_edge("write_report", END)
    return g.compile()

if __name__ == "__main__":
    result = build_graph().invoke({})
    print("Done ✅")
    print("Artifacts dir:", result.get("artifacts_dir"))