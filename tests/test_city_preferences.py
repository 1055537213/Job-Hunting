"""首选城市和其他可接受城市的端到端行为测试。"""

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.city_catalog import cities_in_text, nearby_cities
from job_hunting_agent.models import CandidateProfileInput


def build_profile(**overrides) -> CandidateProfileInput:
    """构造城市偏好测试使用的最小候选人档案。"""

    values = {
        "name": "城市偏好测试",
        "status": "待补充",
        "education": "本科",
        "experience_years": 1,
        "skills": {"Python": "项目使用"},
        "preferred_cities": ["杭州市"],
        "acceptable_cities": [],
        "salary_floor_k": None,
        "expected_salary_k": None,
        "target_directions": ["Python 后端"],
        "unacceptable": [],
    }
    values.update(overrides)
    return CandidateProfileInput(**values)


def test_city_catalog_recognizes_national_names_and_local_neighbours():
    """全国城市目录负责名称规范化，本地关系表负责保守解析邻近城市。"""

    assert cities_in_text("首选杭州市，上海市也可以") == ["杭州", "上海"]
    assert {"嘉兴", "湖州", "绍兴"} <= set(nearby_cities(["杭州市"]))
    # 未收录关系时不把同省所有城市冒充成邻近城市。
    assert nearby_cities(["拉萨"]) == []


def test_profile_stores_multiple_preferred_and_acceptable_cities_separately(tmp_path, account_id):
    """新建档案支持多个首选城市，并剔除两个分类中的重复城市。"""

    app = JobHuntingApp()
    app.initialize()
    candidate_id = app.save_candidate_profile(
        build_profile(
            preferred_cities=["杭州市", "上海市"],
            acceptable_cities=["苏州市", "上海市"],
        ),
        account_id=account_id,
    )

    profile = app.get_candidate_profile(candidate_id, account_id=account_id)

    assert profile.preferred_cities == ["杭州", "上海"]
    assert profile.acceptable_cities == ["苏州"]


def test_conversation_replaces_primary_adds_acceptable_and_can_clear_cities(tmp_path, account_id):
    """明确对话会覆盖首选城市、追加可接受城市，并支持城市不限清除。"""

    app = JobHuntingApp()
    app.initialize()
    candidate_id = app.save_candidate_profile(build_profile(), account_id=account_id)

    replaced = app.ingest_conversation_message(
        candidate_id,
        "我现在首选上海和苏州。",
        account_id=account_id,
    )
    after_replaced = app.get_candidate_profile(candidate_id, account_id=account_id)
    assert "preferred_cities" in replaced.saved_structured_fields
    assert after_replaced.preferred_cities == ["上海", "苏州"]

    expanded = app.ingest_conversation_message(
        candidate_id,
        "首选城市的邻近城市也可以。",
        account_id=account_id,
    )
    after_expanded = app.get_candidate_profile(candidate_id, account_id=account_id)
    assert "acceptable_cities" in expanded.saved_structured_fields
    assert {"嘉兴", "无锡"} <= set(after_expanded.acceptable_cities)
    assert not set(after_expanded.preferred_cities) & set(after_expanded.acceptable_cities)

    app.ingest_conversation_message(candidate_id, "南京和无锡也可以考虑。", account_id=account_id)
    after_explicit = app.get_candidate_profile(candidate_id, account_id=account_id)
    assert {"南京", "无锡"} <= set(after_explicit.acceptable_cities)

    cleared = app.ingest_conversation_message(
        candidate_id,
        "现在不用管城市了，任何城市都可以。",
        account_id=account_id,
    )
    after_cleared = app.get_candidate_profile(candidate_id, account_id=account_id)
    assert {"preferred_cities", "acceptable_cities"} <= set(cleared.saved_structured_fields)
    assert after_cleared.preferred_cities == []
    assert after_cleared.acceptable_cities == []


def test_acceptable_city_changes_match_explanation(tmp_path, account_id):
    """其他可接受城市会得到正向解释，但仍低于首选城市加分。"""

    app = JobHuntingApp()
    app.initialize()
    candidate_id = app.save_candidate_profile(
        build_profile(preferred_cities=["杭州"], acceptable_cities=["绍兴"]),
        account_id=account_id,
    )
    job = app.import_job_text(
        """
        Python 后端开发工程师
        12-18K
        绍兴市
        1-3年
        本科
        职位描述：负责 Python 后端服务开发。
        """,
        account_id=account_id,
    )

    result = app.match_job(candidate_id, job.id, account_id=account_id)

    assert job.city == "绍兴"
    assert any("其他可接受城市" in reason for reason in result.reasons)
    assert not any("目标城市偏好" in deduction for deduction in result.deductions)
