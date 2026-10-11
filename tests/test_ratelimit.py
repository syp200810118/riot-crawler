import asyncio

from riot_crawler.ratelimit import WINDOW_PADDING_S, SlidingWindowLimiter, parse_limit_header


class FakeTime:
    def __init__(self):
        self.t = 1000.0

    def clock(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def test_parse_limit_header():
    assert parse_limit_header("100:120,20:1") == [(20, 1.0), (100, 120.0)]
    assert parse_limit_header("") is None
    assert parse_limit_header(None) is None


def test_respects_every_window():
    ft = FakeTime()
    lim = SlidingWindowLimiter([(2, 1.0), (3, 10.0)], clock=ft.clock, sleep=ft.sleep)

    async def go():
        stamps = []
        for _ in range(4):
            await lim.acquire()
            stamps.append(ft.t - 1000.0)
        return stamps

    s = asyncio.run(go())
    assert s[0] == s[1] == 0.0                         # burst of 2 allowed
    assert abs(s[2] - (1.0 + WINDOW_PADDING_S)) < 1e-9  # third waits for the 1 s window
    assert abs(s[3] - (10.0 + WINDOW_PADDING_S)) < 1e-9  # fourth waits for the 10 s window


def test_block_for():
    ft = FakeTime()
    lim = SlidingWindowLimiter([(100, 1.0)], clock=ft.clock, sleep=ft.sleep)
    lim.block_for(5)
    asyncio.run(lim.acquire())
    assert ft.t >= 1005.0
