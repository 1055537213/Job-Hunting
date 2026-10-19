"""Strict read-only profile projection shared by Python callback and compatibility client."""

import json
import math
from dataclasses import fields

from .models import CandidateProfile

MAX_PROFILE_QUERY_BYTES = 4 * 1024 * 1024
PROFILE_FIELDS = {field.name for field in fields(CandidateProfile)}
MAX_PROFILE_ID = 2**63 - 1
MAX_PROFILE_CREATE_BYTES = 64 * 1024


def validate_creation(value):
    if not isinstance(value, dict) or set(value) != {"candidate_id", "profile"} or not valid_id(value["candidate_id"]):
        raise ValueError("Invalid profile creation receipt")
    validate_projection({"profile": value["profile"]}, value["candidate_id"])
    return value


def valid_id(value):
    return type(value) is int and 0 < value <= MAX_PROFILE_ID


def validate_projection(value, candidate_id=None):
    key = "profiles" if candidate_id is None else "profile"
    if not isinstance(value, dict) or set(value) != {key}:
        raise ValueError("Invalid profile projection")
    rows = value[key] if candidate_id is None else [value[key]]
    if not isinstance(rows, list):
        raise ValueError("Invalid profile collection")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != PROFILE_FIELDS or not valid_id(row["id"]) or row["id"] in seen:
            raise ValueError("Invalid profile fields")
        seen.add(row["id"])
        if candidate_id is not None and row["id"] != candidate_id:
            raise ValueError("Mismatched profile")
        if not all(isinstance(row[key], str) for key in ("name", "status", "education")):
            raise ValueError("Invalid profile text")
        if not _number(row["experience_years"]):
            raise ValueError("Invalid profile experience")
        if not all(row[key] is None or type(row[key]) is int for key in ("salary_floor_k", "expected_salary_k")):
            raise ValueError("Invalid profile salary")
        if not all(isinstance(row[key], list) and all(isinstance(item, str) for item in row[key])
                   for key in ("preferred_cities", "acceptable_cities", "target_directions", "unacceptable")):
            raise ValueError("Invalid profile list")
        for key in ("skills", "preference_weights"):
            if not isinstance(row[key], dict) or not all(isinstance(k, str) and (isinstance(v, str) if key == "skills" else _number(v))
                                                       for k, v in row[key].items()):
                raise ValueError("Invalid profile mapping")
    return value


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def encode_callback(account_id, value, candidate_id=None):
    validate_projection(value, candidate_id)
    if not valid_id(account_id):
        raise ValueError("Invalid profile owner")
    body = json.dumps({"account_id": account_id, **value}, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(body) > MAX_PROFILE_QUERY_BYTES:
        raise OverflowError("Profile query too large")
    return body


def decode_json(body):
    if len(body) > MAX_PROFILE_QUERY_BYTES:
        raise ValueError("Profile query too large")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate profile JSON field")
            value[key] = item
        return value
    return json.loads(body, object_pairs_hook=unique)


def decode_projection(body, candidate_id=None):
    return validate_projection(decode_json(body), candidate_id)
