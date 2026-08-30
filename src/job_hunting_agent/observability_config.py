"""在运行时从受保护的 `.env` 生成 Alertmanager 配置。"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_ENV_PATH, load_dotenv_values


@dataclass(frozen=True)
class AlertmanagerSettings:
    """邮件通知所需的最小配置。"""

    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from_email: str
    alert_email_to: str


def load_alertmanager_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> AlertmanagerSettings:
    """读取 SMTP 与值班收件人；任何缺失都在启动前失败。"""

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def required(key: str) -> str:
        value = environment.get(key) or file_values.get(key)
        if not value or not value.strip():
            raise ValueError(f"缺少 Alertmanager 配置：{key}")
        return value.strip()

    try:
        port = int(required("JOB_AGENT_SMTP_PORT"))
    except ValueError as error:
        raise ValueError("JOB_AGENT_SMTP_PORT 必须是 1 到 65535 的整数") from error
    if port < 1 or port > 65535:
        raise ValueError("JOB_AGENT_SMTP_PORT 必须是 1 到 65535 的整数")
    return AlertmanagerSettings(
        smtp_host=required("JOB_AGENT_SMTP_HOST"),
        smtp_port=port,
        smtp_username=required("JOB_AGENT_SMTP_USERNAME"),
        smtp_password=required("JOB_AGENT_SMTP_PASSWORD"),
        smtp_from_email=required("JOB_AGENT_SMTP_FROM_EMAIL"),
        alert_email_to=required("JOB_AGENT_ALERT_EMAIL_TO"),
    )


def render_alertmanager_config(
    settings: AlertmanagerSettings,
    *,
    smtp_require_tls: bool = True,
) -> str:
    """生成 JSON 形式的 YAML 配置，避免用字符串替换处理 SMTP 密钥。

    生产入口始终使用默认的 TLS 要求；显式关闭只供隔离的 Mailpit 验收脚本使用。
    """

    config = {
        "global": {
            "resolve_timeout": "5m",
            "smtp_smarthost": f"{settings.smtp_host}:{settings.smtp_port}",
            "smtp_from": settings.smtp_from_email,
            "smtp_auth_username": settings.smtp_username,
            "smtp_auth_password": settings.smtp_password,
            "smtp_require_tls": smtp_require_tls,
        },
        "route": {
            "receiver": "operations-email",
            "group_by": ["alertname", "severity"],
            "group_wait": "30s",
            "group_interval": "5m",
            "repeat_interval": "4h",
        },
        "receivers": [
            {
                "name": "operations-email",
                "email_configs": [
                    {
                        "to": settings.alert_email_to,
                        "send_resolved": True,
                        "force_implicit_tls": settings.smtp_port == 465,
                    }
                ],
            }
        ],
        "inhibit_rules": [
            {
                "source_matchers": ['severity="critical"'],
                "target_matchers": ['severity="warning"'],
                "equal": ["alertname"],
            }
        ],
    }
    return json.dumps(config, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> None:
    """写入只允许 Alertmanager 用户读取的配置文件。"""

    parser = argparse.ArgumentParser(prog="job-agent-alertmanager-config")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--output", required=True)
    parser.add_argument("--owner-uid", type=int, default=None)
    parser.add_argument("--owner-gid", type=int, default=None)
    args = parser.parse_args(argv)

    settings = load_alertmanager_settings(args.env_file)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(render_alertmanager_config(settings), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(output)
    if args.owner_uid is not None:
        os.chown(
            output,
            args.owner_uid,
            args.owner_gid if args.owner_gid is not None else args.owner_uid,
        )
    print(f"Alertmanager configuration written to {output}")


if __name__ == "__main__":
    main()
