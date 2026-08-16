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
  - #2573: Issue 本文のシークレット検知（定番プレフィックス + 高エントロピー文字列）
    - 定番プレフィックス: ghp_・github_pat_・sk-・AKIA + 英数 16 文字 等
    - 高エントロピー文字列: Base64 / hex 40 文字超のトークン様文字列（Shannon エントロピー閾値）
    - escape hatch: <!-- allow-secret-pattern: <理由> --> で False positive をバイパス（理由必須）
"""

from __future__ import annotations

import json  # gh issue view の JSON stdout を parse するため
import math
import os
import re
import shlex
import shutil  # `gh` バイナリの PATH 解決
import subprocess  # gh issue view サブプロセス呼び出し（#1581 の本文取得フォールバック）
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

BODY_FILE_SIZE_LIMIT = 1024 * 1024  # 1MB

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
# Issue #2953: セクション本文抽出は `_lib.yaru_sections.extract_section`（`_extract_yaru_section`
# として利用）に集約した。Issue #2998: matcher: "Bash" で全 Bash コマンドに反応する本 hook
# では import 時 subprocess コスト（`_lib.gh_cache` 経由）を避けるため、実際に使う
# 関数内でのみ遅延 import する（モジュールトップレベルでは import しない）。
# Section presence check: 行頭 `## <name>` から始まるセクションを検知する（inline code は除外）。
# body 抽出側（extract_section・BEHAVIOR_SECTION_BODY_RE）と `##` 直後の空白許容度を揃える。
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

# Issue #2573: シークレット検知
# 定番プレフィックス regex: プレフィックスに続く英数字・特殊文字 8 文字以上でマッチ
_SECRET_PREFIX_RE = re.compile(
    r"""
    (?:
        ghp_[A-Za-z0-9_]{8,}               # GitHub Personal Access Token (Classic)
        | github_pat_[A-Za-z0-9_]{8,}       # GitHub Personal Access Token (Fine-grained)
        | ghs_[A-Za-z0-9]{8,}              # GitHub Server-to-server token
        | ghr_[A-Za-z0-9]{8,}              # GitHub Refresh token
        | sk-[A-Za-z0-9\-]{20,}            # OpenAI / Anthropic API key (sk- / sk-proj- / sk-ant- 等・#2601)
        | AKIA[A-Z0-9]{16}                 # AWS Access Key ID (exactly 4+16=20 chars)
        | AGPA[A-Z0-9]{16}                 # AWS Access Key ID variant
        | AIDA[A-Z0-9]{16}                 # AWS Access Key ID variant
        | AROA[A-Z0-9]{16}                 # AWS Access Key ID variant
        | ASCA[A-Z0-9]{16}                 # AWS Access Key ID variant
        | ASIA[A-Z0-9]{16}                 # AWS Temporary credentials
        | xox[bpars]-[A-Za-z0-9\-]{10,}   # Slack token
        | eyJ[A-Za-z0-9_\-]{20,}           # JWT token (base64url JSON header)
    )
    """,
    re.VERBOSE,
)

# escape hatch: <!-- allow-secret-pattern: <理由> --> で False positive をバイパス
# 理由が空（スペースのみも含む）の場合はバイパス無効
# single_line=True（DOTALL 使わない）: 理由なしコメントの `-->` を飛び越えて後続コメントまで
# マッチすると「理由あり」と誤判定してバイパスが誤許可されるため（1 行完結に限定する・Issue #2954）
_ALLOW_SECRET_PATTERN_MARKER = "allow-secret-pattern"

# 高エントロピー文字列検知用 regex（Base64 / hex 41 文字以上）
# - Base64: A-Za-z0-9+/= の 41 文字以上の連続文字列
# - hex 41 文字以上: 0-9a-fA-F の 41 文字以上（git SHA 40 文字ちょうどは除外）
# 境界値: hex 40 文字ちょうど（git SHA）は除外するため 41 文字以上に設定
_HIGH_ENTROPY_CANDIDATE_RE = re.compile(
    r"""
    (?:
        [A-Za-z0-9+/]{41,}={0,2}           # Base64 (41+ chars, optional padding)
        | [0-9a-fA-F]{41,}                  # Hex (41+ chars, excludes git SHA=40)
    )
    """,
    re.VERBOSE,
)

# Shannon エントロピー閾値（detect-secrets 標準値を参考）
_ENTROPY_THRESHOLD_BASE64 = 4.5  # Base64 アルファベット（64 文字）での閾値
_ENTROPY_THRESHOLD_HEX = 3.0  # Hex アルファベット（16 文字）での閾値


# Issue #1578: --body "$(cat <<'EOF'\n...\nEOF\n)" パターンの heredoc body を直接抽出する。
# shlex.split は $(cat ...) の内部にある二重引用符を区切り文字と誤解釈して body を truncate する。
# heredoc 構文自体のパース（終端デリミタの実在確認・境界値処理）は Issue #2951 で
# `_lib/shell_parse.find_heredoc_body` へ集約した。ここでは「--body に紐づく heredoc の
# 開始位置」を特定するアンカーのみを扱う（`-b` も許容）。
_BODY_HEREDOC_ANCHOR_RE = re.compile(r"""(?:--body|-b)\s+"?\$\(cat\s+<<""")


def _extract_heredoc_body(fragment: str) -> str | None:
    """--body "$(cat <<'EOF'\n...\nEOF\n)" パターンから heredoc body を直接抽出する.

    shlex.split は $(cat ...) の内側を解釈しないため、heredoc body 内の二重引用符で
    --body トークンが途中打ち切りされる (#1578)。heredoc 構文のパース自体は
    `_lib/shell_parse.find_heredoc_body`（2 パス方式・#2951）に委譲する。

    Returns:
        heredoc body 文字列（マッチしない場合は None）。
    """
    anchor = _BODY_HEREDOC_ANCHOR_RE.search(fragment)
    if anchor is None:
        return None
    _lib_dir = Path(__file__).resolve().parent / "_lib"
    if str(_lib_dir) not in sys.path:
        sys.path.insert(0, str(_lib_dir))
    from shell_parse import find_heredoc_body  # type: ignore[import-not-found]

    return find_heredoc_body(fragment[anchor.start() :])


def _split_shell_fragments(s: str) -> list[str]:
    """クォートを考慮してシェル演算子（&&, ||, |, ;, 改行）で分割する.

    Issue #2966: 実装は `_lib/shell_parse.split_shell_fragments` へ集約した
    （`ban-hardcoded-repo.py`・`require-merge-ci-status.py`・`block-dangerous-git.py`
    と共有する）。本関数は呼び出し元互換のための薄いラッパー。
    """
    _lib_dir = Path(__file__).resolve().parent / "_lib"
    if str(_lib_dir) not in sys.path:
        sys.path.insert(0, str(_lib_dir))
    from shell_parse import split_shell_fragments  # type: ignore[import-not-found]

    return split_shell_fragments(s)


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

    # Issue #2573: シークレット検知（本文が確定した後に最初に実行する）
    if body:
        errors.extend(_check_secret_patterns(body))

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

    if FEAT_TITLE_RE.match(title or "") and not DESIGN_SECTION_RE.search(body):
        errors.append(
            'feat 系 Issue は "## 設計の選択肢" セクションが必要です'
            "（採用案・不採用案を明記してください）"
        )

    if (
        FEAT_FIX_TITLE_RE.match(title or "")
        # Issue #1855: hook 契約系 Issue（やることが .claude/hooks/ のみ）は Gherkin 不要
        and not _is_hook_contract_only_issue(body)
        and not BEHAVIOR_SECTION_HEADER_RE.search(body)
    ):
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
    # Issue #2998 レビュー指摘: `_lib.yaru_sections` は `_lib.gh_cache` を経由し
    # import 時に `git remote get-url origin` を実行するため、matcher: "Bash" で
    # あらゆる Bash コマンドに反応する本 hook では、この関数（GH_COMMAND_GUARD_RE
    # 一致後にのみ到達する）でのみ遅延 import する。
    from _lib.yaru_sections import extract_section as _extract_yaru_section

    todo_section = _extract_yaru_section(body)
    if todo_section is not None:
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

    # Issue #2956: 静的な Gherkin 検査は上記 2 チェックで hook 内完結済みのため、
    # tidd issue-quality-check への subprocess 委譲は廃止した（セッション外の reminder のみ残す）。
    if not errors and body:
        quality_errors = _run_issue_quality_check()
        errors.extend(quality_errors)

    return errors


def _validate_gh_edit_fragment(gh_edit_fragment: str) -> list[str]:
    """gh issue edit フラグメントを検証してエラーリストを返す（Issue #1581）.

    🙋 needs-human-input ラベルが --add-label に含まれる場合のみ、
    本文に ## 判断してほしいこと セクションがあるかを確認する。

    Issue #2602: --body / --body-file による本文更新には、ラベルの有無に
    かかわらず _check_secret_patterns によるシークレット検知を適用する。

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

    # 本文を取得（--body > --body-file。gh issue view フォールバックは後段）
    body = ""
    body_file_error = False
    if body_inline:
        body = body_inline
    elif body_file:
        try:
            file_size = os.path.getsize(body_file)
            if file_size <= BODY_FILE_SIZE_LIMIT:
                with open(body_file, "r", encoding="utf-8") as f:
                    body = f.read()
        except OSError:
            body_file_error = True  # 読み込み失敗はフェイルオープン

    # Issue #2602: --body / --body-file による本文更新にシークレット検知を適用する
    # （create 経路と同じ _check_secret_patterns を再利用。escape hatch も共通）
    errors: list[str] = []
    if body:
        errors.extend(_check_secret_patterns(body))

    # needs-human-input ラベルが --add-label に含まれない場合は以降のチェック不要
    has_needs_human_input = any(lbl == NEEDS_HUMAN_INPUT_LABEL for lbl in add_labels)
    if not has_needs_human_input:
        return errors

    if body_file_error:
        return errors  # body-file 読み込み失敗はフェイルオープン

    if not body_inline and not body_file:
        # --body / --body-file がない場合: gh issue view で現在の本文を取得
        if not issue_num or not issue_num.isdigit():
            return errors  # Issue 番号がなければフェイルオープン

        gh_bin = shutil.which("gh")
        if gh_bin is None:
            return errors  # gh が PATH にない場合はスキップ

        try:
            proc = subprocess.run(
                [gh_bin, "issue", "view", issue_num, "--json", "body"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=GH_VIEW_TIMEOUT_SEC,
            )
        except (subprocess.TimeoutExpired, OSError):
            return errors  # タイムアウト / エラーはフェイルオープン

        if proc.returncode != 0:
            return errors  # gh issue view 失敗はフェイルオープン

        try:
            view_data = json.loads(proc.stdout or "{}")
            body = view_data.get("body") or ""
        except (json.JSONDecodeError, AttributeError):
            return errors

    if not body or not DECISION_SECTION_RE.search(body):
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

    return errors


def _shannon_entropy(s: str) -> float:
    """Shannon エントロピーを計算する（bits per character）.

    detect-secrets と同じ計算方式: H = -sum(p * log2(p)) for each unique char.
    空文字列は 0.0 を返す。
    """
    if not s:
        return 0.0
    length = len(s)
    freq: dict[str, int] = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    return -sum((count / length) * math.log2(count / length) for count in freq.values())


def _is_high_entropy_string(candidate: str) -> bool:
    """文字列が高エントロピーかどうかを判定する.

    Hex 文字セット（0-9a-fA-F）のみで構成 → Hex 閾値で判定。
    Base64 文字セット（A-Za-z0-9+/=）のみで構成 → Base64 閾値で判定。
    それ以外は False（対象外）。

    Hex は Base64 の部分集合のため必ず先に判定する。Base64 閾値（4.5）は
    hex アルファベットの最大エントロピー log2(16)=4.0 を超えており、
    Base64 判定を先にすると hex シークレットが絶対にブロックされなくなる。
    """
    # Hex（大文字小文字問わず）— Base64 より先に判定する
    if re.fullmatch(r"[0-9a-fA-F]+", candidate):
        entropy = _shannon_entropy(candidate.lower())
        return entropy >= _ENTROPY_THRESHOLD_HEX

    # Base64: padding = を除いた本体で判定
    stripped = candidate.rstrip("=")
    if re.fullmatch(r"[A-Za-z0-9+/]+", stripped):
        entropy = _shannon_entropy(stripped)
        return entropy >= _ENTROPY_THRESHOLD_BASE64

    return False


def _check_secret_patterns(body: str) -> list[str]:
    """Issue 本文にシークレットの可能性がある文字列が含まれるかチェックする（Issue #2573）.

    escape hatch:
      本文に `<!-- allow-secret-pattern: <理由> -->` が含まれ、かつ理由が空でなければバイパスする。
      理由が空の場合はバイパス無効（ブロックされる）。

    検知項目:
      1. 定番プレフィックス（ghp_・github_pat_・sk-・AKIA 等）
      2. 高エントロピー文字列（Base64 / hex 41 文字以上でエントロピー閾値超）

    Returns:
        エラーメッセージのリスト（空リスト = 検知なし）。
    """
    # escape hatch: 理由付きの allow-secret-pattern があればバイパス（single_line・Issue #2954）
    _lib_dir = Path(__file__).resolve().parent / "_lib"
    if str(_lib_dir) not in sys.path:
        sys.path.insert(0, str(_lib_dir))
    try:
        from override_markers import (  # type: ignore[import-not-found]
            has_override_marker,
        )

        if has_override_marker(body, _ALLOW_SECRET_PATTERN_MARKER, single_line=True):
            return []
    except ImportError:
        pass
    # 理由が空 / marker なしの場合はバイパス無効（後続チェックを継続）

    matches: list[str] = []

    # 1. 定番プレフィックス検知
    for m in _SECRET_PREFIX_RE.finditer(body):
        matched = m.group(0)
        # マスク表示: 先頭 8 文字 + *** + 末尾 4 文字（ただし短い場合は全マスク）
        if len(matched) > 12:
            masked = matched[:8] + "***" + matched[-4:]
        else:
            masked = matched[:4] + "***"
        matches.append(f"定番プレフィックス一致: {masked}")

    # 2. 高エントロピー文字列検知
    for m in _HIGH_ENTROPY_CANDIDATE_RE.finditer(body):
        candidate = m.group(0)
        if _is_high_entropy_string(candidate):
            # マスク表示
            if len(candidate) > 12:
                masked = candidate[:8] + "***" + candidate[-4:]
            else:
                masked = candidate[:4] + "***"
            matches.append(f"高エントロピー文字列: {masked}")

    if not matches:
        return []

    # エラーメッセージ（検知した文字列自体はそのまま出力しない）
    lines = ["シークレットの可能性がある文字列が本文に含まれています:"]
    for item in matches:
        lines.append(f"    {item}")
    lines.append(
        "  秘匿情報を削除またはマスクした上で再実行してください。\n"
        "  誤検知の場合は本文に以下を追加してください:\n"
        "    <!-- allow-secret-pattern: <理由> -->"
    )
    return ["\n".join(lines)]


def _is_hook_contract_only_issue(body: str) -> bool:
    """Issue #1855: hook 契約系 Issue かどうかを判定する.

    `## やること` に記載されたファイルパスが `.claude/hooks/` のみを含み、
    他のパス（projects/, docs/, .github/, .circleci/ 等）が混在しない場合に True を返す。
    ファイルパスの記述が一切ない Issue は False（汎用 Issue として除外しない）。
    """
    from _lib.yaru_sections import extract_section as _extract_yaru_section

    todo_section = _extract_yaru_section(body)
    if todo_section is None:
        return False
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


def _find_gherkin_markers_yaml() -> Path | None:
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


def _check_gherkin_forbidden_words(body: str) -> list[str]:
    """Issue #1457: `## 振る舞い` の Then 句が gherkin-forbidden-words.yaml の禁止語を含むかチェック.

    Then 句のみを対象とする（Given / When は対象外）。
    禁止語にヒットしたら「禁止語 '<word>' が Then 句に含まれています」のエラー文字列を返す。

    Issue #2956: Then/And 継続行抽出・禁止語ローダーは `_lib/gherkin_check.py` に一本化した
    （旧: `_load_gherkin_forbidden_words` が example_bad/example_good 非対応の劣化コピーだった）。
    """
    if "## 振る舞い" not in body:
        return []
    from _lib.gherkin_check import (
        extract_then_lines,
        find_forbidden_word,
        load_forbidden_words,
        split_scenarios,
    )

    forbidden = load_forbidden_words()
    if not forbidden:
        return []

    behavior_match = re.search(
        r"##\s*振る舞い\s*(.*?)(?=^##|\Z)", body, re.MULTILINE | re.DOTALL
    )
    if not behavior_match:
        return []
    section = behavior_match.group(1)

    scenarios = split_scenarios(section)
    if len(scenarios) <= 1:
        return []

    errors: list[str] = []
    for i, sc in enumerate(scenarios[1:], start=1):
        combined = "\n".join(extract_then_lines(sc))
        hit = find_forbidden_word(combined, forbidden)
        if hit is None:
            continue
        errors.append(
            f"Scenario {i}: Then 句に主観表現 '{hit.matched_word}' が含まれています "
            f"(id={hit.marker_id})。\n"
            "  Then 句には「何が起きたか」を観測できる具体的な値を書いてください。\n\n"
            "  NG（主観表現）: Then 正しく動く / Then うまくいく / Then 期待通り\n"
            "  OK（観測可能）: Then exit code 0 で終了する\n"
            '               Then stderr に "Error:" が出力される\n'
            "               Then ~/.cache/foo.json が作成される"
        )
    return errors


def _check_gherkin_positive_markers(body: str) -> list[str]:
    """`## 振る舞い` セクションの各 Scenario の Then 句が観測要素を含むかチェック.

    Issue #1305: positive list 方式（観測要素の regex 列にマッチしないと NG）。
    Issue #2956: Then/And 継続行抽出は `_lib/gherkin_check.py` に一本化した
    （`_check_gherkin_forbidden_words` との 2 重ループを解消）。
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

    from _lib.gherkin_check import extract_then_lines, split_scenarios

    # Scenario ごとに分割
    scenarios = split_scenarios(section)
    if len(scenarios) <= 1:
        return []

    errors: list[str] = []
    for i, sc in enumerate(scenarios[1:], start=1):
        # Then 句と、その直後の And/But 継続行のみを抽出（Given/When 配下の And/But は含めない）
        rows = extract_then_lines(sc)
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


def _run_issue_quality_check() -> list[str]:
    """Issue #2956: 静的な Gherkin 検査（禁止語・positive markers）は既に
    `_check_gherkin_forbidden_words`/`_check_gherkin_positive_markers` で hook 内完結しているため、
    `tidd issue-quality-check` への subprocess 委譲を廃止した。

    **Issue #1301 で意味判定（Pain 深さ・Gherkin の検証可能性）は `/issue-review` skill に
    外出し済み。** Claude Code セッション外（CI / cron / 素の bash 起動）では `/issue-review`
    が使えないため、その旨を skip 警告として stderr に出力するだけの no-op になる
    （常に空リストを返す＝この関数がエラーを生成することはない）。

    Issue #2956: セッション判定は `os.environ.get("CLAUDECODE")` の直書きをやめ、
    共有ヘルパー `_lib/session_detector.is_claude_code_session()` に統一した。
    """
    if os.environ.get("VALIDATE_ISSUE_QUALITY_CHECK") == "0":
        return []

    from _lib.session_detector import is_claude_code_session

    if not is_claude_code_session():
        sys.stderr.write(
            "validate-issue.py: quality check skipped (outside Claude Code session). "
            "意味判定は /issue-review skill 経由で実施してください。\n"
        )
    return []


def _main_impl() -> int:
    # Issue #2957: stdin 読み取りを hook_io.read_hook_input へ集約（schema 不一致は exit 2）。
    _lib_dir = Path(__file__).resolve().parent / "_lib"
    if str(_lib_dir) not in sys.path:
        sys.path.insert(0, str(_lib_dir))
    from hook_io import (
        read_hook_input as _read_hook_input,  # type: ignore[import-not-found]
    )

    data = _read_hook_input(hook_name="PreToolUse")

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
        from hook_io import (
            is_hook_enabled as _is_hook_enabled_1633,  # type: ignore[import-not-found]
        )

        if not _is_hook_enabled_1633("validate-issue"):
            return 0
    except ImportError:
        pass  # hook_io が存在しない場合は無視（前方互換）

    return _main_impl()


if __name__ == "__main__":
    sys.exit(main())
