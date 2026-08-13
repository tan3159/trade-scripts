"""Bash コマンド文字列から heredoc 本文の抽出・除去、コマンドチェーンの分割を行う共通ユーティリティ.

Issue #2951: `validate-issue.py` / `ban-ng-words.py` / `block-direct-yaru-tick.py` /
`block-dangerous-git.py` / `require-red-first.py` の 5 hook が heredoc パースを
それぞれ別実装していたため、堅牢性に差が生じ hook 間で判定が不一致になる
（バイパス判定のすり抜け・誤ブロックの温床）問題を解消するために新設した。

Issue #2966: クォート・エスケープを考慮してシェルコマンドをチェーン区切り
（`&&`・`||`・`|`・`;`・改行）で分割する `split_shell_fragments()` は、以前
`validate-issue.py` にのみ quote-aware 実装があり、`ban-hardcoded-repo.py`・
`require-merge-ci-status.py`・`block-dangerous-git.py` は naive な `re.split` を
使っていたため、クォート内の区切り文字（例: `echo "a; b"`）でも誤って分割して
しまう問題（誤検出・見逃しの温床）があった。本モジュールへ集約し全 hook で共有する。

`block-dangerous-git.py` が採用していた 2 パス方式（Issue #2608）を基準に統一する:

1. パス目: `<<DELIM` 形式の heredoc 開始候補を行単位で検出する。
2. パス目: 開始候補ごとに後続行を先読みし、終端デリミタ行
   （行全体が `strip()` した結果デリミタ名と一致する行）が実在する場合のみ
   heredoc として確定する。

この 2 パス方式により、シェル算術式のビットシフト（`(( x << 2 ))`）や
herestring（`<<< word`）のような `<<` を含むが heredoc ではない構文への
誤マッチ（Issue #2608）を避けられる。

stdlib のみ使用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEREDOC_OPEN_RE = re.compile(
    r"<<(?!<)"  # `<<` で始まるが `<<<`（herestring）ではない
    r"-?"  # 省略可能な `-`（`<<-` 形式）
    r"\s*"  # 省略可能な空白
    r"['\"]?(\w+)['\"]?"  # クォートあり/なしのデリミタ名
)


@dataclass(frozen=True)
class HeredocMatch:
    """検出された heredoc 1 件分の情報.

    Attributes:
        delimiter: heredoc の終端デリミタ名（例: `EOF`）。
        body: heredoc 本文（開始行・終端行を含まない）。
        open_start: `<<DELIM` の `<<` が始まる command 内の文字オフセット。
        open_line_start: `<<DELIM` を含む行の先頭の command 内の文字オフセット
            （呼び出し側が「heredoc 開始行のうち `<<` より前の部分」を
            調べたい場合に ``command[open_line_start:open_start]`` で取得できる）。
        end: 終端デリミタ行の直後（改行文字を含まない終端行末尾）の
            command 内の文字オフセット。
    """

    delimiter: str
    body: str
    open_start: int
    open_line_start: int
    end: int


def find_heredocs(command: str) -> list[HeredocMatch]:
    """command 内の全 heredoc を検出する（2 パス方式・終端デリミタ実在確認付き）.

    ネストした heredoc は非対応（シェルの heredoc はネスト不可なので問題ない）。
    終端デリミタが見つからない開始候補（ビットシフト・herestring 等の誤検出含む）は
    heredoc として扱わず無視する（例外は送出しない）。

    Returns:
        command 内に現れる順の ``HeredocMatch`` のリスト（heredoc がなければ空リスト）。
    """
    lines = command.split("\n")
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += (
            len(line) + 1
        )  # +1 は "\n" 分（最終行に無くても後続で参照しないため無害）

    matches: list[HeredocMatch] = []
    i = 0
    while i < len(lines):
        m = _HEREDOC_OPEN_RE.search(lines[i])
        if m:
            delimiter = m.group(1)
            # 先読み: 後続行に終端デリミタ行が実在するかを確認する
            for j in range(i + 1, len(lines)):
                if lines[j].strip() == delimiter:
                    body = "\n".join(lines[i + 1 : j])
                    matches.append(
                        HeredocMatch(
                            delimiter=delimiter,
                            body=body,
                            open_start=offsets[i] + m.start(),
                            open_line_start=offsets[i],
                            end=offsets[j] + len(lines[j]),
                        )
                    )
                    # 次の検索は終端行の次から開始する
                    i = j + 1
                    break
            else:
                # 終端デリミタが見つからない → heredoc ではない（ビットシフト等）
                i += 1
        else:
            i += 1
    return matches


def find_heredoc_body(command: str) -> str | None:
    """command 内で最初に見つかった heredoc の本文を返す.

    heredoc が存在しない場合（終端デリミタ欠如も含む）は ``None`` を返す
    （例外は送出しない）。
    """
    matches = find_heredocs(command)
    return matches[0].body if matches else None


def strip_heredoc_bodies(command: str) -> str:
    """heredoc 本文（終端デリミタ行含む）を除去した文字列を返す（元の command は変更しない）.

    対応形式: `<<DELIM` / `<<'DELIM'` / `<<"DELIM"` / `<<-DELIM` 等。
    開始行（`<<DELIM` を含む行）自体はコマンド部分を含むため残し、
    その次の行から終端デリミタ行までを除去する。

    用途: heredoc 本文に含まれる引用符・危険操作の文字列表記が
    `shlex.split` や正規表現ベースの判定を誤動作させるのを防ぐ
    （判定専用のコピーを返す。元の command 文字列は変更しない）。
    """
    lines = command.split("\n")
    skip_indices: set[int] = set()
    # find_heredocs は文字オフセットベースの結果を返すため、除去対象行の
    # 特定には行インデックスベースで同じ 2 パス走査を行う。
    i = 0
    while i < len(lines):
        m = _HEREDOC_OPEN_RE.search(lines[i])
        if m:
            delimiter = m.group(1)
            for j in range(i + 1, len(lines)):
                if lines[j].strip() == delimiter:
                    for k in range(i + 1, j + 1):
                        skip_indices.add(k)
                    i = j + 1
                    break
            else:
                i += 1
        else:
            i += 1

    result_lines = [line for idx, line in enumerate(lines) if idx not in skip_indices]
    return "\n".join(result_lines)


def split_shell_fragments(command: str) -> list[str]:
    """クォート・エスケープを考慮してシェル演算子（&&, ||, |, ;, 改行）で分割する（Issue #2966）.

    naive な `re.split(r"&&|;|\\|\\||\\|", command)` はクォート内の区切り文字
    （例: `echo "a; b"` の `;`）でも分割してしまうため、シェル演算子はクォート
    （`'...'` / `"..."`）の外にあるものだけを区切りとして扱う。

    - ダブルクォート内のバックスラッシュエスケープ（`\\"` 等）はエスケープとして
      扱い、クォート終端と誤認識しない（シングルクォート内の `\\` は literal）。
    - クォート外のバックスラッシュも次の 1 文字をエスケープとして保護する
      （`\\;` 等が区切り文字として解釈されるのを防ぐ）。
    - 閉じクォートが存在しない不正な文字列（例: `echo "unterminated && rm -rf /`）
      でも例外を送出せず、安全側フォールバックとして分割しない
      （クォート開始以降の文字はすべて 1 フラグメントに含まれる）。

    Returns:
        分割されたフラグメントのリスト（区切り文字が 1 つもなければ要素 1 件）。
    """
    fragments: list[str] = []
    current: list[str] = []
    quote_char: str | None = None
    i = 0
    while i < len(command):
        c = command[i]
        if quote_char:
            if quote_char == '"' and c == "\\" and i + 1 < len(command):
                current.append(c)
                current.append(command[i + 1])
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
        elif c == "\\" and i + 1 < len(command):
            current.append(c)
            current.append(command[i + 1])
            i += 2
        elif command[i : i + 2] in ("&&", "||"):
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
