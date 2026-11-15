#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 订阅自动生成流水线（纯标准库，无第三方依赖）

流程：
  1. 拉取多个自动维护的上游 m3u 清单
  2. 频道名归一化，合并同一频道的候选源
  3. 逐个实测：首包时延 + 吞吐量 + TS 的 PMT 解析（判定有没有画面、音轨是 AAC 还是 MP2/AC3）
  4. 只保留「有画面 + AAC 音轨」的源，按速度排序，每频道保留最快 N 个
  5. 全部源都挂掉的频道直接剔除
  6. 输出 output/iptv.m3u、output/iptv.txt、output/app_channels.json、output/report.json
"""

import json
import os
import re
import socket
import sys
import time
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
OUT = os.path.join(ROOT, "output")

UA = ("Mozilla/5.0 (Linux; Android 9; Box) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")

TIMEOUT = int(CFG.get("probe_timeout", 8))
MAXB = int(CFG.get("probe_bytes", 2_500_000))
MAXCAND = int(CFG.get("max_candidates_per_channel", 8))
KEEP = int(CFG.get("keep_best_per_channel", 3))
THREADS = int(CFG.get("threads", 48))
BUDGET = int(os.environ.get("PROBE_BUDGET", "1500"))  # 秒，防止 Actions 超时

socket.setdefaulttimeout(TIMEOUT)

# 探测直播流时绕开系统代理（代理会让测速失真，且国内流本来就不该走代理）
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# --------------------------------------------------------------------------
# 频道名归一化
# --------------------------------------------------------------------------
CCTV_ALIAS = {
    "中央电视综合": "CCTV-1", "中央电视台综合": "CCTV-1",
    "中央电视财经": "CCTV-2", "中央电视台财经": "CCTV-2",
}


def canon(name):
    """把上游各种叫法归一成标准频道名，返回 None 表示不要这个台。"""
    n = (name or "").strip()
    n = n.replace("　", " ").strip()
    if not n:
        return None

    # 去掉后缀修饰：CCTV-1 综合 / CCTV1高清 / 湖南卫视HD
    n = re.sub(r"[\s\-_·]*(高清|超清|标清|蓝光|HD|SD|FHD|4K|8K)$", "", n, flags=re.I)
    n = n.strip()

    if n in CCTV_ALIAS:
        return CCTV_ALIAS[n]

    # 央视
    m = re.match(r"^(?:CCTV|央视|中央)[\s\-_]*(1[0-9]|[1-9])(\+)?", n, flags=re.I)
    if m:
        num = int(m.group(1))
        if 1 <= num <= 17:
            return "CCTV-%d%s" % (num, "+" if m.group(2) else "")
    if re.match(r"^CGTN", n, flags=re.I) or "中国环球电视" in n:
        return "CGTN"

    # 卫视
    m = re.match(r"^([\u4e00-\u9fa5]{2,4})卫视$", n)
    if m:
        return m.group(1) + "卫视"

    # 明显不要的：购物、彩票、导视、付费、测试、轮播
    if re.search(r"购物|彩票|导视|测试|付费|轮播|游戏|证券|点播|广告|预告", n):
        return None
    # 县级台常见的杂乱频道，公网源质量极差
    if re.search(r"县|区|镇|乡|街道", n):
        return None
    if len(n) > 8:
        return None
    return n


def group_of(name):
    if name.startswith("CCTV") or name == "CGTN":
        return CFG["groups"]["cctv"]
    if name.endswith("卫视"):
        return CFG["groups"]["satellite"]
    return CFG["groups"]["other"]


def cctv_sort_key(name):
    m = re.match(r"CCTV-(\d+)(\+)?$", name)
    if m:
        return (0, int(m.group(1)) + (0.5 if m.group(2) else 0), name)
    if name == "CGTN":
        return (0, 99, name)
    if name.endswith("卫视"):
        return (1, 0, name)
    return (2, 0, name)


# --------------------------------------------------------------------------
# 上游清单
# --------------------------------------------------------------------------
def http_get(url, limit, timeout=None, deadline=None, direct=False):
    """带整体时间上限的下载：deadline 是绝对时间戳，超时立刻返回已拿到的东西。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    opener = _DIRECT.open if direct else urllib.request.urlopen
    r = opener(req, timeout=timeout or TIMEOUT)
    buf = []
    got = 0
    while got < limit:
        if deadline and time.time() > deadline:
            break
        b = r.read(32768)
        if not b:
            break
        buf.append(b)
        got += len(b)
    return b"".join(buf)


def parse_m3u(text):
    """返回 [(name, url)]，兼容 #EXTINF 各种写法。"""
    out = []
    cur = None
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            m = re.search(r'tvg-name="([^"]*)"', line)
            if not m:
                m = re.search(r'tvg-id="([^"]*)"', line)
            if not m:
                m = re.search(r",([^,]*)$", line)
            cur = m.group(1).strip() if m else None
        elif line.startswith("#"):
            continue
        elif cur:
            out.append((cur, line))
            cur = None
    return out


def load_upstream():
    name2urls = OrderedDict()

    # 先注入「模板源」（按编号能拼出整套餐的 CDN），这样不会被每频道候选数上限挤掉
    for pat in CFG.get("pattern_sources", []):
        tpl = pat.get("template") or ""
        mp = pat.get("map") or {}
        if not mp:
            # 无 map 的「单源模板」：names 可为 {频道名:URL}（dict）或 [频道名]（list，template 里 {name} 被频道名替换）
            names = pat.get("names") or []
            if isinstance(names, dict):
                for nm, u in names.items():
                    if not u.startswith("http"):
                        continue
                    bucket = name2urls.setdefault(canon(nm), [])
                    if u not in bucket:
                        bucket.insert(0, u)
            else:
                for nm in names:
                    u = tpl.replace("{name}", nm)
                    if not u.startswith("http"):
                        continue
                    bucket = name2urls.setdefault(canon(nm), [])
                    if u not in bucket:
                        bucket.insert(0, u)
            continue
        for idx, nm in mp.items():
            try:
                u = tpl % int(idx)
            except Exception:
                continue
            bucket = name2urls.setdefault(nm, [])
            if u not in bucket:
                bucket.insert(0, u)
    if name2urls:
        print("[pattern] 注入模板源 %d 条，覆盖 %d 个频道"
              % (sum(len(v) for v in name2urls.values()), len(name2urls)), flush=True)
    for u in CFG["sources"]:
        t0 = time.time()
        try:
            data = http_get(u, 4_000_000, timeout=20)
            txt = data.decode("utf-8", "ignore")
            items = parse_m3u(txt)
            n_added = 0
            for raw_name, url in items:
                cn = canon(raw_name)
                if not cn:
                    continue
                bucket = name2urls.setdefault(cn, [])
                if url in bucket:
                    continue
                if len(bucket) >= MAXCAND:
                    continue
                if not url.lower().startswith(("http://", "https://", "rtmp://")):
                    continue
                bucket.append(url)
                n_added += 1
            print("[upstream] ok  %5.1fs %4d条 %4d新增  %s"
                  % (time.time() - t0, len(items), n_added, u), flush=True)
        except Exception as e:
            print("[upstream] ERR %5.1fs %s  %s" % (time.time() - t0, str(e)[:60], u),
                  flush=True)
    # 丢掉一个源都没有的
    name2urls = OrderedDict((k, v) for k, v in name2urls.items() if v)
    total = sum(len(v) for v in name2urls.values())
    print("[upstream] 频道 %d 个，候选源 %d 条" % (len(name2urls), total), flush=True)
    return name2urls


# --------------------------------------------------------------------------
# TS / PMT 解析：判断有没有画面、音轨编码
# --------------------------------------------------------------------------
VIDEO_TYPES = {0x01, 0x02, 0x1B, 0x24, 0x27, 0x10, 0x42}
AAC_TYPES = {0x0F, 0x11, 0x1C}
MPA_TYPES = {0x03, 0x04, 0x81, 0x87}   # MPEG-1/2 音频、AC3、EAC3、DTS（安卓默认解不了）


def _pid(pkt):
    return ((pkt[1] & 0x1F) << 8) | pkt[2]


def _payload(pkt):
    afc = (pkt[3] >> 4) & 0x03
    off = 4
    if afc in (2, 3):
        if off >= len(pkt):
            return None
        off += 1 + pkt[4]
    if off >= len(pkt):
        return None
    return pkt[off:]


def _section(pkt):
    """取 PSI 段（处理 pointer_field）"""
    pl = _payload(pkt)
    if not pl:
        return None
    if not (pkt[1] & 0x40):   # 没有 payload_unit_start_indicator 就是续包，跳过
        return None
    ptr = pl[0]
    sec = pl[1 + ptr:]
    if len(sec) < 8:
        return None
    return sec


def ts_info(data):
    """返回 (video:bool|None, audio:str|None)。None 表示数据不够判断。"""
    if len(data) < 188 * 4:
        return None, None
    start = data.find(b"\x47")
    if start < 0 or start > 376:
        return None, None
    data = data[start:]
    npkt = len(data) // 188
    if npkt < 4:
        return None, None

    pmt_pid = None
    pmt_types = None

    for i in range(npkt):
        pkt = data[i * 188:(i + 1) * 188]
        if len(pkt) < 188 or pkt[0] != 0x47:
            continue
        pid = _pid(pkt)
        if pid == 0 and pmt_pid is None:
            sec = _section(pkt)
            if not sec or sec[0] != 0x00:
                continue
            seclen = ((sec[1] & 0x0F) << 8) | sec[2]
            body = sec[8:3 + seclen - 4]
            for k in range(0, len(body) - 3, 4):
                prog = (body[k] << 8) | body[k + 1]
                ppid = ((body[k + 2] & 0x1F) << 8) | body[k + 3]
                if prog != 0:
                    pmt_pid = ppid
                    break
        elif pmt_pid is not None and pid == pmt_pid and pmt_types is None:
            sec = _section(pkt)
            if not sec or sec[0] != 0x02:
                continue
            seclen = ((sec[1] & 0x0F) << 8) | sec[2]
            pil = ((sec[10] & 0x0F) << 8) | sec[11]
            body = sec[12 + pil:3 + seclen - 4]
            types = []
            j = 0
            while j + 5 <= len(body):
                st = body[j]
                esl = ((body[j + 3] & 0x0F) << 8) | body[j + 4]
                types.append(st)
                j += 5 + esl
            pmt_types = types
            break

    if pmt_types is None:
        return None, None

    has_video = any(t in VIDEO_TYPES for t in pmt_types)
    if any(t in AAC_TYPES for t in pmt_types):
        audio = "aac"
    elif any(t in MPA_TYPES for t in pmt_types):
        audio = "mp2"
    elif pmt_types:
        audio = "other"
    else:
        audio = "none"
    return has_video, audio


# --------------------------------------------------------------------------
# 单源实测
# --------------------------------------------------------------------------
def resolve_hls(url, text, deadline, depth=0):
    """在 master/media playlist 里找到第一个可下载的分片，返回 seg_url。"""
    lines = [l.strip() for l in text.replace("\r", "").split("\n")]
    variants = [l for l in lines if l and not l.startswith("#")]
    if not variants or depth >= 3:
        return None
    base = url.rsplit("/", 1)[0] + "/"
    nxt = variants[0]
    if not nxt.lower().startswith("http"):
        nxt = base + nxt
    if nxt.lower().split("?")[0].endswith((".m3u8", ".m3u")):
        try:
            body = http_get(nxt, 200_000, deadline=deadline, direct=True).decode("utf-8", "ignore")
        except Exception:
            return None
        if "#EXTM3U" in body:
            return resolve_hls(nxt, body, deadline, depth + 1)
    return nxt


def probe(url):
    """实测单个源，返回 dict。所有 IO 共享一个整体 deadline，避免死链拖几十秒。"""
    t0 = time.time()
    deadline = t0 + TIMEOUT + 5
    try:
        first = http_get(url, 120_000, deadline=deadline, direct=True)
    except Exception as e:
        return {"ok": False, "err": type(e).__name__, "ttfb": 99.0}
    ttfb = time.time() - t0
    if not first:
        return {"ok": False, "err": "empty", "ttfb": ttfb}

    if b"#EXTM3U" in first[:3000]:
        try:
            seg_url = resolve_hls(url, first.decode("utf-8", "ignore"), deadline)
        except Exception as e:
            return {"ok": False, "err": "hls:" + type(e).__name__, "ttfb": ttfb}
        if not seg_url:
            return {"ok": False, "err": "no-segment", "ttfb": ttfb}
        try:
            t1 = time.time()
            seg = http_get(seg_url, MAXB, deadline=deadline, direct=True)
            dt = max(time.time() - t1, 0.05)
        except Exception as e:
            return {"ok": False, "err": "seg:" + type(e).__name__, "ttfb": ttfb}
        if not seg:
            return {"ok": False, "err": "seg-empty", "ttfb": ttfb}
        kbps = int(len(seg) * 8 / dt / 1000)
        size = len(seg)
        blob = seg
    else:
        kbps = int(len(first) * 8 / max(time.time() - t0, 0.05) / 1000)
        size = len(first)
        blob = first

    video, audio = ts_info(blob)

    # 分片太小 → 多半是黑屏/静态图/错误页
    if size < 30_000:
        return {"ok": False, "err": "tiny", "ttfb": round(ttfb, 2), "size": size}

    # HTML 错误页/过期页面，看着 200 其实没流
    head = first[:400].lstrip().lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html"):
        return {"ok": False, "err": "html", "ttfb": round(ttfb, 2), "size": size}

    # http-flv 直连流：能读到 FLV 头就说明是活流；音轨交给播放器自己解
    kind = "hls"
    if first[:3] == b"FLV":
        kind = "flv"
        if video is None:
            video = True
    elif first[:1] == b"\x47":
        kind = "ts"

    # 拿到的是 fMP4 之类没法解 PMT 的，标记为未知音轨，只作备用
    return {
        "ok": True,
        "ttfb": round(ttfb, 2),
        "kbps": kbps,
        "size": size,
        "kind": kind,
        "video": video,
        "audio": audio,
        "elapsed": round(time.time() - t0, 2),
    }


def grade(info):
    """质量分级：0 最好。1=音轨未知但有画面，2=无声/MP2，9=不可用"""
    if not info or not info.get("ok"):
        return 9
    if info.get("audio") == "aac" and info.get("video") is not False:
        return 0
    if info.get("audio") is None and info.get("video") is not False:
        return 1
    return 2


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main():
    os.makedirs(OUT, exist_ok=True)
    started = time.time()
    name2urls = load_upstream()

    jobs = []
    for name, urls in name2urls.items():
        for u in urls:
            jobs.append((name, u))
    print("[probe] 共 %d 条待测" % len(jobs), flush=True)

    results = {}
    done = 0
    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        futs = {ex.submit(probe, u): (n, u) for n, u in jobs}
        for f, (n, u) in list(futs.items()):
            if time.time() - started > BUDGET:
                print("[probe] 超出时间预算，提前收尾", flush=True)
                break
            try:
                results[u] = f.result()
            except Exception:
                results[u] = {"ok": False, "err": "exc", "ttfb": 99.0}
            done += 1
            if done % 100 == 0:
                alive = sum(1 for v in results.values() if v and v.get("ok"))
                print("[probe] %d/%d 存活 %d 用时 %.0fs"
                      % (done, len(jobs), alive, time.time() - started), flush=True)

    # 汇总
    wl = set(CFG.get("other_whitelist", []))
    other_policy = CFG.get("other_policy", "whitelist")
    channels = []          # [(name, [(url, info)...])]
    dropped_ch = []
    skipped_ch = []
    for name, urls in name2urls.items():
        if group_of(name) == CFG["groups"]["other"] and other_policy != "all":
            if other_policy == "none" or name not in wl:
                skipped_ch.append(name)
                continue
        cands = []
        for u in urls:
            info = results.get(u)
            g = grade(info)
            info = info or {}
            info["grade"] = g
            if g <= 1:      # 只留 AAC 或有画面但音轨未知的
                cands.append((u, info))
        cands.sort(key=lambda x: (x[1]["grade"], x[1]["ttfb"], -x[1].get("kbps", 0)))
        if not cands:
            dropped_ch.append(name)
            continue
        channels.append((name, cands[:KEEP]))

    channels.sort(key=lambda x: cctv_sort_key(x[0]))

    # ---- m3u ----
    m3u = ["#EXTM3U"]
    txt = ["# 频道名,地址  更新于 " + time.strftime("%Y-%m-%d %H:%M:%S")]
    for name, cands in channels:
        grp = group_of(name)
        for u, info in cands:
            m3u.append('#EXTINF:-1 tvg-name="%s" tvg-logo="" group-title="%s",%s'
                       % (name, grp, name))
            m3u.append(u)
            txt.append("%s,%s" % (name, u))

    with open(os.path.join(OUT, "iptv.m3u"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(m3u) + "\n")
    with open(os.path.join(OUT, "iptv.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(txt) + "\n")

    # ---- 给 App 用的 json（分组结构，和内置 assets 一致）----
    groups = OrderedDict()
    for name, cands in channels:
        groups.setdefault(group_of(name), []).append(
            {"name": name, "sources": [u for u, _ in cands]})
    app_json = {"groups": [{"name": g, "channels": cs} for g, cs in groups.items()]}
    with open(os.path.join(OUT, "app_channels.json"), "w", encoding="utf-8") as f:
        json.dump(app_json, f, ensure_ascii=False, separators=(",", ":"))

    # ---- 报告 ----
    report = {
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - started, 1),
        "channels": len(channels),
        "streams": sum(len(c) for _, c in channels),
        "probed": len(results),
        "alive": sum(1 for v in results.values() if v and v.get("ok")),
        "aac": sum(1 for v in results.values() if v and v.get("audio") == "aac"),
        "dropped_channels": dropped_ch,
        "skipped_other": len(skipped_ch),
        "detail": {n: [{"url": u, "ttfb": i.get("ttfb"), "kbps": i.get("kbps"),
                        "video": i.get("video"), "audio": i.get("audio")}
                       for u, i in cands] for n, cands in channels},
    }
    with open(os.path.join(OUT, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    print("[done] 频道 %d 个 / 源 %d 条 / 实测 %d 条 / 存活 %d / AAC %d / 剔除频道 %d / 用时 %.0fs"
          % (report["channels"], report["streams"], report["probed"],
             report["alive"], report["aac"], len(dropped_ch), report["elapsed_sec"]),
          flush=True)
    for g in app_json["groups"]:
        print("   %s: %d 个台" % (g["name"], len(g["channels"])), flush=True)


if __name__ == "__main__":
    main()
