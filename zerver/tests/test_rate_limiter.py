from zerver.lib.rate_limiter import (
    add_ratelimit_rule,
    remove_ratelimit_rule,
    RateLimitedObject,
    RateLimitedUser,
    RateLimiterBackend,
    RedisRateLimiterBackend,
    TornadoInMemoryRateLimiterBackend,
)

from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.utils import generate_random_token

from typing import Dict, List, Tuple, Type

import mock
import time

RANDOM_KEY_PREFIX = generate_random_token(32)

class RateLimitedTestObject(RateLimitedObject):
    def __init__(self, name: str, rules: List[Tuple[int, int]],
                 backend: Type[RateLimiterBackend]) -> None:
        self.name = name
        self._rules = rules
        self._rules.sort(key=lambda x: x[0])
        super().__init__(backend)

    def key(self) -> str:
        return RANDOM_KEY_PREFIX + self.name

    def rules(self) -> List[Tuple[int, int]]:
        return self._rules

class RateLimiterBackendBase(ZulipTestCase):
    __unittest_skip__ = True

    def setUp(self) -> None:
        self.requests_record = {}  # type: Dict[str, List[float]]

    def create_object(self, name: str, rules: List[Tuple[int, int]]) -> RateLimitedTestObject:
        obj = RateLimitedTestObject(name, rules, self.backend)
        obj.clear_history()

        return obj

    def make_request(self, obj: RateLimitedTestObject, expect_ratelimited: bool=False,
                     verify_api_calls_left: bool=True) -> None:
        key = obj.key()
        if key not in self.requests_record:
            self.requests_record[key] = []

        ratelimited, secs_to_freedom = obj.rate_limit()
        if not ratelimited:
            self.requests_record[key].append(time.time())

        self.assertEqual(ratelimited, expect_ratelimited)

        if verify_api_calls_left:
            self.verify_api_calls_left(obj)

    def verify_api_calls_left(self, obj: RateLimitedTestObject) -> None:
        now = time.time()
        with mock.patch('time.time', return_value=now):
            calls_remaining, time_till_reset = obj.api_calls_left()

        expected_calls_remaining, expected_time_till_reset = self.expected_api_calls_left(obj, now)
        self.assertEqual(expected_calls_remaining, calls_remaining)
        self.assertEqual(expected_time_till_reset, time_till_reset)

    def expected_api_calls_left(self, obj: RateLimitedTestObject, now: float) -> Tuple[int, float]:
        longest_rule = obj.rules()[-1]
        max_window, max_calls = longest_rule
        history = self.requests_record.get(obj.key())
        if history is None:
            return max_calls, 0
        history.sort()

        return self.api_calls_left_from_history(history, max_window, max_calls, now)

    def api_calls_left_from_history(self, history: List[float], max_window: int,
                                    max_calls: int, now: float) -> Tuple[int, float]:
        """
        This depends on the algorithm used in the backend, and should be defined by the test class.
        """
        raise NotImplementedError  # nocoverage

    def test_hit_ratelimits(self) -> None:
        obj = self.create_object('test', [(2, 3), ])

        start_time = time.time()
        for i in range(3):
            with mock.patch('time.time', return_value=(start_time + i * 0.1)):
                self.make_request(obj, expect_ratelimited=False)

        with mock.patch('time.time', return_value=(start_time + 0.4)):
            self.make_request(obj, expect_ratelimited=True)

        with mock.patch('time.time', return_value=(start_time + 2.01)):
            self.make_request(obj, expect_ratelimited=False)

    def test_clear_history(self) -> None:
        obj = self.create_object('test', [(2, 3), ])
        start_time = time.time()
        for i in range(3):
            with mock.patch('time.time', return_value=(start_time + i * 0.1)):
                self.make_request(obj, expect_ratelimited=False)
        with mock.patch('time.time', return_value=(start_time + 0.4)):
            self.make_request(obj, expect_ratelimited=True)

        obj.clear_history()
        self.requests_record[obj.key()] = []
        for i in range(3):
            with mock.patch('time.time', return_value=(start_time + i * 0.1)):
                self.make_request(obj, expect_ratelimited=False)

    def test_block_unblock_access(self) -> None:
        obj = self.create_object('test', [(2, 5), ])
        start_time = time.time()

        obj.block_access(1)
        with mock.patch('time.time', return_value=(start_time)):
            self.make_request(obj, expect_ratelimited=True, verify_api_calls_left=False)

        obj.unblock_access()
        with mock.patch('time.time', return_value=(start_time)):
            self.make_request(obj, expect_ratelimited=False, verify_api_calls_left=False)

    def test_api_calls_left(self) -> None:
        obj = self.create_object('test', [(2, 5), (3, 6)])
        start_time = time.time()

        # Check the edge case when no requests have been made yet.
        with mock.patch('time.time', return_value=(start_time)):
            self.verify_api_calls_left(obj)

        with mock.patch('time.time', return_value=(start_time)):
            self.make_request(obj)

        # Check the correct default values again, after the reset has happened on the first rule,
        # but not the other.
        with mock.patch('time.time', return_value=(start_time + 2.1)):
            self.make_request(obj)

class RedisRateLimiterBackendTest(RateLimiterBackendBase):
    __unittest_skip__ = False
    backend = RedisRateLimiterBackend

    def api_calls_left_from_history(self, history: List[float], max_window: int,
                                    max_calls: int, now: float) -> Tuple[int, float]:
        latest_timestamp = history[-1]
        relevant_requests = [t for t in history if (t >= now - max_window)]
        relevant_requests_amount = len(relevant_requests)

        return max_calls - relevant_requests_amount, latest_timestamp + max_window - now

    def test_block_access(self) -> None:
        """
        This test cannot verify that the user will get unblocked
        after the correct amount of time, because that event happens
        inside redis, so we're not able to mock the timer. Making the test
        sleep for 1s is also too costly to be worth it.
        """
        obj = self.create_object('test', [(2, 5), ])

        obj.block_access(1)
        self.make_request(obj, expect_ratelimited=True, verify_api_calls_left=False)

class TornadoInMemoryRateLimiterBackendTest(RateLimiterBackendBase):
    __unittest_skip__ = False
    backend = TornadoInMemoryRateLimiterBackend

    def api_calls_left_from_history(self, history: List[float], max_window: int,
                                    max_calls: int, now: float) -> Tuple[int, float]:
        reset_time = 0.0
        for timestamp in history:
            reset_time = max(reset_time, timestamp) + (max_window / max_calls)

        calls_left = (now + max_window - reset_time) * max_calls // max_window
        calls_left = int(calls_left)

        return calls_left, reset_time - now

    def test_used_in_tornado(self) -> None:
        user_profile = self.example_user("hamlet")
        with self.settings(RUNNING_INSIDE_TORNADO=True):
            obj = RateLimitedUser(user_profile)
        self.assertEqual(obj.backend, TornadoInMemoryRateLimiterBackend)

    def test_block_access(self) -> None:
        obj = self.create_object('test', [(2, 5), ])
        start_time = time.time()

        obj.block_access(1)
        with mock.patch('time.time', return_value=(start_time)):
            self.make_request(obj, expect_ratelimited=True, verify_api_calls_left=False)

        with mock.patch('time.time', return_value=(start_time + 1.01)):
            self.make_request(obj, expect_ratelimited=False, verify_api_calls_left=False)

class RateLimitedUserTest(ZulipTestCase):
    def test_user_rate_limits(self) -> None:
        user_profile = self.example_user("hamlet")
        user_profile.rate_limits = "1:3,2:4"
        obj = RateLimitedUser(user_profile)

        self.assertEqual(obj.rules(), [(1, 3), (2, 4)])

    def test_add_remove_rule(self) -> None:
        user_profile = self.example_user("hamlet")
        add_ratelimit_rule(1, 2)
        add_ratelimit_rule(4, 5, domain='some_new_domain')
        add_ratelimit_rule(10, 100, domain='some_new_domain')
        obj = RateLimitedUser(user_profile)

        self.assertEqual(obj.rules(), [(1, 2), ])
        obj.domain = 'some_new_domain'
        self.assertEqual(obj.rules(), [(4, 5), (10, 100)])

        remove_ratelimit_rule(10, 100, domain='some_new_domain')
        self.assertEqual(obj.rules(), [(4, 5), ])