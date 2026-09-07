import click
import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from .k8s import handle_k8s_api_exception


def api_exception(status: int, body, reason: str | None = None) -> ApiException:
    error = ApiException(status=status, reason=reason)
    error.body = body
    return error


def test_reports_the_reason_and_message_from_the_server():
    error = api_exception(404, '{"reason":"NotFound","message":"clients.jumpstarter.dev \\"nope\\" not found"}')

    with pytest.raises(click.ClickException, match=r"Error from server \(NotFound\): .*not found"):
        handle_k8s_api_exception(error)


def test_reports_a_status_that_carries_no_reason():
    # Schema and admission failures answer with a message and nothing else.
    error = api_exception(500, '{"kind":"Status","message":".provisioner: field not declared in schema","code":500}')

    with pytest.raises(click.ClickException, match="Error from server: .provisioner: field not declared in schema"):
        handle_k8s_api_exception(error)


def test_falls_back_to_the_http_reason_when_the_body_says_nothing():
    error = api_exception(503, "{}", reason="Service Unavailable")

    with pytest.raises(click.ClickException, match="Error from server: Service Unavailable"):
        handle_k8s_api_exception(error)


def test_passes_through_a_body_that_is_not_json():
    error = api_exception(502, "<html>bad gateway</html>")

    with pytest.raises(click.ClickException, match="Server error: <html>bad gateway</html>"):
        handle_k8s_api_exception(error)


@pytest.mark.parametrize("body", ["null", '"just a string"', "[1,2,3]"])
def test_passes_through_json_that_is_not_a_status(body):
    # A proxy in front of the API server can answer with valid JSON that is not
    # a Status object; reading it as one used to raise AttributeError.
    error = api_exception(502, body)

    with pytest.raises(click.ClickException, match="Server error: "):
        handle_k8s_api_exception(error)


def test_passes_through_a_missing_body():
    error = api_exception(500, None)

    with pytest.raises(click.ClickException, match="Server error: None"):
        handle_k8s_api_exception(error)
