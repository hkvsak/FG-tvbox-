#!/usr/bin/env python3
"""
8x8x (3abfug92d.com) — 视频爬虫 + PotPlayer 播放 + 缓存管理
用法: python 8x8x_crawler.py
"""

import os, re, shutil, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import curl_cffi.requests as requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BASE_URL = "https://www.3abfug92d.com"
POTPLAYER = r"E:\potplayer\PotPlayerMini64.exe"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
CATS = {1: "大陆", 2: "日韩", 3: "欧美", 4: "动漫", 5: "三级"}
CACHE_DIR = Path(os.environ.get("TEMP", "/tmp")) / "8x8x_cache"


def S():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    s.verify = False
    return s


# ═══════════ 数据获取 ═══════════

def cats(s):
    r = s.get(f"{BASE_URL}/32if2/", impersonate="chrome120")
    r.raise_for_status()
    d = {}
    for m in re.finditer(r'href=/category/(\d+)/[^>]*>([^<]+)</a>', r.text):
        cid = int(m.group(1))
        n = m.group(2).strip()
        if cid not in d and "&#187;" not in n:
            d[cid] = n
    return d


def maxp(s, cid):
    r = s.get(f"{BASE_URL}/32if2/category/{cid}/", impersonate="chrome120")
    r.raise_for_status()
    m = re.search(r'data-max=(\d+)', r.text)
    return int(m.group(1)) if m else 1


def crawl(s, cid, page):
    r = s.get(f"{BASE_URL}/32if2/category/{cid}/page/{page}/",
              impersonate="chrome120", timeout=15)
    r.raise_for_status()
    vids = []
    for m in re.finditer(r'<a href=(/vd/(\d+)/)\s[^>]*>(.*?)</a>', r.text, re.DOTALL):
        vid = m.group(2)
        inner = m.group(3)
        t = re.search(r"<div class=card-title>(.*?)</div>", inner)
        vids.append({"vid": vid, "title": t.group(1) if t else "N/A"})
    return vids


def video(s, vid):
    r = s.get(f"{BASE_URL}/32if2/vd/{vid}/", impersonate="chrome120", timeout=15)
    r.raise_for_status()
    h = r.text
    info = {"vid": vid, "routes": []}
    m = re.search(r"<title>(.*?)</title>", h)
    info["title"] = m.group(1) if m else "N/A"
    mp = re.search(r"data-m3u8\s*=\s*([^\s>]+)", h)
    if mp:
        p = mp.group(1).strip('"').strip("'")
        for i, rn in enumerate(["data-route1", "data-route2", "data-route3"], 1):
            rm = re.search(rf'{rn}\s*=\s*([^\s>]+)', h)
            if rm:
                rt = rm.group(1).strip('"').strip("'")
                info["routes"].append({
                    "name": f"线路{i}",
                    "url": rt.rstrip('/') + '/' + p.lstrip('/'),
                    "status": "?",
                })
    return info


# ═══════════ 下载 + AES解密 + 合并 ═══════════

def parse_m3u8(s, master_url):
    r = s.get(master_url, impersonate="chrome120",
              headers={"Referer": BASE_URL + "/"}, timeout=10)
    r.raise_for_status()
    bw, bp = 0, None
    for ln in r.text.split('\n'):
        if 'BANDWIDTH=' in ln:
            b = int(re.search(r'BANDWIDTH=(\d+)', ln).group(1))
            if b > bw:
                bw = b
    lns = r.text.split('\n')
    for i, ln in enumerate(lns):
        if f'BANDWIDTH={bw}' in ln and i + 1 < len(lns) \
           and lns[i + 1].strip() and not lns[i + 1].startswith('#'):
            bp = lns[i + 1].strip()
            break
    if not bp:
        raise ValueError("no variant")

    base = master_url.rsplit('/', 1)[0]
    var_url = base + '/' + bp
    r2 = s.get(var_url, impersonate="chrome120",
               headers={"Referer": BASE_URL + "/"}, timeout=10)
    r2.raise_for_status()

    ts_urls = []
    dur = 0.0
    key_url = var_url.rsplit('/', 1)[0] + '/key.key'
    for ln in r2.text.split('\n'):
        if ln.startswith('#EXTINF:'):
            dur += float(ln.split(':')[1].rstrip(','))
        elif ln.strip().endswith('.ts') and ln.strip().startswith('http'):
            ts_urls.append(ln.strip())

    kr = s.get(key_url, impersonate="chrome120",
               headers={"Referer": BASE_URL + "/"}, timeout=10)
    kr.raise_for_status()
    return var_url, key_url, kr.content, ts_urls, dur


def dl_ts(s, url, idx, total, cache_dir):
    fn = cache_dir / f"seg_{idx:04d}.ts"
    if fn.exists():
        return idx, fn, True
    try:
        r = s.get(url, impersonate="chrome120",
                  headers={"Referer": BASE_URL + "/"}, timeout=30)
        r.raise_for_status()
        fn.write_bytes(r.content)
        return idx, fn, True
    except Exception as e:
        return idx, None, str(e)


def download_and_decrypt(s, master_url, progress_cb=None):
    var_url, key_url, key, ts_urls, total_dur = parse_m3u8(s, master_url)
    vid_hash = re.search(r'/([a-f0-9]{32})/', var_url)
    vhash = vid_hash.group(1) if vid_hash else "video"
    cache_dir = CACHE_DIR / vhash
    cache_dir.mkdir(parents=True, exist_ok=True)
    output = cache_dir / "output.ts"

    if output.exists():
        if progress_cb:
            progress_cb(f"已缓存 ({output.stat().st_size // 1024 // 1024}MB)")
        return str(output), int(total_dur), vhash

    total = len(ts_urls)
    if progress_cb:
        progress_cb(f"下载 {total} 个分片...")

    downloaded = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {
            ex.submit(dl_ts, s, url, i, total, cache_dir): i
            for i, url in enumerate(ts_urls)
        }
        for f in as_completed(futures):
            idx, fname, ok = f.result()
            downloaded[idx] = (fname, ok)
            if progress_cb and idx % 10 == 0:
                done = sum(1 for v in downloaded.values() if v[0])
                progress_cb(f"下载中... {done}/{total}")

    failed = [(i, err) for i, (fn, ok) in downloaded.items() if not fn]
    if failed:
        raise RuntimeError(f"{len(failed)} 个分片失败: {failed[:3]}")

    if progress_cb:
        progress_cb("AES解密 + 合并...")

    iv = b'\x00' * 16
    with open(output, 'wb') as outf:
        for i in sorted(downloaded.keys()):
            enc = downloaded[i][0].read_bytes()
            dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor().update(enc)
            outf.write(dec)

    size_mb = output.stat().st_size // 1024 // 1024
    if progress_cb:
        progress_cb(f"✅ {int(total_dur // 60)}分{int(total_dur % 60)}秒 | {size_mb}MB")
    return str(output), int(total_dur), vhash


# ═══════════ 缓存管理 ═══════════

def cache_stats():
    if not CACHE_DIR.exists():
        return 0, 0, []
    items = []
    for d in sorted(CACHE_DIR.iterdir()):
        if d.is_dir():
            out = d / "output.ts"
            if out.exists():
                items.append((d.name, out.stat().st_size // 1024 // 1024, str(out)))
    total_mb = sum(s for _, s, _ in items)
    return len(items), total_mb, items


def cache_menu():
    """缓存管理界面"""
    while True:
        cls()
        n, total_mb, items = cache_stats()

        if n == 0:
            print("\n══════════════════════════════════════════")
            print("  缓存管理 — 空")
            print("══════════════════════════════════════════")
            print("\n  [B] 返回")
        else:
            print("\n══════════════════════════════════════════")
            print(f"  缓存管理 — {n} 个视频, 共 {total_mb}MB")
            print("══════════════════════════════════════════")
            print(f"\n  {'#':<4} {'哈希':<36} 大小")
            print(f"  {'─'*4} {'─'*36} {'─'*10}")
            for i, (h, sz, _) in enumerate(items, 1):
                print(f"  {i:<4} {h:<36} {sz}MB")
            print(f"\n  [数字] 删除单个  [A] 全部删除  [B] 返回")

        try:
            cmd = input("\n👉 ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return

        if cmd == "b":
            return

        if cmd == "a" and items:
            try:
                cf = input(f"  ⚠ 确认删除全部 {n} 个缓存 ({total_mb}MB)? [y/N]: ").strip().lower()
            except:
                continue
            if cf == "y":
                shutil.rmtree(str(CACHE_DIR))
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                print(f"  ✅ 已清空全部缓存")
                time.sleep(1)
            continue

        try:
            idx = int(cmd)
            if 1 <= idx <= len(items):
                h, sz, out_path = items[idx - 1]
                d = CACHE_DIR / h
                try:
                    cf = input(f"  ⚠ 删除 [{h[:12]}...] ({sz}MB)? [y/N]: ").strip().lower()
                except:
                    continue
                if cf == "y":
                    shutil.rmtree(str(d))
                    print(f"  ✅ 已删除, 释放 {sz}MB")
                    time.sleep(1)
        except ValueError:
            pass


# ═══════════ 测速 ═══════════

def speedtest(routes):
    for rt in routes:
        try:
            t0 = time.time()
            resp = requests.get(rt["url"], headers={"User-Agent": UA},
                                verify=False, timeout=8, impersonate="chrome120")
            ms = int((time.time() - t0) * 1000)
            rt["status"] = f"✅ {ms}ms" if resp.status_code == 200 else f"❌ {resp.status_code}"
        except:
            rt["status"] = "❌ 超时"
    return routes


# ═══════════ 播放 ═══════════

def play(path):
    for p in [POTPLAYER, r"E:\potplayer\PotPlayer.exe",
              r"E:\PotPlayer\PotPlayerMini64.exe"]:
        if os.path.exists(p):
            break
    else:
        print(f"\n  ❌ 找不到 PotPlayer!\n  📋 {path}")
        return False
    print(f"\n  🎬 {os.path.basename(p)}")
    print(f"  📺 {path}")
    subprocess.Popen([p, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


# ═══════════ UI ═══════════

def cls():
    os.system("cls" if os.name == "nt" else "clear")


def main_menu(s):
    """主菜单: 选分类 / 缓存管理"""
    n_cache, cache_mb, _ = cache_stats()
    cache_info = f"缓存:{n_cache}个/{cache_mb}MB" if n_cache else "缓存:空"

    cts = cats(s) or CATS

    print(f"\n  ╔══════════════════════════════════╗")
    print(f"  ║    8x8x 视频爬虫 | {cache_info:<16} ║")
    print(f"  ╚══════════════════════════════════╝")
    print()
    for cid, name in sorted(cts.items()):
        print(f"    [{cid}] {name}")
    print()
    print(f"    [D] 🗑️  缓存管理")
    print(f"    [0] 退出")

    while True:
        try:
            c = input("\n  👉 ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if c == "0":
            return None
        if c == "d":
            cache_menu()
            return "menu"  # 返回后刷新主菜单
        try:
            cid = int(c)
            if cid in cts:
                return cid
        except ValueError:
            pass


def pick_vid(s, cid, cn):
    mp = maxp(s, cid)
    cur = 1
    while True:
        cls()
        print(f"\n  ══ {cn} — 第 {cur}/{mp} 页 ══\n")
        try:
            vs = crawl(s, cid, cur)
        except Exception as e:
            print(f"  ❌ {e}")
            time.sleep(2)
            continue
        print(f"  {'#':<4} {'ID':<8} 标题")
        print(f"  {'─'*4} {'─'*8} {'─'*55}")
        for i, v in enumerate(vs, 1):
            print(f"  {i:<4} [{v['vid']:<6}] {v['title'][:55]}")
        print(f"\n  [N]下页 [P]上页 [G]跳转  [数字]选择  [B]返回 [Q]退出 [D]缓存")
        try:
            cmd = input("\n  👉 ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if cmd in ("q", "b"):
            return None
        if cmd == "d":
            cache_menu()
            cls()
            print(f"\n  ══ {cn} — 第 {cur}/{mp} 页 ══\n")
            try:
                vs = crawl(s, cid, cur)
            except:
                pass
            continue
        if cmd == "n" and cur < mp:
            cur += 1
        elif cmd == "p" and cur > 1:
            cur -= 1
        elif cmd == "g":
            try:
                p = int(input("  页码: ").strip())
                if 1 <= p <= mp:
                    cur = p
            except ValueError:
                pass
        else:
            try:
                idx = int(cmd)
                if 1 <= idx <= len(vs):
                    return vs[idx - 1]
            except ValueError:
                pass


def show_detail(s, v):
    vid = v["vid"]
    print(f"\n  🎬 加载 [{vid}]...")
    try:
        info = video(s, vid)
        info["routes"] = speedtest(info["routes"])
    except Exception as e:
        print(f"  ❌ {e}")
        return None

    while True:
        cls()
        print(f"\n  ══ {info.get('title', v['title'])} ══\n")
        rs = info.get("routes", [])
        if rs:
            print(f"  ┌{'─'*56}┐")
            print(f"  │ {'线路 (CDN)':^54} │")
            print(f"  ├{'─'*8}┬{'─'*12}┬{'─'*33}┤")
            print(f"  │ {'按键':^6} │ {'状态':^10} │ {'地址':^31} │")
            print(f"  ├{'─'*8}┼{'─'*12}┼{'─'*33}┤")
            for i, rt in enumerate(rs, 1):
                host = re.search(r'https?://([^/]+)', rt["url"])
                h = host.group(1)[:31] if host else rt["url"][:31]
                print(f"  │  [{i}]播放  │ {rt['status']:<10} │ {h:<31} │")
            print(f"  └{'─'*8}┴{'─'*12}┴{'─'*33}┘")
            try:
                best = min(rs, key=lambda r: (
                    9999 if "❌" in r["status"]
                    else int(re.search(r'(\d+)ms', r["status"]).group(1))))
                print(f"\n  ⚡ 最快: {best['name']} ({best['status']})")
            except:
                pass
            print(f"\n  输入 [1/2/3] 下载+播放  [R]重新测速  [B]返回  [D]缓存")
        else:
            print(f"  ⚠ 无可用线路\n  [B]返回")

        try:
            cmd = input("\n  👉 ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if cmd == "b":
            return None
        if cmd == "d":
            cache_menu()
            cls()
            print(f"\n  ══ {info.get('title', v['title'])} ══\n")
            # 重新显示线路 — 无需重发请求
            if rs:
                print(f"  ┌{'─'*56}┐")
                print(f"  │ {'线路 (CDN)':^54} │")
                print(f"  ├{'─'*8}┬{'─'*12}┬{'─'*33}┤")
                print(f"  │ {'按键':^6} │ {'状态':^10} │ {'地址':^31} │")
                print(f"  ├{'─'*8}┼{'─'*12}┼{'─'*33}┤")
                for i, rt in enumerate(rs, 1):
                    host = re.search(r'https?://([^/]+)', rt["url"])
                    h = host.group(1)[:31] if host else rt["url"][:31]
                    print(f"  │  [{i}]播放  │ {rt['status']:<10} │ {h:<31} │")
                print(f"  └{'─'*8}┴{'─'*12}┴{'─'*33}┘")
                try:
                    best = min(rs, key=lambda r: (
                        9999 if "❌" in r["status"]
                        else int(re.search(r'(\d+)ms', r["status"]).group(1))))
                    print(f"\n  ⚡ 最快: {best['name']} ({best['status']})")
                except:
                    pass
            continue
        if cmd == "r" and rs:
            print("  ⏳ 重新测速...")
            speedtest(rs)
            continue
        try:
            n = int(cmd)
            if rs and 1 <= n <= len(rs):
                rt = rs[n - 1]

                def prog(msg):
                    print(f"\r  {msg:<50}", end="", flush=True)

                print(f"\n  ⏳ 下载+解密中...")
                try:
                    local, dur, vhash = download_and_decrypt(s, rt["url"], prog)
                    print(f"\n  ✅ 完成!")
                    return local
                except Exception as e:
                    print(f"\n  ❌ 失败: {e}")
                    time.sleep(2)
                    continue
        except ValueError:
            pass


# ═══════════ 主循环 ═══════════

def main():
    s = S()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    while True:
        cls()
        ret = main_menu(s)
        if ret is None:
            break
        if ret == "menu":
            continue
        cid = ret
        cn = CATS.get(cid, f"分类{cid}")

        while True:
            v = pick_vid(s, cid, cn)
            if v is None:
                break
            f = show_detail(s, v)
            if f is None:
                continue
            print(f"\n{'=' * 50}")
            play(f)
            print(f"{'=' * 50}")
            try:
                cmd = input("\n  👉 [B]返回列表  [Q]退出: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                break
            if cmd == "q":
                print("\n  👋 再见!")
                return

    print("\n  👋 再见!")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  👋 已退出")
