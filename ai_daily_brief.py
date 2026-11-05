#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_daily_brief.py —— 每天北京时间 08:00 推送一份「AI 圈重大新闻摘要」

设计目标
  1) 零第三方依赖：只用 Python 标准库，GitHub Actions 上不用 pip install，失败面最小。
  2) 只保留重大事件：规则预筛（源分级 + 时间窗 + 去重）→ DeepSeek 按 rubric 打分 → 每天 ≤5 条。
  3) 不编造链接：模型只能返回候选 id，链接由本地映射，模型无法凭空生成 URL。
  4) 三种推送渠道：邮件（SMTP）/ 飞书自定义机器人 / Server酱（微信）。
  5) 失败必告警：任何异常都会尝试往同一渠道发一条失败通知，并以非 0 退出码结束。

用法
  python3 ai_daily_brief.py --dry-run --fake-llm     # 本地自检：抓源+渲染，不调模型、不推送
  python3 ai_daily_brief.py --check-feeds            # 每月巡检：各源是否还活着
  python3 ai_daily_brief.py --at 08:00               # 等到北京时间 08:00 再生成并推送
  python3 ai_daily_brief.py --no-wait                # 立即生成并推送（手动测试用）

环境变量
  DEEPSEEK_API_KEY   必填（除 --dry-run --fake-llm）
  DEEPSEEK_MODEL     默认 deepseek-flash
  CHANNEL            dingtalk | email | feishu | serverchan | console，默认 console
  DINGTALK_WEBHOOK [DINGTALK_SECRET]   钉钉群自定义机器人（推荐，支持加签）
  SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASS MAIL_TO [MAIL_FROM]
  FEISHU_WEBHOOK [FEISHU_SECRET]
  SERVERCHAN_KEY
"""

import argparse
import base64
import gzip
import hashlib
import hmac
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlencode

CST = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (compatible; ai-daily-brief/1.0; +https://github.com/)"
MAX_ITEMS = 5          # 每日条数上限
MIN_SCORE = 4          # 重大事件门槛（0-5）
PER_CATEGORY = 2       # 同一类别每天最多几条
WINDOW_HOURS = 24      # 时间窗

# ---------------------------------------------------------------------------
# 源清单：tier 1 = 官方一手；tier 2 = 一线科技媒体；tier 3 = 聚合/研究（默认关闭）
# enabled=False 的源保留在配置里但不抓取，便于随时启用。
# 实测状态见 README（2026-11-05 抓取）。
# ---------------------------------------------------------------------------
FEEDS = [
    {"name": "OpenAI News",      "tier": 1, "lang": "en", "enabled": True,
     "url": "https://openai.com/news/rss.xml"},
    {"name": "Google DeepMind",  "tier": 1, "lang": "en", "enabled": True,
     # 本机实测偶发 TLS 握手超时，给更短的超时与更少的重试，避免拖慢整轮
     "timeout": 12, "retries": 1,
     "url": "https://deepmind.google/blog/rss.xml"},
    {"name": "Microsoft Research", "tier": 1, "lang": "en", "enabled": True,
     "url": "https://www.microsoft.com/en-us/research/feed/"},
    {"name": "Hugging Face Blog", "tier": 1, "lang": "en", "enabled": False,
     "url": "https://huggingface.co/blog/feed.xml"},  # 本次实测不可达，按需确认
    {"name": "TechCrunch AI",    "tier": 2, "lang": "en", "enabled": True,
     "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "The Verge AI",     "tier": 2, "lang": "en", "enabled": True,
     "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"},
    {"name": "量子位",            "tier": 2, "lang": "zh", "enabled": True,
     "url": "https://www.qbitai.com/feed"},
    {"name": "36氪",              "tier": 2, "lang": "zh", "enabled": True,
     "url": "https://www.36kr.com/feed"},
    {"name": "arXiv cs.AI",      "tier": 3, "lang": "en", "enabled": False,
     "url": "https://export.arxiv.org/rss/cs.AI"},   # 每日数百条，只在做研究专题时开
]

CATEGORIES = ["模型发布", "融资并购", "监管政策", "安全事件", "算力芯片", "组织变动"]

PROMPT = """你是资深 AI 行业编辑，负责给一位中国制造业技术负责人做每日简报。

下面是过去 {hours} 小时的候选新闻（JSON 数组，字段：id/标题/来源/源等级/摘要）。

【只保留重大事件】命中以下任一即算重大，否则一律不要：
1. 模型/产品：头部厂商（OpenAI、Anthropic、Google、Meta、xAI、DeepSeek、阿里、字节、月之暗面、智谱、Mistral、NVIDIA）发布新模型或重大版本，或核心产品能力/定价发生 ≥30% 变化。
2. 融资并购：单笔 ≥ 1 亿美元（约 7 亿人民币），或标的为头部 AI 公司。
3. 监管政策：国家级法律/规章正式生效或发布、监管处罚 ≥ 1000 万美元、出口管制清单变化。
4. 安全事件：官方确认的入侵、大规模数据泄露、严重滥用事件。
5. 算力芯片：头部芯片公司新品、禁售/解禁、产能重大变化。
6. 组织变动：头部 AI 公司 CEO/核心联创离职或重大重组。

【明确排除】教程与 How-to、Prompt 技巧、观点评论/预测、非官方测评榜单、例行版本更新（bugfix/小版本）、招聘、活动预告、纯股价波动、无原始出处的二手搬运、软文。

【打分】每条 0-5 分，综合考虑：是否真的重大（0-2）、是否一手/首发（0-2）、是否可验证（0-1）。只输出 score >= {min_score} 的条目。

【限额】最多 {max_items} 条；同一 category 最多 {per_cat} 条；同一事件被多家报道时只保留一条，优先 tier 1 来源。宁缺毋滥，没有达标事件就返回空数组。

【输出】严格输出 JSON，不要任何解释文字：
{{"items":[{{"id":<候选id>,"score":<0-5>,"category":"模型发布|融资并购|监管政策|安全事件|算力芯片|组织变动","title":"中文标题，不超过40字","one_line":"一句话要点，不超过60字，必须含 主体+做了什么+影响","why":"入选理由，不超过30字"}}]}}

硬性约束：id 必须来自候选列表；禁止编造新闻；禁止输出候选之外的链接。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def today_str():
    now = datetime.now(CST)
    return "%s %s" % (now.strftime("%Y-%m-%d"), WEEK[now.weekday()])


def log(msg):
    print("[%s] %s" % (datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def norm_title(s):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (s or "").lower())


def norm_link(u):
    u = (u or "").split("#")[0]
    u = re.sub(r"[?&](utm_[^&]*|ref|source|spm)=[^&]*", "", u)
    return u.rstrip("/")


def parse_dt(text):
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CST)
    except Exception:
        pass
    try:
        iso = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CST)
    except Exception:
        return None


def tag_local(el):
    return el.tag.rsplit("}", 1)[-1].lower()


def child_text(el, names):
    for c in el:
        if tag_local(c) in names and (c.text or "").strip():
            return c.text.strip()
    return ""


def child_link(el):
    # RSS: <link>url</link>；Atom: <link href="url" rel="alternate"/>
    for c in el:
        if tag_local(c) != "link":
            continue
        href = c.attrib.get("href")
        rel = c.attrib.get("rel", "alternate")
        if href and rel in ("alternate", ""):
            return href.strip()
        if (c.text or "").strip():
            return c.text.strip()
    return ""


# ---------------------------------------------------------------------------
# 抓取 + 解析
# ---------------------------------------------------------------------------
def http_get(url, timeout=20, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
            # 有些源（如 DeepMind）即使没要 gzip 也会压缩返回
            if raw[:2] == b"\x1f\x8b" or "gzip" in enc:
                raw = gzip.decompress(raw)
            elif "deflate" in enc:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            return raw
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries:
                time.sleep(2 * (i + 1))
    raise last


def parse_feed(raw):
    """返回 [(title, link, summary, published_dt)]，兼容 RSS 2.0 与 Atom。"""
    root = ET.fromstring(raw)
    out = []
    for el in root.iter():
        t = tag_local(el)
        if t not in ("item", "entry"):
            continue
        title = strip_html(child_text(el, {"title"}))
        link = child_link(el)
        summary = strip_html(child_text(el, {"description", "summary", "content", "encoded"}))
        published = parse_dt(child_text(el, {"pubdate", "published", "updated", "date"}))
        if title and link:
            out.append((title, link, summary, published))
    return out


def collect(feeds, window_hours):
    now = datetime.now(CST)
    since = now - timedelta(hours=window_hours)
    items, errors, seen_title, seen_link = [], [], set(), set()
    for f in feeds:
        if not f.get("enabled", True):
            continue
        try:
            raw = http_get(f["url"], timeout=f.get("timeout", 20), retries=f.get("retries", 2))
            parsed = parse_feed(raw)
            fresh = 0
            for title, link, summary, pub in parsed:
                if pub and pub < since:
                    continue
                nt, nl = norm_title(title), norm_link(link)
                if not nt or nt in seen_title or (nl and nl in seen_link):
                    continue
                seen_title.add(nt)
                if nl:
                    seen_link.add(nl)
                items.append({
                    "title": title[:180],
                    "link": link,
                    "summary": summary[:300],
                    "published": pub.strftime("%m-%d %H:%M") if pub else "",
                    "source": f["name"],
                    "tier": f["tier"],
                    "lang": f["lang"],
                })
                fresh += 1
            log("OK   %-22s 解析 %3d 条，24h 内 %3d 条" % (f["name"], len(parsed), fresh))
        except Exception as e:  # noqa: BLE001
            errors.append("%s: %s" % (f["name"], e))
            log("FAIL %-22s %s" % (f["name"], e))
    # 官方源优先，其次媒体
    items.sort(key=lambda x: (x["tier"], x["published"] == ""))
    return items, errors


# ---------------------------------------------------------------------------
# 模型打分
# ---------------------------------------------------------------------------
def fake_llm(candidates):
    """本地自检用：按关键词给分，模拟模型返回结构。"""
    hot = ("发布", "推出", "融资", "收购", "监管", "禁令", "泄露", "模型", "GPT", "Claude",
           "Gemini", "DeepSeek", "launch", "release", "raises", "acquire", "model")
    picked = []
    for i, c in enumerate(candidates):
        text = (c["title"] + " " + c["summary"]).lower()
        score = 5 if any(k.lower() in text for k in hot) else 2
        cat = "模型发布"
        if any(k in text for k in ("融资", "收购", "raises", "acquire")):
            cat = "融资并购"
        elif any(k in text for k in ("监管", "法案", "禁令", "regulation")):
            cat = "监管政策"
        if score >= MIN_SCORE:
            picked.append({"id": i, "score": score, "category": cat,
                           "title": c["title"][:40],
                           "one_line": strip_html(c["summary"])[:60] or c["title"][:60],
                           "why": "fake-llm 关键词命中"})
    return {"items": picked}


def call_deepseek(candidates, api_key, model):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你只输出严格 JSON，不输出任何多余文字。"},
            {"role": "user", "content": PROMPT.format(
                hours=WINDOW_HOURS, min_score=MIN_SCORE, max_items=MAX_ITEMS,
                per_cat=PER_CATEGORY) + "\n\n候选：\n" + json.dumps(candidates, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        # DeepSeek V4 默认开启思考模式；这里关掉：省 token、降延迟，且 JSON 输出更稳
        "thinking": {"type": "disabled"},
        "temperature": 0.2,
        "max_tokens": 2000,
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        body = json.loads(r.read().decode("utf-8"))
    content = body["choices"][0]["message"]["content"]
    usage = body.get("usage", {})
    log("DeepSeek 用量：input=%s output=%s" % (usage.get("prompt_tokens"), usage.get("completion_tokens")))
    return json.loads(content)


def select(picked, candidates, items):
    """把模型返回的 id 映射回本地候选，强制限额与去重。"""
    rows, seen_cat, seen_key = [], {}, set()
    for it in sorted(picked.get("items", []), key=lambda x: -float(x.get("score", 0))):
        try:
            idx = int(it["id"])
            c = items[idx]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if float(it.get("score", 0)) < MIN_SCORE:
            continue
        cat = it.get("category") if it.get("category") in CATEGORIES else "模型发布"
        if seen_cat.get(cat, 0) >= PER_CATEGORY:
            continue
        key = norm_title(it.get("title") or c["title"])[:30]
        if key in seen_key:
            continue
        seen_key.add(key)
        seen_cat[cat] = seen_cat.get(cat, 0) + 1
        rows.append({
            "category": cat,
            "title": (it.get("title") or c["title"])[:60],
            "one_line": (it.get("one_line") or "")[:90],
            "why": (it.get("why") or "")[:40],
            "source": c["source"],
            "link": c["link"],            # 链接只来自本地候选，模型无法伪造
            "tier": c["tier"],
        })
        if len(rows) >= MAX_ITEMS:
            break
    return rows


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def render_text(rows, candidates_count, errors, date_str):
    if not rows:
        return ("【AI 日报·今日无重大事件】%s\n\n"
                "过去 %d 小时共扫描 %d 条候选，没有达到「重大事件」门槛的新闻。\n"
                "（本推送每天 08:00 必达，用于确认链路正常。）" % (date_str, WINDOW_HOURS, candidates_count))
    lines = ["【AI 日报】%s  ·  过去 %d 小时 / 候选 %d 条 / 入选 %d 条" % (
        date_str, WINDOW_HOURS, candidates_count, len(rows)), ""]
    for i, r in enumerate(rows, 1):
        lines.append("%d. [%s] %s" % (i, r["category"], r["title"]))
        lines.append("   %s" % r["one_line"])
        lines.append("   来源：%s（T%d） %s" % (r["source"], r["tier"], r["link"]))
        lines.append("")
    if errors:
        lines.append("⚠️ 本次有 %d 个源抓取失败：%s" % (len(errors), "; ".join(errors)[:300]))
    lines.append("—— 由 ai_daily_brief 自动生成，规则见仓库 README")
    return "\n".join(lines)


def render_html(rows, candidates_count, errors, date_str):
    if not rows:
        return ("<h2>AI 日报 · 今日无重大事件</h2><p>%s，过去 %d 小时扫描 %d 条候选，"
                "无达到门槛的新闻。</p>" % (date_str, WINDOW_HOURS, candidates_count))
    parts = ['<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
             'max-width:680px;color:#1a1a1a">',
             '<h2 style="margin:0 0 4px">AI 日报 · %s</h2>' % date_str,
             '<p style="color:#666;margin:0 0 18px">过去 %d 小时 / 候选 %d 条 / 入选 %d 条</p>'
             % (WINDOW_HOURS, candidates_count, len(rows))]
    for r in rows:
        parts.append(
            '<div style="padding:14px 0;border-top:1px solid #e5e5e5">'
            '<div style="font-size:12px;color:#8a6d3b;margin-bottom:4px">%s</div>'
            '<div style="font-size:16px;font-weight:600;line-height:1.4">%s</div>'
            '<div style="font-size:14px;color:#333;margin:6px 0 8px;line-height:1.5">%s</div>'
            '<div style="font-size:12px;color:#666">来源：%s（T%d） · '
            '<a href="%s" style="color:#0b62c4">原文链接</a></div></div>'
            % (html.escape(r["category"]), html.escape(r["title"]),
               html.escape(r["one_line"]), html.escape(r["source"]),
               r["tier"], html.escape(r["link"])))
    if errors:
        parts.append('<p style="font-size:12px;color:#b45309">⚠️ 抓取失败的源：%s</p>'
                     % html.escape("; ".join(errors)[:300]))
    parts.append('<p style="font-size:12px;color:#999;margin-top:20px">'
                 '由 ai_daily_brief 自动生成</p></div>')
    return "".join(parts)


# ---------------------------------------------------------------------------
# 推送
# ---------------------------------------------------------------------------
def push_email(subject, text, html_body):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    pwd = os.environ["SMTP_PASS"]
    to = [x.strip() for x in os.environ["MAIL_TO"].split(",") if x.strip()]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("MAIL_FROM", user)
    msg["To"] = ", ".join(to)
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30, context=ctx) as s:
            s.login(user, pwd)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(user, pwd)
            s.send_message(msg)
    log("邮件已发送至 %s" % ", ".join(to))


def push_feishu(subject, text):
    url = os.environ["FEISHU_WEBHOOK"]
    secret = os.environ.get("FEISHU_SECRET", "")
    payload = {"msg_type": "text", "content": {"text": "%s\n\n%s" % (subject, text)}}
    if secret:
        ts = str(int(time.time()))
        key = ("%s\n%s" % (ts, secret)).encode("utf-8")
        sign = base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()
        payload["timestamp"] = ts
        payload["sign"] = sign
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode("utf-8"))
    if resp.get("code") not in (0, None) and resp.get("StatusCode") not in (0, None):
        raise RuntimeError("飞书返回异常: %s" % resp)
    log("飞书已推送")


def push_serverchan(subject, text):
    key = os.environ["SERVERCHAN_KEY"]
    data = urlencode({"title": subject[:32], "desp": text}).encode("utf-8")
    req = urllib.request.Request("https://sctapi.ftqq.com/%s.send" % key, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode("utf-8"))
    if resp.get("code") != 0:
        raise RuntimeError("Server酱返回异常: %s" % resp)
    log("Server酱（微信）已推送")


def push_dingtalk(subject, text):
    """钉钉群自定义机器人。用「加签」模式（GitHub Runner 出口 IP 不固定，不能用 IP 白名单）。

    加签算法按官方示例：sign = urlencode(base64(HmacSHA256(secret, f"{timestamp}\n{secret}")))
    """
    url = os.environ["DINGTALK_WEBHOOK"]
    secret = os.environ.get("DINGTALK_SECRET", "")
    if secret:
        ts = str(int(time.time() * 1000))
        string_to_sign = "%s\n%s" % (ts, secret)
        digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                          digestmod=hashlib.sha256).digest()
        sign = quote_plus(base64.b64encode(digest))
        sep = "&" if ("?" in url) else "?"
        url = "%s%stimestamp=%s&sign=%s" % (url, sep, ts, sign)
    payload = {"msgtype": "markdown", "markdown": {"title": subject, "text": text}}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode("utf-8"))
    if resp.get("errcode") != 0:
        raise RuntimeError("钉钉返回异常: %s" % resp)
    log("钉钉已推送")


def dispatch(subject, text, html_body, channel):
    if channel == "dingtalk":
        push_dingtalk(subject, text)
    elif channel == "email":
        push_email(subject, text, html_body)
    elif channel == "feishu":
        push_feishu(subject, text)
    elif channel == "serverchan":
        push_serverchan(subject, text)
    else:
        log("console 模式，不推送。预览如下：\n" + "-" * 60 + "\n" + text + "\n" + "-" * 60)


def alert(subject, err, channel):
    try:
        dispatch("❌ " + subject, "生成失败，错误信息：\n%s" % err, "", channel)
    except Exception as e:  # noqa: BLE001
        log("告警也失败了：%s" % e)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def wait_until(hhmm):
    h, m = [int(x) for x in hhmm.split(":")]
    now = datetime.now(CST)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        # 已经过了目标时刻（GitHub 排队延迟，或你手动改 cron 做测试）：
        # 一律立即发送，绝不「等明天」——否则一旦延迟就会静默漏推一整天。
        late = (now - target).total_seconds() / 60.0
        log("已过目标时刻 %.0f 分钟，立即发送（不等待、不顺延到明天）" % late)
        return
    delta = (target - datetime.now(CST)).total_seconds()
    log("等待 %.0f 秒，直到 %s (CST)" % (delta, target.strftime("%Y-%m-%d %H:%M")))
    while delta > 0:
        time.sleep(min(delta, 30))
        delta = (target - datetime.now(CST)).total_seconds()


def build(subject, rows, count, errors, date_str):
    return (subject, render_text(rows, count, errors, date_str),
            render_html(rows, count, errors, date_str))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", default="", help="等到北京时间这个时刻再推送，如 08:00")
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="不推送，只打印")
    ap.add_argument("--fake-llm", action="store_true", help="本地自检，不调用 DeepSeek")
    ap.add_argument("--check-feeds", action="store_true", help="只检查各源是否可用")
    ap.add_argument("--channel", default=os.environ.get("CHANNEL", "console"))
    ap.add_argument("--retries", type=int, default=3, help="生成阶段失败重试次数")
    args = ap.parse_args()

    date_str = today_str()

    if args.check_feeds:
        total = 0
        for f in FEEDS:
            if not f.get("enabled", True):
                continue
            try:
                n = len(parse_feed(http_get(f["url"], timeout=f.get("timeout", 20),
                                             retries=f.get("retries", 2))))
                total += 1
                log("OK   %-22s %3d 条  %s" % (f["name"], n, f["url"]))
            except Exception as e:  # noqa: BLE001
                log("FAIL %-22s %s  (%s)" % (f["name"], e, f["url"]))
        log("可用源 %d 个" % total)
        return

    if args.at and not args.no_wait and not args.dry_run:
        wait_until(args.at)

    subject = "AI 日报 · " + date_str
    try:
        rows, items, errors = None, [], []
        for attempt in range(1, args.retries + 1):
            try:
                items, errors = collect(FEEDS, WINDOW_HOURS)
                log("候选合计 %d 条，源错误 %d 个（第 %d/%d 次）"
                    % (len(items), len(errors), attempt, args.retries))
                if not items:
                    raise RuntimeError("所有源都没有取到内容，检查网络或源地址")
                candidates = [{
                    "id": i,
                    "标题": it["title"],
                    "来源": it["source"],
                    "源等级": "T%d" % it["tier"],
                    "时间": it["published"],
                    "摘要": it["summary"],
                } for i, it in enumerate(items)]
                if args.fake_llm:
                    result = fake_llm(items)
                else:
                    key = os.environ.get("DEEPSEEK_API_KEY")
                    if not key:
                        raise RuntimeError("缺少 DEEPSEEK_API_KEY")
                    result = call_deepseek(candidates, key,
                                           os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))
                rows = select(result, candidates, items)
                break
            except Exception as e:  # noqa: BLE001
                log("第 %d 次生成失败：%s" % (attempt, e))
                if attempt >= args.retries:
                    raise
                time.sleep(60)
        log("最终入选 %d 条" % len(rows))
        subject, text, html_body = build(subject, rows, len(items), errors, date_str)

        if args.dry_run:
            print("-" * 70)
            print(text)
            print("-" * 70)
            return
        dispatch(subject, text, html_body, args.channel)
        log("完成")
    except Exception as e:  # noqa: BLE001
        log("运行失败：%s" % e)
        if not args.dry_run:
            alert(subject, repr(e), args.channel)
        sys.exit(1)


if __name__ == "__main__":
    main()
