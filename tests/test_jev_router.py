import unittest
from types import SimpleNamespace

from jev_router import JevConfig, JevRouter, parse_options


def choice(value, probabilities):
    return SimpleNamespace(choice=value, probabilities=probabilities)


class FakeJevClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def system_one(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def response(candidate="option_2", route="l0_direct", candidate_probability=0.99,
             route_probability=0.99):
    return SimpleNamespace(
        model="jev-test",
        answers={
            "candidate_answer": choice(candidate, {
                "option_1": 1 - candidate_probability,
                "option_2": candidate_probability,
            }),
            "route": choice(route, {
                "l0_direct": route_probability if route == "l0_direct" else 0,
                "verify_l1": route_probability if route == "verify_l1" else 0,
                "direct_l2": route_probability if route == "direct_l2" else 0,
                "other_mass": 1 - route_probability,
            }),
        },
    )


class JevRouterTests(unittest.TestCase):
    def router(self, reply=None, threshold=0.990, error=None):
        client = FakeJevClient(reply, error)
        router = JevRouter(JevConfig(True, "secret", "jev-test", threshold), client)
        return router, client

    def test_config_is_disabled_by_default_or_when_invalid(self):
        self.assertFalse(JevConfig.from_env({}).enabled)
        self.assertFalse(JevConfig.from_env({"JEV_ENABLED": "true"}).enabled)
        self.assertFalse(JevConfig.from_env({
            "JEV_ENABLED": "true", "TYPESAFE_API_KEY": "x",
            "JEV_DIRECT_CONFIDENCE": "invalid",
        }).enabled)
        config = JevConfig.from_env({
            "JEV_ENABLED": "yes", "TYPESAFE_API_KEY": "x",
            "JEV_MODEL": "jev-custom", "JEV_DIRECT_CONFIDENCE": "0.995",
        })
        self.assertTrue(config.enabled)
        self.assertEqual(config.model, "jev-custom")
        self.assertEqual(config.direct_confidence, 0.995)

    def test_client_initialization_failure_disables_l0(self):
        def fail(**_kwargs):
            raise RuntimeError("no client")

        router = JevRouter.from_env({
            "JEV_ENABLED": "true", "TYPESAFE_API_KEY": "secret",
        }, client_factory=fail)
        self.assertFalse(router.available)

    def test_single_choice_maps_internal_key_back_to_original_text(self):
        router, client = self.router(response())
        result = router.solve("1 + 1 = ?", "A. 1\nB. 2", "single")
        self.assertEqual(result.answer, "B. 2")
        self.assertTrue(result.can_answer_directly)
        call = client.calls[0]
        self.assertEqual(call["state"]["options"], {
            "option_1": "A. 1", "option_2": "B. 2",
        })
        self.assertEqual(set(call["questions"]), {"candidate_answer", "route"})

    def test_judgement_preserves_original_option_text(self):
        router, _ = self.router(response(candidate="option_1"))
        result = router.solve("天空是蓝色的", "正确\n错误", "judgement")
        self.assertEqual(result.answer, "正确")

    def test_threshold_is_inclusive_and_both_probabilities_are_required(self):
        router, _ = self.router(response(candidate_probability=0.990,
                                         route_probability=0.990))
        self.assertTrue(router.solve("题", "甲\n乙", "single").can_answer_directly)
        for candidate_probability, route_probability in ((0.989, 0.999), (0.999, 0.989)):
            with self.subTest(candidate=candidate_probability, route=route_probability):
                router, _ = self.router(response(
                    candidate_probability=candidate_probability,
                    route_probability=route_probability,
                ))
                self.assertFalse(router.solve("题", "甲\n乙", "single").can_answer_directly)

    def test_probabilities_are_normalized(self):
        reply = response()
        reply.answers["candidate_answer"].probabilities = {
            "option_1": 1, "option_2": 99,
        }
        router, _ = self.router(reply)
        result = router.solve("题", "甲\n乙", "single")
        self.assertEqual(result.answer_probability, 0.99)

    def test_unsupported_or_ambiguous_options_do_not_call_api(self):
        for kind, options in (
            ("multiple", "甲\n乙"),
            ("completion", ""),
            ("judgement", "正确\n错误\n不确定"),
            ("single", "A. 相同\nB. 相同"),
            ("single", "唯一选项"),
        ):
            with self.subTest(kind=kind, options=options):
                router, client = self.router(response())
                self.assertIsNone(router.solve("题", options, kind))
                self.assertEqual(client.calls, [])

    def test_sdk_and_invalid_response_fail_closed(self):
        router, _ = self.router(error=TimeoutError("timeout"))
        self.assertIsNone(router.solve("题", "甲\n乙", "single"))
        for reply in (
            SimpleNamespace(model="jev", answers=None),
            response(candidate="unknown"),
            response(route="unknown"),
        ):
            with self.subTest(reply=reply):
                router, _ = self.router(reply)
                self.assertIsNone(router.solve("题", "甲\n乙", "single"))

    def test_option_parser_accepts_labelled_and_unlabelled_text(self):
        self.assertEqual(parse_options("A、甲\nB）乙", "single"), {
            "option_1": "A、甲", "option_2": "B）乙",
        })


if __name__ == "__main__":
    unittest.main()
