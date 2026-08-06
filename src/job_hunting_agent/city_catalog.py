"""城市偏好与邻近城市目录。

城市名称既会从网页表单进入档案，也会从职位原文和对话中进入系统。
本模块负责统一名称、从全国城市目录中识别城市，并提供一个可替换的
邻近城市解析边界。当前使用本地目录，后续可以在这个边界内接入地图或
地理编码服务，而不让匹配器直接依赖外部网络。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path


# 按长度从长到短处理，避免“香港特别行政区”先被截成“香港”。
CITY_SUFFIXES = (
    "特别行政区",
    "自治州",
    "地区",
    "盟",
    "市",
    "县",
)


# 首版只收录关系较稳定、求职场景中最常见的城市群；未知城市不做猜测。
NEARBY_CITY_GROUPS: dict[str, tuple[str, ...]] = {
    "北京": ("天津", "廊坊", "保定", "唐山"),
    "天津": ("北京", "廊坊", "唐山", "沧州"),
    "上海": ("苏州", "嘉兴", "南通", "宁波"),
    "杭州": ("嘉兴", "湖州", "绍兴", "宁波", "金华"),
    "宁波": ("杭州", "绍兴", "舟山", "台州"),
    "南京": ("镇江", "扬州", "马鞍山", "滁州", "常州"),
    "苏州": ("上海", "无锡", "常州", "南通", "嘉兴"),
    "广州": ("佛山", "东莞", "中山", "深圳", "珠海", "惠州"),
    "深圳": ("东莞", "惠州", "广州", "中山", "珠海"),
    "佛山": ("广州", "东莞", "中山", "肇庆"),
    "成都": ("德阳", "眉山", "资阳", "遂宁", "乐山"),
    "重庆": ("成都", "泸州", "内江", "达州"),
    "武汉": ("鄂州", "黄冈", "孝感", "黄石", "咸宁"),
    "西安": ("咸阳", "渭南", "铜川", "商洛"),
    "郑州": ("开封", "新乡", "许昌", "焦作", "洛阳"),
    "长沙": ("株洲", "湘潭", "岳阳", "益阳"),
    "合肥": ("芜湖", "马鞍山", "滁州", "六安", "淮南"),
    "厦门": ("泉州", "漳州", "龙岩"),
    "青岛": ("潍坊", "烟台", "日照", "淄博"),
}


def normalize_city_name(value: str | None) -> str:
    """把城市显示名转换成稳定的比较名，例如“杭州市”变成“杭州”。"""

    if value is None:
        return ""
    text = re.sub(r"[\s,，、;；]+", "", str(value).strip())
    for suffix in CITY_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    return text


def normalize_city_list(values: list[str] | tuple[str, ...] | None) -> list[str]:
    """清理、规范化并保持顺序去重一组城市。"""

    result: list[str] = []
    for value in values or []:
        city = normalize_city_name(value)
        if city and city not in result:
            result.append(city)
    return result


@lru_cache(maxsize=1)
def city_groups() -> tuple[dict[str, object], ...]:
    """读取前端随包分发的全国城市目录。"""

    path = Path(__file__).with_name("web_static") / "china_cities.js"
    text = path.read_text(encoding="utf-8")
    _, payload = text.split("=", 1)
    payload = payload.strip()
    if payload.endswith(";"):
        payload = payload[:-1].rstrip()
    # 文件是简单的 JavaScript 对象字面量，键名未加引号；转成 JSON 后再解析。
    payload = re.sub(r"([,{]\s*)(province|cities)\s*:", r'\1"\2":', payload)
    payload = re.sub(r",\s*]$", "]", payload)
    data = json.loads(payload)
    return tuple(data)


@lru_cache(maxsize=1)
def all_cities() -> tuple[str, ...]:
    """返回全国城市目录中的规范化城市名。"""

    values: list[str] = []
    for group in city_groups():
        for value in group.get("cities", []):
            city = normalize_city_name(str(value))
            if city and city not in values:
                values.append(city)
    return tuple(values)


@lru_cache(maxsize=1)
def city_aliases() -> tuple[tuple[str, str], ...]:
    """返回按匹配长度降序排列的“原名 -> 规范名”别名表。"""

    pairs: dict[str, str] = {}
    for group in city_groups():
        for value in group.get("cities", []):
            display = str(value).strip()
            canonical = normalize_city_name(display)
            if canonical:
                pairs[display] = canonical
                pairs.setdefault(canonical, canonical)
    return tuple(sorted(pairs.items(), key=lambda item: len(item[0]), reverse=True))


def cities_in_text(text: str) -> list[str]:
    """从一段中文文本中提取全国目录内的城市，并按出现顺序去重。"""

    source = str(text or "")
    found: list[tuple[int, str]] = []
    for alias, canonical in city_aliases():
        position = source.find(alias)
        if position >= 0:
            found.append((position, canonical))
    result: list[str] = []
    for _, city in sorted(found, key=lambda item: item[0]):
        if city not in result:
            result.append(city)
    return result


def nearby_cities(city_names: list[str] | tuple[str, ...]) -> list[str]:
    """返回本地目录能够可靠确认的邻近城市。

    目录没有覆盖的城市返回空列表，调用方应提示用户明确选择城市，
    而不是把同省所有城市误判成“邻近城市”。
    """

    sources = normalize_city_list(list(city_names))
    source_set = set(sources)
    result: list[str] = []
    for city in sources:
        related = list(NEARBY_CITY_GROUPS.get(city, ()))
        # 关系目录按常见中心城市维护；反向关系也应当可用。
        related.extend(
            origin
            for origin, neighbours in NEARBY_CITY_GROUPS.items()
            if city in neighbours
        )
        for nearby in related:
            canonical = normalize_city_name(nearby)
            if canonical and canonical not in result and canonical not in source_set:
                result.append(canonical)
    return result


def city_province(city: str | None) -> str | None:
    """返回城市所属省级行政区，无法识别时返回 None。"""

    canonical = normalize_city_name(city)
    if not canonical:
        return None
    for group in city_groups():
        cities = {normalize_city_name(str(value)) for value in group.get("cities", [])}
        if canonical in cities:
            return str(group.get("province") or "") or None
    return None
