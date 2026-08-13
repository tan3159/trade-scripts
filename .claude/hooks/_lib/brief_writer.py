"""Stop hook の `cache/brief.md` 生成ロジック（Issue #1175・#2967）.

`.claude/hooks/on-stop.py` に同居していた brief 生成ロジックを移設したもの。
`/brief` スキルから参照される前回セッション状況のソースを書き出す。

stdlib のみ使用。git/gh subprocess 実行は ``_lib/git_helpers.py`` に委譲する。
"""

from __future__ import annotations

import datetime as _dt
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from git_helpers import run_gh as _gh  # type: ignore[import-not-found]
from git_helpers import run_git_in_repo as _git  # type: ignore[import-not-found]


def write_brief(repo: str, brief_path: Path) -> None:
    """Issue #1175: 独立した git / gh 呼び出しを ThreadPoolExecutor で並列実行する.

    ``repo view -> pr list`` のみ順序依存で、それ以外は完全に独立。
    """
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    now = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M")

    issue_high_args = (
        "issue",
        "list",
        "--label",
        "priority: high",
        "--label",
        "priority: critical",
        "--state",
        "open",
        "--json",
        "number,title",
        "--template",
        '{{range .}}- #{{.number}} {{.title}}{{"\\n"}}{{end}}',
    )
    issue_all_args = (
        "issue",
        "list",
        "--state",
        "open",
        "--json",
        "number,title,labels",
        "--template",
        '{{range .}}- #{{.number}} {{.title}}{{"\\n"}}{{end}}',
    )

    with ThreadPoolExecutor(max_workers=5, thread_name_prefix="brief") as ex:
        f_branch = ex.submit(_git, repo, "branch", "--show-current")
        f_log = ex.submit(_git, repo, "log", "--oneline", "-5")
        f_repo_nwo = ex.submit(
            _gh, "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"
        )
        f_issue_high = ex.submit(_gh, *issue_high_args)
        f_issue_all = ex.submit(_gh, *issue_all_args)

        rc_nwo, repo_nwo_raw = f_repo_nwo.result()
        repo_nwo = repo_nwo_raw.strip() if rc_nwo == 0 else ""
        pr_args = ["pr", "list"]
        if repo_nwo:
            pr_args.extend(["--repo", repo_nwo])
        pr_args.extend(
            [
                "--state",
                "open",
                "--json",
                "number,title,headRefName",
                "--template",
                '{{range .}}- #{{.number}} {{.title}} ({{.headRefName}}){{"\\n"}}{{end}}',
            ]
        )
        f_pr = ex.submit(_gh, *pr_args)

        rc_branch, out_branch, _ = f_branch.result()
        rc_log, out_log, _ = f_log.result()
        rc_pr, out_pr = f_pr.result()
        rc_ih, out_ih = f_issue_high.result()
        rc_ia, out_ia = f_issue_all.result()

    lines: list[str] = []
    lines.append(f"# Brief — {now}")
    lines.append("")
    lines.append("## ブランチ")
    lines.append(out_branch.strip() if rc_branch == 0 else "")
    lines.append("")
    lines.append("## 直近のコミット")
    lines.append(out_log.rstrip() if rc_log == 0 else "")
    lines.append("")

    lines.append("## Open PR")
    lines.append(out_pr.rstrip() if rc_pr == 0 and out_pr.strip() else "（なし）")
    lines.append("")

    lines.append("## Open Issue（priority: high/critical）")
    lines.append(out_ih.rstrip() if rc_ih == 0 and out_ih.strip() else "（なし）")
    lines.append("")

    lines.append("## Open Issue（全件）")
    lines.append(out_ia.rstrip() if rc_ia == 0 and out_ia.strip() else "（なし）")

    brief_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
