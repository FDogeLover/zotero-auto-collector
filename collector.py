#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zotero-auto-collector — 每日学术文献增量采集，自动写入 Zotero 云端库

数据源：OpenAlex（主源，全学科+引用数据）+ arXiv API（预印本时效）
流程：昨日窗口增量查询 → 关键词过滤 → 双层去重（本地 state.db + 云端标题查重）
      → 多通道分流（个人库 / 任意群组库）→ Zotero Web API v3 写入

仅 Python 标准库，零第三方依赖。配置见 .env.example。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# Windows GBK 控制台防中文输出崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))


# ---------------- 配置 ----------------

def load_env():
    cfg = {}
    path = os.path.join(BASE, ".env")
    if not os.path.exists(path):
        sys.exit("缺少配置文件 .env（复制 .env.example 并填写）")
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


CFG = load_env()
USER = CFG.get("ZOTERO_USER_ID", "")
KEY = CFG.get("ZOTERO_API_KEY", "")
MAILTO = CFG.get("OPENALEX_MAILTO", "zotero-auto-collector@example.com")  # OpenAlex 礼貌池


def load_channels():
    """解析 .env 中的 CHANNEL_<N>_NAME / _KEYWORDS / _TARGET 配置。

    TARGET 取值：user（个人库）或 group:<GroupID>（群组库）。
    """
    nums = sorted({m.group(1) for k in CFG
                   for m in [re.match(r"CHANNEL_(\d+)_KEYWORDS", k)] if m})
    channels = []
    for n in nums:
        kws = [k.strip() for k in CFG[f"CHANNEL_{n}_KEYWORDS"].split(",") if k.strip()]
        if not kws:
            continue
        channels.append({
            "name": CFG.get(f"CHANNEL_{n}_NAME", f"channel{n}"),
            "keywords": kws,
            "target": CFG.get(f"CHANNEL_{n}_TARGET", "user"),
        })
    return channels


def target_lib(target):
    """通道目标 → (API 库路径, 可写性检查)"""
    if target == "user":
        return f"users/{USER}"
    if target.startswith("group:"):
        return f"groups/{target.split(':', 1)[1]}"
    raise ValueError(f"非法 TARGET: {target}（应为 user 或 group:<GroupID>）")


# ---------------- 日志（超 1MB 自动截半，适配小磁盘 VPS） ----------------

LOGF = os.path.join(BASE, "collector.log")
TODAY_TAG = datetime.now().strftime("%Y-%m-%d")


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOGF, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    try:
        if os.path.getsize(LOGF) > 1_000_000:
            data = open(LOGF, "rb").read()
            open(LOGF, "wb").write(data[-400_000:])
    except OSError:
        pass


# ---------------- HTTP（带退避重试） ----------------

def http(url, data=None, method="GET", auth=True, retry=3):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = f"Bearer {KEY}"
        headers["Zotero-API-Version"] = "3"
    req = urllib.request.Request(
        url, json.dumps(data).encode() if data is not None else None,
        headers=headers, method=method)
    for i in range(retry):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                body = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(body) if body.strip().startswith(("{", "[")) else body)
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503) and i < retry - 1:
                time.sleep(6 * (i + 1))
                continue
            return e.code, {}
        except Exception:
            if i < retry - 1:
                time.sleep(4)
                continue
            raise


# ---------------- 去重状态 ----------------

db = sqlite3.connect(os.path.join(BASE, "state.db"))
db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, title TEXT, ts TEXT)")


def seen_before(item_id, title, lib):
    if db.execute("SELECT 1 FROM seen WHERE id=?", (item_id,)).fetchone():
        return True
    # 云端标题查重：防重复入库，也防与手动添加的文献撞车
    q = urllib.parse.quote(title[:80])
    st, items = http(f"https://api.zotero.org/{lib}/items?q={q}&limit=5")
    if st == 200 and isinstance(items, list):
        for it in items:
            if (it.get("data", {}).get("title") or "").strip().lower() == title.strip().lower():
                return True
    return False


def mark_seen(item_id, title):
    db.execute("INSERT OR REPLACE INTO seen VALUES (?,?,datetime('now'))", (item_id, title))
    db.commit()


# ---------------- 数据源 ----------------

def oa_abstract(work):
    """OpenAlex 的 abstract_inverted_index（倒排索引）→ 正常文本"""
    inv = work.get("abstract_inverted_index")
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))[:2000]


def fetch_openalex(keyword, day):
    url = (f"https://api.openalex.org/works"
           f"?filter=from_publication_date:{day},to_publication_date:{day}"
           f"&search={urllib.parse.quote(keyword)}&per-page=50&mailto={MAILTO}")
    st, data = http(url, auth=False)
    if st != 200 or not isinstance(data, dict):
        log(f"  OpenAlex 失败 kw={keyword} HTTP={st}")
        return []
    out = []
    for w in data.get("results", []):
        doi = (w.get("doi") or "").replace("https://doi.org/", "")
        src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
        authors = []
        for a in (w.get("authorships") or [])[:10]:
            nm = (a.get("author") or {}).get("display_name") or ""
            if nm:
                parts = nm.rsplit(" ", 1)
                authors.append({"creatorType": "author",
                                "firstName": parts[0], "lastName": parts[1]}
                               if len(parts) == 2 else {"creatorType": "author", "name": nm})
        oa_pdf = ((w.get("best_oa_location") or {}).get("pdf_url") or "")
        out.append({
            "id": "oa:" + (w.get("id", "").rsplit("/", 1)[-1] or doi or w.get("display_name", "")),
            "item": {
                "itemType": "journalArticle",
                "title": w.get("display_name") or "",
                "creators": authors,
                "DOI": doi,
                "publicationTitle": src,
                "date": str(w.get("publication_date") or w.get("publication_year") or ""),
                "abstractNote": oa_abstract(w),
                "url": (w.get("primary_location") or {}).get("landing_page_url") or "",
                "extra": (f"OpenAlex: {w.get('id', '')}\n被引: {w.get('cited_by_count', 0)}"
                          + (f"\nOA PDF: {oa_pdf}" if oa_pdf else "")),
            },
        })
    return out


def fetch_arxiv(keyword, day):
    day8 = day.replace("-", "")  # arXiv 日期语法 YYYYMMDDHHMM，不带连字符
    # 多词关键词必须整句引号包裹，否则 arXiv 按 token 拆分报 500
    phrase = urllib.parse.quote(f'"{keyword}"')
    url = ("https://export.arxiv.org/api/query?search_query="
           f"all:{phrase}+AND+submittedDate:[{day8}0000+TO+{day8}2359]&max_results=40")
    try:
        with urllib.request.urlopen(url, timeout=40) as r:
            xml = r.read().decode("utf-8", "replace")
    except Exception as e:
        log(f"  arXiv 失败 kw={keyword}: {e}")
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(xml)
    except Exception as e:
        log(f"  arXiv 解析失败: {e}")
        return []
    out = []
    for e in root.findall("a:entry", ns):
        title = re.sub(r"\s+", " ", e.findtext("a:title", "", ns)).strip()
        arxiv_id = (e.findtext("a:id", "", ns).rsplit("/abs/", 1)[-1] or "").split("v")[0]
        authors = []
        for a in e.findall("a:author/a:name", ns):
            parts = (a.text or "").rsplit(" ", 1)
            authors.append({"creatorType": "author",
                            "firstName": parts[0], "lastName": parts[1]}
                           if len(parts) == 2 else {"creatorType": "author", "name": a.text})
        out.append({
            "id": "arxiv:" + arxiv_id,
            "item": {
                "itemType": "preprint",
                "title": title,
                "creators": authors,
                "repository": "arXiv",
                "archiveID": f"arXiv:{arxiv_id}",
                "date": (e.findtext("a:published", "", ns) or "")[:10],
                "abstractNote": re.sub(r"\s+", " ", e.findtext("a:summary", "", ns)).strip()[:2000],
                "url": f"https://arxiv.org/abs/{arxiv_id}",
            },
        })
    return out


# ---------------- 主流程 ----------------

def parse_args():
    p = argparse.ArgumentParser(description="每日学术文献增量采集 → Zotero 云端库")
    p.add_argument("--date", metavar="YYYY-MM-DD",
                   help="只采集指定日期（宕机补漏用）")
    p.add_argument("--days", type=int, default=1,
                   help="回溯 N 天（从昨天起往回共 N 天，默认 1=仅昨天）")
    return p.parse_args()


def main():
    args = parse_args()
    log("=== 采集开始 ===")
    if not (USER and KEY):
        log("缺少 ZOTERO_USER_ID / ZOTERO_API_KEY")
        return 1
    channels = load_channels()
    if not channels:
        log("未配置任何通道（CHANNEL_<N>_KEYWORDS），跳过采集")
        return 0

    if args.date:
        days = [args.date]
    else:
        days = [(datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d")
                for d in range(1, max(1, args.days) + 1)]
    window = f"{days[0]}" if len(days) == 1 else f"{days[-1]} ~ {days[0]}（共 {len(days)} 天）"
    log(f"采集窗口: {window}")
    for c in channels:
        log(f"通道 [{c['name']}] → {target_lib(c['target'])} | 关键词: {c['keywords']}")

    # 抓取：所有通道的候选统一收集（同一文献只入库一次，先进先得）
    candidates = []
    for day in days:
        for c in channels:
            for kw in c["keywords"]:
                for cand in fetch_openalex(kw, day) + fetch_arxiv(kw, day):
                    cand["target"] = c["target"]
                    cand["ch_name"] = c["name"]
                    cand["kw"] = kw
                    candidates.append(cand)
                time.sleep(1)  # 对数据源的礼貌间隔
    log(f"候选: {len(candidates)} 条")

    # 去重 + 打标签
    to_write = []  # (lib, item)
    skipped = 0
    claimed = set()
    for c in candidates:
        title = c["item"]["title"]
        if not title:
            continue
        norm = title.strip().lower()
        if norm in claimed:
            skipped += 1
            continue
        lib = target_lib(c["target"])
        if seen_before(c["id"], title, lib):
            skipped += 1
            continue
        claimed.add(norm)
        c["item"]["tags"] = [{"tag": "自动采集"}, {"tag": TODAY_TAG},
                             {"tag": c["kw"]}, {"tag": c["ch_name"]}]
        to_write.append((lib, c))

    # 分库批量写入（每批 25，API 上限 50 取稳妥值）
    ok = 0
    by_lib = {}
    for lib, c in to_write:
        by_lib.setdefault(lib, []).append(c)
    for lib, items in by_lib.items():
        for i in range(0, len(items), 25):
            batch = items[i:i + 25]
            st, resp = http(f"https://api.zotero.org/{lib}/items",
                            data=[c["item"] for c in batch], method="POST")
            if st == 200:
                succ = resp.get("successful", {})
                ok += len(succ)
                for idx in succ:
                    mark_seen(batch[int(idx)]["id"], batch[int(idx)]["item"]["title"])
                if resp.get("failed"):
                    log(f"  部分失败: {list(resp['failed'].values())[:2]}")
            else:
                log(f"  写入 {lib} 失败 HTTP={st}: {str(resp)[:300]}")
            time.sleep(2)

    log(f"=== 完成：写入 {ok} 条 | 去重跳过 {skipped} 条 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
