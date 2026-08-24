from __future__ import annotations

from unittest.mock import AsyncMock
import unittest

from app.routers.ten_day_leader_rotation_actions import (
    TenDayLeaderRotationActionDependencies,
    build_ten_day_leader_rotation_actions_router,
)
from app.routers.ten_day_leader_rotation_reads import build_ten_day_leader_rotation_reads_router
from app.ten_day_leader_rotation_contracts import TenDayLeaderRotationLatestResponse


class TenDayLeaderRotationRouterTests(unittest.TestCase):
    def test_feature_routes_stay_separate_and_method_scoped(self) -> None:
        actions = build_ten_day_leader_rotation_actions_router(
            TenDayLeaderRotationActionDependencies(run=AsyncMock(return_value={"status": "completed"})),
        )
        reads = build_ten_day_leader_rotation_reads_router(AsyncMock())
        methods = {route.path: route.methods for route in [*actions.routes, *reads.routes]}

        self.assertEqual(methods["/api/v1/research/ten-day-leader-rotation/run"], {"POST"})
        self.assertEqual(methods["/api/v1/research/ten-day-leader-rotation/latest"], {"GET"})

    def test_latest_read_route_declares_the_typed_shadow_projection(self) -> None:
        router = build_ten_day_leader_rotation_reads_router(AsyncMock())
        route = next(route for route in router.routes if route.path.endswith("/latest"))

        self.assertIs(route.response_model, TenDayLeaderRotationLatestResponse)


if __name__ == "__main__":
    unittest.main()
