"""
Railway API client for managing store deployments.

Uses Railway's public API (GraphQL) to:
- List env vars on a service
- Upsert env vars (push credentials from master)
- Trigger a redeploy

Docs: https://docs.railway.com/reference/public-api
"""

import logging
import httpx
from app.config import get_config

logger = logging.getLogger(__name__)

RAILWAY_API_URL = "https://backboard.railway.com/graphql/v2"


def _headers() -> dict:
    config = get_config()
    if not config.railway_api_token:
        raise RuntimeError("RAILWAY_API_TOKEN is not configured")
    return {
        "Authorization": f"Bearer {config.railway_api_token}",
        "Content-Type": "application/json",
    }


async def _graphql(query: str, variables: dict | None = None) -> dict:
    """Execute a Railway GraphQL query."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            RAILWAY_API_URL,
            headers=_headers(),
            json={"query": query, "variables": variables or {}},
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            error_msg = data["errors"][0].get("message", str(data["errors"]))
            raise RuntimeError(f"Railway API error: {error_msg}")
        return data.get("data", {})


async def get_service_info(service_id: str) -> dict:
    """Get basic info about a Railway service."""
    query = """
    query($serviceId: String!) {
        service(id: $serviceId) {
            id
            name
            projectId
            updatedAt
        }
    }
    """
    data = await _graphql(query, {"serviceId": service_id})
    return data.get("service", {})


async def get_variables(service_id: str, environment_id: str) -> dict:
    """Get all environment variables for a service in a given environment."""
    query = """
    query($serviceId: String!, $environmentId: String!) {
        variables(serviceId: $serviceId, environmentId: $environmentId)
    }
    """
    data = await _graphql(query, {
        "serviceId": service_id,
        "environmentId": environment_id,
    })
    return data.get("variables", {})


async def upsert_variables(
    service_id: str,
    environment_id: str,
    variables: dict[str, str],
) -> bool:
    """
    Set environment variables on a Railway service.
    Merges with existing vars (does not delete unmentioned ones).
    """
    query = """
    mutation($input: VariableCollectionUpsertInput!) {
        variableCollectionUpsert(input: $input)
    }
    """
    await _graphql(query, {
        "input": {
            "serviceId": service_id,
            "environmentId": environment_id,
            "variables": variables,
        },
    })
    logger.info(f"Upserted {len(variables)} variables on service {service_id}")
    return True


async def get_environments(project_id: str) -> list[dict]:
    """List all environments in a Railway project."""
    query = """
    query($projectId: String!) {
        environments(projectId: $projectId) {
            edges {
                node {
                    id
                    name
                }
            }
        }
    }
    """
    data = await _graphql(query, {"projectId": project_id})
    edges = data.get("environments", {}).get("edges", [])
    return [edge["node"] for edge in edges]


async def redeploy_service(service_id: str, environment_id: str) -> str:
    """Trigger a redeploy of a Railway service. Returns the new deployment ID."""
    query = """
    mutation($serviceId: String!, $environmentId: String!) {
        serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
    }
    """
    data = await _graphql(query, {
        "serviceId": service_id,
        "environmentId": environment_id,
    })
    logger.info(f"Triggered redeploy for service {service_id}")
    return data.get("serviceInstanceRedeploy", "")


async def get_latest_deployment(service_id: str) -> dict | None:
    """Get the latest deployment for a service."""
    query = """
    query($input: DeploymentListInput!) {
        deployments(input: $input) {
            edges {
                node {
                    id
                    status
                    createdAt
                }
            }
        }
    }
    """
    data = await _graphql(query, {
        "input": {"serviceId": service_id, "first": 1},
    })
    edges = data.get("deployments", {}).get("edges", [])
    return edges[0]["node"] if edges else None
