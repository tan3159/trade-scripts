"""一時領域不足を実装失敗と区別し、unattended 実行時に bounded retry で
`skip-and-later-reselect` を判定する（Issue #4149）.

親 Issue #4145（Epic）の分割 3/3・実装本体。分割 1/3（#4143・`tmp_capacity`）の TMPDIR
切り替え、分割 2/3（#4144・`tmp_cleanup`）の stale artifact 掃除を尽くしても回復できない
一時領域不足は、従来 `classify-test-failure` が判定不能となり、恒久的な人間入力を
必要としない Issue に `🙋 needs-human-input` が付与されて夜間の無停止運用が止まっていた
（mn-scripts Issue #1238 の park 事象）。

**分類:** 一時領域不足かどうかは `tmp_capacity.diagnose()` の `ok` フィールドのみで判定する
（`classify_shortage()`）。pytest のエラーメッセージ文字列は一切解析しない。診断が
`ok=True` であれば、後続の pytest 失敗がどのような理由であっても一時領域不足として
分類せず bounded retry の対象外とする（実装失敗との区別・Scenario 3）。

**bounded retry + backoff:** unattended 実行時のみ、再試行上限（既定 3 回・
`TIDD_TMP_RETRY_MAX_ATTEMPTS`）に達するまで backoff（既定 30 秒・
`TIDD_TMP_RETRY_BACKOFF_SECONDS`）待機してから `tmp_cleanup.recover_capacity()` と
再診断を繰り返す。attended（対話）実行時は #4144 までの挙動（1 回限りの掃除・再診断）を
維持する。

**exit code / JSON 契約:** unattended で再試行上限に到達し回復しなかった場合、
`needs-human-input` を一切付与せず、診断ログ（stderr）へ試行回数・backoff・回復不能理由を
出力したうえで、`EXIT_UNRECOVERABLE`（3）を返し、stdout へ `status: skip-and-later-reselect`
の JSON を 1 行出力する。この契約自体が本 Issue のスコープであり、`issue-next` skill 側の
実際の park 回避配線（ラベルを付けず再選定キューへ戻す）は別 Issue（#4150）で行う。
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from time import sleep as _real_sleep
from typing import TextIO

from tidd_tools import tmp_capacity, tmp_cleanup

#: unattended bounded retry の最大試行回数（初回診断込み）を上書きする環境変数
MAX_ATTEMPTS_ENV = "TIDD_TMP_RETRY_MAX_ATTEMPTS"
#: 再試行前の待機秒数を上書きする環境変数
BACKOFF_SECONDS_ENV = "TIDD_TMP_RETRY_BACKOFF_SECONDS"

_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BACKOFF_SECONDS = 30.0

#: 診断成功（容量十分、または再試行の途中で回復した）
EXIT_OK = 0
#: attended 実行時に単発の掃除・再診断でも回復しなかった（bounded retry 対象外）
EXIT_ATTENDED_FAILURE = 1
#: unattended 実行時に再試行上限へ到達し回復不能（park せず再選定可能・#4149）
EXIT_UNRECOVERABLE = 3

#: 再選定可能を示す stdout JSON の status 値
STATUS_SKIP_AND_LATER_RESELECT = "skip-and-later-reselect"


def _max_attempts_default() -> int:
    raw = os.environ.get(MAX_ATTEMPTS_ENV)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value >= 1:
            return value
    return _DEFAULT_MAX_ATTEMPTS


def _backoff_seconds_default() -> float:
    raw = os.environ.get(BACKOFF_SECONDS_ENV)
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = -1.0
        if value >= 0:
            return value
    return _DEFAULT_BACKOFF_SECONDS


@dataclasses.dataclass(frozen=True)
class RetryOutcome:
    """`diagnose_with_bounded_retry()` の結果."""

    exit_code: int
    diagnosis: tmp_capacity.Diagnosis
    attempts: int
    unattended: bool


def classify_shortage(diagnosis: tmp_capacity.Diagnosis) -> bool:
    """診断結果が一時領域不足に起因するかを分類する（実装失敗と区別する）.

    `tmp_capacity.diagnose()` の `ok` フィールドのみで判定し、pytest のエラー文字列は
    解析しない。`ok=True` であれば常に False（一時領域不足として分類しない）。
    """
    return not diagnosis.ok


def _emit_unrecoverable(stdout: TextIO, diagnosis: tmp_capacity.Diagnosis, *, attempts: int) -> None:
    payload: dict[str, object] = {
        "status": STATUS_SKIP_AND_LATER_RESELECT,
        "reason": diagnosis.reason,
        "attempts": attempts,
    }
    print(json.dumps(payload, ensure_ascii=False), file=stdout)


def diagnose_with_bounded_retry(
    *,
    unattended: bool,
    default_dir: Path | None = None,
    fallback_dir: Path | None = None,
    max_attempts: int | None = None,
    backoff_seconds: float | None = None,
    sleep: Callable[[float], None] = _real_sleep,
    stderr: TextIO | None = None,
    stdout: TextIO | None = None,
) -> RetryOutcome:
    """一時領域不足を分類し、unattended 実行時のみ bounded retry + backoff を行う.

    分類は `tmp_capacity.diagnose()` の `ok` のみに基づく（`classify_shortage()`）。
    `ok=True`（一時領域不足ではない）であれば、実装失敗の有無に関わらず即座に成功として
    返す（`attempts=1`・bounded retry は実行しない・Scenario 3）。

    `unattended=False`（attended）の場合、#4144 までの挙動（`tmp_cleanup.recover_capacity()`
    による 1 回限りの掃除・再診断）のみを行う。`unattended=True` の場合、再試行上限
    （`max_attempts` 省略時は `_max_attempts_default()`）に達するまで `backoff_seconds`
    （省略時は `_backoff_seconds_default()`）秒待機してから掃除・再診断を繰り返す。

    上限到達後も回復しなければ、診断ログ（`stderr`）へ試行回数・backoff・回復不能理由を
    出力し、`stdout` へ `status: skip-and-later-reselect` の JSON を 1 行出力して
    `EXIT_UNRECOVERABLE`（3）を返す。
    """
    out = sys.stderr if stderr is None else stderr
    output = sys.stdout if stdout is None else stdout
    default = default_dir if default_dir is not None else tmp_capacity.default_tmpdir()
    fallback = fallback_dir if fallback_dir is not None else tmp_capacity.fallback_tmpdir()

    diagnosis = tmp_capacity.diagnose(default_dir=default, fallback_dir=fallback, stderr=out)
    if not classify_shortage(diagnosis):
        return RetryOutcome(exit_code=EXIT_OK, diagnosis=diagnosis, attempts=1, unattended=unattended)

    if not unattended:
        tmp_cleanup.recover_capacity(diagnosis.default_dir, fallback, stderr=out)
        diagnosis = tmp_capacity.diagnose(default_dir=default, fallback_dir=fallback, stderr=out)
        exit_code = EXIT_OK if diagnosis.ok else EXIT_ATTENDED_FAILURE
        return RetryOutcome(exit_code=exit_code, diagnosis=diagnosis, attempts=1, unattended=unattended)

    limit = max_attempts if max_attempts is not None else _max_attempts_default()
    wait = backoff_seconds if backoff_seconds is not None else _backoff_seconds_default()
    attempts = 1
    while attempts < limit:
        print(
            f"==> 一時領域不足のため再試行します（{attempts}/{limit} 回目・"
            f"backoff {wait:.0f}秒・理由: {diagnosis.reason}）",
            file=out,
        )
        sleep(wait)
        tmp_cleanup.recover_capacity(diagnosis.default_dir, fallback, stderr=out)
        attempts += 1
        diagnosis = tmp_capacity.diagnose(default_dir=default, fallback_dir=fallback, stderr=out)
        if diagnosis.ok:
            return RetryOutcome(exit_code=EXIT_OK, diagnosis=diagnosis, attempts=attempts, unattended=unattended)

    print(
        f"ERROR: 一時領域不足が再試行上限（{limit}回）到達後も回復しません"
        f"（試行回数={attempts}・backoff {wait:.0f}秒・理由: {diagnosis.reason}）",
        file=out,
    )
    _emit_unrecoverable(output, diagnosis, attempts=attempts)
    return RetryOutcome(exit_code=EXIT_UNRECOVERABLE, diagnosis=diagnosis, attempts=attempts, unattended=unattended)
