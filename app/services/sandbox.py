from agentic_sandbox import SandboxClient

from app.config import SANDBOX_API_URL, SANDBOX_NAMESPACE


def reconstruct_sandbox(claim_name: str, template_name: str) -> SandboxClient:
    """Reconstruct a SandboxClient from a persisted claim_name."""
    sandbox = SandboxClient(
        template_name=template_name,
        namespace=SANDBOX_NAMESPACE,
        api_url=SANDBOX_API_URL,
    )
    sandbox.claim_name = claim_name
    sandbox.base_url = SANDBOX_API_URL
    return sandbox
