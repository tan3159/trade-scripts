"""PR size XXL 自動 REQUEST_CHANGES gate（Issue #1296 / #3996）.

追加行数 >= 1000 の PR は自動で REQUEST_CHANGES 判定にする。
監査 §4-2 の指摘: 500 行超 PR が 33% / 最大 2,167 行という状態への「作った後」の是正機構。

**使い方:**

ai-review の main フローで、バックエンド実行前に ``check_xxl_gate()`` を呼ぶ。
XXL の場合は事前 REQUEST_CHANGES コメントを投稿して exit 1 を返す。

**設計判断（Issue #3996 で label 参照から実 additions 参照へ変更）:**

- `label-pr` hook は PR 作成時にしか走らないため、PR を分割して行数を減らしても
  `size/XXL` ラベルが古いまま残り、ラベルだけを判定根拠にすると分割後もレビューが
  永久に REQUEST_CHANGES になり続ける（実例: 2026-08-17・PR #907）。
- ``gh pr view <N> --json labels,body,additions`` で実 additions を取得し、
  ``additions >= XXL_LINE_THRESHOLD`` を判定根拠にする。ラベルは「ラベルと実 additions が
  乖離している場合の warning 表示」にのみ使う（表示用メタデータの遅延・失敗に判定が
  引きずられないようにする）。
- labels/body は取得できたが additions が取得できなかった場合（古い ``gh`` が
  ``--json additions`` を認識せず ``labels,body`` のみの再試行で成功した場合等）は
  stderr に warning を出し、既存の ``size/XXL`` ラベル参照へフォールバックして判定を継続する。
- XXL でも意図的な巨大 PR（chezmoi 一括同期など）を許容するため PR ボディに
  ``<!-- allow-xxl: <理由> -->``（理由必須。空文字列は無効）マーカーがあれば bypass する。
- gh 未インストール等の環境で PR metadata（labels/body/additions）自体が取得できない場合は
  skip（silent）ではなく WARN + 通過。
- レビュー指摘（PR #4004）: 投稿される ``XXL_REQUEST_CHANGES_MESSAGE`` の文面も
  「PR に size/XXL ラベルが付いています」から実 additions 基準の説明へ改めた
  （``size/M`` ラベルでも実 additions が閾値以上なら block されるため、旧文面は
  ユーザー向け表示が実際の判定根拠とずれていた）。

**Issue #3994:** `tidd pre-flight` の diff-size gate（#3081）と本 gate はどちらも escape hatch
マーカー名・置き場所が食い違い、片方を通過した PR がもう片方で落ちるレビュー手戻りが実測された。
マーカー名は `allow-xxl` に統一し、旧名 `allow-large-pr` は後方互換で受理する。
`XXL_LINE_THRESHOLD` は pre_flight.py 側の diff-size ブロック閾値（1000 行）と値を共有する
単一の真実源として公開する。
レビュー指摘（PR #4006）: マーカーの理由非空チェックが pre_flight._check_diff_size と
非対称だった（空理由でも bypass できてしまっていた）ため、`m.group(1).strip()` で
理由必須を pre_flight 側と揃えた。

stdlib のみ使用。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

_XXL_LABEL = "size/XXL"
# Issue #3994 レビュー指摘: pre_flight._ALLOW_XXL_RE / _ALLOW_LARGE_PR_RE と同じく
# 理由をキャプチャし、空理由（`<!-- allow-xxl: -->`）は bypass 扱いにしない
# （空理由でも一致していた旧 regex は pre_flight 側と非対称だった）。
_ALLOW_XXL_RE = re.compile(r"<!--\s*allow-xxl\s*:\s*((?:(?!-->).)+?)\s*-->")
# Issue #3994: 旧名（pre-flight 側の diff-size gate が使っていたマーカー名）を後方互換で受理する。
_ALLOW_LARGE_PR_LEGACY_RE = re.compile(r"<!--\s*allow-large-pr\s*:\s*((?:(?!-->).)+?)\s*-->")

# Issue #3994: pre_flight.py._DIFF_SIZE_BLOCK_THRESHOLD が本定数を import して参照する
# （2 つの size gate の閾値がドリフトして「片方は通るがもう片方で落ちる」状況を防ぐ）。
# Issue #3996: 実 additions 判定の閾値としても本定数を使う。
XXL_LINE_THRESHOLD = 1000

XXL_REQUEST_CHANGES_MESSAGE = (
    "VERDICT: REQUEST_CHANGES\n\n"
    f"追加行数 {XXL_LINE_THRESHOLD} 行以上（{_XXL_LABEL} 相当）のため自動 REQUEST_CHANGES: "
    "PR を分割してください\n\n"
    "## サマリー\n"
    f"この PR の実追加行数が {XXL_LINE_THRESHOLD} 行以上です"
    f"（判定根拠は {_XXL_LABEL} ラベルではなく `gh pr view --json additions` の実測値。"
    f"実測値が取得できない場合のみ {_XXL_LABEL} ラベルへフォールバックします）。"
    "レビュー負荷が高く、テスト GREEN 粒度も揃わないため、PR を垂直分割してください。\n\n"
    "## 指摘事項\n"
    f"- [HIGH] PR 全体: 実追加行数 {XXL_LINE_THRESHOLD} 行以上のため"
    "自動 REQUEST_CHANGES gate 発火（PR 分割を推奨）\n\n"
    "## 分割ガイド\n"
    "- `docs/reference/pr-splitting-guide.md` の「Phase 内垂直分割の判断基準」を参照\n"
    "- どうしても分割不能な正当理由がある場合は PR ボディに\n"
    "  `<!-- allow-xxl: <理由> -->` マーカーを追加して再実行してください\n"
)


def _run_gh_pr_view(pr_num: str, repo: str, json_fields: str) -> subprocess.CompletedProcess[str] | None:
    """``gh pr view --json <json_fields>`` を実行する。失敗時は None を返す.

    呼び出し自体の失敗（gh 未インストール・タイムアウト・非 0 終了コード）を
    まとめて None として扱う（呼び出し側でのフォールバック判断に使う）。
    """
    try:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_num),
                "--repo",
                repo,
                "--json",
                json_fields,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc


def _fetch_pr_metadata(pr_num: str, repo: str) -> tuple[list[str], str, int | None] | None:
    """PR の label 一覧・body・実 additions を取得する (Issue #3996).

    Returns:
        (labels, body, additions) のタプル。additions は取得できなかった場合 None。
        gh 呼び出し自体（gh 未インストール・API エラー等）が失敗した場合は None を返す。

    レビュー指摘（PR #4004）: 古い ``gh`` バイナリは ``--json labels,body,additions`` の
    ``additions`` フィールドを認識せず、呼び出し全体が非 0 終了コードで失敗する
    （JSON 内で ``additions`` キーだけが欠ける、という穏やかな失敗にはならない）。
    このケースで labels 情報ごと失う（＝ラベル参照へのフォールバックが機能しない）ことを
    防ぐため、``additions`` 込みの呼び出しが失敗したら ``labels,body`` のみで再試行する。
    """
    proc = _run_gh_pr_view(pr_num, repo, "labels,body,additions")
    if proc is None:
        # additions を含む問い合わせ自体が失敗した場合、labels,body のみで再試行し、
        # 少なくともラベル参照へのフォールバックを可能にする。
        proc = _run_gh_pr_view(pr_num, repo, "labels,body")
        if proc is None:
            return None
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        labels = [label.get("name", "") for label in data.get("labels") or []]
        body = data.get("body") or ""
        return labels, body, None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    labels = [label.get("name", "") for label in data.get("labels") or []]
    body = data.get("body") or ""
    additions_raw = data.get("additions")
    additions = additions_raw if isinstance(additions_raw, int) else None
    return labels, body, additions


def _has_allow_xxl_marker(body: str) -> bool:
    """理由必須の allow-xxl マーカー（新名・旧名両対応）が存在するかを判定する.

    Issue #3994 レビュー指摘: 理由が空（空白のみ含む）のマーカーは bypass 扱いにしない
    （`m.group(1).strip()` が非空の場合のみ有効）。
    """
    for marker_re in (_ALLOW_XXL_RE, _ALLOW_LARGE_PR_LEGACY_RE):
        m = marker_re.search(body)
        if m and m.group(1).strip():
            return True
    return False


def check_xxl_gate(pr_num: str, repo: str) -> tuple[bool, str]:
    """size gate が発火すべきかを返す (Issue #1296 / #3996).

    Issue #3996: 判定根拠を ``size/XXL`` ラベルから実 additions
    （``additions >= XXL_LINE_THRESHOLD``）へ変更した。``label-pr`` hook は
    PR 作成時にしか走らずラベルが古いまま残りうるため。

    Args:
        pr_num: PR 番号
        repo: リポジトリ (owner/name)

    Returns:
        (should_block, reason):
          - should_block=True: gate 発火（呼び出し側は REQUEST_CHANGES 出力すべき）
          - should_block=False: 通過（閾値未満か allow-xxl マーカーあり）
          - reason: 呼び出し側のログ用メッセージ
    """
    metadata = _fetch_pr_metadata(pr_num, repo)
    if metadata is None:
        return False, "WARN: size gate: PR metadata 取得失敗のため skip"
    labels, body, additions = metadata
    label_xxl = _XXL_LABEL in labels

    if additions is None:
        # Issue #3996: additions が取得できない場合は既存のラベル参照へフォールバックする。
        print(
            f"WARN: size gate: additions 取得に失敗したため既存の {_XXL_LABEL} ラベル参照へフォールバックします",
            file=sys.stderr,
        )
        if not label_xxl:
            return False, f"size gate: {_XXL_LABEL} ラベルなし・通過"
        if _has_allow_xxl_marker(body):
            return False, "size gate: allow-xxl マーカー検出・通過"
        return True, f"size gate: {_XXL_LABEL} 検出・REQUEST_CHANGES 発火"

    real_xxl = additions >= XXL_LINE_THRESHOLD
    if real_xxl != label_xxl:
        print(
            f"WARN: size gate: ラベル（{_XXL_LABEL if label_xxl else 'なし'}）と実 additions"
            f"（{additions} 行）が乖離しています",
            file=sys.stderr,
        )

    if not real_xxl:
        return False, f"size gate: 実 additions {additions} 行（閾値 {XXL_LINE_THRESHOLD} 未満）・通過"
    if _has_allow_xxl_marker(body):
        return False, "size gate: allow-xxl マーカー検出・通過"
    return True, (f"size gate: 実 additions {additions} 行（閾値 {XXL_LINE_THRESHOLD} 以上）・REQUEST_CHANGES 発火")
