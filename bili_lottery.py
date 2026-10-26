#!/usr/bin/env python3
# 一次性脚本：B站「双关注 + 转发」抽奖
#
# 用法（配置已写死在下方）:
#   export BILI_COOKIE='SESSDATA=xxx'
#   python3 bili_lottery.py <一等奖人数> <二等奖人数> <seed>
#
# 离线复算（不需要 Cookie）:
#   python3 bili_lottery.py --replay <名单.csv> <一等奖人数> <二等奖人数> <seed>
#
# 结果可复现：名单按 UID 排序 -> sha256 定 seed 派生 -> Fisher-Yates 洗牌
# 同一次洗牌切成：一等奖 / 二等奖 / 备选，三档天然不重叠。

import sys, os, re, json, time, random, hashlib, csv
from urllib.parse import urlencode, quote
import requests

# ============ 固定配置：改这里 ============
DYNAMIC_URL = "https://www.bilibili.com/opus/1261303758808154120"
UP_A_UID = "297667433"
UP_B_UID = "3747571612781876"
BACKUP_N = 3          # 备选人数
# ==========================================

MIXIN_TAB = [
    46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,
    39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,
    6,63,57,62,11,36,20,34,44,52
]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")
API = "https://api.bilibili.com"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Bili:
    def __init__(self, cookie):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
        })
        if cookie:
            for kv in cookie.split(";"):
                if "=" in kv:
                    k, v = kv.strip().split("=", 1)
                    self.s.cookies.set(k, v, domain=".bilibili.com")
        self._wbi = None
        self._buvid()

    def _buvid(self):
        try:
            r = self.s.get(f"{API}/x/frontend/finger/spi", timeout=10).json()
            d = r.get("data") or {}
            if d.get("b_3"):
                self.s.cookies.set("buvid3", d["b_3"], domain=".bilibili.com")
            if d.get("b_4"):
                self.s.cookies.set("buvid4", d["b_4"], domain=".bilibili.com")
            log("[i] buvid ok")
        except Exception as e:
            log("[!] buvid fail:", e)

    def wbi_keys(self, force=False):
        if self._wbi and not force:
            return self._wbi
        d = self.s.get(f"{API}/x/web-interface/nav", timeout=10).json()
        w = (d.get("data") or {}).get("wbi_img") or {}
        img = (w.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (w.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if not img or not sub:
            raise RuntimeError(f"拿不到 wbi keys: {d.get('code')} {d.get('message')}")
        self._wbi = (img, sub)
        return self._wbi

    def sign(self, params):
        img, sub = self.wbi_keys()
        mixin = "".join((img + sub)[i] for i in MIXIN_TAB)[:32]
        p = dict(params)
        p["wts"] = int(time.time())
        p = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in sorted(p.items())}
        p["w_rid"] = hashlib.md5((urlencode(p) + mixin).encode()).hexdigest()
        return p

    def get(self, path, params=None, signed=True, tries=3):
        params = params or {}
        for i in range(tries):
            try:
                p = self.sign(params) if signed else params
                r = self.s.get(API + path, params=p, timeout=15)
                if r.status_code == 412:
                    log("[!] 412 IP 风控，等 5s 重试")
                    time.sleep(5); continue
                if r.status_code != 200:
                    log(f"[!] http {r.status_code} {path}")
                    time.sleep(2); continue
                j = r.json()
                code = j.get("code")
                if code == -352:
                    log("[!] -352 签名/风控，刷新 key 重试")
                    self.wbi_keys(force=True); time.sleep(2); continue
                if code == -101:
                    raise RuntimeError("账号未登录：Cookie 没带上或已失效（SESSDATA 过期）")
                return j
            except RuntimeError:
                raise
            except Exception as e:
                log("[!] err", path, e); time.sleep(2)
        raise RuntimeError(f"请求失败: {path}")


def parse_dynamic_id(s):
    s = s.strip()
    if s.isdigit():
        return s
    m = re.search(r"(?:t\.bilibili\.com/|/opus/|/dynamic/)(\d+)", s)
    if m:
        return m.group(1)
    raise SystemExit(f"认不出的动态ID: {s}")


def collect_forwarders(b, dyn_id, max_pages=2000, gap=1.0):
    """拉全部转发者。自适应解析不同返回结构。"""
    out, seen = [], set()
    offset = ""
    for page in range(1, max_pages + 1):
        j = b.get("/x/polymer/web-dynamic/v1/detail/forward",
                  {"id": dyn_id, "offset": offset, "page_size": 20})
        data = j.get("data") or {}
        items = data.get("items") or data.get("list") or data.get("reposts") or []
        if not items:
            log(f"[i] 第 {page} 页空，结束（共 {len(out)} 条）")
            break
        for it in items:
            user = it.get("user") or {}
            uid = user.get("mid") or it.get("mid") or it.get("uid")
            if not uid:
                continue
            uid = str(uid)
            if uid in seen:
                continue
            seen.add(uid)
            desc = it.get("desc") or {}
            out.append({
                "uid": uid,
                "uname": user.get("name") or "",
                "ctime": it.get("pub_time") or "",
                "content": (desc.get("text") or "")[:200].replace("\n", " "),
            })
        nxt = data.get("offset") or ""
        if not nxt or str(nxt) == str(offset) or not data.get("has_more", True):
            log(f"[i] 无下一页游标，结束（共 {len(out)} 条）")
            break
        offset = nxt
        log(f"[i] page {page} -> {len(out)} 条, next={offset}")
        time.sleep(gap)
    return out


def check_follow(b, uid, targets, max_pages=80, gap=0.3):
    """查 uid 的关注列表，看是否包含 targets。返回 (命中集合, code, total)
    code: 0=成功, 22115=用户设置了隐私（无法验证）"""
    found, pn, total = set(), 1, None
    while pn <= max_pages:
        try:
            j = b.get("/x/relation/followings", {"vmid": uid, "pn": pn, "ps": 50}, signed=False)
        except Exception as e:
            log(f"[!] {uid} p{pn} {e}")
            return found, -1, total
        if j.get("code") != 0:
            return found, j.get("code"), total
        d = j.get("data") or {}
        total = d.get("total")
        lst = d.get("list") or []
        for x in lst:
            m = str(x.get("mid"))
            if m in targets:
                found.add(m)
        if found >= targets or not lst or len(lst) < 50 or (total and total > 0 and pn * 50 >= total):
            break
        pn += 1
        time.sleep(gap)
    return found, 0, total


def draw(uids, n_first, n_second, seed_text):
    """确定性抽奖：排序 -> 洗牌 -> 一次性切出一等奖/二等奖/备选（互不重叠）"""
    ordered = sorted(uids)
    digest = hashlib.sha256((seed_text + "|" + ",".join(ordered)).encode()).hexdigest()
    rng = random.Random(int(digest[:16], 16))
    pool = ordered[:]
    rng.shuffle(pool)
    first = pool[:n_first]
    second = pool[n_first:n_first + n_second]
    backup = pool[n_first + n_second:n_first + n_second + BACKUP_N]
    return first, second, backup, digest


def show(first, second, backup, digest, name, seed, extra=""):
    print("\n===== 抽奖结果 =====")
    if extra:
        print(extra)
    print(f"名单摘要 sha256: {digest}")
    print(f"seed: {seed}")
    print(f"\n一等奖（{len(first)} 名）:")
    for i, u in enumerate(first, 1):
        print(f"  {i}. {name.get(u,'')}  uid={u}  https://space.bilibili.com/{u}")
    print(f"\n二等奖（{len(second)} 名）:")
    for i, u in enumerate(second, 1):
        print(f"  {i}. {name.get(u,'')}  uid={u}  https://space.bilibili.com/{u}")
    print(f"\n备选（{len(backup)} 名）:")
    for i, u in enumerate(backup, 1):
        print(f"  {i}. {name.get(u,'')}  uid={u}")


def replay(csv_path, n_first, n_second, seed):
    """离线复算：只用名单 CSV，不联网。任何人可验证。"""
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
    good = sorted([r["uid"] for r in rows if str(r.get("ok")) == "True"])
    name = {r["uid"]: r.get("uname", "") for r in rows}
    first, second, backup, digest = draw(good, n_first, n_second, seed)
    print("===== 离线复算 =====")
    print(f"名单文件: {csv_path}")
    print(f"合格人数: {len(good)}")
    show(first, second, backup, digest, name, seed)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--replay":
        if len(sys.argv) < 6:
            raise SystemExit("用法: bili_lottery.py --replay <名单.csv> <一等奖人数> <二等奖人数> <seed>")
        replay(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5])
        return

    if len(sys.argv) < 4:
        raise SystemExit("用法: python3 bili_lottery.py <一等奖人数> <二等奖人数> <seed>")
    n_first = int(sys.argv[1])
    n_second = int(sys.argv[2])
    seed = sys.argv[3]
    cookie = os.environ.get("BILI_COOKIE", "")

    dyn_id = parse_dynamic_id(DYNAMIC_URL)
    up1, up2 = UP_A_UID, UP_B_UID
    b = Bili(cookie)

    log(f"[i] 动态 {dyn_id} | UP {up1} & {up2} | 一等奖 {n_first} 二等奖 {n_second} | seed={seed}")

    detail = b.get("/x/polymer/web-dynamic/v1/detail", {"id": dyn_id})
    st = ((detail.get("data") or {}).get("item") or {}).get("modules", {}).get("module_stat", {})
    log(f"[i] 转发数: {(st.get('forward') or {}).get('count')}")

    fs = collect_forwarders(b, dyn_id)
    if not fs:
        raise SystemExit("没抓到转发者（检查 Cookie / 动态是否设置了转发可见）")

    uids = [f["uid"] for f in fs]
    targets = {str(up1), str(up2)}
    for i, f in enumerate(fs, 1):
        hit, code, total = check_follow(b, f["uid"], targets)
        f["f_up1"] = str(up1) in hit
        f["f_up2"] = str(up2) in hit
        f["following"] = total
        f["state"] = "privacy" if code == 22115 else ("ok" if code == 0 else f"err{code}")
        f["ok"] = f["state"] == "ok" and f["f_up1"] and f["f_up2"]
        log(f"[{i}/{len(fs)}] {f['uid']} {f['uname'][:12]} 关注数={total} A={f['f_up1']} B={f['f_up2']} {f['state']}")

    good = [f["uid"] for f in fs if f["ok"]]
    priv = [f for f in fs if f["state"] == "privacy"]
    log(f"[i] 转发 {len(fs)} 人 -> 合格 {len(good)} 人 | 隐私无法验证 {len(priv)} 人")

    csv_path = f"lottery_{dyn_id}_all.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=["uid", "uname", "ctime", "f_up1", "f_up2", "ok", "state", "following", "content"])
        w.writeheader(); w.writerows(fs)
    log(f"[i] 全量名单 -> {csv_path}")

    if not good:
        raise SystemExit("无人满足双关注条件")

    winners_first, winners_second, backup, digest = draw(good, n_first, n_second, seed)
    name = {f["uid"]: f["uname"] for f in fs}
    show(winners_first, winners_second, backup, digest, name, seed,
         f"动态: {dyn_id}\n合格人数: {len(good)}")


if __name__ == "__main__":
    main()
