"""本地 Web 前端入口。

这个模块现在同时支持两种聊天路径：

- 标准 LangChain Agent 模式：Web -> JobHuntingAgent -> Tools -> JobHuntingApp
- 本地规则兜底模式：Web -> JobHuntingApp.ingest_conversation_message

之所以保留兜底，是为了在 `.env` 未配置或测试场景下，项目仍然能离线运行。
一旦用户开启“使用 LangChain Agent”，主流程就会走标准 Agent 结构。
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import JobHuntingAgent
from .app import JobHuntingApp
from .config import (
    load_embedding_settings,
    load_llm_settings,
    masked_embedding_settings,
    masked_llm_settings,
)
from .models import CandidateProfileInput
from .rag import EmbeddingRequestError


STATIC_DIR = Path(__file__).with_name("web_static")


class ProfilePayload(BaseModel):
    """网页创建候选人档案时提交的数据。"""

    name: str
    status: str = "待补充"
    education: str = "待补充"
    experience_years: float = 0
    skills: dict[str, str] = Field(default_factory=dict)
    preferred_cities: list[str] = Field(default_factory=list)
    salary_floor_k: int | None = None
    expected_salary_k: int | None = None
    target_directions: list[str] = Field(default_factory=list)
    unacceptable: list[str] = Field(default_factory=list)


class ChatPayload(BaseModel):
    """网页聊天输入。

    为了兼容已有前端字段名，这里沿用 `use_env_llm`。现在它的语义是：

    - `true`：走标准 LangChain Agent 主流程。
    - `false`：走本地规则兜底流程。
    """

    candidate_id: int
    message: str
    use_env_llm: bool = False
    auto_rag: bool = True
    session_id: str | None = None


class JobPayload(BaseModel):
    """网页导入职位文本时提交的数据。"""

    raw_text: str
    source_url: str | None = None


def create_web_app(
    db_path: str | Path = "data/job_agent.db",
    env_file: str | Path = ".env",
    rag_dir: str | Path = "data/chroma",
    chat_agent: JobHuntingAgent | None = None,
) -> FastAPI:
    """创建本地 FastAPI 应用。

    这里显式保留数据库路径、`.env` 路径、RAG 目录和可注入 Agent，目的是：

    - 生产使用时，Web 层通过 `JobHuntingAgent` 或 `JobHuntingApp` 访问业务能力；
    - 测试时，可以安全地注入临时 SQLite、临时 Chroma 和假模型；
    - Web 层自己不直接碰 SQLite 连接、RAG 向量库细节或厂商 SDK。
    """

    backend = JobHuntingApp(db_path, env_file)
    backend.initialize()
    env_path = Path(env_file)
    rag_path = Path(rag_dir)

    agent_error: str | None = None
    if chat_agent is None:
        try:
            chat_agent = JobHuntingAgent(backend, env_path=env_path, rag_dir=rag_path)
        except ValueError as error:
            # `.env` 没配好时，网页仍然可以以本地规则模式工作。
            agent_error = str(error)

    web_app = FastAPI(title="Job Hunting Agent Web", version="0.1.0")
    web_app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @web_app.get("/")
    def home() -> FileResponse:
        """返回单页 Web 前端。"""

        return FileResponse(STATIC_DIR / "index.html")

    @web_app.get("/api/health")
    def health() -> dict[str, object]:
        """返回本地服务状态和脱敏模型配置。"""

        try:
            llm_config: dict[str, object] = masked_llm_settings(load_llm_settings(env_path))
        except ValueError as error:
            llm_config = {"configured": False, "error": str(error)}
        else:
            llm_config["configured"] = True
        try:
            embedding_config = masked_embedding_settings(load_embedding_settings(env_path))
        except ValueError as error:
            embedding_config = {"configured": False, "error": str(error)}
        return {
            "status": "ok",
            "db_path": str(Path(db_path)),
            "rag_dir": str(rag_path),
            "llm": llm_config,
            "embedding": embedding_config,
            "agent": {
                "configured": chat_agent is not None,
                "error": agent_error,
            },
        }

    @web_app.get("/api/profiles")
    def list_profiles() -> dict[str, object]:
        """列出候选人档案，供左侧栏选择。"""

        return {"profiles": [asdict(profile) for profile in backend.list_candidate_profiles()]}

    @web_app.post("/api/profiles")
    def create_profile(payload: ProfilePayload) -> dict[str, object]:
        """创建候选人档案。"""

        if not payload.name.strip():
            raise HTTPException(status_code=400, detail="候选人姓名不能为空。")
        candidate_id = backend.save_candidate_profile(
            CandidateProfileInput(
                name=payload.name.strip(),
                status=payload.status.strip() or "待补充",
                education=payload.education.strip() or "待补充",
                experience_years=payload.experience_years,
                skills=clean_string_dict(payload.skills),
                preferred_cities=clean_string_list(payload.preferred_cities),
                salary_floor_k=payload.salary_floor_k,
                expected_salary_k=payload.expected_salary_k,
                target_directions=clean_string_list(payload.target_directions),
                unacceptable=clean_string_list(payload.unacceptable),
            )
        )
        return {"candidate_id": candidate_id, "profile": asdict(backend.get_candidate_profile(candidate_id))}

    @web_app.get("/api/profiles/{candidate_id}")
    def get_profile(candidate_id: int) -> dict[str, object]:
        """读取某个候选人档案。"""

        return {"profile": asdict(get_profile_or_404(backend, candidate_id))}

    @web_app.post("/api/chat")
    def chat(payload: ChatPayload) -> dict[str, object]:
        """处理网页聊天消息。

        - 开启 Agent 时：执行标准 LangChain Agent 对话。
        - 关闭 Agent 时：回退到原有“对话式自动入库”规则链路。
        """

        get_profile_or_404(backend, payload.candidate_id)
        if not payload.message.strip():
            raise HTTPException(status_code=400, detail="消息不能为空。")

        if payload.use_env_llm:
            if chat_agent is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"LangChain Agent 未就绪：{agent_error or '请检查 .env 配置'}",
                )
            try:
                result = chat_agent.chat(
                    payload.message.strip(),
                    candidate_id=payload.candidate_id,
                    session_id=payload.session_id or f"web-candidate-{payload.candidate_id}",
                    use_tool_llm=True,
                    auto_rag=payload.auto_rag,
                )
            except EmbeddingRequestError as error:
                raise HTTPException(status_code=502, detail=str(error)) from error
            except Exception as error:  # noqa: BLE001 - 对 Web 层统一转成可读错误。
                raise HTTPException(status_code=502, detail=str(error)) from error
            return {
                "mode": result.mode,
                "reply": result.reply,
                "used_tools": result.used_tools,
                "tool_outputs": result.tool_outputs,
                "profile": asdict(backend.get_candidate_profile(payload.candidate_id)),
            }

        try:
            result = backend.ingest_conversation_message(
                payload.candidate_id,
                payload.message.strip(),
                llm_client=None,
                rag_persist_directory=rag_path,
                auto_rebuild_rag=payload.auto_rag,
            )
        except EmbeddingRequestError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return {
            "mode": "rule_based_ingestion",
            "reply": result.reply,
            "used_tools": ["ingest_conversation_message"],
            "tool_outputs": [{"tool_name": "ingest_conversation_message", "data": asdict(result)}],
            "result": asdict(result),
            "profile": asdict(backend.get_candidate_profile(payload.candidate_id)),
        }

    @web_app.post("/api/jobs")
    def import_job(payload: JobPayload) -> dict[str, object]:
        """保存候选人从 BOSS 页面主动复制回来的职位文本。"""

        if not payload.raw_text.strip():
            raise HTTPException(status_code=400, detail="职位文本不能为空。")
        job = backend.import_job_text(payload.raw_text, payload.source_url)
        return {"job": asdict(job)}

    @web_app.get("/api/jobs")
    def list_jobs() -> dict[str, object]:
        """列出已经导入的职位。"""

        return {"jobs": [asdict(job) for job in backend.list_jobs()]}

    @web_app.get("/api/matches/{candidate_id}")
    def list_matches(candidate_id: int) -> dict[str, object]:
        """返回候选人与所有本地职位的匹配结果。"""

        get_profile_or_404(backend, candidate_id)
        jobs_by_id = {job.id: job for job in backend.list_jobs()}
        matches = backend.match_all_jobs(candidate_id)
        return {
            "candidate_id": candidate_id,
            "matches": [
                {"job": asdict(jobs_by_id[match.job_id]), "match": asdict(match)}
                for match in matches
            ],
        }

    @web_app.get("/api/rag/search")
    def search_rag(query: str = Query(...), top_k: int = 5) -> dict[str, object]:
        """检索本地 RAG 证据片段。"""

        try:
            results = backend.search_rag(query, rag_path, top_k)
        except EmbeddingRequestError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return {"query": query, "results": [asdict(result) for result in results]}

    return web_app


def get_profile_or_404(backend: JobHuntingApp, candidate_id: int):
    """读取候选人档案；不存在时转换为 Web 友好的 404。"""

    try:
        return backend.get_candidate_profile(candidate_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"候选人不存在：{candidate_id}") from error


def clean_string_list(values: list[str]) -> list[str]:
    """清理网页表单中的字符串列表。"""

    return [str(value).strip() for value in values if str(value).strip()]


def clean_string_dict(values: dict[str, str]) -> dict[str, str]:
    """清理网页表单中的技能字典。"""

    return {
        str(key).strip(): str(value).strip() or "待确认"
        for key, value in values.items()
        if str(key).strip()
    }


def main(argv: list[str] | None = None) -> None:
    """从命令行启动本地 Web 服务。"""

    parser = argparse.ArgumentParser(prog="job-agent-web")
    parser.add_argument("--db", default="data/job_agent.db")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--rag-dir", default="data/chroma")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run(
        create_web_app(args.db, args.env_file, args.rag_dir),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
