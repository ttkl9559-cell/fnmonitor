"""Unit tests: UPS 模块（NUT 解析 / 状态位 / 无效帧 / 健康评估 / 告警状态机 /
关机保护判定 / 冲突检测 / 配置夹取 / 事件流水）。

全部用假的 upsc 输出与伪造的 NUT 配置文件，不依赖真实 NUT、不碰 /etc/nut、
不下发任何关机命令。
"""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from fnm_support import load_server

srv = load_server()

# 一台 APC Back-UPS 的典型变量集（在线、充电中）
RAW_OL = {
    "ups.status": "OL CHRG",
    "input.voltage": "230.5",
    "input.frequency": "50.0",
    "output.voltage": "230.0",
    "output.current": "1.2",
    "output.frequency": "50.0",
    "ups.load": "31",
    "ups.realpower": "200",
    "battery.charge": "100",
    "battery.runtime": "1700",
    "battery.voltage": "27.2",
    "battery.voltage.nominal": "24",
    "battery.temperature": "24.5",
    "ups.power.nominal": "1500",
    "ups.mfr": "APC",
    "ups.model": "Back-UPS 1500",
    "ups.serial": "AS123456",
    "driver.name": "usbhid-ups",
    "driver.version": "2.8.0",
}

DEVICE_CONF_ON = """# 飞牛生成的 UPS 配置
enable=true
shutdown-ups.enabled=true
power-policy.type=percent
power-policy.value=40
last-device.name=apc
"""

DEVICE_CONF_OFF = """enable=false
shutdown-ups.enabled=false
last-device.name=apc
"""

UPSMON_CONF_ARMED = """MONITOR apc@localhost 1 monuser secret primary
MINSUPPLIES 1
SHUTDOWNCMD "/sbin/shutdown -h +0"
NOTIFYCMD /usr/sbin/upssched
"""

UPSMON_CONF_IDLE = """MINSUPPLIES 1
SHUTDOWNCMD "/sbin/shutdown -h +0"
"""


def _now_at(hh, mm):
    """今天的 hh:mm 对应的本地时间戳（静默时段测试用）。"""
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hh, mm, 0, 0, 0, -1))


class UpsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fnm-ups-")
        # 全局状态全清，避免测试间互相污染
        srv._UPS_EVENTS.clear()
        srv._UPS_LATEST.clear()
        srv._UPS_CONFLICT_CACHE["data"] = None
        srv._UPS_CONFLICT_CACHE["ts"] = 0.0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        srv._UPS_EVENTS.clear()
        srv._UPS_LATEST.clear()

    def write(self, name, text):
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def read_json(self, name):
        with open(os.path.join(self.tmp, name), "r", encoding="utf-8") as f:
            return json.load(f)

    def patch_confs(self, device=None, upsmon=None, upssched=None, procs=()):
        """把三份 NUT 配置指向临时文件；procs 模拟 /proc 扫到的 upsmon 进程。"""
        paths = {}
        for key, const, text in (("device", "UPS_NUT_DEVICE_CONF", device),
                                 ("upsmon", "UPS_NUT_UPSMON_CONF", upsmon),
                                 ("upssched", "UPS_NUT_UPSSCHED_CONF", upssched)):
            p = os.path.join(self.tmp, key + ".conf")
            if text is not None:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(text)
            paths[const] = p
        stack = []
        for const, p in paths.items():
            stack.append(mock.patch.object(srv, const, p))
        stack.append(mock.patch.object(srv, "ups_procs_named",
                                       lambda names: list(procs)))
        for m in stack:
            m.start()
            self.addCleanup(m.stop)
        srv._UPS_CONFLICT_CACHE["data"] = None
        srv._UPS_CONFLICT_CACHE["ts"] = 0.0


# ---------------------------------------------------------------------------
# NUT 解析
# ---------------------------------------------------------------------------
class TestNutParse(UpsTestBase):
    def test_no_upsc_means_no_nut(self):
        with mock.patch.object(srv, "ups_upsc", lambda: ""):
            with self.assertRaises(srv.UpsError) as cm:
                srv.ups_list()
        self.assertEqual(cm.exception.code, "no_nut")

    def test_list_plain_names(self):
        with mock.patch.object(srv, "ups_upsc", lambda: "/usr/bin/upsc"), \
             mock.patch.object(srv, "ups_run", lambda a, timeout=8: (0, "apc\ncyber\n", "")):
            self.assertEqual(srv.ups_list(), ["apc", "cyber"])

    def test_list_name_with_colon_is_stripped(self):
        # 新版 NUT 会输出 "名称: 描述"；右侧要取名称且不能带前导空格
        with mock.patch.object(srv, "ups_upsc", lambda: "/usr/bin/upsc"), \
             mock.patch.object(srv, "ups_run",
                               lambda a, timeout=8: (0, "apc: my apc@localhost\n", "")):
            self.assertEqual(srv.ups_list(), ["my apc@localhost"])

    def test_list_blank_output_is_no_device(self):
        with mock.patch.object(srv, "ups_upsc", lambda: "/usr/bin/upsc"), \
             mock.patch.object(srv, "ups_run", lambda a, timeout=8: (0, "\n \n", "")):
            with self.assertRaises(srv.UpsError) as cm:
                srv.ups_list()
        self.assertEqual(cm.exception.code, "no_device")

    def test_list_connection_refused_is_no_nut(self):
        with mock.patch.object(srv, "ups_upsc", lambda: "/usr/bin/upsc"), \
             mock.patch.object(srv, "ups_run",
                               lambda a, timeout=8: (1, "", "Error: Connection refused")):
            with self.assertRaises(srv.UpsError) as cm:
                srv.ups_list()
        self.assertEqual(cm.exception.code, "no_nut")

    def test_list_other_failure_is_error(self):
        with mock.patch.object(srv, "ups_upsc", lambda: "/usr/bin/upsc"), \
             mock.patch.object(srv, "ups_run", lambda a, timeout=8: (1, "", "boom")):
            with self.assertRaises(srv.UpsError) as cm:
                srv.ups_list()
        self.assertEqual(cm.exception.code, "error")

    def test_pick_prefers_configured_name(self):
        with mock.patch.object(srv, "ups_list", lambda: ["a", "b"]):
            self.assertEqual(srv.ups_pick({"ups_name": "b"}), ("b", ["a", "b"]))
            # 配的名字已不存在（换过机器）→ 回落第一台，不报错
            self.assertEqual(srv.ups_pick({"ups_name": "gone"}), ("a", ["a", "b"]))
            self.assertEqual(srv.ups_pick({}), ("a", ["a", "b"]))

    def test_read_vars_parses_key_value(self):
        out = "ups.status: OL CHRG\nbattery.charge: 100\nbad key: v\nnoise\n"
        with mock.patch.object(srv, "ups_upsc", lambda: "/usr/bin/upsc"), \
             mock.patch.object(srv, "ups_run", lambda a, timeout=8: (0, out, "")):
            d = srv.ups_read_vars("apc")
        self.assertEqual(d["ups.status"], "OL CHRG")
        self.assertEqual(d["battery.charge"], "100")
        self.assertNotIn("bad key", d)      # 键里带空格的一律丢弃

    def test_read_vars_error_mapping(self):
        cases = [("Error: Data stale", "stale"),
                 ("Error: Driver not connected", "stale"),
                 ("Error: Unknown UPS", "no_device"),
                 ("Error: Access denied", "error")]
        for err, code in cases:
            with mock.patch.object(srv, "ups_upsc", lambda: "/usr/bin/upsc"), \
                 mock.patch.object(srv, "ups_run", lambda a, timeout=8: (1, "", err)):
                with self.assertRaises(srv.UpsError) as cm:
                    srv.ups_read_vars("apc")
            self.assertEqual(cm.exception.code, code, err)

    def test_read_vars_empty_output_is_error(self):
        with mock.patch.object(srv, "ups_upsc", lambda: "/usr/bin/upsc"), \
             mock.patch.object(srv, "ups_run", lambda a, timeout=8: (0, "", "")):
            with self.assertRaises(srv.UpsError) as cm:
                srv.ups_read_vars("apc")
        self.assertEqual(cm.exception.code, "error")

    def test_run_maps_missing_binary_and_timeout(self):
        self.assertEqual(srv.ups_run(["/nonexistent/x"], timeout=1)[0], -2)
        self.assertEqual(srv.ups_run(["python", "-c", "import time;time.sleep(5)"],
                                    timeout=1)[0], -3)

    def test_fnum_never_invents_zero(self):
        for bad in (None, "", "  ", "abc", "-", "--", "1.2.3", "N/A"):
            self.assertIsNone(srv.ups_fnum(bad), repr(bad))
        self.assertEqual(srv.ups_fnum("230.5"), 230.5)
        self.assertEqual(srv.ups_fnum("-3"), -3.0)
        self.assertEqual(srv.ups_fnum(" 0 "), 0.0)


# ---------------------------------------------------------------------------
# 状态位与快照
# ---------------------------------------------------------------------------
class TestStatusAndBuild(UpsTestBase):
    def test_status_bits(self):
        self.assertEqual(srv.ups_decode_status("OL CHRG"), 17)
        self.assertEqual(srv.ups_decode_status("OB DISCHRG"), 34)
        self.assertEqual(srv.ups_decode_status("OL CHRG BOGUS"), 17)   # 未知词忽略
        self.assertEqual(srv.ups_decode_status(""), 0)
        self.assertEqual(srv.ups_decode_status(None), 0)
        self.assertEqual(srv.ups_decode_status("FSD"), srv.UPS_BIT_FSD)

    def test_status_labels_and_codes(self):
        mask = srv.ups_decode_status("OB LB DISCHRG")
        self.assertEqual(srv.ups_status_labels(mask), ["电池供电", "低电量", "放电中"])
        self.assertEqual(srv.ups_status_codes(mask), ["OB", "LB", "DISCHRG"])

    def test_build_normal_frame(self):
        snap = srv.ups_build("apc", RAW_OL, srv.ups_normalize({}))
        live = snap["live"]
        self.assertTrue(live["valid"])
        self.assertFalse(live["volatile"])
        self.assertEqual(live["out_v"], 230.0)
        self.assertEqual(live["load"], 31.0)
        self.assertEqual(snap["device"]["mfr"], "APC")
        self.assertEqual(snap["device"]["driver"], "usbhid-ups")
        self.assertEqual(snap["device"]["name"], "apc")

    def test_build_drops_zero_voltage_frame(self):
        raw = dict(RAW_OL, **{"output.voltage": "0", "input.voltage": "0",
                              "ups.load": "0"})
        live = srv.ups_build("apc", raw, srv.ups_normalize({}))["live"]
        self.assertFalse(live["valid"])
        self.assertTrue(live["volatile"])
        for k in ("in_v", "in_hz", "out_v", "out_a", "out_hz", "load", "rp",
                  "nominal_va"):
            self.assertIsNone(live[k], k)
        # 电量/续航/电池电压不受影响：它们是判断市电状态的依据，必须留着
        self.assertEqual(live["chg"], 100.0)
        self.assertEqual(live["bat_v"], 27.2)

    def test_build_missing_output_voltage_is_valid(self):
        raw = dict(RAW_OL)
        del raw["output.voltage"]
        live = srv.ups_build("apc", raw, srv.ups_normalize({}))["live"]
        self.assertTrue(live["valid"])

    def test_build_derives_nominal_voltage_from_battery(self):
        raw = dict(RAW_OL)
        del raw["battery.voltage.nominal"]
        snap = srv.ups_build("apc", raw, srv.ups_normalize({}))
        self.assertEqual(snap["health"]["nominal_wh"], 24 * 9.0)   # 27.2V → 按 24V 组串


# ---------------------------------------------------------------------------
# 电池健康评估（Peukert 区间判定）
# ---------------------------------------------------------------------------
class TestHealth(UpsTestBase):
    cfg = None

    def setUp(self):
        UpsTestBase.setUp(self)
        self.cfg = srv.ups_normalize({})

    def assess(self, chg, rt, rp, extra_raw=None, bat_v=27.2, temp=None, status="OL CHRG"):
        raw = dict(RAW_OL, **{"battery.charge": str(chg), "battery.runtime": str(rt),
                              "ups.realpower": str(rp), "ups.status": status})
        raw.update(extra_raw or {})
        live = {"chg": chg, "rt": rt, "rp": rp, "temp": temp, "bat_v": bat_v,
                "st": srv.ups_decode_status(status), "valid": True}
        return srv.ups_assess(live, raw, self.cfg)

    def test_missing_nominal_is_unknown(self):
        a = self.assess(100, 1700, 200, extra_raw={"battery.voltage.nominal": ""},
                        bat_v=None)
        self.assertEqual(a["verdict"], "unknown")
        self.assertIsNone(a["score"])
        self.assertIn("缺少标称电压", " ".join(a["notes"]))

    def test_no_load_or_charge_is_unknown(self):
        a = self.assess(0, 1700, 200)
        self.assertEqual(a["verdict"], "unknown")
        self.assertIsNone(a["score"])

    def test_expected_range_is_bracketed(self):
        a = self.assess(100, 1700, 200)
        lo, hi = a["expect_runtime_s"]
        self.assertLess(lo, hi)
        self.assertEqual(a["peukert_k"], list(srv.UPS_PEUKERT_RANGE))
        # 倍率越低（k 越小）修正后可用容量越大 → 期望续航上限
        self.assertGreater(hi - lo, 300)

    def test_in_range_is_ok(self):
        a = self.assess(100, 1700, 200)
        self.assertEqual(a["verdict"], "ok")
        self.assertEqual(a["grade"], "good")
        self.assertGreaterEqual(a["score"], 90)

    def test_slight_loss_is_watch(self):
        a = self.assess(100, 1200, 200)
        self.assertEqual(a["verdict"], "watch")       # 落在 exp_lo*0.85 ~ exp_lo
        self.assertLess(a["score"], 90)

    def test_heavy_loss_is_suspect(self):
        a = self.assess(100, 900, 200)
        self.assertEqual(a["verdict"], "suspect")
        self.assertLess(a["score"], 55)

    def test_rb_flag_costs_fifteen_points(self):
        base = self.assess(100, 1700, 200)
        rb = self.assess(100, 1700, 200, status="OL CHRG RB")
        self.assertEqual(base["verdict"], rb["verdict"])     # 续航没变，结论不变
        self.assertEqual(rb["score"], base["score"] - 15)
        self.assertIn("RB", " ".join(rb["notes"]))

    def test_hot_battery_costs_points(self):
        base = self.assess(100, 1700, 200)
        hot = self.assess(100, 1700, 200, temp=40.0)
        self.assertLess(hot["score"], base["score"])
        self.assertIn("电池温度", " ".join(hot["notes"]))

    def test_partial_charge_is_scaled_to_full(self):
        # 50% 电量、续航 850 秒 → 折算满电 1700 秒，结论应与满电一致
        a = self.assess(50, 850, 200)
        self.assertEqual(a["verdict"], "ok")
        self.assertEqual(a["full_runtime_s"], 1700)

    def test_too_light_load_is_unreliable(self):
        a = self.assess(100, 1700, 30)
        self.assertEqual(a["verdict"], "unknown")
        self.assertIn("低于可靠测算区间", " ".join(a["notes"]))

    def test_extreme_c_rate_is_unreliable(self):
        a = self.assess(100, 300, 3000)
        self.assertEqual(a["verdict"], "unknown")
        self.assertIn("放电倍率过高", " ".join(a["notes"]))

    def test_manual_peukert_k_is_used(self):
        cfg = srv.ups_normalize({"peukert_k": 1.25})
        raw = dict(RAW_OL, **{"battery.charge": "100", "battery.runtime": "1700",
                              "ups.realpower": "200"})
        live = {"chg": 100, "rt": 1700, "rp": 200, "temp": None, "bat_v": 27.2,
                "st": 1, "valid": True}
        a = srv.ups_assess(live, raw, cfg)
        self.assertEqual(a["peukert_k"], 1.25)
        lo, hi = a["expect_runtime_s"]
        self.assertLess(hi - lo, 100)      # 单一 k → 区间收窄为一点


# ---------------------------------------------------------------------------
# 配置归一化与夹取
# ---------------------------------------------------------------------------
class TestConfig(UpsTestBase):
    def test_defaults_when_missing(self):
        cfg = srv.ups_load(self.tmp)
        self.assertEqual(cfg["enabled"], 0)
        self.assertEqual(cfg["poll_interval"], 5)
        self.assertEqual(cfg["guard_mode"], "notify")
        self.assertEqual(len(cfg["notify_events"]), len(srv.UPS_NOTIFY_CHOICES))

    def test_bad_json_falls_back(self):
        self.write("ups.json", "{not json")
        self.assertEqual(srv.ups_load(self.tmp)["poll_interval"], 5)

    def test_json_array_falls_back(self):
        self.write("ups.json", "[1,2,3]")
        self.assertEqual(srv.ups_load(self.tmp)["enabled"], 0)

    def test_numeric_clamping(self):
        cfg = srv.ups_normalize({"poll_interval": 1, "notify_max_per_min": 0,
                                 "guard_time_min": 999, "guard_grace_s": 1,
                                 "guard_min_ob_s": 5, "guard_boot_hold_s": -1,
                                 "battery_ah": 0.1, "battery_dod": 2.0,
                                 "battery_eff": 0.1})
        self.assertEqual(cfg["poll_interval"], 3)
        self.assertEqual(cfg["notify_max_per_min"], 1)
        self.assertEqual(cfg["guard_time_min"], 120)
        self.assertEqual(cfg["guard_grace_s"], 10)
        self.assertEqual(cfg["guard_min_ob_s"], 10)
        self.assertEqual(cfg["guard_boot_hold_s"], 0)
        self.assertEqual(cfg["battery_ah"], 0.5)
        self.assertEqual(cfg["battery_dod"], 1.0)
        self.assertEqual(cfg["battery_eff"], 0.5)

    def test_guard_low_v_hard_floor(self):
        # 硬下限双保险之一：用户把阈值填到 15V 也抬回 21.4V
        self.assertEqual(srv.ups_normalize({"guard_low_v": 15})["guard_low_v"], 21.4)
        self.assertEqual(srv.ups_normalize({"guard_low_v": -5})["guard_low_v"], 21.4)
        self.assertEqual(srv.ups_normalize({"guard_low_v": 99})["guard_low_v"], 30.0)
        self.assertEqual(srv.ups_normalize({"guard_low_v": 23.5})["guard_low_v"], 23.5)
        self.assertEqual(srv.ups_normalize({"guard_low_v": "abc"})["guard_low_v"], 22.6)

    def test_onoff_parsing(self):
        for yes in (1, "1", "true", "ON", "yes", True):
            self.assertEqual(srv.ups_onoff(yes), 1, repr(yes))
        for no in (0, "0", "", "false", "off", None, "no"):
            self.assertEqual(srv.ups_onoff(no), 0, repr(no))

    def test_notify_events_filtered_and_deduped(self):
        cfg = srv.ups_normalize({"notify_events": ["POWER_LOSS", "BOGUS",
                                                   "power_loss", " LOW_BATTERY "]})
        self.assertEqual(cfg["notify_events"], ["POWER_LOSS", "LOW_BATTERY"])
        # 逗号串也接受
        cfg = srv.ups_normalize({"notify_events": "POWER_LOSS,DEVICE_LOST"})
        self.assertEqual(cfg["notify_events"], ["POWER_LOSS", "DEVICE_LOST"])

    def test_guard_mode_whitelist(self):
        self.assertEqual(srv.ups_normalize({"guard_mode": "SHUTDOWN"})["guard_mode"],
                         "shutdown")
        self.assertEqual(srv.ups_normalize({"guard_mode": "rm -rf /"})["guard_mode"],
                         "notify")

    def test_peukert_k_below_one_means_auto(self):
        self.assertEqual(srv.ups_normalize({"peukert_k": 1.0})["peukert_k"], 0.0)
        self.assertEqual(srv.ups_normalize({"peukert_k": 0.8})["peukert_k"], 0.0)
        self.assertEqual(srv.ups_normalize({"peukert_k": 1.2})["peukert_k"], 1.2)
        self.assertEqual(srv.ups_normalize({"peukert_k": 9})["peukert_k"], 2.0)

    def test_quiet_window_format(self):
        self.assertEqual(srv.ups_quiet_ok("22:00-6:00"), "22:00-06:00")
        self.assertEqual(srv.ups_quiet_ok(" 8:05 - 9:05 "), "08:05-09:05")
        for bad in ("", "abc", "24:00-06:00", "10:70-11:00", "10:00", "10->11",
                    "8:5-9:05"):          # 分钟必须两位
            self.assertEqual(srv.ups_quiet_ok(bad), "", repr(bad))

    def test_quiet_hit_window_math(self):
        self.assertTrue(srv.ups_quiet_hit({"notify_quiet": "09:00-17:00"}, _now_at(12, 0)))
        self.assertFalse(srv.ups_quiet_hit({"notify_quiet": "09:00-17:00"}, _now_at(18, 0)))
        # 跨零点
        self.assertTrue(srv.ups_quiet_hit({"notify_quiet": "22:00-06:00"}, _now_at(23, 0)))
        self.assertTrue(srv.ups_quiet_hit({"notify_quiet": "22:00-06:00"}, _now_at(3, 0)))
        self.assertFalse(srv.ups_quiet_hit({"notify_quiet": "22:00-06:00"}, _now_at(12, 0)))
        # 空窗 / 起止相同 → 不静默
        self.assertFalse(srv.ups_quiet_hit({"notify_quiet": ""}, _now_at(12, 0)))
        self.assertFalse(srv.ups_quiet_hit({"notify_quiet": "10:00-10:00"}, _now_at(10, 0)))

    def test_public_config_has_no_secrets_and_stable_extras(self):
        pub = srv.ups_public(srv.ups_normalize({}))
        for k in ("events_meta", "choices", "floor_v", "upsc", "nut_state"):
            self.assertIn(k, pub)
        self.assertEqual(pub["floor_v"], 21.4)
        self.assertEqual({e["code"] for e in pub["events_meta"]},
                         set(srv.UPS_EVENT_META))
        self.assertEqual(pub["nut_state"], "off")

    def test_off_snapshot_shape_is_stable(self):
        snap = srv.ups_off_snapshot(srv.ups_normalize({}))
        self.assertEqual(set(snap), {"ok", "enabled", "state", "ts", "stale", "error",
                                     "hint", "device", "live", "health", "guard",
                                     "config", "upsc", "ups_list"})
        self.assertEqual(snap["state"], "off")
        self.assertFalse(snap["ok"])

    def test_save_persists_and_merges(self):
        with mock.patch.object(srv, "ups_poller_ensure", lambda *a, **k: None):
            cfg, err = srv.ups_save(self.tmp, {"battery_ah": 12})
            self.assertEqual(err, "")
            self.assertEqual(cfg["battery_ah"], 12.0)
            # 合并保存：第二次只改一个字段，其余保留
            cfg, err = srv.ups_save(self.tmp, {"poll_interval": 10})
            self.assertEqual(err, "")
            self.assertEqual(cfg["poll_interval"], 10)
            self.assertEqual(cfg["battery_ah"], 12.0)
        self.assertEqual(srv.ups_load(self.tmp)["poll_interval"], 10)

    def test_save_blocked_when_fnOS_still_manages_ups(self):
        self.patch_confs(device=DEVICE_CONF_ON, procs=(4242,))
        self.write("ups.json", json.dumps({"enabled": 0}))
        before = os.path.getmtime(os.path.join(self.tmp, "ups.json"))
        with mock.patch.object(srv, "ups_poller_ensure", lambda *a, **k: None):
            cfg, err = srv.ups_save(self.tmp, {"guard_enabled": 1,
                                               "guard_mode": "shutdown"})
        self.assertIn("飞牛", err)
        self.assertEqual(cfg["guard_enabled"], 0)          # 返回的是旧配置
        self.assertEqual(os.path.getmtime(os.path.join(self.tmp, "ups.json")), before)
        self.assertEqual(srv.ups_load(self.tmp)["guard_mode"], "notify")

    def test_save_allows_shutdown_after_acknowledging_conflict(self):
        self.patch_confs(device=DEVICE_CONF_ON, procs=(4242,))
        with mock.patch.object(srv, "ups_poller_ensure", lambda *a, **k: None):
            cfg, err = srv.ups_save(self.tmp, {"guard_enabled": 1,
                                               "guard_mode": "shutdown",
                                               "guard_allow_conflict": 1})
        self.assertEqual(err, "")
        self.assertEqual(cfg["guard_mode"], "shutdown")

    def test_save_notify_mode_is_never_blocked(self):
        self.patch_confs(device=DEVICE_CONF_ON, procs=(4242,))
        with mock.patch.object(srv, "ups_poller_ensure", lambda *a, **k: None):
            cfg, err = srv.ups_save(self.tmp, {"guard_enabled": 1,
                                               "guard_mode": "notify"})
        self.assertEqual(err, "")
        self.assertEqual(cfg["guard_enabled"], 1)

    def test_poller_ensure_does_not_spawn_when_disabled(self):
        # 「未启用零副作用」的唯一保证点：不建线程
        srv.ups_poller_stop()
        self.assertIsNone(srv.ups_poller_ensure(self.tmp, {}))
        self.assertIsNone(srv._UPS_POLLER)


# ---------------------------------------------------------------------------
# 冲突检测
# ---------------------------------------------------------------------------
class TestConflicts(UpsTestBase):
    def test_fnOS_enabled_blocks_shutdown(self):
        self.patch_confs(device=DEVICE_CONF_ON, procs=(4242,))
        c = srv.ups_guard_conflicts()
        self.assertEqual(c["risk"], "high")
        self.assertTrue(c["block_shutdown"])
        keys = {i["key"] for i in c["items"]}
        self.assertIn("fnos_enable", keys)
        self.assertIn("fnos_shutdown", keys)
        blocked, why = srv.ups_conflict_blocks(c)
        self.assertTrue(blocked)
        self.assertIn("飞牛", why)

    def test_fnOS_off_is_no_risk(self):
        self.patch_confs(device=DEVICE_CONF_OFF)
        c = srv.ups_guard_conflicts()
        self.assertEqual(c["risk"], "none")
        self.assertFalse(c["block_shutdown"])
        self.assertEqual(c["items"], [])
        self.assertFalse(srv.ups_conflict_blocks(c)[0])

    def test_unreadable_fnOS_conf_is_warn_only(self):
        # Windows / 非 root 场景：读不到 /etc/nut/device.conf 只提示，绝不阻断
        self.patch_confs(device=None)
        c = srv.ups_guard_conflicts()
        self.assertFalse(c["fnos"]["available"])
        self.assertEqual(c["risk"], "none")
        self.assertFalse(c["block_shutdown"])
        lv = {i["key"]: i["level"] for i in c["items"]}
        self.assertEqual(lv.get("fnos_unknown"), "warn")

    def test_upsmon_with_monitor_is_medium(self):
        self.patch_confs(device=DEVICE_CONF_OFF, upsmon=UPSMON_CONF_ARMED, procs=(11,))
        c = srv.ups_guard_conflicts()
        self.assertEqual(c["risk"], "medium")
        self.assertFalse(c["block_shutdown"])       # medium 只提示
        self.assertIn("nut_upsmon", {i["key"] for i in c["items"]})
        self.assertEqual(c["nut"]["minsupplies"], 1)
        self.assertEqual(c["nut"]["shutdown_cmd"], "/sbin/shutdown -h +0")

    def test_upsmon_without_monitor_is_idle(self):
        # 飞牛关掉 UPS 功能后 upsmon 进程还在，但 MONITOR 被清空 —— 不能报风险
        self.patch_confs(device=DEVICE_CONF_OFF, upsmon=UPSMON_CONF_IDLE, procs=(11,))
        c = srv.ups_guard_conflicts()
        self.assertEqual(c["risk"], "none")
        lv = {i["key"]: i["level"] for i in c["items"]}
        self.assertEqual(lv.get("nut_upsmon_idle"), "info")

    def test_upssched_with_cmdscript_is_medium(self):
        self.patch_confs(device=DEVICE_CONF_OFF, upssched="CMDSCRIPT /etc/nut/x.sh\n")
        c = srv.ups_guard_conflicts()
        self.assertEqual(c["risk"], "medium")
        self.assertTrue(c["nut"]["upssched_ready"])
        self.assertIn("nut_upssched", {i["key"] for i in c["items"]})

    def test_conflict_blocks_only_on_high(self):
        self.assertFalse(srv.ups_conflict_blocks(None)[0])
        self.assertFalse(srv.ups_conflict_blocks({"risk": "medium"})[0])
        self.assertFalse(srv.ups_conflict_blocks({"risk": "none"})[0])
        self.assertTrue(srv.ups_conflict_blocks({"risk": "high"})[0])

    def test_conflict_cache_reused_unless_forced(self):
        self.patch_confs(device=DEVICE_CONF_ON)
        first = srv.ups_conflicts()
        self.assertEqual(first["risk"], "high")
        # 改成无冲突后，60 秒内仍返回缓存
        with open(os.path.join(self.tmp, "device.conf"), "w", encoding="utf-8") as f:
            f.write(DEVICE_CONF_OFF)
        self.assertEqual(srv.ups_conflicts()["risk"], "high")
        self.assertEqual(srv.ups_conflicts(force=True)["risk"], "none")


# ---------------------------------------------------------------------------
# 关机保护判定（纯函数）
# ---------------------------------------------------------------------------
class TestGuardVerdict(UpsTestBase):
    def cfg(self, **kw):
        base = {"guard_enabled": 1, "guard_min_ob_s": 30, "guard_time_min": 10,
                "guard_low_v": 22.6, "guard_grace_s": 60}
        base.update(kw)
        return srv.ups_normalize(base)

    def test_disabled_resets(self):
        st, act, _ = srv.ups_guard_verdict(25.0, True, 900, "watching",
                                           srv.ups_normalize({"guard_enabled": 0}))
        self.assertEqual((st, act), ("idle", "reset"))
        st, act, _ = srv.ups_guard_verdict(25.0, True, 900, "idle",
                                           srv.ups_normalize({"guard_enabled": 0}))
        self.assertEqual((st, act), ("idle", None))

    def test_on_mains_is_idle(self):
        st, act, _ = srv.ups_guard_verdict(27.0, False, 0, "idle", self.cfg())
        self.assertEqual((st, act), ("idle", None))
        st, act, note = srv.ups_guard_verdict(27.0, False, 0, "armed", self.cfg())
        self.assertEqual((st, act), ("idle", "cancel"))
        self.assertIn("市电恢复", note)

    def test_confirmation_window(self):
        st, act, note = srv.ups_guard_verdict(26.0, True, 10, "idle", self.cfg())
        self.assertEqual((st, act), ("watching", None))
        self.assertIn("未满", note)

    def test_time_criterion(self):
        st, act, note = srv.ups_guard_verdict(25.0, True, 600, "watching", self.cfg())
        self.assertEqual((st, act), ("armed", "arm"))
        self.assertIn("超过", note)
        # 差一秒不触发
        st, act, _ = srv.ups_guard_verdict(25.0, True, 599, "watching", self.cfg())
        self.assertEqual((st, act), ("watching", None))

    def test_voltage_criterion(self):
        st, act, note = srv.ups_guard_verdict(22.0, True, 100, "watching", self.cfg())
        self.assertEqual((st, act), ("armed", "arm"))
        self.assertIn("22.0V", note)

    def test_both_criteria(self):
        st, act, note = srv.ups_guard_verdict(21.9, True, 700, "watching", self.cfg())
        self.assertEqual((st, act), ("armed", "arm"))
        self.assertIn("两条判据都到", note)

    def test_unknown_voltage_falls_back_to_time_only(self):
        st, act, _ = srv.ups_guard_verdict(None, True, 700, "watching", self.cfg())
        self.assertEqual((st, act), ("armed", "arm"))
        st, act, _ = srv.ups_guard_verdict(None, True, 100, "watching", self.cfg())
        self.assertEqual((st, act), ("watching", None))

    def test_armed_does_not_retreat_when_voltage_recovers(self):
        # 铅酸带载电压会随负载波动，跟着它反复进出比不判更危险
        for st0 in ("armed", "alerted"):
            st, act, _ = srv.ups_guard_verdict(26.5, True, 700, st0, self.cfg())
            self.assertEqual((st, act), (st0, None))

    def test_zero_voltage_not_treated_as_low(self):
        st, act, _ = srv.ups_guard_verdict(0, True, 100, "watching", self.cfg())
        self.assertEqual((st, act), ("watching", None))


# ---------------------------------------------------------------------------
# 关机保护状态机（UpsGuard）
# ---------------------------------------------------------------------------
class TestGuardMachine(UpsTestBase):
    def make(self, **kw):
        cfg = srv.ups_normalize(dict({"guard_enabled": 1, "guard_min_ob_s": 10,
                                      "guard_time_min": 1, "guard_low_v": 22.6,
                                      "guard_grace_s": 10, "guard_boot_hold_s": 0,
                                      "guard_mode": "notify"}, **kw))
        g = srv.UpsGuard(self.tmp)
        g.override_cmd = "true"          # 绝不真的关机
        g.boot_at = int(time.time()) - 99999
        return g, cfg

    def snap(self, status, bat_v=25.0, chg=80, ts=None):
        st = srv.ups_decode_status(status)
        return {"ts": ts if ts is not None else int(time.time()),
                "device": {"name": "apc", "mfr": "APC", "model": "X"},
                "live": {"st": st, "status": status, "bat_v": bat_v, "chg": chg,
                         "valid": True, "in_v": 0.0, "out_v": 230.0, "load": 30.0,
                         "temp": 24.0, "rt": 900},
                "health": {}}

    def arm(self, g, grace=60):
        """把状态机推进到「已进入倒计时」——这一步正常要分两轮 evaluate 才能到。"""
        g.state = "armed"
        g.state_since = int(time.time())
        g.ob_since = int(time.time()) - 100
        g.armed_at = int(time.time())
        g.deadline = int(time.time()) + grace

    def test_invalid_frame_is_ignored(self):
        g, cfg = self.make()
        s = self.snap("OB DISCHRG", bat_v=21.0)
        s["live"]["valid"] = False
        self.assertFalse(g.evaluate(s, cfg))
        self.assertEqual(g.state, "idle")
        self.assertIsNone(g.ob_since)

    def test_boot_hold_prevents_early_fire(self):
        g, cfg = self.make(guard_boot_hold_s=300)
        g.boot_at = int(time.time())
        g.evaluate(self.snap("OB DISCHRG", bat_v=21.0), cfg)
        self.assertEqual(g.state, "held")

    def test_watching_then_armed(self):
        g, cfg = self.make(guard_min_ob_s=10, guard_time_min=1)
        g.evaluate(self.snap("OB DISCHRG", bat_v=26.0), cfg)
        self.assertEqual(g.state, "watching")
        g.ob_since = int(time.time()) - 70
        g.evaluate(self.snap("OB DISCHRG", bat_v=26.0), cfg)
        self.assertEqual(g.state, "armed")
        self.assertTrue(g.deadline)

    def test_mains_restore_cancels(self):
        g, cfg = self.make()
        g.state = "armed"
        g.deadline = int(time.time()) + 30
        g.evaluate(self.snap("OL CHRG", bat_v=27.0), cfg)
        self.assertEqual(g.state, "idle")
        self.assertIsNone(g.deadline)

    def test_notify_mode_alerts_without_shutdown(self):
        g, cfg = self.make(guard_low_v=22.6, guard_mode="notify")
        self.write("notify.json", json.dumps({"enabled": 0}))
        self.arm(g, grace=-1)
        with mock.patch("subprocess.Popen") as popen:
            g.evaluate(self.snap("OB LB DISCHRG", bat_v=22.0), cfg)
        self.assertEqual(g.state, "alerted")
        popen.assert_not_called()
        self.assertIsNone(g.fired_at)

    def test_arm_starts_grace_period_without_firing(self):
        # 进入倒计时的那一轮只 arm，宽限期内不动手
        g, cfg = self.make(guard_low_v=22.6, guard_mode="shutdown")
        self.patch_confs(device=DEVICE_CONF_OFF)
        self.write("notify.json", json.dumps({"enabled": 0}))
        g.ob_since = int(time.time()) - 100
        with mock.patch("subprocess.Popen") as popen:
            g.evaluate(self.snap("OB LB DISCHRG", bat_v=22.0), cfg)
        self.assertEqual(g.state, "armed")
        popen.assert_not_called()

    def test_shutdown_mode_blocked_when_fnOS_conflicts(self):
        self.patch_confs(device=DEVICE_CONF_ON, procs=(4242,))
        g, cfg = self.make(guard_mode="shutdown", guard_allow_conflict=0)
        self.arm(g, grace=-1)
        with mock.patch("subprocess.Popen") as popen:
            g.evaluate(self.snap("OB LB DISCHRG", bat_v=22.0), cfg)
        self.assertEqual(g.state, "blocked")
        popen.assert_not_called()

    def test_shutdown_mode_fires_once(self):
        self.patch_confs(device=DEVICE_CONF_OFF)
        g, cfg = self.make(guard_mode="shutdown", guard_allow_conflict=0)
        self.arm(g, grace=-1)
        self.write("notify.json", json.dumps({"enabled": 0}))
        with mock.patch("subprocess.Popen") as popen, \
             mock.patch("time.sleep", lambda s: None):
            g.evaluate(self.snap("OB LB DISCHRG", bat_v=22.0), cfg)
            self.assertEqual(g.state, "fired")
            self.assertEqual(popen.call_count, 1)
            g.evaluate(self.snap("OB LB DISCHRG", bat_v=22.0), cfg)   # 不重入
            self.assertEqual(popen.call_count, 1)
        self.assertIsNotNone(g.fired_at)
        # 落盘留档，便于重启后回答「上次到底关没关」
        last = self.read_json("ups_guard_last.json")
        self.assertEqual(last["mode"], "shutdown")
        self.assertEqual(last["bat_v"], 22.0)

    def test_stale_snapshot_aborts_fire(self):
        self.patch_confs(device=DEVICE_CONF_OFF)
        g, cfg = self.make(guard_mode="shutdown")
        self.arm(g, grace=-1)
        self.write("notify.json", json.dumps({"enabled": 0}))
        with mock.patch("subprocess.Popen") as popen, \
             mock.patch("time.sleep", lambda s: None):
            g.evaluate(self.snap("OB LB DISCHRG", bat_v=22.0, ts=int(time.time()) - 60), cfg)
        self.assertEqual(g.state, "armed")          # 快照过期 → 本轮放弃，状态不动
        popen.assert_not_called()

    def test_missing_shutdown_command_blocks(self):
        self.patch_confs(device=DEVICE_CONF_OFF)
        g, cfg = self.make(guard_mode="shutdown")
        g.override_cmd = ""
        self.arm(g, grace=-1)
        self.write("notify.json", json.dumps({"enabled": 0}))
        with mock.patch.object(g, "_resolve_cmd", lambda: None), \
             mock.patch("subprocess.Popen") as popen:
            g.evaluate(self.snap("OB LB DISCHRG", bat_v=22.0), cfg)
        self.assertEqual(g.state, "blocked")
        popen.assert_not_called()

    def test_hard_floor_compresses_deadline(self):
        self.patch_confs(device=DEVICE_CONF_OFF)
        g, cfg = self.make(guard_mode="notify", guard_grace_s=60)
        self.arm(g, grace=300)
        self.write("notify.json", json.dumps({"enabled": 0}))
        before = g.deadline
        g.evaluate(self.snap("OB LB DISCHRG", bat_v=21.2), cfg)
        self.assertLess(g.deadline, before)
        self.assertLessEqual(g.deadline, int(time.time()) + 30)

    def test_cancel_blocks_retrigger_for_ten_minutes(self):
        g, cfg = self.make()
        g.state = "armed"
        g.deadline = int(time.time()) + 30
        ok, msg = g.cancel(cfg)
        self.assertTrue(ok)
        self.assertIn("取消", msg)
        self.assertEqual(g.state, "idle")
        g.evaluate(self.snap("OB LB DISCHRG", bat_v=21.0), cfg)
        self.assertEqual(g.state, "idle")          # 600 秒静默内不再 arm

    def test_cancel_without_pending_is_rejected(self):
        g, cfg = self.make()
        ok, msg = g.cancel(cfg)
        self.assertFalse(ok)
        self.assertIn("没有待执行", msg)

    def test_status_snapshot_is_json_safe(self):
        g, cfg = self.make()
        self.patch_confs(device=DEVICE_CONF_OFF)
        st = g.status(cfg)
        json.dumps(st)          # 必须可序列化
        self.assertIn("conflicts", st)
        self.assertEqual(st["floor_v"], 21.4)
        self.assertEqual(st["state_cn"], "待命")


# ---------------------------------------------------------------------------
# 告警状态机与通知
# ---------------------------------------------------------------------------
class TestNotify(UpsTestBase):
    SNAP = {"ts": int(time.time()),
            "device": {"name": "apc", "mfr": "APC", "model": "Back-UPS", "serial": "AS1"},
            "live": {"valid": True, "st": 2, "status": "OB DISCHRG", "bat_v": 25.4,
                     "chg": 88, "rt": 900, "in_v": 0.0, "out_v": 230.0,
                     "load": 31.0, "temp": 24.5}}

    def cfg(self, **kw):
        base = {"notify_enabled": 1, "notify_debounce_s": 60,
                "notify_max_per_min": 8, "notify_quiet": ""}
        base.update(kw)
        return srv.ups_normalize(base)

    def enable_channel(self):
        self.write("notify.json", json.dumps({"enabled": 1, "host": "smtp.example.com",
                                              "user": "a@b.c", "to": "a@b.c",
                                              "sender": "a@b.c", "password": "x"}))

    def ev(self, etype="POWER_LOSS", level="crit"):
        return {"type": etype, "ts": int(time.time()),
                "label": srv.UPS_EVENT_META[etype][0], "level": level,
                "from_st": "OL CHRG", "to_st": "OB DISCHRG", "chg": 88, "rt": 900,
                "in_v": 0.0, "out_v": 230.0, "load": 31.0, "temp": 24.5,
                "note": "市电中断"}

    def test_no_channel_means_no_queue(self):
        got = srv.ups_notify_after_poll([self.ev()], self.SNAP, self.cfg(), self.tmp)
        self.assertIsNone(got)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "ups_state.json")))
        # 但事件流水必须记下（这是审计，不依赖有没有配通知）
        evs, total = srv.ups_events_load(self.tmp)
        self.assertEqual(total, 1)
        self.assertEqual(evs[0]["type"], "POWER_LOSS")

    def test_notify_disabled_means_no_queue(self):
        self.enable_channel()
        got = srv.ups_notify_after_poll([self.ev()], self.SNAP,
                                        self.cfg(notify_enabled=0), self.tmp)
        self.assertIsNone(got)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "ups_state.json")))

    def test_success_marks_sent(self):
        self.enable_channel()
        with mock.patch.object(srv, "notify_send_all",
                               lambda nc, s, b: (True, {"mail": {"ok": True}})):
            got = srv.ups_notify_after_poll([self.ev()], self.SNAP, self.cfg(), self.tmp)
        self.assertIn("市电中断", got)
        st = self.read_json("ups_state.json")
        self.assertEqual(st["pending"], [])
        self.assertIn("POWER_LOSS", st["sent"])
        self.assertTrue(st["last"]["ok"])

    def test_failure_keeps_pending_and_retries(self):
        self.enable_channel()
        calls = []

        def boom(nc, s, b):
            calls.append(s)
            return False, {"mail": {"ok": False, "err": "timeout"}}

        with mock.patch.object(srv, "notify_send_all", boom):
            srv.ups_notify_after_poll([self.ev()], self.SNAP, self.cfg(), self.tmp)
            st = self.read_json("ups_state.json")
            self.assertEqual(len(st["pending"]), 1)
            self.assertEqual(st["pending"][0]["tries"], 1)
            self.assertEqual(len(calls), 1)

            # 下一轮没有新事件：仍要捞起来重投（边沿事件不会自己再出现）
            st["pending"][0]["next_try"] = 0
            with open(os.path.join(self.tmp, "ups_state.json"), "w", encoding="utf-8") as f:
                json.dump(st, f)
            srv.ups_notify_after_poll([], self.SNAP, self.cfg(notify_debounce_s=0),
                                      self.tmp)
            self.assertEqual(len(calls), 2)
            st = self.read_json("ups_state.json")
            self.assertEqual(st["pending"][0]["tries"], 2)

    def test_retry_backoff_skips_too_soon(self):
        self.enable_channel()
        calls = []
        with mock.patch.object(srv, "notify_send_all",
                               lambda nc, s, b: (calls.append(1), (False, {}))[1]):
            srv.ups_notify_after_poll([self.ev()], self.SNAP, self.cfg(), self.tmp)
            n = len(calls)
            for _ in range(3):
                srv.ups_notify_after_poll([], self.SNAP, self.cfg(notify_debounce_s=0),
                                          self.tmp)
        self.assertEqual(len(calls), n)      # 60 秒退避内不再打扰通道

    def test_gives_up_after_max_tries(self):
        self.enable_channel()
        srv.ups_state_write(self.tmp, {"inited": 1, "ts": 0, "sent": {}, "window": [],
                                       "pending": [{"type": "POWER_LOSS", "ts": 1,
                                                    "tries": srv.UPS_NOTIFY_TRIES,
                                                    "next_try": 0}],
                                       "last": None})
        with mock.patch.object(srv, "notify_send_all",
                               lambda nc, s, b: (self.fail("不该再发"), (True, {}))[1]):
            srv.ups_notify_after_poll([], self.SNAP, self.cfg(), self.tmp)
        self.assertEqual(self.read_json("ups_state.json")["pending"], [])

    def test_idle_round_does_not_touch_state_file(self):
        self.enable_channel()
        with mock.patch.object(srv, "notify_send_all", lambda *a: (True, {})):
            srv.ups_notify_after_poll([], self.SNAP, self.cfg(), self.tmp)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "ups_state.json")))

    def test_debounce_drops_repeat(self):
        self.enable_channel()
        srv.ups_state_write(self.tmp, {"inited": 1, "ts": 0,
                                       "sent": {"POWER_LOSS": time.time()},
                                       "window": [], "pending": [], "last": None})
        with mock.patch.object(srv, "notify_send_all", lambda *a: (True, {})):
            got = srv.ups_notify_after_poll([self.ev()], self.SNAP, self.cfg(), self.tmp)
        self.assertIsNone(got)
        self.assertEqual(self.read_json("ups_state.json")["pending"], [])

    def test_rate_limit_drops_beyond_cap(self):
        self.enable_channel()
        srv.ups_state_write(self.tmp, {"inited": 1, "ts": 0, "sent": {},
                                       "window": [time.time()], "pending": [],
                                       "last": None})
        cfg = self.cfg(notify_debounce_s=0, notify_max_per_min=1)
        with mock.patch.object(srv, "notify_send_all", lambda *a: (True, {})):
            got = srv.ups_notify_after_poll([self.ev("LOW_BATTERY")], self.SNAP, cfg,
                                            self.tmp)
        self.assertIsNone(got)

    def test_quiet_window_blocks_non_critical_only(self):
        self.enable_channel()
        cfg = self.cfg(notify_quiet="00:00-23:59")
        with mock.patch.object(srv, "notify_send_all", lambda *a: (True, {})):
            srv.ups_notify_after_poll([self.ev("REPLACE_BATTERY", "warn")], self.SNAP,
                                      cfg, self.tmp)
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "ups_state.json")))
            srv.ups_notify_after_poll([self.ev("POWER_LOSS", "crit")], self.SNAP, cfg,
                                      self.tmp)
        st = self.read_json("ups_state.json")
        self.assertEqual(st["pending"], [])
        self.assertIn("POWER_LOSS", st["sent"])

    def test_event_not_selected_is_skipped(self):
        self.enable_channel()
        cfg = self.cfg(notify_events=["POWER_RESTORE"])
        with mock.patch.object(srv, "notify_send_all", lambda *a: (True, {})):
            got = srv.ups_notify_after_poll([self.ev("POWER_LOSS")], self.SNAP, cfg,
                                            self.tmp)
        self.assertIsNone(got)

    def test_guard_events_bypass_selection(self):
        # 关机保护告警属安全告警，不受「告警事件」勾选控制
        self.enable_channel()
        cfg = self.cfg(notify_events=["POWER_RESTORE"])
        ev = self.ev("GUARD_ALERT")
        with mock.patch.object(srv, "notify_send_all", lambda *a: (True, {})):
            got = srv.ups_notify_after_poll([ev], self.SNAP, cfg, self.tmp)
        self.assertIn("关机保护告警", got)

    def test_message_contains_key_fields(self):
        subject, body = srv.ups_build_msg(self.ev(), self.SNAP, 8777)
        self.assertTrue(subject.startswith("[飞牛监控] UPS 市电中断"))
        self.assertIn("APC Back-UPS", subject)
        self.assertIn("市电中断", body)
        self.assertIn("OL CHRG → OB DISCHRG", body)
        self.assertIn("88%", body)
        self.assertIn("剩余续航：15 分钟", body)
        self.assertIn("序列号：AS1", body)

    def test_message_handles_missing_values(self):
        ev = {"type": "DEVICE_LOST", "ts": int(time.time())}
        subject, body = srv.ups_build_msg(ev, {}, 8777)
        self.assertIn("UPS 通信中断", subject)
        self.assertIn("检测时间", body)
        self.assertNotIn("电量：", body)      # 缺值整行省略，不写「—」误导
        self.assertNotIn("电压：", body)


# ---------------------------------------------------------------------------
# 边沿事件检测
# ---------------------------------------------------------------------------
class TestDetect(UpsTestBase):
    class Fake:
        def __init__(self):
            self.last_mask = None
            self.last_status = ""

    def snap(self, status, **live):
        base = {"ts": int(time.time()), "device": {"name": "apc"},
                "live": {"status": status, "st": srv.ups_decode_status(status),
                         "chg": 90, "rt": 900, "in_v": 230.0, "out_v": 230.0,
                         "load": 30.0, "temp": 24.0}}
        base["live"].update(live)
        return base

    def test_first_tick_produces_no_event(self):
        p = self.Fake()
        self.assertEqual(srv.ups_detect(p, self.snap("OL CHRG")), [])
        self.assertEqual(p.last_mask, 17)

    def test_power_loss_and_restore(self):
        p = self.Fake()
        srv.ups_detect(p, self.snap("OL CHRG"))
        evs = srv.ups_detect(p, self.snap("OB DISCHRG"))
        self.assertEqual([e["type"] for e in evs], ["POWER_LOSS"])
        self.assertEqual(evs[0]["from_st"], "OL CHRG")
        self.assertEqual(evs[0]["to_st"], "OB DISCHRG")
        evs = srv.ups_detect(p, self.snap("OL CHRG"))
        self.assertEqual([e["type"] for e in evs], ["POWER_RESTORE"])

    def test_low_battery_and_overload(self):
        p = self.Fake()
        srv.ups_detect(p, self.snap("OB DISCHRG"))
        evs = srv.ups_detect(p, self.snap("OB LB DISCHRG"))
        self.assertEqual([e["type"] for e in evs], ["LOW_BATTERY"])
        evs = srv.ups_detect(p, self.snap("OB LB OVER DISCHRG"))
        self.assertEqual([e["type"] for e in evs], ["OVERLOAD"])

    def test_replace_battery_and_fsd(self):
        p = self.Fake()
        srv.ups_detect(p, self.snap("OL CHRG"))
        evs = srv.ups_detect(p, self.snap("OL CHRG RB"))
        self.assertEqual([e["type"] for e in evs], ["REPLACE_BATTERY"])
        evs = srv.ups_detect(p, self.snap("OL CHRG RB FSD"))
        self.assertEqual([e["type"] for e in evs], ["FORCED_SHUTDOWN"])

    def test_no_state_change_no_event(self):
        p = self.Fake()
        srv.ups_detect(p, self.snap("OL CHRG"))
        self.assertEqual(srv.ups_detect(p, self.snap("OL CHRG")), [])

    def test_unknown_bit_change_alone_is_silent(self):
        p = self.Fake()
        srv.ups_detect(p, self.snap("OL CHRG"))
        # 只是充电中→放电中（不涉及 OB 位）不该报市电中断
        self.assertEqual(srv.ups_detect(p, self.snap("OL DISCHRG")), [])


# ---------------------------------------------------------------------------
# 事件流水（环形淘汰 / 落盘 / 回填）
# ---------------------------------------------------------------------------
class TestEvents(UpsTestBase):
    def test_memory_ring_caps_at_max(self):
        for i in range(srv.UPS_EVENT_MAX + 120):
            srv.ups_events_append(None, [{"type": "T%d" % i, "ts": i}])
        self.assertEqual(len(srv._UPS_EVENTS), srv.UPS_EVENT_MAX)
        self.assertEqual(srv._UPS_EVENTS[-1]["type"], "T%d" % (srv.UPS_EVENT_MAX + 119))

    def test_events_load_newest_first(self):
        srv.ups_events_append(self.tmp, [{"type": "A", "ts": 1}, {"type": "B", "ts": 2}])
        evs, total = srv.ups_events_load(self.tmp, 10)
        self.assertEqual(total, 2)
        self.assertEqual([e["type"] for e in evs], ["B", "A"])

    def test_limit_is_clamped(self):
        srv.ups_events_append(self.tmp, [{"type": "A", "ts": i} for i in range(5)])
        evs, _ = srv.ups_events_load(self.tmp, -5)
        self.assertEqual(len(evs), 1)          # 下限 1
        evs, _ = srv.ups_events_load(self.tmp, 999)
        self.assertEqual(len(evs), 5)          # 上限 200，但只有 5 条

    def test_disk_compaction_keeps_tail(self):
        path = os.path.join(self.tmp, "ups_events.jsonl")
        batch = [{"type": "X", "ts": i} for i in range(750)]
        srv.ups_events_append(self.tmp, batch)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 750)      # 未到阈值不压
        srv.ups_events_append(self.tmp, [{"type": "Y", "ts": i} for i in range(100)])
        with open(path, encoding="utf-8") as f:
            rows = f.readlines()
        self.assertEqual(len(rows), srv.UPS_EVENT_MAX)
        self.assertIn('"type": "Y"', rows[-1])

    def test_backfill_from_disk_when_memory_empty(self):
        path = os.path.join(self.tmp, "ups_events.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"type": "OLD", "ts": 1}\n')
            f.write("this is not json\n")
            f.write('[1,2]\n')
            f.write('{"type": "NEW", "ts": 2}\n')
        evs, total = srv.ups_events_load(self.tmp, 10)
        self.assertEqual([e["type"] for e in evs], ["NEW", "OLD"])
        self.assertEqual(total, 2)

    def test_bad_directory_does_not_raise(self):
        srv.ups_events_append("/nonexistent/nope", [{"type": "A", "ts": 1}])
        self.assertEqual(len(srv._UPS_EVENTS), 1)


class TestHelpers(unittest.TestCase):
    def test_guard_dur_format(self):
        self.assertEqual(srv.ups_guard_dur(0), "0 秒")
        self.assertEqual(srv.ups_guard_dur(59), "59 秒")
        self.assertEqual(srv.ups_guard_dur(60), "1 分钟")
        self.assertEqual(srv.ups_guard_dur(90), "1 分 30 秒")
        self.assertEqual(srv.ups_guard_dur(3600), "1 小时")
        self.assertEqual(srv.ups_guard_dur(5400), "1 小时 30 分")
        self.assertEqual(srv.ups_guard_dur("x"), "—")

    def test_risk_cn(self):
        self.assertEqual(srv.ups_risk_cn("high"), "有冲突")
        self.assertEqual(srv.ups_risk_cn("none"), "无冲突")
        self.assertEqual(srv.ups_risk_cn("weird"), "weird")


if __name__ == "__main__":
    unittest.main()
