# -*- coding: utf-8 -*-
import argparse
import os
import re
from datetime import timezone

from feedgen.ext.base import BaseExtension
from feedgen.feed import FeedGenerator
from github import Github
from marko.ext.gfm import gfm as marko

PRIMARY_FEED_FILENAME = "rss.xml"
FEED_ICON_PATH = "static/icon.png"
RSS_SUMMARY_MAX_CHARS = 360
WEBFEEDS_NS = "http://webfeeds.org/rss/1.0"
BACKUP_DIR = "BACKUP"
SITE_BASE_URL = "https://wjy9902.github.io/ai-daily"

TOP_ISSUES_LABELS = ["Top"]
IGNORE_LABELS = ["Top", "TODO"]

MD_HEAD = """# 甲鱼AI日报

> 每日 AI 前沿技术情报，由 AI 辅助创作。内容可能存在错误，请以原始信息为准。

📡 RSS 订阅：{feed_subscribe_url}  
🌐 网站：{site_url}

## 最近更新

"""


def login(token):
    return Github(token)


def get_repo(user, repo_full_name):
    """repo_full_name: 'owner/repo'"""
    return user.get_repo(repo_full_name)


def get_me():
    """Get repo owner from env (always available in GitHub Actions)"""
    return (
        os.environ.get("GITHUB_REPOSITORY_OWNER")
        or os.environ.get("GITHUB_ACTOR")
        or "wjy9902"
    )


def is_me(issue, me):
    return issue.user.login == me


def format_time(time):
    """Convert UTC datetime to Asia/Shanghai date string"""
    from datetime import timedelta
    cst = time + timedelta(hours=8)
    return str(cst)[:10]


def add_md_header(filename):
    feed_subscribe_url = f"{SITE_BASE_URL}/{PRIMARY_FEED_FILENAME}"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(MD_HEAD.format(
            feed_subscribe_url=feed_subscribe_url,
            site_url=SITE_BASE_URL,
        ))


def add_md_recent(repo, filename, me, limit=10):
    count = 0
    with open(filename, "a", encoding="utf-8") as f:
        for issue in repo.get_issues(state="open", sort="created", direction="desc"):
            if not is_me(issue, me):
                continue
            labels = [l.name for l in issue.labels]
            if any(l in IGNORE_LABELS for l in labels):
                continue
            f.write(f"- [{issue.title}]({issue.html_url}) — {format_time(issue.created_at)}\n")
            count += 1
            if count >= limit:
                break


def add_md_top(repo, filename, me):
    issues = list(repo.get_issues(labels=TOP_ISSUES_LABELS, state="open"))
    if not issues:
        return
    with open(filename, "a", encoding="utf-8") as f:
        f.write("\n## 置顶\n\n")
        for issue in issues:
            if is_me(issue, me):
                f.write(f"- ⭐ [{issue.title}]({issue.html_url})\n")


def add_md_footer(filename):
    with open(filename, "a", encoding="utf-8") as f:
        f.write("\n---\n\nPowered by 🍗 鸡胸肉 | [甲鱼AI日报](https://wjy9902.github.io/ai-daily/)\n")


class WebfeedsExtension(BaseExtension):
    def extend_ns(self):
        return {"webfeeds": WEBFEEDS_NS}


class WebfeedsEntryExtension(BaseExtension):
    pass


def generate_rss_feed(repo, feed_filename, me):
    fg = FeedGenerator()
    fg.id(SITE_BASE_URL)
    fg.title("甲鱼AI日报")
    fg.subtitle("每日 AI 前沿技术情报")
    fg.link(href=SITE_BASE_URL, rel="alternate")
    fg.link(href=f"{SITE_BASE_URL}/{feed_filename}", rel="self")
    fg.language("zh-CN")

    count = 0
    for issue in repo.get_issues(state="open", sort="created", direction="desc"):
        if not is_me(issue, me):
            continue
        labels = [l.name for l in issue.labels]
        if any(l in IGNORE_LABELS for l in labels):
            continue

        body = issue.body or ""
        html_body = marko(body)
        summary = re.sub(r"<[^>]+>", "", html_body)[:RSS_SUMMARY_MAX_CHARS]

        fe = fg.add_entry()
        fe.id(issue.html_url)
        fe.title(issue.title)
        fe.link(href=issue.html_url)
        fe.published(issue.created_at.replace(tzinfo=timezone.utc))
        fe.updated(issue.updated_at.replace(tzinfo=timezone.utc))
        fe.summary(summary)
        fe.content(html_body, type="html")

        count += 1
        if count >= 30:
            break

    fg.rss_file(feed_filename, pretty=True)


def get_to_generate_issues(repo, me, issue_number=None):
    """Return issues that need to be saved to BACKUP/"""
    existing = set()
    if os.path.exists(BACKUP_DIR):
        for fname in os.listdir(BACKUP_DIR):
            parts = fname.split("_")
            if parts[0].isdigit():
                existing.add(int(parts[0]))

    to_generate = []
    if issue_number:
        issue = repo.get_issue(int(issue_number))
        if is_me(issue, me) and issue.number not in existing:
            to_generate.append(issue)
    else:
        for issue in repo.get_issues(state="open", sort="created", direction="desc"):
            if is_me(issue, me) and issue.number not in existing and not issue.pull_request:
                to_generate.append(issue)
    return to_generate


def save_issue(issue, me, dir_name=BACKUP_DIR):
    safe_title = issue.title.replace("/", "-").replace(" ", ".")
    md_name = os.path.join(dir_name, f"{issue.number}_{safe_title}.md")
    with open(md_name, "w", encoding="utf-8") as f:
        f.write(f"# [{issue.title}]({issue.html_url})\n\n")
        f.write(issue.body or "")


def main(token, repo_name, issue_number=None):
    user = login(token)
    me = get_me()
    repo = get_repo(user, repo_name)

    os.makedirs(BACKUP_DIR, exist_ok=True)

    add_md_header("README.md")
    add_md_recent(repo, "README.md", me)
    add_md_top(repo, "README.md", me)
    add_md_footer("README.md")

    generate_rss_feed(repo, PRIMARY_FEED_FILENAME, me)

    for issue in get_to_generate_issues(repo, me, issue_number):
        save_issue(issue, me, BACKUP_DIR)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("github_token", help="github_token")
    parser.add_argument("repo_name", help="repo_name (owner/repo)")
    parser.add_argument("--issue_number", help="issue_number", default=None, required=False)
    options = parser.parse_args()
    main(options.github_token, options.repo_name, options.issue_number)
