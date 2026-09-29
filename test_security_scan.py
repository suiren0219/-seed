"""离线验证四项加固（不联网、不 import astrbot）：

1. 敏感信息加密：SecretBox 往返 / 错误密钥报错 / 脱敏 / 房间摘要
2. 历史记录去明文：storage 写入摘要后仍能正确查询，且文件里没有 roomverify
3. 接口容错：ApiConfig 可覆盖取值路径，解析不出时落盘结构指纹
4. 搜校风控：令牌桶限速、并发闸门、连续失败熔断

运行：python test_security_scan.py
"""
import asyncio
import json
import logging
import os
import random
import re
import shutil
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import secure_store
import power_service
import scheduler_job
from config_loader import box_of, load_config
from power_service import ApiConfig, extract_odd
from storage import PowerStorage

MAIN_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "astrbot_plugin_dorm_power", "main.py")
ROOM = "101-1--11-101"
ACCOUNT = "440000200001010000"   # 仅结构演示用的假号段
passed = 0


def check(desc, cond):
    global passed
    assert cond, f"✗ {desc}"
    passed += 1
    print(f"  [OK] {desc}")


# ---------------------------------------------------------------- 1. 加密
def test_secret_box():
    print("[1] 敏感信息加密")
    for name, box in (("fernet/自动选择", secure_store.SecretBox(secure_store.generate_key())),
                      ("无密钥(不加密)", secure_store.SecretBox(None))):
        src = json.dumps({"account": ACCOUNT, "rooms": [["1栋A-101", ROOM]]}, ensure_ascii=False)
        enc = box.encrypt(src)
        check(f"{name}：加解密往返一致", box.decrypt(enc) == src)
        if box.available:
            check(f"{name}：密文里不含明文账号", ACCOUNT not in enc and ROOM not in enc)
    box = secure_store.SecretBox(secure_store.generate_key())
    wrong = secure_store.SecretBox(secure_store.generate_key())
    try:
        wrong.decrypt(box.encrypt(ACCOUNT))
    except Exception as e:
        check(f"错误密钥被拒绝：{type(e).__name__}", True)
    else:
        raise AssertionError("错误密钥竟然解密成功")
    check("脱敏首尾各隐 4 位：****0020000101****",
          secure_store.mask_id(ACCOUNT) == "****0020000101****")
    check("脱敏结果不含原号前 4 位/后 4 位",
          not secure_store.mask_id(ACCOUNT).startswith(ACCOUNT[:4])
          and not secure_store.mask_id(ACCOUNT).endswith(ACCOUNT[-4:]))
    check("短账号整体打码（不露任何位）", secure_store.mask_id("12345678") == "********")
    check("空账号返回空串", secure_store.mask_id("") == "" and secure_store.mask_id(None) == "")
    check("房间摘要确定且不可逆", secure_store.room_key("k", ROOM) == secure_store.room_key("k", ROOM)
          and ROOM not in secure_store.room_key("k", ROOM))
    check("无密钥时摘要退化为原文（兼容旧数据）", secure_store.room_key(None, ROOM) == ROOM)


# ---------------------------------------------------------------- 2. 存储
def test_storage():
    print("[2] 历史记录去明文")
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "history.json")
        st = PowerStorage(path, secret="demo-key")
        check("空历史 last_record 为 None", st.last_record(ROOM) is None)
        st.record(ROOM, 30.0)
        check("首次记录后 last_record 返回它", st.last_record(ROOM)["odd"] == 30.0)
        st.record(ROOM, 28.0)
        prev = st.last_record(ROOM)
        check("再记录后返回最新一条", prev is not None and prev["odd"] == 28.0)
        check("文件里没有明文 roomverify", ROOM not in open(path, encoding="utf-8").read())

        # 造两条相隔 2 小时的记录（30 -> 28 度），验证摘要主键下仍能算速率
        now = datetime.now()
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["records"][0]["time"] = (now - timedelta(hours=2)).isoformat(timespec="seconds")
        data["records"][1]["time"] = now.isoformat(timespec="seconds")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        st2 = PowerStorage(path, secret="demo-key")
        rate = st2.drop_rate(ROOM, hours=24)
        check(f"掉电速率可计算：{rate} 度/小时", rate is not None and abs(rate - 1.0) < 0.05)

        # 旧明文记录（未迁移的历史数据）仍可匹配
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["records"].insert(0, {"time": (now - timedelta(hours=4)).isoformat(timespec="seconds"),
                                   "room": ROOM, "odd": 50.0})  # 插到最前，保持时间递增
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        st3 = PowerStorage(path, secret="demo-key")
        check("兼容历史明文记录", st3.drop_rate(ROOM, hours=24 * 365 * 10) is not None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 3. 配置与解析
def test_config_and_parse():
    print("[3] 配置解密 + 接口字段容错")
    tmp = tempfile.mkdtemp()
    try:
        key = secure_store.generate_key()
        box = secure_store.SecretBox(key)
        cfg_path = os.path.join(tmp, "config.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(f'student:\n  account: "{box.encrypt(ACCOUNT)}"\n'
                    f'  customercode: 1000145\n'
                    f'security:\n  key: "{key}"\n')
        os.environ.pop("DORM_POWER_KEY", None)
        cfg = load_config(cfg_path)
        check("配置里的密文账号被还原", cfg["student"]["account"] == ACCOUNT)

        api = ApiConfig.from_dict({"odd_paths": ["modlist.0.odd"]})
        odd, probe = extract_odd({"modlist": [{"odd": "31.5"}]}, api=api)
        check(f"按配置路径取到电量 {odd}", odd == 31.5 and probe["field"] == "modlist.0.odd")
        odd2, probe2 = extract_odd({"modlist": [{"surplus": 12}]},
                                   api=ApiConfig.from_dict({"odd_fields": ["surplus"]}))
        check(f"字段名改了只改配置即可（{probe2['field']}）", odd2 == 12.0)
        # 结构指纹落盘位置改到临时目录：默认写 data/schema_probe.log，
        # 测试往里灌假结构会让真正的上游改版线索淹没在噪声里。
        schema_log = os.path.join(tmp, "schema_probe.log")
        power_service.SCHEMA_LOG = schema_log
        power_service._seen_schema.clear()
        odd3, _ = extract_odd({"unexpected": {"x": 1}}, api=api)
        check("认不出结构时返回 None 而不是崩溃", odd3 is None)
        check("结构指纹已落盘便于适配", os.path.exists(schema_log)
              and "unexpected" in open(schema_log, encoding="utf-8").read())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 3b. 预警与接口小坑
def test_warn_and_helpers():
    print("[3b] 预警速率 / 时间戳 / 房间筛选")
    # 掉电预警要按「每小时速率」判断：跨小时的低消耗不能被当成异常掉电
    slow = scheduler_job.should_warn(prev_odd=50.0, cur_odd=40.0, threshold=20,
                                    drop_per_hour=5, hours_between=10.0)
    check("10 小时掉 10 度（1 度/小时）不预警", slow is None)
    fast = scheduler_job.should_warn(prev_odd=50.0, cur_odd=40.0, threshold=20,
                                     drop_per_hour=5, hours_between=1.0)
    check("1 小时掉 10 度（10 度/小时）报掉电异常", fast is not None and "掉电异常" in fast)
    low = scheduler_job.should_warn(prev_odd=None, cur_odd=8.0, threshold=20, drop_per_hour=5)
    check("低于阈值报充值提醒", low is not None and "充值" in low)
    unknown = scheduler_job.should_warn(prev_odd=50.0, cur_odd=40.0, threshold=20,
                                        drop_per_hour=5, hours_between=None)
    check("间隔未知时退回按差值判断（保守报）", unknown is not None)

    # 时间戳必须是 yyyyMMddHHmmssSSS：旧实现多了一个字面量 H，上游可能因此报错
    ts = power_service._timestamp()
    datetime.strptime(ts, "%Y%m%d%H%M%S%f")
    check(f"时间戳格式为 17 位纯数字：{ts}", ts.isdigit() and len(ts) == 17)

    rooms = [{"roomfullname": "主分区_1栋A_9层_101"},
             {"roomfullname": "主分区_1栋A_10层_102"}]
    check("关键词唯一命中时返回该房间",
          power_service.pick_room(rooms, "102")["roomfullname"].endswith("102"))
    check("关键词无命中返回 None", power_service.pick_room(rooms, "999") is None)
    check("无关键词且多房间返回 None（让调用方处理）", power_service.pick_room(rooms, "") is None)
    check("无关键词且只有一个房间直接返回",
          power_service.pick_room(rooms[:1], "") is rooms[0])


def test_http_contracts():
    print("[3c] 接口封装契约")

    class FakeResponse:
        def __init__(self, outer, status_code=200):
            self.status_code = status_code
            self._outer = outer
            self.text = "<fake>"

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(response=self)

        def json(self):
            return self._outer

    class FakeSession:
        def __init__(self, responses):
            self.responses = iter(responses)
            self.payloads = []

        def post(self, _url, data, timeout):
            self.payloads.append(data)
            item = next(self.responses)
            if isinstance(item, Exception):
                raise item
            return item

    old_session = power_service._session
    old_sleep = power_service._sleep_backoff
    try:
        power_service._sleep_backoff = lambda *_args: None
        session = FakeSession([FakeResponse({"result_": "true", "body": {"odd": "12.5"}})])
        power_service._session = lambda: session
        body = power_service._post("https://fake", {"param": "{}"}, retries=0)
        check("body 已是对象时可直接解析", body == {"odd": "12.5"})

        session = FakeSession([FakeResponse({"result_": "true", "body": json.dumps({"odd": 9})})])
        power_service._session = lambda: session
        check("body 为 JSON 字符串时可二次解析",
              power_service._post("https://fake", {}, retries=0) == {"odd": 9})

        session = FakeSession([FakeResponse({"result_": "true", "body": "[]"})])
        power_service._session = lambda: session
        try:
            power_service._post("https://fake", {}, retries=0)
        except power_service.PowerSchemaError:
            check("body 非对象时抛出结构异常", True)
        else:
            raise AssertionError("body 非对象未抛异常")

        session = FakeSession([FakeResponse({"result_": "false", "message_": "bad"})])
        power_service._session = lambda: session
        try:
            power_service._post("https://fake", {}, retries=2)
        except power_service.PowerBusinessError:
            check("业务失败不重试", len(session.payloads) == 1)
        else:
            raise AssertionError("业务失败未抛异常")

        session = FakeSession([FakeResponse({}, 503), FakeResponse({"result_": "true", "body": "{}"})])
        power_service._session = lambda: session
        check("HTTP 503 后按策略重试", power_service._post("https://fake", {}, retries=1) == {})
        check("重试请求次数正确", len(session.payloads) == 2)

        api = ApiConfig(command="default")
        session = FakeSession([FakeResponse({"result_": "true", "body": "{\"roomlist\": []}"})])
        power_service._session = lambda: session
        power_service.bind_rooms("acct", 123, command="override", api=api)
        sent = session.payloads[0]
        check("bind_rooms 显式 command 覆盖生效", sent["command"] == "override")
    finally:
        power_service._session = old_session
        power_service._sleep_backoff = old_sleep


# ---------------------------------------------------------------- 4. 扫描风控
def _load_scan_classes():
    """从 main.py 抽出限速/熔断类单独 exec（避免 import astrbot）。"""
    src = open(MAIN_PY, encoding="utf-8").read()
    start = src.index("class _Http:")
    end = src.index("@register(")
    aiohttp_stub = types.SimpleNamespace(ClientSession=object, ClientTimeout=object,
                                        TCPConnector=object)
    ns = {"asyncio": asyncio, "time": time, "random": random,
          "logger": logging.getLogger("test_scan"), "aiohttp": aiohttp_stub}
    exec(compile(src[start:end], "scan_extract", "exec"), ns)
    return ns["_ScanGuard"], ns["_RateLimiter"]


def test_scan_guard():
    print("[4] 搜校限速与熔断")
    _ScanGuard, _RateLimiter = _load_scan_classes()

    async def run():
        # 令牌桶：burst=2、qps=10，取 8 次应耗时约 (8-2)/10 = 0.6s
        limiter = _RateLimiter(10, burst=2)
        t0 = time.monotonic()
        for _ in range(8):
            await limiter.acquire()
        elapsed = time.monotonic() - t0
        check(f"令牌桶限速生效（8 次请求耗时 {elapsed:.2f}s ≈ 0.6s）", 0.4 < elapsed < 1.2)

        # 并发闸门：并发 2，6 个任务各占 0.1s -> 至少 0.3s
        guard = _ScanGuard(concurrency=2, qps=100, max_hits=99)
        t0 = time.monotonic()

        async def task():
            await guard.slot()
            try:
                await asyncio.sleep(0.1)
            finally:
                guard.release()

        await asyncio.gather(*(task() for _ in range(6)))
        elapsed = time.monotonic() - t0
        check(f"并发闸门生效（6 任务耗时 {elapsed:.2f}s ≥ 0.3s）", elapsed >= 0.28)

        # 熔断：连续失败达到阈值 -> 冷却；再达到 -> 中止
        guard2 = _ScanGuard(concurrency=4, qps=100, cooldown=0.2, max_trips=1, fail_threshold=5)
        results = []
        for _ in range(5):
            results.append(await guard2.report(False))
        check("首次连续失败只降温、不中止", all(results) and not guard2.aborted
              and guard2._pause_until > time.monotonic())
        aborted = False
        for _ in range(5):
            if not await guard2.report(False):
                aborted = True
        check("反复失败后主动中止扫描", aborted and guard2.aborted and bool(guard2.reason))

        # 成功探测会重置失败计数
        guard3 = _ScanGuard(concurrency=1, qps=100, fail_threshold=3, max_trips=1)
        await guard3.report(False)
        await guard3.report(True)
        await guard3.report(False)
        check("成功探测重置失败计数（不会误熔断）", guard3._fails == 1 and not guard3.aborted)

    asyncio.run(run())


def main():
    test_secret_box()
    test_storage()
    test_config_and_parse()
    test_warn_and_helpers()
    test_http_contracts()
    test_scan_guard()
    print(f"\n全部 {passed} 项通过（加密后端：{secure_store.SecretBox(secure_store.generate_key()).backend_name}）")


if __name__ == "__main__":
    main()
