"""Private deterministic OpenAI-compatible server for isolated Docker acceptance only."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

COUNTS = {}
LOCK = Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        with LOCK:
            body = json.dumps(COUNTS).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = data["messages"]
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        label = "update" if "acceptance-update" in user else "failure" if "acceptance-failure" in user else "other"
        with LOCK:
            COUNTS[label] = COUNTS.get(label, 0) + 1
            call_number = COUNTS[label]
        if label == "failure":
            body = b'{"error":{"message":"private-model-error","type":"server_error"}}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        tool = label == "update" and messages[-1]["role"] != "tool"
        if tool:
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": f"acceptance-tool-{call_number}", "type": "function",
                "function": {"name": "ingest_candidate_message", "arguments": json.dumps({
                    "message": "我的首选城市改为上海", "auto_rag": False}, ensure_ascii=False)}}]}
        else:
            delta = {"role": "assistant", "content": "Completed."}
        frames = [
            {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 60, "completion_tokens": 20, "total_tokens": 80}},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for frame in frames:
            frame.update(id="chatcmpl-acceptance", object="chat.completion.chunk", created=1, model="acceptance-chat")
            self.wfile.write(("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()
