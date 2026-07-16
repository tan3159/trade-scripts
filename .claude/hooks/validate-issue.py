#!/usr/bin/env python3
"""PreToolUse hook: gh issue create / gh issue edit の本文・ラベルを機械チェックする.

旧 validate-issue.sh の Python embed を抜き出して直接 .py 化した（Phase 4 / #1057）。
振る舞いは 1:1 で同一。stdlib のみ使用。

.claude/rules/issue-creation.md のチェック項目（フォーマット・ラベル）を検証する。
不備があれば exit 2 でブロックし、理由を stderr に出力する。

設計:
  - Python 単体で JSON 解析からバリデーションまで完結
  - shlex.split のパースエラー時は exit 2 でブロック（バイパス不可）
  - --body-file の読み込みはサイズ上限（1MB）を設ける
  - gh issue create のサブコマンド開始位置を特定し、前段コマンドの誤反応を防ぐ
  - #1581: 🙋 needs-human-input ラベル付与時に「## 判断してほしいこと」セクションを必須化
    - gh issue create: 本文（--body / --body-file）から検証
    - gh issue edit --add-label: gh issue view <N> で本文を取得して検証（gh/ネットワーク失敗時のみフェイルオープン）
  - #1627: type: fix には source: ラベル（5 分類のいずれか）を必須化
    - source: ci / source: rework / source: human-report / source: new-bug / source: spec-change
  - #2072: タイトルの 🤖 prefix は任意化（無 prefix を標準とし、既存の 🤖 prefix も後方互換で受理する）
"""

from __future__ import annotations

import json  # tidd issue-quality-check の JSON stdout を parse するため
import os
import re
import shlex
import shutil  # `tidd` バイナリの PATH 解決
import subprocess  # tidd issue-quality-check サブプロセス呼び出し
import sys
from pathlib import Path

BODY_FILE_SIZE_LIMIT = 1024 * 1024  # 1MB
QUALITY_CHECK_TIMEOUT_SEC = 15  # tidd 側の 10s + subprocess オーバーヘッド

VALID_TYPES_RE = r"feat|fix|docs|refactor|build|ci|research"
TYPE_LABEL_RE = re.compile(rf"^type: ({VALID_TYPES_RE})$")
PRIORITY_LABEL_RE = re.compile(r"^priority: (critical|high|medium|low)$")
# Issue #2072: 🤖 prefix は任意化（無 prefix を標準とし、既存の 🤖 prefix も後方互換で受理する）
TITLE_TYPE_RE = re.compile(rf"^(?:🤖 )?({VALID_TYPES_RE}): .+")
FEAT_TITLE_RE = re.compile(r"^(?:🤖 )?feat: ")
FEAT_FIX_TITLE_RE = re.compile(r"^(?:🤖 )?(feat|fix): ")
SOURCE_LABELS = frozenset(
    {
        "source: ci",
        "source: rework",
        "source: human-report",
        "source: new-bug",
        "source: spec-change",
    }
)
TODO_PLAIN_BULLET_RE = re.compile(r"^- (?!\[)")
# Issue #1328: 見出し検知は行頭アンカー `^` で判定する（inline code 内の文字列を除外）。
# 見出しに `（feat系必須）` 等のアノテーションが続くケースを許容するため末尾アンカーは付けない。
TODO_SECTION_RE = re.compile(
    r"^##\s*やること[^\n]*\n?(.*?)(?=^##|\Z)", re.MULTILINE | re.DOTALL
)
# Section presence check: 行頭 `## <name>` から始まるセクションを検知する（inline code は除外）。
# body 抽出側（TODO_SECTION_RE・BEHAVIOR_SECTION_BODY_RE）と `##` 直後の空白許容度を揃える。
BACKGROUND_SECTION_RE = re.compile(r"^##\s*背景", re.MULTILINE)
TODO_SECTION_HEADER_RE = re.compile(r"^##\s*やること", re.MULTILINE)
DESIGN_SECTION_RE = re.compile(r"^##\s*設計の選択肢", re.MULTILINE)
BEHAVIOR_SECTION_HEADER_RE = re.compile(r"^##\s*振る舞い", re.MULTILINE)
BEHAVIOR_SECTION_BODY_RE = re.compile(
    r"^##\s*振る舞い[^\n]*\n?(.*?)(?=^##|\Z)", re.MULTILINE | re.DOTALL
)
# Issue #1581: 🙋 needs-human-input ラベル付与時の「## 判断してほしいこと」セクション必須化
DECISION_SECTION_RE = re.compile(r"^##\s*判断してほしいこと", re.MULTILINE)
# Issue #1855: hook 契約系 Issue の Gherkin 除外判定
# `## やること` に記載されたファイルパス（`.claude/hooks/` を含む行）を抽出するパターン
_TODO_FILE_PATH_RE = re.compile(r"\.claude/hooks/[\w./\-]+")
# `/` を含む任意のパス（.github/・.circleci/ 等も含む）を広く検出する
_TODO_ANY_PATH_RE = re.compile(r"[\w.][\w./\-]*/[\w./\-]+")
NEEDS_HUMAN_INPUT_LABEL = "🙋 needs-human-input"
EQ_OPT_RE = re.compile(r"^(--[\w-]+)=(.*)$", re.DOTALL)
GH_FRAG_RE = re.compile(r"^\s*gh\s+issue\s+create\b")
# Issue #1581: gh issue edit --add-label "🙋 needs-human-input" も検知する
GH_EDIT_FRAG_RE = re.compile(r"^\s*gh\s+issue\s+edit\b")
GH_COMMAND_GUARD_RE = re.compile(
    r"(?:^|&&|\|\||\||;|\n)[ \t]*gh[ \t]+issue[ \t]+(create|edit)\b"
)
GH_VIEW_TIMEOUT_SEC = 5  # gh issue view のタイムアウト（フェイルオープン用）

SHELL_OPS = {"&&", "||", ";", "|"}

# Issue #1578: --body "$(cat <<'EOF'\n...\nEOF\n)" パターンの heredoc body を直接抽出する。
# shlex.split は $(cat ...) の内部にある二重引用符を区切り文字と誤解釈して body を truncate する。
# この regex でコマンド置換ブロック全体を先行抽出することで shlex 依存を回避する。
#
# パターン: --body "$(cat <<'?(\w+)'?\s*\n<content>\n<marker>\n?)"
#   - <<'EOF' / <<EOF の両形式に対応（single-quote optional）
#   - heredoc terminator は任意の単語（EOF, BODY, ISSUE_BODY 等）
#   - re.DOTALL で複数行にわたる body をキャプチャ
_HEREDOC_BODY_RE = re.compile(
    r"""(?:--body|-b)\s+"?\$\(cat\s+<<['"]{0,1}(\w+)['"]{0,1}\s*\n(.*?)\n\1\s*\n?\s*\)"?""",
    re.DOTALL,
)


def _extract_heredoc_body(fragment: str) -> "str | None":
    """--body "$(cat <<'EOF'\n...\nEOF\n)" パターンから heredoc body を直接抽出する.

    shlex.split は $(cat ...) の内側を解釈しないため、heredoc body 内の二重引用符で
    --body トークンが途中打ち切りされる (#1578)。このルーチンで先行抽出することで
    shlex 依存を回避する。

    Returns:
        heredoc body 文字列（マッチしない場合は None）。
    """
    m = _HEREDOC_BODY_RE.search(fragment)
    if m is None:
        return None
    return m.group(2)


def _split_shell_fragments(s: str) -> list[str]:
    """クォートを考慮してシェル演算子（&&, ||, |, ;, 改行）で分割する."""
    fragments: list[str] = []
    current: list[str] = []
    quote_char: str | None = None
    i = 0
    while i < len(s):
        c = s[i]
        if quote_char:
            # ダブルクォート内のバックスラッシュエスケープ（`\"` 等）を保護する。
            # シングルクォート内では `\` は literal なのでエスケープしない。
            if quote_char == '"' and c == "\\" and i + 1 < len(s):
                current.append(c)
                current.append(s[i + 1])
                i += 2
                continue
            if c == quote_char:
                quote_char = None
            current.append(c)
            i += 1
        elif c in ('"', "'"):
            quote_char = c
            current.append(c)
            i += 1
        elif c == "\\" and i + 1 < len(s):
            current.append(c)
            current.append(s[i + 1])
            i += 2
        elif s[i : i + 2] in ("&&", "||"):
            fragments.append("".join(current))
            current = []
            i += 2
        elif c in ("|", ";", "\n"):
            fragments.append("".join(current))
            current = []
            i += 1
        else:
            current.append(c)
            i += 1
    fragments.append("".join(current))
    return fragments


def _validate_gh_fragment(gh_fragment: str) -> list[str]:
    """gh issue create フラグメントを検証してエラーリストを返す.

    shlex パースエラーは即 exit 2（バイパス不可）。

    Issue #1578: --body "$(cat <<'EOF'\n...\nEOF\n)" パターンは shlex.split の前に
    _extract_heredoc_body() で body を直接抽出する。heredoc body 内の二重引用符が
    shlex の区切り文字と誤解釈されて body が truncate されるバグを回避する。
    """
    # Issue #1578: heredoc パターンの body を shlex より先に正規表現で抽出する。
    # shlex は $(cat <<'EOF'...) の内部を解釈しないため、body 内の二重引用符で
    # --body トークンが途中打ち切りされる。
    heredoc_body = _extract_heredoc_body(gh_fragment)

    try:
        tokens = shlex.split(gh_fragment, posix=True)
    except ValueError as e:
        sys.stderr.write(f"Blocked: コマンドのパースに失敗しました: {e}\n")
        sys.stderr.write("クォートや特殊文字を確認して再実行してください。\n")
        sys.exit(2)

    start = None
    for idx in range(len(tokens) - 2):
        if (
            tokens[idx] == "gh"
            and tokens[idx + 1] == "issue"
            and tokens[idx + 2] == "create"
        ):
            start = idx + 3
            break
    if start is None:
        return []

    title = ""
    body = ""
    body_file = ""
    labels: list[str] = []

    i = start
    while i < len(tokens):
        t = tokens[i]
        if t in SHELL_OPS:
            break
        m = EQ_OPT_RE.match(t)
        if m:
            key, val = m.group(1), m.group(2)
        else:
            key, val = t, None

        if key in ("--title", "-t"):
            if val is not None:
                title = val
            elif i + 1 < len(tokens) and tokens[i + 1] not in SHELL_OPS:
                title = tokens[i + 1]
                i += 1
        elif key in ("--body", "-b"):
            if heredoc_body is not None:
                # Issue #1578: heredoc body は shlex ではなく正規表現で抽出済み。
                # shlex が返す truncated トークンではなく確実な heredoc_body を使う。
                body = heredoc_body
                # shlex のトークンを読み飛ばす（truncate された値のため使わない）。
                if (
                    val is None
                    and i + 1 < len(tokens)
                    and tokens[i + 1] not in SHELL_OPS
                ):
                    i += 1  # 壊れたトークンを消費して次へ
            else:
                if val is not None:
                    body = val
                elif i + 1 < len(tokens) and tokens[i + 1] not in SHELL_OPS:
                    body = tokens[i + 1]
                    i += 1
        elif key in ("--body-file", "-F"):
            if val is not None:
                body_file = val
            elif i + 1 < len(tokens) and tokens[i + 1] not in SHELL_OPS:
                body_file = tokens[i + 1]
                i += 1
        elif key in ("--label", "-l"):
            raw_lbl: str | None = val
            if raw_lbl is None:
                if i + 1 < len(tokens) and tokens[i + 1] not in SHELL_OPS:
                    raw_lbl = tokens[i + 1]
                    i += 1
                else:
                    raw_lbl = ""
            for lbl in raw_lbl.split(","):
                lbl = lbl.strip()
                if lbl:
                    labels.append(lbl)
        i += 1

    # --body-file 読み込み（サイズ上限あり）
    if body_file and not body:
        try:
            file_size = os.path.getsize(body_file)
            if file_size > BODY_FILE_SIZE_LIMIT:
                sys.stderr.write(
                    f"Blocked: --body-file のサイズが上限（1MB）を超えています: {file_size} バイト\n"
                )
                sys.stderr.write("Issue の本文を短くして再実行してください。\n")
                sys.exit(2)
            with open(body_file, "r", encoding="utf-8") as f:
                body = f.read()
        except OSError as e:
            sys.stderr.write(f"Blocked: --body-file の読み込みに失敗しました: {e}\n")
            sys.stderr.write("ファイルパスを確認して再実行してください。\n")
            sys.exit(2)

    errors: list[str] = []

    # タイトル
    if title:
        if not TITLE_TYPE_RE.match(title):
            errors.append(
                f'タイトル形式不正: "{title}" — "<type>: <説明>" '
                '形式にしてください（例: "feat: ～を実装する"。スコープ不可。'
                "type は feat/fix/docs/refactor/build/ci/research のいずれか）"
            )
    else:
        errors.append(
            "タイトルが取得できませんでした。--title フラグを確認してください"
        )

    # 本文セクション（Issue #1328: 行頭アンカーで inline code 内の言及を除外）
    if not BACKGROUND_SECTION_RE.search(body):
        errors.append('本文に "## 背景" セクションがありません')
    if not TODO_SECTION_HEADER_RE.search(body):
        errors.append('本文に "## やること" セクションがありません')

    if FEAT_TITLE_RE.match(title or ""):
        if not DESIGN_SECTION_RE.search(body):
            errors.append(
                'feat 系 Issue は "## 設計の選択肢" セクションが必要です'
                "（採用案・不採用案を明記してください）"
            )

    if FEAT_FIX_TITLE_RE.match(title or ""):
        # Issue #1855: hook 契約系 Issue（やることが .claude/hooks/ のみ）は Gherkin 不要
        if not _is_hook_contract_only_issue(
            body
        ) and not BEHAVIOR_SECTION_HEADER_RE.search(body):
            errors.append(
                "feat/fix 系 Issue は ## 振る舞い セクションが必要です。\n"
                "  Gherkin（ゲルキン）とは「Given（前提条件）/ When（操作）/ Then（期待結果）」の\n"
                "  3 ステップで振る舞いを記述する、自然言語に近いテスト記述形式です。\n\n"
                "  記述例:\n"
                "  ## 振る舞い\n\n"
                "  Feature: 機能名\n\n"
                "    Scenario: 正常系の説明\n"
                "      Given 前提条件（例: ファイルが存在する）\n"
                "      When 操作（例: コマンドを実行する）\n"
                "      Then 期待結果（例: exit code 0 で終了する）\n\n"
                "  例外: やることが .claude/hooks/ のみの hook 契約系 Issue は Gherkin 不要。\n"
                "  代わりに test_*.py 契約テストを書いてください。\n"
                "  詳細: docs/reference/hooks.md#validate-issuepy"
            )

    # やること チェックボックス形式
    m_todo = TODO_SECTION_RE.search(body)
    if m_todo:
        todo_section = m_todo.group(1)
        for line in todo_section.split("\n"):
            stripped = line.strip()
            if TODO_PLAIN_BULLET_RE.match(stripped):
                errors.append(
                    "「## やること」の項目は - [ ] 形式（チェックボックス）で書いてください"
                    "（例: `- [ ] タスク名`）。平箇条書き（`- タスク名`）は使用できません"
                )
                break

    # ラベル
    has_type_label = any(TYPE_LABEL_RE.match(lbl) for lbl in labels)
    has_priority_label = any(PRIORITY_LABEL_RE.match(lbl) for lbl in labels)
    if not has_type_label:
        errors.append(
            '"type:" ラベルがありません: type: feat / type: fix / type: docs / '
            "type: refactor / type: build / type: ci / type: research のいずれかを "
            "--label で指定してください"
        )
    if not has_priority_label:
        errors.append(
            '"priority:" ラベルがありません: priority: critical / priority: high / '
            "priority: medium / priority: low のいずれかを --label で指定してください"
        )

    # Issue #1627: type: fix の source: ラベル強制
    # Issue #2072: タイトル文字列ではなく "type: fix" ラベルの有無で判定する
    # （🤖 の有無は無関係。タイトルとラベルが一致しないケース（例: type: fix ラベル + docs: 始まりの
    # タイトル）でも source: 強制が漏れないようにする）。
    # body の有無に依存しない（境界値: 空 body でも動作する）。
    if "type: fix" in labels:
        has_source_label = any(lbl in SOURCE_LABELS for lbl in labels)
        if not has_source_label:
            errors.append(
                '"source:" ラベルがありません: fix Issue には起因分類として以下の 5 種から 1 つを '
                "--label で指定してください:\n"
                "  source: ci          — CI 自動検知（bats 定期実行・CircleCI nightly 等）\n"
                "  source: rework      — AI 実装の手戻り（廃止対応漏れ・設計漏れ・プロンプト不備等）\n"
                "  source: human-report — 運用中に人間が観察・体験して起票\n"
                "  source: new-bug     — 外部要因（証明書期限・ツール変更・依存更新）や初期設定ミス\n"
                "  source: spec-change — セキュリティ要件・設計見直し・ポリシー変更に起因\n"
                "  詳細: .claude/rules/issue-creation.md"
            )

    # Issue #1581: 🙋 needs-human-input ラベル付与時に「## 判断してほしいこと」セクション必須化
    has_needs_human_input = any(lbl == NEEDS_HUMAN_INPUT_LABEL for lbl in labels)
    if has_needs_human_input and body and not DECISION_SECTION_RE.search(body):
        errors.append(
            "🙋 needs-human-input ラベルを付与するには「## 判断してほしいこと」セクションが必要です。\n"
            "  本文に以下の形式でセクションを追加してください:\n\n"
            "  ## 判断してほしいこと\n\n"
            "  <状況の 1 文説明>\n\n"
            "  A. <選択肢 1> — <トレードオフ>（推奨）\n"
            "  B. <選択肢 2> — <トレードオフ>\n\n"
            "  判断できなければ → A\n\n"
            "  書式詳細: .claude/rules/escalation-format.md"
        )

    # Issue #1457: gherkin-forbidden-words.yaml 静的スキャン（Then 句のみ対象）
    # 「禁止語 '...' が Then 句に含まれています」形式のメッセージで報告するため、
    # 意味的に重複する positive markers denylist より先に評価する。
    # Issue #1855: hook 契約系 Issue は Gherkin チェックをスキップ
    if (
        not errors
        and body
        and FEAT_FIX_TITLE_RE.match(title or "")
        and not _is_hook_contract_only_issue(body)
    ):
        forbidden_errors = _check_gherkin_forbidden_words(body)
        errors.extend(forbidden_errors)

    # Issue #1305: Gherkin 必須観測要素 positive list チェック（静的規則）
    # Issue #1855: hook 契約系 Issue は Gherkin チェックをスキップ
    if (
        not errors
        and body
        and FEAT_FIX_TITLE_RE.match(title or "")
        and not _is_hook_contract_only_issue(body)
    ):
        gherkin_errors = _check_gherkin_positive_markers(body)
        errors.extend(gherkin_errors)

    # 意味的品質チェック（Pain・Gherkin）を tidd issue-quality-check にサブプロセスで委譲
    if not errors and body:
        issue_type = _extract_type_from_title(title)
        quality_errors = _run_issue_quality_check(body, issue_type)
        errors.extend(quality_errors)

    return errors


def _validate_gh_edit_fragment(gh_edit_fragment: str) -> list[str]:
    """gh issue edit フラグメントを検証してエラーリストを返す（Issue #1581）.

    🙋 needs-human-input ラベルが --add-label に含まれる場合のみ、
    本文に ## 判断してほしいこと セクションがあるかを確認する。

    本文の取得優先順位（hook は実行前に走るため、edit コマンドが指定する本文が最新）:
    1. --body <text> が指定されている場合: その値を使用
    2. --body-file <path> が指定されている場合: ファイルを読んで使用
    3. どちらもない場合: gh issue view <N> --json body で現在の本文を取得

    フェイルオープン設計（以下の場合は exit 0）:
    - gh が PATH にない
    - gh issue view がタイムアウト / エラー
    - Issue 番号が取得できない
    - --body-file が読めない場合
    """
    try:
        tokens = shlex.split(gh_edit_fragment, posix=True)
    except ValueError:
        return []  # パースエラーはフェイルオープン

    # トークン解析: gh issue edit <N> [--add-label <lbl>] [--body <text>] [--body-file <path>] ...
    start = None
    for idx in range(len(tokens) - 2):
        if (
            tokens[idx] == "gh"
            and tokens[idx + 1] == "issue"
            and tokens[idx + 2] == "edit"
        ):
            start = idx + 3
            break
    if start is None:
        return []

    issue_num = ""
    add_labels: list[str] = []
    body_inline = ""
    body_file = ""

    i = start
    while i < len(tokens):
        t = tokens[i]
        if t in SHELL_OPS:
            break
        m = EQ_OPT_RE.match(t)
        if m:
            key, val = m.group(1), m.group(2)
        else:
            key, val = t, None

        if key in ("--add-label",):
            raw_lbl: str | None = val
            if raw_lbl is None:
                if i + 1 < len(tokens) and tokens[i + 1] not in SHELL_OPS:
                    raw_lbl = tokens[i + 1]
                    i += 1
                else:
                    raw_lbl = ""
            for lbl in raw_lbl.split(","):
                lbl = lbl.strip()
                if lbl:
                    add_labels.append(lbl)
        elif key in ("--body", "-b"):
            if val is not None:
                body_inline = val
            elif i + 1 < len(tokens) and tokens[i + 1] not in SHELL_OPS:
                body_inline = tokens[i + 1]
                i += 1
        elif key in ("--body-file", "-F"):
            if val is not None:
                body_file = val
            elif i + 1 < len(tokens) and tokens[i + 1] not in SHELL_OPS:
                body_file = tokens[i + 1]
                i += 1
        elif not key.startswith("--") and not key.startswith("-") and not issue_num:
            # 最初の非オプション引数が Issue 番号
            issue_num = key.lstrip("#")
        i += 1

    # needs-human-input ラベルが --add-label に含まれない場合はチェック不要
    has_needs_human_input = any(lbl == NEEDS_HUMAN_INPUT_LABEL for lbl in add_labels)
    if not has_needs_human_input:
        return []

    # 本文を取得（優先順位: --body > --body-file > gh issue view）
    body = ""
    if body_inline:
        body = body_inline
    elif body_file:
        try:
            file_size = os.path.getsize(body_file)
            if file_size <= BODY_FILE_SIZE_LIMIT:
                with open(body_file, "r", encoding="utf-8") as f:
                    body = f.read()
        except OSError:
            return []  # body-file 読み込み失敗はフェイルオープン
    else:
        # --body / --body-file がない場合: gh issue view で現在の本文を取得
        if not issue_num or not issue_num.isdigit():
            return []  # Issue 番号がなければフェイルオープン

        gh_bin = shutil.which("gh")
        if gh_bin is None:
            return []  # gh が PATH にない場合はスキップ

        try:
            proc = subprocess.run(
                [gh_bin, "issue", "view", issue_num, "--json", "body"],
                capture_output=True,
                text=True,
                check=False,
                timeout=GH_VIEW_TIMEOUT_SEC,
            )
        except (subprocess.TimeoutExpired, OSError):
            return []  # タイムアウト / エラーはフェイルオープン

        if proc.returncode != 0:
            return []  # gh issue view 失敗はフェイルオープン

        try:
            view_data = json.loads(proc.stdout or "{}")
            body = view_data.get("body") or ""
        except (json.JSONDecodeError, AttributeError):
            return []

    if not body or not DECISION_SECTION_RE.search(body):
        return [
            "🙋 needs-human-input ラベルを付与するには「## 判断してほしいこと」セクションが必要です。\n"
            "  本文に以下の形式でセクションを追加してください:\n\n"
            "  ## 判断してほしいこと\n\n"
            "  <状況の 1 文説明>\n\n"
            "  A. <選択肢 1> — <トレードオフ>（推奨）\n"
            "  B. <選択肢 2> — <トレードオフ>\n\n"
            "  判断できなければ → A\n\n"
            "  書式詳細: .claude/rules/escalation-format.md"
        ]

    return []


def _is_hook_contract_only_issue(body: str) -> bool:
    """Issue #1855: hook 契約系 Issue かどうかを判定する.

    `## やること` に記載されたファイルパスが `.claude/hooks/` のみを含み、
    他のパス（projects/, docs/, .github/, .circleci/ 等）が混在しない場合に True を返す。
    ファイルパスの記述が一切ない Issue は False（汎用 Issue として除外しない）。
    """
    m_todo = TODO_SECTION_RE.search(body)
    if not m_todo:
        return False
    todo_section = m_todo.group(1)
    all_paths = _TODO_ANY_PATH_RE.findall(todo_section)
    if not all_paths:
        return False
    has_hook_path = any(_TODO_FILE_PATH_RE.search(p) for p in all_paths)
    all_are_hook_paths = all(_TODO_FILE_PATH_RE.search(p) for p in all_paths)
    return has_hook_path and all_are_hook_paths


def _load_gherkin_markers_yaml() -> tuple[list[re.Pattern[str]], list[str], list[str]]:
    """`.claude/rules/gherkin-required-markers.yaml` を読んで regex を返す.

    stdlib のみで簡易 YAML パース（regex / description / example の list of dict）を行う。
    完全 YAML は使わずに必要な部分だけ regex で抽出する（依存ゼロ）。

    Returns:
        (markers, denylist, marker_examples) のタプル
    """
    yaml_path = _find_gherkin_markers_yaml()
    if not yaml_path or not yaml_path.is_file():
        return [], [], []
    try:
        text = yaml_path.read_text(encoding="utf-8")
    except OSError:
        return [], [], []

    markers: list[re.Pattern[str]] = []
    marker_examples: list[str] = []
    denylist: list[str] = []

    for match in re.finditer(
        r"^\s*-\s+id:\s*\w+\s*\n\s+regex:\s*'([^']+)'\s*\n\s+description:\s*'([^']*)'\s*\n\s+example:\s*'([^']*)'",
        text,
        re.MULTILINE,
    ):
        raw_regex = match.group(1)
        try:
            markers.append(re.compile(raw_regex, re.MULTILINE))
            marker_examples.append(f"- {match.group(2)}: {match.group(3)}")
        except re.error:
            continue

    for match in re.finditer(r"^\s*-\s*'([^']+)'\s*$", text, re.MULTILINE):
        # denylist セクション内のパターンだけ拾う
        denylist.append(match.group(1))

    return markers, denylist, marker_examples


def _find_gherkin_markers_yaml() -> "Path | None":
    """カレントディレクトリから遡って `.claude/rules/gherkin-required-markers.yaml` を探す."""
    here = Path.cwd().resolve()
    for parent in (here, *here.parents):
        candidate = parent / ".claude" / "rules" / "gherkin-required-markers.yaml"
        if candidate.is_file():
            return candidate
    # fallback: hook 自身の場所から探す（`.claude/hooks/validate-issue.py`）
    # hook_dir = `.claude/hooks/`, その親が `.claude/`, 兄弟の `rules/` を見る
    hook_dir = Path(__file__).resolve().parent
    candidate = hook_dir.parent / "rules" / "gherkin-required-markers.yaml"
    return candidate if candidate.is_file() else None


def _find_gherkin_forbidden_yaml() -> "Path | None":
    """Issue #1457: `.claude/rules/gherkin-forbidden-words.yaml` を探す."""
    here = Path.cwd().resolve()
    for parent in (here, *here.parents):
        candidate = parent / ".claude" / "rules" / "gherkin-forbidden-words.yaml"
        if candidate.is_file():
            return candidate
    hook_dir = Path(__file__).resolve().parent
    candidate = hook_dir.parent / "rules" / "gherkin-forbidden-words.yaml"
    return candidate if candidate.is_file() else None


def _load_gherkin_forbidden_words() -> list[tuple[str, re.Pattern[str]]]:
    """Issue #1457: `.claude/rules/gherkin-forbidden-words.yaml` を読んで (id, regex) のリストを返す.

    stdlib のみで簡易 YAML パース。
    """
    yaml_path = _find_gherkin_forbidden_yaml()
    if not yaml_path or not yaml_path.is_file():
        return []
    try:
        text = yaml_path.read_text(encoding="utf-8")
    except OSError:
        return []
    entries: list[tuple[str, re.Pattern[str]]] = []
    # 各エントリは `- id: xxx` 行の次に `regex: '...'` を持つ
    for match in re.finditer(
        r"^\s*-\s+id:\s*(\w+)\s*\n\s+regex:\s*'([^']+)'",
        text,
        re.MULTILINE,
    ):
        marker_id = match.group(1)
        raw_regex = match.group(2)
        try:
            entries.append((marker_id, re.compile(raw_regex, re.MULTILINE)))
        except re.error:
            continue
    return entries


def _check_gherkin_forbidden_words(body: str) -> list[str]:
    """Issue #1457: `## 振る舞い` の Then 句が gherkin-forbidden-words.yaml の禁止語を含むかチェック.

    Then 句のみを対象とする（Given / When は対象外）。
    禁止語にヒットしたら「禁止語 '<word>' が Then 句に含まれています」のエラー文字列を返す。
    """
    if "## 振る舞い" not in body:
        return []
    forbidden = _load_gherkin_forbidden_words()
    if not forbidden:
        return []

    behavior_match = re.search(
        r"##\s*振る舞い\s*(.*?)(?=^##|\Z)", body, re.MULTILINE | re.DOTALL
    )
    if not behavior_match:
        return []
    section = behavior_match.group(1)

    # Scenario ごとに分割し Then / And / But 継続行を抽出
    scenarios = re.split(r"^\s*Scenario:", section, flags=re.MULTILINE)
    if len(scenarios) <= 1:
        return []

    errors: list[str] = []
    for i, sc in enumerate(scenarios[1:], start=1):
        then_lines: list[str] = []
        in_then_block = False
        for line in sc.split("\n"):
            stripped = line.strip()
            if re.match(r"^Then\s+", stripped):
                in_then_block = True
                then_lines.append(stripped)
            elif re.match(r"^(Given|When)\s+", stripped):
                in_then_block = False
            elif in_then_block and re.match(r"^(And|But)\s+", stripped):
                then_lines.append(stripped)
        combined = "\n".join(then_lines)
        if not combined:
            continue
        for marker_id, pattern in forbidden:
            m = pattern.search(combined)
            if m:
                matched_word = m.group(1) if m.groups() else m.group(0)
                errors.append(
                    f"Scenario {i}: Then 句に主観表現 '{matched_word}' が含まれています "
                    f"(id={marker_id})。\n"
                    "  Then 句には「何が起きたか」を観測できる具体的な値を書いてください。\n\n"
                    "  NG（主観表現）: Then 正しく動く / Then うまくいく / Then 期待通り\n"
                    "  OK（観測可能）: Then exit code 0 で終了する\n"
                    '               Then stderr に "Error:" が出力される\n'
                    "               Then ~/.cache/foo.json が作成される"
                )
                break  # 1 Scenario につき最初のヒットのみ報告
    return errors


def _check_gherkin_positive_markers(body: str) -> list[str]:
    """`## 振る舞い` セクションの各 Scenario の Then 句が観測要素を含むかチェック.

    Issue #1305: positive list 方式（観測要素の regex 列にマッチしないと NG）。
    """
    # Issue #1328: 行頭アンカーで inline code 内の言及を除外
    if not BEHAVIOR_SECTION_HEADER_RE.search(body):
        return []
    markers, denylist, examples = _load_gherkin_markers_yaml()
    if not markers:
        return []  # YAML 未配置ならフェイルオープン

    # ## 振る舞い セクションを抽出（行頭アンカー付き）
    behavior_match = BEHAVIOR_SECTION_BODY_RE.search(body)
    if not behavior_match:
        return []
    section = behavior_match.group(1)

    # Scenario ごとに分割
    scenarios = re.split(r"^\s*Scenario:", section, flags=re.MULTILINE)
    if len(scenarios) <= 1:
        return []

    errors: list[str] = []
    for i, sc in enumerate(scenarios[1:], start=1):
        # Then 句と、その直後の And/But 継続行のみを抽出（Given/When 配下の And/But は含めない）
        rows: list[str] = []
        in_then_block = False
        for line in sc.split("\n"):
            stripped = line.strip()
            if re.match(r"^Then\s+", stripped):
                in_then_block = True
                rows.append(stripped)
            elif re.match(r"^(Given|When)\s+", stripped):
                in_then_block = False
            elif in_then_block and re.match(r"^(And|But)\s+", stripped):
                rows.append(stripped)
        if not rows:
            errors.append(
                f"Scenario {i}: Then 句がありません。観測可能な結果を Then/And 句で記述してください"
            )
            continue
        combined = "\n".join(rows)

        # 主観表現チェック（denylist にヒットしたら明確に NG）
        for word in denylist:
            if word in combined:
                errors.append(
                    f"Scenario {i}: Then 句に主観表現 '{word}' が含まれています。\n"
                    "  Then 句には「何が起きたか」を観測できる具体的な値を書いてください。\n\n"
                    "  NG（主観表現）: Then 正しく動く / Then うまくいく / Then 期待通り\n"
                    "  OK（観測可能）: Then exit code 0 で終了する\n"
                    '               Then stderr に "Error:" が出力される\n'
                    "               Then ~/.cache/foo.json が作成される"
                )
                break
        else:
            # 観測要素チェック（positive list のいずれかにマッチする必要）
            if not any(m.search(combined) for m in markers):
                bulleted = "\n".join(examples[:5])
                errors.append(
                    f"Scenario {i}: Gherkin 品質: Then 句に観測要素（exit code / stderr / "
                    "ファイル状態等）を含めてください。"
                    "観測可能な要素の例:\n" + bulleted
                )

    return errors


def _extract_type_from_title(title: str) -> str:
    """タイトルから type を抽出（'🤖 feat: xxx' / 'feat: xxx' の両方に対応）."""
    m = re.match(rf"^(?:🤖 )?({VALID_TYPES_RE}):", title or "")
    return m.group(1) if m else ""


def _run_issue_quality_check(body: str, issue_type: str) -> list[str]:
    """tidd issue-quality-check をサブプロセスで呼び、FAIL 時はエラーメッセージを返す.

    **Issue #1301 で意味判定は `/issue-review` skill に外出しされ、本サブコマンドは常に
    fallback PASS を返す no-op になった。** hook からは互換性のため呼び続けるが、
    セッション経路別に skip 警告を出す。

    以下の場合は空リストを返して既存挙動を維持する（フェイルオープン）:
    - `tidd` コマンドが PATH にない（未 install 環境）
    - subprocess がタイムアウト
    - stdout が JSON parse 不能
    - JSON が verdict=PASS または fallback=true
    - ``VALIDATE_ISSUE_QUALITY_CHECK`` 環境変数が ``0`` に設定されている
    """
    if os.environ.get("VALIDATE_ISSUE_QUALITY_CHECK") == "0":
        return []

    # Issue #1301: 意味判定は /issue-review skill に外出しされた
    # Claude Code セッション内: /issue-next / /issue-review で意味判定が別途走る
    # セッション外: 静的チェックのみで通し、explicit skip 警告を出して即 return
    if os.environ.get("CLAUDECODE") != "1":
        sys.stderr.write(
            "validate-issue.py: quality check skipped (outside Claude Code session). "
            "意味判定は /issue-review skill 経由で実施してください。\n"
        )
        return []

    tidd_bin = shutil.which("tidd")
    if tidd_bin is None:
        return []

    try:
        proc = subprocess.run(
            [tidd_bin, "issue-quality-check", "--type", issue_type or "feat"],
            input=body,
            capture_output=True,
            text=True,
            check=False,
            timeout=QUALITY_CHECK_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return []

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []

    if data.get("fallback"):
        return []
    if data.get("verdict") != "FAIL":
        return []

    messages: list[str] = []
    pain_reason = data.get("pain_reason") or "Pain の記述が不十分です"
    if data.get("pain_score", 3) < 3:
        messages.append(f"Pain 記述が不十分です: {pain_reason}")
    gherkin_issues = data.get("gherkin_issues") or []
    for issue in gherkin_issues:
        messages.append(f"Gherkin 品質: {issue}")
    if not messages:
        messages.append(
            "Issue 品質チェックが FAIL を返しました。Pain と Gherkin を見直してください"
        )
    return messages


def _main_impl() -> int:
    raw_input = sys.stdin.read()
    try:
        data = json.loads(raw_input) if raw_input.strip() else {}
    except json.JSONDecodeError:
        return 0
    if not isinstance(data, dict):
        return 0

    # Issue #1364: PreToolUse schema で validate（不一致で exit 2）
    _lib_dir = Path(__file__).resolve().parent / "_lib"
    if str(_lib_dir) not in sys.path:
        sys.path.insert(0, str(_lib_dir))
    try:
        from validate_payload import (  # type: ignore[import-not-found]
            PayloadValidationError,
            validate_payload,
        )

        validate_payload(data, "PreToolUse")
    except ImportError:
        pass
    except PayloadValidationError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0
    command = str(tool_input.get("command") or "")
    if not command:
        return 0

    if not GH_COMMAND_GUARD_RE.search(command):
        return 0

    fragments = _split_shell_fragments(command)
    all_errors: list[str] = []
    found_any = False
    for fragment in fragments:
        frag = fragment.strip()
        if GH_FRAG_RE.search(frag):
            found_any = True
            all_errors.extend(_validate_gh_fragment(frag))
        elif GH_EDIT_FRAG_RE.search(frag):
            found_any = True
            all_errors.extend(_validate_gh_edit_fragment(frag))

    if not found_any:
        return 0

    if all_errors:
        sys.stderr.write(
            "Blocked: gh issue create / gh issue edit に以下の不備があります:\n"
        )
        for err in all_errors:
            sys.stderr.write(f"  - {err}\n")
        sys.stderr.write("\n修正して再実行してください。\n")
        sys.stderr.write("詳細: docs/reference/hooks.md#validate-issuepy\n")
        return 2

    return 0


def main() -> int:
    # Issue #1633: hook 機能別 on/off（_lib を明示的に sys.path へ追加）
    _lib_dir_1633 = Path(__file__).resolve().parent / "_lib"
    if str(_lib_dir_1633) not in sys.path:
        sys.path.insert(0, str(_lib_dir_1633))
    try:
        from hook_io import is_hook_enabled as _is_hook_enabled_1633  # type: ignore[import-not-found]

        if not _is_hook_enabled_1633("validate-issue"):
            return 0
    except ImportError:
        pass  # hook_io が存在しない場合は無視（前方互換）

    return _main_impl()


if __name__ == "__main__":
    sys.exit(main())
