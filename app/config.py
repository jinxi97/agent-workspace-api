import os

from dotenv import load_dotenv

load_dotenv()

SANDBOX_TEMPLATE_NAME = os.getenv("SANDBOX_TEMPLATE_NAME", "python-runtime-template")
CLAUDE_AGENT_SANDBOX_TEMPLATE_NAME = os.getenv("CLAUDE_AGENT_SANDBOX_TEMPLATE_NAME", "claude-agent-sandbox-template")
SANDBOX_NAMESPACE = os.getenv("SANDBOX_NAMESPACE", "pod-snapshots-ns")
SANDBOX_API_URL = os.getenv(
    "SANDBOX_API_URL",
    "http://sandbox-router-svc.agent-sandbox-application.svc.cluster.local:8080",
)
CLOUD_SQL_CONNECTION_NAME = os.getenv("CLOUD_SQL_CONNECTION_NAME", "")

SNAPSHOT_NAMESPACE = os.getenv("SNAPSHOT_NAMESPACE", SANDBOX_NAMESPACE)
SNAPSHOT_STORAGE_CONFIG_NAME = os.getenv("SNAPSHOT_STORAGE_CONFIG_NAME", "cpu-pssc-gcs")
