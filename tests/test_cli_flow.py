"""CLI 行为测试。

CLI 是当前教学 MVP 最直接的使用入口，所以这里用真实命令参数跑通
“创建档案 -> 导入职位 -> 批量匹配”的最小流程。
"""

import json

from job_hunting_agent.cli import main


def test_cli_can_create_profile_import_job_and_match_all(tmp_path, capsys):
    """命令行可以创建候选人档案、导入职位，并输出批量匹配结果。"""

    db_path = tmp_path / "cli.db"
    profile_file = tmp_path / "profile.json"
    job_file = tmp_path / "job.txt"
    profile_file.write_text(
        json.dumps(
            {
                "name": "小林",
                "status": "离职",
                "education": "本科",
                "experience_years": 1.0,
                "skills": {"Python": "项目使用", "FastAPI": "项目使用"},
                "preferred_cities": ["杭州"],
                "salary_floor_k": 10,
                "expected_salary_k": 15,
                "target_directions": ["Python 后端开发"],
                "unacceptable": ["外包"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    job_file.write_text(
        """
        Python 后端开发工程师
        15-20K
        杭州
        1-3年
        本科
        职位描述：负责 Python 和 FastAPI 后端开发。
        """,
        encoding="utf-8",
    )

    main(["--db", str(db_path), "create-profile", "--from-json", str(profile_file)])
    created_profile = json.loads(capsys.readouterr().out)
    candidate_id = created_profile["candidate_id"]

    main(["--db", str(db_path), "import-job", str(job_file)])
    imported_job = json.loads(capsys.readouterr().out)

    main(["--db", str(db_path), "match-all", str(candidate_id)])
    match_output = json.loads(capsys.readouterr().out)

    assert imported_job["title"] == "Python 后端开发工程师"
    assert match_output["candidate_id"] == candidate_id
    assert match_output["matches"][0]["job"]["id"] == imported_job["id"]
    assert match_output["matches"][0]["match"]["tier"] in {"强推荐", "可投递"}


def test_cli_can_create_rule_based_resume_draft(tmp_path, capsys):
    """命令行可以生成不依赖真实 LLM 的证据约束简历草稿。"""

    db_path = tmp_path / "cli.db"
    profile_file = tmp_path / "profile.json"
    job_file = tmp_path / "job.txt"
    profile_file.write_text(
        json.dumps(
            {
                "name": "小林",
                "status": "离职",
                "education": "本科",
                "experience_years": 1.0,
                "skills": {"Python": "项目使用", "FastAPI": "项目使用"},
                "preferred_cities": ["杭州"],
                "salary_floor_k": 10,
                "expected_salary_k": 15,
                "target_directions": ["Python 后端开发"],
                "unacceptable": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    job_file.write_text(
        """
        Python 后端开发工程师
        15-20K
        杭州
        1-3年
        本科
        职位描述：负责 Python、FastAPI 和 Kubernetes 平台开发。
        """,
        encoding="utf-8",
    )

    main(["--db", str(db_path), "create-profile", "--from-json", str(profile_file)])
    candidate_id = json.loads(capsys.readouterr().out)["candidate_id"]
    main(["--db", str(db_path), "import-job", str(job_file)])
    job_id = json.loads(capsys.readouterr().out)["id"]

    main(["--db", str(db_path), "draft-resume", str(candidate_id), str(job_id)])
    draft_output = json.loads(capsys.readouterr().out)

    assert draft_output["candidate_id"] == candidate_id
    assert draft_output["job_id"] == job_id
    assert draft_output["version"] == 1
    assert "Python" in draft_output["draft"]["content"]
    assert "FastAPI" in draft_output["draft"]["content"]
    assert "Kubernetes" not in draft_output["draft"]["content"]
    assert any("未确认技能：Kubernetes" in risk for risk in draft_output["draft"]["authenticity_risks"])


def test_cli_can_show_masked_llm_config(tmp_path, capsys):
    """命令行可以读取 `.env` 并只展示脱敏后的 LLM 配置。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_LLM_PROVIDER=deepseek",
                "JOB_AGENT_LLM_MODEL=deepseek-v4-pro",
                "JOB_AGENT_LLM_API_KEY=sk-secret",
                "JOB_AGENT_LLM_BASE_URL=https://api.deepseek.com",
            ]
        ),
        encoding="utf-8",
    )

    main(["--env-file", str(env_file), "llm-config"])
    output_text = capsys.readouterr().out
    output = json.loads(output_text)

    assert output["provider"] == "deepseek"
    assert output["model"] == "deepseek-v4-pro"
    assert output["api_key_set"] is True
    assert "sk-secret" not in output_text


def test_cli_can_rebuild_and_search_rag_index(tmp_path, capsys):
    """命令行可以重建本地 RAG 索引并检索来源证据。"""

    db_path = tmp_path / "cli.db"
    rag_dir = tmp_path / "chroma"
    profile_file = tmp_path / "profile.json"
    job_file = tmp_path / "job.txt"
    profile_file.write_text(
        json.dumps(
            {
                "name": "小林",
                "status": "离职",
                "education": "本科",
                "experience_years": 1.0,
                "skills": {"Python": "项目使用", "FastAPI": "项目使用"},
                "preferred_cities": ["杭州"],
                "salary_floor_k": 10,
                "expected_salary_k": 15,
                "target_directions": ["Python 后端开发"],
                "unacceptable": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    job_file.write_text(
        """
        Python 后端开发工程师
        15-20K
        杭州
        1-3年
        本科
        职位描述：负责 Python、FastAPI 和职位文本处理。
        """,
        encoding="utf-8",
    )

    main(["--db", str(db_path), "create-profile", "--from-json", str(profile_file)])
    capsys.readouterr()
    main(["--db", str(db_path), "import-job", str(job_file)])
    capsys.readouterr()
    main(["--db", str(db_path), "--rag-dir", str(rag_dir), "rag-rebuild"])
    rebuild_output = json.loads(capsys.readouterr().out)
    main(["--db", str(db_path), "--rag-dir", str(rag_dir), "rag-search", "FastAPI 职位文本"])
    search_output = json.loads(capsys.readouterr().out)

    assert rebuild_output["chunk_count"] >= 1
    assert search_output["query"] == "FastAPI 职位文本"
    assert search_output["results"]
    assert any("FastAPI" in result["content"] for result in search_output["results"])
