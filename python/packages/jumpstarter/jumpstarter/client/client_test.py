"""Tests for client factory functions."""

from collections import OrderedDict
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.anyio


def create_mock_report(uuid: str, parent_uuid: str = "", name: str = "driver"):
    """Create a mock driver report."""
    report = MagicMock()
    report.uuid = uuid
    report.parent_uuid = parent_uuid
    report.labels = {
        "jumpstarter.dev/client": "test.MockClient",
        "jumpstarter.dev/name": name,
    }
    report.description = f"Mock {name} driver"
    report.methods_description = {}
    return report


class MockDriverClient:
    """Mock driver client class for testing."""

    def __init__(self, uuid, labels, stub, portal, stack, children, description, methods_description):
        self.uuid = uuid
        self.labels = labels
        self.stub = stub
        self.portal = portal
        self.stack = stack
        self.children = children
        self.description = description
        self.methods_description = methods_description


class TestClientFromChannel:
    async def test_client_from_channel_builds_driver_tree(self) -> None:
        """Test that client_from_channel builds a driver tree from reports."""
        # Create mock reports for a simple tree: root -> child
        root_uuid = str(uuid4())
        child_uuid = str(uuid4())

        root_report = create_mock_report(root_uuid, parent_uuid="", name="root")
        child_report = create_mock_report(child_uuid, parent_uuid=root_uuid, name="child")

        mock_response = MagicMock()
        mock_response.reports = [root_report, child_report]

        mock_stub = MagicMock()
        mock_stub.GetReport = AsyncMock(return_value=mock_response)

        mock_channel = MagicMock()
        mock_portal = MagicMock()
        mock_stack = ExitStack()

        with patch(
            "jumpstarter.client.client.MultipathExporterStub",
            return_value=mock_stub
        ), patch(
            "jumpstarter.client.client.import_class",
            return_value=MockDriverClient
        ):
            from jumpstarter.client.client import client_from_channel

            client = await client_from_channel(
                mock_channel, mock_portal, mock_stack, allow=[], unsafe=True
            )

        # Should return the last (root) client
        assert client is not None
        assert isinstance(client, MockDriverClient)
        # Child should be in the root's children
        assert "child" in client.children

    async def test_client_from_channel_topological_order(self) -> None:
        """Test that clients are built in topological order (children before parents)."""
        # Create a tree: grandparent -> parent -> child
        gp_uuid = str(uuid4())
        parent_uuid = str(uuid4())
        child_uuid = str(uuid4())

        gp_report = create_mock_report(gp_uuid, parent_uuid="", name="grandparent")
        parent_report = create_mock_report(parent_uuid, parent_uuid=gp_uuid, name="parent")
        child_report = create_mock_report(child_uuid, parent_uuid=parent_uuid, name="child")

        mock_response = MagicMock()
        mock_response.reports = [gp_report, parent_report, child_report]

        mock_stub = MagicMock()
        mock_stub.GetReport = AsyncMock(return_value=mock_response)

        mock_channel = MagicMock()
        mock_portal = MagicMock()
        mock_stack = ExitStack()

        build_order = []

        class TrackingMockClient(MockDriverClient):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                build_order.append(kwargs["labels"]["jumpstarter.dev/name"])

        with patch(
            "jumpstarter.client.client.MultipathExporterStub",
            return_value=mock_stub
        ), patch(
            "jumpstarter.client.client.import_class",
            return_value=TrackingMockClient
        ):
            from jumpstarter.client.client import client_from_channel

            await client_from_channel(
                mock_channel, mock_portal, mock_stack, allow=[], unsafe=True
            )

        # Children should be built before parents in topological order
        # The order should be child, parent, grandparent (or parent, child, grandparent)
        assert "grandparent" in build_order
        assert build_order.index("grandparent") > build_order.index("parent")

    async def test_client_from_channel_parent_child_relationships(self) -> None:
        """Test that parent-child relationships are correctly established."""
        root_uuid = str(uuid4())
        child1_uuid = str(uuid4())
        child2_uuid = str(uuid4())

        root_report = create_mock_report(root_uuid, parent_uuid="", name="root")
        child1_report = create_mock_report(child1_uuid, parent_uuid=root_uuid, name="child1")
        child2_report = create_mock_report(child2_uuid, parent_uuid=root_uuid, name="child2")

        mock_response = MagicMock()
        mock_response.reports = [root_report, child1_report, child2_report]

        mock_stub = MagicMock()
        mock_stub.GetReport = AsyncMock(return_value=mock_response)

        mock_channel = MagicMock()
        mock_portal = MagicMock()
        mock_stack = ExitStack()

        with patch(
            "jumpstarter.client.client.MultipathExporterStub",
            return_value=mock_stub
        ), patch(
            "jumpstarter.client.client.import_class",
            return_value=MockDriverClient
        ):
            from jumpstarter.client.client import client_from_channel

            client = await client_from_channel(
                mock_channel, mock_portal, mock_stack, allow=[], unsafe=True
            )

        # Root should have both children
        assert "child1" in client.children
        assert "child2" in client.children
        assert len(client.children) == 2

    async def test_client_from_channel_single_driver(self) -> None:
        """Test that a single driver without children works correctly."""
        root_uuid = str(uuid4())

        root_report = create_mock_report(root_uuid, parent_uuid="", name="root")

        mock_response = MagicMock()
        mock_response.reports = [root_report]

        mock_stub = MagicMock()
        mock_stub.GetReport = AsyncMock(return_value=mock_response)

        mock_channel = MagicMock()
        mock_portal = MagicMock()
        mock_stack = ExitStack()

        with patch(
            "jumpstarter.client.client.MultipathExporterStub",
            return_value=mock_stub
        ), patch(
            "jumpstarter.client.client.import_class",
            return_value=MockDriverClient
        ):
            from jumpstarter.client.client import client_from_channel

            client = await client_from_channel(
                mock_channel, mock_portal, mock_stack, allow=[], unsafe=True
            )

        assert client is not None
        assert client.children == {}
        assert client.uuid == UUID(root_uuid)

    async def test_client_from_channel_preserves_labels(self) -> None:
        """Test that driver labels are correctly passed to clients."""
        root_uuid = str(uuid4())

        root_report = create_mock_report(root_uuid, parent_uuid="", name="root")
        root_report.labels["custom.label"] = "custom_value"

        mock_response = MagicMock()
        mock_response.reports = [root_report]

        mock_stub = MagicMock()
        mock_stub.GetReport = AsyncMock(return_value=mock_response)

        mock_channel = MagicMock()
        mock_portal = MagicMock()
        mock_stack = ExitStack()

        with patch(
            "jumpstarter.client.client.MultipathExporterStub",
            return_value=mock_stub
        ), patch(
            "jumpstarter.client.client.import_class",
            return_value=MockDriverClient
        ):
            from jumpstarter.client.client import client_from_channel

            client = await client_from_channel(
                mock_channel, mock_portal, mock_stack, allow=[], unsafe=True
            )

        assert client.labels["custom.label"] == "custom_value"
        assert client.labels["jumpstarter.dev/name"] == "root"

    async def test_client_from_channel_passes_description(self) -> None:
        """Test that driver description is correctly passed to clients."""
        root_uuid = str(uuid4())

        root_report = create_mock_report(root_uuid, parent_uuid="", name="root")
        root_report.description = "Test driver description"
        root_report.methods_description = {"method1": "Does something"}

        mock_response = MagicMock()
        mock_response.reports = [root_report]

        mock_stub = MagicMock()
        mock_stub.GetReport = AsyncMock(return_value=mock_response)

        mock_channel = MagicMock()
        mock_portal = MagicMock()
        mock_stack = ExitStack()

        with patch(
            "jumpstarter.client.client.MultipathExporterStub",
            return_value=mock_stub
        ), patch(
            "jumpstarter.client.client.import_class",
            return_value=MockDriverClient
        ):
            from jumpstarter.client.client import client_from_channel

            client = await client_from_channel(
                mock_channel, mock_portal, mock_stack, allow=[], unsafe=True
            )

        assert client.description == "Test driver description"
        assert client.methods_description == {"method1": "Does something"}
