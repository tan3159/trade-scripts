"""`tidd detect-duplicates-batch` ローカル埋め込み pre-filter (Issue #1626).

CPU で動くローカル埋め込みモデル（model2vec または sentence-transformers）で
全 open Issue をベクトル化し、cosine 類似度閾値以上のペアのみを
`duplicate-detector` subagent に渡す 2 段構成の pre-filter を提供する。

**設計方針:**

- モデルは optional 依存（model2vec を第 1 候補・sentence-transformers を第 2 候補）
- ベクトルと updatedAt タイムスタンプを sqlite にキャッシュ（未変更 Issue は再計算しない）
- cosine 類似度閾値（既定 0.85）以上のペアのみ抽出
- 933 件規模では sqlite の線形スキャンで十分（faiss は Issue #1626 の scope 外）

**モデル選択:**

- model2vec: `minishlab/potion-multilingual-128M` 等の多言語静的埋め込みモデル
  - 推論ミリ秒級・依存が軽量・日本語 Issue に対応
- sentence-transformers: SBERT ファミリーの軽量多言語モデル（第 2 候補）
  - model2vec より重いが精度が高い場合あり

stdlib + optional 依存のみ使用。ネットワーク接続不要（モデルは事前ダウンロード済み前提）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tidd_tools.shared import paths

_DEFAULT_MODEL2VEC_MODEL = "minishlab/potion-multilingual-128M"
_DEFAULT_ST_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
# Issue #2950: Path.home() / ".cache" / "tidd" ハードコードを shared/paths.cache_dir() へ統一
# （Windows/macOS でも OS 別の正しいキャッシュ場所に解決される）
_DEFAULT_DB_PATH = paths.cache_dir() / "issue-embeddings.db"
_DEFAULT_THRESHOLD = 0.85


def _cache_db_path() -> Path:
    """環境変数 TIDD_EMBEDDING_DB が設定されていればそのパス、なければデフォルト."""
    override = os.environ.get("TIDD_EMBEDDING_DB")
    if override:
        return Path(override)
    return _DEFAULT_DB_PATH


# --- 埋め込みモデルのロード ---


def _try_load_model2vec() -> Callable[[list[str]], list[list[float]]] | None:
    """model2vec が利用可能なら embed_fn を返す。未インストールなら None."""
    try:
        from model2vec import StaticModel
    except ImportError:
        return None

    model_name = os.environ.get("TIDD_MODEL2VEC_MODEL", _DEFAULT_MODEL2VEC_MODEL)
    try:
        model = StaticModel.from_pretrained(model_name)
    except Exception:
        return None

    def embed(texts: list[str]) -> list[list[float]]:
        embeddings = model.encode(texts)
        # model2vec の encode は numpy 配列または list を返す
        try:
            result: list[list[float]] = embeddings.tolist()
            return result
        except AttributeError:
            return [list(v) for v in embeddings]

    return embed


def _try_load_sentence_transformers() -> Callable[[list[str]], list[list[float]]] | None:
    """sentence-transformers が利用可能なら embed_fn を返す。未インストールなら None."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return None

    model_name = os.environ.get("TIDD_ST_MODEL", _DEFAULT_ST_MODEL)
    try:
        model = SentenceTransformer(model_name)
    except Exception:
        return None

    def embed(texts: list[str]) -> list[list[float]]:
        embeddings = model.encode(texts)
        try:
            result: list[list[float]] = embeddings.tolist()
            return result
        except AttributeError:
            return [list(v) for v in embeddings]

    return embed


def load_embed_fn() -> Callable[[list[str]], list[list[float]]]:
    """利用可能なバックエンドから embed_fn を返す。

    第 1 候補: model2vec
    第 2 候補: sentence-transformers
    両方未インストールなら ImportError を送出する。
    """
    fn = _try_load_model2vec()
    if fn is not None:
        return fn

    fn = _try_load_sentence_transformers()
    if fn is not None:
        return fn

    raise ImportError("ローカル埋め込みモデルが見つかりません。`uv add model2vec` を実行してください")


# --- テキスト構築 ---


def _build_issue_text(issue: dict[str, Any]) -> str:
    """Issue dict からベクトル化用テキストを構築する（タイトル + 本文）."""
    title = str(issue.get("title", ""))
    body = str(issue.get("body", "") or "")
    return f"{title}\n{body}".strip()


# --- cosine 類似度 ---


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """2 つのベクトルの cosine 類似度を返す（ゼロベクトルは 0.0）."""
    dot: float = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a: float = sum(x * x for x in a) ** 0.5
    norm_b: float = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# --- sqlite キャッシュ ---


def open_cache_db(db_path: Path) -> sqlite3.Connection:
    """sqlite キャッシュ DB を開く（存在しなければ作成する）."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS issue_embeddings (
            issue_number INTEGER NOT NULL,
            updated_at   TEXT    NOT NULL,
            vector_json  TEXT    NOT NULL,
            PRIMARY KEY (issue_number, updated_at)
        )
        """
    )
    conn.commit()
    return conn


def get_cached_embedding(
    conn: sqlite3.Connection,
    issue_number: int,
    updated_at: str,
) -> list[float] | None:
    """キャッシュから埋め込みを取得する。キャッシュミスの場合は None。"""
    cursor = conn.execute(
        "SELECT vector_json FROM issue_embeddings WHERE issue_number=? AND updated_at=?",
        (issue_number, updated_at),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    result: list[float] = json.loads(row[0])
    return result


def store_embedding(
    conn: sqlite3.Connection,
    issue_number: int,
    updated_at: str,
    vector: list[float],
) -> None:
    """埋め込みをキャッシュに保存する（upsert）."""
    conn.execute(
        """
        INSERT OR REPLACE INTO issue_embeddings (issue_number, updated_at, vector_json)
        VALUES (?, ?, ?)
        """,
        (issue_number, updated_at, json.dumps(vector)),
    )


# --- キャッシュを使った埋め込み計算 ---


def compute_embeddings_with_cache(
    issues: list[dict[str, Any]],
    db_path: Path,
    embed_fn: Callable[[list[str]], list[list[float]]],
) -> tuple[dict[int, list[float]], dict[str, int]]:
    """Issues リストをベクトル化する（キャッシュ済み Issue は再計算しない）。

    Returns:
        (vectors, stats) where:
          - vectors: {issue_number: vector}
          - stats: {"computed": int, "cached": int}
    """
    conn = open_cache_db(db_path)
    try:
        vectors: dict[int, list[float]] = {}
        to_compute: list[dict[str, Any]] = []

        # キャッシュチェック
        for issue in issues:
            number = int(issue.get("number", 0))
            updated_at = str(issue.get("updatedAt", ""))
            cached = get_cached_embedding(conn, number, updated_at)
            if cached is not None:
                vectors[number] = cached
            else:
                to_compute.append(issue)

        # 未キャッシュ Issue をバッチで埋め込み計算
        if to_compute:
            texts = [_build_issue_text(issue) for issue in to_compute]
            embeddings = embed_fn(texts)
            for issue, vec in zip(to_compute, embeddings, strict=False):
                number = int(issue.get("number", 0))
                updated_at = str(issue.get("updatedAt", ""))
                vectors[number] = vec
                store_embedding(conn, number, updated_at, vec)
            conn.commit()

        stats = {"computed": len(to_compute), "cached": len(issues) - len(to_compute)}
        return vectors, stats
    finally:
        conn.close()


# --- ペアフィルタリング ---


def filter_pairs_by_similarity(
    issues: list[dict[str, Any]],
    vectors: dict[int, list[float]],
    threshold: float,
) -> list[dict[str, Any]]:
    """cosine 類似度が threshold 以上のペアのみ返す。

    Returns:
        [{"a": int, "b": int, "similarity": float}, ...]
        a < b になるよう正規化済み。
    """
    numbers = [int(issue.get("number", 0)) for issue in issues]
    pairs: list[dict[str, Any]] = []

    for i in range(len(numbers)):
        for j in range(i + 1, len(numbers)):
            n_i = numbers[i]
            n_j = numbers[j]
            vec_i = vectors.get(n_i)
            vec_j = vectors.get(n_j)
            if vec_i is None or vec_j is None:
                continue
            sim = cosine_similarity(vec_i, vec_j)
            if sim >= threshold:
                a, b = (n_i, n_j) if n_i < n_j else (n_j, n_i)
                pairs.append({"a": a, "b": b, "similarity": round(sim, 4)})

    return pairs


# --- メインエントリポイント ---


def run_with_embeddings(
    issues: list[dict[str, Any]],
    db_path: Path | None = None,
    threshold: float = _DEFAULT_THRESHOLD,
) -> list[dict[str, Any]]:
    """Issues リストに対して pre-filter を実行し、閾値以上のペアを返す。

    Args:
        issues: gh issue list から取得した Issue 情報のリスト
        db_path: sqlite キャッシュのパス（None の場合はデフォルト）
        threshold: cosine 類似度の閾値（既定 0.85）

    Returns:
        [{"a": int, "b": int, "similarity": float}, ...]

    Raises:
        ImportError: ローカル埋め込みモデルが未インストールの場合
    """
    if db_path is None:
        db_path = _cache_db_path()

    embed_fn = load_embed_fn()

    vectors, stats = compute_embeddings_with_cache(issues, db_path, embed_fn)

    # キャッシュ統計を stdout に出力
    print(f"埋め込み計算: {stats['computed']} 件（キャッシュ済み: {stats['cached']} 件）")

    # ペアフィルタリング
    total_pairs = len(issues) * (len(issues) - 1) // 2
    filtered_pairs = filter_pairs_by_similarity(issues, vectors, threshold)

    # pre-filter 結果を stdout に出力
    print(f"pre-filter: {total_pairs} ペア → {len(filtered_pairs)} ペアに絞り込み")

    return filtered_pairs
