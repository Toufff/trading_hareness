import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.xiaojie_leader_flow import build_xiaojie_leader_flow_router
from app.xiaojie_leader_flow import evaluate_snapshot


class XiaojieLeaderFlowRouterTests(unittest.TestCase):
    def test_evaluate_route_is_research_only_and_typed(self):
        app = FastAPI()
        app.include_router(build_xiaojie_leader_flow_router(evaluate_snapshot))
        payload = {
            "snapshot": {
                "index_above_support": True,
                "index_volume_ratio": 1.2,
                "breadth_up_count": 2000,
                "breadth_down_count": 1000,
                "main_sector_present": True,
                "sector_strength_percentile": 0.9,
                "candidate_strength_rank": 1,
                "prior_one_word_board": True,
                "limit_up_return_flow": True,
                "re_seal_confirmed": True,
            }
        }
        response = TestClient(app).post("/api/v1/research/strategies/xiaojie-leader-flow/evaluate", json=payload)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["live_effect"], "none")
        self.assertEqual(body["boundary"], "research_only; no_automatic_order")
        self.assertEqual(body["decision"], "research_candidate")

    def test_unregistered_parameter_is_rejected(self):
        app = FastAPI()
        app.include_router(build_xiaojie_leader_flow_router(evaluate_snapshot))
        response = TestClient(app).post(
            "/api/v1/research/strategies/xiaojie-leader-flow/evaluate",
            json={"snapshot": {}, "parameters": {"live_weight": 1}},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
