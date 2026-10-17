"""Bounded transport and ownership checks for portable account exports."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

MAX_EXPORT_BYTES = 16 * 1024 * 1024
DIRECT_TABLES = (
    "candidate_profiles", "jobs", "long_texts", "chat_sessions", "chat_messages",
    "project_experience_cards", "resume_drafts", "knowledge_assets", "project_archive_imports",
    "project_collection_sessions", "visual_knowledge_items", "resume_artifacts", "usage_events",
    "tool_call_traces", "background_tasks", "account_balance_ledger", "recharge_orders",
)
JOINED_TABLES = {
    "knowledge_asset_versions": ("knowledge_assets", "asset_id"),
    "project_archive_files": ("project_archive_imports", "project_archive_id"),
    "project_collection_files": ("project_collection_sessions", "collection_id"),
}
ACCOUNT_FIELDS = {"id", "email", "display_name", "role", "status", "must_change_password",
                  "email_verified_at", "deleted_at", "created_at", "updated_at"}
FORBIDDEN_ROW_FIELDS = {"password_hash", "token_hash", "embedding", "session_token", "internal_token"}


def validate_export(value: object, account_id: int | None = None) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "exported_at", "account", "consents", "account_balance", *DIRECT_TABLES, *JOINED_TABLES,
    }:
        raise ValueError("Invalid export fields")
    account = value["account"]
    if (not isinstance(account, dict) or set(account) != ACCOUNT_FIELDS
            or type(account["id"]) is not int or account["id"] <= 0
            or (account_id is not None and account["id"] != account_id)
            or account["status"] != "active" or account["deleted_at"] is not None
            or not isinstance(value["exported_at"], str)):
        raise ValueError("Invalid export account")
    owner = account["id"]

    def row_valid(row):
        return isinstance(row, dict) and not FORBIDDEN_ROW_FIELDS.intersection(row)

    for table in (*DIRECT_TABLES, *JOINED_TABLES):
        rows = value[table]
        if not isinstance(rows, list) or any(not row_valid(row) for row in rows):
            raise ValueError("Invalid export rows")
        if table in DIRECT_TABLES and any(type(row.get("account_id")) is not int or row["account_id"] != owner for row in rows):
            raise ValueError("Export owner mismatch")
    balance = value["account_balance"]
    if not row_valid(balance) or type(balance.get("account_id")) is not int or balance["account_id"] != owner:
        raise ValueError("Export balance owner mismatch")
    for table, (parent, field) in JOINED_TABLES.items():
        parents = {row["id"] for row in value[parent]}
        if any(type(row.get(field)) is not int or row[field] not in parents for row in value[table]):
            raise ValueError("Export parent mismatch")
    consents = value["consents"]
    if not isinstance(consents, list) or any(
        not isinstance(row, dict) or set(row) != {"document_type", "version", "accepted_at"}
        or any(not isinstance(item, str) for item in row.values()) for row in consents
    ):
        raise ValueError("Invalid export consents")
    return value


def encode_export(value: dict, account_id: int) -> bytes:
    validate_export(value, account_id)
    result = bytearray()

    def portable(item):
        if isinstance(item, Decimal) and item.is_finite():
            return str(item)
        if isinstance(item, date):
            return item.isoformat()
        raise TypeError("Unsupported portable export value")

    for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False, default=portable, separators=(",", ":")).iterencode(value):
        chunk = part.encode("utf-8")
        if len(result) + len(chunk) > MAX_EXPORT_BYTES:
            raise OverflowError("Account export exceeds synchronous download limit")
        result.extend(chunk)
    return bytes(result)


def decode_export(body: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate export field")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("Non-finite export number")

    return validate_export(json.loads(body, object_pairs_hook=unique, parse_constant=invalid_constant))
