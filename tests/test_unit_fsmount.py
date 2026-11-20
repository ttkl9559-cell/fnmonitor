"""ZFS / btrfs 挂载补齐的单元测试。

lsblk 的 MOUNTPOINT 来自 /proc/mounts 的「设备」列，ZFS 挂在 dataset 上、
btrfs 多设备时只记挂载用的那一块盘——两类都会被误判成「未挂载」。
这里用的全部是构造输出，不依赖真实 ZFS/btrfs 环境。
"""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app"))

import server as srv  # noqa: E402


ZPOOL_STATUS = """  pool: trim_37f9b28d-82e9-4747-9f21-cbb6795deea9
 state: ONLINE
config:

\tNAME                                                          STATE     READ WRITE CKSUM
\ttrim_37f9b28d-82e9-4747-9f21-cbb6795deea9                     ONLINE       0     0     0
\t  /dev/sde1                                                   ONLINE       0     0     0

errors: No known data errors
"""

ZPOOL_MIRROR = """  pool: tank
 state: ONLINE
config:

\tNAME        STATE     READ WRITE CKSUM
\ttank        ONLINE       0     0     0
\t  mirror-0  ONLINE       0     0     0
\t    /dev/sdb1  ONLINE       0     0     0
\t    /dev/sdc1  ONLINE       0     0     0

errors: No known data errors
"""

ZFS_LIST = "trim_37f9b28d-82e9-4747-9f21-cbb6795deea9\t/vol10\n"
ZFS_LIST_MULTI = "tank\t/vol4\ntank/data\t/vol4/data\n"

BTRFS_SHOW = """Label: 'data'  uuid: 8f2c1e4a-0000-1111-2222-333344445555
\tTotal devices 2 FS bytes used 1.20TiB
\tdevid    1 size 3.64TiB used 1.20TiB path /dev/sda1
\tdevid    2 size 3.64TiB used 1.20TiB path /dev/sdf1

Label: 'solo'  uuid: 9a8b7c6d-3333-4444-5555-666677778888
\tTotal devices 1 FS bytes used 100.00GiB
\tdevid    1 size 1.82TiB used 100.00GiB path /dev/sdg1
"""


class ParserTest(unittest.TestCase):
    def test_parse_zfs_pools(self):
        self.assertEqual(srv._parse_zfs_pools(ZPOOL_STATUS),
                         {"trim_37f9b28d-82e9-4747-9f21-cbb6795deea9": ["/dev/sde1"]})

    def test_parse_zfs_pools_mirror(self):
        self.assertEqual(srv._parse_zfs_pools(ZPOOL_MIRROR),
                         {"tank": ["/dev/sdb1", "/dev/sdc1"]})

    def test_parse_zfs_datasets_skips_legacy(self):
        got = srv._parse_zfs_datasets("tank\t/vol4\ntank/old\tlegacy\ntank/none\tnone\n")
        self.assertEqual(got, {"tank": "/vol4"})

    def test_parse_btrfs_groups(self):
        self.assertEqual(srv._parse_btrfs_fs(BTRFS_SHOW),
                         [["sda1", "sdf1"], ["sdg1"]])

    def test_parse_btrfs_empty(self):
        self.assertEqual(srv._parse_btrfs_fs(""), [])


class ZfsMemberMountsTest(unittest.TestCase):
    def setUp(self):
        srv._FS_MAP_CACHE.update({"ts": 0.0, "data": {}})

    def tearDown(self):
        srv._FS_MAP_CACHE.update({"ts": 0.0, "data": {}})

    def _run(self, outputs):
        with mock.patch.object(srv.shutil, "which", return_value="/usr/sbin/zpool"), \
             mock.patch.object(srv, "run_cmd", side_effect=outputs):
            return srv._zfs_member_mounts()

    def test_single_disk_pool(self):
        got = self._run([(ZPOOL_STATUS, 0), (ZFS_LIST, 0)])
        self.assertEqual(got, {"sde1": ["/vol10"]})

    def test_mirror_members_share_mountpoint(self):
        got = self._run([(ZPOOL_MIRROR, 0), (ZFS_LIST_MULTI, 0)])
        self.assertEqual(got["sdb1"], ["/vol4", "/vol4/data"])
        self.assertEqual(got["sdc1"], ["/vol4", "/vol4/data"])

    def test_no_zpool_binary(self):
        with mock.patch.object(srv.shutil, "which", return_value=None):
            self.assertEqual(srv._zfs_member_mounts(), {})

    def test_zpool_failure_is_silent(self):
        self.assertEqual(self._run([("", -1), ("", -1)]), {})


class BtrfsMemberMountsTest(unittest.TestCase):
    BY_DEV = {"sda1": ["/vol1"], "sdf1": [], "sdg1": ["/vol2"]}

    def _run(self, by_dev=None):
        with mock.patch.object(srv.shutil, "which", return_value="/usr/bin/btrfs"), \
             mock.patch.object(srv, "run_cmd", return_value=(BTRFS_SHOW, 0)):
            return srv._btrfs_member_mounts(by_dev if by_dev is not None else self.BY_DEV)

    def test_other_member_gets_same_mountpoint(self):
        got = self._run()
        self.assertEqual(got["sda1"], ["/vol1"])
        self.assertEqual(got["sdf1"], ["/vol1"])   # 这一块在 lsblk 里是空的

    def test_single_device_fs_ignored(self):
        self.assertNotIn("sdg1", self._run())

    def test_unmounted_fs_ignored(self):
        self.assertEqual(self._run({"sda1": [], "sdf1": []}), {})


class FsMemberMountsTest(unittest.TestCase):
    """fs_member_mounts：把两类来源合起来，并且只认 / 开头的挂载点。"""

    def setUp(self):
        srv._FS_MAP_CACHE.update({"ts": 0.0, "data": {}})

    def tearDown(self):
        srv._FS_MAP_CACHE.update({"ts": 0.0, "data": {}})

    def test_merges_sources(self):
        mounts = [{"device": "/dev/sda1", "mount": "/vol1", "fs": "btrfs"},
                  {"device": "trim_x", "mount": "/vol10", "fs": "zfs"}]
        with mock.patch.object(srv, "is_linux", return_value=True), \
             mock.patch.object(srv, "read_mounts", return_value=mounts), \
             mock.patch.object(srv, "_btrfs_member_mounts", return_value={"sdf1": ["/vol1"]}), \
             mock.patch.object(srv, "_zfs_member_mounts", return_value={"sde1": ["/vol10"]}):
            got = srv.fs_member_mounts()
        self.assertEqual(got, {"sdf1": ["/vol1"], "sde1": ["/vol10"]})


class CollectDetailZfsTest(unittest.TestCase):
    """collect_disks_detail：ZFS 成员盘不再是「未挂载」。"""

    LSBLK = json.dumps({"blockdevices": [{
        "name": "sde", "model": "MG08SDA400NY", "vendor": "TOSHIBA",
        "size": 4000787030016, "fstype": None, "mountpoint": None,
        "ro": False, "type": "disk", "serial": "42L0A01CFYZG",
        "children": [{"name": "sde1", "size": 4000787030016, "type": "part",
                      "fstype": "zfs_member", "mountpoint": None}],
    }]})

    def setUp(self):
        srv._DISK_HEALTH_MEM.clear()
        srv._DISK_TEMP_CACHE.clear()

    def tearDown(self):
        srv._DISK_HEALTH_MEM.clear()
        srv._DISK_TEMP_CACHE.clear()

    def test_zfs_disk_gets_mountpoint_and_usage(self):
        usage = [{"mount": "/vol10", "fs": "zfs", "total": 3870000000000,
                  "used": 1670000000000, "percent": 43.2, "fsid": 1}]
        with mock.patch.object(srv, "run_cmd", return_value=(self.LSBLK, 0)), \
             mock.patch.object(srv, "collect_disks", return_value=usage), \
             mock.patch.object(srv, "fs_member_mounts", return_value={"sde1": ["/vol10"]}):
            rows = srv.collect_disks_detail(with_temps=False, with_states=False)
        row = rows[0]
        self.assertEqual(row["mounts"], ["/vol10"])
        self.assertEqual(row["total"], 3870000000000)
        self.assertEqual(row["percent"], 43.2)
        self.assertEqual(row["serial"], "42L0A01CFYZG")


class MountDiskMapTest(unittest.TestCase):
    """休眠保护用「挂载点 → 物理盘」，ZFS 盘也必须能归到。/vol10 归 sde。"""

    LSBLK = json.dumps({"blockdevices": [
        {"name": "sde", "type": "disk", "mountpoint": None, "children": []}]})

    def setUp(self):
        with srv._MOUNT_DISK_LOCK:
            srv._MOUNT_DISK_CACHE.update({"ts": 0.0, "mounts": {}})

    def tearDown(self):
        with srv._MOUNT_DISK_LOCK:
            srv._MOUNT_DISK_CACHE.update({"ts": 0.0, "mounts": {}})

    def test_zfs_volume_mapped_to_disk(self):
        with mock.patch.object(srv, "read_mounts", return_value=[]), \
             mock.patch.object(srv, "run_cmd", return_value=(self.LSBLK, 0)), \
             mock.patch.object(srv, "fs_member_mounts", return_value={"sde1": ["/vol10"]}):
            got = srv._mount_disk_map()
        self.assertEqual(got.get("/vol10"), {"sde"})
