#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
时迹 TimeTrace — RustTimeNoter 的时间线查看器

RustTimeNoter 自带的报表只给"某个应用共用了多久"，看不出"几点几分在用什么"。
本工具复用它的官方导出接口（tracker export --format json），把逐条记录合成
"会话"，再生成一个可以按时间点查看的本地 HTML 页面。

用法：
    TimeTrace.exe html                 生成时间线并打开浏览器
    TimeTrace.exe at "14:30"           查询某一时刻在用什么
    TimeTrace.exe app pallas           查某个软件什么时候被用过
    TimeTrace.exe day --date 2026-10-26  在控制台打印某天的时间流水
    TimeTrace.exe prune --dry-run      看看哪些过期数据会被清理

只用标准库，不修改 tracker 本体，也不碰加密数据文件。
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta

APP_NAME = "时迹"
APP_NAME_EN = "TimeTrace"
VERSION = "2.4.0"
APP_TITLE = "时迹 · 操作时间线"

# 日志保留天数：超过这个天数的数据分片会被自动删除
RETENTION_DAYS = 30

# 相邻两条同应用记录之间允许的最大缝（秒）：采样抖动导致的小缝要合并
MERGE_GAP_SECS = 3

# 常见进程 -> 友好名称（不在表里就显示去掉 .exe 的文件名）
LABELS = {
    "msedge.exe": "Edge 浏览器",
    "chrome.exe": "Chrome 浏览器",
    "firefox.exe": "Firefox 浏览器",
    "explorer.exe": "资源管理器",
    "WindowsTerminal.exe": "Windows 终端",
    "cmd.exe": "命令提示符",
    "powershell.exe": "PowerShell",
    "pwsh.exe": "PowerShell",
    "Code.exe": "VS Code",
    "sublime_text.exe": "Sublime Text",
    "Weixin.exe": "微信",
    "WeChat.exe": "微信",
    "QQ.exe": "QQ",
    "QQMusic.exe": "QQ 音乐",
    "qmbrowser.exe": "QQ 音乐·内嵌页",
    "Doubao.exe": "豆包",
    "DeepSeek Harness.exe": "DeepSeek Harness",
    "Taskmgr.exe": "任务管理器",
    "mmc.exe": "管理控制台",
    "Notepad.exe": "记事本",
    "StartMenuExperienceHost.exe": "开始菜单",
    "SearchHost.exe": "Windows 搜索",
    "ShellHost.exe": "系统外壳",
    "CHXSmartScreen.exe": "应用防护",
    "YdScreenCapture.exe": "有道截图",
    "YoudaoDict.exe": "有道词典",
    "wegame.exe": "WeGame",
    "pallas.exe": "WeGame 组件",
    "browser.exe": "内嵌浏览器",
    "League of Legends.exe": "英雄联盟",
    "LeagueClient.exe": "英雄联盟·客户端",
    "LeagueClientUx.exe": "英雄联盟·客户端",
    "ACE-Helper.exe": "反作弊组件",
    "NGR-Win64-Shipping.exe": "NGR",
}

PALETTE = [
    "#2f6ded", "#e8590c", "#0ca678", "#7048e8", "#d6336c",
    "#f59f00", "#1c7ed6", "#37b24d", "#ae3ec9", "#e03131",
    "#1098ad", "#5c940d", "#f76707", "#4263eb", "#c2255c",
    "#0b7285", "#7950f2", "#2b8a3e",
]


# --------------------------------------------------------------------------
# U 盘接入记录：Windows 设备日志（Kernel-PnP）+ 盘符映射
# --------------------------------------------------------------------------
_PS_USB_TEMPLATE = r'''
$start = Get-Date '%s'
$end = (Get-Date '%s').AddDays(1)
$ev = Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Kernel-PnP/Configuration'; StartTime=$start; EndTime=$end} -ErrorAction SilentlyContinue
$letters = @{}
try {
  $props = Get-ItemProperty 'HKLM:\SYSTEM\MountedDevices' -ErrorAction Stop
  foreach ($p in $props.PSObject.Properties) {
    if ($p.Name -notlike '\DosDevices\??:') { continue }
    $s = [Text.Encoding]::Unicode.GetString($p.Value)
    if ($s -match 'USBSTOR#Disk&Ven_([^&]+)&Prod_([^&]+)&Rev_([^#]+)#([^#&]+)') {
      $letters[$matches[4]] = $p.Name.Substring($p.Name.Length - 2, 1)
    }
  }
} catch { }
$out = New-Object System.Collections.ArrayList
foreach ($e in $ev) {
  if ($e.Id -ne 400 -and $e.Id -ne 410) { continue }
  if ($e.Message -notmatch 'usbstor\.inf') { continue }
  $inst = [string]$e.Properties[0].Value
  if ($inst -notmatch '^USB\\VID_([0-9A-Fa-f]+)&PID_([0-9A-Fa-f]+)\\(.+)$') { continue }
  $v = $matches[1]; $p2 = $matches[2]; $sn = $matches[3]
  $letter = ''
  if ($letters.ContainsKey($sn)) { $letter = $letters[$sn] }
  [void]$out.Add([pscustomobject]@{
    time = $e.TimeCreated.ToString('yyyy-MM-ddTHH:mm:ss')
    vid = $v
    pid = $p2
    letter = $letter
  })
}
$out | ConvertTo-Json -Compress
'''


def load_usb_events(start_day, end_day):
    """读取设备日志里的 U 盘接入事件（只取时间 + VID/PID + 盘符，不取序列号）。"""
    if os.name != "nt":
        return []
    script = _PS_USB_TEMPLATE % (start_day.isoformat(), end_day.isoformat())
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=90,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    raw = proc.stdout.decode("utf-8", "replace").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    events = []
    last_seen = {}
    for item in data:
        try:
            when = datetime.strptime(item.get("time", ""), "%Y-%m-%dT%H:%M:%S")
            vid = str(item.get("vid") or "")
            pid = str(item.get("pid") or "")
            letter = str(item.get("letter") or "").upper().rstrip(":")
        except (TypeError, ValueError, AttributeError):
            continue
        if not vid or not pid:
            continue
        # 同一次插拔会连着写 400/410 两条，5 分钟内同型号合并成一次
        key = vid + ":" + pid
        if key in last_seen and (when - last_seen[key]).total_seconds() <= 300:
            continue
        last_seen[key] = when
        midnight = datetime.combine(when.date(), dtime.min)
        events.append({
            "date": when.date().isoformat(),
            "t": int((when - midnight).total_seconds()),
            "vid": vid,
            "pid": pid,
            "letter": letter,
        })
    events.sort(key=lambda e: (e["date"], e["t"]))
    return events


_PS_RECENT_TEMPLATE = r'''
$recent = Join-Path $env:APPDATA 'Microsoft\Windows\Recent'
$vols = @{}
try {
  Get-Volume -ErrorAction Stop | ForEach-Object {
    if ($_.DriveLetter) { $vols[[string]$_.DriveLetter] = [string]$_.DriveType }
  }
} catch { }
$sh = New-Object -ComObject WScript.Shell
$out = New-Object System.Collections.ArrayList
if (Test-Path $recent) {
  $links = Get-ChildItem $recent -Filter *.lnk -ErrorAction SilentlyContinue
  foreach ($l in $links) {
    try { $t = $sh.CreateShortcut($l.FullName).TargetPath } catch { continue }
    if (-not $t -or $t -notmatch '^([A-Za-z]):\\') { continue }
    $d = $matches[1].ToUpper()
    $type = ''
    if ($vols.ContainsKey($d)) { $type = $vols[$d] }
    if ($type -eq 'Fixed' -or $type -eq 'CDRom' -or $type -eq 'Network') { continue }
    [void]$out.Add([pscustomobject]@{
      time = $l.LastWriteTime.ToString('yyyy-MM-ddTHH:mm:ss')
      path = $t
      drive = $d
    })
  }
}
$out | ConvertTo-Json -Compress
'''


def load_recent_usb_files():
    """从「最近使用」里找出指向可移动盘（或当前不存在的盘符）的文件。"""
    if os.name != "nt":
        return []
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
             base64.b64encode(_PS_RECENT_TEMPLATE.encode("utf-16-le")).decode("ascii")],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    raw = proc.stdout.decode("utf-8", "replace").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    out, seen = [], set()
    for item in data:
        try:
            path = str(item.get("path") or "")
            drive = str(item.get("drive") or "").upper()
            when = datetime.strptime(item.get("time", ""), "%Y-%m-%dT%H:%M:%S")
        except (TypeError, ValueError, AttributeError):
            continue
        if not path or len(path) < 4:
            continue
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        midnight = datetime.combine(when.date(), dtime.min)
        out.append({"date": when.date().isoformat(),
                    "t": int((when - midnight).total_seconds()),
                    "path": path, "drive": drive})
    out.sort(key=lambda e: (e["date"], e["t"]), reverse=True)
    return out[:300]


# --------------------------------------------------------------------------
# 浏览器下载记录（Edge / Chrome）里，目标落在 U 盘的那些
# --------------------------------------------------------------------------
def drive_kind(letter):
    """removable / fixed / cdrom / network / absent"""
    if os.name != "nt":
        return "unknown"
    try:
        import ctypes
        t = ctypes.windll.kernel32.GetDriveTypeW(letter + ":\\")
    except Exception:
        return "unknown"
    return {1: "absent", 2: "removable", 3: "fixed", 4: "network", 5: "cdrom"}.get(t, "unknown")


def _read_browser_downloads(db_path):
    tmp = tempfile.mkdtemp(prefix="tt_hist_")
    dst = os.path.join(tmp, "History")
    try:
        shutil.copy2(db_path, dst)
        for ext in ("-wal", "-shm"):
            src = db_path + ext
            if os.path.isfile(src):
                try:
                    shutil.copy2(src, dst + ext)
                except OSError:
                    pass
        con = sqlite3.connect(dst)
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT target_path, start_time, COALESCE(total_bytes, received_bytes, 0), "
                "COALESCE(state, 1) FROM downloads ORDER BY start_time DESC LIMIT 1000"
            )
            return cur.fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def load_usb_downloads():
    """返回下载目标是 U 盘（或已拔出盘符）的条目。"""
    if os.name != "nt":
        return []
    base = os.environ.get("LOCALAPPDATA", "")
    roots = [
        os.path.join(base, "Microsoft", "Edge", "User Data"),
        os.path.join(base, "Google", "Chrome", "User Data"),
    ]
    out, seen = [], set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            profiles = os.listdir(root)
        except OSError:
            continue
        for profile in profiles:
            db = os.path.join(root, profile, "History")
            if not os.path.isfile(db):
                continue
            for row in _read_browser_downloads(db):
                try:
                    target, start, size, state = row
                except (TypeError, ValueError):
                    continue
                if not target or not isinstance(target, str):
                    continue
                m = re.match(r"^([A-Za-z]):\\", target)
                if not m:
                    continue
                if state not in (1, 3, 4):  # 1=完成；3/4=中断但目标已确定
                    continue
                kind = drive_kind(m.group(1).upper())
                if kind in ("fixed", "cdrom", "network"):
                    continue
                try:
                    when = datetime.fromtimestamp(start / 1000000.0 - 11644473600)
                except (OverflowError, OSError, ValueError):
                    continue
                key = target.lower()
                if key in seen:
                    continue
                seen.add(key)
                midnight = datetime.combine(when.date(), dtime.min)
                out.append({
                    "date": when.date().isoformat(),
                    "t": int((when - midnight).total_seconds()),
                    "path": target,
                    "drive": m.group(1).upper(),
                    "size": int(size or 0),
                })
    out.sort(key=lambda e: (e["date"], e["t"]), reverse=True)
    return out[:300]


def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return ""


# --------------------------------------------------------------------------
# tracker 调用
# --------------------------------------------------------------------------
def app_dir():
    """打包成 exe 后 __file__ 在临时解包目录里，要用 exe 自己所在的目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def find_tracker():
    here = app_dir()
    cands = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "RustTimeNoter", "bin", "tracker.exe"),
        os.path.join(here, "tracker.exe"),
        os.path.join(here, "bin", "tracker.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    sys.exit(
        "找不到 tracker.exe。\n"
        "时迹读取的是 RustTimeNoter 的数据，请先安装 RustTimeNoter（双击它的 install.bat），\n"
        "或者把 tracker.exe 放到本程序同一个文件夹里。\n"
        "默认查找位置：%LOCALAPPDATA%\\RustTimeNoter\\bin\\tracker.exe"
    )


# --------------------------------------------------------------------------
# 应用图标：从 exe 里取图标，缓存成本地 PNG，页面里用 base64 内嵌
# --------------------------------------------------------------------------
ICON_CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "TimeTrace", "icons")

_PS_ICON_TEMPLATE = r'''
Add-Type -AssemblyName System.Drawing
$map = Get-Content -Raw -Encoding UTF8 '%s' | ConvertFrom-Json
foreach ($prop in $map.PSObject.Properties) {
    $src = $prop.Name
    $dst = $prop.Value
    try {
        if (-not (Test-Path -LiteralPath $src)) { continue }
        $ico = [System.Drawing.Icon]::ExtractAssociatedIcon($src)
        if ($null -eq $ico) { continue }
        $bmp = $ico.ToBitmap()
        $bmp.Save($dst, [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()
        $ico.Dispose()
    } catch { }
}
'''


def _icon_cache_file(app_path):
    """按 路径+大小+修改时间 缓存，程序升级后图标会自动刷新。"""
    try:
        st = os.stat(app_path)
        key = "%s|%d|%d" % (app_path.lower(), st.st_size, int(st.st_mtime))
    except OSError:
        key = app_path.lower()
    name = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".png"
    return os.path.join(ICON_CACHE_DIR, name)


def load_icons(apps):
    """返回 {exe 路径: data:image/png;base64,...}；取不到的应用直接跳过（页面回退到色块）。"""
    if os.name != "nt":
        return {}
    usable = [a for a in apps if a and os.path.isfile(a)]
    cached, todo = {}, []
    for a in usable:
        p = _icon_cache_file(a)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            cached[a] = p
        else:
            todo.append(a)

    if todo:
        try:
            os.makedirs(ICON_CACHE_DIR, exist_ok=True)
            tmp = tempfile.mkdtemp(prefix="tt_icon_")
            listing = dict((a, _icon_cache_file(a)) for a in todo)
            list_file = os.path.join(tmp, "list.json")
            with open(list_file, "w", encoding="utf-8") as fp:
                json.dump(listing, fp, ensure_ascii=False)
            script = _PS_ICON_TEMPLATE % (list_file.replace("'", "''"),)
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=90,
            )
            for a in todo:
                p = listing[a]
                if os.path.isfile(p) and os.path.getsize(p) > 0:
                    cached[a] = p
            shutil.rmtree(tmp, ignore_errors=True)
        except (OSError, subprocess.SubprocessError):
            pass

    out = {}
    for a, p in cached.items():
        try:
            with open(p, "rb") as fp:
                out[a] = "data:image/png;base64," + base64.b64encode(fp.read()).decode("ascii")
        except OSError:
            continue
    return out


def export_records(from_day, to_day):
    """调用 tracker export，返回原始记录列表。"""
    tracker = find_tracker()
    tmp_dir = tempfile.mkdtemp(prefix="rtn_timeline_")
    out = os.path.join(tmp_dir, "export.json")
    try:
        cmd = [
            tracker, "export",
            "--format", "json",
            "--out", out,
            "--from", from_day.isoformat(),
            "--to", to_day.isoformat(),
        ]
        proc = subprocess.run(cmd, capture_output=True)
        if not os.path.isfile(out):
            msg = (proc.stdout or b"") + (proc.stderr or b"")
            sys.exit("导出失败：\n" + msg.decode("utf-8", "replace").strip())
        with open(out, "r", encoding="utf-8") as f:
            return json.load(f)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def parse_start(rec):
    try:
        return datetime.strptime(
            "%s %s" % (rec.get("date"), rec.get("start_time")), "%Y-%m-%d %H:%M:%S"
        )
    except (TypeError, ValueError):
        ts = rec.get("start_timestamp") or ""
        try:
            return datetime.fromisoformat(ts).replace(tzinfo=None)
        except ValueError:
            return None


def base_name(path):
    if not path:
        return ""
    return re.split(r"[\\/]", path)[-1]


# --------------------------------------------------------------------------
# 记录 -> 按天、按会话
# --------------------------------------------------------------------------
def split_midnight(start, end):
    """把跨零点的区间切成按本地自然日的若干段。"""
    while start < end:
        day_end = datetime.combine(start.date() + timedelta(days=1), dtime.min)
        stop = min(end, day_end)
        yield start, stop
        start = stop


def build_days(records):
    """把逐条记录铺成"每秒前台归属"，再还原成互不重叠的会话。

    tracker 的记录偶尔会区间重叠（后台短记录落在前台长记录内部）。
    这里以"开始时间更晚的记录代表更新的状态"为准逐秒覆盖，
    于是时间线永远不重叠，符合"那一刻前台是什么"的直觉。
    """
    per_day = defaultdict(list)  # date -> [(start_sec, dur_sec, kind, app)]
    for rec in records:
        start = parse_start(rec)
        if start is None:
            continue
        try:
            dur = int(rec.get("duration_secs") or 0)
        except (TypeError, ValueError):
            dur = 0
        if dur <= 0:
            continue
        end = start + timedelta(seconds=dur)
        kind = rec.get("record_type") or "activity"
        app = rec.get("app_path") or ""
        if kind == "activity" and not app:
            kind = "gap"
        for s, e in split_midnight(start, end):
            base = datetime.combine(s.date(), dtime.min)
            per_day[s.date()].append(((s - base).total_seconds(), (e - s).total_seconds(), kind, app))

    days = []
    for day in sorted(per_day):
        items = per_day[day]
        labels = [None] * 86400
        lo, hi = 86400, 0
        for t0, d, kind, app in sorted(items, key=lambda x: x[0]):
            a = int(round(t0))
            n = max(1, int(round(d)))
            a = max(0, min(86399, a))
            b = min(86400, a + n)
            for i in range(a, b):
                labels[i] = (kind, app)
            lo = min(lo, a)
            hi = max(hi, b)

        runs = []
        i = lo
        while i < hi:
            if labels[i] is None:
                i += 1
                continue
            j = i
            while j + 1 < hi and labels[j + 1] == labels[i]:
                j += 1
            kind, app = labels[i]
            label = (kind, app)
            runs.append({"t": float(i), "d": float(j + 1 - i), "kind": kind, "a": app})
            i = j + 1

        # 同一应用被 1~3 秒的采样缝隙切开时缝回去
        merged = []
        for r in runs:
            if merged:
                p = merged[-1]
                if (p["a"] == r["a"] and p["kind"] == r["kind"]
                        and r["t"] - (p["t"] + p["d"]) <= MERGE_GAP_SECS):
                    p["d"] = r["t"] + r["d"] - p["t"]
                    continue
            merged.append(r)

        sessions = [{"t": r["t"], "d": r["d"], "a": r["a"]} for r in merged if r["kind"] != "gap"]
        gaps = [{"t": r["t"], "d": r["d"]} for r in merged if r["kind"] == "gap"]

        totals = defaultdict(float)
        for s in sessions:
            totals[s["a"]] += s["d"]
        app_totals = sorted(totals.items(), key=lambda kv: -kv[1])

        activity = sum(s["d"] for s in sessions)
        gap_secs = sum(g["d"] for g in gaps)
        first = merged[0]["t"] if merged else None
        last = (merged[-1]["t"] + merged[-1]["d"]) if merged else None
        span = (last - first) if merged else 0.0

        days.append({
            "date": day.isoformat(),
            "weekday": "一二三四五六日"[day.weekday()],
            "sessions": sessions,
            "gaps": gaps,
            "app_totals": [[a, round(t)] for a, t in app_totals],
            "stats": {
                "records": len(items),
                "activity": round(activity),
                "gap": round(gap_secs),
                "untracked": round(max(0.0, span - activity - gap_secs)),
                "span": round(span),
                "offline": round(max(0.0, 86400 - span)),
                "covered": round(activity + gap_secs),
                "first": None if first is None else round(first),
                "last": None if last is None else round(last),
                "sessions": len(sessions),
            },
        })
    return days


def rank_apps(days):
    totals = defaultdict(float)
    for d in days:
        for app, secs in d["app_totals"]:
            totals[app] += secs
    return [a for a, _ in sorted(totals.items(), key=lambda kv: -kv[1])]


def app_stats(days):
    """每个应用：总时长、会话数、出现天数、首次与最近一次使用。"""
    stats = {}
    for d in days:
        for s in d["sessions"]:
            r = stats.get(s["a"])
            if r is None:
                r = {"a": s["a"], "total": 0.0, "n": 0, "dates": set(),
                     "first": [d["date"], s["t"]], "last": [d["date"], s["t"] + s["d"]]}
                stats[s["a"]] = r
            r["total"] += s["d"]
            r["n"] += 1
            r["dates"].add(d["date"])
            if (d["date"], s["t"]) < tuple(r["first"]):
                r["first"] = [d["date"], s["t"]]
            if (d["date"], s["t"] + s["d"]) > tuple(r["last"]):
                r["last"] = [d["date"], s["t"] + s["d"]]
    out = [
        {"a": r["a"], "total": round(r["total"]), "n": r["n"], "days": len(r["dates"]),
         "first": r["first"], "last": r["last"]}
        for r in stats.values()
    ]
    out.sort(key=lambda x: -x["total"])
    return out


# --------------------------------------------------------------------------
# 记录器状态（启动 / 停止 / 被删除）
# --------------------------------------------------------------------------
def lifecycle_path():
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "RustTimeNoter", "lifecycle.log")


def append_lifecycle(kind, note="", ts=None):
    try:
        path = lifecycle_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rec = {"ts": (ts or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "note": note}
        with open(path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def read_lifecycle():
    out = []
    path = lifecycle_path()
    if not os.path.isfile(path):
        return out
    try:
        with open(path, encoding="utf-8", errors="replace") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                try:
                    dt = datetime.strptime(str(rec.get("ts", "")), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                out.append({"dt": dt, "kind": str(rec.get("kind", "")), "note": str(rec.get("note", ""))})
    except OSError:
        return out
    out.sort(key=lambda r: r["dt"])
    return out


def _model_range(days):
    if not days:
        return None
    lo = datetime.combine(date.fromisoformat(days[0]["date"]), dtime(0, 0))
    hi = datetime.combine(date.fromisoformat(days[-1]["date"]) + timedelta(days=1), dtime(0, 0))
    return lo, hi


def lifecycle_events(days, life):
    rng = _model_range(days)
    if rng is None:
        return []
    lo, hi = rng
    out = []
    for r in life:
        if lo <= r["dt"] < hi:
            out.append({
                "date": r["dt"].strftime("%Y-%m-%d"),
                "t": r["dt"].hour * 3600 + r["dt"].minute * 60 + r["dt"].second,
                "kind": r["kind"],
            })
    return out


def lifecycle_spans(days, life):
    rng = _model_range(days)
    if rng is None:
        return []
    lo, hi = rng
    raw = []
    open_dt = None
    for r in life:
        if r["kind"] in ("stopped", "missing"):
            if open_dt is None:
                open_dt = r["dt"]
        elif r["kind"] in ("started", "restored"):
            if open_dt is not None:
                raw.append((open_dt, r["dt"]))
                open_dt = None
    if open_dt is not None:
        raw.append((open_dt, hi))
    out = []
    for a, b in raw:
        s = max(a, lo)
        e = min(b or hi, hi)
        if e <= s:
            continue
        cur = s
        while cur < e:
            nxt = datetime.combine(cur.date() + timedelta(days=1), dtime(0, 0))
            seg_end = min(nxt, e)
            base = datetime.combine(cur.date(), dtime(0, 0))
            out.append({
                "date": cur.strftime("%Y-%m-%d"),
                "t": int((cur - base).total_seconds()),
                "d": int((seg_end - cur).total_seconds()),
            })
            cur = seg_end
    return out


def build_model(days, with_icons=False):
    apps = rank_apps(days)
    color_map = {}
    for i, app in enumerate(apps):
        color_map[app] = PALETTE[i % len(PALETTE)]
    labels = {}
    for app in apps:
        base = base_name(app)
        labels[app] = LABELS.get(base, os.path.splitext(base)[0])
    total = {
        "activity": sum(d["stats"]["activity"] for d in days),
        "sessions": sum(d["stats"]["sessions"] for d in days),
        "apps": len(apps),
        "days": len(days),
    }
    model = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "days": days,
        "total": total,
        "appStats": app_stats(days),
        "colorMap": color_map,
        "labels": labels,
        "palette": PALETTE,
    }
    if with_icons:
        model["icons"] = load_icons(apps)
    life = read_lifecycle()
    model["lifecycle"] = lifecycle_events(days, life)
    model["lifeSpans"] = lifecycle_spans(days, life)
    return model


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--ink:#171b24;--muted:#6b7280;--line:#e4e7ec;--accent:#2f6ded;--soft:#eef1f5;--hit:#fff3bf}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font:14px/1.55 "Segoe UI","Microsoft YaHei","PingFang SC",system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1200px;margin:0 auto;padding:22px 20px 48px}
header.top{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:14px}
h1{font-size:20px;margin:0;letter-spacing:.2px;font-weight:650}
.sub{color:var(--muted);font-size:12.5px;margin-top:4px}
.sub code{background:var(--soft);padding:1px 5px;border-radius:4px;font-size:11px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:14px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.row.between{justify-content:space-between}
h2{font-size:12px;margin:0 0 10px;color:var(--muted);font-weight:600;letter-spacing:.4px;text-transform:uppercase}
h3{font-size:15px;margin:0;font-weight:650;letter-spacing:.2px}
button,.btn{font:inherit;font-size:13px;padding:6px 11px;border-radius:8px;border:1px solid var(--line);background:#fff;color:var(--ink);cursor:pointer;transition:.12s}
button:hover{background:#f2f4f7}
input[type=text]{font:inherit;font-size:13px;padding:6px 9px;border-radius:8px;border:1px solid var(--line);background:#fff;width:230px}
input:focus{outline:2px solid rgba(47,109,237,.22);border-color:var(--accent)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:14px}
.stat .k{font-size:11.5px;color:var(--muted)}
.stat .v{font-size:21px;font-weight:640;font-variant-numeric:tabular-nums;letter-spacing:-.2px}
.legend{display:flex;flex-wrap:wrap;gap:7px}
.lg{display:inline-flex;align-items:center;gap:6px;padding:3px 9px;border:1px solid var(--line);border-radius:999px;font-size:12.5px;cursor:pointer;background:#fff;transition:.12s;color:inherit;text-decoration:none}
.lg:hover{background:#f2f4f7}
.lg .sw{width:9px;height:9px;border-radius:3px;flex:none}
.ico{width:15px;height:15px;flex:none;border-radius:3px;vertical-align:-3px}
.lg .ico{width:14px;height:14px}
.segico{position:absolute;top:50%;transform:translateY(-50%);width:10px;height:10px;pointer-events:none;z-index:2;filter:drop-shadow(0 0 1px rgba(255,255,255,.95))}
.lg .n{font-variant-numeric:tabular-nums;color:var(--muted);font-size:11.5px}
.lg.active{border-color:var(--ink);box-shadow:0 0 0 1px var(--ink) inset}
.days-index{display:flex;flex-wrap:wrap;gap:6px}
.days-index a{font-size:12.5px;padding:3px 9px;border:1px solid var(--line);border-radius:999px;text-decoration:none;color:var(--ink);background:#fff}
.days-index a:hover{background:#f2f4f7}
.apptable{margin-top:12px}
.approw{cursor:pointer}
.approw:hover td{background:#f8fafc}
.approw.active td{background:#eef4ff}
.dayhead{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;cursor:pointer;user-select:none}
.foldbtn{font-size:12px;padding:4px 10px;border-radius:999px;flex:none}
.day.collapsed .daytable{display:none}
.hstrip{display:grid;grid-template-columns:repeat(24,1fr);gap:2px;margin-top:5px}
.hcell{height:24px;display:flex;align-items:center;justify-content:center;border-radius:4px;background:#f6f7f9;position:relative;overflow:hidden}
.hcell .ico{width:14px;height:14px}
.hcell .sw{width:12px;height:12px}
.usbmark{position:absolute;top:0;bottom:0;width:0;border-left:2px dashed #0f766e;opacity:.85;z-index:2;pointer-events:auto}
.lifemark{position:absolute;top:0;bottom:0;width:0;border-left:2px solid #e5484d;opacity:.9;z-index:4;pointer-events:auto}
.lifemark.ok{border-left-color:#30a46c}
.lifespan{position:absolute;top:0;bottom:0;min-width:5px;z-index:6;pointer-events:auto;
  background:repeating-linear-gradient(45deg,#e5484d 0 3px,#ffc9c9 3px 6px);
  box-shadow:inset 0 0 0 1px #b42318}
.lifebad{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;background:#fee4e2;color:#b42318}
.lifeok{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;background:#e7f6ec;color:#087443}
.lifelegend{display:inline-block;width:16px;height:10px;margin-left:6px;vertical-align:middle;border:1px solid #b42318;border-radius:2px;background:repeating-linear-gradient(45deg,#e5484d 0 2px,#ffc9c9 2px 4px)}
.foldbtn{font-size:12px;padding:3px 10px;border-radius:999px}
body.dark{--bg:#0e1116;--card:#161b22;--ink:#e6e9ef;--muted:#98a2b3;--line:#2a313b;--soft:#1b212a;--hit:#3b3520}
body.dark .lg{background:#1d2430;color:var(--ink)}
body.dark button,body.dark input[type=text]{background:#1d2430;color:var(--ink)}
body.dark .days-index a{background:#1b212a}
body.dark .row button,body.dark .foldbtn{background:#1d2430;color:var(--ink);border-color:var(--line)}
body.dark .tip{background:#1d2430;color:var(--ink);border-color:var(--line)}
body.dark .segbase{background:#1b212a}
body.dark td{border-bottom-color:#232a34}
body.dark td.t{color:#aab3c0}
body.dark tbody tr:hover{background:#1b212a}
body.dark .daysub .sep{color:#3a4350}
body.dark .axis{color:#6b7684}
body.dark .card.note{background:#141a21}
body.dark .lifebad{background:#3a1d1d;color:#ff9b9b}
body.dark .lifeok{background:#12301f;color:#7ee2a8}
.usbmark::after{content:"";position:absolute;top:2px;left:-4px;width:6px;height:6px;border-radius:50%;background:#0f766e}
.usbpath{max-width:640px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#344054}
.hcell .hbar{position:absolute;left:0;right:0;bottom:0;height:3px;opacity:.9}
.hcell.hoverable{cursor:pointer}
.hcell.hoverable:hover{background:#eceff4}
.switch{display:inline-flex;align-items:center;gap:6px;font-size:13px;cursor:pointer;color:#344054}
.switch input{width:15px;height:15px;accent-color:#2f6ded}
.dayhead .wd{color:var(--muted);font-weight:400;font-size:12.5px;margin-left:6px}
.daysub{color:var(--muted);font-size:12px;margin-top:3px;font-variant-numeric:tabular-nums}
.daysub b{color:var(--ink);font-weight:600}
.daysub .sep{color:#cfd4dc;margin:0 6px}
.ribbon{position:relative;height:40px;border-radius:8px;background:#eff1f5;overflow:hidden;cursor:crosshair;margin-top:11px;user-select:none}
.ribbon .seg{position:absolute;top:0;bottom:0;min-width:1px;transition:opacity .1s}
.ribbon .seg.gap{background:repeating-linear-gradient(45deg,#d5d9e0 0 4px,#e7eaef 4px 8px)}
.ribbon .seg.dim{opacity:.18}
.ribbon .cursorline{position:absolute;top:0;bottom:0;width:2px;margin-left:-1px;background:#111;pointer-events:none;display:none;z-index:3}
.ribbon .cursorline::before{content:"";position:absolute;top:0;left:-4px;border-left:5px solid transparent;border-right:5px solid transparent;border-top:6px solid #111}
.axis{position:relative;height:16px;margin-top:4px;font-size:10.5px;color:#98a2b3;font-variant-numeric:tabular-nums}
.axis .tick{position:absolute;top:0;transform:translateX(-50%)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:12px}
th{text-align:left;font-weight:600;color:var(--muted);font-size:11.5px;padding:6px 8px;border-bottom:1px solid var(--line);text-transform:uppercase;letter-spacing:.3px}
td{padding:5px 8px;border-bottom:1px solid #f3f5f8;font-variant-numeric:tabular-nums}
tbody tr:hover{background:#f8fafc}
tbody tr.hit{background:var(--hit)!important}
td.t{color:#475467;width:92px}
td.app{display:flex;align-items:center;gap:7px}
td.app .sw{width:9px;height:9px;border-radius:3px;flex:none}
td.app .ico{width:14px;height:14px}
td.app .exe{color:#b0b7c3;font-size:11.5px;margin-left:2px}
td.num{text-align:right;color:var(--muted);width:92px}
.empty{padding:18px;text-align:center;color:var(--muted)}
.tip{position:fixed;z-index:60;pointer-events:none;background:#171b24;color:#fff;padding:6px 9px;border-radius:7px;font-size:12px;line-height:1.45;box-shadow:0 6px 18px rgba(16,24,40,.22);display:none;white-space:nowrap}
.tip .t{font-variant-numeric:tabular-nums;opacity:.78}
.note{font-size:12.5px;color:#4b5563;line-height:1.85}
.note b{color:var(--ink)}
.kbd{background:var(--soft);border-radius:4px;padding:0 4px;font-size:11.5px;font-family:Consolas,monospace}
footer{color:#98a2b3;font-size:11.5px;text-align:center;margin-top:10px}
@media(max-width:700px){td.app .exe{display:none}}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <h1>时迹 <span style="font-size:12px;font-weight:400;color:#98a2b3;letter-spacing:.6px">TIMETRACE</span></h1>
      <div class="sub" id="sub"></div>
    </div>
    <div class="row">
      <input type="text" id="q" placeholder="搜索 14:30 或 2026-10-26 14:30">
      <button id="go">定位</button>
      <button id="save" title="把当前这份时间线存成 HTML 文件">保存本页</button>
      <button id="theme" title="切换亮色 / 暗色">🌙 暗色</button>
    </div>
  </header>

  <div class="card">
    <div class="stats" id="stats"></div>
    <div class="row between" style="margin-top:16px">
      <h2 style="margin:0">全部应用（点击可只看它）</h2>
      <span class="hint" id="filterhint" style="color:var(--muted);font-size:12px"></span>
    </div>
    <div class="legend" id="legend" style="margin-top:8px"></div>
    <div class="row" style="margin-top:16px;gap:10px">
      <h2 style="margin:0">跳到某天</h2>
      <div class="days-index" id="daysIndex"></div>
    </div>
    <div class="row" style="margin-top:16px;gap:12px">
      <label class="switch"><input type="checkbox" id="fold"> 每天默认收起表格（点日期那一行展开）</label>
      <button id="expandAll" style="font-size:12px;padding:4px 10px;border-radius:999px">全部展开</button>
      <button id="collapseAll" style="font-size:12px;padding:4px 10px;border-radius:999px">全部收起</button>
      <button id="orderBtn" style="font-size:12px;padding:4px 10px;border-radius:999px">日期：新→旧</button>
    </div>
  </div>

  <div class="card" id="usbcard" style="display:none">
    <div class="row between">
      <h2 style="margin:0">U 盘记录</h2>
      <div class="row" style="gap:8px">
        <span id="usbhint" class="hint" style="color:var(--muted);font-size:12px"></span>
        <button class="foldbtn" data-target="usbbody">收起</button>
      </div>
    </div>

    <div id="usbbody">
    <div id="usbconnwrap">
      <h3 style="font-size:13px;margin:14px 0 6px;font-weight:600">接入时刻</h3>
      <table class="apptable">
        <thead><tr>
          <th style="width:150px">时间</th>
          <th>设备</th>
          <th style="width:90px">盘符</th>
        </tr></thead>
        <tbody id="usbtbody"></tbody>
      </table>
    </div>

    <div id="usbfileswrap" style="display:none">
      <h3 style="font-size:13px;margin:16px 0 6px;font-weight:600">打开过的文件
        <span id="usbfileshint" class="hint" style="color:var(--muted);font-size:12px;font-weight:400"></span>
      </h3>
      <table class="apptable">
        <thead><tr>
          <th style="width:150px">时间</th>
          <th>文件</th>
          <th style="width:70px">盘符</th>
        </tr></thead>
        <tbody id="usbfilestbody"></tbody>
      </table>
    </div>

    <div id="usbdlwrap" style="display:none">
      <h3 style="font-size:13px;margin:16px 0 6px;font-weight:600">下载到 U 盘的文件
        <span id="usbdlhint" class="hint" style="color:var(--muted);font-size:12px;font-weight:400"></span>
      </h3>
      <table class="apptable">
        <thead><tr>
          <th style="width:150px">时间</th>
          <th>文件</th>
          <th style="width:90px">大小</th>
          <th style="width:70px">盘符</th>
        </tr></thead>
        <tbody id="usbdltbody"></tbody>
      </table>
    </div>

    <div class="note" style="margin-top:10px">
      接入时刻来自 Windows 设备日志（Kernel-PnP）；「打开过的文件」来自「最近使用」记录；
      「下载到 U 盘的文件」来自 Edge / Chrome 的下载记录。
      能看出<b>从 U 盘打开过什么</b>、<b>浏览器下载到 U 盘了什么</b>；
      资源管理器里的拖拽、复制粘贴、删除，两个方向 Windows 都不记录，因此无法看到。
      不写入设备序列号，也不读取文件内容。
    </div>
    </div>
  </div>

  <div class="card">
    <div class="row between">
      <h2 style="margin:0">查应用：它是什么时候被用过的</h2>
      <input type="text" id="appq" placeholder="进程名或名字，如 pallas、微信" style="width:300px">
    </div>
    <div id="apphint" style="color:var(--muted);font-size:12px;margin-top:6px"></div>
    <table class="apptable">
      <thead><tr>
        <th>应用</th>
        <th style="text-align:right;width:110px">总时长</th>
        <th style="width:90px">出现天数</th>
        <th style="width:126px">首次使用</th>
        <th style="width:126px">最近一次</th>
        <th style="text-align:right;width:76px">段数</th>
      </tr></thead>
      <tbody id="apptbody"></tbody>
    </table>
    <div class="note" style="margin-top:8px">点一行 = 只看这个应用，并跳到它最近一次使用的那一段。</div>
  </div>

  <div id="days"></div>

  <div class="card" id="lifecard" style="display:none">
    <div class="row between">
      <h2 style="margin:0">记录器状态</h2>
      <div class="row" style="gap:8px">
        <span id="lifehint" class="hint" style="color:var(--muted);font-size:12px"></span>
        <button class="foldbtn" data-target="lifebody">收起</button>
      </div>
    </div>
    <div id="lifebody">
      <table class="apptable">
        <thead><tr>
          <th style="width:150px">时间</th>
          <th style="width:140px">事件</th>
        </tr></thead>
        <tbody id="lifetbody"></tbody>
      </table>
      <div class="note" style="margin-top:10px">
        记录器被<b>手动停止</b>、退出或<b>程序被删除</b>时，这里会留一条记录，
        并在当天色带上用<b style="color:#b42318">红色斜纹</b>标出中断的那段区间（正常记录不标注）。
      </div>
    </div>
  </div>

  <div class="card note">
    <b>怎么读：</b>色带上每一块是一段会话，鼠标移上去可看应用与起止时间；
    色带下面那排小格子是<b>每小时的主力应用</b>（点格子可只看它）；
    连续使用超过十几分钟的时段，色带上会直接贴出应用图标。<br>
    点<b>日期那一行</b>可以展开/收起当天的完整会话表（上面的开关决定默认是否收起）。
    灰色斜纹 = 有记录但未归属到应用的空档；空白 = 完全没有记录（关机 / 睡眠 / 记录器未运行）。
    <b style="color:#b42318">红色斜纹</b> = 记录器被停止 / 删除的那段区间（重点标出来）。
    相邻的同一应用记录（间隔 ≤ 3 秒）已合并成一段。
  </div>
  <footer>由 <b>时迹 TimeTrace</b> 生成 · 数据来自 <b>RustTimeNoter</b> · __GEN__ · 作者：Weisifengbuxi</footer>
</div>
<div class="tip" id="tip"></div>
<script id="data" type="application/json">__DATA__</script>
<script>
(function(){
"use strict";
var M = JSON.parse(document.getElementById("data").textContent);
var DAY = 86400;
var APPS = Object.keys(M.colorMap);
var IDX = {};
for (var ai = 0; ai < APPS.length; ai++) IDX[APPS[ai]] = ai;
var state = { filter: null, fold: true, order: 1, dark: false };
try { state.fold = localStorage.getItem("rtn.fold") !== "0"; } catch (e) {}
var $ = function(s){ return document.querySelector(s); };

function pad(n){ n = Math.floor(n); return (n < 10 ? "0" : "") + n; }
function hms(t){ t = Math.max(0, Math.floor(t)); return pad(t/3600) + ":" + pad((t%3600)/60) + ":" + pad(t%60); }
function hm(t){ t = Math.max(0, Math.floor(t)); return pad(t/3600) + ":" + pad((t%3600)/60); }
function durTxt(sec){
  sec = Math.round(sec);
  if (sec < 60) return sec + " 秒";
  var h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60), s = sec%60;
  if (h) return m ? h + " 小时 " + m + " 分" : h + " 小时";
  if (s && m < 10) return m + " 分 " + s + " 秒";
  return m + " 分";
}
function esc(s){
  return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c];
  });
}
function colorOf(a){ return M.colorMap[a] || "#98a2b3"; }
function labelOf(a){ return M.labels[a] || a; }
function exeOf(a){ return String(a).split(/[\\/]/).pop(); }
function pct(v){ return (v*100).toFixed(3); }
function iconOf(app){ return (M.icons && M.icons[app]) || null; }
function iconHtml(app){
  var src = iconOf(app);
  if (src) return '<img class="ico" src="' + src + '" alt="">';
  return '<span class="sw" style="background:' + colorOf(app) + '"></span>';
}

/* ---------- 顶部概览 ---------- */
function chipHtml(app, secs, opts){
  var active = state.filter === app ? " active" : "";
  return '<span class="lg' + active + '" data-app="' + esc(app) + '"'
    + (opts && opts.link ? ' data-day="' + esc(opts.link) + '"' : "")
    + ' title="' + esc(app) + '">'
    + iconHtml(app)
    + esc(labelOf(app)) + '<span class="n">' + durTxt(secs) + '</span></span>';
}

function totalAppList(){
  var tot = {}, order = [];
  for (var i = 0; i < M.days.length; i++){
    var at = M.days[i].app_totals;
    for (var j = 0; j < at.length; j++){
      var a = at[j][0], v = at[j][1];
      if (!(a in tot)){ tot[a] = 0; order.push(a); }
      tot[a] += v;
    }
  }
  order.sort(function(x, y){ return tot[y] - tot[x]; });
  return order.map(function(a){ return [a, tot[a]]; });
}

function renderOverview(){
  var t = M.total;
  $("#sub").innerHTML = "共 " + M.days.length + " 天 · 总记录 " + durTxt(t.activity)
    + " · " + M.days[0].date + " ~ " + M.days[M.days.length-1].date;
  var items = [
    ["天数", M.days.length + " 天"],
    ["总记录时长", durTxt(t.activity)],
    ["会话数", t.sessions + " 段"],
    ["应用数", t.apps + " 个"],
    ["平均每天", durTxt(Math.round(t.activity / Math.max(1, M.days.length)))]
  ];
  var h = "";
  for (var i = 0; i < items.length; i++){
    h += '<div class="stat"><div class="k">' + items[i][0] + '</div><div class="v">' + items[i][1] + '</div></div>';
  }
  $("#stats").innerHTML = h;

  var list = totalAppList();
  var lg = "";
  for (var j = 0; j < list.length; j++) lg += chipHtml(list[j][0], list[j][1]);
  $("#legend").innerHTML = lg;

  var di = "";
  for (var k = M.days.length - 1; k >= 0; k--){
    var d = M.days[k];
    di += '<a href="#d' + d.date + '">' + d.date.slice(5) + " 周" + d.weekday
        + ' <span style="color:#98a2b3">' + durTxt(d.stats.activity) + "</span></a>";
  }
  $("#daysIndex").innerHTML = di;
}

/* ---------- 查应用 ---------- */
function shortDT(pair){
  return pair[0].slice(5) + " " + hms(pair[1]).slice(0, 5);
}
function renderAppTable(){
  var rows = "";
  for (var i = 0; i < M.appStats.length; i++){
    var r = M.appStats[i];
    var hay = (labelOf(r.a) + " " + exeOf(r.a) + " " + r.a).toLowerCase();
    rows += '<tr class="approw" data-app="' + esc(r.a) + '" data-hay="' + esc(hay) + '">'
      + '<td class="app">' + iconHtml(r.a)
      + esc(labelOf(r.a)) + '<span class="exe">' + esc(exeOf(r.a)) + '</span></td>'
      + '<td class="num">' + durTxt(r.total) + '</td>'
      + '<td>' + r.days + ' 天</td>'
      + '<td class="t">' + shortDT(r.first) + '</td>'
      + '<td class="t">' + shortDT(r.last) + '</td>'
      + '<td class="num">' + r.n + '</td></tr>';
  }
  $("#apptbody").innerHTML = rows;
  updateAppHint(M.appStats.length, "");
}
function updateAppHint(n, q){
  $("#apphint").textContent = q
    ? ("匹配 " + n + " 个应用")
    : ("共 " + n + " 个应用，按使用时长排序；输入进程名或名字可以过滤");
}
function findAppStat(app){
  for (var i = 0; i < M.appStats.length; i++) if (M.appStats[i].a === app) return M.appStats[i];
  return null;
}
function onAppRowClick(e){
  var tr = closestClass(e.target, "approw");
  if (!tr) return;
  var app = tr.getAttribute("data-app");
  var was = state.filter;
  toggleFilter(app);
  if (state.filter === app && was !== app){
    var st = findAppStat(app);
    if (st && st.last) jump(st.last[0], Math.max(0, st.last[1] - 1), true);
  }
}

/* ---------- 每天 ---------- */
function ribbonHtml(d){
  var segs = "";
  var s, i;
  for (i = 0; i < d.sessions.length; i++){
    s = d.sessions[i];
    segs += '<div class="seg" data-a="' + IDX[s.a] + '" data-w="' + pct(s.d/DAY) + '" style="left:' + pct(s.t/DAY)
          + '%;width:calc(' + pct(s.d/DAY) + '% + 0.7px);background:' + colorOf(s.a) + '"></div>';
  }
  for (i = 0; i < d.gaps.length; i++){
    s = d.gaps[i];
    segs += '<div class="seg gap" style="left:' + pct(s.t/DAY) + '%;width:calc(' + pct(s.d/DAY) + '% + 0.7px)"></div>';
  }
  return '<div class="ribbon" data-day="' + d.date + '"><div class="segbase" style="position:absolute;inset:0"></div>'
       + segs + usbMarksHtml(d) + lifeSpansHtml(d) + '<div class="cursorline"></div></div>';
}

/* U 盘接入在色带上的标记 */
var USB_BY_DATE = {};
function usbMarksHtml(d){
  var list = USB_BY_DATE[d.date];
  if (!list) return "";
  var h = "";
  for (var i = 0; i < list.length; i++){
    h += '<div class="usbmark" style="left:' + (list[i].t / DAY * 100).toFixed(3)
       + '%" title="U 盘接入 ' + hms(list[i].t) + (list[i].letter ? "（" + list[i].letter + ":）" : "") + '"></div>';
  }
  return h;
}

/* 记录器状态：启动 / 停止 / 被删除 */
var LIFE_BY_DATE = {};
var LIFE_SPANS = {};
function lifeLabel(kind){
  return {installed: "安装", started: "记录器启动", stopped: "记录器停止",
          missing: "记录器被删除", restored: "记录器恢复"}[kind] || kind;
}
function lifeMarksHtml(d){
  var list = LIFE_BY_DATE[d.date];
  if (!list) return "";
  var h = "";
  for (var i = 0; i < list.length; i++){
    var e = list[i];
    var bad = (e.kind === "stopped" || e.kind === "missing");
    h += '<div class="lifemark' + (bad ? "" : " ok") + '" style="left:' + (e.t / DAY * 100).toFixed(3)
       + '%" title="' + lifeLabel(e.kind) + " " + hms(e.t) + '"></div>';
  }
  return h;
}
function lifeSpansHtml(d){
  var list = LIFE_SPANS[d.date];
  if (!list) return "";
  var h = "";
  for (var i = 0; i < list.length; i++){
    var s = list[i];
    h += '<div class="lifespan" style="left:' + (s.t / DAY * 100).toFixed(3)
       + '%;width:calc(' + (s.d / DAY * 100).toFixed(3) + '% + 0.5px)"'
       + ' title="记录中断：' + hms(s.t) + ' 起，持续 ' + durTxt(s.d) + '（这段时间没有记录）"></div>';
  }
  return h;
}
function renderLifecycle(){
  var list = M.lifecycle || [];
  if (!list.length) return;
  var rows = "";
  for (var i = 0; i < list.length; i++){
    var e = list[i];
    var bad = (e.kind === "stopped" || e.kind === "missing");
    rows += '<tr><td class="t">' + e.date.slice(5) + " " + hms(e.t) + '</td><td>'
      + (bad ? '<span class="lifebad">' + lifeLabel(e.kind) + '</span>'
             : '<span class="lifeok">' + lifeLabel(e.kind) + '</span>') + '</td></tr>';
  }
  $("#lifetbody").innerHTML = rows;
  $("#lifehint").textContent = "共 " + list.length + " 条";
  $("#lifecard").style.display = "";
}

function axisHtml(){
  var h = "";
  for (var i = 0; i <= 24; i++){
    h += '<div class="tick" style="left:' + (i/24*100) + '%">' + pad(i) + '</div>';
  }  return '<div class="axis">' + h + '</div>';
}

function tableHtml(d){
  if (!d.sessions.length) return '<div class="empty">这一天没有记录</div>';
  var rows = "";
  for (var i = 0; i < d.sessions.length; i++){
    var s = d.sessions[i];
    rows += '<tr data-a="' + IDX[s.a] + '" data-t="' + s.t + '" data-d="' + s.d + '">'
      + '<td class="t">' + hms(s.t) + '</td><td class="t">' + hms(s.t + s.d) + '</td>'
      + '<td class="app" title="' + esc(s.a) + '">' + iconHtml(s.a)
      + esc(labelOf(s.a)) + '<span class="exe">' + esc(exeOf(s.a)) + '</span></td>'
      + '<td class="num">' + durTxt(s.d) + '</td></tr>';
  }
  return '<table><thead><tr><th>开始</th><th>结束</th><th>应用</th><th style="text-align:right">时长</th></tr></thead>'
       + '<tbody>' + rows + '</tbody></table>';
}

/* 每小时主要在用哪个应用：24 个格子，格子里是那个应用的真实图标 */
function hourIconsHtml(d){
  var per = [], h, i;
  for (h = 0; h < 24; h++) per.push({});
  for (i = 0; i < d.sessions.length; i++){
    var s = d.sessions[i];
    for (h = 0; h < 24; h++){
      var a = Math.max(s.t, h*3600), b = Math.min(s.t + s.d, (h+1)*3600);
      if (b > a) per[h][s.a] = (per[h][s.a] || 0) + (b - a);
    }
  }
  var out = '<div class="hstrip">';
  for (h = 0; h < 24; h++){
    var best = null, bestV = 0, k;
    for (k in per[h]) if (per[h][k] > bestV){ best = k; bestV = per[h][k]; }
    if (!best){
      out += '<div class="hcell"></div>';
      continue;
    }
    out += '<div class="hcell hoverable" data-app="' + esc(best) + '" title="'
         + pad(h) + ':00 主要在用 ' + esc(labelOf(best)) + '（' + durTxt(bestV) + '）">'
         + iconHtml(best)
         + '<span class="hbar" style="background:' + colorOf(best) + '"></span></div>';
  }
  return out + '</div>';
}

function dayHtml(d){
  var st = d.stats;
  var cover = st.first == null ? "—" : hm(st.first) + " – " + hm(st.last);
  var sub = '<div class="daysub"><b>' + durTxt(st.activity) + '</b>'
    + '<span class="sep">·</span>' + st.sessions + ' 段'
    + '<span class="sep">·</span>覆盖 ' + cover
    + '<span class="sep">·</span>空档 ' + durTxt(st.gap)
    + '<span class="sep">·</span>未记录 ' + durTxt(st.untracked)
    + '</div>';
  var spList = LIFE_SPANS[d.date];
  if (spList && spList.length){
    sub = sub.replace('</div>',
      '<span class="sep">·</span><b style="color:#b42318">记录中断 ' + spList.length + ' 次</b>'
      + '<span class="lifelegend" title="红色斜纹 = 记录器被停止 / 删除的那段区间"></span></div>');
  }

  var chips = "";
  for (var i = 0; i < d.app_totals.length; i++) chips += chipHtml(d.app_totals[i][0], d.app_totals[i][1]);

  var usbList = USB_BY_DATE[d.date];
  if (usbList && usbList.length){
    sub += '<div class="daysub" style="color:#0f766e">U 盘接入 ' + usbList.length + ' 次'
         + '<span class="sep">·</span>' + usbList.map(function(e){ return hm(e.t); }).join("、")
         + '</div>';
  }

  return '<section class="card day" id="d' + d.date + '">'
    + '<div class="dayhead">'
    +   '<div><h3>' + d.date + '<span class="wd">星期' + d.weekday + '</span></h3>' + sub + '</div>'
    +   '<button class="foldbtn" data-day="' + d.date + '">展开表格</button>'
    + '</div>'
    + ribbonHtml(d) + axisHtml() + hourIconsHtml(d)
    + '<div class="chips">' + chips + '</div>'
    + '<div class="daytable">' + tableHtml(d) + '</div>'
    + '</section>';
}

function renderUsb(){
  var list = M.usb || [];
  var files = M.usbfiles || [];
  var dls = M.usbdl || [];
  if (!list.length && !files.length && !dls.length) return;

  if (list.length){
    var h = "";
    for (var i = list.length - 1; i >= 0; i--){
      var e = list[i];
      h += '<tr><td class="t">' + e.date.slice(5) + " " + hm(e.t) + '</td>'
         + '<td>USB VID_' + esc(e.vid) + '&amp;PID_' + esc(e.pid) + '</td>'
         + '<td>' + (e.letter ? esc(e.letter) + ":" : "—") + '</td></tr>';
    }
    $("#usbtbody").innerHTML = h;
  } else {
    $("#usbconnwrap").style.display = "none";
  }

  if (files.length){
    var fh = "";
    for (var j = 0; j < files.length; j++){
      var f = files[j];
      fh += '<tr><td class="t">' + f.date.slice(5) + " " + hm(f.t) + '</td>'
          + '<td class="usbpath" title="' + esc(f.path) + '">' + esc(f.path) + '</td>'
          + '<td>' + esc(f.drive) + ':</td></tr>';
    }
    $("#usbfilestbody").innerHTML = fh;
    $("#usbfileshint").textContent = "共 " + files.length + " 条";
    $("#usbfileswrap").style.display = "";
  } else {
    $("#usbfilestbody").innerHTML = '<tr><td colspan="3" class="hint">没有查到（Windows 只记录「打开过」的文件，复制粘贴到 U 盘不算）</td></tr>';
    $("#usbfileshint").textContent = "";
    $("#usbfileswrap").style.display = "";
  }

  if (dls.length){
    var dh = "";
    for (var k = 0; k < dls.length; k++){
      var d = dls[k];
      dh += '<tr><td class="t">' + d.date.slice(5) + " " + hm(d.t) + '</td>'
          + '<td class="usbpath" title="' + esc(d.path) + '">' + esc(d.path) + '</td>'
          + '<td class="num">' + (d.size ? humanSize(d.size) : "") + '</td>'
          + '<td>' + esc(d.drive) + ':</td></tr>';
    }
    $("#usbdltbody").innerHTML = dh;
    $("#usbdlhint").textContent = "共 " + dls.length + " 条";
    $("#usbdlwrap").style.display = "";
  } else {
    $("#usbdltbody").innerHTML = '<tr><td colspan="4" class="hint">没有查到（只统计浏览器直接下载到 U 盘的记录）</td></tr>';
    $("#usbdlhint").textContent = "";
    $("#usbdlwrap").style.display = "";
  }

  var bits = [];
  if (list.length) bits.push("接入 " + list.length + " 次");
  if (files.length) bits.push("打开 " + files.length + " 条");
  if (dls.length) bits.push("下载 " + dls.length + " 条");
  $("#usbhint").textContent = bits.join(" · ") + " · 来自 Windows 记录";
  $("#usbcard").style.display = "";
}
function humanSize(n){
  if (!n || n <= 0) return "";
  var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
  while (n >= 1024 && i < u.length - 1){ n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + " " + u[i];
}
function indexUsb(){
  USB_BY_DATE = {};
  var list = M.usb || [];
  for (var i = 0; i < list.length; i++){
    var e = list[i];
    if (!USB_BY_DATE[e.date]) USB_BY_DATE[e.date] = [];
    USB_BY_DATE[e.date].push(e);
  }
}

function renderDays(){
  var h = "";
  // 默认「大到小」（新的日期在前），点按钮切换
  if (state.order === 1){
    for (var i = M.days.length - 1; i >= 0; i--) h += dayHtml(M.days[i]);
  } else {
    for (var j = 0; j < M.days.length; j++) h += dayHtml(M.days[j]);
  }
  $("#days").innerHTML = h;
  applyFold();
  decorateRibbons();
}

/* ---------- 色带上的应用图标（只贴在够宽的会话上） ---------- */
function decorateRibbons(){
  var ribs = document.querySelectorAll("#days .ribbon");
  for (var i = 0; i < ribs.length; i++){
    var rib = ribs[i];
    var old = rib.querySelectorAll(".segico");
    for (var k = 0; k < old.length; k++) rib.removeChild(old[k]);
    var w = rib.clientWidth;
    if (!w) continue;
    var segs = rib.querySelectorAll(".seg[data-a]");
    var runApp = null, runRight = -1, runIcon = false;
    for (var j = 0; j < segs.length; j++){
      var seg = segs[j];
      var app = APPS[parseInt(seg.getAttribute("data-a"), 10)];
      var left = parseFloat(seg.style.left) || 0;
      var wp = parseFloat(seg.getAttribute("data-w")) || 0;
      // 同一应用、间隔 ≤5 分钟（0.35%）的相邻小块算同一段，只贴一个图标
      if (!(app === runApp && left - runRight < 0.35)){
        runApp = app;
        runIcon = false;
      }
      runRight = left + wp;
      if (runIcon) continue;
      var src = M.icons && M.icons[app];
      if (!src) continue;
      if (wp / 100 * w < 14) continue;
      var img = document.createElement("img");
      img.className = "segico";
      img.alt = "";
      img.src = src;
      img.style.left = "calc(" + left + "% + 2px)";
      rib.appendChild(img);
      runIcon = true;
    }
  }
}
var resizeTimer = null;
window.addEventListener("resize", function(){
  if (resizeTimer) clearTimeout(resizeTimer);
  resizeTimer = setTimeout(decorateRibbons, 150);
});

/* ---------- 表格收起 / 展开 ---------- */
function setSectionFold(secEl, collapsed){
  if (collapsed) secEl.classList.add("collapsed"); else secEl.classList.remove("collapsed");
  var b = secEl.querySelector(".foldbtn");
  if (b) b.textContent = collapsed ? "展开表格" : "收起表格";
}
function applyFold(){
  var secs = document.querySelectorAll("#days section.day");
  for (var i = 0; i < secs.length; i++) setSectionFold(secs[i], state.fold);
  var cb = document.getElementById("fold");
  if (cb) cb.checked = state.fold;
}
function setAllFold(collapsed){
  var secs = document.querySelectorAll("#days section.day");
  for (var i = 0; i < secs.length; i++) setSectionFold(secs[i], collapsed);
}
function toggleDay(secEl){
  setSectionFold(secEl, !secEl.classList.contains("collapsed"));
}

/* ---------- 保存本页 ---------- */
function savePage(){
  var clone = document.documentElement.cloneNode(true);
  var wipe = ["days", "apptbody", "legend", "stats", "daysIndex", "sub"];
  for (var i = 0; i < wipe.length; i++){
    var node = clone.querySelector("#" + wipe[i]);
    if (node) node.innerHTML = "";
  }
  var tip = clone.querySelector("#tip");
  if (tip){ tip.style.display = "none"; tip.innerHTML = ""; }
  var q = clone.querySelector("#q"); if (q) q.setAttribute("value", "");
  var aq = clone.querySelector("#appq"); if (aq) aq.setAttribute("value", "");
  var html = "<!DOCTYPE html>\n" + clone.outerHTML;
  var blob = new Blob([html], { type: "text/html;charset=utf-8" });
  var a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "操作时间线-" + M.days[0].date + "_" + M.days[M.days.length-1].date + ".html";
  document.body.appendChild(a);
  a.click();
  window.setTimeout(function(){
    URL.revokeObjectURL(a.href);
    if (a.parentNode) a.parentNode.removeChild(a);
  }, 800);
}

/* ---------- 交互 ---------- */
var tipEl = $("#tip");
function closestRibbon(node){
  while (node && node !== document){
    if (node.classList && node.classList.contains("ribbon")) return node;
    node = node.parentNode;
  }
  return null;
}
function sessionAt(d, sec){
  var lo = 0, hi = d.sessions.length - 1;
  while (lo <= hi){
    var mid = (lo + hi) >> 1, s = d.sessions[mid];
    if (sec < s.t) hi = mid - 1;
    else if (sec >= s.t + s.d) lo = mid + 1;
    else return s;
  }
  return null;
}
function dayOf(dateStr){
  for (var i = 0; i < M.days.length; i++) if (M.days[i].date === dateStr) return M.days[i];
  return null;
}
function secFromEvent(rib, e){
  var r = rib.getBoundingClientRect();
  var f = (e.clientX - r.left) / r.width;
  if (f < 0) f = 0;
  if (f > 1) f = 1;
  return f * DAY;
}
function showTip(e, html){
  tipEl.innerHTML = html;
  tipEl.style.display = "block";
  var x = e.clientX + 14, y = e.clientY + 16;
  if (x + tipEl.offsetWidth > window.innerWidth - 8) x = e.clientX - tipEl.offsetWidth - 12;
  if (y + tipEl.offsetHeight > window.innerHeight - 8) y = e.clientY - tipEl.offsetHeight - 12;
  tipEl.style.left = x + "px";
  tipEl.style.top = y + "px";
}
function hideTip(){ tipEl.style.display = "none"; }

function onMove(e){
  var rib = closestRibbon(e.target);
  if (!rib){ hideTip(); return; }
  var d = dayOf(rib.getAttribute("data-day"));
  var sec = secFromEvent(rib, e);
  var s = d ? sessionAt(d, sec) : null;
  var html;
  if (s){
    var ico = M.icons && M.icons[s.a]
      ? '<img class="ico" src="' + M.icons[s.a] + '" style="margin-right:5px">' : "";
    html = ico + "<b>" + esc(labelOf(s.a)) + "</b><br><span class='t'>"
         + hms(s.t) + " – " + hms(s.t + s.d) + " · " + durTxt(s.d) + "</span>";
  } else {
    html = "<span class='t'>" + hms(sec) + " · 这个时刻没有记录</span>";
  }
  showTip(e, html);
}

function markCursor(rib, sec){
  var cl = rib.querySelector(".cursorline");
  cl.style.display = "block";
  cl.style.left = (sec/DAY*100) + "%";
}
function highlightRow(secEl, sec){
  var rows = secEl.querySelectorAll("tbody tr");
  var best = null;
  for (var i = 0; i < rows.length; i++){
    var t = parseFloat(rows[i].getAttribute("data-t"));
    var dd = parseFloat(rows[i].getAttribute("data-d"));
    if (sec >= t && sec < t + dd){ best = rows[i]; break; }
  }
  var old = document.querySelectorAll("tr.hit");
  for (var k = 0; k < old.length; k++) old[k].className = old[k].className.replace(/\s*hit/g, "");
  if (best) best.className += " hit";
  return best;
}

function closestClass(node, cls){
  while (node && node !== document){
    if (node.classList && node.classList.contains(cls)) return node;
    node = node.parentNode;
  }
  return null;
}

function toggleFilter(app){
  state.filter = (state.filter === app) ? null : app;
  applyFilter();
}

function onClick(e){
  var lg = closestClass(e.target, "lg");
  if (lg && lg.getAttribute("data-app")){ toggleFilter(lg.getAttribute("data-app")); return; }
  var hc = closestClass(e.target, "hcell");
  if (hc && hc.getAttribute("data-app")){ toggleFilter(hc.getAttribute("data-app")); return; }
  var head = closestClass(e.target, "dayhead");
  if (head){
    var sec = closestClass(head, "day");
    if (sec){ toggleDay(sec); return; }
  }
  var rib = closestRibbon(e.target);
  if (!rib) return;
  var sec2 = secFromEvent(rib, e);
  markCursor(rib, sec2);
  var secEl = document.getElementById("d" + rib.getAttribute("data-day"));
  if (secEl) highlightRow(secEl, sec2);
}

function applyFilter(){
  var idx = state.filter == null ? -1 : IDX[state.filter];
  var i;
  var segs = document.querySelectorAll("#days .seg[data-a]");
  for (i = 0; i < segs.length; i++){
    var on = idx < 0 || parseInt(segs[i].getAttribute("data-a"), 10) === idx;
    if (on) segs[i].classList.remove("dim"); else segs[i].classList.add("dim");
  }
  var rows = document.querySelectorAll("#days tbody tr");
  var perDay = {};
  for (i = 0; i < rows.length; i++){
    var showRow = idx < 0 || parseInt(rows[i].getAttribute("data-a"), 10) === idx;
    rows[i].style.display = showRow ? "" : "none";
    if (showRow){
      var sec = closestClass(rows[i], "day");
      if (sec) perDay[sec.id] = (perDay[sec.id] || 0) + 1;
    }
  }
  // 只看某个应用时，把那天完全没用到它的卡片也收起来
  var sections = document.querySelectorAll("#days section.day");
  for (i = 0; i < sections.length; i++){
    sections[i].style.display = (idx < 0 || perDay[sections[i].id]) ? "" : "none";
  }
  var chips = document.querySelectorAll(".lg[data-app]");
  for (i = 0; i < chips.length; i++){
    var active = state.filter != null && chips[i].getAttribute("data-app") === state.filter;
    if (active) chips[i].classList.add("active"); else chips[i].classList.remove("active");
  }
  var appRows = document.querySelectorAll("#apptbody tr");
  for (i = 0; i < appRows.length; i++){
    var act = state.filter != null && appRows[i].getAttribute("data-app") === state.filter;
    if (act) appRows[i].classList.add("active"); else appRows[i].classList.remove("active");
  }
  if (state.filter == null){
    $("#filterhint").textContent = "";
  } else {
    var st = findAppStat(state.filter);
    $("#filterhint").textContent = "只看 " + labelOf(state.filter)
      + (st ? "（" + st.days + " 天 · 共 " + durTxt(st.total) + "）" : "")
      + " · 再点一次取消";
  }
}

/* ---------- 时间定位 ---------- */
function parseQuery(q){
  q = (q || "").trim();
  var m = q.match(/^(?:(\d{4})[-/.](\d{1,2})[-/.](\d{1,2}))?\s*(\d{1,2})[:：时点]?\s*(\d{1,2})?(?:[:：分]\s*(\d{1,2}))?/);
  if (!m) return null;
  var dateStr = null;
  if (m[1]) dateStr = m[1] + "-" + pad(m[2]) + "-" + pad(m[3]);
  var hh = parseInt(m[4], 10);
  var mm = m[5] != null ? parseInt(m[5], 10) : 0;
  var ss = m[6] != null ? parseInt(m[6], 10) : 0;
  if (isNaN(hh) || hh > 23 || mm > 59) return null;
  return { date: dateStr, sec: hh*3600 + mm*60 + ss };
}

function jump(dayStr, sec, smooth){
  var secEl = document.getElementById("d" + dayStr);
  if (!secEl) return false;
  var rib = secEl.querySelector(".ribbon");
  markCursor(rib, sec);
  var row = highlightRow(secEl, sec);
  var target = row || secEl;
  if (target.scrollIntoView) target.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "center" });
  return true;
}

$("#go").addEventListener("click", function(){
  var p = parseQuery($("#q").value);
  if (!p){ alert("看不懂这个时间，试试 14:30 或 2026-10-26 14:30"); return; }
  if (p.date){
    if (!jump(p.date, p.sec, true)) alert("没有 " + p.date + " 的数据");
    return;
  }
  // 没写日期：从最近一天往前找，定位到第一处有记录（或最近）的那天
  for (var i = M.days.length - 1; i >= 0; i--){
    var d = M.days[i];
    if (sessionAt(d, p.sec)){ jump(d.date, p.sec, true); return; }
  }
  jump(M.days[M.days.length-1].date, p.sec, true);
});
$("#q").addEventListener("keydown", function(e){ if (e.key === "Enter") $("#go").click(); });

document.getElementById("days").addEventListener("mousemove", onMove);
document.getElementById("days").addEventListener("mouseleave", hideTip);
document.getElementById("days").addEventListener("click", onClick);
document.getElementById("legend").addEventListener("click", onClick);
document.getElementById("apptbody").addEventListener("click", onAppRowClick);
document.getElementById("fold").addEventListener("change", function(){
  state.fold = this.checked;
  try { localStorage.setItem("rtn.fold", state.fold ? "1" : "0"); } catch (e) {}
  applyFold();
});
document.getElementById("expandAll").addEventListener("click", function(){ setAllFold(false); });
document.getElementById("collapseAll").addEventListener("click", function(){ setAllFold(true); });

/* 主题切换 */
document.getElementById("theme").addEventListener("click", function(){
  state.dark = !state.dark;
  document.body.classList.toggle("dark", state.dark);
  this.textContent = state.dark ? "☀ 亮色" : "🌙 暗色";
});

/* 日期排序：默认大到小 */
document.getElementById("orderBtn").addEventListener("click", function(){
  state.order = state.order === 1 ? -1 : 1;
  this.textContent = state.order === 1 ? "日期：新→旧" : "日期：旧→新";
  renderDays();
});

/* U 盘记录 / 记录器状态 的展开收起 */
Array.prototype.forEach.call(document.querySelectorAll(".foldbtn"), function(btn){
  btn.addEventListener("click", function(){
    var el = document.getElementById(btn.getAttribute("data-target"));
    if (!el) return;
    var hidden = el.style.display === "none";
    el.style.display = hidden ? "" : "none";
    btn.textContent = hidden ? "收起" : "展开";
  });
});
document.getElementById("save").addEventListener("click", savePage);
document.getElementById("appq").addEventListener("input", function(){
  var q = this.value.trim().toLowerCase();
  var rows = document.querySelectorAll("#apptbody tr");
  var shown = 0;
  for (var i = 0; i < rows.length; i++){
    var ok = !q || rows[i].getAttribute("data-hay").indexOf(q) >= 0;
    rows[i].style.display = ok ? "" : "none";
    if (ok) shown++;
  }
  updateAppHint(shown, q);
});

indexUsb();
(function(){
  var lc = M.lifecycle || [], i, e;
  for (i = 0; i < lc.length; i++){
    e = lc[i];
    (LIFE_BY_DATE[e.date] = LIFE_BY_DATE[e.date] || []).push(e);
  }
  var sp = M.lifeSpans || [];
  for (i = 0; i < sp.length; i++){
    (LIFE_SPANS[sp[i].date] = LIFE_SPANS[sp[i].date] || []).push(sp[i]);
  }
})();
renderOverview();
renderLifecycle();
renderUsb();
renderAppTable();
renderDays();
})();
</script>
</body>
</html>
"""

def render_html(model, title=APP_TITLE):
    data = json.dumps(model, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("</", "<\\/")
    return (
        HTML_TEMPLATE
        .replace("__TITLE__", title)
        .replace("__GEN__", model["generated"])
        .replace("__DATA__", data)
    )


# --------------------------------------------------------------------------
# 文本输出
# --------------------------------------------------------------------------
def hms_local(sec):
    sec = max(0, int(sec))
    return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def disp_width(text):
    """中文按 2 列算，用来对齐终端里的表格。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad_disp(text, width):
    return text + " " * max(0, width - disp_width(text))


def dur_local(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return "%d 秒" % seconds
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    if h:
        return "%d 小时 %d 分" % (h, m) if m else "%d 小时" % h
    if s and m < 10:
        return "%d 分 %d 秒" % (m, s)
    return "%d 分" % m


def label_for(model, app):
    return model["labels"].get(app, base_name(app))


def find_day(model, date_str):
    for d in model["days"]:
        if d["date"] == date_str:
            return d
    return None


def print_day(model, date_str):
    d = find_day(model, date_str)
    if d is None:
        print("没有 %s 的记录。可用日期：%s" % (date_str, "、".join(x["date"] for x in model["days"])))
        return
    st = d["stats"]
    print()
    print("=" * 72)
    print(" %s 星期%s · 记录 %s · %d 段会话" % (d["date"], d["weekday"], dur_local(st["activity"]), st["sessions"]))
    if st["first"] is not None:
        print(" 覆盖时段 %s – %s · 空档 %s · 无记录 %s"
              % (hms_local(st["first"]), hms_local(st["last"]),
                 dur_local(st["gap"]), dur_local(st["untracked"])))
    print("=" * 72)

    timeline = [(s["t"], s["t"] + s["d"], s["a"], "act") for s in d["sessions"]]
    timeline += [(g["t"], g["t"] + g["d"], "", "gap") for g in d["gaps"]]
    timeline.sort()
    prev_end = None
    for t, e, app, kind in timeline:
        if prev_end is not None and t - prev_end >= 60:
            print("        ...   （这段没有记录，%s）" % dur_local(t - prev_end))
        if kind == "gap":
            print("%s - %s  %s%s" % (hms_local(t), hms_local(e),
                                     pad_disp(dur_local(e - t), 12), "（空档）"))
        else:
            print("%s - %s  %s%s" % (hms_local(t), hms_local(e),
                                     pad_disp(dur_local(e - t), 12), label_for(model, app)))
        prev_end = e if prev_end is None else max(prev_end, e)

    print("-" * 72)
    print(" 按应用合计")
    for app, secs in d["app_totals"]:
        print("   %s%s" % (pad_disp(label_for(model, app), 30), dur_local(secs)))
    print()


def print_at(model, date_str, sec, context_min=10):
    d = find_day(model, date_str)
    if d is None:
        print("没有 %s 的记录。可用日期：%s" % (date_str, "、".join(x["date"] for x in model["days"])))
        return
    print()
    print("=" * 72)
    print(" %s %s 这一时刻" % (d["date"], hms_local(sec)))
    print("=" * 72)

    hit = [s for s in d["sessions"] if s["t"] <= sec < s["t"] + s["d"]]
    gap = [g for g in d["gaps"] if g["t"] <= sec < g["t"] + g["d"]]
    if hit:
        s = hit[0]
        idx = d["sessions"].index(s) + 1
        print(" 正在使用：%s" % label_for(model, s["a"]))
        print(" 进程路径：%s" % s["a"])
        print(" 本段会话：%s → %s（共 %s，该时刻已进行 %s，第 %d 段）"
              % (hms_local(s["t"]), hms_local(s["t"] + s["d"]), dur_local(s["d"]),
                 dur_local(sec - s["t"]), idx))
    elif gap:
        g = gap[0]
        print(" 这一时刻属于空档（有记录但未归属到应用）：%s → %s（%s）"
              % (hms_local(g["t"]), hms_local(g["t"] + g["d"]), dur_local(g["d"])))
    else:
        print(" 这一时刻没有任何记录（离开电脑 / 关机 / 记录器未运行）")
        before = [s for s in d["sessions"] if s["t"] + s["d"] <= sec]
        after = [s for s in d["sessions"] if s["t"] > sec]
        if before:
            b = before[-1]
            print(" 上一段：%s %s → %s" % (label_for(model, b["a"]), hms_local(b["t"]), hms_local(b["t"] + b["d"])))
        if after:
            a = after[0]
            print(" 下一段：%s 从 %s 开始" % (label_for(model, a["a"]), hms_local(a["t"])))

    lo = max(0, sec - context_min * 60)
    hi = min(86400, sec + context_min * 60)
    print()
    print(" 前后 %d 分钟（%s – %s）出现过：" % (context_min, hms_local(lo), hms_local(hi)))
    totals = {}
    for s in d["sessions"]:
        overlap = min(s["t"] + s["d"], hi) - max(s["t"], lo)
        if overlap > 0:
            totals[s["a"]] = totals.get(s["a"], 0) + overlap
    for app, secs in sorted(totals.items(), key=lambda kv: -kv[1]):
        print("   %s%s" % (pad_disp(label_for(model, app), 30), dur_local(secs)))
    if not totals:
        print("   （这段时间没有任何记录）")

    near = [s for s in d["sessions"] if s["t"] < hi and s["t"] + s["d"] > lo]
    if near:
        print()
        print(" 这段区间里的会话：")
        for s in near:
            mark = " ←" if s in hit else ""
            print("   %s - %s  %s%s%s" % (hms_local(s["t"]), hms_local(s["t"] + s["d"]),
                                          pad_disp(dur_local(s["d"]), 12),
                                          label_for(model, s["a"]), mark))
    print()


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------
def parse_time_text(text, default_date):
    """支持 '14:30'、'1430'、'2026-10-26 14:30'、'14:30:15'。"""
    text = text.strip()
    m = re.match(
        r"^(?:(\d{4})[-/.](\d{1,2})[-/.](\d{1,2}))?\s*"
        r"(\d{1,2})[:：时点]?\s*(\d{1,2})?(?:[:：分]\s*(\d{1,2}))?",
        text,
    )
    if not m:
        return None
    ds = None
    if m.group(1):
        ds = "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
    hh = int(m.group(4))
    mm = int(m.group(5)) if m.group(5) is not None else 0
    ss = int(m.group(6)) if m.group(6) is not None else 0
    if hh > 23 or mm > 59 or ss > 59:
        return None
    return (ds or default_date), hh * 3600 + mm * 60 + ss


def all_log_days():
    """直接从数据目录的文件名（YYYY-MM-DD.log）找出有记录的日期，不必先导出。"""
    root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "RustTimeNoter", "data")
    out = []
    for dirpath, _dirnames, files in os.walk(root):
        for name in files:
            m = re.match(r"^(\d{4}-\d{2}-\d{2})\.log$", name)
            if m:
                try:
                    out.append(datetime.strptime(m.group(1), "%Y-%m-%d").date())
                except ValueError:
                    pass
    return sorted(set(out))


def prune_logs(days=RETENTION_DAYS, dry_run=False):
    """删除早于保留期的日志分片；只动 YYYY-MM-DD.log，别的文件一律不碰。"""
    root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "RustTimeNoter", "data")
    cutoff = date.today() - timedelta(days=max(1, days) - 1)
    removed, kept, freed = [], 0, 0
    if not os.path.isdir(root):
        return cutoff, removed, kept, freed
    for dirpath, _dirnames, files in os.walk(root):
        for name in files:
            m = re.match(r"^(\d{4}-\d{2}-\d{2})\.log$", name)
            if not m:
                continue
            try:
                d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            path = os.path.join(dirpath, name)
            if d < cutoff:
                try:
                    size = os.path.getsize(path)
                    if not dry_run:
                        os.remove(path)
                except OSError:
                    continue
                removed.append((d, path, size))
                freed += size
            else:
                kept += 1
    # 顺手收掉删空的 YYYY / MM 目录
    if not dry_run:
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            if dirpath == root or filenames or dirnames:
                continue
            if re.search(r"[\\/](\d{4}|\d{2})$", dirpath):
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
    return cutoff, removed, kept, freed


def auto_prune(args):
    """除了显式 --no-prune，每次用到数据前都清一次过期分片。"""
    if getattr(args, "no_prune", False):
        return
    days = getattr(args, "retention", RETENTION_DAYS) or RETENTION_DAYS
    _cutoff, removed, _kept, freed = prune_logs(days)
    if removed:
        print("已清理 %d 个超过 %d 天的日志分片（释放 %.0f KB）" % (len(removed), days, freed / 1024.0))


def resolve_range(args, default_days=None):
    """有 --days/--from/--to 就按它；否则用保留期内的日子。

    注意：日志分片是按 **UTC 日** 命名的，本地日期与它最多差一天。
    所以结尾一律用「本地今天」，不能拿最后那个分片的文件名当结尾，
    否则本地 0:00–8:00（UTC+8）这段会被漏掉。
    """
    if args.days or args.from_day or args.to_day:
        return range_from_args(args)
    days = default_days or getattr(args, "retention", RETENTION_DAYS) or RETENTION_DAYS
    logs = all_log_days()
    t = date.today()
    f = logs[0] if logs else t - timedelta(days=days - 1)
    floor = t - timedelta(days=days - 1)
    if f < floor:
        f = floor
    if f > t:
        f = t
    return f, t


def range_from_args(args, default_days=7):
    days = args.days or default_days
    if args.from_day or args.to_day:
        f = datetime.strptime(args.from_day, "%Y-%m-%d").date() if args.from_day else None
        t = datetime.strptime(args.to_day, "%Y-%m-%d").date() if args.to_day else None
        if f is None:
            f = t - timedelta(days=days - 1)
        if t is None:
            t = date.today()
        if f > t:
            f, t = t, f
        return f, t
    t = date.today()
    return t - timedelta(days=days - 1), t


def cmd_prune(args):
    cutoff, removed, kept, freed = prune_logs(args.days, args.dry_run)
    print()
    if args.dry_run:
        print("（演练模式：只列出会删什么，没有真的删除）")
    print("保留最近 %d 天（%s 及以后），当前保留 %d 个分片" % (args.days, cutoff.isoformat(), kept))
    if removed:
        print("%s %d 个过期分片（%.1f KB）：" % ("将删除" if args.dry_run else "已删除",
                                              len(removed), freed / 1024.0))
        for d, path, size in removed[:40]:
            print("  %s  %.1f KB  %s" % (d.isoformat(), size / 1024.0, path))
        if len(removed) > 40:
            print("  …还有 %d 个" % (len(removed) - 40))
    else:
        print("没有需要删除的过期分片。")
    print()


def cmd_html(args):
    auto_prune(args)
    f, t = resolve_range(args)
    records = export_records(f, t)
    days = build_days(records)
    if not days:
        sys.exit("这段时间（%s 至 %s）没有任何记录。" % (f, t))
    model = build_model(days, with_icons=not getattr(args, "no_icons", False))
    if not getattr(args, "no_events", False):
        ef, et = event_range(args, t)
        model["usb"] = load_usb_events(ef, et)
        model["usbfiles"] = load_recent_usb_files()
        model["usbdl"] = load_usb_downloads()
    html = render_html(model)

    out = args.out
    if not out:
        out = os.path.join(tempfile.gettempdir(), "时迹_时间线.html")
    out = os.path.abspath(out)
    with open(out, "w", encoding="utf-8") as fp:
        fp.write(html)

    print("时间线已生成：%s" % out)
    print("覆盖 %s 至 %s，共 %d 天、%d 条会话。"
          % (days[0]["date"], days[-1]["date"], len(days),
             sum(d["stats"]["sessions"] for d in days)))
    icons = model.get("icons") or {}
    if icons:
        print("应用图标：%d/%d 个已内嵌（缓存于 %s）" % (len(icons), len(model["colorMap"]), ICON_CACHE_DIR))
    usb = model.get("usb") or []
    if usb:
        print("U 盘接入：%d 次（来自 Windows 设备日志）" % len(usb))
    files = model.get("usbfiles") or []
    if files:
        print("U 盘文件：%d 条（来自“最近使用”记录）" % len(files))
    dl = model.get("usbdl") or []
    if dl:
        print("下载到 U 盘：%d 条（来自浏览器下载记录）" % len(dl))
    if not args.no_open and not os.environ.get("TT_NO_OPEN"):
        try:
            os.startfile(out)  # noqa: S606  (Windows)
        except (AttributeError, OSError):
            import webbrowser
            webbrowser.open("file:///" + out.replace("\\", "/"))


def cmd_day(args):
    auto_prune(args)
    if getattr(args, "ask", False) and not args.date:
        try:
            args.date = input("日期（YYYY-MM-DD，直接回车 = 最近一天）：").strip() or None
        except (EOFError, KeyboardInterrupt):
            return
    if args.date and not (args.days or args.from_day or args.to_day):
        f = t = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        f, t = range_from_args(args)
    records = export_records(f, t)
    days = build_days(records)
    if not days:
        sys.exit("这段时间没有任何记录。")
    model = build_model(days)
    target = args.date or days[-1]["date"]
    print_day(model, target)


def app_match(stats, query):
    q = query.strip().lower()
    hits = []
    for r in stats:
        app = r["a"]
        base = base_name(app)
        hay = " ".join([app, base, LABELS.get(base, ""), os.path.splitext(base)[0]]).lower()
        if q in hay:
            hits.append(r)
    return hits


def print_app_report(model, hits, gap_secs=300, max_hits=8):
    for r in hits[:max_hits]:
        app = r["a"]
        print()
        print("◆ %s  (%s)" % (label_for(model, app), base_name(app)))
        print("   路径    %s" % app)
        print("   总时长  %s · %d 段 · 出现 %d 天" % (dur_local(r["total"]), r["n"], r["days"]))
        print("   首次    %s %s" % (r["first"][0], hms_local(r["first"][1])))
        print("   最近    %s %s" % (r["last"][0], hms_local(r["last"][1])))
        for d in model["days"]:
            ss = [s for s in d["sessions"] if s["a"] == app]
            if not ss:
                continue
            spans = []
            for s in ss:
                if spans and s["t"] - spans[-1]["e"] <= gap_secs:
                    spans[-1]["e"] = max(spans[-1]["e"], s["t"] + s["d"])
                    spans[-1]["n"] += 1
                else:
                    spans.append({"s": s["t"], "e": s["t"] + s["d"], "n": 1})
            pieces = []
            for i, x in enumerate(spans):
                if i == 8:
                    pieces.append("…还有 %d 段" % (len(spans) - 8))
                    break
                pieces.append("%s–%s" % (hms_local(x["s"])[:5], hms_local(x["e"])[:5]))
            day_total = sum(s["d"] for s in ss)
            print("   %s  共 %s · %s" % (d["date"], dur_local(day_total), "  ".join(pieces)))
    if len(hits) > max_hits:
        print()
        print("（还有 %d 个匹配的应用没列出，查得更精确一些吧）" % (len(hits) - max_hits))


def event_range(args, today=None):
    """U 盘事件单独取范围：默认覆盖整个保留期，而不是只到有应用数据的那天。"""
    t = today or date.today()
    if getattr(args, "days", None) or getattr(args, "from_day", None) or getattr(args, "to_day", None):
        return range_from_args(args)
    days = getattr(args, "retention", RETENTION_DAYS) or RETENTION_DAYS
    return t - timedelta(days=days - 1), t


def cmd_events(args):
    auto_prune(args)
    ef, et = event_range(args)
    events = load_usb_events(ef, et)
    files = load_recent_usb_files()
    print()
    print("U 盘接入记录 · %s 至 %s" % (ef.isoformat(), et.isoformat()))
    print("-" * 66)
    if not events:
        print(" 这段时间没有查到 U 盘接入记录。")
    else:
        for e in events:
            extra = ("   盘符 " + e["letter"] + ":") if e["letter"] else ""
            print("  %s %s   USB VID_%s&PID_%s%s" % (e["date"], hms_local(e["t"])[:5],
                                                     e["vid"], e["pid"], extra))
    print()
    print("U 盘上打开过的文件（来自“最近使用”）")
    print("-" * 66)
    if not files:
        print(" 没有查到。")
    else:
        for f in files[:40]:
            print("  %s %s   [%s:]  %s" % (f["date"], hms_local(f["t"])[:5], f["drive"], f["path"]))
        if len(files) > 40:
            print("  …还有 %d 条" % (len(files) - 40))
    print()
    print("下载到 U 盘的文件（来自浏览器下载记录）")
    print("-" * 66)
    dl = load_usb_downloads()
    if not dl:
        print(" 没有查到。")
    else:
        for d in dl[:40]:
            size = human_size(d["size"])
            print("  %s %s   [%s:]  %s%s" % (d["date"], hms_local(d["t"])[:5], d["drive"],
                                             d["path"], ("  " + size) if size else ""))
        if len(dl) > 40:
            print("  …还有 %d 条" % (len(dl) - 40))
    print()


def cmd_app(args):
    query = args.name
    auto_prune(args)
    if not query:
        print("输入应用名或进程名的一部分（如 pallas、微信、WeGame），直接回车退出。")
        try:
            query = input("应用名：").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not query:
            return
    f, t = resolve_range(args)
    records = export_records(f, t)
    days = build_days(records)
    if not days:
        sys.exit("这段时间没有任何记录。")
    model = build_model(days)
    hits = app_match(model["appStats"], query)
    if not hits:
        names = "、".join(label_for(model, r["a"]) for r in model["appStats"][:15])
        sys.exit("没有找到包含“%s”的应用。\n有记录的应用包括：%s" % (query, names))
    print()
    print("=" * 72)
    print(" “%s” 匹配到 %d 个应用 · 数据范围 %s 至 %s" % (query, len(hits), days[0]["date"], days[-1]["date"]))
    print("=" * 72)
    print_app_report(model, hits, args.gap)
    print()


def cmd_at(args):
    auto_prune(args)
    today = date.today().isoformat()
    text = args.time
    if not text:
        print("输入想看的时间（如 14:30 或 2026-10-26 14:30），直接回车退出。")
        try:
            text = input("时间：").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not text:
            return
    parsed = parse_time_text(text, today)
    if parsed is None:
        sys.exit("看不懂“%s”，示例：14:30 、2026-10-26 14:30" % text)
    day_str, sec = parsed

    f = datetime.strptime(day_str, "%Y-%m-%d").date()
    records = export_records(f, f)
    days = build_days(records)
    if not days:
        sys.exit("%s 没有记录（那天可能没开机）。双击 时间线.bat 可以看到哪几天有数据。" % day_str)
    model = build_model(days)
    if find_day(model, day_str) is None:
        sys.exit("%s 没有记录。可查的最近日期：%s" % (day_str, days[-1]["date"]))
    print_at(model, day_str, sec, args.context)


def build_parser():
    p = argparse.ArgumentParser(
        prog="TimeTrace",
        description="时迹 TimeTrace — RustTimeNoter 的时间线查看器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  TimeTrace.exe html\n"
            "  TimeTrace.exe at \"14:30\"\n"
            "  TimeTrace.exe app pallas\n"
            "  TimeTrace.exe day --date 2026-10-26\n"
            "  TimeTrace.exe prune --dry-run\n"
        ),
    )
    p.add_argument("--version", action="version",
                   version="%(prog)s " + VERSION + "  (RustTimeNoter 时间线查看器)")
    p.add_argument("--menu", action="store_true", help="打开交互菜单（不带参数时默认也会打开）")
    p.add_argument("--tray", action="store_true", help="在右下角显示托盘图标")
    p.add_argument("--pause", action="store_true", help="命令结束后等待回车（托盘调用时用）")
    sub = p.add_subparsers(dest="cmd")

    def add_range(sp, default_days=None):
        sp.add_argument("--days", type=int, default=default_days,
                        help="只看最近 N 天（默认：保留期内的全部日子）")
        sp.add_argument("--from", dest="from_day", help="起始日期 YYYY-MM-DD")
        sp.add_argument("--to", dest="to_day", help="截止日期 YYYY-MM-DD")
        sp.add_argument("--retention", type=int, default=RETENTION_DAYS,
                        help="日志保留天数（默认 30，超期分片会被自动删除）")
        sp.add_argument("--no-prune", action="store_true", help="这次不要清理过期日志")

    h = sub.add_parser("html", help="生成“打开即全部展开”的时间线并打开浏览器")
    add_range(h)
    h.add_argument("--out", help="输出 HTML 路径")
    h.add_argument("--no-open", action="store_true", help="只生成文件，不打开浏览器")
    h.add_argument("--no-icons", action="store_true", help="不提取应用图标，页面里只用色块")
    h.add_argument("--no-events", action="store_true", help="不读取 U 盘接入记录")
    h.set_defaults(func=cmd_html)

    d = sub.add_parser("day", help="控制台打印某天的时间流水")
    add_range(d, default_days=7)
    d.add_argument("--date", help="指定日期 YYYY-MM-DD（默认最近一天）")
    d.add_argument("--ask", action="store_true", help="先询问要哪一天")
    d.set_defaults(func=cmd_day)

    a = sub.add_parser("at", help="查询某一时刻在用什么")
    a.add_argument("time", nargs="?", help="如 14:30 或 2026-10-26 14:30；省略则交互输入")
    a.add_argument("--context", type=int, default=10, help="前后参照分钟数（默认 10）")
    a.add_argument("--retention", type=int, default=RETENTION_DAYS, help="日志保留天数（默认 30）")
    a.add_argument("--no-prune", action="store_true", help="这次不要清理过期日志")
    a.set_defaults(func=cmd_at)

    ap = sub.add_parser("app", help="查某个应用是什么时候被用过的")
    ap.add_argument("name", nargs="?", help="应用名或进程名的一部分，如 pallas、微信；省略则交互输入")
    add_range(ap)
    ap.add_argument("--gap", type=int, default=300, help="间隔多少秒内的使用合并成一个时段（默认 300）")
    ap.set_defaults(func=cmd_app)

    ev = sub.add_parser("events", help="列出 U 盘接入记录（Windows 设备日志）")
    add_range(ev)
    ev.set_defaults(func=cmd_events)

    st = sub.add_parser("status", help="显示记录器状态与数据位置")
    st.set_defaults(func=cmd_status)

    ins = sub.add_parser("install", help="安装时迹 + 改版 RustTimeNoter（复制到 LOCALAPPDATA）")
    ins.set_defaults(func=cmd_install)

    uni = sub.add_parser("uninstall", help="卸载时迹，恢复原版 tracker.exe")
    uni.set_defaults(func=cmd_uninstall)

    wt = sub.add_parser("watch", help="检查记录器是否被停止或删除")
    wt.add_argument("--loop", action="store_true", help="常驻，每分钟检查一次")
    wt.set_defaults(func=cmd_watch)

    pr = sub.add_parser("prune", help="删除超过保留期的日志分片（默认 30 天）")
    pr.add_argument("--days", type=int, default=RETENTION_DAYS, help="保留天数（默认 30）")
    pr.add_argument("--dry-run", action="store_true", help="只列出会删什么，不真的删除")
    pr.set_defaults(func=cmd_prune)

    return p


def setup_console():
    """双击 exe 时控制台默认是 GBK，切到 UTF-8 才不会乱码。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetConsoleOutputCP(65001)
        k32.SetConsoleCP(65001)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 一体化入口：确保记录器在跑 + 交互菜单
# --------------------------------------------------------------------------
def _run_tracker(arg_list, timeout=25):
    tracker = find_tracker()
    try:
        proc = subprocess.run([tracker] + arg_list, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return proc.stdout.decode(enc)
        except UnicodeDecodeError:
            continue
    return proc.stdout.decode("utf-8", "replace")


def autostart_installed():
    if os.name != "nt":
        return True
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run")
        try:
            count = winreg.QueryInfoKey(key)[1]
            for i in range(count):
                _name, value, _type = winreg.EnumValue(key, i)
                low = str(value).lower()
                if "tracker" in low or "rusttimenoter" in low:
                    return True
        finally:
            winreg.CloseKey(key)
    except OSError:
        return True
    return False


def ensure_daemon():
    """记录器没跑就拉起来；顺带把开机自启补上。返回 (状态文本, 是否刚装了自启)。"""
    tracker = find_tracker()
    text = _run_tracker(["status"])
    if "running" not in text.lower():
        try:
            flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                     | getattr(subprocess, "DETACHED_PROCESS", 0))
            subprocess.Popen([tracker, "run"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags)
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(1.2)
        text = _run_tracker(["status"])
    installed = False
    if not autostart_installed():
        _run_tracker(["install", "autostart"])
        installed = True
    return text, installed


def _fmt_span(text):
    """把 '1h 37m 15s' 转成 '1 小时 37 分'。"""
    m = re.match(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?\s*$", text.strip())
    if not m:
        return text.strip()
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    if h:
        return "%d 小时 %d 分" % (h, mi)
    if mi:
        return "%d 分" % mi
    return "%d 秒" % s


def status_summary(text):
    daemon = "运行中" if "running" in text.lower() else "未运行"
    today = ""
    for line in text.splitlines():
        if line.lower().startswith("today records:"):
            rest = " ".join(line.split(":", 1)[1].split())
            m = re.match(r"(\d+)\s+total:\s*(.+)$", rest)
            if m:
                today = "%s 条 · %s" % (m.group(1), _fmt_span(m.group(2)))
            else:
                today = rest
    return daemon, today


def _ns(**kw):
    base = dict(days=None, from_day=None, to_day=None, retention=RETENTION_DAYS,
                no_prune=False, no_open=False, no_icons=False, no_events=False,
                out=None, context=10, gap=300, date=None, time=None, name=None,
                dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


def tray_cfg_path():
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "TimeTrace", "tray.cfg")


def tray_enabled():
    try:
        with open(tray_cfg_path(), encoding="utf-8") as fp:
            return fp.read().strip().lower() != "off"
    except OSError:
        return True


def set_tray_enabled(enabled):
    try:
        os.makedirs(os.path.dirname(tray_cfg_path()), exist_ok=True)
        with open(tray_cfg_path(), "w", encoding="utf-8") as fp:
            fp.write("on" if enabled else "off")
        return True
    except OSError:
        return False


def choose_tray():
    """安装时问一次：托盘里显不显示图标。回车 = 保持当前设置。"""
    cur = tray_enabled()
    cur_txt = "显示" if cur else "不显示"
    print()
    print("  右下角托盘图标（RustTimeNoter 那个）要显示吗？")
    print("    1) 显示 —— 右键就能打开时间线、停止记录")
    print("    2) 不显示 —— 托盘里完全看不到它")
    print("       代价：以后要停止记录、看报表，只能在「时迹」这个程序里操作")
    print("       适合：希望它一直安静跑着、不想让人在托盘里发现的人")
    try:
        answer = input("  选择 [1/2，回车=保持当前「%s」]：" % cur_txt).strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer == "2":
        set_tray_enabled(False)
        print("  已选择：不显示托盘图标")
    elif answer == "1":
        set_tray_enabled(True)
        print("  已选择：显示托盘图标")
    else:
        set_tray_enabled(cur)
        print("  保持当前设置：%s托盘图标" % cur_txt)


def restart_tracker():
    try:
        exe = find_tracker()
    except SystemExit:
        return False
    try:
        subprocess.run([exe, "stop"], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass
    time.sleep(2)
    try:
        subprocess.Popen([exe], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                        | getattr(subprocess, "DETACHED_PROCESS", 0)))
    except (OSError, subprocess.SubprocessError):
        return False
    time.sleep(1)
    return True


def recorder_running():
    return "running" in _run_tracker(["status"]).lower()


def create_desktop_shortcut():
    if os.name != "nt":
        return False
    exe = os.path.join(os.environ.get("LOCALAPPDATA", ""), "TimeTrace", "TimeTrace.exe")
    if not os.path.isfile(exe):
        return False
    script = (
        "$desk=[Environment]::GetFolderPath('Desktop');"
        "$p=Join-Path $desk '时迹.lnk';"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p);"
        "$s.TargetPath='%s';$s.WorkingDirectory='%s';$s.IconLocation='%s';$s.Save()"
        % (exe, os.path.dirname(exe), exe)
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
             base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def interactive_menu():
    tracker = None
    try:
        tracker = find_tracker()
    except SystemExit:
        tracker = None

    if tracker is None:
        print()
        print("=" * 58)
        print(" 时迹 TimeTrace %s" % VERSION)
        print("=" * 58)
        print(" 这台电脑还没装 RustTimeNoter 记录器。")
        print(" 时迹可以帮你把「记录器 + 时间线」一起装好（不需要管理员权限）。")
        print()
        try:
            answer = input(" 现在安装？[Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if answer in ("", "y", "yes"):
            try:
                cmd_install(_ns())
            except SystemExit as exc:
                print(exc)
            try:
                input("\n按回车退出…")
            except (EOFError, KeyboardInterrupt):
                print()
        return

    text, installed = ensure_daemon()
    daemon, today = status_summary(text)
    if installed:
        print("已为 RustTimeNoter 开启开机自启（之后开机就自动记录）。")

    while True:
        text = _run_tracker(["status"])
        daemon, today = status_summary(text)
        print()
        print("=" * 58)
        print(" 时迹 TimeTrace %s" % VERSION)
        print(" 记录器：RustTimeNoter（%s）%s" % (daemon, ("   今天 " + today) if today else ""))
        print("=" * 58)
        print(" 1) 打开时间线")
        print(" 2) 查某个时刻在用什么")
        print(" 3) 查某个软件什么时候被用过")
        print(" 4) 查 U 盘")
        print(" 5) 查看某一天的使用记录")
        print(" 6) 记录器状态与数据位置")
        print(" 7) 安装 / 修复（重新部署程序）")
        print(" 8) 卸载（恢复原版 tracker.exe）")
        print(" 9) 托盘图标：当前%s（点一下切换）" % ("显示" if tray_enabled() else "不显示"))
        print(" 10) %s记录器" % ("停止" if recorder_running() else "启动"))
        print(" 0) 退出")
        try:
            choice = input("\n请选择 [0-10]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if choice == "0":
            return
        try:
            if choice == "1":
                cmd_html(_ns())
            elif choice == "2":
                cmd_at(_ns())
            elif choice == "3":
                cmd_app(_ns())
            elif choice == "4":
                cmd_events(_ns())
            elif choice == "5":
                day = input("日期（YYYY-MM-DD，直接回车 = 最近一天）：").strip()
                cmd_day(_ns(date=day or None))
            elif choice == "6":
                print()
                print(_run_tracker(["status"]).strip())
                print("数据目录：%s" % os.path.join(os.environ.get("LOCALAPPDATA", ""), "RustTimeNoter"))
                print("图标缓存：%s" % ICON_CACHE_DIR)
            elif choice == "7":
                cmd_install(_ns())
            elif choice == "8":
                cmd_uninstall(_ns())
                return
            elif choice == "9":
                now_on = not tray_enabled()
                set_tray_enabled(now_on)
                restart_tracker()
                print("托盘图标已设为：%s（记录器已重启）" % ("显示" if now_on else "不显示"))
            elif choice == "10":
                try:
                    exe = find_tracker()
                except SystemExit as exc:
                    print(exc)
                else:
                    if recorder_running():
                        append_lifecycle("stopped", "在时迹里手动停止")
                        subprocess.run([exe, "stop"], capture_output=True, timeout=15)
                        print("已停止记录。要恢复：再选一次 10。")
                    else:
                        subprocess.Popen([exe], stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                                        | getattr(subprocess, "DETACHED_PROCESS", 0)))
                        print("已启动记录。")
                    time.sleep(1)
            else:
                continue
        except SystemExit as exc:
            if exc.code not in (None, 0):
                print(exc)
        except (EOFError, KeyboardInterrupt):
            print()
            return
        except Exception as exc:  # noqa: BLE001
            print("出错了：%s" % exc)

        try:
            input("\n按回车返回菜单…")
        except (EOFError, KeyboardInterrupt):
            print()
            return


def hide_console():
    if os.name != "nt":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def single_instance(name="TimeTraceTray_SingleInstance"):
    """同一会话里的单实例。用会话内名字，普通用户也能创建（用 Global 前缀需要特权）。"""
    if os.name != "nt":
        return object()
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        handle = k32.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
        if not handle:
            return object()
        if err == 183:  # ERROR_ALREADY_EXISTS
            return None
        return handle
    except Exception:
        return object()


def promote_tray_icon():
    """把托盘图标标记为“始终显示”，否则新图标会被 Windows 收进 ^ 里。"""
    if os.name != "nt":
        return
    try:
        import winreg
        target = os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__).lower()
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Control Panel\NotifyIconSettings", 0,
                             winreg.KEY_READ | winreg.KEY_SET_VALUE)
    except OSError:
        return
    try:
        count = winreg.QueryInfoKey(key)[1]
        for i in range(count):
            sub = winreg.EnumKey(key, i)
            try:
                with winreg.OpenKey(key, sub, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as sk:
                    try:
                        path, _ = winreg.QueryValueEx(sk, "ExecutablePath")
                    except OSError:
                        continue
                    if os.path.abspath(str(path)).lower() == target:
                        winreg.SetValueEx(sk, "IsPromoted", 0, winreg.REG_DWORD, 1)
            except OSError:
                continue
    finally:
        winreg.CloseKey(key)


def tray_first_run_hint():
    """第一次启动时提示一下图标藏在 ^ 里。"""
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "TimeTrace")
    flag = os.path.join(base, "tray-hint.flag")
    if os.path.isfile(flag):
        return
    try:
        os.makedirs(base, exist_ok=True)
        with open(flag, "w", encoding="utf-8") as fp:
            fp.write("1")
    except OSError:
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            "时迹已经在右下角运行。\n\n"
            "如果没看到图标：点任务栏时钟旁边的「^」，\n"
            "找到时迹图标，把它拖到任务栏上固定即可。",
            "时迹 TimeTrace", 0x40)
    except Exception:
        pass


def _tray_post_start():
    time.sleep(1.5)
    promote_tray_icon()
    tray_first_run_hint()


def run_tracker_action(arg_list):
    """直接调用 tracker 自己的命令（原版报表 / 停止等）。"""
    try:
        tracker = find_tracker()
    except SystemExit:
        return
    try:
        subprocess.Popen([tracker] + arg_list, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass


def tray_base_command():
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def spawn_engine(arg_list, console=True):
    """以子进程运行自己（菜单里的各项操作）。console=True 时弹一个控制台窗口。"""
    cmd = tray_base_command() + list(arg_list)
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if console else getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(cmd, creationflags=flags, close_fds=True)
    except (OSError, subprocess.SubprocessError):
        pass


def _run_key():
    if os.name != "nt":
        return None
    try:
        import winreg
        return winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                              r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                              winreg.KEY_READ | winreg.KEY_SET_VALUE)
    except OSError:
        return None


def tray_autostart_on():
    key = _run_key()
    if key is None:
        return False
    try:
        import winreg
        try:
            value, _type = winreg.QueryValueEx(key, TRAY_RUN_VALUE)
        except OSError:
            return False
        return "--tray" in str(value)
    finally:
        winreg.CloseKey(key)


def set_tray_autostart(enabled):
    key = _run_key()
    if key is None:
        return False
    try:
        import winreg
        if enabled:
            target = subprocess.list2cmdline(tray_base_command() + ["--tray"])
            winreg.SetValueEx(key, TRAY_RUN_VALUE, 0, winreg.REG_SZ, target)
        else:
            try:
                winreg.DeleteValue(key, TRAY_RUN_VALUE)
            except OSError:
                pass
        return True
    except OSError:
        return False
    finally:
        winreg.CloseKey(key)


def tracker_icon_png():
    """取 RustTimeNoter 自己的图标（缓存成 PNG），让托盘图标跟它长得一样。"""
    cache_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "TimeTrace")
    cache = os.path.join(cache_dir, "tracker-icon.png")
    if os.path.isfile(cache) and os.path.getsize(cache) > 0:
        return cache
    try:
        tracker = find_tracker()
        os.makedirs(cache_dir, exist_ok=True)
        script = (
            "Add-Type -AssemblyName System.Drawing\n"
            "$ico = [System.Drawing.Icon]::ExtractAssociatedIcon('%s')\n"
            "$ico.ToBitmap().Save('%s', [System.Drawing.Imaging.ImageFormat]::Png)\n"
            % (tracker.replace("'", "''"), cache.replace("'", "''"))
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
             base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, SystemExit):
        return None
    return cache if os.path.isfile(cache) and os.path.getsize(cache) > 0 else None


def make_tray_image():
    png = tracker_icon_png()
    if png:
        try:
            from PIL import Image
            return Image.open(png).convert("RGBA")
        except Exception:
            pass
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([3, 3, size - 4, size - 4], radius=15, fill=(47, 109, 237, 255))
    d.ellipse([15, 15, size - 16, size - 16], outline=(255, 255, 255, 255), width=4)
    d.line([32, 32, 32, 20], fill=(255, 255, 255, 255), width=4)
    d.line([32, 32, 42, 39], fill=(255, 255, 255, 255), width=4)
    return img


def run_tray():
    try:
        from pystray import Icon, Menu, MenuItem
    except ImportError:
        sys.exit("托盘模式需要 pystray 和 Pillow：pip install pystray pillow（或用打包好的 TimeTrace.exe）")

    guard = single_instance()
    if guard is None:
        return

    hide_console()

    # 记录器没跑就拉起来
    try:
        ensure_daemon()
    except SystemExit:
        pass

    state = {"autostart": tray_autostart_on()}

    def do_timeline(icon=None, item=None):
        spawn_engine(["html"], console=False)

    def do_at(icon=None, item=None):
        spawn_engine(["--pause", "at"], console=True)

    def do_app(icon=None, item=None):
        spawn_engine(["--pause", "app"], console=True)

    def do_events(icon=None, item=None):
        spawn_engine(["--pause", "events"], console=True)

    def do_day(icon=None, item=None):
        spawn_engine(["--pause", "day", "--ask"], console=True)

    def do_status(icon=None, item=None):
        spawn_engine(["--pause", "status"], console=True)

    def do_tracker_view(icon=None, item=None):
        run_tracker_action(["view"])

    def do_data_dir(icon=None, item=None):
        try:
            os.startfile(os.path.join(os.environ.get("LOCALAPPDATA", ""), "RustTimeNoter"))
        except (OSError, AttributeError):
            pass

    def do_stop(icon=None, item=None):
        try:
            import ctypes
            answer = ctypes.windll.user32.MessageBoxW(
                None,
                "停止后不再记录使用情况，直到下次启动。\n\n确定要停止记录器吗？",
                "RustTimeNoter", 0x04 | 0x20)  # MB_YESNO | MB_ICONQUESTION
            if answer == 6:  # IDYES
                run_tracker_action(["stop"])
        except Exception:
            pass

    def toggle_autostart(icon=None, item=None):
        state["autostart"] = not state["autostart"]
        if not set_tray_autostart(state["autostart"]):
            state["autostart"] = tray_autostart_on()
        try:
            icon.update_menu()
        except Exception:
            pass

    def do_quit(icon=None, item=None):
        icon.stop()

    menu = Menu(
        MenuItem("打开时间线（所有日子、图标、U 盘）", do_timeline, default=True),
        MenuItem("查某个时刻在用什么", do_at),
        MenuItem("查某个软件什么时候被用过", do_app),
        MenuItem("查 U 盘：接入 / 打开 / 下载", do_events),
        MenuItem("某个日期的文字流水", do_day),
        MenuItem("记录器状态与数据位置", do_status),
        Menu.SEPARATOR,
        MenuItem("RustTimeNoter 原版报表", do_tracker_view),
        MenuItem("打开数据目录", do_data_dir),
        MenuItem("停止记录器", do_stop),
        Menu.SEPARATOR,
        MenuItem("开机自动启动", toggle_autostart, checked=lambda item: state["autostart"]),
        MenuItem("退出", do_quit),
    )
    icon = Icon("TimeTrace", make_tray_image(), "RustTimeNoter · 时迹", menu)
    import threading
    threading.Thread(target=_tray_post_start, daemon=True).start()
    icon.run()


def cmd_status(args):
    text = _run_tracker(["status"]).strip()
    print(text or "记录器没有响应。")
    print()
    print("数据目录：%s" % os.path.join(os.environ.get("LOCALAPPDATA", ""), "RustTimeNoter"))
    print("图标缓存：%s" % ICON_CACHE_DIR)
    print("托盘开机自启：%s" % ("已开启" if tray_autostart_on() else "未开启"))


def _local_dirs():
    base = os.environ.get("LOCALAPPDATA", "")
    rtn = os.path.join(base, "RustTimeNoter")
    tt = os.path.join(base, "TimeTrace")
    return rtn, os.path.join(rtn, "bin"), tt


def bundled_tracker():
    """找打包进 exe 的改版 tracker.exe，其次找 exe 旁边的。"""
    cands = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        cands.append(os.path.join(sys._MEIPASS, "RustTimeNoter", "tracker.exe"))
    here = app_dir()
    cands.append(os.path.join(here, "RustTimeNoter", "tracker.exe"))
    cands.append(os.path.join(here, "vendor", "RustTimeNoter", "tracker.exe"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def cmd_install(args):
    """把时迹 + 改过的 RustTimeNoter 装到 %LOCALAPPDATA%，并设好开机自启。"""
    src_tracker = bundled_tracker()
    if not src_tracker:
        sys.exit("安装包里缺少 RustTimeNoter\\tracker.exe，请完整解压后再运行。")

    rtn, bin_dir, tt_dir = _local_dirs()
    os.makedirs(bin_dir, exist_ok=True)
    os.makedirs(tt_dir, exist_ok=True)

    dst_tracker = os.path.join(bin_dir, "tracker.exe")
    print("[1/5] 停止正在运行的记录器 …")
    if os.path.isfile(dst_tracker):
        try:
            subprocess.run([dst_tracker, "stop"], capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with open(dst_tracker, "rb+"):
                    break
            except OSError:
                time.sleep(0.5)

    print("[2/5] 复制程序到 LOCALAPPDATA …")
    # 常驻检查进程正占用着已安装的 exe，先停掉再覆盖
    try:
        stop_watch_loop()
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.5)
    if getattr(sys, "frozen", False):
        me = os.path.abspath(sys.executable)
        target = os.path.join(tt_dir, "TimeTrace.exe")
        if os.path.abspath(me).lower() != target.lower():
            last = None
            for _ in range(20):
                try:
                    shutil.copy2(me, target)
                    last = None
                    break
                except OSError as exc:
                    last = exc
                    time.sleep(0.5)
            if last is not None:
                sys.exit("复制 TimeTrace.exe 失败（文件被占用）：%s" % last)
    else:
        target = os.path.abspath(__file__)
    with open(os.path.join(tt_dir, "launcher.txt"), "w", encoding="utf-8") as fp:
        fp.write(target)

    bak = os.path.join(bin_dir, "tracker.exe.orig")
    if os.path.isfile(dst_tracker) and not os.path.isfile(bak):
        try:
            shutil.copy2(dst_tracker, bak)
            print("      原版 tracker.exe 已备份为 tracker.exe.orig")
        except OSError:
            pass

    # 记录器退出需要一点时间；被占用时等一会儿再复制
    last_err = None
    for _ in range(20):
        try:
            shutil.copy2(src_tracker, dst_tracker)
            last_err = None
            break
        except OSError as exc:
            last_err = exc
            time.sleep(0.5)
    if last_err is not None:
        sys.exit("复制 tracker.exe 失败（文件被占用）：%s\n请先关掉记录器再试。" % last_err)

    print("[3/6] 初始化配置 …")
    try:
        subprocess.run([dst_tracker, "config", "init"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass

    print("[4/6] 托盘图标 …")
    choose_tray()

    print("[5/6] 设置开机自启 …")
    try:
        subprocess.run([dst_tracker, "install", "autostart"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass

    print("[6/6] 启动记录器 …")
    try:
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "DETACHED_PROCESS", 0))
        subprocess.Popen([dst_tracker, "run"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        pass
    time.sleep(1.5)

    append_lifecycle("installed", "安装 / 更新完成")
    install_watch_task()
    create_desktop_shortcut()
    try:
        cmd_watch(_ns())
    except Exception:
        pass

    print()
    if tray_enabled():
        print("安装完成。右下角会出现 RustTimeNoter 图标，右键就是菜单：")
        print("  打开时间线 / 查某个时刻 / 查某个软件 / 查 U 盘 / 查看某一天的使用记录 / 记录器状态")
    else:
        print("安装完成。托盘里不会显示图标（按你的选择）。")
        print("  要看报表或停止记录：双击桌面上的「时迹」")
    print()
    print("程序目录：%s" % tt_dir)
    print("数据目录：%s" % rtn)
    print("再次双击这个程序可以安装 / 修复；在菜单里选 8 可以卸载（数据会保留）。")


def cmd_uninstall(args):
    rtn, bin_dir, tt_dir = _local_dirs()
    dst_tracker = os.path.join(bin_dir, "tracker.exe")
    print("[1/3] 停止记录器并移除开机自启 …")
    append_lifecycle("uninstalled", "卸载时迹")
    remove_watch_task()
    if os.path.isfile(dst_tracker):
        for cmd in (["stop"], ["uninstall", "autostart"]):
            try:
                subprocess.run([dst_tracker] + cmd, capture_output=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                pass
    print("[2/3] 恢复原版 tracker.exe（如果有备份）…")
    bak = os.path.join(bin_dir, "tracker.exe.orig")
    if os.path.isfile(bak):
        shutil.copy2(bak, dst_tracker)
    print("[3/3] 删除时迹程序 …")
    shutil.rmtree(tt_dir, ignore_errors=True)
    print()
    print("已卸载。你的使用数据仍然保留在：%s" % rtn)
    print("要彻底删除，手动删掉这个目录即可。")


def boot_id():
    """本机这次开机的时刻（unix 秒）；用来识别「重启了」。"""
    try:
        import ctypes
        ms = ctypes.windll.kernel32.GetTickCount64()
        return int(time.time() - ms / 1000)
    except Exception:
        return 0


def last_record_end(today):
    """今天最后一条记录的结束时刻；用来把「停止」精确到秒。"""
    try:
        out = os.path.join(tempfile.gettempdir(), "tt_watch_export.json")
        tracker = find_tracker()
        subprocess.run([tracker, "export", "--format", "json", "--out", out],
                       capture_output=True, timeout=25,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with open(out, encoding="utf-8") as fp:
            recs = json.load(fp)
        best = None
        for r in recs:
            if r.get("date") != today:
                continue
            try:
                start = datetime.strptime(r.get("start_time", ""), "%H:%M:%S")
            except ValueError:
                continue
            end = start + timedelta(seconds=int(r.get("duration_secs") or 0))
            if best is None or end > best:
                best = end
        if best is None:
            return None
        return datetime.combine(date.fromisoformat(today), best.time())
    except (OSError, ValueError, subprocess.SubprocessError, SystemExit):
        return None


def watch_once():
    """检查一次：记录器「开机 / 启动 / 停止 / 被删除」这类事件写到 lifecycle.log。"""
    tracker = None
    try:
        tracker = find_tracker()
    except SystemExit:
        tracker = None
    present = tracker is not None
    running = False
    if present:
        text = _run_tracker(["status"])
        running = "running" in text.lower()

    state_path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "TimeTrace", "watch.state")
    prev = {}
    try:
        with open(state_path, encoding="utf-8") as fp:
            prev = json.load(fp)
    except (OSError, ValueError):
        prev = {}

    now = datetime.now()
    boot = boot_id()
    events = []
    if prev:
        if prev.get("present") and not present:
            events.append(("missing", "记录器文件不见了（被删除或移走）", None))
        if not prev.get("present") and present:
            events.append(("restored", "记录器文件回来了", None))
        if prev.get("running") and not running:
            ts = now
            end = last_record_end(now.strftime("%Y-%m-%d"))
            if end is not None and timedelta(0) <= (now - end) <= timedelta(minutes=3):
                ts = end
            events.append(("stopped", "记录器停止运行（手动退出或被结束）", ts))
        if not prev.get("running") and running:
            events.append(("started", "记录器开始运行", None))
        # 重启过（即使之前也在运行），补一条「开机启动」
        if boot and prev.get("boot") and prev.get("boot") != boot and running:
            events.append(("started", "开机自启", None))
    for kind, note, ts in events:
        append_lifecycle(kind, note, ts)

    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as fp:
            json.dump({"present": present, "running": running, "boot": boot}, fp)
    except OSError:
        pass


def watch_pid_file():
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "TimeTrace", "watch.pid")


def _k32():
    import ctypes
    from ctypes import wintypes
    if not hasattr(_k32, "_dll"):
        dll = ctypes.WinDLL("kernel32", use_last_error=True)
        dll.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        dll.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        dll.OpenProcess.restype = wintypes.HANDLE
        dll.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        dll.WaitForSingleObject.restype = wintypes.DWORD
        dll.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        dll.CloseHandle.argtypes = [wintypes.HANDLE]
        _k32._dll = dll
    return _k32._dll


def find_tracker_pid():
    """枚举进程找 tracker.exe（只读快照，不启动任何程序）。"""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    k32 = _k32()
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return 0
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not k32.Process32FirstW(snap, ctypes.byref(entry)):
            return 0
        while True:
            if entry.szExeFile.lower() == "tracker.exe":
                return int(entry.th32ProcessID)
            if not k32.Process32NextW(snap, ctypes.byref(entry)):
                return 0
    finally:
        k32.CloseHandle(snap)


def watch_loop():
    """常驻：等 tracker 进程句柄退出（内核事件），不轮询、不启动子进程。"""
    hide_console()
    if single_instance("TimeTraceWatch_Loop") is None:
        return
    try:
        os.makedirs(os.path.dirname(watch_pid_file()), exist_ok=True)
        with open(watch_pid_file(), "w", encoding="ascii") as fp:
            fp.write(str(os.getpid()))
    except OSError:
        pass

    k32 = _k32()
    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 258
    boot = boot_id()
    state_path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "TimeTrace", "watch.state")
    was_running = None
    handle = None
    pid = 0

    # 启动时先补一条「开机自启」（重启后即使一直没停过也要有标记）
    try:
        prev = {}
        try:
            with open(state_path, encoding="utf-8") as fp:
                prev = json.load(fp)
        except (OSError, ValueError):
            prev = {}
        if prev.get("running") is not None and prev.get("boot") != boot:
            append_lifecycle("started", "开机自启")
            was_running = None
        _write_state(state_path, True, True, boot)
    except Exception:  # noqa: BLE001
        pass

    while True:
        try:
            cur = find_tracker_pid()
            tracker = None
            try:
                tracker = find_tracker()
            except SystemExit:
                tracker = None
            present = tracker is not None

            if cur:
                if handle is None or cur != pid:
                    if handle:
                        k32.CloseHandle(handle)
                    handle = k32.OpenProcess(SYNCHRONIZE, False, cur)
                    pid = cur
                    if was_running is False:
                        append_lifecycle("started", "记录器开始运行")
                    was_running = True
                    _write_state(state_path, present, True, boot)
                if handle:
                    rc = k32.WaitForSingleObject(handle, 20000)
                    if rc == WAIT_OBJECT_0:
                        append_lifecycle("stopped", "记录器停止运行（手动退出或被结束）")
                        k32.CloseHandle(handle)
                        handle = None
                        pid = 0
                        was_running = False
                        _write_state(state_path, present, False, boot)
                        if not present:
                            append_lifecycle("missing", "记录器文件不见了（被删除或移走）")
                    # 超时就继续等，不做任何额外工作
                else:
                    time.sleep(5)
            else:
                if was_running is not False:
                    append_lifecycle("stopped", "记录器停止运行（手动退出或被结束）")
                    was_running = False
                    _write_state(state_path, present, False, boot)
                    if not present:
                        append_lifecycle("missing", "记录器文件不见了（被删除或移走）")
                time.sleep(5)
                if present:
                    # 文件回来了但进程没起：等用户/自启再拉起
                    _write_state(state_path, present, False, boot)
        except Exception:  # noqa: BLE001
            time.sleep(5)


def _write_state(path, present, running, boot):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"present": present, "running": running, "boot": boot}, fp)
    except OSError:
        pass


def cmd_watch(args):
    if getattr(args, "loop", False):
        watch_loop()
        return
    watch_once()


def watch_launch_command():
    installed = os.path.join(os.environ.get("LOCALAPPDATA", ""), "TimeTrace", "TimeTrace.exe")
    if os.path.isfile(installed):
        return '"%s" watch --loop' % installed
    if getattr(sys, "frozen", False):
        return '"%s" watch --loop' % os.path.abspath(sys.executable)
    return '"%s" "%s" watch --loop' % (sys.executable, os.path.abspath(__file__))


def start_watch_loop():
    cmd = watch_launch_command()
    try:
        subprocess.Popen(cmd, shell=False, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                        | getattr(subprocess, "DETACHED_PROCESS", 0)))
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def stop_watch_loop():
    try:
        with open(watch_pid_file(), encoding="ascii") as fp:
            pid = int(fp.read().strip())
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=15,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        os.remove(watch_pid_file())
    except OSError:
        pass


def install_watch_task():
    """登录自启一个常驻的 watch --loop（不用计划任务，避免每分钟启动 exe）。"""
    if os.name != "nt":
        return False
    stop_watch_loop()
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                             winreg.KEY_SET_VALUE)
        try:
            winreg.SetValueEx(key, "TimeTraceWatch", 0, winreg.REG_SZ, watch_launch_command())
        finally:
            winreg.CloseKey(key)
    except OSError:
        return False
    start_watch_loop()
    return True


def remove_watch_task():
    if os.name != "nt":
        return
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                             winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, "TimeTraceWatch")
        except OSError:
            pass
        finally:
            winreg.CloseKey(key)
    except OSError:
        pass
    try:
        with open(watch_pid_file(), encoding="ascii") as fp:
            pid = int(fp.read().strip())
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=15,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        subprocess.run(["schtasks", "/delete", "/tn", "TimeTraceWatch", "/f"],
                       capture_output=True, timeout=15,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass


def main():
    setup_console()
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "tray", False):
        run_tray()
        return
    if getattr(args, "menu", False):
        interactive_menu()
        return
    if not getattr(args, "cmd", None):
        try:
            tty = sys.stdin.isatty()
        except (AttributeError, ValueError):
            tty = False
        if tty:
            interactive_menu()
        else:
            parser.print_help()
        return
    try:
        args.func(args)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            print(exc.code)
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except OSError:
            pass
    except KeyboardInterrupt:
        print()
    finally:
        if getattr(args, "pause", False):
            try:
                input("\n按回车关闭窗口…")
            except (EOFError, KeyboardInterrupt, OSError):
                pass


if __name__ == "__main__":
    main()











