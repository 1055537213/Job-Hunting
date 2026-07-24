"""本地 Web 前端入口。

这个模块把现有 `JobHuntingApp` 暴露成一个本地 FastAPI 应用，并提供静态网页。
Web 层只负责请求/响应和页面资源，不直接绕过应用服务操作 SQLite、RAG 或 LLM。
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .app import JobHuntingApp
from .config import load_llm_settings, masked_llm_settings
from .llm import LLMClient, LLMRequestError, build_llm_client
from .models import CandidateProfileInput


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
    """网页聊天输入。"""

    candidate_id: int
    message: str
    use_env_llm: bool = False
    auto_rag: bool = True


class JobPayload(BaseModel):
    """网页导入职位文本时提交的数据。"""

    raw_text: str
    source_url: str | None = None


def create_web_app(
    db_path: str | Path = "data/job_agent.db",
    env_file: str | Path = ".env",
    rag_dir: str | Path = "data/chroma",
) -> FastAPI:
    """创建本地 FastAPI 应用。

    参数保留为显式路径，方便测试使用临时数据库，也方便用户后续把 Web 服务指向
    不同的数据目录。
    """

    backend = JobHuntingApp(db_path)
    backend.initialize()
    env_path = Path(env_file)
    rag_path = Path(rag_dir)

    web_app = FastAPI(title="Job Hunting Agent Web", version="0.1.0")
    web_app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @web_app.get("/")
    def home() -> FileResponse:
        """返回单页 Web 前端。"""

        return FileResponse(STATIC_DIR / "index.html")

    @web_app.get("/api/health")
    def health() -> dict[str, object]:
        """返回本地服务状态和脱敏 LLM 配置。"""

        try:
            llm_config: dict[str, object] = masked_llm_settings(load_llm_settings(env_path))
        except ValueError as error:
            llm_config = {"configured": False, "error": str(error)}
        else:
            llm_config["configured"] = True
        return {
            "status": "ok",
            "db_path": str(Path(db_path)),
            "rag_dir": str(rag_path),
            "llm": llm_config,
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
        """处理网页聊天消息，并执行对话式自动入库。"""

        get_profile_or_404(backend, payload.candidate_id)
        if not payload.message.strip():
            raise HTTPException(status_code=400, detail="消息不能为空。")
        llm_client = build_web_llm(payload.use_env_llm, env_path)
        try:
            result = backend.ingest_conversation_message(
                payload.candidate_id,
                payload.message.strip(),
                llm_client=llm_client,
                rag_persist_directory=rag_path,
                auto_rebuild_rag=payload.auto_rag,
            )
        except LLMRequestError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return {
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

        return {
            "query": query,
            "results": [asdict(result) for result in backend.search_rag(query, rag_path, top_k)],
        }

    return web_app


def get_profile_or_404(backend: JobHuntingApp, candidate_id: int):
    """读取候选人档案；不存在时转换为 Web 友好的 404。"""

    try:
        return backend.get_candidate_profile(candidate_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"候选人不存在：{candidate_id}") from error


def build_web_llm(use_env_llm: bool, env_file: Path) -> LLMClient | None:
    """根据网页开关决定是否使用 `.env` 中配置的真实模型。"""

    if not use_env_llm:
        return None
    try:
        return build_llm_client(load_llm_settings(env_file))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


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
