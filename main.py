# -*- coding: utf-8 -*-
import argparse
import html
import os
import re

import markdown
from feedgen.ext.base import BaseExtension
from feedgen.feed import FeedGenerator
from github import Github
from lxml import html as lxml_html
from lxml import etree as lxml_etree
from lxml.etree import tostring
from marko.ext.gfm import gfm as marko

PRIMARY_FEED_FILENAME = "rss.xml"
FEED_ICON_PATH = "static/icon.png"
FEED_ICON_SIZE = 144
RSS_SUMMARY_MAX_CHARS = 360
WEBFEEDS_NS = "http://webfeeds.org/rss/1.0"

MD_HEAD = """# 甲鱼AI日报

> AI 前沿技术情报，每日自动生成。内容由 AI 辅助创作，可能存在错误，请以原始信息为准。

RSS 订阅：https://wjy9902.github.io/ai-daily/rss.xml

## Links

| Platform | Link |
| :--- | :--- |
| RSS Feed | [Subscribe]({feed_subscribe_url}) |
| Markdown 备份 | [BACKUP](https://github.com/{repo_name}/tree/{branch_name}/BACKUP) |
| GitHub Pages | [View](https://wjy9902.github.io/ai-daily/) |

---

"""

BACKUP_DIR = "BACKUP"
ANCHOR_NUMBER = 5
TOP_ISSUES_LABELS = ["Top"]
TODO_ISSUES_LABELS = ["TODO"]
IGNORE_LABELS = TOP_ISSUES_LABELS + TODO_ISSUES_LABELS


def get_me(user):
    try:
        return user.get_user().login
    except Exception:
        # Fallback: use GITHUB_REPOSITORY_OWNER env var
        owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
        if owner:
            return owner
        raise


def get_me_from_repo(repo):
    return repo.owner.login


def is_me(issue, me):
    return issue.user.login == me


def format_time(time):
    return str(time)[:10]


def login(token):
    return Github(token)


def get_repo(user, repo):
    return user.get_repo(repo)


def parse_me(issues):
    for issue in issues:
        return issue.user.login


def get_to_generate_issues(repo, dir_name, me, issue_number=None):
    to_generate_issues = []
    if issue_number:
        issue = repo.get_issue(int(issue_number))
        if is_me(issue, me):
            to_generate_issues.append(issue)
    else:
        for issue in repo.get_issues(state="open"):
            if is_me(issue, me):
                to_generate_issues.append(issue)
    return to_generate_issues


def add_md_header(filename, repo_name, feed_filename, branch_name="master"):
    base_url = f"https://wjy9902.github.io/ai-daily"
    feed_subscribe_url = f"{base_url}/{feed_filename}"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(
            MD_HEAD.format(
                feed_subscribe_url=feed_subscribe_url,
                repo_name=repo_name,
                branch_name=branch_name,
            )
        )


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
    with open(filename, "a", encoding="utf-8") as f:
        for issue in repo.get_issues(labels=TOP_ISSUES_LABELS, state="open"):
            if not is_me(issue, me):
                continue
            f.write(f"- :star: [{issue.title}]({issue.html_url})\n")


def add_md_footer(filename):
    with open(filename, "a", encoding="utf-8") as f:
        f.write("\n---\n\n")
        f.write("Powered by [甲鱼AI日报](https://wjy9902.github.io/ai-daily/) · Generated with 🍗\n")


class WebfeedsExtension(BaseExtension):
    def extend_ns(self):
        return {"webfeeds": WEBFEEDS_NS}


class WebfeedsEntryExtension(BaseExtension):
    pass


def generate_rss_feed(repo, feed_filename, me):
    base_url = "https://wjy9902.github.io/ai-daily"
    fg = FeedGenerator()
    fg.register_extension("webfeeds", WebfeedsExtension, WebfeedsEntryExtension)
    fg.id(base_url)
    fg.title("甲鱼AI日报")
    fg.subtitle("每日 AI 前沿技术情报")
    fg.link(href=base_url, rel="alternate")
    fg.link(href=f"{base_url}/{feed_filename}", rel="self")
    fg.language("zh-CN")
    fg.logo(f"{base_url}/{FEED_ICON_PATH}")

    icon_elem = lxml_etree.SubElement(
        fg.rss_file(pretty=True), f"{{{WEBFEEDS_NS}}}icon"
    )
    icon_elem.text = f"{base_url}/{FEED_ICON_PATH}"

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
        fe.published(issue.created_at)
        fe.updated(issue.updated_at)
        fe.summary(summary)
        fe.content(html_body, type="html")

        count += 1
        if count >= 30:
            break

    fg.rss_file(feed_filename, pretty=True)


def main(token, repo_name, issue_number=None):
    user = login(token)
    me = get_me(user)
    repo = get_repo(user, repo_name.split("/")[-1] if "/" in repo_name else repo_name)
    repo = user.get_repo(repo_name)

    default_branch = repo.default_branch

    add_md_header("README.md", repo_name, PRIMARY_FEED_FILENAME, default_branch)
    add_md_top(repo, "README.md", me)
    add_md_recent(repo, "README.md", me)
    add_md_footer("README.md")

    generate_rss_feed(repo, PRIMARY_FEED_FILENAME, me)
    to_generate_issues = get_to_generate_issues(repo, BACKUP_DIR, me, issue_number)

    for issue in to_generate_issues:
        save_issue(issue, me, BACKUP_DIR)


def save_issue(issue, me, dir_name=BACKUP_DIR):
    md_name = os.path.join(
        dir_name, f"{issue.number}_{issue.title.replace('/', '-').replace(' ', '.')}.md"
    )
    with open(md_name, "w", encoding="utf-8") as f:
        f.write(f"# [{issue.title}]({issue.html_url})\n\n")
        f.write(issue.body or "")
        if issue.comments:
            for c in issue.get_comments():
                if is_me(c, me):
                    f.write("\n\n---\n\n")
                    f.write(c.body or "")


if __name__ == "__main__":
    if not os.path.exists(BACKUP_DIR):
        os.mkdir(BACKUP_DIR)
    parser = argparse.ArgumentParser()
    parser.add_argument("github_token", help="github_token")
    parser.add_argument("repo_name", help="repo_name")
    parser.add_argument(
        "--issue_number", help="issue_number", default=None, required=False
    )
    options = parser.parse_args()
    main(options.github_token, options.repo_name, options.issue_number)
